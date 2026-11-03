#!/usr/bin/env python3
"""
monitor.py  —  Team Imperium  |  Caterpillar Tech Challenge
===========================================================
Serial bridge + terminal dashboard + live GUI plotter.

USAGE
-----
    # Test without ESP32 (reads dataset, simulates ESP32 responses)
    python monitor.py --file dataset.xlsx --dry-run

    # With real ESP32
    python monitor.py --file dataset.xlsx --port /dev/ttyUSB0

    # Faster playback (10× real-time)
    python monitor.py --file dataset.xlsx --port /dev/ttyUSB0 --speed 10

    # With CSV logging
    python monitor.py --file dataset.xlsx --dry-run --logfile session.csv

    # Show raw packets (debugging)
    python monitor.py --file dataset.xlsx --dry-run --verbose

    # Terminal dashboard only, no GUI window (e.g. over SSH)
    python monitor.py --dry-run --no-gui

LIVE GUI PLOT WINDOW
--------------------
By default a plot window opens automatically alongside the terminal dashboard
and updates in real time from the same data the monitor sends to the ESP32.
It has clickable view buttons (SOC vs dataset / SOH / Fault severity / Cell
voltages), mouse zoom + pan (toolbar), a "Follow latest" toggle, and a window
slider — the y-axis auto-ranges to the data, serial-plotter style. Use
--no-gui to run the terminal dashboard alone. Plotting code lives in
gui_plotter.py; terminal rendering lives in dashboard.py.

INJECTION KEYS (type in the terminal during playback + Enter)
-------------------------------------------------------------
    0  Voltage imbalance      5  Low temperature
    1  Thermal hotspot        6  Gas / pressure
    2  Sensor loss            7  Acoustic / impact
    3  Voltage gradient       8  Impedance spike
    4  Temperature jump       q  Quit
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import queue as qmod
import select
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ── Rich imports ─────────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text
except ImportError:
    sys.exit("ERROR: rich not installed.  Run:  pip install rich")

try:
    import serial
except ImportError:
    sys.exit("ERROR: pyserial not installed.  Run:  pip install pyserial")

# Joint SOC/SOH UKF — Python mirror of BMS_ESP32_Demo/src/ukf_soh/ukf_soh.h
from bms_state import FAULT_LEVELS, FAULT_NAMES, NUM_FAULTS, State, is_detected
from csv_logger import run_logger
from dashboard import build_layout
from detector_sim import DetectorSim, Frame
from gui_plotter import gui_available, run_gui
from soc_ukf import SocUkf


# ─────────────────────────────────────────────────────────────────────────────
#  Monitor configuration
# ─────────────────────────────────────────────────────────────────────────────

# Dataset fault type → which of our fault IDs would fire in dry-run.
# IDs match the C++ FaultID enum (types.h): GAS_PRESSURE=6, EIS_FAULT=7.
FAULT_TYPE_MAP: dict[str, set[int]] = {
    "Micro-short / abnormal self-heating":      {1, 7},   # THERMAL_HOT, EIS_FAULT
    "Gas generation / pressure build-up":        {6},      # GAS_PRESSURE
    "Impedance growth / contact degradation":    {3, 7},   # VOLT_GRADIENT, EIS_FAULT
    "Voltage sensor noise / loose lead":         {2, 3},   # SENSOR_LOSS, VOLT_GRADIENT
    "Mechanical impact / obstacle hit":          {6},      # GAS_PRESSURE (pressure/acoustic/force)
}

ANOMALY_DEFS = {
    "0": ("Voltage imbalance",       8.0),
    "1": ("Thermal hotspot",        10.0),
    "2": ("Sensor loss (suppress)",  6.0),
    "3": ("Voltage gradient",        2.0),
    "4": ("Temperature jump",        2.0),
    "5": ("Low temperature",        10.0),
    "6": ("Gas / pressure",          8.0),
    "7": ("Acoustic / impact",       2.0),
    "8": ("Impedance spike",         2.0),
}

# Column aliases: internal name → candidate column names in the dataset
COL_ALIASES: dict[str, list[str]] = {
    "time":         ["time_s", "time", "Time"],
    "voltage":      ["voltage_V", "voltage", "cell_voltage_V"],
    "current":      ["current_A", "current", "Current_A", "current_a"],
    "T_surf":       ["cell_surface_temperature_C", "Temperature_measured",
                     "temp_c", "temperature_c"],
    "T_rise":       ["temperature_rise_C", "temp_rise_c"],
    "acoustic_rms": ["acoustic_rms_g", "acoustic_rms", "acoustic_g", "acoustic"],
    "acoustic_kHz": ["acoustic_peak_frequency_kHz", "acoustic_peak_kHz"],
    "pressure":     ["pressure_kPa", "pressure_kpa", "pressure"],
    "impact_force": ["impact_force_N", "impact_force"],
    "dVdt":         ["dVdt_V_per_s", "dVdt"],
    "dTdt":         ["dTdt_C_per_s", "dTdt"],
    "dPdt":         ["dPdt_kPa_per_s", "dPdt"],
    "SOC":          ["SOC_estimated_percent", "SOC_pct", "soc_percent"],
    "fault_flag":   ["fault_flag", "fault", "label"],
    "fault_type":   ["fault_type", "fault_name"],
}


# ─────────────────────────────────────────────────────────────────────────────
#  ESP32 serial parser  —  updates state from ESP32 text output
# ─────────────────────────────────────────────────────────────────────────────

def _parse_esp32_line(line: str, s: State, verbose: bool) -> None:
    """Parse one line from ESP32 serial output and update state."""
    if verbose:
        console.print(f"  [dim]← ESP32: {line}[/dim]")

    t_now = f"[{s.time_s:.1f}s]"

    if line.startswith("ACK,"):
        parts = line.split(",")
        seq   = int(parts[1]) if len(parts) > 1 else 0
        ms    = int(parts[2]) if len(parts) > 2 else 0
        with s.lock:
            s.connected = True
            s.rtt_ms    = max(0, ms - s.seq * 100)   # rough estimate

        ev = Text()
        ev.append(f"{t_now} ", style="dim")
        ev.append("ACK  ", style="bold cyan")
        ev.append(f"seq={seq}  round-trip={s.rtt_ms} ms", style="dim")
        s.add_event(ev)

    elif line.startswith("[FAULT_SET]"):
        # [FAULT_SET] t=X ms  id=N  name=NAME  lvl=LVL  detail
        try:
            fid  = int(_between(line, "id=", "  "))
            name = _between(line, "name=", "  ")
            lvl  = _between(line, "lvl=", "  ")
        except Exception:
            return
        with s.lock:
            s.fault_bits |= (1 << fid)

        color = "red" if lvl == "CRITICAL" else "yellow" if lvl == "HIGH" else "cyan"
        ev = Text()
        ev.append(f"{t_now} ", style="dim")
        ev.append("FAULT_SET  ", style=f"bold {color}")
        ev.append(f"{name}  sev={s.fault_severities[fid]:.2f}  → ",
                  style="white")
        ev.append(f"CHG={'OPEN' if s.chg_open else 'CLOSED'}  "
                  f"DSC={'OPEN' if s.dsc_open else 'CLOSED'}",
                  style="red" if s.chg_open else "green")
        s.add_event(ev)

    elif line.startswith("[FAULT_CLR]"):
        try:
            fid  = int(_between(line, "id=", "  "))
            name = _between(line, "name=", "\n")
        except Exception:
            return
        with s.lock:
            s.fault_bits &= ~(1 << fid)
            if fid < len(s.fault_severities):
                s.fault_severities[fid] = 0.0

        ev = Text()
        ev.append(f"{t_now} ", style="dim")
        ev.append("FAULT_CLR  ", style="bold green")
        ev.append(name, style="dim")
        s.add_event(ev)

    elif line.startswith("[MOSFET]"):
        chg_open = "CHG=OPEN" in line
        dsc_open = "DSC=OPEN" in line
        with s.lock:
            s.chg_open = chg_open
            s.dsc_open = dsc_open

    elif line.startswith("[VALIDATE]"):
        try:
            outcome = _between(line, "outcome=", "\n").strip()
        except Exception:
            return
        with s.lock:
            if outcome == "TP": s.TP += 1
            elif outcome == "TN": s.TN += 1
            elif outcome == "FP": s.FP += 1
            elif outcome == "FN": s.FN += 1

        ev = Text()
        ev.append(f"{t_now} ", style="dim")
        ev.append("VALIDATE  ", style="bold blue")
        color = "green" if outcome in ("TP", "TN") else \
                "red"   if outcome == "FP" else "yellow"
        ev.append(f"gt={s.ground_truth}  faults=0x{s.fault_bits:04X}  ",
                  style="dim")
        ev.append(outcome, style=f"bold {color}")
        s.add_event(ev)

    elif line.startswith("[META]"):
        scenario = _between(line, "scenario_id=", ",")
        with s.lock:
            s.scenario = scenario
        ev = Text()
        ev.append(f"{t_now} ", style="dim")
        ev.append("META  ", style="bold green")
        ev.append(line[6:80], style="dim")
        s.add_event(ev)

    elif line.startswith("[BMS]"):
        ev = Text()
        ev.append(f"{t_now} ", style="dim")
        ev.append("BOOT  ", style="bold green")
        ev.append(line[5:80], style="dim")
        s.add_event(ev)
        with s.lock:
            s.connected = True


def _between(s: str, start: str, end: str) -> str:
    """Extract substring between two markers."""
    i = s.index(start) + len(start)
    j = s.index(end, i) if end in s[i:] else len(s)
    return s[i:j].strip()


# ─────────────────────────────────────────────────────────────────────────────
#  Serial reader thread  (runs in background when not --dry-run)
# ─────────────────────────────────────────────────────────────────────────────

def serial_reader_thread(ser: "serial.Serial", s: State,
                          verbose: bool, stop: threading.Event) -> None:
    """Continuously read from ESP32 and update state."""
    while not stop.is_set():
        try:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if line:
                _parse_esp32_line(line, s, verbose)
        except Exception:
            with s.lock:
                s.connected = False


# ─────────────────────────────────────────────────────────────────────────────
#  Dry-run simulator  —  generates fake ESP32 events from dataset labels
# ─────────────────────────────────────────────────────────────────────────────

class DryRunSim:
    """Simulates ESP32 responses so the dashboard works without hardware."""

    def __init__(self, s: State):
        self.s           = s
        self.prev_fault  = 0
        self.prev_active: set[int] = set()

    def step(self, fault_flag: int, fault_type: str,
             T_surf: float, T_rise: float, dVdt: float,
             acoustic: float, SOC_pct: float, pressure: float = 101.3) -> None:
        s = self.s
        t_now = f"[{s.time_s:.1f}s]"

        target_ids: set[int] = set()
        if fault_flag == 1:
            for key, ids in FAULT_TYPE_MAP.items():
                if key.lower() in fault_type.lower():
                    target_ids = ids
                    break
            if not target_ids:
                target_ids = {1}   # generic: THERMAL_HOT

        # Compute rough per-fault severities for active faults (IDs per types.h)
        severities = [0.0] * NUM_FAULTS
        if 1 in target_ids:   # THERMAL_HOT
            severities[1] = min(1.0, max(0.0,
                (T_surf - 38.0) / 27.0 + (T_rise - 5.0) / 7.0) / 2)
        if 2 in target_ids:   # SENSOR_LOSS
            severities[2] = min(1.0, max(0.0, (abs(dVdt) - 0.60) / 0.40))
        if 3 in target_ids:   # VOLT_GRADIENT
            severities[3] = min(1.0, max(0.0, (abs(dVdt) - 0.28) / 0.72))
        if 6 in target_ids:   # GAS_PRESSURE
            severities[6] = min(1.0, max(0.0, (pressure - 105.0) / 35.0))
        if 7 in target_ids:   # EIS_FAULT
            severities[7] = min(1.0, max(0.0, (acoustic - 0.042) / 0.06))

        # New faults
        newly_set = target_ids - self.prev_active
        for fid in newly_set:
            with s.lock:
                s.fault_bits |= (1 << fid)
                s.fault_severities[fid] = max(0.01, severities[fid])
            sev   = s.fault_severities[fid]
            level = FAULT_LEVELS[fid]
            name  = FAULT_NAMES[fid]
            color = "red" if level == "CRITICAL" else \
                    "yellow" if level == "HIGH" else "cyan"
            chg_open = True   # any fault opens charge by default
            dsc_open = level == "CRITICAL"
            with s.lock:
                s.chg_open = chg_open
                s.dsc_open = dsc_open
            ev = Text()
            ev.append(f"{t_now} ", style="dim")
            ev.append("FAULT_SET  ", style=f"bold {color}")
            ev.append(f"{name}  sev={sev:.2f}  → ", style="white")
            ev.append(f"CHG=OPEN  DSC={'OPEN' if dsc_open else 'CLOSED'}",
                      style="red")
            s.add_event(ev)

        # Cleared faults
        newly_clr = self.prev_active - target_ids
        for fid in newly_clr:
            with s.lock:
                s.fault_bits &= ~(1 << fid)
                s.fault_severities[fid] = 0.0
            ev = Text()
            ev.append(f"{t_now} ", style="dim")
            ev.append("FAULT_CLR  ", style="bold green")
            ev.append(FAULT_NAMES[fid], style="dim")
            s.add_event(ev)

        # Restore MOSFETs if no faults
        if not target_ids:
            with s.lock:
                s.chg_open = False
                s.dsc_open = False

        # Accuracy update
        any_detected = bool(s.fault_bits)
        with s.lock:
            if fault_flag == 1 and any_detected:  s.TP += 1
            elif fault_flag == 0 and not any_detected: s.TN += 1
            elif fault_flag == 1 and not any_detected: s.FN += 1
            else: s.FP += 1

        self.prev_active = target_ids


# ─────────────────────────────────────────────────────────────────────────────
#  Detector simulator  —  runs the REAL detectors.h algorithm in Python
#
#  Unlike DryRunSim (which replays the dataset's ground-truth labels), this
#  feeds the raw sensor signals through detector_sim.DetectorSim — a faithful
#  port of the ESP32 firmware — so the TP/FP/FN numbers reflect how our actual
#  fault algorithm performs. This is the engine behind `--sim`.
# ─────────────────────────────────────────────────────────────────────────────

class DetectorSimRunner:
    """Drive State from the Python port of detectors.h and score it honestly."""

    def __init__(self, s: State):
        self.s   = s
        self.det = DetectorSim()
        self.prev_bits = 0
        # Per-fault-type recall and per-detector false-fire diagnostics
        self.type_total:  dict[str, int] = {}
        self.type_caught: dict[str, int] = {}
        self.tp_by_det = [0] * NUM_FAULTS
        self.fp_by_det = [0] * NUM_FAULTS

    def step(self, row: dict, seq: int) -> None:
        s = self.s
        t_now = f"[{s.time_s:.1f}s]"

        f = Frame(
            seq=seq,
            v=[row["v1"], row["v2"], row["v3"]],
            T_surf=row["T_surf"], T_rise=row["T_rise"],
            current=row["current"], acoustic_rms=row["acoustic_rms"],
            pressure=row["pressure"], impact_force=row["impact_force"],
            dVdt=row["dVdt"], dTdt=row["dTdt"], dPdt=row["dPdt"],
            SOC_pct=row["SOC_pct"], ts_ms=int(row["time_s"] * 1000),
            soc_est=row.get("soc_est", -1.0),   # UKF estimate (impedance OCV ref)
        )
        res  = self.det.step(f)
        bits = res["fault_bits"]
        gt   = int(row["fault_flag"])

        # Emit FAULT_SET / FAULT_CLR events on bit transitions
        changed = bits ^ self.prev_bits
        for fid in range(NUM_FAULTS):
            if not (changed & (1 << fid)):
                continue
            now_set = bool(bits & (1 << fid))
            ev = Text()
            ev.append(f"{t_now} ", style="dim")
            if now_set:
                level = FAULT_LEVELS[fid]
                color = "red" if level == "CRITICAL" else \
                        "yellow" if level == "HIGH" else "cyan"
                ev.append("FAULT_SET  ", style=f"bold {color}")
                ev.append(f"{FAULT_NAMES[fid]}  sev={res['severities'][fid]:.2f}  → ",
                          style="white")
                ev.append(f"CHG={'OPEN' if res['chg_open'] else 'CLOSED'}  "
                          f"DSC={'OPEN' if res['dsc_open'] else 'CLOSED'}",
                          style="red" if res["chg_open"] else "green")
            else:
                ev.append("FAULT_CLR  ", style="bold green")
                ev.append(FAULT_NAMES[fid], style="dim")
            s.add_event(ev)
        self.prev_bits = bits

        # Write detector results into shared State
        with s.lock:
            s.fault_bits       = bits
            s.fault_severities = res["severities"]
            s.chg_open         = res["chg_open"]
            s.dsc_open         = res["dsc_open"]

        # Honest scoring: only genuine (non-advisory) faults count as detections
        detected = is_detected(bits)
        with s.lock:
            if   gt == 1 and detected:     s.TP += 1
            elif gt == 0 and not detected: s.TN += 1
            elif gt == 1 and not detected: s.FN += 1
            else:                          s.FP += 1

        # Diagnostics for the end-of-run report
        ftype = row["fault_type"]
        if gt == 1:
            self.type_total[ftype]  = self.type_total.get(ftype, 0) + 1
            if detected:
                self.type_caught[ftype] = self.type_caught.get(ftype, 0) + 1
        for fid in range(NUM_FAULTS):
            if bits & (1 << fid):
                if gt == 1: self.tp_by_det[fid] += 1
                else:       self.fp_by_det[fid] += 1

    def report(self) -> Text:
        """Build a per-fault-type recall + per-detector breakdown for printing."""
        t = Text()
        t.append("\nPER-FAULT-TYPE RECALL (sim)\n", style="bold cyan")
        for ftype in sorted(self.type_total):
            tot = self.type_total[ftype]
            cau = self.type_caught.get(ftype, 0)
            pct = (cau / tot * 100) if tot else 0.0
            color = "green" if pct >= 90 else "yellow" if pct >= 50 else "red"
            t.append(f"  {ftype:42s} {cau:5d}/{tot:<5d}  ", style="dim")
            t.append(f"{pct:5.1f}%\n", style=color)

        t.append("\nPER-DETECTOR FIRES (tp on fault rows / fp on normal rows)\n",
                 style="bold cyan")
        for fid in range(NUM_FAULTS):
            tp, fp = self.tp_by_det[fid], self.fp_by_det[fid]
            if tp == 0 and fp == 0:
                continue
            color = "red" if fp > tp else "green"
            t.append(f"  {FAULT_NAMES[fid]:14s} tp={tp:<6d} fp={fp:<6d}\n",
                     style=color)
        return t


# ─────────────────────────────────────────────────────────────────────────────
#  Dataset loader
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_col(cols: list[str], aliases: list[str]) -> str | None:
    for c in aliases:
        if c in cols:
            return c
    return None


def _parse_metadata(path: Path) -> dict[str, str]:
    """Read leading '# key=value' comment lines from a CSV.

    Caterpillar SIL test files prefix the data with a metadata header, e.g.
        # scenario_id=THERMAL_RUNAWAY_INIT
        # cell_count=6
        # initial_soc=80
        # ambient_temp_c=25
        # fault_type=thermal_runaway
        # fault_start_time_s=120
    Returns a lower-cased key→value dict (empty for xlsx or headerless files).
    """
    meta: dict[str, str] = {}
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return meta
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("#"):
                    break
                body = line[1:].strip()
                if "=" in body:
                    k, v = body.split("=", 1)
                    meta[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return meta


def _numbered_cols(cols: list[str], pattern: str) -> list[str]:
    """Columns whose name fully matches `pattern` (e.g. cell voltages / temps),
    returned in natural numeric order (cell1_v, cell2_v, … cell10_v)."""
    rx = re.compile(pattern, re.IGNORECASE)
    hits = [c for c in cols if rx.fullmatch(str(c))]
    return sorted(hits, key=lambda c: int(re.search(r"(\d+)", str(c)).group(1)))


def _safe_gradient(signal: np.ndarray, t: np.ndarray) -> np.ndarray:
    """d(signal)/dt computed from consecutive samples, robust to gaps.
    Used when the dataset does NOT ship pre-computed dVdt/dTdt/dPdt (the
    Caterpillar SIL files only carry raw signals)."""
    if len(signal) < 2:
        return np.zeros_like(signal, dtype=float)
    g = np.gradient(np.asarray(signal, float), np.asarray(t, float))
    return np.nan_to_num(g, nan=0.0, posinf=0.0, neginf=0.0)


def load_dataset(path: str) -> pd.DataFrame:
    """Load either our synthetic .xlsx OR a Caterpillar SIL CSV.

    The Caterpillar playback format (see BMS_Evaluation_instructions) differs
    from our synthetic file in several ways, all handled here so the firmware
    runs unchanged:
      • a '# key=value' metadata header (scenario, initial_soc, ambient_temp_c,
        fault_start_time_s, …)
      • 4–6 individual cell voltages (cell1_v … cellN_v) instead of one
      • multiple temperatures (temp1_c … tempN_c)
      • mandatory signals only (Time/Voltage/Current/Temp) — NO pre-computed
        dVdt/dTdt/dPdt, which we derive from consecutive samples
      • no per-row label — ground truth comes from fault_start_time_s metadata
    The .xlsx path is unchanged so the validated sim numbers are preserved.
    """
    p = Path(path)
    meta = _parse_metadata(p)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        df = pd.read_excel(path, sheet_name="TimeSeries_Data")
    else:
        skip = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"): skip += 1
                else: break
        df = pd.read_csv(path, skiprows=skip)

    cols = list(df.columns)
    out  = {}
    for field_name, aliases in COL_ALIASES.items():
        col = _resolve_col(cols, aliases)
        out[field_name] = df[col] if col else None

    n        = len(df)
    ambient  = float(meta.get("ambient_temp_c", 25.0))
    init_soc = float(meta["initial_soc"]) if meta.get("initial_soc") else None

    # ── time ──────────────────────────────────────────────────────────────────
    time_s = (out["time"].to_numpy(float) if out.get("time") is not None
              else np.arange(n, dtype=float) * 0.1)

    # ── cell voltages → packet's 3 channels = [min, mean, max] ─────────────────
    #  Passing min/mean/max preserves the exact cell spread (what the imbalance
    #  and balance detectors need) for ANY cell count, and the mean is the pack
    #  voltage the UKF uses. A single voltage column is expanded as before.
    cell_cols = _numbered_cols(cols, r"cell\s*\d+\s*_?v(olt(age)?)?")
    if not cell_cols:
        cell_cols = [c for c in ("v1", "v2", "v3") if c in cols]
    if cell_cols:
        V  = np.column_stack([df[c].to_numpy(float) for c in cell_cols])
        v1, v2, v3 = V.min(axis=1), V.mean(axis=1), V.max(axis=1)
        n_cells = len(cell_cols)
    elif out.get("voltage") is not None:
        rng = np.random.default_rng(42)
        base = out["voltage"].to_numpy(float)
        v1 = base
        v2 = base * (1 + rng.normal(0, 0.002, n))
        v3 = base * (1 + rng.normal(0, 0.003, n))
        n_cells = 1
    else:
        v1 = np.full(n, 3.7); v2, v3 = v1.copy(), v1.copy(); n_cells = 0
    v_mean = (v1 + v2 + v3) / 3.0

    # ── temperatures → T_surf = hottest cell; T_rise above ambient ─────────────
    temp_cols = _numbered_cols(cols, r"temp(erature)?\s*\d+\s*_?c")
    if temp_cols:
        T_surf = np.column_stack(
            [df[c].to_numpy(float) for c in temp_cols]).max(axis=1)
    elif out.get("T_surf") is not None:
        T_surf = out["T_surf"].to_numpy(float)
    else:
        T_surf = np.full(n, 25.0)
    T_rise = (out["T_rise"].to_numpy(float) if out.get("T_rise") is not None
              else T_surf - ambient)

    # ── optional signals (benign defaults if the sensor isn't present) ─────────
    def _get(field_name: str, default: float) -> np.ndarray:
        s = out.get(field_name)
        return s.to_numpy(float) if s is not None else np.full(n, default)

    current  = _get("current", 0.0)
    pressure = _get("pressure", 101.3)
    acoustic = _get("acoustic_rms", 0.015)

    # ── gradients: use shipped columns, else derive from consecutive samples ───
    dVdt = (out["dVdt"].to_numpy(float) if out.get("dVdt") is not None
            else _safe_gradient(v_mean, time_s))
    dTdt = (out["dTdt"].to_numpy(float) if out.get("dTdt") is not None
            else _safe_gradient(T_surf, time_s))
    dPdt = (out["dPdt"].to_numpy(float) if out.get("dPdt") is not None
            else _safe_gradient(pressure, time_s))

    # ── SOC: column, else initial_soc from metadata, else 100% ─────────────────
    if out.get("SOC") is not None:
        SOC = out["SOC"].to_numpy(float)
    else:
        SOC = np.full(n, init_soc if init_soc is not None else 100.0)

    # ── ground truth: per-row label, else derived from fault_start_time_s ──────
    if out.get("fault_flag") is not None:
        fault_flag = out["fault_flag"].to_numpy(float).astype(int)
    elif meta.get("fault_start_time_s"):
        fault_flag = (time_s >= float(meta["fault_start_time_s"])).astype(int)
    else:
        fault_flag = np.zeros(n, dtype=int)

    if out.get("fault_type") is not None:
        fault_type = out["fault_type"].to_numpy(str)
    elif meta.get("fault_type"):
        fault_type = np.where(fault_flag == 1, meta["fault_type"], "Normal")
    else:
        fault_type = np.where(fault_flag == 1, "Fault", "Normal")

    result = pd.DataFrame({
        "time_s":       time_s,
        "v1": v1, "v2": v2, "v3": v3,
        "T_surf":       T_surf,
        "T_rise":       T_rise,
        "current":      current,
        "acoustic_rms": acoustic,
        "acoustic_kHz": _get("acoustic_kHz", 120.0),
        "dVdt":         dVdt,
        "dTdt":         dTdt,
        "dPdt":         dPdt,
        "pressure":     pressure,
        "impact_force": _get("impact_force", 0.0),
        "SOC_pct":      SOC,
        "fault_flag":   fault_flag,
        "fault_type":   fault_type,
    })

    dt = float(np.median(np.diff(result["time_s"].values)))
    result.attrs["sample_hz"]   = round(1.0 / max(dt, 1e-9))
    result.attrs["scenario"]    = meta.get("scenario_id", p.stem)
    result.attrs["initial_soc"] = init_soc
    result.attrs["cell_count"]  = n_cells
    result.attrs["meta"]        = meta
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Anomaly injection
# ─────────────────────────────────────────────────────────────────────────────

class _RawTerminal:
    """Put the controlling terminal into cbreak mode (single key, no echo) for
    the lifetime of the context, restoring the previous settings on exit. No-op
    on Windows (msvcrt reads keys directly) or when stdin is not a TTY (piped /
    CI). Restoration is also registered with atexit so a crash can't leave the
    user's shell in no-echo mode."""

    def __init__(self) -> None:
        self.fd = None
        self.saved = None

    def __enter__(self) -> "_RawTerminal":
        if sys.platform != "win32" and sys.stdin.isatty():
            import atexit
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            atexit.register(self._restore)
        return self

    def _restore(self) -> None:
        if self.fd is not None and self.saved is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            self.saved = None

    def __exit__(self, *_exc) -> None:
        self._restore()


