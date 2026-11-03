# Fault Detection & RUL — Design Specification

**Team Imperium | Caterpillar Tech Challenge**

This document defines the BMS fault-detection and Remaining-Useful-Life (RUL) design
on **first principles (battery physics + sensor reality)**, *not* tuned to the
specific synthetic dataset. Each detector is built to survive **sensor noise** and to
make physical sense on a real Caterpillar pack with **dynamic load, varying
temperature, and multi-cycle aging**.

> **Scope honesty.** The evaluation dataset
> (`synthetic_NMC__1C_fault_EIS_dataset.xlsx`) is a **single partial discharge at a
> constant ~−2.9 A and fixed ~25 °C ambient, with no rest or charge phases**. Several
> physically-correct detectors here — and *all* of RUL — therefore **cannot be
> validated on this data**; they are implemented and demonstrated on synthetic
> signals. The validation matrix in §6 marks exactly what the dataset can and cannot
> prove. We deliberately do **not** shave thresholds to this dataset's min/max.

---

## §0 System & sensing model

| Channel | Symbol | Rate | Physical meaning | Measured noise (1σ, normal) |
|---|---|---|---|---|
| Cell-group voltages | `v1,v2,v3` | 10 Hz | 3 series groups (3S2P pack) | ~2–5 mV |
| Surface temperature | `T_surf` | 10 Hz | cell can temperature | ~slow; rides T_rise |
| Temperature rise | `T_rise` | 10 Hz | `T_surf − ambient` | ±2.3 °C |
| Pack current | `I` | 10 Hz | negative = discharge | ±15 mA (Hall) |
| Acoustic RMS | `acoustic_rms` | 10 Hz | ultrasonic emission (g) | ±4 mg |
| Acoustic peak freq | `acoustic_kHz` | 10 Hz | emission spectrum | — |
| Internal pressure | `pressure` | 10 Hz | cell can pressure (kPa) | low |
| Impact force | `impact_force` | 10 Hz | mechanical shock (N) | low |
| Derived gradients | `dVdt,dTdt,dPdt` | 10 Hz | finite-difference rates | **amplified noise** |
| UKF estimates | `SOC, SOH, Vrc` | 10 Hz | Kalman states | filtered |

**Key noise facts that shape the design**
- **Ambient is recoverable** as `ambient = T_surf − T_rise` → enables temperature
  compensation even though this dataset's ambient is fixed.
- **Derived channels (`dVdt` etc.) are the noisiest** — differentiation amplifies
  high-frequency sensor noise. Detectors that lean on them must use statistics
  (z-score) or persistence, never a bare instantaneous threshold.
- **Current sensor drifts** with temperature (Hall zero-point). A 50 mV / 50 mA
  absolute offset is within drift; any detector keyed on a small *absolute* current
  delta is fragile and must be corroborated by a second, independent channel.

---

## §1 Noise-handling principles (applied to every detector)

These six rules are the backbone; each fault in §3 cites which it uses.

1. **Filter to the fault's physical timescale.** Slow faults (impedance, gas, thermal)
   get an EMA / median filter; impulsive faults (impact) are read raw or with a
   1–2 sample window. EMA: `x_f += α(x − x_f)`, with `α ≈ 0.1` (~1 s @10 Hz) for slow
   signals.
2. **Debounce with persistence counters.** A fault latches only after `N` consecutive
   qualifying frames and clears after the count decays — a single noise spike can
   never trip it. (Already used for thermal/temp-gradient; we extend it to all
   non-impulsive faults.)
3. **Adaptive baselines, not magic numbers.** Where the absolute level drifts or is
   noisy, track a running mean/variance (Welford) and trigger on a **z-score**, so the
   detector self-calibrates to the unit and conditions.
4. **Condition normalization.** Divide out the operating point. The canonical example:
   voltage sag is `I·R`, so we detect on **resistance `R = sag/I`**, not on raw sag —
   a load spike then cannot masquerade as a fault.
5. **Multi-signal corroboration.** For faults a single drifting sensor could fake,
   require **two independent channels** to agree (logical AND). Example: a soft short
   must show *both* excess current draw *and* acoustic emission.
6. **Severity hysteresis.** Separate set/clear thresholds (or the persistence
   up/down counter) prevent chattering around a boundary.

---

## §2 State-estimation backbone

The detectors and RUL share three estimated quantities.

**UKF (`soc_ukf.py` ↔ `BMS_ESP32_Demo/src/ukf_soh/ukf_soh.h`).** A 3-state joint
filter `x = [SOC, Vrc, SOH]` over a 1-RC Thévenin model
`V = OCV(SOC) + (I − I_NOM)·R0 + Vrc`. The OCV curve was fitted to the pack at
`I_NOM = −2.9 A`, so `OCV(SOC)` already includes the steady IR drop at nominal load.

