# Team-Imperium — Battery SOC/SOH Pipeline

NASA Li-ion battery state-of-charge / state-of-health estimation on the
30-battery room-temperature subset, cycled to end-of-life.

Pipeline: Coulomb counting → UKF on a 1RC ECM → SOH-aware OCV → per-cycle
parameter ID + LSTM-predicted ECM params → 4-estimator comparison.

## Layout

```
.
├── utils.py                   # BatteryUKF, loaders, metrics, plot helpers
├── ground_truth.ipynb         # builds discharge_ground_truth.csv per battery
├── pipeline.ipynb             # main notebook — runs the whole comparison
├── organize_room_temp.py      # built room_temp_batteries/ from the raw NASA dump
└── room_temp_batteries/       # data (B0005 = train, B0006 = validation)
```

## Setup

```bash
source cat_venv/bin/activate    # Python 3.13 venv
```

## Run

`discharge_ground_truth.csv` is committed under each battery dir, so the
ground-truth notebook only needs to run when those files are missing.

```bash
jupyter notebook pipeline.ipynb
```

Then execute cells top-to-bottom. The notebook trains on B0005 and validates
on B0006, producing the final 4-estimator RMSE table on a fresh and an aged
cycle.
