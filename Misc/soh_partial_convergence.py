"""
soh_partial_convergence.py
==========================
Single-cycle SOH convergence story for TARGET_CYCLE on B0006.

Three panels:
  1. Voltage trace during discharge — sliding window markers at key widths
  2. dV vs window length — how the measured drop grows as we look further
  3. Linear fit (dV → SOH) trained on B0005 — cycle 60's point highlighted
"""

import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.stats import linregress

_HERE      = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT  = os.path.join(_HERE, 'room_temp_batteries')

Q_NOMINAL_AH       = 2.0
ACTIVE_DISCHARGE_I = -1.0
MAX_WINDOW_S       = 1800
TARGET_CYCLE       = 60
TRAIN_BAT          = 'B0005'
VAL_BAT            = 'B0006'

# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _bat_dir(bat):
    return os.path.join(DATA_ROOT, bat)

def _load_meta(bat):
    return pd.read_csv(os.path.join(_bat_dir(bat), 'metadata.csv'))

def _load_cycle(bat, filename):
    return pd.read_csv(os.path.join(_bat_dir(bat), 'data', filename))

def discharge_meta(bat):
    m = _load_meta(bat)
    d = m[m['type'] == 'discharge'].copy().sort_values('test_id').reset_index(drop=True)
    d['cycle_num'] = np.arange(1, len(d) + 1)
    d['SOH']       = d['Capacity'] / Q_NOMINAL_AH
    return d

def dv_from_cc(cc_df, window_s):
    win = cc_df[cc_df['t_rel'] <= window_s]
    if len(win) < 5:
        return np.nan
    return float(win['Voltage_measured'].iloc[0] - win['Voltage_measured'].iloc[-1])

# ---------------------------------------------------------------------------
# Train linear model on B0005
# ---------------------------------------------------------------------------
print(f'Building features for {TRAIN_BAT}...')
d_train = discharge_meta(TRAIN_BAT)
train_rows = []
for _, row in d_train.iterrows():
    cyc = _load_cycle(TRAIN_BAT, row['filename'])
    cc  = cyc[cyc['Current_measured'] < ACTIVE_DISCHARGE_I].reset_index(drop=True)
    if len(cc) < 5:
        continue
    cc['t_rel'] = cc['Time'] - cc['Time'].iloc[0]
    dv = dv_from_cc(cc, MAX_WINDOW_S)
    if not np.isnan(dv):
        train_rows.append({'dV': dv, 'SOH': row['SOH']})
feat_train = pd.DataFrame(train_rows)

slope, intercept, r, *_ = linregress(feat_train['dV'], feat_train['SOH'])
print(f'  SOH = {intercept:.4f} + {slope:.4f} × dV   (R²={r**2:.3f})')

def predict_soh(dv):
    return intercept + slope * dv

