# BMS Fault Detection — Presentation Guide

**Team Imperium · Caterpillar Tech Challenge**

> Read this once and you can explain the whole project. It goes top-to-bottom in
> the order you'd present: the pitch → the problem → how it works → the results
> (including the hard questions) → the demo → Q&A.

---

## 0. The 10-second and 60-second versions

**One sentence:**
> "We built a battery management system that watches a battery's sensors in real
> time, decides if something dangerous is happening, and automatically disconnects
> the battery before it fails — and we can prove how well it works."

**One paragraph:**
> "Battery packs fail in specific ways — they overheat, build up gas, get
> mechanically damaged, or degrade internally. Our system streams real battery
> sensor data through 10 fault detectors running on an ESP32 microcontroller. Each
> detector watches for one failure signature. When a fault is found, the system
> scores how dangerous it is and takes graded action — from just warning, to
> blocking charging, to fully disconnecting the battery. We also estimate the
> battery's charge and health with a Kalman filter. Crucially, we don't just claim
> it works — we replay a labelled dataset of 36,000 real fault scenarios through
> the exact same algorithm and measure precision and recall honestly."

---

## 1. The problem — why this matters

Lithium battery packs (EVs, heavy equipment, Caterpillar machines) can fail
catastrophically: thermal runaway, fire, venting. A **Battery Management System
(BMS)** is the safety brain. Its job:

1. **Sense** — read cell voltages, temperature, current, pressure, etc.
2. **Detect** — recognise when a reading means a real fault.
3. **Decide** — judge how serious it is.
4. **Act** — disconnect the battery (open "contactors") before damage spreads,
   and alert with LEDs/buzzer.

The hard part is **detect + decide**: real operation is noisy, and you must catch
true faults *without* nuisance trips (a BMS that cries wolf gets disabled by users).

**Our contribution:** a complete, working detect→decide→act pipeline, plus an
honest, measurable evaluation of how good the detection actually is.

---

## 2. The big picture (architecture)

```
   ┌───────────────────────── PC (Python) ─────────────────────────┐
   │                                                                │
 dataset.xlsx ─► monitor.py ─► serial packet ─────────►  ESP32 (firmware)
   │               │                                        │
   │               ├─► shared State ─► terminal dashboard   │ runs 10 detectors,
   │               │               └─► live GUI plot        │ severity, contactors,
   │               │                                        │ LEDs, buzzer
   │               └─► detector_sim.py (PC mirror, sim mode)│
   │                                                        ▼
   │   csv_logger  ◄──────── WiFi UDP ◄──────────── reports results back
   └────────────────────────────────────────────────────────────────┘
```

Two halves:

- **The ESP32 firmware** (`BMS_ESP32_Demo/`, C++) is the *real product* — it would
  sit inside the battery pack reading sensors and driving the disconnect hardware.
- **The PC tool** (`monitor.py`, Python) feeds it data, shows live dashboards, and
  — importantly — contains a **faithful Python copy of the firmware's algorithm**
  so we can test and tune on a laptop without the board.

**Why a Python mirror?** So we can run the *exact same fault logic* against 36,000
labelled rows in seconds and measure how well it performs — something you can't do
quickly on hardware. This is our evaluation engine.

---

## 3. The data

A synthetic but physically-realistic NMC lithium-cell dataset:
`synthetic_NMC__1C_fault_EIS_dataset.xlsx` — **36,001 rows at 10 Hz = 1 hour** of a
battery discharging, with five fault types injected at known times. Each row has
voltage, temperature, current, pressure, acoustic, impact force, plus pre-computed
rates of change (dV/dt, dT/dt, dP/dt) and a **ground-truth label** (fault or not).

| Scenario | Rows | What it is physically |
|----------|------|------------------------|
| **Normal** | 30,926 | Healthy discharge |
| Gas generation / pressure build-up | 2,401 | Electrolyte breaking down → gas → pressure rises |
| Impedance growth / contact degradation | 1,601 | Internal resistance rising (aging / bad contact) |
| Micro-short / abnormal self-heating | 501 | Internal short dissipating heat |
| Voltage sensor noise / loose lead | 451 | Measurement fault (loose wire) |
| Mechanical impact / obstacle hit | 121 | Physical strike on the pack |

