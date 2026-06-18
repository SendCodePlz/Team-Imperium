"""Shared utilities for NASA battery SOC / SOH / RUL analysis.

Imported by every stage notebook so loaders, the Coulomb-count function,
metrics, and plot helpers live in one place.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOM_TEMP_ROOT = '/Users/sidharth/Desktop/Caterpillar/Coding/room_temp_batteries'

Q_NOMINAL_AH = 2.0           # rated capacity for B0005-B0007 / B0018 (per README)
EOL_SOH = 0.70               # 30 % fade end-of-life
ACTIVE_DISCHARGE_I_A = -1.0  # threshold below which the cell is "actively discharging"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def battery_dir(battery_id: str) -> str:
    return os.path.join(ROOM_TEMP_ROOT, battery_id)


def load_metadata(battery_id: str) -> pd.DataFrame:
    """Full metadata for one battery — all cycle types."""
    return pd.read_csv(os.path.join(battery_dir(battery_id), 'metadata.csv'))


def load_cycle(battery_id: str, filename: str) -> pd.DataFrame:
    """Load a single cycle's time-series CSV (e.g. '05122.csv')."""
    return pd.read_csv(os.path.join(battery_dir(battery_id), 'data', filename))


def discharge_metadata(battery_id: str) -> pd.DataFrame:
    """Discharge cycles only, sorted chronologically, with cycle_num and SOH columns."""
    m = load_metadata(battery_id)
    d = m[m['type'] == 'discharge'].copy().sort_values('test_id').reset_index(drop=True)
    d['cycle_num'] = np.arange(1, len(d) + 1)
    d['SOH'] = d['Capacity'] / Q_NOMINAL_AH
    return d


def load_discharge_ground_truth(battery_id: str) -> pd.DataFrame:
    """Long-form per-sample ground truth: cycle_num, time_s, voltage, current,
    temperature, extracted_ah, soc_nominal, soh.

    Produced by the Stage 0 notebook.
    """
    path = os.path.join(battery_dir(battery_id), 'discharge_ground_truth.csv')
    return pd.read_csv(path)


# ---------------------------------------------------------------------------
# Coulomb counting (the same trapezoidal integral used in Stage 0)
# ---------------------------------------------------------------------------

def coulomb_count(time_s, current_a, soc_init: float = 1.0,
                  q_ah: float = Q_NOMINAL_AH) -> np.ndarray:
    """Causal Coulomb-counting SOC estimator.

    Parameters
    ----------
    time_s : array-like
        Sample timestamps in seconds.
    current_a : array-like
        Measured current in amps. Negative during discharge in this dataset.
    soc_init : float
        Initial SOC at time_s[0]. In ground-truth use this is 1.0 (post-charge).
        In a deployed estimator you have to *guess* this.
    q_ah : float
        Capacity used as the denominator. Use rated capacity for a naive
        estimator, or a per-cycle/aged value if you have one.

    Returns
    -------
    soc : np.ndarray
        SOC at every sample.
    """
    t = np.asarray(time_s, dtype=float)
    i = np.asarray(current_a, dtype=float)
    # trapezoidal cumulative integral of -I dt, in A·s
    extracted_as = np.concatenate([
        [0.0],
        np.cumsum(0.5 * (-i[:-1] + -i[1:]) * np.diff(t)),
    ])
    return soc_init - extracted_as / (q_ah * 3600.0)


# ---------------------------------------------------------------------------
# Error metrics
# ---------------------------------------------------------------------------

def rmse(y_true, y_pred) -> float:
    return float(np.sqrt(np.mean((np.asarray(y_true) - np.asarray(y_pred)) ** 2)))


def mae(y_true, y_pred) -> float:
    return float(np.mean(np.abs(np.asarray(y_true) - np.asarray(y_pred))))


def max_abs_err(y_true, y_pred) -> float:
    return float(np.max(np.abs(np.asarray(y_true) - np.asarray(y_pred))))


# ---------------------------------------------------------------------------
# Plot helpers
# ---------------------------------------------------------------------------

