# Caterpillar BMS Demo — Context Handoff

Team Imperium | Caterpillar Tech Challenge

> Docs map: **`PRESENTATION.md`** = read-once presenter's guide (narrative, results
> story, demo script, Q&A). **`README.md`** = full technical reference (architecture,
> every detector, severity system, sensors). **`fault_detection.md`** = physics-first
> detection + dual-mode RUL design spec (the detector authority — read before touching
> any detector). **This file** = concise dev handoff.

> **Detection redesign (2026-06-25):** taxonomy expanded to **14 faults** (added
> `CELL_OV_UV`, `OVERCURRENT`, `RUNAWAY_WARN`, `CONTACT_RES` — physical guards, inert on
> this dataset). Micro-short = current **AND** acoustic hybrid. **Impedance growth is no
> longer a per-frame fault** — it's multi-cycle aging, deferred to RUL power-fade.
> Mirrored in `detector_sim.py` + firmware. Scored sim: **Prec 100% / Recall 56%**.
> RUL (dual-mode) designed but not yet built. Firmware reflash required.

---

## Project Structure

```
monitor.py          main orchestration: dataset, serial, stream loop, modes, CSV logging
bms_state.py        shared State dataclass + fault catalogue (must match types.h)
dashboard.py        Rich terminal dashboard
gui_plotter.py      matplotlib live GUI (dark, monospace, rounded panels)
detector_sim.py     Python port of detectors.h  ← --sim engine
soc_ukf.py          Python SOC/SOH UKF — mirrors BMS_ESP32_Demo/src/ukf_soh/ukf_soh.h
csv_logger.py       UDP WiFi log receiver (run_logger() — imported by monitor.py for --wifi)
PRESENTATION.md     presenter's guide (read this to explain the project)
README.md           full detailed project documentation
requirements.txt    pip deps (rich pyserial numpy pandas openpyxl matplotlib)
.gitignore          ignores caches + run outputs (session.csv, wifi_log.csv, _* )
BMS_ESP32_Demo/     ESP32 Arduino firmware
  config.h          all thresholds (one source of truth)
  detectors.h       10 fault detectors + severity/tier/contactor logic (~800 lines)
  types.h           Frame / DetectorState structs, FaultID enum
  src/ukf_soh/      C++ UKF
```

All Python modules share one `State` object created in `monitor.py`.

---

## How To Run

Just run `python monitor.py` with no args → an **interactive menu** picks the mode
(1 Dry run / 2 Dry run + Sim / 3 ESP32 / 4 ESP32 + WiFi). Flags still work and skip the menu:

```bash
python monitor.py --dry-run            # replay dataset labels (UI check)
python monitor.py --sim                # run the REAL algorithm on the data
python monitor.py --sim --no-gui --speed 1000   # fast headless algo validation
python monitor.py --port COM3          # ESP32 over serial (Win) / /dev/tty.usbserial-0001 (mac)
python monitor.py --port COM3 --wifi   # ESP32 + built-in WiFi UDP logger → wifi_log.csv
python csv_logger.py                   # standalone WiFi logger (alternative to --wifi)
```

The menu is skipped automatically when stdin is piped (CI) → defaults to dry-run.

**Keyboard works in BOTH the plot window and the terminal:** `0-8` inject a fault
(meaningful in `--sim`/`--port`, not label-driven `--dry-run`), `q` quits.

---

## Run Modes Explained

| Flag | What drives faults | Purpose |
|---|---|---|
| `--dry-run` | Dataset ground-truth labels (`fault_flag`/`fault_type`) | UI/streaming smoke test — TP=100% is **fake** (replays answer key) |
| `--sim` | **`detector_sim.py`** — Python port of `detectors.h` | Validate/tune the actual detector logic; prints per-fault-type recall + per-detector tp/fp |
| `--port` | Real ESP32 C++ firmware (serial) | Hardware demo |
| `--port --wifi` | ESP32 firmware + integrated UDP logger | Hardware demo + auto-capture WiFi log to `wifi_log.csv` (no 2nd terminal) |

`--sim` is the honest scoreboard. Current run: **Prec 99.8% / Recall 49%** (FP=6).
Per-type recall: gas 82%, sensor-noise 74%, impact 70%, micro-short 21%, **impedance ~0%**.
- Advisory faults (`SOC_LOW`/`VOLT_IMBAL`/`CELL_BALANCE`) are **excluded from scoring**
  (`is_detected()` / `SCORING_MASK` in `bms_state.py`; mirrored in firmware `logValidation`).
- **Impedance growth is structurally undetectable per-frame** (no instantaneous signature;
  it was only ever "caught" by SOC_LOW coinciding with low SOC). Catching it needs the
  SOH/aging detector — see follow-ups. **Don't chase its recall by overfitting thresholds.**