The labels let us **score** our detector: every row is a True/False Positive/Negative.

---

## 4. How one frame flows through the system

1. `monitor.py` reads a row, packs it into a comma-separated **packet** and sends it
   over USB serial (or, in sim mode, straight into the Python mirror).
2. The ESP32 parses it into a `Frame` struct.
3. A Kalman filter updates the **SOC/SOH** estimate.
4. **All 10 detectors run** and each sets/clears its fault bit.
5. The **severity system** scores the situation and sets the action tier.
6. **Contactors (MOSFETs), LEDs and buzzer** are driven accordingly.
7. The result is compared to the ground-truth label (**TP/TN/FP/FN**) and sent back
   to the PC over serial and (optionally) WiFi.

On the ESP32 this runs as **three parallel FreeRTOS tasks**: one reads serial, one
processes + detects (on its own CPU core), one streams logs over WiFi — so logging
never slows down detection.

---

## 5. The 10 fault detectors (plain language)

Each detector watches **one** failure signature. They only raise a flag — they do
**not** decide what the hardware does (that's the severity system, next section).

| # | Detector | "It fires when…" (in plain words) |
|---|----------|-----------------------------------|
| 0 | Voltage Imbalance | the three cells drift apart by >50 mV (one cell is weak) |
| 1 | Thermal Hotspot | surface temp >38 °C or rises >12 °C — and stays there |
| 2 | Sensor Loss | voltage jumps wildly with no matching temp/pressure change → a wire is loose |
| 3 | Voltage Gradient | voltage changes too fast (>0.28 V/s) → internal resistance jumped |
| 4 | Temp Gradient | temperature spikes >1.2 °C/s — and stays |
| 5 | Low Temperature | cell below 18 °C (charging cold plates lithium → dangerous) |
| 6 | Gas / Pressure | internal pressure >105 kPa, **or** a mechanical impact (force/acoustic) |
| 7 | EIS / Impedance | voltage behaviour becomes statistically abnormal, **or** acoustic + heat = micro-short |
| 8 | SOC Low | charge drops below 15% (advisory) |
| 9 | Cell Balance | cells need balancing (housekeeping) |

**Two clever bits worth mentioning:**

- **Persistence filter** (detectors 1, 4, 5): a fault must be seen for **8 frames in
  a row** before it latches. A single noisy reading can't trip it. *This is how we
  avoid false alarms.*
- **Welford running statistics** (detector 7): we track the *average and spread* of
  the voltage rate-of-change continuously, using a formula that needs **no stored
  history** (critical on a tiny microcontroller). If a new reading is more than 4
  standard deviations from normal, it's flagged. This catches "abnormal" without
  hard-coding what normal is.

---

## 6. The severity system — the core idea (explain this slowly)

> This is the most interesting part of the project. Detectors answer *"is this fault
> present?"* The severity system answers *"how bad is it, and what do we do?"*

It runs in **three steps** every frame, after all detectors vote:

### Step 1 — score each active fault from 0.0 to 1.0
A linear ramp between the trigger threshold and the worst value physically possible:
```
severity = (signal − threshold) / (worst_expected − threshold)   (clamped 0–1)
```
*Example:* gas fault triggers at 105 kPa and physical damage is ~140 kPa, so
119.8 kPa scores **(119.8−105)/(140−105) ≈ 0.42**.

**The key trick — severity ceilings.** Some faults are *capped* so a minor issue can
never trigger a drastic response:

| Fault | Capped at | So it can never do more than… |
|-------|-----------|-------------------------------|
| Cell balance | 0.05 | just log (housekeeping) |
| Voltage imbalance | 0.29 | just warn (LED) |
| Low temperature | 0.35 | block charging only |
| Low SOC | 0.54 | block charging only (never cut discharge) |
| Sensor loss | 0.65 | open both contactors (blind = unsafe) |

> Talking point: "Priority is built into the *number*, not into a pile of if-statements.
> Losing a sensor always disconnects; a low battery never cuts your discharge path."

### Step 2 — the worst fault wins
```
system_severity = the maximum severity across all active faults
```
The system responds to its single most dangerous condition.

### Step 3 — map severity to a hardware action tier
```
   0.00 ──── 0.30 ──────── 0.55 ──────── 0.80 ──── 1.00
    │ NORMAL  │ INHIBIT_CHG │  OPEN_ALL   │ LATCH_OPEN │
    │ log+LED │ open charge │ open both   │ open both  │
    │         │ contactor   │ contactors  │ + lock until manual reset │
```

**Latch:** once severity hits 0.80, the battery stays disconnected for good until a
human resets it — for faults too serious to auto-recover from.

The same severity also drives the **LEDs** (green heartbeat → yellow warn → red
fault) and the **buzzer** (louder/faster patterns for worse faults).

---

## 7. SOC & SOH estimation (the Kalman filter)

Alongside fault detection, we estimate two things the operator cares about:

- **SOC (State of Charge)** — how full the battery is (the "fuel gauge").
- **SOH (State of Health)** — how degraded it is vs. new.

We use an **Unscented Kalman Filter (UKF)** — a standard technique to estimate
hidden quantities from noisy measurements (here: estimate true SOC/SOH from messy
voltage + current). It runs both on the ESP32 (C++) and on the PC (Python mirror).

**The one number to remember:** a generic battery voltage-curve gave ~13% SOC error
on this dataset; we **re-fitted the curve to this cell's actual data** and cut error
to **~3%**. (Shown in `ocv_comparison.png`.)

---

## 8. The three+one run modes (and why dry-run vs sim matters)

| Mode | What runs the detection | Use it to… |
|------|-------------------------|-----------|
| **Dry run** | nothing — replays the answer key | demo the UI quickly |
| **Sim** | the real algorithm (Python mirror) | **measure how good detection is** |
| **ESP32** | the real firmware on the board | the hardware demo |
| **ESP32 + WiFi** | firmware + built-in log capture | hardware demo that auto-saves the WiFi log |

> **Critical talking point — don't get caught out here.** Dry-run shows
> "100% accuracy", but that is **fake** — it just replays the dataset's labels, it
> doesn't run our detectors. The *honest* score comes from **sim mode**, which runs
> the actual algorithm. We built sim mode specifically so we'd never fool ourselves
> with the fake number.

---

## 8b. Evaluation compliance (SIL playback testing)

The official test is **Software-in-the-Loop (SIL) playback**: the judges feed a
time-series CSV into our BMS digitally (over serial), instead of real sensors, and
capture our fault outputs. Our architecture already *is* their diagram — `monitor.py`
is the "test harness / data player", the ESP32 is the BMS firmware. We meet all four
requirements:

| Their requirement | How we meet it |
|-------------------|----------------|
| **Data injection mode** (read sensors from a comms interface, not ADCs) | The firmware already reads sensor frames over serial — there is no ADC path. That's the whole design. |
| **Timing preservation** (process each frame at the data rate, e.g. 100 Hz) | The player streams one frame per timestep; the firmware processes each as it arrives. We tested at 100 Hz. |
| **Interface & protocol** (simple per-timestep message) | One comma-separated frame per timestep over UART. |
| **Outputs & logging** (emit fault flags / actions for capture) | The firmware prints `[FAULT_SET]`, `[MOSFET]`, `[TIER]`, `[VALIDATE]` lines and can stream them over WiFi UDP. |

**What we added so their data 'just works':** their files use a different layout than
our development dataset, so we made the loader accept it directly —
- a `# key=value` **metadata header** (scenario, initial SOC, ambient temp, fault
  start time) — we parse it and seed the filter / derive ground truth from it;
- **4–6 individual cell voltages** — we pass through min/mean/max, which preserves the
  exact cell spread the imbalance detector needs, for any number of cells;
- **multiple temperatures** — we use the hottest;
- **only raw signals** (no pre-computed rates) — we **compute dV/dt, dT/dt, dP/dt
  on the fly** from consecutive samples;
- **no per-row label** — we derive ground truth from `fault_start_time_s`.

> **We verified this** by generating a file in their exact format (6 cells, 6 temps,
> 100 Hz, metadata header, raw signals only) and running it through unchanged — it
> loaded correctly and the detectors responded. So when the judges hand us a file,
> our algorithm runs without modification.

---

## 9. Results — the honest scoreboard

Running our real algorithm (sim mode) over all 36,000 rows:

```
Precision 99.8%   Recall 49%   (only 6 false positives in 30,926 normal rows)
```

| Fault type | We catch | Why |
|------------|----------|-----|
| Gas / pressure build-up | **82%** | pressure is a clean, strong signal |
| Sensor noise / loose lead | **74%** | erratic voltage is distinctive |
| Mechanical impact | **70%** | force/acoustic spikes are obvious |
| Micro-short | **21%** | weak signature, overlaps normal |
| Impedance growth | **~0%** | *no instantaneous signature* (see below) |

**How to present this confidently:**

- **Precision 99.8% is excellent** — we almost never false-alarm (6 in ~31,000). For
  a safety system that users must trust, this is the metric that matters most.
- **Recall 49% is honest, not weak** — and here's the insight that makes us look
  *smart*, not incomplete 👇

### The impedance story (turn a weakness into a strength)

Impedance growth is 31% of all fault rows and we catch ~0% of it. **This is not a
bug — it's a real, defensible finding:**

> "Impedance-growth faults have *no per-frame signature*. Those rows look exactly
> like normal operation except the battery is at low charge — and normal discharge
> *also* goes to low charge. The **same sensor values appear in both fault and
> normal rows**, so no instantaneous threshold can ever separate them. We could have
> faked a high recall by flagging 'low charge = fault', but that produced 3,750
> false alarms on perfectly normal rows. We deliberately removed that cheat to keep
> precision honest."

> "To actually catch impedance growth you need a *trend over time*, not a single
> reading — that's our SOH/aging detector, which is our clear next step."

This shows the judges you **understand your data and resist overfitting** — exactly
what an engineer should do.

### Two engineering decisions to highlight

1. **Gas physics fix.** The original gas detector required pressure to be high *and*
   rising fast. But gas builds up *slowly* — the rise rate is nearly zero. Requiring
   a fast rise was wrong physics and suppressed most of the fault. Fixing it took gas
   recall from **39% → 82%**.
2. **Honest scoring.** We excluded "advisory" conditions (low charge, cell imbalance)
   from the fault score. They're normal operating states, not faults — counting them
   was inflating false positives. This took precision from **43% → 99.8%**.

> Meta-point for judges: "We tuned from *physics with safety margins*, not by shaving
> thresholds to fit this one dataset — so it'll generalise to real hardware."

---

## 10. The hardware

- **ESP32 microcontroller** (dual-core) — runs everything in real time.
- **2 MOSFETs** — the charge and discharge "contactors" that disconnect the battery.
- **3 LEDs** (green/yellow/red) + **buzzer** — operator alerts.
- **Sensors the detectors need** (for the PCB):

| Sensor | Used for |
|--------|----------|
| 3× cell voltage taps | imbalance, voltage-gradient, impedance, balancing |
| Current sensor | charge tracking (Coulomb counting) |
| Temperature (+ ambient ref) | hotspot, thermal-gradient, low-temp |
| Pressure sensor | gas build-up, impact |
| Acoustic / vibration | micro-short, impact |
| Force / impact | mechanical impact |

*Rates of change (dV/dt etc.) are computed on-chip — no extra sensors needed.*

---

## 11. What we'd do next (shows maturity)

1. **SOH/aging detector** — catch impedance growth via the Kalman filter's health
   trend (the right way; biggest recall gain). *This is the headline next step.*
2. **Single source of truth for thresholds** — auto-share the config between firmware
   and the Python mirror so they can't drift.
3. **More real-world validation** — test against noisier, real (not synthetic) data.

---

## 12. Live demo script

```bash
# 1) Show the honest evaluation (fast, no hardware)
python monitor.py --sim --no-gui --speed 1000
#    → point at: Precision 99.8%, and the per-fault breakdown

# 2) Show the live dashboard + plots
python monitor.py --sim
#    → in the PLOT WINDOW press 6 (inject gas) and 7 (inject impact)
#    → watch a fault appear, severity rise, contactors open in the dashboard
#    → press q to quit

# 3) (If board present) the real hardware
python monitor.py --port COM3 --wifi
#    → LEDs change, buzzer sounds, wifi_log.csv fills automatically
```

**What to point at during the demo:** the four tabs (SOC / SOH / Severity /
Voltages), the fault event log scrolling, the contactor state (CHG/DSC OPEN/CLOSED)
flipping when you inject a fault, and the accuracy counters at the bottom.

---

## 13. Anticipated Q&A (rehearse these)

**Q: Your recall is only 49% — isn't that bad?**
A: For the catchable faults it's 70–82%. The 49% is dragged down by impedance growth,
which has no instantaneous signature — it's physically impossible to detect per-frame
from this data. We chose honesty over a faked number. Precision is 99.8%, which is the
metric a safety system lives or dies by.

**Q: Why is precision more important than recall here?**
A: A BMS that false-alarms gets distrusted and bypassed by operators, defeating its
purpose. We'd rather miss a subtle, slow-developing fault (which a separate aging
detector handles) than constantly cry wolf on a healthy pack.