def plot_soc_comparison(t, truth, *estimates, labels=None,
                        title: str | None = None, ax=None):
    """Overlay one ground-truth curve and N estimator curves on a single axis.

    Pass `estimates` as separate positional args. `labels` should be a list of
    same length as `estimates`, or None to auto-name them.
    """
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 4))
    ax.plot(t, truth, 'k-', linewidth=1.6, label='Ground truth')

    if labels is None:
        labels = [f'Estimate {k+1}' for k in range(len(estimates))]
    for est, label in zip(estimates, labels):
        ax.plot(t, est, '-', alpha=0.85, label=label)

    ax.set_xlabel('Time (s)')
    ax.set_ylabel('SOC')
    if title:
        ax.set_title(title)
    ax.legend()
    ax.grid(True)
    return ax


def plot_soc_error(t, truth, *estimates, labels=None,
                   title: str | None = None, ax=None):
    """Plot signed error (estimate - truth) for each estimator."""
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 4))
    if labels is None:
        labels = [f'Estimate {k+1}' for k in range(len(estimates))]
    for est, label in zip(estimates, labels):
        ax.plot(t, np.asarray(est) - np.asarray(truth), '-', alpha=0.85, label=label)
    ax.axhline(0, color='k', linewidth=0.8)
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('SOC error  (estimate − truth)')
    if title:
        ax.set_title(title)
    ax.legend()
    ax.grid(True)
    return ax


# ---------------------------------------------------------------------------
# Static OCV lookup table  (the on-device / ESP representation)
# ---------------------------------------------------------------------------

def make_ocv_lookup(soc_grid, ocv_grid):
    """Wrap a (SOC, OCV) lookup table as a callable OCV(SOC).

    This is exactly what runs on the ESP: two flat arrays + linear interp
    (`np.interp` -> a `lerp` over the bracketing grid points on-device). SOC is
    clipped to [grid_min, grid_max] so the filter never extrapolates past the
    table, which is the failure mode a fitted polynomial has at SOC -> 0.
    """
    soc_grid = np.asarray(soc_grid, dtype=float)
    ocv_grid = np.asarray(ocv_grid, dtype=float)

    def ocv(soc):
        return np.interp(np.clip(soc, soc_grid[0], soc_grid[-1]), soc_grid, ocv_grid)

    return ocv


def build_static_ocv(battery_id, r0r1_prior, n_grid: int = 51,
                     n_cycles: int = 5, soc_lo: float = 0.15):
    """Build ONE static OCV(SOC) lookup table from a battery's fresh cycles.

    No SOH dependence — a single curve, the "normal OCV that is not fitted
    per-cycle". Anchors come from two sources, pooled over the first
    `n_cycles` (freshest) discharge cycles:

      * rested endpoints: V at t=0 (SOC=1, fully charged) and the relaxed
        voltage at the end of the post-discharge rest (SOC = SOC_end);
      * IR-corrected active-discharge samples ``V - I*(R0+R1)_prior`` for
        SOC > `soc_lo` (low-SOC samples are dropped because the cell's true
        impedance balloons there and drags the curve down).

    The pooled (SOC, OCV) cloud is binned onto a uniform SOC grid, forced
    monotonically non-decreasing (OCV rises with SOC), gap-filled, and returned.

    Returns
    -------
    (soc_grid, ocv_grid) : two 1-D arrays of length `n_grid`.
    """
    disc = discharge_metadata(battery_id)
    soc_pts, ocv_pts = [], []
    for _, row in disc.head(n_cycles).iterrows():
        cyc = load_cycle(battery_id, row['filename'])
        t = cyc['Time'].to_numpy()
        i = cyc['Current_measured'].to_numpy()
        v = cyc['Voltage_measured'].to_numpy()
        # SOC referenced to THIS cycle's true capacity, so the OCV table is
        # capacity-referenced and stays valid as the cell ages.
        soc = coulomb_count(t, i, soc_init=1.0, q_ah=float(row['Capacity']))

        active = i < ACTIVE_DISCHARGE_I_A
        if not active.any():
            continue
        # IR-corrected OCV on the active CC phase, away from the low-SOC tail
        keep = active & (soc > soc_lo)
        soc_pts.append(soc[keep])
        ocv_pts.append(v[keep] - i[keep] * r0r1_prior)
        # rested endpoints (no IR correction needed at rest)
        soc_pts.append(np.array([1.0, float(soc[-1])]))
        ocv_pts.append(np.array([float(v[0]), float(v[-1])]))

    soc_all = np.concatenate(soc_pts)
    ocv_all = np.concatenate(ocv_pts)

    soc_grid = np.linspace(0.0, 1.0, n_grid)
    # bin-average onto the grid
    idx = np.clip(np.digitize(soc_all, soc_grid) - 1, 0, n_grid - 1)
    ocv_grid = np.full(n_grid, np.nan)
    for b in range(n_grid):
        sel = idx == b
        if sel.any():
            ocv_grid[b] = ocv_all[sel].mean()
    # fill empty bins by interpolating over the filled ones
    filled = ~np.isnan(ocv_grid)
    ocv_grid = np.interp(soc_grid, soc_grid[filled], ocv_grid[filled])
    # enforce monotonic non-decreasing OCV vs SOC
    ocv_grid = np.maximum.accumulate(ocv_grid)
    return soc_grid, ocv_grid