def key_stream(stop_ev: "threading.Event"):
    """Yield single keypresses (no Enter required) until stop_ev is set.
    Escape sequences (arrow keys, function keys) are swallowed so they don't get
    mistaken for injection keys. Falls back to line-buffered reads when stdin is
    not an interactive TTY, so piped input (CI: "1\\nq\\n") still works."""
    if not sys.stdin.isatty():
        for line in sys.stdin:
            if stop_ev.is_set():
                return
            yield line.strip()
        return

    if sys.platform == "win32":
        import msvcrt
        while not stop_ev.is_set():
            if msvcrt.kbhit():
                yield msvcrt.getwch()
            else:
                time.sleep(0.03)
        return

    fd = sys.stdin.fileno()
    while not stop_ev.is_set():
        if not select.select([fd], [], [], 0.1)[0]:
            continue
        ch = os.read(fd, 1).decode("utf-8", "ignore")
        if ch == "\x1b":                         # ESC: drain the rest of the seq
            while select.select([fd], [], [], 0.001)[0]:
                os.read(fd, 1)
            continue
        if ch:
            yield ch


def apply_anomaly(row: dict, aid: str) -> dict:
    if   aid == "0": row["v1"] += 0.22
    elif aid == "1": row["T_surf"] = 52.0; row["T_rise"] = 12.0
    elif aid == "2": row["_suppress"] = True
    elif aid == "3": row["dVdt"] = 0.85;   row["v1"] -= 0.30
    elif aid == "4": row["dTdt"] = 3.5;    row["T_surf"] += 4.0
    elif aid == "5": row["T_surf"] = 4.0;  row["T_rise"] = -15.0
    elif aid == "6": row["pressure"] = 120.0
    elif aid == "7": row["acoustic_rms"] = 0.55; row["impact_force"] = 500.0
    elif aid == "8": row["dVdt"] = 0.70
    return row


