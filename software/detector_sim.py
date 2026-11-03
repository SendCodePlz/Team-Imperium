#!/usr/bin/env python3
"""
detector_sim.py  —  Team Imperium  |  Caterpillar Tech Challenge
================================================================
Python mirror of the ESP32 fault-detection firmware
(BMS_ESP32_Demo/detectors.h + config.h + types.h).

This lets us run our ACTUAL fault algorithm against the dataset on the PC,
without flashing the ESP32. It is the engine behind monitor.py's `--sim` mode.

  --dry-run        replays the dataset's ground-truth labels  → tests the UI
  --dry-run --sim  runs THESE detectors on the raw signals    → tests the algo
  --port COMx      runs the real C++ firmware on hardware      → final demo

Every threshold, persistence counter, Welford z-score and severity ceiling here
is a line-for-line copy of the C++. If you change a threshold in config.h, change
it here too (and vice-versa) — they must stay in sync to be a valid mirror.

The one faithful difference: SENSOR_LOSS's frame-timeout and sequence-gap
branches can't fire in replay (frames are always fresh and sequential), so only
its noise-pattern branch is active here — exactly what would happen on hardware
fed a clean stream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from bms_state import FAULT_NAMES, NUM_FAULTS
from soc_ukf import ocv, R0, I_NOM

# ── Fault IDs — must match BMS_ESP32_Demo/types.h FaultID enum ────────────────
FLT_VOLT_IMBAL    = 0
FLT_THERMAL_HOT   = 1
FLT_SENSOR_LOSS   = 2
FLT_VOLT_GRADIENT = 3
FLT_TEMP_GRADIENT = 4
FLT_TEMP_LOW      = 5
FLT_GAS_PRESSURE  = 6
FLT_EIS_FAULT     = 7
FLT_SOC_LOW       = 8
FLT_CELL_BALANCE  = 9
FLT_CELL_OV_UV    = 10   # per-cell over/under-voltage (NEW)
FLT_OVERCURRENT   = 11   # |I| beyond rated continuous/peak (NEW)
FLT_RUNAWAY_WARN  = 12   # thermal-runaway precursor (NEW)
FLT_CONTACT_RES   = 13   # per-group contact / connection resistance (NEW)
FLT_COUNT         = 14

# ── Thresholds — must match BMS_ESP32_Demo/config.h ──────────────────────────
THR_IMBAL_V         = 0.050
THR_T_SURF_HOT      = 38.0
THR_T_RISE_FAULT    = 12.0
THR_T_COLD          = 18.0
THR_DT_DT           = 1.2
THR_PERSIST_N       = 8
THR_ACOUSTIC_RMS    = 0.060
THR_IMPACT_FORCE    = 10.0
THR_PRES_HIGH_KPA   = 105.0
THR_PRES_IMPACT_KPA = 115.0
THR_DP_DT           = 0.8
THR_DV_DT_HIGH      = 0.28
THR_DV_DT_CRITICAL  = 0.60
THR_EIS_ZSCORE      = 4.0
EIS_WARMUP_N        = 30
# Filtered-signal internal-fault detectors (micro-short + impedance growth).
# Raw v/current are too noisy per-frame; an EMA exposes the small SUSTAINED
# offsets these faults produce. EMA_ALPHA≈0.1 ≈ 1 s time-constant at 10 Hz.
# See fault_detection.md §2/§3 (I, J) for the physics + noise rationale.
EMA_ALPHA           = 0.10
MIN_LOAD_A          = 0.5    # resistance/impedance only observable under load (A)
THR_INTF_PERSIST    = 3      # frames the internal-fault branch must hold

# Micro-short (demo heuristic): parasitic draw AND acoustic emission must BOTH
# hold, so a drifting current sensor alone can't trip it. Dataset-specific — the
# physical detector is dV/dQ + rest-phase coulombic efficiency (production stub).
THR_MICRO_I_FILT    = 2.95   # |filtered current| (A)
THR_MICRO_ACOUSTIC  = 0.025  # acoustic RMS (g)

# Impedance growth is NOT a per-frame fault here — it is a multi-cycle aging
# signature surfaced via RUL power-fade (fault_detection.md §4). The internal-
# resistance estimator _r_excess() is retained for RUL + contact-resistance.

# New physical guard detectors (see fault_detection.md §3 A,C,E,K). Thresholds
# from cell spec, NOT shaved to this dataset — inert on the constant-1C demo.
V_CELL_MAX          = 4.20   # per-cell over-voltage limit (V)
V_CELL_MIN          = 2.50   # per-cell under-voltage limit (V)
I_RATED_CONT        = 5.0    # continuous current rating (A) ≈ 1.7C of 2.9 Ah
I_RATED_PEAK        = 10.0   # instantaneous peak current rating (A)
OC_CONT_PERSIST     = 20     # frames over continuous rating before tripping (~2 s)
THR_CONTACT_DR_OHM  = 0.020  # per-group excess-R divergence vs siblings (Ω)
OVUV_PERSIST        = 3       # frames a cell must stay out-of-window
THR_SOC_WARN_PCT    = 15.0
THR_SOC_CUTOFF_PCT  = 5.0
CELL_CAPACITY_AH    = 2.9

# ── Severity action tiers — must match config.h ──────────────────────────────
SEV_INHIBIT_LO  = 0.30
SEV_OPEN_ALL_LO = 0.55
SEV_LATCH_LO    = 0.80

TIER_NORMAL      = 0
TIER_INHIBIT_CHG = 1
TIER_OPEN_ALL    = 2
TIER_LATCH_OPEN  = 3
TIER_NAMES = ["NORMAL", "INHIBIT_CHG", "OPEN_ALL", "LATCH_OPEN"]


def _clampf(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


# ── Incoming sensor frame — mirror of the C++ `struct Frame` ─────────────────
@dataclass
class Frame:
    seq:          int = 0
    v:            list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    T_surf:       float = 25.0
    T_rise:       float = 0.0
    current:      float = 0.0
    acoustic_rms: float = 0.0
    pressure:     float = 101.3
    impact_force: float = 0.0
    dVdt:         float = 0.0
    dTdt:         float = 0.0
    dPdt:         float = 0.0
    SOC_pct:      float = 100.0
    ts_ms:        int = 0
    soc_est:      float = -1.0   # UKF SOC estimate [%]; <0 → fall back to SOC_pct


class DetectorSim:
    """Mirror of detectors.h + the DetectorState struct.

    Call .step(frame) once per frame. It runs all 10 detectors, recomputes
    severity and contactor state, and returns a result dict.
    """

    def __init__(self) -> None:
        # Persistence counters
        self.hot_cnt   = 0
        self.cold_cnt  = 0
        self.tgrad_cnt = 0
        # Sensor-loss tracking
        self.next_expected_seq = 0
        # Welford online statistics on dVdt (EIS detector)
        self.eis_n    = 0
        self.eis_mean = 0.0
        self.eis_M2   = 0.0
        # EMA-filtered current/voltage for the internal-fault branches
        self._filt_init = False
        self.i_filt     = 0.0
        self.v_filt     = 0.0
        self.intf_cnt   = 0
        # Per-group filtered voltage (contact-resistance divergence)
        self._cf_init   = False
        self.vf3        = [0.0, 0.0, 0.0]
        # New-detector persistence counters
        self.ovuv_cnt    = 0   # over/under-voltage
        self.oc_cnt      = 0   # overcurrent (continuous)
        self.rw_cnt      = 0   # runaway precursor (dTdt)
        self.contact_cnt = 0   # contact resistance
        # Inter-frame state
        self.prev_ts_ms = 0
        self.prev_valid = False
        # Coulomb-counting SOC cross-check
        self.soc_cc  = [0.0, 0.0, 0.0]
        self.cc_init = False
        # Passive balance flags
        self.balance_cell = [False, False, False]
        # Latch + severity/tier
        self.latched         = False
        self.severity        = [0.0] * FLT_COUNT
        self.system_severity = 0.0
        self.action_tier     = TIER_NORMAL
        # Outputs
        self.fault_bits = 0
        self.chg_open   = False
        self.dsc_open   = False
        # Diagnostics (not in firmware — for the sim report)
        self.fire_count    = [0] * FLT_COUNT   # rising-edge activations
        self.active_frames = [0] * FLT_COUNT   # frames the bit was held

    # ── set/clear one fault bit (mirror of setFault) ─────────────────────────
    def _set(self, fid: int, active: bool) -> None:
        bit = 1 << fid
        was = bool(self.fault_bits & bit)
        if active and not was:
            self.fault_bits |= bit
            self.fire_count[fid] += 1
        elif not active and was:
            self.fault_bits &= ~bit
            self.severity[fid] = 0.0
        if active:
            self.active_frames[fid] += 1

    # ── Detectors (1:1 with detectors.h) ─────────────────────────────────────
    def _voltage_imbalance(self, f: Frame) -> None:
        spread = max(f.v) - min(f.v)
        self._set(FLT_VOLT_IMBAL, spread > THR_IMBAL_V)

    def _thermal_hotspot(self, f: Frame) -> None:
        hot = (f.T_surf > THR_T_SURF_HOT) or (f.T_rise > THR_T_RISE_FAULT)
        if hot:
            self.hot_cnt = min(THR_PERSIST_N, self.hot_cnt + 1)
        elif self.hot_cnt > 0:
            self.hot_cnt -= 1
        self._set(FLT_THERMAL_HOT, self.hot_cnt >= THR_PERSIST_N)

    def _sensor_loss(self, f: Frame) -> None:
        # Timeout (A) and sequence-gap (B) can't trigger in clean replay; only
        # the noise-pattern branch (C) is meaningful here.
        self.next_expected_seq = (f.seq + 1) & 0xFFFF
        noise = (abs(f.dVdt) > THR_DV_DT_CRITICAL) and \
                (abs(f.dPdt) < 0.5) and (abs(f.dTdt) < 0.5)
        self._set(FLT_SENSOR_LOSS, noise)

    def _voltage_gradient(self, f: Frame) -> None:
        self._set(FLT_VOLT_GRADIENT, abs(f.dVdt) > THR_DV_DT_HIGH)

    def _temp_gradient(self, f: Frame) -> None:
        spike = abs(f.dTdt) > THR_DT_DT
        if spike:
            self.tgrad_cnt = min(THR_PERSIST_N, self.tgrad_cnt + 1)
        elif self.tgrad_cnt > 0:
            self.tgrad_cnt -= 1
        self._set(FLT_TEMP_GRADIENT, self.tgrad_cnt >= THR_PERSIST_N)

    def _low_temp(self, f: Frame) -> None:
        cold = f.T_surf < THR_T_COLD
        if cold:
            self.cold_cnt = min(THR_PERSIST_N, self.cold_cnt + 1)
        elif self.cold_cnt > 0:
            self.cold_cnt -= 1
        self._set(FLT_TEMP_LOW, self.cold_cnt >= THR_PERSIST_N)

    def _gas_pressure(self, f: Frame) -> None:
        # Gas generation is a slow sustained pressure rise (dPdt stays ~0), so an
        # absolute pressure threshold is the right discriminator. The old
        # "and dPdt > THR_DP_DT" conjunct contradicted that physics and is removed.
        gas_trip    = (f.pressure > THR_PRES_HIGH_KPA)
        impact_trip = (f.pressure > THR_PRES_IMPACT_KPA) or \
                      (f.acoustic_rms > THR_ACOUSTIC_RMS) or \
                      (abs(f.impact_force) > THR_IMPACT_FORCE)
        self._set(FLT_GAS_PRESSURE, gas_trip or impact_trip)

    # ── internal-resistance estimator (fault_detection.md §2) ────────────────
    def _soc_frac(self, f: Frame) -> float:
        """SOC [0..1] for the OCV reference: UKF estimate if supplied, else the
        streamed SOC. Real deployment uses the estimate (it isn't measured)."""
        return (f.soc_est if f.soc_est >= 0.0 else f.SOC_pct) / 100.0

    def _r_excess(self, v_filt: float, f: Frame) -> float:
        """Excess series resistance [Ω] beyond the healthy 1-RC model, NORMALIZED
        by current so it is load-independent. ~0 healthy; positive when degraded.
            R_excess = (OCV(SOC) + (I−I_NOM)·R0 − V_filt) / |I|
        OCV is fitted at I_NOM (folds in the nominal IR drop), so the (I−I_NOM)·R0
        term corrects for off-nominal load before dividing out the current."""
        i = self.i_filt
        if abs(i) < MIN_LOAD_A:
            return 0.0
        v_pred = ocv(self._soc_frac(f)) + (i - I_NOM) * R0
        return (v_pred - v_filt) / abs(i)

    def _eis_fault(self, f: Frame) -> None:
        # Welford adaptive baseline on dVdt — runs every frame (noise rule 3).
        self.eis_n += 1
        delta = f.dVdt - self.eis_mean
        self.eis_mean += delta / self.eis_n
        self.eis_M2   += delta * (f.dVdt - self.eis_mean)

        active = False
        if self.eis_n >= EIS_WARMUP_N:
            sigma = math.sqrt(self.eis_M2 / (self.eis_n - 1)) if self.eis_n > 1 else 0.001
            if sigma > 1e-6:
                z = abs(f.dVdt - self.eis_mean) / sigma
                if z > THR_EIS_ZSCORE:
                    active = True
        # Legacy micro-short heuristic: elevated acoustic + abnormal T_rise.
        if (not active) and (f.acoustic_rms > 0.042) and (f.T_rise > 4.5):
            active = True

        # ── EMA filter (noise rule 1) feeding the normalized branches ────────
        vmean = sum(f.v) / 3.0
        if not self._filt_init:
            self.i_filt, self.v_filt = f.current, vmean
            self._filt_init = True
        else:
            self.i_filt += EMA_ALPHA * (f.current - self.i_filt)
            self.v_filt += EMA_ALPHA * (vmean - self.v_filt)

        # Micro-short (demo heuristic): small STEADY extra draw AND acoustic
        # emission together (rule 5 — two independent channels), so a drifting
        # current sensor alone can't trip it. Production path = dV/dQ shift +
        # rest-phase coulombic efficiency (inert here; constant-current stream).
        micro = (abs(self.i_filt) > THR_MICRO_I_FILT) and \
                (f.acoustic_rms > THR_MICRO_ACOUSTIC)
        if micro:
            self.intf_cnt = min(THR_PERSIST_N, self.intf_cnt + 1)
        elif self.intf_cnt > 0:
            self.intf_cnt -= 1
        if self.intf_cnt >= THR_INTF_PERSIST:
            active = True

        # NOTE: impedance growth is a MULTI-CYCLE AGING signature, not a per-frame
        # fault. Within one constant-current cycle the SOC-estimate error (~3–5%)
        # swamps the ~0.014 Ω resistance rise, so it is surfaced through RUL
        # power-fade (fault_detection.md §4), NOT flagged as an instantaneous
        # fault. _r_excess() is retained for RUL + contact-resistance (K).

        self._set(FLT_EIS_FAULT, active)

    # ── New physical guard detectors (fault_detection.md §3 A,C,E,K) ─────────
    def _cell_ov_uv(self, f: Frame) -> None:
        """Per-cell over/under-voltage vs the safe window + short persistence."""
        bad = any((v > V_CELL_MAX or v < V_CELL_MIN) for v in f.v)
        if bad:
            self.ovuv_cnt = min(THR_PERSIST_N, self.ovuv_cnt + 1)
        elif self.ovuv_cnt > 0:
            self.ovuv_cnt -= 1
        self._set(FLT_CELL_OV_UV, self.ovuv_cnt >= OVUV_PERSIST)

    def _overcurrent(self, f: Frame) -> None:
        """Two-level: instantaneous peak (raw) OR sustained over continuous
        rating (filtered + time). Uses i_filt updated in _eis_fault."""
        peak = abs(f.current) > I_RATED_PEAK
        if abs(self.i_filt) > I_RATED_CONT:
            self.oc_cnt = min(255, self.oc_cnt + 1)
        elif self.oc_cnt > 0:
            self.oc_cnt -= 1
        self._set(FLT_OVERCURRENT, peak or (self.oc_cnt >= OC_CONT_PERSIST))

    def _runaway_precursor(self, f: Frame) -> None:
        """Multi-signal onset fingerprint (rule 5): sustained temp acceleration
        AND gas pressure AND a voltage collapse — never any one noisy channel."""
        if abs(f.dTdt) > THR_DT_DT:
            self.rw_cnt = min(THR_PERSIST_N, self.rw_cnt + 1)
        elif self.rw_cnt > 0:
            self.rw_cnt -= 1
        precursor = (self.rw_cnt >= THR_INTF_PERSIST) and \
                    (f.pressure > THR_PRES_HIGH_KPA) and \
                    (f.dVdt < -THR_DV_DT_HIGH)
        self._set(FLT_RUNAWAY_WARN, precursor)

    def _contact_resistance(self, f: Frame) -> None:
        """Loose lead / corroded joint: ONE group's excess series resistance
        diverges from its siblings (per-group R_excess, rule 4 + per-cell)."""
        if not self._cf_init:
            self.vf3 = list(f.v)
            self._cf_init = True
        else:
            for i in range(3):
                self.vf3[i] += EMA_ALPHA * (f.v[i] - self.vf3[i])
        active = False
        if abs(self.i_filt) > MIN_LOAD_A:
            r = sorted(self._r_excess(self.vf3[i], f) for i in range(3))
            diverge = r[2] - r[1]          # worst group above the median
            if diverge > THR_CONTACT_DR_OHM:
                self.contact_cnt = min(THR_PERSIST_N, self.contact_cnt + 1)
            elif self.contact_cnt > 0:
                self.contact_cnt -= 1
            active = self.contact_cnt >= THR_INTF_PERSIST
        self._set(FLT_CONTACT_RES, active)

    def _soc_low(self, f: Frame) -> None:
        if (not self.cc_init) and abs(f.current) > 0.1:
            self.soc_cc = [f.SOC_pct / 100.0] * 3
            self.cc_init = True
        if self.cc_init and self.prev_valid:
            dt_s = (f.ts_ms - self.prev_ts_ms) / 1000.0
            if dt_s > 0.001:
                for i in range(3):
                    self.soc_cc[i] -= (f.current * dt_s) / (CELL_CAPACITY_AH * 3600.0)
                    self.soc_cc[i]  = _clampf(self.soc_cc[i], 0.0, 1.0)
        self._set(FLT_SOC_LOW, f.SOC_pct < THR_SOC_WARN_PCT)

    def _cell_balance(self, f: Frame) -> None:
        spread = max(f.v) - min(f.v)
        needed = spread > THR_IMBAL_V
        vmean  = sum(f.v) / 3.0
        for i in range(3):
            self.balance_cell[i] = needed and (f.v[i] > vmean + THR_IMBAL_V / 2.0)
        self._set(FLT_CELL_BALANCE, needed)

    # ── Severity (mirror of computeFaultSeverity) ────────────────────────────
    def _compute_severity(self, fid: int, f: Frame) -> float:
        if fid == FLT_THERMAL_HOT:
            s = (f.T_surf - THR_T_SURF_HOT) / (65.0 - THR_T_SURF_HOT)
        elif fid == FLT_VOLT_GRADIENT:
            s = (abs(f.dVdt) - THR_DV_DT_HIGH) / (1.0 - THR_DV_DT_HIGH)
        elif fid == FLT_GAS_PRESSURE:
            s = (f.pressure - THR_PRES_HIGH_KPA) / (140.0 - THR_PRES_HIGH_KPA)
        elif fid == FLT_SENSOR_LOSS:
            s = 0.65
        elif fid == FLT_TEMP_GRADIENT:
            s = (abs(f.dTdt) - THR_DT_DT) / (5.0 - THR_DT_DT)
        elif fid == FLT_EIS_FAULT:
            s = (abs(f.dVdt) - THR_DV_DT_HIGH) / (1.0 - THR_DV_DT_HIGH)
            if f.acoustic_rms > THR_ACOUSTIC_RMS:
                s = max(s, (f.acoustic_rms - THR_ACOUSTIC_RMS) / (0.70 - THR_ACOUSTIC_RMS))
        elif fid == FLT_SOC_LOW:
            s = _clampf((THR_SOC_WARN_PCT - f.SOC_pct) / THR_SOC_WARN_PCT, 0.0, 0.54)
        elif fid == FLT_VOLT_IMBAL:
            spread = max(f.v) - min(f.v)
            s = _clampf((spread - THR_IMBAL_V) / (0.20 - THR_IMBAL_V), 0.0, 0.29)
        elif fid == FLT_TEMP_LOW:
            s = 0.35
        elif fid == FLT_CELL_BALANCE:
            s = 0.05
        elif fid == FLT_CELL_OV_UV:
            s = 0.75    # out-of-window cell → OPEN_ALL
        elif fid == FLT_OVERCURRENT:
            s = 0.65    # over rated current → OPEN_ALL
        elif fid == FLT_RUNAWAY_WARN:
            s = 0.90    # runaway onset → LATCH_OPEN
        elif fid == FLT_CONTACT_RES:
            s = 0.45    # connection fault → INHIBIT/advisory
        else:
            s = 0.10
        return _clampf(s, 0.0, 1.0)

    def _update_severity(self, f: Frame) -> None:
        max_sev = 0.0
        for fid in range(FLT_COUNT):
            if self.fault_bits & (1 << fid):
                self.severity[fid] = self._compute_severity(fid, f)
                max_sev = max(max_sev, self.severity[fid])
            else:
                self.severity[fid] = 0.0
        self.system_severity = max_sev
        if max_sev >= SEV_LATCH_LO:
            self.action_tier = TIER_LATCH_OPEN
        elif max_sev >= SEV_OPEN_ALL_LO:
            self.action_tier = TIER_OPEN_ALL
        elif max_sev >= SEV_INHIBIT_LO:
            self.action_tier = TIER_INHIBIT_CHG
        else:
            self.action_tier = TIER_NORMAL

    def _update_contactors(self) -> None:
        if self.action_tier == TIER_LATCH_OPEN:
            self.latched = True
        if self.latched or self.action_tier in (TIER_OPEN_ALL, TIER_LATCH_OPEN):
            self.chg_open, self.dsc_open = True, True
        elif self.action_tier == TIER_INHIBIT_CHG:
            self.chg_open, self.dsc_open = True, False
        else:
            self.chg_open, self.dsc_open = False, False

    # ── Master dispatcher (mirror of runAllDetectors) ────────────────────────
    def step(self, f: Frame) -> dict:
        self._sensor_loss(f)
        self._voltage_imbalance(f)
        self._low_temp(f)
        self._thermal_hotspot(f)
        self._voltage_gradient(f)
        self._temp_gradient(f)
        self._gas_pressure(f)
        self._eis_fault(f)            # updates i_filt/v_filt used below
        self._cell_ov_uv(f)
        self._overcurrent(f)
        self._runaway_precursor(f)
        self._contact_resistance(f)
        self._cell_balance(f)
        self._soc_low(f)

        self._update_severity(f)
        self._update_contactors()

        self.prev_ts_ms = f.ts_ms
        self.prev_valid = True

        return {
            "fault_bits": self.fault_bits,
            "severities": list(self.severity),
            "system_severity": self.system_severity,
            "action_tier": self.action_tier,
            "chg_open": self.chg_open,
            "dsc_open": self.dsc_open,
        }

    def active_fault_names(self) -> list[str]:
        return [FAULT_NAMES[i] for i in range(NUM_FAULTS)
                if self.fault_bits & (1 << i)]