# ---------------------------------------------------------------------------
# 1RC parameter identification (single cycle) + identifiability diagnostics
# ---------------------------------------------------------------------------

def fit_1rc_cycle(t, i, v, ocv_fn, q_ah: float = Q_NOMINAL_AH,
                  current_threshold: float = 0.05,
                  tau_bounds=(5.0, 120.0), min_relax_s: float = 120.0):
    """Joint least-squares fit of (R0, R1, tau, V_inf) for one discharge cycle.

    Uses the active CC-discharge phase plus the post-discharge relaxation tail,
    with the shared sign convention ``V_t = OCV(SOC) + I*R0 + V_RC1`` (I < 0 in
    discharge). Unlike the heavier pipeline version, `tau` is bounded tightly
    (default 5-120 s) and the **1-sigma parameter standard errors** (sqrt of the
    `curve_fit` covariance diagonal) are returned so callers can quantify how
    well each parameter is actually identified by this data.

    Returns a dict with R0, R1, C1, tau, V_inf, the per-parameter std_errs,
    `settled` (did the relaxation tail flatten out), and `n_active`, or None if
    the cycle can't be fit.
    """
    from scipy.optimize import curve_fit

    t = np.asarray(t, dtype=float)
    i = np.asarray(i, dtype=float)
    v = np.asarray(v, dtype=float)

    active_idx = np.where(np.abs(i) > current_threshold)[0]
    if len(active_idx) < 5:
        return None
    end = active_idx[-1]

    t_act = t[:end + 1]
    i_act = i[:end + 1]
    v_act = v[:end + 1]
    soc_act = coulomb_count(t_act, i_act, soc_init=1.0, q_ah=q_ah)
    ocv_act = ocv_fn(soc_act)
    t_act_rel = t_act - t_act[0]
    n_act = len(t_act)

    relax = slice(end + 1, len(t))
    t_rel = t[relax] - t[relax][0] if (len(t) - end - 1) >= 5 else np.array([])
    v_rel = v[relax] if len(t_rel) else np.array([])
    n_rel = len(t_rel)

    settled = False
    if n_rel >= 3 and t_rel[-1] >= min_relax_s:
        last = t_rel >= (t_rel[-1] - 60.0)
        if last.sum() >= 2:
            dvdt = (v_rel[last][-1] - v_rel[last][0]) / max(t_rel[last][-1] - t_rel[last][0], 1e-3)
            settled = abs(dvdt) * 1000.0 < 2.0

    def model(_x, R0, R1, tau, V_inf):
        out = np.empty(n_act + n_rel)
        out[:n_act] = ocv_act + i_act * R0 + i_act * R1 * (1.0 - np.exp(-t_act_rel / tau))
        if n_rel:
            v_rc_disc = i_act[-1] * R1 * (1.0 - np.exp(-t_act_rel[-1] / tau))
            out[n_act:] = V_inf + v_rc_disc * np.exp(-t_rel / tau)
        return out

    y = np.concatenate([v_act, v_rel]) if n_rel else v_act
    v_inf0 = float(v_rel[-1]) if n_rel else float(v_act[-1])
    try:
        popt, pcov = curve_fit(
            model, np.zeros(n_act + n_rel), y,
            p0=[0.10, 0.05, np.mean(tau_bounds), v_inf0],
            bounds=([1e-3, 1e-3, tau_bounds[0], 2.0],
                    [0.5, 0.5, tau_bounds[1], 4.5]),
            maxfev=8000,
        )
    except Exception:
        return None

    R0_h, R1_h, tau_h, V_inf_h = (float(x) for x in popt)
    std = np.sqrt(np.clip(np.diag(pcov), 0.0, np.inf))
    return {
        'R0': R0_h, 'R1': R1_h, 'C1': tau_h / R1_h, 'tau': tau_h, 'V_inf': V_inf_h,
        'std_errs': {'R0': float(std[0]), 'R1': float(std[1]),
                     'tau': float(std[2]), 'V_inf': float(std[3])},
        'settled': bool(settled), 'n_active': int(n_act),
    }