**Internal-resistance estimator (new).** The physically-meaningful impedance signal is
the *excess* series resistance beyond the healthy model:

```
R_excess = ( OCV(SOC) + (I_filt − I_NOM)·R0 − V_filt ) / |I_filt|        [Ω]
```

- Uses **UKF `SOC`** (real deployment estimates SOC; it is not a measured input).
- `V_filt`, `I_filt` are EMA-filtered (rule 1) so noise doesn't inflate `R_excess`.
- Normalizing by `|I_filt|` (rule 4) makes it **load-independent**: at any current, a
  healthy cell reads `R_excess ≈ 0`; a degraded/contact-fault cell reads a positive Ω.

**Slow baselines.** `R_baseline` and `SOH_baseline` are captured as long-EMA / first-
N-frame references at begin-of-life, so detection and RUL react to *deviation/trend*,
not absolute value (handles unit-to-unit spread and slow drift).

**Deferred (documented) robustness** — not in the first implementation, flagged in
code as TODO:
- **Temperature compensation** `R_baseline = f(ambient, SOC)`: internal resistance
  rises sharply in the cold, so the impedance threshold must scale with
  `ambient = T_surf − T_rise`. Cannot be validated on fixed-ambient data.
- **UKF-convergence gating**: suppress impedance/RUL outputs until the filter
  covariance has converged, so a transient SOC-estimate error can't be read as a
  resistance fault.

---

## §3 Fault catalogue

Format per fault: **Mechanism → Signature → Detector (noise-robust) → Thresholds &
rationale → Severity → Validatable on this dataset?**
New modes are marked **(NEW)**. Faults map to the existing severity tier machine (§5).

### A. Per-cell over/under-voltage **(NEW)**
- **Mechanism.** A cell driven outside its safe window (over-charge >4.2 V, deep
  discharge <2.5 V) — irreversible damage / lithium plating / venting risk.
- **Signature.** Any `v_i` (filtered) outside `[V_MIN, V_MAX]`.
- **Detector.** EMA-filter each cell voltage; compare to window; short persistence
  (≈3 frames) to reject noise; per-cell so a single weak group is caught.
- **Thresholds.** `V_MAX = 4.20 V`, `V_MIN = 2.50 V` (NMC datasheet limits) — physical,
  not dataset-derived.
- **Severity.** Over-voltage → OPEN_ALL (stop charge); deep under-voltage → INHIBIT/OPEN.
- **Validatable here?** Partial — dataset V spans 2.8–4.06 V (never violates), so this
  guards real operation without firing on the demo.

### B. Cell imbalance / weak cell
- **Mechanism.** Capacity/SOC divergence between series groups → one group hits a
  limit first; chronic imbalance signals a weak cell.
- **Signature.** Growing `spread = max(v) − min(v)`.
- **Detector.** Spread of **filtered** cell voltages vs threshold + persistence
  (not raw spread, which is noisy at mV scale).
- **Thresholds.** `THR_IMBAL_V = 0.050 V` (physically meaningful pack mismatch).
- **Severity.** Advisory→balancing; large persistent spread escalates.
- **Validatable here?** Yes.

### C. Overcurrent / abnormal load **(NEW)**
- **Mechanism.** Current beyond the cell's rated continuous or peak limit → overheating,
  accelerated aging, safety. Directly answers the "current isn't constant" reality.
- **Signature.** `|I_filt|` above continuous rating (sustained) or peak rating (brief).
- **Detector.** Two-level: `|I_filt| > I_CONT` sustained `t_cont`, OR `|I| > I_PEAK`
  for any frame. Filtered for the continuous test; raw for the instantaneous peak.
- **Thresholds.** Per cell datasheet (e.g. `I_CONT` ≈ 1–2 C, `I_PEAK` ≈ 3–5 C of
  2.9 Ah) — set from spec, **not** the dataset's flat 2.9 A.
- **Severity.** Peak → OPEN_ALL; sustained over-continuous → INHIBIT + thermal watch.
- **Validatable here?** Partial — dataset current is ~constant 1C, so this never trips
  here but is essential for a real machine (and it is what makes the micro-short
  detector safe to deploy — see I).