**Q: How do you know the algorithm is actually good and you're not just replaying labels?**
A: Two separate modes. Dry-run replays labels (for UI demos) and we never quote its
numbers. Sim mode runs the *real* algorithm and is what we score. They're clearly
separated in the code on purpose.

**Q: Is the Python the same as what runs on the chip?**
A: Yes — `detector_sim.py` is a line-for-line mirror of the C++ `detectors.h`, same
thresholds, same persistence counters, same statistics. We verify identical behaviour.

**Q: Did you overfit to this dataset?**
A: Deliberately not. We made exactly two changes — a physics correction to the gas
detector and excluding non-fault advisory conditions from scoring. Every other
threshold is left at its physically-motivated value with safety margin, so it
generalises to real hardware that's noisier than this clean synthetic data.

**Q: What happens on a truly severe fault?**
A: Severity ≥ 0.80 hits the LATCH tier — both contactors open and stay open until a
human manually resets. It won't silently re-enable a dangerous battery.

**Q: What's the hardest fault and why?**
A: Impedance growth and micro-short — their signatures overlap normal operation on a
single reading. They need time-trend analysis (our next step: the SOH detector).

**Q: Can your BMS run our SIL playback test / read our data file?**
A: Yes. Our firmware already reads sensors digitally over serial (no ADC path), so it
*is* in data-injection mode by design. Our data player parses your metadata header,
handles 4–6 cells and multiple temperatures, computes the rate-of-change signals from
raw inputs if you don't provide them, and derives ground truth from `fault_start_time_s`.
We tested it against a file in your exact format and the algorithm ran unchanged.

**Q: Your dataset has 3 cells but ours has 6 — does that break anything?**
A: No. Our detection works on the cell-voltage *spread* (max − min), so we pass through
min/mean/max of however many cells you provide — the spread is exact regardless of cell
count, and the mean feeds the SOC/SOH filter.

---

## 14. Cheat-sheet (numbers to memorise)

- **36,001 rows, 10 Hz, 1 hour, 5 fault types, 5,075 fault rows.**
- **10 detectors, 4 action tiers** (NORMAL / INHIBIT_CHG / OPEN_ALL / LATCH_OPEN).
- **Severity tiers at 0.30 / 0.55 / 0.80.**
- **Precision 99.8%, Recall 49%, only 6 false positives.**
- **Gas 82%, sensor-noise 74%, impact 70%, micro-short 21%, impedance ~0%.**
- **SOC error 13% → 3%** after re-fitting the voltage curve.
- **Persistence = 8 frames; impedance z-score threshold = 4σ.**
- **Two fixes:** gas physics (39→82%), honest scoring (precision 43→99.8%).

---

*Deeper technical reference: `README.md`. Project/handoff notes: `CLAUDE.md`.*
