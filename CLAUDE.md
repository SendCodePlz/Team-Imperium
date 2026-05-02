# CLAUDE.md

## Project
Battery SOC/SOH estimation for NASA Li-ion dataset (30 batteries, cycled to EOL).
6-stage pipeline: Coulomb Counting → UKF → LSTM-enhanced UKF → SOH → RUL → Fault Detection.

## Stage Status
- Stage 0–3: Complete (notebooks exist)
- Stage 4–6: Planned

## Key Files
- `utils.py` — BatteryUKF class, loaders, metrics, plot helpers
- `stage0_soc_soh.ipynb` — ground truth extraction → discharge_ground_truth.csv
- `stage1_2_ukf_baseline.ipynb` — Coulomb counting + UKF, Scenarios A–D
- `stage3_lstm_ecm.ipynb` — LSTM predicts V_RC1 init per discharge window
- `room_temp_batteries/` — raw data (B0005 = train, B0006 = validation)
- `cat_venv/` — Python 3.13 venv (`source cat_venv/bin/activate`)

## Architecture
ECM: `V_terminal = OCV(SOC) + I·R0 + V_RC1`, states = [SOC, V_RC1]
Stage 2: fixed R0, R1, C1 from cycle 1. Stage 3: LSTM predicts V_RC1 init.
Fault reference: Scenario D = SOC offset (0.85 vs 1.0) + current bias (+10mA)

## Constraints
- Don't change BatteryUKF.run() signature — all notebooks depend on it
- Match plot style from stage1_2_ukf_baseline (black truth line, alpha=0.85)
- Cross-battery: always train B0005, validate B0006