#!/usr/bin/env python3
"""
soc_ukf.py  —  Team Imperium  |  Caterpillar Tech Challenge
===========================================================
Joint SOC + SOH estimator using an Unscented Kalman Filter (UKF).

This is the *Python mirror* of the on-device C++ filter in
BMS_ESP32_Demo/src/ukf_soh/ukf_soh.h — both implement the exact same model
and tuning, so the dashboard shows the same estimate whether it runs in
dry-run (this file) or on the ESP32.

MODEL  (1-RC Thevenin equivalent circuit + joint capacity state)
----------------------------------------------------------------
State  x = [ SOC , Vrc , SOH ]
    SOC   state of charge            [0..1]
    Vrc   RC-pair polarisation volt  [V]
    SOH   capacity health fraction   [~1.0], usable_cap = Q_nom * SOH

Process (current I in A, discharge negative, dt in s):
    cap_As   = Q_nom_Ah * SOH * 3600
    SOC[k+1] = SOC[k] + I*dt / cap_As
    Vrc[k+1] = Vrc[k]*exp(-dt/tau) + I*R1*(1 - exp(-dt/tau)),  tau = R1*C1
    SOH[k+1] = SOH[k]                      (slow random walk)

Measurement (terminal voltage):
    V = OCV(SOC) + I*R0 + Vrc

OCV(SOC) uses the Chen / Rincón-Mora NMC-style open-circuit-voltage curve.

USAGE
-----
    from soc_ukf import SocUkf
    ukf = SocUkf(soc0=1.0)
    soc_pct, soh_pct = ukf.update(voltage=4.05, current=-2.9, dt=0.1)
"""

from __future__ import annotations

import math

import numpy as np

# ── Cell / model parameters (shared verbatim with the C++ port) ───────────────
Q_NOM_AH = 2.9        # nominal cell capacity                     [Ah]
R0       = 0.030      # ohmic series resistance                   [Ω]
R1       = 0.015      # RC-pair resistance                        [Ω]
C1       = 2000.0     # RC-pair capacitance                       [F]  (tau = 30 s)
I_NOM    = -2.9       # nominal discharge current the OCV curve was fit at [A]

# Process / measurement noise (tuned for the 10 Hz NMC dataset)
Q_SOC    = 1.0e-7     # SOC process variance per step
Q_VRC    = 1.0e-5     # Vrc process variance per step
Q_SOH    = 1.0e-9     # SOH process variance per step (very slow)
R_VOLT   = (0.010) ** 2   # voltage measurement variance  (10 mV σ)

# Initial covariance
P0_SOC   = 1.0e-2
P0_VRC   = 1.0e-3
P0_SOH   = 2.0e-2

# Unscented transform (Van der Merwe scaled sigma points).
# alpha=1.0, kappa=0 (=> lambda=0) keeps all sigma-point weights O(1), which
# is numerically safe on the ESP32's 32-bit FPU. The C++ port uses the same.
_ALPHA = 1.0
_BETA  = 2.0
_KAPPA = 0.0


def ocv(soc: float) -> float:
    """Terminal-voltage-vs-SOC curve fitted to the NMC dataset at I_NOM.

    Degree-5 polynomial fit of voltage_V against SOC over all normal rows
    (fit residual σ ≈ 11 mV). Because the dataset runs at a near-constant
    ~2.9 A discharge, this curve already folds in the steady IR drop at
    I_NOM; the UKF measurement model corrects for any current deviation
    from I_NOM via R0.
    """
    s = min(1.0, max(0.0, soc))
    return (((((-0.411719 * s + 0.063607) * s + 1.274983) * s
              - 1.732312) * s + 2.062802) * s + 2.777598)