### D. Thermal hotspot / over-temperature
- **Mechanism.** Exothermic side reactions / poor cooling → cell over-temp.
- **Signature.** High absolute `T_surf`, or `T_rise` beyond what the load explains.
- **Detector.** Absolute `T_surf` threshold + persistence (primary). `T_rise` used only
  with a **high** threshold, because normal 1C discharge self-heats and `T_rise`
  overlaps normal — keying on a low `T_rise` is the classic false-positive trap.
- **Thresholds.** `THR_T_SURF_HOT = 38 °C`; `THR_T_RISE_FAULT = 12 °C` (raised from 5 to
  stop firing on normal self-heating).
- **Severity.** Scales `(T_surf−38)/(65−38)` → OPEN_ALL/LATCH near runaway.
- **Validatable here?** Yes.

### E. Thermal-runaway precursor **(strengthened)**
- **Mechanism.** Onset of runaway: temperature accelerates *and* gas is generated *and*
  voltage drops — a multi-signal fingerprint, not any one channel.
- **Signature.** Sustained `dTdt` **AND** (rising `pressure`/`dPdt` **OR** abnormal
  voltage drop).
- **Detector.** `dTdt` persistence counter corroborated (rule 5) by pressure or
  voltage-collapse — avoids the noisy `dTdt` channel firing alone.
- **Thresholds.** `THR_DT_DT = 1.2 °C/s` (persisted) + pressure rise / V-drop gate.
- **Severity.** LATCH_OPEN (highest) — irreversible safety event.
- **Validatable here?** Partial (channels exist; no true runaway event labeled).

### F. Low-temperature operation
- **Mechanism.** Cold → high impedance, lithium-plating risk on charge.
- **Signature.** `T_surf` below a cold limit.
- **Detector.** Absolute `T_surf < THR_T_COLD` + persistence.
- **Thresholds.** `THR_T_COLD = 18 °C` (charge-derate point; physical).
- **Severity.** TEMP_LOW → inhibit/derate charge.
- **Validatable here?** Yes (no cold frames → no FP, by design).

### G. Gas generation / pressure build-up
- **Mechanism.** Electrolyte decomposition / separator failure → slow sustained
  pressure rise; vent precursor.