---

## SIL playback (evaluation format)

`load_dataset()` in `monitor.py` accepts **both** our `.xlsx` and the Caterpillar
SIL CSV: parses the `# key=value` metadata header (scenario / initial_soc /
ambient_temp_c / fault_start_time_s), handles 4–6 cells (→ min/mean/max, preserves
spread), multiple temps (→ hottest), computes dVdt/dTdt/dPdt from raw samples when
absent, and derives ground truth from `fault_start_time_s`. The `.xlsx` path is
byte-identical to before (validated numbers unchanged). Firmware needs no change —
it already reads frames over serial (no ADC path) and logs fault outputs. Details
in `README.md` → "SIL playback" and `PRESENTATION.md` §8b.

---

## Demo board firmware (ESP32 breadboard)

Pack is **3S2P → 3 series cell-groups** (matches the 3-cell logic). All pins in
`config.h`. Low-side IRLZ44N drivers — gate HIGH = action + its LED on.

| GPIO | Function |
|---|---|
| 14 | Q1 main cutoff + RED LED (ON at OPEN_ALL/latched) |
| 27/26/25 | Q2/Q3/Q4 cell-balance + YELLOW LEDs (`DS.balance_cell[0..2]`) |
| 33 | GREEN system-OK LED (solid while NORMAL) |
| 32 | Active buzzer via BC547 — `digitalWrite` patterns, **not** LEDC PWM |
| 21/22 | OLED SDA/SCL (SSD1306 128×64 @ 0x3C) |
| 18/19/5 | Buttons: inject-critical / inject-balance / reset (INPUT_PULLUP) |

Only the **hardware-output layer** changed (`config.h` pins; `detectors.h`
`updateContactors`/`updateLEDs`/`tickBuzzer`; `.ino` adds OLED `renderOLED()`,
`pollButtons()`, new `setup`/`loop`). Detection/severity/UKF untouched → SIL
results unchanged. Needs Arduino libs **Adafruit SSD1306 + Adafruit GFX**.
Buttons give a no-laptop demo; `monitor.py` streaming = live SIL detection.

---

## `detector_sim.py` (BUILT)

Faithful Python port of all 10 `detectors.h` detectors + severity/tier/contactor
logic. Mirrors the `DetectorState` struct: persistence counters (`hot_cnt`,
`tgrad_cnt`, `cold_cnt`), Welford EIS z-score, Coulomb counter, latch.
**Thresholds are duplicated from `config.h` — keep both in sync.**
`monitor.py`'s `DetectorSimRunner` adapts it to `State` + scoring.

Faithful caveat: SENSOR_LOSS timeout/seq-gap branches can't fire in clean replay,
so only its noise-pattern branch is active in sim.

---

## Severity Chain (how the ESP decides hardware response)

Detectors only set/clear fault bits — they do NOT control hardware. After all
detectors vote, `runAllDetectors()` runs: `updateSeverity()` → `updateContactors()`.

1. **Per-fault severity** (`computeFaultSeverity`): each active fault →
   `clamp((signal − threshold) / (max_expected − threshold), 0, 1)`.
   Priority is enforced by **ceilings**: `CELL_BALANCE`=0.05, `VOLT_IMBAL`≤0.29
   (→ NORMAL), `TEMP_LOW`=0.35, `SOC_LOW`≤0.54 (→ INHIBIT_CHG), `SENSOR_LOSS`=0.65
   (→ OPEN_ALL). So e.g. low SOC can never open the *discharge* path.
2. **System severity** = `max(active severities)` — worst fault wins.
3. **Tier** (`severityToTier`): `<0.30` NORMAL · `0.30–0.55` INHIBIT_CHG (open CHG) ·
   `0.55–0.80` OPEN_ALL (open both) · `≥0.80` LATCH_OPEN (open both + manual reset).
4. **Latch**: once LATCH_OPEN is hit, `DS.latched` stays set for the rest of the run.

`detector_sim.py` reproduces all four steps. Full worked examples in `README.md`.

---

## Key Thresholds (`config.h`)

