#!/usr/bin/env python3
"""
bms_state.py  —  Team Imperium  |  Caterpillar Tech Challenge
=============================================================
Shared data model: the fault catalogue constants and the `State` object that
the streaming thread writes and the terminal dashboard + GUI window read.

Kept in its own module so both  dashboard.py  and  gui_plotter.py  can use it
without importing the big  monitor.py  orchestration file.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from rich.text import Text


# ── Fault catalogue (shared by the detectors mirror, dashboard, and logs) ─────
#  Index order MUST match the C++ FaultID enum in BMS_ESP32_Demo/types.h so the
#  Python sim (detector_sim.py) and the ESP32 firmware agree on bit positions.
FAULT_NAMES = [
    "VOLT_IMBAL", "THERMAL_HOT", "SENSOR_LOSS",
    "VOLT_GRADIENT", "TEMP_GRADIENT", "TEMP_LOW",
    "GAS_PRESSURE", "EIS_FAULT", "SOC_LOW", "CELL_BALANCE",
    # Expanded physical taxonomy (fault_detection.md §3 A,C,E,K) — appended so
    # existing bit positions are unchanged. Keep in sync with types.h FaultID.
    "CELL_OV_UV", "OVERCURRENT", "RUNAWAY_WARN", "CONTACT_RES",
]
NUM_FAULTS = len(FAULT_NAMES)

FAULT_LEVELS = [
    "WARNING", "CRITICAL", "CRITICAL",
    "HIGH",    "HIGH",     "HIGH",
    "CRITICAL", "HIGH",    "WARNING",  "ACTION",
    "CRITICAL", "CRITICAL", "CRITICAL", "HIGH",
]

# ── Advisory faults — excluded from TP/FP/FN scoring ──────────────────────────
#  VOLT_IMBAL (0), SOC_LOW (8) and CELL_BALANCE (9) are normal operating
#  conditions / housekeeping, not labeled fault EVENTS: low SOC happens during
#  normal end-of-discharge and cell imbalance is routine. They still display,
#  log and drive LEDs/contactors, but counting them as "detections" floods the
#  false-positive count (low SOC alone caused ~3750 FPs on normal rows). So
#  detection accuracy is scored only on the genuine safety faults below.
#  Keep this in sync with SCORING_MASK in BMS_ESP32_Demo/detectors.h logValidation().
ADVISORY_FAULTS = {0, 8, 9}
SCORING_MASK = sum(1 << i for i in range(NUM_FAULTS) if i not in ADVISORY_FAULTS)


def is_detected(fault_bits: int) -> bool:
    """True if any *scored* (non-advisory) fault bit is set."""
    return (fault_bits & SCORING_MASK) != 0


# ─────────────────────────────────────────────────────────────────────────────
#  State dataclass  (shared between the streaming thread and the UI threads)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class State:
    # Current sensor values (updated every frame)
    seq:          int   = 0
    time_s:       float = 0.0
    v:            list  = field(default_factory=lambda: [0.0, 0.0, 0.0])
    T_surf:       float = 25.0
    T_rise:       float = 0.0
    current:      float = 0.0
    acoustic:     float = 0.0
    SOC_pct:      float = 100.0    # SOC from the dataset (ground truth)
    soc_est:      float = 100.0    # SOC estimated by the UKF
    soh_est:      float = 100.0    # SOH (capacity health) estimated by the UKF
    ground_truth: int   = 0
    fault_type_str: str = "Normal"

    # Fault state (updated from ESP32 replies or dry-run simulator)
    fault_bits:       int  = 0               # bitmask of active faults
    fault_severities: list = field(default_factory=lambda: [0.0] * NUM_FAULTS)

    # MOSFET states
    chg_open: bool = True    # starts open (safe default)
    dsc_open: bool = True

    # Event log (list of rich Text objects, newest first)
    events: list = field(default_factory=list)

    # Live plot history (consumed by the GUI window in real time)
    hist_t:        list = field(default_factory=list)
    hist_soc_data: list = field(default_factory=list)
    hist_soc_est:  list = field(default_factory=list)
    hist_soh:      list = field(default_factory=list)
    hist_sev:      list = field(default_factory=list)
    hist_v1:       list = field(default_factory=list)
    hist_v2:       list = field(default_factory=list)
    hist_v3:       list = field(default_factory=list)

    # Detection accuracy counters
    TP: int = 0
    TN: int = 0
    FP: int = 0
    FN: int = 0

    # Connection / metadata
    connected:  bool = False
    rtt_ms:     int  = 0
    scenario:   str  = "—"
    sample_hz:  int  = 10
    speed:      float = 1.0

    # Internal
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_event(self, text: Text) -> None:
        """Add to the front of the event log, keep last 8."""
        with self.lock:
            self.events.insert(0, text)
            if len(self.events) > 8:
                self.events.pop()

    def push_history(self, t: float, soc_data: float, soc_est: float,
                     soh: float, sev: float, v1: float, v2: float, v3: float,
                     cap: int = 4000) -> None:
        """Append one trend sample for every plot series. Decimates by 2 when
        full so the whole run stays visible with bounded memory."""
        with self.lock:
            self.hist_t.append(t)
            self.hist_soc_data.append(soc_data)
            self.hist_soc_est.append(soc_est)
            self.hist_soh.append(soh)
            self.hist_sev.append(sev)
            self.hist_v1.append(v1)
            self.hist_v2.append(v2)
            self.hist_v3.append(v3)
            if len(self.hist_t) > cap:
                self.hist_t        = self.hist_t[::2]
                self.hist_soc_data = self.hist_soc_data[::2]
                self.hist_soc_est  = self.hist_soc_est[::2]
                self.hist_soh      = self.hist_soh[::2]
                self.hist_sev      = self.hist_sev[::2]
                self.hist_v1       = self.hist_v1[::2]
                self.hist_v2       = self.hist_v2[::2]
                self.hist_v3       = self.hist_v3[::2]

    def precision(self) -> float:
        denom = self.TP + self.FP
        return (self.TP / denom * 100) if denom else 0.0

    def recall(self) -> float:
        denom = self.TP + self.FN
        return (self.TP / denom * 100) if denom else 0.0

    def f1(self) -> float:
        p, r = self.precision(), self.recall()
        denom = p + r
        return (2 * p * r / denom) if denom else 0.0