# ─────────────────────────────────────────────────────────────────────────────
#  Packet encoder
# ─────────────────────────────────────────────────────────────────────────────

def encode_packet(row: dict, seq: int) -> str:
    return (
        f"$,{seq:04d},"
        f"{row['v1']:.4f},{row['v2']:.4f},{row['v3']:.4f},"
        f"{row['T_surf']:.2f},{row['T_rise']:.2f},"
        f"{row['current']:.3f},"
        f"{row['acoustic_rms']:.5f},{row['acoustic_kHz']:.2f},"
        f"{row['pressure']:.3f},{row['impact_force']:.2f},"
        f"{row['dVdt']:.5f},{row['dTdt']:.5f},{row['dPdt']:.5f},"
        f"{row['SOC_pct']:.2f},"
        f"{int(row['fault_flag'])},"
        f"{int(row['time_s'] * 1000)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  CSV log writer
# ─────────────────────────────────────────────────────────────────────────────

LOG_COLS = [
    "wall_time", "time_s", "seq",
    "v1", "v2", "v3", "T_surf", "T_rise",
    "current", "acoustic_rms",
    "SOC_pct", "soc_est", "soh_est",
    "fault_bits_hex", "fault_names",
    "chg_open", "dsc_open",
    "ground_truth", "outcome",
]


def outcome_str(ground_truth: int, fault_bits: int) -> str:
    detected = is_detected(fault_bits)   # advisory faults excluded from scoring
    if ground_truth == 1 and detected:     return "TP"
    if ground_truth == 0 and not detected: return "TN"
    if ground_truth == 1 and not detected: return "FN"
    return "FP"


def log_row(writer: "csv.DictWriter", s: State, time_s: float) -> None:
    names = "|".join(
        FAULT_NAMES[i] for i in range(NUM_FAULTS)
        if s.fault_bits & (1 << i)
    ) or "NONE"
    writer.writerow({
        "wall_time":    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "time_s":       f"{time_s:.1f}",
        "seq":          s.seq,
        "v1":           f"{s.v[0]:.4f}",
        "v2":           f"{s.v[1]:.4f}",
        "v3":           f"{s.v[2]:.4f}",
        "T_surf":       f"{s.T_surf:.2f}",
        "T_rise":       f"{s.T_rise:.2f}",
        "current":      f"{s.current:.3f}",
        "acoustic_rms": f"{s.acoustic:.4f}",
        "SOC_pct":      f"{s.SOC_pct:.2f}",
        "soc_est":      f"{s.soc_est:.2f}",
        "soh_est":      f"{s.soh_est:.2f}",
        "fault_bits_hex": f"0x{s.fault_bits:04X}",
        "fault_names":  names,
        "chg_open":     "OPEN" if s.chg_open else "CLOSED",
        "dsc_open":     "OPEN" if s.dsc_open else "CLOSED",
        "ground_truth": s.ground_truth,
        "outcome":      outcome_str(s.ground_truth, s.fault_bits),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

console = Console(highlight=False)


def interactive_menu(args: argparse.Namespace) -> None:
    """Ask the user which run mode to use, instead of remembering CLI flags.
    Mutates args in place. Only shown when no mode flag/port was given and we
    have a real terminal (skipped automatically in CI / piped input)."""
    console.print()
    console.print("[bold white]BMS Monitor — choose a run mode:[/bold white]")
    console.print("  [bold cyan]1[/bold cyan]  Dry run         "
                  "[dim]— replay dataset labels (quick UI check)[/dim]")
    console.print("  [bold cyan]2[/bold cyan]  Dry run + Sim   "
                  "[dim]— run our real fault algorithm on the data[/dim]")
    console.print("  [bold cyan]3[/bold cyan]  ESP32           "
                  "[dim]— connect to the board over serial[/dim]")
    console.print("  [bold cyan]4[/bold cyan]  ESP32 + WiFi    "
                  "[dim]— serial + built-in WiFi UDP logger (→ wifi_log.csv)[/dim]")
    choice = ""
    while choice not in ("1", "2", "3", "4"):
        try:
            choice = input("Select [1/2/3/4] (default 1): ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            choice = "1"
            break
    if choice == "1":
        args.dry_run = True
    elif choice == "2":
        args.dry_run = True
        args.sim     = True
    else:
        if choice == "4":
            args.wifi = True
        # ESP32 modes require a serial port — keep asking, no dry-run fallback.
        while not args.port:
            try:
                args.port = input("  Serial port (e.g. COM3): ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[red]No port given — exiting.[/red]")
                sys.exit(1)
            if not args.port:
                console.print("[yellow]A serial port is required for ESP32 mode "
                              "(Ctrl-C to quit).[/yellow]")


def main() -> None:
    # ── CLI ───────────────────────────────────────────────────────────────────
    ap = argparse.ArgumentParser(description="BMS Live Monitor + Serial Bridge")
    default_file = str(Path(__file__).with_name(
        "synthetic_NMC__1C_fault_EIS_dataset.xlsx"))
    ap.add_argument("--file",     default=default_file,
                    help="Dataset XLSX or CSV "
                         "(defaults to the synthetic NMC dataset)")
    ap.add_argument("--port",     default=None,
                    help="Serial port (omit to run in dry-run/demo mode)")
    ap.add_argument("--baud",     type=int,   default=115200)
    ap.add_argument("--speed",    type=float, default=8.0,
                    help="Playback speed (1.0 = real-time)")
    ap.add_argument("--logfile",  default="session.csv")
    ap.add_argument("--dry-run",  action="store_true",
                    help="No serial — replay dataset ground-truth labels")
    ap.add_argument("--sim",      action="store_true",
                    help="No serial — run the REAL detectors.h algorithm "
                         "(Python port) on the dataset signals")
    ap.add_argument("--verbose",  action="store_true",
                    help="Print raw packets for debugging")
    ap.add_argument("--loop",     action="store_true",
                    help="Loop dataset indefinitely")
    ap.add_argument("--no-gui",   action="store_true",
                    help="Don't open the live GUI plot window "
                         "(terminal dashboard only)")
    ap.add_argument("--wifi",     action="store_true",
                    help="ESP32 + WiFi: also run the UDP log receiver in-process "
                         "(no separate csv_logger.py terminal needed). Use with --port.")
    ap.add_argument("--wifi-port", type=int, default=4210,
                    help="UDP port for the integrated WiFi logger "
                         "(must match LOG_SERVER_PORT in config.h)")
    ap.add_argument("--wifi-out", default="wifi_log.csv",
                    help="Output CSV for the integrated WiFi logger")
    args = ap.parse_args()

    # No mode chosen at all → show the interactive picker (if we have a TTY).
    if not args.port and not args.dry_run and not args.sim:
        if sys.stdin.isatty():
            interactive_menu(args)
        else:
            args.dry_run = True   # piped/CI input → default to plain dry-run

    # --sim runs locally (no serial); ensure dry-run semantics.
    if args.sim:
        args.dry_run = True
    # A bare --dry-run / no port still means "no serial".
    if not args.port:
        args.dry_run = True
    # The integrated WiFi logger only makes sense with a real board.
    if args.wifi and not args.port:
        console.print("[yellow]--wifi needs an ESP32 (--port); ignoring it.[/yellow]")
        args.wifi = False

    # ── Load dataset ──────────────────────────────────────────────────────────
    console.print(f"[cyan]Loading dataset:[/cyan] {args.file}")
    try:
        df = load_dataset(args.file)
    except Exception as e:
        console.print(f"[red]ERROR loading dataset:[/red] {e}")
        sys.exit(1)

    hz       = df.attrs["sample_hz"]
    sleep_s  = (1.0 / hz) / args.speed
    scenario = df.attrs["scenario"]
    console.print(f"[green]Loaded:[/green] {len(df)} rows  "
                  f"@ {hz} Hz  ({df['time_s'].max():.0f}s)  "
                  f"speed={args.speed}×")

    # ── Serial port (re-prompt on failure — no silent dry-run fallback) ───────
    ser: "serial.Serial | None" = None
    if not args.dry_run:
        while True:
            try:
                ser = serial.Serial(args.port, args.baud, timeout=0.05)
                time.sleep(2.0)
                console.print(f"[green]Serial open:[/green] {args.port} @ {args.baud}")
                break
            except Exception as e:
                console.print(f"[red]Serial error on '{args.port}':[/red] {e}")
                if not sys.stdin.isatty():
                    console.print("[red]No terminal to re-prompt — aborting.[/red]")
                    sys.exit(1)
                try:
                    new_port = input("  Enter another serial port "
                                     "(blank to quit): ").strip()
                except (EOFError, KeyboardInterrupt):
                    new_port = ""
                if not new_port:
                    console.print("[red]No port given — exiting.[/red]")
                    sys.exit(1)
                args.port = new_port

    # ── State + threads ───────────────────────────────────────────────────────
    s          = State(scenario=scenario, sample_hz=hz, speed=args.speed)
    cmd_q:  qmod.Queue[str] = qmod.Queue()
    stop_ev = threading.Event()

    # ── Integrated WiFi UDP logger (ESP32 → PC) ──────────────────────────────
    # In --wifi mode the dashboard is driven by the data the ESP32 sends BACK
    # over WiFi (not the local cable copy): every UDP datagram updates the live
    # State below via _on_wifi_packet. The serial cable is still used to stream
    # frames TO the board; the WiFi link carries its processed results back.
    wifi_ready = threading.Event()
    wifi_seen  = {"prev_faults": 0}

    def _on_wifi_packet(row: dict) -> None:
        """Drive the live dashboard from one ESP32 WiFi datagram."""
        try:
            v1, v2, v3 = float(row["v1"]), float(row["v2"]), float(row["v3"])
            ts      = float(row["ts_ms"]) / 1000.0
            soc_est = float(row["soc_est"]);  soh_est = float(row["soh_est"])
            sev     = float(row["est_severity"])
            fb      = row["fault_bits"].strip()
            fbits   = int(fb, 16) if fb.lower().startswith("0x") else int(fb or 0)
            outcome = row["outcome"].strip()
        except (ValueError, KeyError):
            return
        with s.lock:
            s.seq          = int(float(row.get("seq", 0) or 0))
            s.time_s       = ts
            s.v            = [v1, v2, v3]
            s.T_surf       = float(row["T_surf"])
            s.current      = float(row["current"])
            s.SOC_pct      = float(row["SOC_pct"])
            s.soc_est      = soc_est
            s.soh_est      = soh_est
            s.fault_bits   = fbits
            s.chg_open     = row["chg_state"].strip() == "OPEN"
            s.dsc_open     = row["dsc_state"].strip() == "OPEN"
            s.ground_truth = int(float(row["ground_truth"]))
            s.connected    = True
            # The ESP reports one system severity; spread it over the active
            # faults so the dashboard's per-fault + max() severity views light up.
            s.fault_severities = [sev if (fbits >> i) & 1 else 0.0
                                  for i in range(len(s.fault_severities))]
            if   outcome == "TP": s.TP += 1
            elif outcome == "TN": s.TN += 1
            elif outcome == "FP": s.FP += 1
            elif outcome == "FN": s.FN += 1
            prev = wifi_seen["prev_faults"]
        s.push_history(ts, s.SOC_pct, soc_est, soh_est, sev, v1, v2, v3)
        if fbits != prev:                       # log only on fault-set changes
            wifi_seen["prev_faults"] = fbits
            names = (row.get("active_faults", "") or "NONE").strip()
            ev = Text()
            ev.append(f"[{ts:.1f}s] ", style="dim")
            ev.append("WIFI  ", style="bold cyan")
            ev.append(f"faults={names}  sev={sev:.2f}  {outcome}", style="white")
            s.add_event(ev)

    if args.wifi:
        threading.Thread(
            target=run_logger,
            kwargs=dict(port=args.wifi_port, out=args.wifi_out, stop_ev=stop_ev,
                        quiet=True, on_packet=_on_wifi_packet, ready_ev=wifi_ready),
            daemon=True,
        ).start()
        console.print(f"[green]WiFi logger:[/green] UDP {args.wifi_port} → "
                      f"{args.wifi_out}  [dim](set LOG_SERVER_IP in config.h to "
                      f"this PC's LAN IP)[/dim]")

    if args.wifi and ser:
        # ── WiFi mode: wait for the ESP32 to JOIN WIFI before streaming ───────
        # The first UDP packet means the board booted, joined WiFi, and can reach
        # this PC. We hold off streaming frames until then. The dashboard is fed
        # by _on_wifi_packet, so we do NOT start the serial reader here (the WiFi
        # link is the single source of truth for State).
        console.print("[cyan]Waiting for ESP32 to join WiFi "
                      "(first UDP packet)...[/cyan]")
        if wifi_ready.wait(timeout=30.0):
            console.print("[green]ESP32 connected over WiFi — streaming.[/green]")
        else:
            console.print("[yellow]No WiFi packet after 30s — check WIFI_SSID / "
                          "LOG_SERVER_IP in config.h. Starting anyway.[/yellow]")

    elif not args.dry_run and ser:
        # ── Serial-only mode (--port without --wifi): cable drives everything ──
        threading.Thread(
            target=serial_reader_thread,
            args=(ser, s, args.verbose, stop_ev),
            daemon=True,
        ).start()

        # Wait for the ESP32 to announce itself (first [BMS] boot line / ACK) so
        # we don't fire packets into a device that is still booting.
        handshake_timeout = 15.0
        console.print("[cyan]Waiting for ESP32 to connect...[/cyan]")
        t0 = time.perf_counter()
        while True:
            with s.lock:
                connected = s.connected
            if connected:
                console.print("[green]ESP32 connected.[/green]")
                break
            if time.perf_counter() - t0 > handshake_timeout:
                console.print(
                    f"[yellow]No response after {handshake_timeout:.0f}s — "
                    f"starting anyway.[/yellow]")
                break
            time.sleep(0.1)

    # Keyboard input thread — single keypress, no Enter, no echo (cbreak).
    def _kb():
        console.print("[dim]Anomaly keys: 0-8=inject  q=quit[/dim]")
        with _RawTerminal():
            for ch in key_stream(stop_ev):
                cmd = ch.strip().lower()
                if not cmd:
                    continue
                if cmd == "q" or cmd in ANOMALY_DEFS:
                    cmd_q.put(cmd)
                if cmd == "q":
                    return
    threading.Thread(target=_kb, daemon=True).start()

    # Pick the local responder: real-algorithm sim, or label replay.
    detsim: "DetectorSimRunner | None" = None
    sim:    "DryRunSim | None"         = None
    if args.dry_run:
        if args.sim:
            detsim = DetectorSimRunner(s)
            console.print("[bold magenta]SIM mode:[/bold magenta] running the "
                          "detectors.h algorithm on the dataset.")
        else:
            sim = DryRunSim(s)

    # ── Joint SOC/SOH estimator (mirror of the on-device ESP32 UKF) ───────────
    soc0 = float(df["SOC_pct"].iloc[0]) / 100.0
    ukf  = SocUkf(soc0=soc0)
    with s.lock:
        s.soc_est = soc0 * 100.0
        s.soh_est = ukf.soh_pct

    # ── Send metadata ─────────────────────────────────────────────────────────
    fault_types = list(df.loc[df["fault_flag"] == 1, "fault_type"].unique())
    meta = (f"#,scenario_id={scenario},cell_count=3,sample_hz={hz},"
            f"fault_types={'|'.join(fault_types)},duration_s={df['time_s'].max():.0f}")
    if ser:
        ser.write((meta + "\n").encode())
    ev0 = Text()
    ev0.append("[0.0s] ", style="dim")
    ev0.append("META  ", style="bold green")
    ev0.append(meta[2:70], style="dim")
    s.add_event(ev0)

    # ── CSV log ───────────────────────────────────────────────────────────────
    log_f      = open(args.logfile, "w", newline="")
    log_writer = csv.DictWriter(log_f, fieldnames=LOG_COLS)
    log_writer.writeheader()
    log_f.flush()

    # ── Per-frame streaming step (shared by terminal + GUI modes) ─────────────
    fstate = {"seq": 0, "prev_t": None}
    # Manual fault injection (keys 0-8). drain_commands() arms it; process_frame
    # applies apply_anomaly() for the requested number of frames, then it decays.
    inject = {"id": None, "frames_left": 0}
    # In --wifi mode the ESP32 is the source of truth for the plot/State: we only
    # forward frames over serial and let _on_wifi_packet drive the dashboard from
    # the data the board sends back. Skip the local UKF + history push here.
    wifi_drive = bool(args.wifi and ser)

    def process_frame(idx: int) -> None:
        raw = df.iloc[idx]
        row = {
            "v1":           float(raw["v1"]),
            "v2":           float(raw["v2"]),
            "v3":           float(raw["v3"]),
            "T_surf":       float(raw["T_surf"]),
            "T_rise":       float(raw["T_rise"]),
            "current":      float(raw["current"]),
            "acoustic_rms": float(raw["acoustic_rms"]),
            "acoustic_kHz": float(raw["acoustic_kHz"]),
            "pressure":     float(raw["pressure"]),
            "impact_force": float(raw["impact_force"]),
            "dVdt":         float(raw["dVdt"]),
            "dTdt":         float(raw["dTdt"]),
            "dPdt":         float(raw["dPdt"]),
            "SOC_pct":      float(raw["SOC_pct"]),
            "fault_flag":   int(raw["fault_flag"]),
            "fault_type":   str(raw["fault_type"]),
            "time_s":       float(raw["time_s"]),
            "_suppress":    False,
        }

        # Apply any armed manual injection to this frame's signals
        if inject["frames_left"] > 0 and inject["id"] is not None:
            apply_anomaly(row, inject["id"])
            inject["frames_left"] -= 1

        fstate["seq"] += 1
        seq = fstate["seq"]

        if wifi_drive:
            # The ESP echoes its processed result over WiFi; _on_wifi_packet owns
            # the plot/State. Here we only track seq + time for the stream loop.
            fstate["prev_t"] = row["time_s"]
            with s.lock:
                s.seq    = seq
                s.time_s = row["time_s"]
        else:
            # Joint SOC/SOH UKF step (pack-average voltage + current)
            dt_s = (row["time_s"] - fstate["prev_t"]) if fstate["prev_t"] is not None \
                   else (1.0 / hz)
            fstate["prev_t"] = row["time_s"]
            v_avg = (row["v1"] + row["v2"] + row["v3"]) / 3.0
            soc_est, soh_est = ukf.update(v_avg, row["current"], dt_s)
            row["soc_est"] = soc_est   # UKF SOC → impedance detector OCV reference

            with s.lock:
                s.seq          = seq
                s.time_s       = row["time_s"]
                s.v            = [row["v1"], row["v2"], row["v3"]]
                s.T_surf       = row["T_surf"]
                s.T_rise       = row["T_rise"]
                s.current      = row["current"]
                s.acoustic     = row["acoustic_rms"]
                s.SOC_pct      = row["SOC_pct"]
                s.soc_est      = soc_est
                s.soh_est      = soh_est
                s.ground_truth = row["fault_flag"]
                s.fault_type_str = row["fault_type"]
                s.connected    = True

            # Feed the live chart (all views)
            sys_sev = max(s.fault_severities) if s.fault_severities else 0.0
            s.push_history(row["time_s"], row["SOC_pct"], soc_est, soh_est,
                           sys_sev, row["v1"], row["v2"], row["v3"])

        # Send or simulate
        if not row["_suppress"]:
            pkt = encode_packet(row, seq)
            if ser:
                ser.write((pkt + "\n").encode())
                if args.verbose:
                    console.print(f"  [dim]→ {pkt[:80]}[/dim]")
            if sim:
                sim.step(
                    row["fault_flag"], row["fault_type"],
                    row["T_surf"], row["T_rise"],
                    row["dVdt"], row["acoustic_rms"], row["SOC_pct"],
                    row["pressure"],
                )
            if detsim:
                detsim.step(row, seq)

        # Log to CSV every 10 rows to avoid huge files
        if idx % 10 == 0:
            log_row(log_writer, s, row["time_s"])
            log_f.flush()

    def drain_commands() -> bool:
        """Process queued keyboard commands (from the terminal or the GUI
        window). Returns False if quit requested."""
        while not cmd_q.empty():
            cmd = cmd_q.get_nowait()
            if cmd == "q":
                return False
            if cmd in ANOMALY_DEFS:
                desc, dur = ANOMALY_DEFS[cmd]
                inject["id"] = cmd
                inject["frames_left"] = max(1, int(dur * hz))
                ev = Text()
                ev.append(f"[{s.time_s:.1f}s] ", style="dim")
                ev.append("INJECT  ", style="bold magenta")
                ev.append(f"{desc}  ({dur:.0f}s)", style="white")
                s.add_event(ev)
        return True

    def stream_worker(stop_on_end: bool) -> None:
        """Play back the dataset frame by frame at the requested speed.
        stop_on_end=True (terminal-only) ends the program when the data runs
        out; False (GUI mode) leaves the window open to inspect afterwards."""
        loop_start = time.perf_counter()
        idx = 0
        nframes = 0
        while not stop_ev.is_set():
            if idx >= len(df):
                if args.loop:
                    idx = 0
                else:
                    if stop_on_end:
                        stop_ev.set()
                    break
            if not drain_commands():
                stop_ev.set()           # 'q' → stop everything (incl. GUI)
                break
            process_frame(idx)
            idx += 1
            nframes += 1
            leftover = nframes * sleep_s - (time.perf_counter() - loop_start)
            if leftover > 0:
                time.sleep(min(leftover, 0.25))

    # ── Run: terminal dashboard + (by default) live GUI plot window ───────────
    want_gui = not args.no_gui
    gui_ok   = want_gui and gui_available()
    if want_gui and not gui_ok:
        console.print("[yellow]No GUI backend available — terminal dashboard "
                      "only. Install one (pip install PyQt5) or run where a "
                      "display exists.[/yellow]")

    if gui_ok:
        # Streaming + terminal dashboard run in background threads; the GUI
        # owns the main thread (matplotlib requirement). The GUI reads the same
        # live State the streamer writes, so it is real-time in sync with the
        # packets sent to the ESP32.
        worker = threading.Thread(target=stream_worker, args=(False,), daemon=True)
        worker.start()

        def dashboard_loop():
            try:
                with Live(build_layout(s), refresh_per_second=8,
                          console=console, screen=True) as live:
                    while not stop_ev.is_set():
                        live.update(build_layout(s))
                        time.sleep(0.12)
            except Exception:
                pass
        dash = threading.Thread(target=dashboard_loop, daemon=True)
        dash.start()

        try:
            run_gui(s, stop_ev, cmd_q)   # blocks until window closed or quit
        finally:
            stop_ev.set()
            worker.join(timeout=1.0)
            dash.join(timeout=1.0)
            log_f.close()
            if ser:
                ser.close()
    else:
        loop_start = time.perf_counter()
        with Live(build_layout(s), refresh_per_second=8,
                  console=console, screen=True) as live:
            try:
                idx = 0
                nframes = 0
                while True:
                    if idx >= len(df):
                        if args.loop:
                            idx = 0
                        else:
                            break
                    if not drain_commands():
                        raise KeyboardInterrupt
                    process_frame(idx)
                    live.update(build_layout(s))
                    idx += 1
                    nframes += 1
                    leftover = nframes * sleep_s - (time.perf_counter() - loop_start)
                    if leftover > 0:
                        time.sleep(leftover)
            except KeyboardInterrupt:
                pass
            finally:
                stop_ev.set()
                log_f.close()
                if ser:
                    ser.close()

    console.print(f"\n[green]Done.[/green] Session log: {args.logfile}")
    console.print(f"TP={s.TP}  TN={s.TN}  FP={s.FP}  FN={s.FN}  "
                  f"Prec={s.precision():.0f}%  Recall={s.recall():.0f}%")
    if detsim:
        console.print(detsim.report())


if __name__ == "__main__":
    main()
