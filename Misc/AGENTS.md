# AGENTS.md

## Project
Battery SOC/SOH estimation for NASA Li-ion dataset (30 batteries, cycled to EOL).
Pipeline: Coulomb Counting → UKF → SOH-aware OCV → LSTM-predicted ECM params → robust UKF.

## Key Files
- `utils.py` — BatteryUKF class, loaders, metrics, plot helpers
- `ground_truth.ipynb` — one-off: produces `discharge_ground_truth.csv` per battery
- `pipeline.ipynb` — main notebook. Segment 1 (SOH-aware OCV), Segment 2 (per-cycle R0/R1/C1 + LSTM), final 4-estimator comparison (CC vs UKF-base vs UKF-Seg1 vs UKF-Seg2)
- `organize_room_temp.py` — one-off: built `room_temp_batteries/` from raw NASA dump
- `room_temp_batteries/` — data (B0005 = train, B0006 = validation)
- `cat_venv/` — Python 3.13 venv (`source cat_venv/bin/activate`)

## Run order
1. `ground_truth.ipynb` — only if `discharge_ground_truth.csv` files are missing (already committed under each battery dir)
2. `pipeline.ipynb` — top-to-bottom

## Architecture
ECM: `V_terminal = OCV(SOC) + I·R0 + V_RC1`, states = [SOC, V_RC1]
Baseline UKF: fixed R0, R1, C1 from EIS metadata; OCV polynomial from cycle-1 of B0005.
Pipeline upgrades: SOH-indexed OCV table; per-cycle (R0, R1, C1) joint LS fit; LSTM predicts (V_RC1_init, R0, R1) from a short window.
Fault reference: Scenario D = SOC offset (0.85 vs 1.0) + current bias (+10mA).

## Constraints
- Don't change `BatteryUKF.run()` signature — `pipeline.ipynb` depends on it
- Plot style: black truth line, alpha=0.85 on overlaid estimators
- Cross-battery: always train B0005, validate B0006