# ---------------------------------------------------------------------------
# 1RC ECM + UKF
# ---------------------------------------------------------------------------

class BatteryUKF:
    """Unscented Kalman Filter for SOC + V_RC1 in a 1RC equivalent-circuit model.

    State:        x = [SOC, V_RC1]
    Input:        u = current (A; negative during discharge in this dataset)
    Measurement:  V_terminal = OCV(SOC) + u·R0 + V_RC1

    Parameters (R0, R1, C1) are *fixed* for Stage 2. Stage 3 will replace them
    with values predicted by an LSTM.
    """

    def __init__(self, ocv_fn, R0: float, R1: float, C1: float,
                 q_ah: float = Q_NOMINAL_AH,
                 alpha: float = 1e-3, beta: float = 2.0, kappa: float = 0.0):
        self.ocv = ocv_fn
        self.R0, self.R1, self.C1 = R0, R1, C1
        self.q_ah = q_ah
        self.alpha, self.beta, self.kappa = alpha, beta, kappa

    # -------- ECM equations --------
    def _propagate(self, x, u, dt):
        """One step of the process model."""
        soc, vrc = x
        a = np.exp(-dt / (self.R1 * self.C1))
        return np.array([
            soc + u * dt / (self.q_ah * 3600.0),     # Coulomb counting
            vrc * a + u * self.R1 * (1.0 - a),       # 1st-order RC dynamics
        ])

    def _measure(self, x, u):
        """Output equation."""
        return self.ocv(x[0]) + u * self.R0 + x[1]

    # -------- main loop --------
    def run(self, time_s, current_a, voltage_v, x0, P0, Q, R):
        """Run the filter over a full cycle.

        Returns
        -------
        states : (T, 2) array — [SOC, V_RC1] at every timestep
        covs   : (T, 2, 2) array — posterior covariances
        """
        t = np.asarray(time_s, dtype=float)
        i = np.asarray(current_a, dtype=float)
        v = np.asarray(voltage_v, dtype=float)
        n = len(x0)
        lam = self.alpha ** 2 * (n + self.kappa) - n
        Wm = np.full(2 * n + 1, 0.5 / (n + lam))
        Wc = Wm.copy()
        Wm[0] = lam / (n + lam)
        Wc[0] = lam / (n + lam) + (1.0 - self.alpha ** 2 + self.beta)

        x = np.array(x0, dtype=float)
        P = np.array(P0, dtype=float)
        T = len(t)
        states = np.zeros((T, n))
        covs = np.zeros((T, n, n))
        states[0] = x
        covs[0] = P

        for k in range(1, T):
            dt = t[k] - t[k - 1]
            u = i[k - 1]                          # ZOH current over this step

            # sigma points
            try:
                S = np.linalg.cholesky((n + lam) * P)
            except np.linalg.LinAlgError:
                P = P + 1e-9 * np.eye(n)
                S = np.linalg.cholesky((n + lam) * P)
            sigmas = np.zeros((2 * n + 1, n))
            sigmas[0] = x
            for j in range(n):
                sigmas[1 + j]     = x + S[:, j]
                sigmas[1 + n + j] = x - S[:, j]

            # predict
            sigmas_x = np.array([self._propagate(s, u, dt) for s in sigmas])
            x_pred = (Wm[:, None] * sigmas_x).sum(0)
            diff = sigmas_x - x_pred
            P_pred = (Wc[:, None, None] *
                      (diff[:, :, None] @ diff[:, None, :])).sum(0) + Q

            # update with the measurement at step k
            sigmas_z = np.array([self._measure(s, i[k]) for s in sigmas_x])
            z_pred = (Wm * sigmas_z).sum()
            dz = sigmas_z - z_pred
            Pzz = (Wc * dz ** 2).sum() + R
            Pxz = (Wc[:, None] * diff * dz[:, None]).sum(0)
            K = Pxz / Pzz
            x = x_pred + K * (v[k] - z_pred)
            P = P_pred - np.outer(K, K) * Pzz

            states[k] = x
            covs[k] = P

        return states, covs