- **Signature.** **Absolute** pressure rises slowly (`dPdt ≈ 0` — it's a ramp).
- **Detector.** Absolute pressure threshold. **No `dPdt` AND-gate** — gas is a slow
  ramp so `dPdt` stays ~0; requiring it suppresses the fault (a real physics bug we
  already fixed: recall 39%→82%).
- **Thresholds.** `THR_PRES_HIGH_KPA = 105`, immediate vent `THR_PRES_IMPACT_KPA = 115`.
- **Severity.** OPEN_ALL → LATCH approaching vent.
- **Validatable here?** Yes.

### H. Mechanical impact / shock
- **Mechanism.** Collision / obstacle strike → cell deformation, internal short risk.
- **Signature.** Impulsive `impact_force` and/or acoustic spike — **transient**.
- **Detector.** Raw threshold, **no filtering / no persistence** (filtering would erase
  a 1-frame impulse — the one case where rule 1/2 are deliberately *not* applied).
- **Thresholds.** `THR_IMPACT_FORCE = 10 N`, `THR_PRES_IMPACT_KPA = 115`,
  `THR_ACOUSTIC_RMS = 0.060 g`.
- **Severity.** OPEN_ALL immediately.
- **Validatable here?** Yes.

### I. Micro-short / soft internal short
- **Mechanism.** Dendrite / contamination bridge → parasitic internal discharge path;
  self-heating and slow self-discharge.
- **Signature (physical, two regimes).**
  - *Under load:* small **extra** discharge draw **plus** acoustic emission (dendrite
    cracking / micro-bubbles).
  - *At rest / charge:* the cell self-discharges faster than neighbors → divergent
    `dV/dQ` and reduced coulombic efficiency over time.
- **Detector.**
  - **Demo/dataset heuristic (constant-current):** `|I_filt| > THR_MICRO_I_FILT`
    **AND** `acoustic_rms > THR_MICRO_ACOUSTIC`. The **AND (rule 5)** is the key:
    a drifting current sensor alone cannot trip it, because it won't also produce
    acoustic emission. Flagged in code as dataset-specific.
  - **Production path (documented stub, inert here):** during detected rest/charge
    phases (`|I| ≈ 0` or charging), track **differential voltage `dV/dQ`** peak shift
    and **per-cell coulombic efficiency**; a soft short shows as a steadily growing
    self-discharge relative to siblings over hours/days. Never fires on this
    constant-current discharge (no rest/charge), so it is safe to ship alongside the
    heuristic.
- **Thresholds.** `THR_MICRO_I_FILT = 2.95 A`, `THR_MICRO_ACOUSTIC ≈ 0.025 g`. **These
  are demo-calibration, explicitly not physical** — on a real machine the absolute
  current means nothing (see overcurrent C); the production path is the physical one.
- **Severity.** HIGH → INHIBIT/OPEN depending on self-heating corroboration.
- **Validatable here?** Heuristic: yes. Production dV/dQ path: **no** (needs rest/charge
  + multi-cycle data).

### J. Impedance growth / contact degradation — **NOT a per-frame fault**
- **Mechanism.** SEI growth, contact corrosion, partial disconnection → rising series
  resistance → power fade (voltage sags more under load).
- **Signature.** **Internal resistance rises** — but there is *no instantaneous
  per-frame signature*; `dVdt` is identical to normal. It is a slow, **multi-cycle
  aging trend**.
- **Why it's not flagged per-frame.** The only per-frame observable is the §2
  `R_excess` = (OCV(SOC) − V)/I. With the realistic **UKF SOC estimate** (the BMS does
  not measure SOC), the ~3–5 % SOC-estimate error produces an `R_excess` bias of the
  same order (~0.015 Ω) as the fault itself — so a per-frame threshold either floods
  false positives or, with a baseline that adapts fast enough to cancel the drift,
  absorbs the fault. Within **one constant-current cycle** it cannot be separated
  cleanly. This is a physical limitation, not a tuning gap.
- **Where it lives instead.** Surfaced through **RUL power-fade (§4)**: track `R_excess`
  (and SOH) as a slow trend **across cycles** and extrapolate to end-of-life. The §2
  `R_excess()` estimator is retained for RUL and for **contact-resistance (K)**, where
  the *per-group divergence* cancels the common-mode SOC bias and is detectable.
- **Severity.** Reported via RUL, not as a contactor-driving fault flag.
- **Validatable here?** No — needs multi-cycle / cycle-life data.

### K. Connection / contact resistance — loose lead (electrical) **(NEW)**
- **Mechanism.** Loose bus-bar / corroded terminal on *one* group → localized high
  series resistance, intermittent step changes in that tap's voltage.
- **Signature.** **Per-cell** `R_excess` divergence (one group's resistance ≫ siblings)
  and/or step offsets in one `v_i` uncorrelated with load.
- **Detector.** Compute `R_excess` per group; flag when one group exceeds the median of
  the others by a margin + persistence. Distinct from sensor noise (L) because it is a
  *DC resistance* offset, not high-frequency noise.
- **Thresholds.** Per-group resistance divergence margin (multiple of `R_baseline`).
- **Severity.** HIGH — connection faults precede thermal events at the joint.
- **Validatable here?** Partial — dataset has one fused voltage trace per group; the
  per-group split is real on hardware.

### L. Voltage-sensor noise / loose lead (sensor-side)
- **Mechanism.** Loose sense wire / ADC fault → erratic voltage *reading* with no real
  cell change.
- **Signature.** High-frequency voltage noise (`|dVdt|` large) **with** temperature and
  pressure quiet — real cell events move T/P too; a pure sensor glitch does not.
- **Detector.** `|dVdt| > THR_DV_DT_CRITICAL` **AND** `|dTdt| < ε` **AND** `|dPdt| < ε`
  (rule 5: corroboration by *absence* of physical co-signals).
- **Thresholds.** `THR_DV_DT_CRITICAL = 0.60 V/s`, quiet bands ε ≈ 0.5.
- **Severity.** SENSOR_LOSS tier → OPEN_ALL (don't trust the pack on bad data).
- **Validatable here?** Yes.

### M. Sensor loss / dropout
- **Mechanism.** Comms loss, frame stall, stuck ADC.
- **Signature.** Frame **timeout**, **sequence-gap**, or a **stuck** value.
- **Detector.** Firmware-side timeout (`millis()` since last frame), `seq` gap check,
  and stuck-value detection. *Timeout/seq-gap branches only fire on hardware* — clean
  replay always delivers fresh sequential frames.
- **Thresholds.** Timeout ≈ a few frame periods; seq must increment.
- **Severity.** SENSOR_LOSS → OPEN_ALL.
- **Validatable here?** Partial — only the stuck-value/noise branch is exercisable in
  replay.

### Advisory — SOC low
- Streamed/UKF SOC under warn (15%) / cutoff (5%); independent **coulomb-counter
  cross-check** flags current-sensor/SOC divergence. Advisory (excluded from scoring),
  inhibits charge near cutoff.

### Advisory — Cell balance
- During rest, divergent group SOC → enable per-group passive balancing. Advisory.

---

## §4 RUL — Remaining Useful Life (dual-mode)

RUL is a **slow, multi-cycle** quantity. We track two independent degradation modes and
report the **limiting** one.

**Mode 1 — Capacity fade.**
- Input: UKF **SOH** (capacity health fraction).
- Method: recursive least-squares (RLS) fit of `SOH` vs **charge throughput**
  (Ah processed, convertible to equivalent-full-cycles `EFC = ΣAh / Q_nom`).
- Project the fitted line to **EOL = 80% SOH**. `RUL_cap = (SOH − 0.80) / |slope|`.

**Mode 2 — Power fade.**
- Input: §2 **`R_excess`** (or absolute `R = R0 + R_excess`).
- Method: RLS fit of `R` vs throughput; project to **EOL = 2 × R0** (doubling of series
  resistance ≈ end of usable power).
- `RUL_pow = (2·R0 − R) / |slope|`.

**Combine & report.**
- `RUL = min(RUL_cap, RUL_pow)`; also report which mode is limiting.
- Units: **equivalent-full-cycles** and **operating hours** (cycles × avg cycle time).
- **Confidence band** from the RLS residual variance (so a noisy/short history widens
  the interval rather than lying with a point estimate).

**Noise / robustness.**
- Update only when the **UKF has converged** and current is flowing (SOH/R observable).
- **Long-window regression** averages out per-frame noise; SOH and R are slow, so RUL
  updates slowly and never twitches on a single noisy sample.
- Clamp/sanity-bound slopes (degradation is monotonic-ish; reject positive-health
  slopes as noise).

**Validatable here? No.** One partial cycle shows negligible SOH/R change, so RUL is
demonstrated on a **synthetic multi-cycle degradation trace** (injected SOH and R
decline); we assert the projected RUL tracks the injected slope within the confidence
band. Field accuracy requires cycle-life data and is stated as such.

---

## §5 Severity → action mapping

Detectors only **set/clear fault bits**; they never touch hardware directly. After all
detectors vote each frame: `computeFaultSeverity → updateSeverity → updateContactors`.

- **Per-fault severity** = `clamp((signal − threshold)/(max_expected − threshold),0,1)`,
  with **priority ceilings** so e.g. low SOC can never open the discharge path.
- **System severity** = max of active severities.
- **Tiers:** `<0.30` NORMAL · `0.30–0.55` INHIBIT_CHG · `0.55–0.80` OPEN_ALL ·
  `≥0.80` LATCH_OPEN (manual reset).
- **Hysteresis** via the persistence up/down counters (rule 6).

New faults' tiers: over-voltage/impact/runaway → OPEN_ALL or LATCH; impedance/contact/
micro-short → HIGH advisory escalating with magnitude; overcurrent → INHIBIT (sustained)
or OPEN (peak).

---

## §6 Validation matrix — what this dataset proves vs reality

| Fault / feature | Observable on this dataset | Needs richer data |
|---|---|---|
| Cell imbalance (B) | ✅ | — |
| Thermal hotspot (D) | ✅ | — |
| Low-temp (F) | ✅ (no FP) | cold-soak data to prove TP |
| Gas/pressure (G) | ✅ | — |
| Mechanical impact (H) | ✅ | — |
| Micro-short heuristic (I) | ✅ | — |
| Micro-short dV/dQ + CE (I) | ❌ | rest/charge + multi-cycle |
| Impedance growth (J) | ❌ (moved to RUL) | multi-cycle aging data |
| Sensor noise (L) | ✅ | — |
| Sensor loss timeout/seq (M) | ❌ (stuck only) | live hardware stream |
| Over/under-voltage (A) | ⚠️ guard-only (never violated) | abuse data |
| Overcurrent (C) | ⚠️ guard-only (flat 1C) | dynamic-load data |
| Runaway precursor (E) | ⚠️ partial | true runaway event |
| Contact resistance (K) | ⚠️ partial | per-group taps on HW |
| RUL (capacity + power) | ❌ | cycle-life / multi-cycle data |

✅ proven on the demo · ⚠️ implemented & inert/guarding (won't false-fire) · ❌ requires
data this dataset doesn't contain (demonstrated on synthetic signals).

---

## §7 Implementation note

All detectors live in the firmware `BMS_ESP32_Demo/detectors.h` (the authority) and are
mirrored line-for-line in `detector_sim.py` for PC validation; thresholds live once in
`config.h` and are duplicated (kept in sync) in `detector_sim.py`. The UKF /
resistance / RUL math is shared between `soc_ukf.py` and `src/ukf_soh/ukf_soh.h`.
Adding any detector or constant means editing **both** sides — see §2/§3 references.