# ---------------------------------------------------------------------------
# Load the target cycle from B0006
# ---------------------------------------------------------------------------
d_val   = discharge_meta(VAL_BAT)
cyc_row = d_val[d_val['cycle_num'] == TARGET_CYCLE]
if cyc_row.empty:
    TARGET_CYCLE = int(d_val['cycle_num'].iloc[len(d_val) // 2])
    cyc_row = d_val[d_val['cycle_num'] == TARGET_CYCLE]
    print(f'  (TARGET_CYCLE adjusted to {TARGET_CYCLE})')

true_soh   = float(cyc_row['SOH'].iloc[0])
filename   = cyc_row['filename'].iloc[0]

cyc_df = _load_cycle(VAL_BAT, filename)
cc_all = cyc_df[cyc_df['Current_measured'] < ACTIVE_DISCHARGE_I].reset_index(drop=True)
cc_all['t_rel'] = cc_all['Time'] - cc_all['Time'].iloc[0]
# Clip to MAX_WINDOW_S so the panels stay tidy
cc_plot = cc_all[cc_all['t_rel'] <= MAX_WINDOW_S]

# dV curve across window lengths
windows = np.arange(10, MAX_WINDOW_S + 1, 5)
dv_curve  = np.array([dv_from_cc(cc_all, w) for w in windows])
soh_curve = predict_soh(dv_curve)

# Key window snapshots to annotate
snap_windows = [60, 120, 300, MAX_WINDOW_S]
snap_colors  = ['#e67e22', '#8e44ad', '#2980b9', '#27ae60']

# ---------------------------------------------------------------------------
# Figure — 3 panels
# ---------------------------------------------------------------------------
fig, axes = plt.subplots(1, 3, figsize=(16, 5))
fig.suptitle(
    f'{VAL_BAT}  Cycle {TARGET_CYCLE}  —  SOH convergence from partial discharge window\n'
    f'True SOH = {true_soh:.3f}   |   Linear model trained on {TRAIN_BAT}',
    fontsize=12, fontweight='bold'
)

# --- Panel 1: Voltage trace + window end markers ---
ax = axes[0]
ax.plot(cc_plot['t_rel'], cc_plot['Voltage_measured'],
        color='steelblue', lw=1.8, label='V(t)')
v0 = float(cc_plot['Voltage_measured'].iloc[0])
for w, col in zip(snap_windows, snap_colors):
    win = cc_all[cc_all['t_rel'] <= w]
    if len(win) < 5:
        continue
    v_end = float(win['Voltage_measured'].iloc[-1])
    dv    = v0 - v_end
    ax.axvline(w, color=col, lw=1.2, ls='--', alpha=0.8)
    ax.annotate(f'{w}s\ndV={dv:.3f}V', xy=(w, v_end),
                xytext=(w + 12, v_end + 0.015),
                fontsize=7.5, color=col,
                arrowprops=dict(arrowstyle='->', color=col, lw=0.8))
ax.set_xlabel('Time into discharge (s)')
ax.set_ylabel('Voltage (V)')
ax.set_title('Discharge voltage trace')
ax.set_xlim(0, MAX_WINDOW_S)
ax.grid(True, alpha=0.3)

# --- Panel 2: dV vs window length ---
ax = axes[1]
ax.plot(windows, dv_curve, color='steelblue', lw=2, label='dV(window)')
for w, col in zip(snap_windows, snap_colors):
    idx = np.searchsorted(windows, w)
    if idx < len(windows):
        ax.scatter(windows[idx], dv_curve[idx], color=col, s=60, zorder=5)
        ax.axvline(w, color=col, lw=1.0, ls='--', alpha=0.6)
ax.set_xlabel('Window length (s)')
ax.set_ylabel('Voltage drop dV (V)')
ax.set_title('dV grows as window expands')
ax.set_xlim(0, MAX_WINDOW_S)
ax.grid(True, alpha=0.3)

# --- Panel 3: Linear fit + cycle 60 point ---
ax = axes[2]
ax.scatter(feat_train['dV'], feat_train['SOH'],
           s=15, alpha=0.4, color='gray', label=f'{TRAIN_BAT} training data')
dv_line = np.linspace(feat_train['dV'].min(), feat_train['dV'].max(), 200)
ax.plot(dv_line, predict_soh(dv_line), 'k-', lw=1.8, label='Linear fit')
ax.axhline(true_soh, color='black', lw=1.0, ls='--', alpha=0.6)

for w, col in zip(snap_windows, snap_colors):
    idx = np.searchsorted(windows, w)
    if idx >= len(windows) or np.isnan(dv_curve[idx]):
        continue
    dv_w   = dv_curve[idx]
    soh_w  = soh_curve[idx]
    ax.scatter(dv_w, soh_w, color=col, s=70, zorder=6,
               label=f'{w}s → SOH={soh_w:.3f}')
    ax.plot([dv_w, dv_w], [ax.get_ylim()[0] if ax.get_ylim()[0] != 0 else 0.6, soh_w],
            color=col, lw=0.8, ls=':', alpha=0.7)

ax.set_xlabel('dV (V)')
ax.set_ylabel('Estimated SOH')
ax.set_title('Linear fit: dV → SOH')
ax.legend(fontsize=7.5, loc='lower right')
ax.grid(True, alpha=0.3)
# Fix ylim after scatter
ax.set_ylim(
    min(feat_train['SOH'].min(), true_soh) - 0.03,
    max(feat_train['SOH'].max(), true_soh) + 0.03,
)

plt.tight_layout()

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
out_path = os.path.join(_HERE, 'Images', 'soh_convergence_single_cycle.png')
fig.savefig(out_path, dpi=150, bbox_inches='tight')
print(f'\nSaved: Images/soh_convergence_single_cycle.png')
plt.show()
