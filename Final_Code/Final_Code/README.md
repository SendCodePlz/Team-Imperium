# Team Imperium — BMS Fault-Detection Demo

Battery Management System prototype for the **Caterpillar Tech Challenge**.

It streams a real battery dataset frame-by-frame, runs a 10-detector fault
algorithm + a Kalman-filter SOC/SOH estimator, drives protective contactors
(MOSFETs), LEDs and a buzzer, and scores its own accuracy against the dataset's
ground-truth labels — live, on a terminal dashboard and a GUI plot window.

The same fault algorithm runs in **three** interchangeable places, so you can
develop and demo without always needing the board:

| Mode | Where the algorithm runs | Use it for |
|------|--------------------------|------------|
| **Dry run** | Nowhere — replays the dataset's answer key | Quick check that the UI/streaming works |
| **Sim** | `detector_sim.py` (Python port of the firmware) | Validating & tuning the real algorithm on a PC |
| **ESP32** | `BMS_ESP32_Demo/` C++ firmware on the board | The final hardware demo |
| **ESP32 + WiFi** | Board firmware + integrated UDP logger | Hardware demo that also captures the board's WiFi log to `wifi_log.csv` |

---

## Table of contents

1. [Quick start](#quick-start)
2. [Architecture](#architecture)
3. [The data pipeline](#the-data-pipeline)
4. [The 10 fault detectors](#the-10-fault-detectors)
5. [**The severity system (how the ESP decides what to do)**](#the-severity-system)
6. [Action tiers & contactors](#action-tiers--contactors)
7. [SOC / SOH estimation (UKF)](#soc--soh-estimation-ukf)
8. [The three run modes in detail](#the-three-run-modes-in-detail)
9. [Sensors & hardware](#sensors--hardware)
10. [File layout](#file-layout)
11. [Tuning workflow](#tuning-workflow)

---

## Quick start

```bash
pip install rich pyserial numpy pandas openpyxl matplotlib

# Easiest: just run it and pick a mode from the menu (1-4)
python monitor.py

# Or be explicit:
python monitor.py --dry-run        # replay labels (UI check)
python monitor.py --sim            # run the real algorithm on the data
python monitor.py --port COM3      # talk to the ESP32 (Windows; macOS: /dev/tty.usbserial-0001)
python monitor.py --port COM3 --wifi   # ESP32 + built-in WiFi UDP logger → wifi_log.csv
```

`--sim --no-gui --speed 1000` gives a fast, headless algorithm score with a
per-fault-type and per-detector breakdown printed at the end.

**Keyboard:** keys work in **both the plot window and the terminal** — `0-8` inject a
fault (meaningful in `--sim`/`--port`, since `--dry-run` is label-driven), `q` quits.

---

## Architecture

```
        ┌─────────────────────────── PC (Python) ───────────────────────────┐
        │                                                                    │
 dataset.xlsx ──► monitor.py ──► encode_packet() ──► serial ──►  ESP32
        │            │  │                                          │
        │            │  ├──► State (shared object)                 │ (firmware)
        │            │  │      ├──► dashboard.py  (terminal)       │
        │            │  │      └──► gui_plotter.py (plots)         │
        │            │  │                                          ▼
        │            │  └──► detector_sim.py  (sim mode only)   detectors.h
        │            │                                          + ukf_soh.h
        │            └──► csv_logger.py  ◄────── WiFi UDP ◄──── wifi_log.h
        └────────────────────────────────────────────────────────────────────┘
```

- **`monitor.py`** is the orchestrator: load dataset → for each row, update the
  shared `State`, run the chosen responder (sim/dry-run) or send a serial packet
  to the ESP32, update the dashboard + GUI, log to CSV.
- **The ESP32 firmware** is three FreeRTOS tasks:
  - `taskRX` (core 0) — reads serial, `parseFrame()` → pushes a `Frame` onto a queue.
  - `taskProcess` (core 1) — pops a frame → UKF step → `runAllDetectors()` →
    `logValidation()`.
  - `taskWiFiLog` (core 0) — drains a queue, sends each event as a UDP datagram
    to `csv_logger.py`.

---

## The data pipeline

Each dataset row becomes one **packet** (`encode_packet()` in `monitor.py`,
parsed by `parseFrame()` in the firmware):

```
$, seq, v1, v2, v3, T_surf, T_rise, current,
   acoustic_rms, acoustic_kHz, pressure, impact_force,
   dVdt, dTdt, dPdt, SOC_pct, ground_truth, ts_ms
```

The firmware fills a `struct Frame` (see `types.h`) with these values. `dVdt`,
`dTdt`, `dPdt` are rate-of-change signals; when the source file ships them they're
used directly, otherwise `load_dataset` computes them from consecutive samples.

---

## SIL playback — accepting the evaluation data format

The Caterpillar evaluation is a **Software-in-the-Loop (SIL) playback** test: a
time-series file is streamed into the BMS digitally (serial), and the firmware's
fault outputs are captured. Our system already matches their model — `monitor.py`
is the data player, the ESP32 firmware reads frames over serial (no ADC path), and
it logs `[FAULT_SET]`/`[MOSFET]`/`[TIER]`/`[VALIDATE]` for capture.

`load_dataset()` accepts **both** our development `.xlsx` and the evaluation CSV
format, with no firmware changes:

| Evaluation file feature | Handling in `load_dataset()` |
|--------------------------|------------------------------|
| `# key=value` metadata header | parsed (`scenario_id`, `initial_soc`, `ambient_temp_c`, `fault_type`, `fault_start_time_s`) |
| 4–6 cell voltages (`cell1_v…cellN_v`) | collapsed to **min / mean / max** — preserves exact spread for any cell count; mean feeds the UKF |
| multiple temperatures (`temp1_c…tempN_c`) | `T_surf` = hottest cell; `T_rise = T_surf − ambient_temp_c` |
| raw signals only (no `dVdt`/`dTdt`/`dPdt`) | computed on the fly from consecutive samples |
| no per-row label | ground truth derived from `fault_start_time_s` |
| physical units, any sample rate (e.g. 100 Hz) | rate auto-detected from the timestamps |

Verified by running a file in the evaluation's exact format (6 cells, 6 temps,
100 Hz, metadata header, raw signals) through the pipeline unchanged.

---

## The 10 fault detectors

Each detector reads the current `Frame` (+ persistent inter-frame state `DS`),
decides if its fault is active, and sets/clears one bit in the 16-bit
`g_faults` word. Bit positions are the `FaultID` enum in `types.h` — and they
**must** match `FAULT_NAMES` in `bms_state.py` and the IDs in `detector_sim.py`.

| # | Name | Fires when | Key thresholds (`config.h`) |
|---|------|-----------|------------------------------|
| 0 | `VOLT_IMBAL` | cell spread `vmax-vmin` > 50 mV | `THR_IMBAL_V` |
| 1 | `THERMAL_HOT` | `T_surf > 38°C` **or** `T_rise > 12°C`, held 8 frames | `THR_T_SURF_HOT`, `THR_T_RISE_FAULT`, `THR_PERSIST_N` |
| 2 | `SENSOR_LOSS` | frame timeout, seq gap, **or** dVdt spike with no P/T change | `THR_DV_DT_CRITICAL` |
| 3 | `VOLT_GRADIENT` | `|dVdt| > 0.28 V/s` | `THR_DV_DT_HIGH` |
| 4 | `TEMP_GRADIENT` | `|dTdt| > 1.2°C/s`, held 8 frames | `THR_DT_DT`, `THR_PERSIST_N` |
| 5 | `TEMP_LOW` | `T_surf < 18°C`, held 8 frames | `THR_T_COLD` |
| 6 | `GAS_PRESSURE` | gas (`P > 105 kPa`) **or** impact (`P>115` / acoustic / force) | `THR_PRES_*`, `THR_ACOUSTIC_RMS`, `THR_IMPACT_FORCE` |
| 7 | `EIS_FAULT` | Welford z-score of dVdt > 4σ **or** acoustic+T_rise micro-short | `THR_EIS_ZSCORE`, `EIS_WARMUP_N` |
| 8 | `SOC_LOW` | `SOC < 15%` (advisory — excluded from scoring) | `THR_SOC_WARN_PCT` |
| 9 | `CELL_BALANCE` | cell spread > 50 mV (sets balance flags; advisory) | `THR_IMBAL_V` |

> **Gas detector (physics fix):** gas generation is a *slow sustained* pressure rise,
> not a fast transient — so it trips on `pressure > 105 kPa` **alone**. The old
> `&& dPdt > 0.8 kPa/s` AND contradicted the slow-ramp physics (dPdt stays ~0) and
> suppressed most of the gas fault; it has been removed (`THR_DP_DT` kept for reference).

Two algorithms worth knowing:

- **Persistence filter** (detectors 1, 4, 5): a counter increments on a hit and
  decrements on a miss; the fault only latches at `THR_PERSIST_N = 8` consecutive
  hits. This kills single-sample noise spikes — the dominant false-positive source.
- **Welford online z-score** (detector 7): keeps a running mean & variance of
  `dVdt` over the whole session with no buffer. After a 30-frame warm-up, a
  reading more than 4σ from the mean flags an impedance anomaly.

---

## The severity system

This is the heart of the ESP's decision-making. Detectors only answer *"is my
fault present?"* — they do **not** decide what the hardware does. That is the
job of the **severity → tier → contactor** chain, run once per frame in
`runAllDetectors()` after all detectors have voted:

```
runAllDetectors(frame)
   ├─ detect_*  (set/clear the 10 fault bits)
   ├─ updateSeverity(frame)   ──► per-fault severity, system severity, action tier
   ├─ updateContactors()      ──► drive the MOSFETs (+ latch)
   ├─ updateLEDs()
   └─ tickBuzzer()
```

### Step 1 — per-fault severity (`computeFaultSeverity`)

Every **active** fault gets a severity score in `0.0–1.0` from a linear ramp
between its trigger threshold and the worst value physically expected:

```
severity = clamp( (signal − threshold) / (max_expected − threshold),  0, 1 )
```

The `max_expected` ceilings are calibrated from the dataset's fault peaks. Worked
examples:

| Fault | Formula | Example |
|-------|---------|---------|
| `THERMAL_HOT` | `(T_surf − 38) / (65 − 38)` | 45°C → `0.26` |
| `GAS_PRESSURE` | `(P − 105) / (140 − 105)` | 119.8 kPa → `0.42`; 139.9 → `≈1.0` |
| `VOLT_GRADIENT` | `(|dVdt| − 0.28) / (1.0 − 0.28)` | 0.53 V/s → `0.35` |
| `TEMP_GRADIENT` | `(|dTdt| − 1.2) / (5.0 − 1.2)` | 3.0°C/s → `0.47` |

**Severity ceilings are how priority is enforced.** Some faults get a *fixed* or
*capped* score so they can never escalate the hardware response beyond what the
fault warrants:

| Fault | Severity | Effect — it can never go past… |
|-------|----------|--------------------------------|
| `CELL_BALANCE` | fixed `0.05` | NORMAL (housekeeping only) |
| `VOLT_IMBAL` | capped `0.29` | NORMAL (log + LED only) |
| `TEMP_LOW` | fixed `0.35` | INHIBIT_CHG (cold → block charging only) |
| `SOC_LOW` | capped `0.54` | INHIBIT_CHG (low SOC never opens *discharge*) |
| `SENSOR_LOSS` | fixed `0.65` | OPEN_ALL (blind = unsafe → disconnect) |

So an imbalance alone lights an LED; losing a sensor always opens both
contactors. The detector doesn't "know" this — the severity ceiling encodes it.

### Step 2 — system severity & action tier (`updateSeverity`)

```
system_severity = max( severity[i]  for every active fault i )
action_tier     = severityToTier(system_severity)
```

The **single worst fault wins** — the system responds to its most dangerous
active condition. The continuous severity is then bucketed into one of four
hardware tiers:

```
        0.00            0.30            0.55            0.80            1.00
         │   NORMAL      │  INHIBIT_CHG  │   OPEN_ALL    │  LATCH_OPEN   │
         └───────────────┴───────────────┴───────────────┴───────────────┘
            log + LED       open charge      open both      open both +
                            contactor        contactors     latch (manual reset)
```

(Cut-points are `SEV_INHIBIT_LO` / `SEV_OPEN_ALL_LO` / `SEV_LATCH_LO` in `config.h`.)

### Step 3 — drive the contactors (`updateContactors`)

The tier maps to MOSFET states:

| Tier | Charge MOSFET | Discharge MOSFET |
|------|---------------|------------------|
| NORMAL | CLOSED | CLOSED |
| INHIBIT_CHG | **OPEN** | CLOSED |
| OPEN_ALL | **OPEN** | **OPEN** |
| LATCH_OPEN | **OPEN** | **OPEN** + latched |

**Latch behaviour:** once severity reaches `LATCH_OPEN` (≥ 0.80), `DS.latched`
is set and both contactors stay open *for the rest of the run*, regardless of
later readings — it models a fault serious enough to require a manual reset.
Transitions are logged as `[TIER]` and `[MOSFET]` lines on serial.

The Python `detector_sim.py` reproduces all three steps exactly, so sim-mode
contactor/tier behaviour matches the firmware.

---

## Action tiers & contactors

The same severity also drives the indicator outputs (`updateLEDs`, `tickBuzzer`):

- **Green status LED** — 1 Hz heartbeat when healthy, 4 Hz when any fault active.
- **Yellow warn LED** — solid at INHIBIT_CHG.
- **Red fault LED** — blinks at OPEN_ALL, solid when latched.
- **Buzzer** — non-blocking state machine; tone/pattern scales with the fault
  level (WARNING / HIGH / CRITICAL), confirmation chirp on clear.

---

## SOC / SOH estimation (UKF)

An **Unscented Kalman Filter** estimates State of Charge and State of Health
from pack-average voltage + current. The Python (`soc_ukf.py`) and C++
(`BMS_ESP32_Demo/src/ukf_soh/ukf_soh.h`) implementations mirror each other.

The OCV–SOC curve was fitted to this dataset's terminal voltage at ~−2.9 A load;
a generic NMC curve gave ~13% SOC error, the fitted one ~3%. See
`ocv_comparison.png`. The dashboard shows dataset SOC, UKF SOC, and UKF SOH
side by side.

---

## The three run modes in detail

### `--dry-run` — replay the answer key
`DryRunSim` reads the dataset's `fault_flag` / `fault_type` columns and sets
fault bits directly from those labels. It does **not** run any detector logic,
so it always scores ~100% precision/recall. Use it only to confirm the
streaming, dashboard and GUI work — **the accuracy numbers are meaningless.**

### `--sim` — run the real algorithm on the PC
`DetectorSimRunner` feeds the raw sensor signals through `detector_sim.py` (the
faithful port of `detectors.h`) and scores the result honestly against
ground truth. At the end it prints **per-fault-type recall** and **per-detector
tp/fp** so you can see exactly what's caught, missed, or false-firing.

Scoring excludes the **advisory** faults `SOC_LOW`, `VOLT_IMBAL`, `CELL_BALANCE`
(`is_detected()` / `SCORING_MASK` in `bms_state.py`) — they still display, log and
drive contactors, but counting them as "detections" is wrong (low SOC and imbalance
are normal operating conditions, not labeled fault events).

> Faithful caveat: `SENSOR_LOSS`'s timeout/sequence-gap branches can't fire in
> a clean replay (frames are always fresh and sequential), so only its
> noise-pattern branch is active in sim — the same as a healthy serial link.

### `--port` — the ESP32
Streams packets over serial to the board, which runs the C++ firmware and
reports `[FAULT_SET]` / `[MOSFET]` / `[VALIDATE]` lines back.

### `--port --wifi` — the ESP32 + integrated WiFi logger
Same as `--port`, but `monitor.py` also stands up the UDP receiver in-process (a
background thread running `csv_logger.run_logger`), so you don't need a second
terminal. It binds the UDP port **before** the serial handshake, so it's already
listening when the board boots and joins WiFi. The board sends UDP datagrams to
`LOG_SERVER_IP:LOG_SERVER_PORT` (set in `config.h`) and they land in `wifi_log.csv`.

Setup: in `config.h` set `WIFI_SSID` / `WIFI_PASS`, and `LOG_SERVER_IP` to **this
PC's LAN IP** (`ipconfig` / `ip a`); `LOG_SERVER_PORT` must equal `--wifi-port`
(default 4210). The standalone `python csv_logger.py` still works as an alternative.

---

## Sensors & hardware

Only these signals actually drive the detectors. `dVdt`, `dTdt`, `dPdt` are
*derived* on-device by differencing — no dedicated sensor needed.

| Sensor | Feeds detectors | Suggested part |
|--------|-----------------|----------------|
| 3× cell voltage taps (ADC) | imbalance, volt-gradient, EIS, balance | divider / AFE per cell |
| Current sensor | SOH/SOC (Coulomb counting) | INA226 (shunt) or ACS712 (Hall) |
| Cell-surface temperature + ambient ref | thermal-hot, temp-gradient, low-temp | NTC thermistor or DS18B20 |
| Pressure sensor (in enclosure) | gas build-up, impact | BMP280 / MPX5100 |
| Acoustic / vibration | EIS micro-short, impact | piezo disc or MEMS accelerometer |
| Impact / force | impact | high-g accel (derive) or piezo force |

Dataset columns `strain_microstrain`, `acoustic_kurtosis`,
`acoustic_event_count`, `acoustic_peak_frequency_kHz` are **not used** by any
detector — skip them unless you add detectors that need them.

### Demo board build (ESP32 breadboard)

Our actual demo rig (pack: **3S2P → 3 series cell-groups**, monitored as 3 cells).
All MOSFETs are low-side IRLZ44N drivers, so a gate HIGH both performs the action
and lights the LED wired on that MOSFET's drain. All pins live in `config.h`.

| GPIO | Function | Driven by |
|------|----------|-----------|
| 14 | Q1 **main cutoff** + RED LED | `updateContactors()` — ON at OPEN_ALL / latched |
| 27 / 26 / 25 | Q2/Q3/Q4 **cell-balance** + YELLOW LEDs | per-cell `DS.balance_cell[0..2]` |
| 33 | GREEN system-OK LED | `updateLEDs()` — solid ON while NORMAL |
| 32 | **Buzzer** (active, via BC547) | `tickBuzzer()` — digital ON/OFF patterns |
| 21 / 22 | OLED SDA / SCL (SSD1306 128×64 @ 0x3C) | `renderOLED()` |
| 18 / 19 / 5 | Buttons: inject-critical / inject-balance / reset | `pollButtons()` (INPUT_PULLUP) |

- **Active buzzer**, not a passive piezo — driven with `digitalWrite` through the
  BC547, *not* LEDC PWM tones.
- **Arduino libraries to install** (Library Manager): *Adafruit SSD1306* and
  *Adafruit GFX Library*. ESP32 board package as usual.
- **Two demo modes:** with `monitor.py` streaming, the real detectors drive the
  outputs (SIL). With no laptop, the **buttons inject faults** so the LEDs/buzzer/OLED
  react live — button 18 = critical (main cut-off + red + alarm), 19 = balance
  (yellow LEDs), 5 = reset/clear. The OLED always shows SOC/SOH, tier, active fault,
  cutoff state, and balance flags.
- The detection/severity/UKF logic is **unchanged** from the SIL build — only the
  hardware-output layer was repinned, so evaluation results are unaffected.

---

## File layout

```
monitor.py        Orchestrator: dataset, streaming, modes, menu, CSV logging
bms_state.py      Shared State dataclass + fault catalogue (must match types.h)
dashboard.py      Rich terminal dashboard
gui_plotter.py    matplotlib live GUI (dark, monospace, rounded panels)
detector_sim.py   Python port of detectors.h  ← --sim engine
soc_ukf.py        Python SOC/SOH UKF  (mirror of src/ukf_soh/ukf_soh.h)
csv_logger.py     UDP WiFi log receiver (ESP32 → PC CSV)

BMS_ESP32_Demo/
  BMS_ESP32_Demo.ino   FreeRTOS tasks: RX, Process, WiFiLog
  config.h             ALL thresholds, pins, WiFi (one source of truth)
  detectors.h          The 10 detectors + severity + hardware control
  types.h              Frame / DetectorState structs, FaultID enum
  wifi_log.h           UDP payload builder
  src/ukf_soh/         On-device UKF
```

> **Sync rule:** the fault thresholds exist in both `config.h` (firmware) and
> `detector_sim.py` (PC sim). If you change one, change the other — otherwise
> the sim stops being a valid mirror. Same for the `FaultID` enum order across
> `types.h`, `bms_state.py`, and `detector_sim.py`.

---

## Tuning workflow

1. Run `python monitor.py --sim --no-gui --speed 1000`.
2. Read the per-fault-type recall + per-detector tp/fp report.
3. Adjust a threshold in **both** `config.h` and `detector_sim.py`.
4. Re-run sim — iterate until precision/recall are acceptable.
5. Flash the firmware and confirm on hardware with `--port`.

> **Don't overfit.** Tune from *physics* with sensible margins, not from this
> dataset's exact min/max — thresholds shaved to clean synthetic data won't
> generalize to a real PCB (real signals are noisier). Only two changes were made
> here, both principled: the **gas physics fix** (slow ramp → pressure-only trigger)
> and **excluding advisory faults** from scoring. Everything else stays at its
> physical default.

### Current sim score: **Prec 99.8% / Recall 49%** (FP=6)

| Fault type | Recall | Note |
|------------|--------|------|
| Gas generation / pressure build-up | 82% | pressure-only trigger |
| Voltage sensor noise / loose lead | 74% | dVdt spikes |
| Mechanical impact / obstacle hit | 70% | force / acoustic |
| Micro-short / abnormal self-heating | 21% | weak per-frame signature |
| Impedance growth / contact degradation | **~0%** | **no per-frame signature** |

**Impedance growth is structurally undetectable per-frame in this dataset** — its
rows look like normal operation except for low SOC, and the *same* low-SOC values
also occur in normal discharge, so no instantaneous threshold can separate them.
It was previously only "caught" by `SOC_LOW` coinciding with low SOC — the same
coincidence that produced ~3750 false positives. The honest answer is to exclude
`SOC_LOW` from scoring (done) and accept the lower recall, **not** to hack thresholds.

### Top follow-up: an SOH/aging detector
The correct way to catch impedance growth is the **UKF's `soh_est` trend** (rising
internal resistance → falling SOH), not a per-frame signal. This is the biggest
remaining recall lever and deserves its own validation pass. Other follow-ups:
make thresholds a single source of truth (parse `config.h` into the sim) to stop
drift; harden `_between()`; revisit `push_history()` decimation.