class SocUkf:
    """3-state joint SOC/SOH Unscented Kalman Filter."""

    N = 3  # state dimension

    def __init__(self, soc0: float = 1.0, soh0: float = 1.0):
        n = self.N
        self.x = np.array([min(1.0, max(0.0, soc0)), 0.0, soh0], dtype=float)
        self.P = np.diag([P0_SOC, P0_VRC, P0_SOH]).astype(float)
        self.Q = np.diag([Q_SOC, Q_VRC, Q_SOH]).astype(float)
        self.R = float(R_VOLT)

        # Sigma-point weights
        lam = _ALPHA ** 2 * (n + _KAPPA) - n
        self._lambda = lam
        self._gamma = math.sqrt(n + lam)
        self.Wm = np.full(2 * n + 1, 1.0 / (2.0 * (n + lam)))
        self.Wc = self.Wm.copy()
        self.Wm[0] = lam / (n + lam)
        self.Wc[0] = lam / (n + lam) + (1.0 - _ALPHA ** 2 + _BETA)

        self._initialised = True

    # ── model functions ──────────────────────────────────────────────────────
    @staticmethod
    def _f(x: np.ndarray, current: float, dt: float) -> np.ndarray:
        soc, vrc, soh = x
        soh = max(0.5, min(1.2, soh))
        cap_as = Q_NOM_AH * soh * 3600.0
        soc_n = soc + (current * dt) / cap_as
        tau = R1 * C1
        a = math.exp(-dt / tau)
        vrc_n = vrc * a + current * R1 * (1.0 - a)
        return np.array([min(1.0, max(0.0, soc_n)), vrc_n, soh])

    @staticmethod
    def _h(x: np.ndarray, current: float) -> float:
        soc, vrc, _soh = x
        # OCV curve already includes the IR drop at I_NOM; only correct for
        # the deviation of the present current from that nominal operating point.
        return ocv(soc) + (current - I_NOM) * R0 + vrc

    # ── sigma points ─────────────────────────────────────────────────────────
    def _sigma_points(self) -> np.ndarray:
        n = self.N
        # Symmetric square root via Cholesky (P kept SPD by jitter below)
        try:
            S = np.linalg.cholesky((n + self._lambda) * self.P)
        except np.linalg.LinAlgError:
            S = np.linalg.cholesky((n + self._lambda) * self.P
                                   + 1e-9 * np.eye(n))
        pts = np.zeros((2 * n + 1, n))
        pts[0] = self.x
        for i in range(n):
            pts[i + 1]     = self.x + S[:, i]
            pts[n + i + 1] = self.x - S[:, i]
        return pts

    # ── one predict/update cycle ─────────────────────────────────────────────
    def update(self, voltage: float, current: float, dt: float) -> tuple[float, float]:
        """Advance the filter one step. Returns (SOC %, SOH %)."""
        if dt <= 0.0:
            dt = 0.1
        n = self.N

        # --- Predict ---
        pts = self._sigma_points()
        prop = np.array([self._f(p, current, dt) for p in pts])
        x_pred = self.Wm @ prop
        P_pred = self.Q.copy()
        for i in range(2 * n + 1):
            d = prop[i] - x_pred
            P_pred += self.Wc[i] * np.outer(d, d)

        # --- Update (measurement = terminal voltage) ---
        zs = np.array([self._h(p, current) for p in prop])
        z_pred = float(self.Wm @ zs)
        Pzz = self.R
        Pxz = np.zeros(n)
        for i in range(2 * n + 1):
            dz = zs[i] - z_pred
            Pzz += self.Wc[i] * dz * dz
            Pxz += self.Wc[i] * (prop[i] - x_pred) * dz

        K = Pxz / Pzz                       # Kalman gain (n,)
        innov = voltage - z_pred
        self.x = x_pred + K * innov
        self.P = P_pred - np.outer(K, K) * Pzz

        # Keep states physical
        self.x[0] = min(1.0, max(0.0, self.x[0]))
        self.x[2] = min(1.2, max(0.5, self.x[2]))
        # Symmetrise + jitter to stay SPD
        self.P = 0.5 * (self.P + self.P.T) + 1e-12 * np.eye(n)

        return self.x[0] * 100.0, self.x[2] * 100.0

    @property
    def soc_pct(self) -> float:
        return self.x[0] * 100.0

    @property
    def soh_pct(self) -> float:
        return self.x[2] * 100.0


# ── self-test: run the filter over the dataset and report tracking error ──────
if __name__ == "__main__":
    import sys
    import pandas as pd

    path = sys.argv[1] if len(sys.argv) > 1 else \
        "synthetic_NMC__1C_fault_EIS_dataset.xlsx"
    df = pd.read_excel(path, sheet_name="TimeSeries_Data")

    t = df["time_s"].to_numpy(float)
    v = df["voltage_V"].to_numpy(float)
    i = df["current_A"].to_numpy(float)
    soc_true = df["SOC_estimated_percent"].to_numpy(float)

    ukf = SocUkf(soc0=soc_true[0] / 100.0)
    est = np.zeros(len(df))
    soh = np.zeros(len(df))
    for k in range(len(df)):
        dt = (t[k] - t[k - 1]) if k > 0 else 0.1
        est[k], soh[k] = ukf.update(v[k], i[k], dt)

    err = est - soc_true
    print(f"rows={len(df)}")
    print(f"SOC est  start={est[0]:.1f}%  end={est[-1]:.1f}%   "
          f"true end={soc_true[-1]:.1f}%")
    print(f"SOC MAE  = {np.mean(np.abs(err)):.2f}%   "
          f"max|err| = {np.max(np.abs(err)):.2f}%")
    print(f"SOH est  start={soh[0]:.1f}%  end={soh[-1]:.1f}%")