```c
THR_IMBAL_V         = 0.050f   // voltage imbalance (V)
THR_T_SURF_HOT      = 38.0f    // thermal hotspot surface (°C)
THR_T_RISE_FAULT    = 12.0f    // thermal rise fault (°C) — raised from 5.0 to cut FP
THR_T_COLD          = 18.0f    // low temp (°C)
THR_DT_DT           = 1.2f     // temp gradient (°C/s)
THR_PERSIST_N       = 8        // persistence counter latch
THR_DV_DT_HIGH      = 0.28f    // voltage gradient fault (V/s)
THR_DV_DT_CRITICAL  = 0.60f    // sensor loss noise pattern (V/s)
THR_ACOUSTIC_RMS    = 0.060f   // acoustic fault (g)
THR_IMPACT_FORCE    = 10.0f    // impact force (N)
THR_PRES_HIGH_KPA   = 105.0f   // gas pressure fault (kPa) — gas trips on this ALONE now
THR_PRES_IMPACT_KPA = 115.0f   // gas pressure immediate (kPa)
THR_DP_DT           = 0.8f     // pressure gradient — NO LONGER used by gas (kept for ref)
THR_EIS_ZSCORE      = 4.0f     // EIS Welford z-score threshold
EIS_WARMUP_N        = 30       // EIS warmup frames
THR_SOC_WARN_PCT    = 15.0f    // SOC-low warning (%)
CELL_CAPACITY_AH    = 2.9f

SEV_INHIBIT_LO      = 0.30f    // action tiers
SEV_OPEN_ALL_LO     = 0.55f
SEV_LATCH_LO        = 0.80f
```

**Gas physics fix:** `detect_GasPressure` now trips on `pressure > 105` ALONE
(removed the `&& dPdt > 0.8` AND — gas is a slow ramp, dPdt stays ~0, so the AND
suppressed most of the fault). Gas recall 39% → 82%. All other thresholds are left
at physical defaults — **deliberately NOT shaved to this dataset's min/max** (no overfitting).

---

## SOC/SOH UKF

`soc_ukf.py` mirrors `BMS_ESP32_Demo/src/ukf_soh/ukf_soh.h`.

OCV curve was fitted to the dataset at ~`-2.9 A` load (generic NMC curves gave ~13% SOC MAE; fitted curve gives ~3%). See `ocv_comparison.png`.

---

## Known Bugs / Open TODOs

| Location | Issue |
|---|---|
| ~~`dPdt` hardcoded to `0.0`~~ | **FIXED** — now read from `dPdt_kPa_per_s` column through to detectors |
| ~~Python `FAULT_NAMES` missing `GAS_PRESSURE`~~ | **FIXED** — `bms_state.py` matches C++ `types.h` (10 faults) |
| ~~`SOH_LOW` floods FPs / wrong name~~ | **FIXED** — renamed `SOC_LOW`; excluded from scoring (advisory). Prec → 99.8% |
| ~~Gas `&& dPdt>0.8` wrong physics~~ | **FIXED** — gas trips on pressure alone. Recall 39% → 82% |
| ~~Keyboard inject/quit dead~~ | **FIXED** — `apply_anomaly()` wired + GUI key handler |
| **Impedance recall ~0%** | Structurally undetectable per-frame. Needs SOH/aging detector (top follow-up). |

### Top follow-ups (not yet done)
- **SOH/aging-based impedance detector** using the UKF `soh_est` trend — only correct way
  to catch impedance growth; biggest remaining recall lever.
- **Single source of truth for thresholds** — duplicated in `config.h` + `detector_sim.py`.
- Minor: harden `_between()`; `push_history()` decimation drops samples on cap.

---

## Useful Commands

```bash
# Syntax check (no __pycache__)
python -c "import ast, pathlib; [ast.parse(pathlib.Path(p).read_text(encoding='utf-8')) for p in ['monitor.py','bms_state.py','dashboard.py','gui_plotter.py','detector_sim.py','soc_ukf.py','csv_logger.py']]; print('syntax OK')"

# pyflakes lint
pyflakes monitor.py bms_state.py dashboard.py gui_plotter.py detector_sim.py soc_ukf.py csv_logger.py

# Dry-run smoke test (UI only — scores fake 100%)
python monitor.py --dry-run --no-gui --speed 500 --logfile _smoke_session.csv

# Sim smoke test (real algorithm — honest score + per-fault breakdown)
python monitor.py --sim --no-gui --speed 1000 --logfile _sim_session.csv
```

---

## GUI (`gui_plotter.py`)

Dark matplotlib window — matches terminal aesthetic. Features:
- Tabs: SOC | SOH | Severity | Voltages
- `FOLLOW LATEST` pill, time-window slider, V1/V2/V3 chip toggles
- Mouse-hover readout bottom-left
- Auto-scrolling Y-axis (shows range around current value, not fixed 0–100)

Layout uses figure coordinates `(x, y)` in `[0,1]` from bottom-left.
Control panel bottom edge is at `y=0.052`, top at `y=0.192`. Labels use `va="top"` so text flows downward into the panel.

---

## User Preferences

- Practical code, no long theory
- GUI: dark, monospaced, neon/cyber accents, rounded panels — no default matplotlib widgets
- Keep modules separate; don't stuff everything into `monitor.py`
- Short, direct responses preferred
