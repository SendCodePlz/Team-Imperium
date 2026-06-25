#pragma once

// =============================================================================
//  detectors.h  —  Team Imperium | Caterpillar Tech Challenge
//
//  All fault detection logic, severity estimation, hardware control (MOSFETs,
//  LEDs, buzzer), and ground-truth validation.
//
//  IMPORTANT: Include this file ONLY from BMS_ESP32_Demo.ino.
//  Including it from multiple translation units will cause ODR violations.
//
//  Globals defined in BMS_ESP32_Demo.ino that this file references:
//    volatile uint16_t  g_faults    — 16-bit fault bitmask
//    bool               g_chg_open  — current charge MOSFET state
//    bool               g_dsc_open  — current discharge MOSFET state
//    DetectorState      DS          — all persistent inter-frame state
//    BeepPattern        g_beep      — buzzer state machine
//    QueueHandle_t      xWifiLogQ   — queue to WiFi log task
// =============================================================================

#include <Arduino.h>
#include <math.h>
#include <string.h>
#include "config.h"
#include "types.h"

// Forward-declare globals defined in the .ino
extern volatile uint16_t g_faults;
extern bool              g_chg_open;
extern bool              g_dsc_open;
extern DetectorState     DS;
extern BeepPattern       g_beep;
extern QueueHandle_t     xWifiLogQ;
extern float             g_soc_est;   // UKF-estimated SOC [%]  (set in taskProcess)
extern float             g_soh_est;   // UKF-estimated SOH [%]


// =============================================================================
//  Internal utility
// =============================================================================

static inline float _clampf(float v, float lo, float hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}


// =============================================================================
//  Buzzer — non-blocking state machine
// =============================================================================

/** Schedule a new active-buzzer beep pattern (on/off via the BC547). Not ISR-safe.
 *  freq is unused (active buzzer has a fixed internal tone); the BeepPattern.freq
 *  field is kept at 0 for struct compatibility. */
static void scheduleBuzzer(uint32_t on_ms, uint32_t off_ms, uint8_t repeat) {
    g_beep = {0, on_ms, off_ms, repeat, 0, millis(), true, true};
}

/**
 * Advance the buzzer state machine by one tick.
 * Active buzzer: digitalWrite HIGH = sound, LOW = silent. Call frequently
 * (from loop() and per processed frame) so the on/off timing stays crisp.
 */
static void tickBuzzer() {
    if (!g_beep.active) { digitalWrite(PIN_BUZZER, LOW); return; }
    uint32_t now     = millis();
    uint32_t elapsed = now - g_beep.phase_start;

    if (g_beep.in_on_phase) {
        if (elapsed >= g_beep.on_ms) {
            digitalWrite(PIN_BUZZER, LOW);
            g_beep.in_on_phase = false;
            g_beep.phase_start = now;
        } else {
            digitalWrite(PIN_BUZZER, HIGH);
        }
    } else {
        if (elapsed >= g_beep.off_ms) {
            g_beep.count_done++;
            bool infinite = (g_beep.repeat == 255);
            if (!infinite && g_beep.count_done >= g_beep.repeat) {
                g_beep.active = false;
                digitalWrite(PIN_BUZZER, LOW);
            } else {
                g_beep.in_on_phase = true;
                g_beep.phase_start = now;
            }
        }
    }
}


// =============================================================================
//  setFault — atomic set/clear of one fault bit + serial log
// =============================================================================

/**
 * Set or clear a single fault bit and emit a structured log line.
 * Only acts (and logs) on state transitions — calling with the same value
 * as the current state is a no-op.
 *
 *   id     — FaultID (0–9)
 *   active — true = fault active, false = fault cleared
 *   level  — "WARNING" / "HIGH" / "CRITICAL" / "ACTION" (for logging only)
 *   detail — human-readable context string
 */
static void setFault(uint8_t id, bool active,
                     const char* level, const char* detail) {
    uint16_t bit = (uint16_t)1u << id;
    bool     was = (g_faults & bit) != 0;

    if (active && !was) {
        g_faults |= bit;
        Serial.printf("[FAULT_SET] t=%lu ms  id=%d  name=%-15s  lvl=%-8s  %s\n",
                      millis(), id, FAULT_NAMES[id], level, detail);
        // Buzzer response scales with severity level
        if      (strcmp(level, "CRITICAL") == 0) scheduleBuzzer(300, 200, 255);
        else if (strcmp(level, "HIGH")     == 0) scheduleBuzzer(200, 100,   2);
        else if (strcmp(level, "WARNING")  == 0) scheduleBuzzer(100,   0,   1);

    } else if (!active && was) {
        g_faults         &= ~bit;
        DS.severity[id]   = 0.0f;
        Serial.printf("[FAULT_CLR] t=%lu ms  id=%d  name=%s\n",
                      millis(), id, FAULT_NAMES[id]);
        if (g_faults == 0) scheduleBuzzer(150, 0, 1);
    }
}


// =============================================================================
//  Severity estimation
//
//  Formula: severity = clamp( (signal - threshold) / (max_expected - threshold) )
//
//  max_expected values are calibrated from the dataset's fault peak observations.
//  Per-fault ceilings enforce that lower-priority faults never escalate contactors
//  beyond what the fault type warrants (e.g., cell imbalance alone = WARNING only).
// =============================================================================

static float computeFaultSeverity(FaultID id, const Frame& f) {
    float s = 0.0f;
    switch (id) {

        case FLT_THERMAL_HOT:
            // Normal max 35.5 °C; micro-short 41 °C; gas build-up 45 °C
            // Threshold 38 °C, physical damage expected above 65 °C
            s = (f.T_surf - THR_T_SURF_HOT) / (65.0f - THR_T_SURF_HOT);
            break;

        case FLT_VOLT_GRADIENT:
            // Impedance fault: dVdt up to 0.53 V/s (threshold 0.28, max 1.0)
            s = (fabsf(f.dVdt) - THR_DV_DT_HIGH) / (1.0f - THR_DV_DT_HIGH);
            break;

        case FLT_GAS_PRESSURE:
            // Gas fault: 119.8 kPa; impact: 139.9 kPa (threshold 105, max 140)
            s = (f.pressure - THR_PRES_HIGH_KPA) / (140.0f - THR_PRES_HIGH_KPA);
            break;

        case FLT_SENSOR_LOSS:
            // Always OPEN_ALL territory — sensor loss is safety-critical
            s = 0.65f;
            break;

        case FLT_TEMP_GRADIENT:
            // Normal σ 0.43 °C/s; threshold 1.2; runaway precursor ~5 °C/s
            s = (fabsf(f.dTdt) - THR_DT_DT) / (5.0f - THR_DT_DT);
            break;

        case FLT_EIS_FAULT:
            // Use dVdt as the primary proxy; blend in acoustic for micro-short
            s = (fabsf(f.dVdt) - THR_DV_DT_HIGH) / (1.0f - THR_DV_DT_HIGH);
            if (f.acoustic_rms > THR_ACOUSTIC_RMS) {
                float acoustic_s = (f.acoustic_rms - THR_ACOUSTIC_RMS)
                                   / (0.70f - THR_ACOUSTIC_RMS);
                s = fmaxf(s, acoustic_s);
            }
            break;

        case FLT_SOC_LOW:
            // Scale from warn threshold (15%) down to cutoff (5%)
            // Cap at 0.54 — low SOC alone should never open the discharge path
            s = (THR_SOC_WARN_PCT - f.SOC_pct) / THR_SOC_WARN_PCT;
            s = _clampf(s, 0.0f, 0.54f);
            break;

        case FLT_VOLT_IMBAL: {
            // WARNING only — imbalance logs and lights LED, never trips contactors
            float spread = fmaxf(f.v[0], fmaxf(f.v[1], f.v[2]))
                         - fminf(f.v[0], fminf(f.v[1], f.v[2]));
            s = (spread - THR_IMBAL_V) / (0.20f - THR_IMBAL_V);
            s = _clampf(s, 0.0f, 0.29f);
            break;
        }

        case FLT_TEMP_LOW:
            // Fixed at INHIBIT_CHG tier — cold battery inhibits charge only
            s = 0.35f;
            break;

        case FLT_CELL_BALANCE:
            // ACTION only — balance flags set, no contactor effect
            s = 0.05f;
            break;

        case FLT_CELL_OV_UV:   s = 0.75f; break;  // out-of-window → OPEN_ALL
        case FLT_OVERCURRENT:  s = 0.65f; break;  // over rating  → OPEN_ALL
        case FLT_RUNAWAY_WARN: s = 0.90f; break;  // runaway onset → LATCH_OPEN
        case FLT_CONTACT_RES:  s = 0.45f; break;  // connection fault → INHIBIT

        default:
            s = 0.10f;
            break;
    }
    return _clampf(s, 0.0f, 1.0f);
}

/** Map a severity float to an ActionTier enum value. */
static ActionTier severityToTier(float severity) {
    if (severity >= SEV_LATCH_LO)    return TIER_LATCH_OPEN;
    if (severity >= SEV_OPEN_ALL_LO) return TIER_OPEN_ALL;
    if (severity >= SEV_INHIBIT_LO)  return TIER_INHIBIT_CHG;
    return TIER_NORMAL;
}

/**
 * Recompute DS.severity[] for all active faults, update DS.system_severity
 * (max across active faults), and set DS.action_tier.
 * Call once per frame after all detectors have voted.
 */
static void updateSeverity(const Frame& f) {
    float max_sev = 0.0f;
    for (uint8_t id = 0; id < FLT_COUNT; id++) {
        if (g_faults & ((uint16_t)1u << id)) {
            DS.severity[id] = computeFaultSeverity((FaultID)id, f);
            if (DS.severity[id] > max_sev) max_sev = DS.severity[id];
        } else {
            DS.severity[id] = 0.0f;
        }
    }
    DS.system_severity = max_sev;
    DS.action_tier     = severityToTier(max_sev);

    // Log only on tier transitions
    static ActionTier prev_tier = TIER_NORMAL;
    if (DS.action_tier != prev_tier) {
        Serial.printf("[TIER] t=%lu ms  %s → %s  sev=%.3f\n",
                      millis(), TIER_NAMES[prev_tier],
                      TIER_NAMES[DS.action_tier], max_sev);
        prev_tier = DS.action_tier;
    }
}


// =============================================================================
//  Hardware output — contactors and LEDs
// =============================================================================

/**
 * Drive MOSFETs based on the current action tier.
 * Once LATCH_OPEN is reached, DS.latched is set and contactors stay open
 * until DS.latched is manually cleared (e.g., via a reset button).
 * Logs every state transition with the triggering fault and severity.
 */
static void updateContactors() {
    if (DS.action_tier == TIER_LATCH_OPEN) DS.latched = true;

    // Single main cutoff: engage (battery disconnected) at OPEN_ALL or latched.
    // INHIBIT_CHG is a warning tier here — the main contactor stays connected;
    // the caution is shown on the OLED + buzzer (no separate charge contactor).
    bool cutoff = (DS.action_tier >= TIER_OPEN_ALL) || DS.latched;

    // Mirror into the legacy charge/discharge flags so the WiFi log + [MOSFET]
    // line stay meaningful (chg path inhibited from INHIBIT_CHG upward).
    g_chg_open = (DS.action_tier >= TIER_INHIBIT_CHG) || DS.latched;
    g_dsc_open = cutoff;

    // Q1 main cutoff (+ RED LED). GATE_ON = MOSFET conducts = disconnect + red lit.
    static bool prev_cutoff = false;
    digitalWrite(PIN_MOS_MAIN, cutoff ? GATE_ON : GATE_OFF);
    if (cutoff != prev_cutoff) {
        prev_cutoff = cutoff;
        int8_t trigger_id = -1;
        for (int8_t i = FLT_COUNT - 1; i >= 0; i--) {
            if (g_faults & ((uint16_t)1u << i)) { trigger_id = i; break; }
        }
        Serial.printf(
            "[MOSFET] t=%lu ms  MAIN=%-6s  tier=%-12s  sev=%.3f  "
            "faults=0x%04X  trigger=%s\n",
            millis(),
            cutoff ? "OPEN" : "CLOSED",
            DS.latched ? "LATCH_OPEN" : TIER_NAMES[DS.action_tier],
            DS.system_severity,
            (unsigned)g_faults,
            (trigger_id >= 0) ? FAULT_NAMES[trigger_id] : "NONE"
        );
    }

    // Q2..Q4 cell-balance MOSFETs (+ YELLOW LEDs) driven from the balance flags.
    const uint8_t bal_pins[3] = BAL_PINS;
    for (int i = 0; i < 3; i++) {
        digitalWrite(bal_pins[i], DS.balance_cell[i] ? GATE_ON : GATE_OFF);
    }
}

/**
 * Drive the green system-OK LED. Red (main-cutoff pin) and yellow (balance
 * pins) are driven in updateContactors, so here we only handle green:
 * solid ON while NORMAL and not latched, OFF the moment any action tier or
 * latch engages — green and red are mutually exclusive, as wired.
 */
static void updateLEDs() {
    bool healthy = (DS.action_tier == TIER_NORMAL) && !DS.latched;
    digitalWrite(PIN_LED_GREEN, healthy ? HIGH : LOW);
}


// =============================================================================
//  Detector functions
//
//  Each function:
//    • reads const Frame& f and DS (persistent state) — no other shared state
//    • calls setFault() only when state transitions occur
//    • returns true if the fault is currently active
//    • must not call other detector functions (no cross-calls)
// =============================================================================

// ── 1. Voltage Imbalance ──────────────────────────────────────────────────────
//
//  Physics: In a healthy series string, all cells at the same SOC sit within a
//  tight voltage band. A spread > 50 mV means one cell is degraded, partially
//  discharged, or has elevated internal resistance relative to its neighbours.
//
//  Response: WARNING only. Sets balance flags (see detect_CellBalance).
//  Does not trip contactors — imbalance requires investigation, not a shutdown.
static bool detect_VoltageImbalance(const Frame& f) {
    float vmax   = fmaxf(f.v[0], fmaxf(f.v[1], f.v[2]));
    float vmin   = fminf(f.v[0], fminf(f.v[1], f.v[2]));
    float spread = vmax - vmin;
    bool  active = (spread > THR_IMBAL_V);

    static char msg[80];
    snprintf(msg, sizeof(msg), "spread=%.3f V (lim %.3f)  V=[%.4f, %.4f, %.4f]",
             spread, THR_IMBAL_V, f.v[0], f.v[1], f.v[2]);
    setFault(FLT_VOLT_IMBAL, active, "WARNING", msg);
    return active;
}

// ── 2. Thermal Hotspot ────────────────────────────────────────────────────────
//
//  Physics: Surface temperature > 38 °C or temperature rise > 5 °C above
//  ambient indicates abnormal internal heat generation — either a micro-short
//  depositing power inside the cell or elevated contact resistance.
//
//  Dataset: normal T_surf peaks at 35.5 °C; micro-short fault reaches 41 °C.
//
//  Persistence filter: requires THR_PERSIST_N consecutive frames before latching
//  to avoid nuisance trips from single noisy reads. The counter increments on
//  a hit and decrements on a miss, so transients decay gracefully.
static bool detect_ThermalHotspot(const Frame& f) {
    bool hot = (f.T_surf > THR_T_SURF_HOT) || (f.T_rise > THR_T_RISE_FAULT);
    if (hot) { if (DS.hot_cnt < THR_PERSIST_N) DS.hot_cnt++; }
    else      { if (DS.hot_cnt > 0)             DS.hot_cnt--; }
    bool active = (DS.hot_cnt >= THR_PERSIST_N);

    static char msg[80];
    snprintf(msg, sizeof(msg), "T_surf=%.2f°C  T_rise=%.2f°C  cnt=%d/%d",
             f.T_surf, f.T_rise, DS.hot_cnt, THR_PERSIST_N);
    setFault(FLT_THERMAL_HOT, active, "CRITICAL", msg);
    return active;
}

// ── 3. Sensor Loss ────────────────────────────────────────────────────────────
//
//  Three independent failure modes — all indicate the measurement chain has
//  broken down, not a battery chemistry event:
//
//  A) Frame timeout: no valid packet for SENSOR_TIMEOUT_MS.
//     Catches: cable disconnection, PC crash, bridge freeze.
//
//  B) Sequence gap: sequence numbers jumped.
//     At 10 Hz over USB-UART this should never happen; a gap means a hardware
//     or driver problem.
//
//  C) Sensor noise / loose lead: |dVdt| > 0.60 V/s with no matching change
//     in pressure or temperature. Real electrochemistry always moves multiple
//     signals; voltage spiking in isolation means a loose ADC connection.
//     Dataset fault ("Voltage sensor noise"): dVdt up to 0.94 V/s.
static bool detect_SensorLoss(const Frame& f) {
    bool active = false;
    static char msg[128];

    // A) Timeout — runs even when the frame is invalid
    if ((millis() - DS.last_frame_local_ms) > SENSOR_TIMEOUT_MS) {
        snprintf(msg, sizeof(msg), "No frame for %lu ms (limit %lu ms)",
                 millis() - DS.last_frame_local_ms, SENSOR_TIMEOUT_MS);
        active = true;
    }

    if (!f.valid) {
        snprintf(msg, sizeof(msg), "Frame parse failure (expected seq ~%u)",
                 DS.next_expected_seq);
        active = true;
        setFault(FLT_SENSOR_LOSS, active, "CRITICAL", msg);
        return active;
    }

    DS.last_frame_local_ms = millis();

    // B) Sequence gap
    if (DS.next_expected_seq != 0 && f.seq != DS.next_expected_seq) {
        snprintf(msg, sizeof(msg), "Seq gap: expected %u, got %u",
                 DS.next_expected_seq, f.seq);
        active = true;
    }
    DS.next_expected_seq = (f.seq + 1) & 0xFFFFu;

    // C) Noise pattern: voltage spike with no matching pressure or thermal event
    bool noise_pattern = (fabsf(f.dVdt) > THR_DV_DT_CRITICAL) &&
                         (fabsf(f.dPdt) < 0.5f) &&
                         (fabsf(f.dTdt) < 0.5f);
    if (noise_pattern) {
        snprintf(msg, sizeof(msg),
                 "Sensor noise: dVdt=%.3f V/s (no matching P/T change)", f.dVdt);
        active = true;
    }

    setFault(FLT_SENSOR_LOSS, active, "CRITICAL", msg);
    return active;
}

// ── 4. High Voltage Gradient ──────────────────────────────────────────────────
//
//  Physics: Under constant-current discharge, terminal voltage should decrease
//  smoothly following the OCV-SOC curve minus a steady IR drop. A sudden spike
//  in dVdt > 0.28 V/s indicates the effective impedance has increased — caused
//  by contact corrosion, partial disconnection, or accelerated capacity fade.
//
//  Dataset fault ("Impedance growth / contact degradation"): dVdt up to 0.53 V/s.
//  Note: this detector also fires on the sensor-noise fault (0.94 V/s), but
//  detect_SensorLoss catches that first with the P/T cross-check.
static bool detect_VoltageGradient(const Frame& f) {
    bool active = (fabsf(f.dVdt) > THR_DV_DT_HIGH);

    static char msg[80];
    snprintf(msg, sizeof(msg), "dVdt=%.4f V/s  (lim %.3f V/s)", f.dVdt, THR_DV_DT_HIGH);
    setFault(FLT_VOLT_GRADIENT, active, "HIGH", msg);
    return active;
}

// ── 5. High Temperature Gradient ─────────────────────────────────────────────
//
//  Physics: Rapid temperature rise > 1.2 °C/s is an early warning of an
//  exothermic side reaction (lithium plating onset, electrolyte decomposition)
//  or a thermal runaway precursor. Normal temperature variance is ±0.43 °C/s
//  (1-sigma); the threshold is approximately 3-sigma above that baseline.
static bool detect_TempGradient(const Frame& f) {
    // Debounce: a genuine rapid thermal event sustains for several frames,
    // whereas dTdt sensor noise spikes for a single sample. Require
    // THR_PERSIST_N consecutive hits (counter decays on a miss) so isolated
    // noise spikes never latch the fault — the dominant false-positive source.
    bool spike = (fabsf(f.dTdt) > THR_DT_DT);
    if (spike) { if (DS.tgrad_cnt < THR_PERSIST_N) DS.tgrad_cnt++; }
    else        { if (DS.tgrad_cnt > 0)             DS.tgrad_cnt--; }
    bool active = (DS.tgrad_cnt >= THR_PERSIST_N);

    static char msg[80];
    snprintf(msg, sizeof(msg), "dTdt=%.3f °C/s  (lim %.3f °C/s  cnt=%d/%d)",
             f.dTdt, THR_DT_DT, DS.tgrad_cnt, THR_PERSIST_N);
    setFault(FLT_TEMP_GRADIENT, active, "HIGH", msg);
    return active;
}

// ── 6. Low Temperature ────────────────────────────────────────────────────────
//
//  Physics: Lithium metal plating occurs at graphite anodes when charging below
//  ~5 °C; risk increases below ~18 °C. Plated lithium dendrites can eventually
//  cause internal shorts. The charge path is inhibited; discharge remains open
//  because draining a cold cell is electrochemically safe.
//
//  Persistence filter prevents nuisance trips on transient cold reads.
static bool detect_LowTemp(const Frame& f) {
    bool cold = (f.T_surf < THR_T_COLD);
    if (cold) { if (DS.cold_cnt < THR_PERSIST_N) DS.cold_cnt++; }
    else       { if (DS.cold_cnt > 0)             DS.cold_cnt--; }
    bool active = (DS.cold_cnt >= THR_PERSIST_N);

    static char msg[80];
    snprintf(msg, sizeof(msg), "T_surf=%.2f°C  (cold lim %.1f°C  cnt=%d/%d)",
             f.T_surf, THR_T_COLD, DS.cold_cnt, THR_PERSIST_N);
    setFault(FLT_TEMP_LOW, active, "HIGH", msg);
    return active;
}

// ── 7. Gas / Pressure + Acoustic / Mechanical Impact ─────────────────────────
//
//  Two physically different events share the pressure sensor as their primary
//  channel, so they are combined in one detector:
//
//  Gas generation: Electrolyte decomposition or separator degradation produces
//  gases (CO2, H2) that slowly raise the internal pressure. The signature is
//  a sustained ELEVATED pressure, not a fast spike — over hundreds of seconds
//  the ramp rate dPdt stays near zero, so an absolute pressure threshold is the
//  correct discriminator. Trigger: pressure > 105 kPa (≈4 kPa over atmospheric).
//  (Previously this also required dPdt > 0.8 kPa/s, which contradicted the slow-
//  ramp physics and suppressed most of the gas fault — that AND has been removed.
//  THR_DP_DT is retained in config.h for reference/logging only.)
//
//  Mechanical impact: An external obstacle strike creates an abrupt pressure
//  spike simultaneously with a large acoustic event and measurable impact force.
//  Trigger: any of — pressure > 115 kPa, acoustic_rms > 0.060 g, impact > 10 N.
static bool detect_GasPressure(const Frame& f) {
    bool gas_trip    = (f.pressure > THR_PRES_HIGH_KPA);
    bool impact_trip = (f.pressure > THR_PRES_IMPACT_KPA) ||
                       (f.acoustic_rms > THR_ACOUSTIC_RMS) ||
                       (fabsf(f.impact_force) > THR_IMPACT_FORCE);
    bool active = gas_trip || impact_trip;

    static char msg[128];
    snprintf(msg, sizeof(msg),
             "P=%.2f kPa  dP=%.3f kPa/s  ac=%.4f g  F=%.1f N%s",
             f.pressure, f.dPdt, f.acoustic_rms, f.impact_force,
             impact_trip ? "  [IMPACT]" : "");
    setFault(FLT_GAS_PRESSURE, active, "CRITICAL", msg);
    return active;
}

// ── 8. EIS / Internal Impedance Fault ─────────────────────────────────────────
//
//  Two internal fault signatures are grouped here because both reflect a change
//  in the cell's internal electrochemical state:
//
//  Impedance growth (Welford z-score on dVdt):
//    The Welford online algorithm maintains a running mean and variance of dVdt
//    over the entire session. When the current reading deviates more than
//    THR_EIS_ZSCORE standard deviations from the running mean, the cell's
//    effective impedance has changed measurably.
//    Dataset fault ("Impedance growth"): z-score reaches ~24, well above threshold 4.
//    A 30-frame warm-up period is required before the z-score is trusted.
//
//  Micro-short / abnormal self-heating:
//    Dendrite growth creates an internal short path that dissipates power as heat.
//    The signature is elevated acoustic emission (dendrite cracking, micro-bubble
//    formation) combined with temperature rise that exceeds what the load current
//    alone could explain.
//    Trigger: acoustic_rms > 0.042 g AND T_rise > 4.5 °C simultaneously.
static bool detect_EISFault(const Frame& f) {
    // Welford update — runs every frame regardless of whether fault is active
    DS.eis_n++;
    float delta  = f.dVdt - DS.eis_mean;
    DS.eis_mean += delta / (float)DS.eis_n;
    DS.eis_M2   += delta * (f.dVdt - DS.eis_mean);

    bool active = false;
    static char msg[128];

    if (DS.eis_n >= EIS_WARMUP_N) {
        float sigma = (DS.eis_n > 1)
                    ? sqrtf(DS.eis_M2 / (float)(DS.eis_n - 1))
                    : 0.001f;
        if (sigma > 1e-6f) {
            float z = fabsf(f.dVdt - DS.eis_mean) / sigma;
            if (z > THR_EIS_ZSCORE) {
                snprintf(msg, sizeof(msg),
                         "dVdt z=%.1f  (dVdt=%.4f  μ=%.4f  σ=%.5f)",
                         z, f.dVdt, DS.eis_mean, sigma);
                active = true;
            }
        }
    }

    // Legacy micro-short heuristic: elevated acoustic + abnormal T_rise.
    if (!active && (f.acoustic_rms > 0.042f) && (f.T_rise > 4.5f)) {
        snprintf(msg, sizeof(msg),
                 "Micro-short: acoustic=%.4f g  T_rise=%.2f°C",
                 f.acoustic_rms, f.T_rise);
        active = true;
    }

    // ── EMA filter (noise rule 1) feeding the internal-fault branches ────────
    float vmean = (f.v[0] + f.v[1] + f.v[2]) / 3.0f;
    if (!DS.filt_init) {
        DS.i_filt = f.current;  DS.v_filt = vmean;
        DS.vf3[0] = f.v[0];     DS.vf3[1] = f.v[1];  DS.vf3[2] = f.v[2];
        DS.filt_init = true;    DS.cf_init = true;
    } else {
        DS.i_filt += EMA_ALPHA * (f.current - DS.i_filt);
        DS.v_filt += EMA_ALPHA * (vmean     - DS.v_filt);
        for (int i = 0; i < 3; i++)
            DS.vf3[i] += EMA_ALPHA * (f.v[i] - DS.vf3[i]);
    }

    // Micro-short (demo heuristic): small STEADY extra draw AND acoustic
    // emission together (rule 5 — two independent channels), so a drifting
    // current sensor alone can't trip it. Production path = dV/dQ + rest-phase
    // coulombic efficiency (inert here; constant-current stream).
    bool micro = (fabsf(DS.i_filt) > THR_MICRO_I_FILT) &&
                 (f.acoustic_rms   > THR_MICRO_ACOUSTIC);
    if (micro) DS.intf_cnt = (DS.intf_cnt < THR_PERSIST_N) ? DS.intf_cnt + 1 : THR_PERSIST_N;
    else if (DS.intf_cnt > 0) DS.intf_cnt--;
    if (DS.intf_cnt >= THR_INTF_PERSIST) {
        snprintf(msg, sizeof(msg),
                 "Micro-short: I_filt=%.3f A  acoustic=%.4f g", DS.i_filt, f.acoustic_rms);
        active = true;
    }

    // NOTE: impedance growth is a MULTI-CYCLE AGING signature, not a per-frame
    // fault — within one constant-current cycle the SOC-estimate error swamps the
    // resistance rise. It is surfaced through RUL power-fade (fault_detection.md
    // §4). r_excess() is retained for RUL + contact-resistance.

    setFault(FLT_EIS_FAULT, active, "HIGH", msg);
    return active;
}

// ── internal-resistance estimator (fault_detection.md §2) ─────────────────────
//  Excess series resistance [Ω] beyond the healthy 1-RC model, NORMALIZED by
//  current so it is load-independent. ~0 healthy; positive when degraded.
//  SOC reference = UKF estimate (g_soc_est). Used by contact-resistance + RUL.
static inline float r_excess(float v_filt, float i_filt) {
    if (fabsf(i_filt) < MIN_LOAD_A) return 0.0f;
    float v_pred = ukf_ocv(g_soc_est / 100.0f) + (i_filt - UKF_I_NOM) * UKF_R0;
    return (v_pred - v_filt) / fabsf(i_filt);
}

// ── New physical guard detectors (fault_detection.md §3 A,C,E,K) ──────────────
//  Inert on this constant-1C dataset; protect a real pack. detect_EISFault must
//  run first each frame to update DS.i_filt / DS.vf3 used here.
static bool detect_CellOvUv(const Frame& f) {
    bool bad = (f.v[0] > V_CELL_MAX) || (f.v[1] > V_CELL_MAX) || (f.v[2] > V_CELL_MAX) ||
               (f.v[0] < V_CELL_MIN) || (f.v[1] < V_CELL_MIN) || (f.v[2] < V_CELL_MIN);
    if (bad) DS.ovuv_cnt = (DS.ovuv_cnt < THR_PERSIST_N) ? DS.ovuv_cnt + 1 : THR_PERSIST_N;
    else if (DS.ovuv_cnt > 0) DS.ovuv_cnt--;
    bool active = DS.ovuv_cnt >= OVUV_PERSIST;
    static char msg[96];
    snprintf(msg, sizeof(msg), "cell V out of [%.2f,%.2f]: %.3f/%.3f/%.3f",
             V_CELL_MIN, V_CELL_MAX, f.v[0], f.v[1], f.v[2]);
    setFault(FLT_CELL_OV_UV, active, "CRITICAL", msg);
    return active;
}

static bool detect_Overcurrent(const Frame& f) {
    bool peak = fabsf(f.current) > I_RATED_PEAK;
    if (fabsf(DS.i_filt) > I_RATED_CONT)
        DS.oc_cnt = (DS.oc_cnt < 255) ? DS.oc_cnt + 1 : 255;
    else if (DS.oc_cnt > 0) DS.oc_cnt--;
    bool active = peak || (DS.oc_cnt >= OC_CONT_PERSIST);
    static char msg[96];
    snprintf(msg, sizeof(msg), "I=%.2f A  I_filt=%.2f A  (cont %.1f / peak %.1f)",
             f.current, DS.i_filt, I_RATED_CONT, I_RATED_PEAK);
    setFault(FLT_OVERCURRENT, active, "CRITICAL", msg);
    return active;
}

static bool detect_RunawayPrecursor(const Frame& f) {
    // Multi-signal onset (rule 5): sustained temp acceleration AND gas pressure
    // AND a voltage collapse — never any single noisy channel.
    if (fabsf(f.dTdt) > THR_DT_DT)
        DS.rw_cnt = (DS.rw_cnt < THR_PERSIST_N) ? DS.rw_cnt + 1 : THR_PERSIST_N;
    else if (DS.rw_cnt > 0) DS.rw_cnt--;
    bool active = (DS.rw_cnt >= THR_INTF_PERSIST) &&
                  (f.pressure > THR_PRES_HIGH_KPA) &&
                  (f.dVdt < -THR_DV_DT_HIGH);
    static char msg[96];
    snprintf(msg, sizeof(msg), "dTdt=%.2f P=%.1f dVdt=%.3f", f.dTdt, f.pressure, f.dVdt);
    setFault(FLT_RUNAWAY_WARN, active, "CRITICAL", msg);
    return active;
}

static bool detect_ContactResistance(const Frame& f) {
    // Loose lead / corroded joint: ONE group's excess series resistance diverges
    // from its siblings. Per-group divergence cancels common-mode SOC-drift bias.
    bool active = false;
    static char msg[96];
    if (fabsf(DS.i_filt) > MIN_LOAD_A) {
        float r[3];
        for (int i = 0; i < 3; i++) r[i] = r_excess(DS.vf3[i], DS.i_filt);
        // median of 3
        float mx = fmaxf(r[0], fmaxf(r[1], r[2]));
        float mn = fminf(r[0], fminf(r[1], r[2]));
        float med = r[0] + r[1] + r[2] - mx - mn;
        if ((mx - med) > THR_CONTACT_DR_OHM)
            DS.contact_cnt = (DS.contact_cnt < THR_PERSIST_N) ? DS.contact_cnt + 1 : THR_PERSIST_N;
        else if (DS.contact_cnt > 0) DS.contact_cnt--;
        active = DS.contact_cnt >= THR_INTF_PERSIST;
        snprintf(msg, sizeof(msg), "group dR=%.4f Ohm (max-median)", mx - med);
    }
    setFault(FLT_CONTACT_RES, active, "HIGH", msg);
    return active;
}

// ── 9. SOC Low ────────────────────────────────────────────────────────────────
//
//  Uses the pre-computed SOC_pct from the dataset packet, which encodes the
//  true capacity fade. Warns at < 15% (end-of-useful-discharge approaching)
//  and triggers a hard cutoff at < 5%.
//
//  On-device cross-check: a Coulomb-counting integration of the current signal
//  is maintained independently of the streamed SOC. Large divergence between
//  on-device and streamed SOC would indicate a current sensor fault. This check
//  is implemented as a future extension point — the counter is maintained but
//  divergence detection is left to the evaluator as a logged value.
static bool detect_SOCLow(const Frame& f) {
    // Initialise Coulomb counter from streamed SOC on first non-zero current
    if (!DS.cc_init && fabsf(f.current) > 0.1f) {
        for (int i = 0; i < 3; i++) DS.soc_cc[i] = f.SOC_pct / 100.0f;
        DS.cc_init = true;
    }
    // Integrate current into Coulomb counter
    if (DS.cc_init && DS.prev_valid) {
        float dt_s = (float)(f.ts_ms - DS.prev_ts_ms) / 1000.0f;
        if (dt_s > 0.001f) {
            for (int i = 0; i < 3; i++) {
                DS.soc_cc[i] -= (f.current * dt_s) / (CELL_CAPACITY_AH * 3600.0f);
                DS.soc_cc[i]  = _clampf(DS.soc_cc[i], 0.0f, 1.0f);
            }
        }
    }

    bool active = (f.SOC_pct < THR_SOC_WARN_PCT);
    static char msg[128];
    snprintf(msg, sizeof(msg),
             "SOC=%.2f%%  (warn<%.0f%%  cutoff<%.0f%%)  cc_soc=%.1f%%",
             f.SOC_pct, THR_SOC_WARN_PCT, THR_SOC_CUTOFF_PCT,
             DS.cc_init ? DS.soc_cc[0] * 100.0f : -1.0f);
    setFault(FLT_SOC_LOW, active, "WARNING", msg);
    return active;
}

// ── 10. Cell Balance ──────────────────────────────────────────────────────────
//
//  Physics: Cells at different voltages during a rest period have different SOCs
//  due to self-discharge rate variations, capacity fade differences, or initial
//  imbalance. Passive balancing bleeds charge from the highest cell through a
//  bypass resistor.
//
//  This sets DS.balance_cell[i] flags which would drive balance switch outputs
//  in a production BMS. The demo hardware does not implement balance switches,
//  but the logic is complete and the flags are logged.
//
//  Note: uses the same 50 mV spread threshold as detect_VoltageImbalance —
//  both detectors share the physics but serve different purposes (one is a
//  safety alert, the other drives active housekeeping).
static bool detect_CellBalance(const Frame& f) {
    float vmax   = fmaxf(f.v[0], fmaxf(f.v[1], f.v[2]));
    float vmin   = fminf(f.v[0], fminf(f.v[1], f.v[2]));
    float spread = vmax - vmin;
    bool  needed = (spread > THR_IMBAL_V);
    float vmean  = (f.v[0] + f.v[1] + f.v[2]) / 3.0f;

    for (int i = 0; i < 3; i++) {
        DS.balance_cell[i] = needed && (f.v[i] > vmean + THR_IMBAL_V / 2.0f);
    }

    static char msg[80];
    snprintf(msg, sizeof(msg), "spread=%.4f V  bal=[%d, %d, %d]",
             spread, DS.balance_cell[0], DS.balance_cell[1], DS.balance_cell[2]);
    setFault(FLT_CELL_BALANCE, needed, "ACTION", msg);
    return needed;
}


// =============================================================================
//  Master dispatcher
// =============================================================================

/**
 * Run all 10 detectors in priority order (cheapest / most safety-critical
 * first), then compute severity, update hardware, and tick the buzzer.
 *
 * Called once per valid frame from taskProcess.
 */
static void runAllDetectors(const Frame& f) {
    detect_SensorLoss(f);         // first: runs even on invalid frames
    detect_VoltageImbalance(f);
    detect_LowTemp(f);            // before thermal: cold ≠ hot
    detect_ThermalHotspot(f);
    detect_VoltageGradient(f);
    detect_TempGradient(f);
    detect_GasPressure(f);
    detect_EISFault(f);           // Welford + EMA filters: must run every frame
    detect_CellOvUv(f);
    detect_Overcurrent(f);        // uses DS.i_filt from detect_EISFault
    detect_RunawayPrecursor(f);
    detect_ContactResistance(f);  // uses DS.vf3 / DS.i_filt from detect_EISFault
    detect_CellBalance(f);
    detect_SOCLow(f);             // Coulomb counter: needs prev_ts_ms

    updateSeverity(f);
    updateContactors();
    updateLEDs();
    tickBuzzer();

    // Carry forward state for next frame's inter-frame calculations
    if (f.valid) {
        for (int i = 0; i < 3; i++) DS.prev_v[i] = f.v[i];
        DS.prev_T_surf = f.T_surf;
        DS.prev_ts_ms  = f.ts_ms;
        DS.prev_valid  = true;
    }
}


// =============================================================================
//  Ground-truth validation + WiFi log queue
// =============================================================================

/**
 * Compare our fault detection against the dataset's ground-truth label and
 * emit a TP/TN/FP/FN result on state changes.
 *
 * Also queues a WifiLogEvent for taskWiFiLog when fault/contactor state changes
 * or the heartbeat timer fires. queue_wifi should be false on invalid frames.
 */
static void logValidation(const Frame& f, bool queue_wifi) {
    // Advisory faults (cell imbalance, low SOC, cell balance) are normal
    // operating conditions, not labeled fault EVENTS — exclude them from the
    // TP/FP/FN scoring so low-SOC end-of-discharge doesn't flood false positives.
    // Keep in sync with ADVISORY_FAULTS / SCORING_MASK in bms_state.py.
    const uint16_t SCORING_MASK = (uint16_t)~(
        ((uint16_t)1u << FLT_VOLT_IMBAL) |
        ((uint16_t)1u << FLT_SOC_LOW)    |
        ((uint16_t)1u << FLT_CELL_BALANCE));
    bool any_fault = (g_faults & SCORING_MASK) != 0;

    const char* outcome;
    if      (f.ground_truth == 1 &&  any_fault) outcome = "TP";
    else if (f.ground_truth == 0 && !any_fault) outcome = "TN";
    else if (f.ground_truth == 1 && !any_fault) outcome = "FN";
    else                                         outcome = "FP";

    // Only log when outcome changes — avoids flooding serial at 10 Hz
    static const char* prev_outcome = "";
    if (strcmp(outcome, prev_outcome) != 0) {
        Serial.printf(
            "[VALIDATE] t=%lu ms  ds_t=%.1fs  gt=%d  "
            "faults=0x%04X  sev=%.3f  tier=%s  outcome=%s\n",
            millis(), f.ts_ms / 1000.0f,
            f.ground_truth, (unsigned)g_faults,
            DS.system_severity, TIER_NAMES[DS.action_tier], outcome
        );
        prev_outcome = outcome;
    }

    // Queue WiFi log event on fault/contactor state changes or heartbeat
    if (queue_wifi && xWifiLogQ != nullptr) {
        static uint16_t prev_fault_bits = 0;
        static bool     prev_chg        = false;
        static bool     prev_dsc        = false;
        static uint32_t last_hb_ms      = 0;

        bool state_changed = (g_faults   != prev_fault_bits) ||
                             (g_chg_open != prev_chg)         ||
                             (g_dsc_open != prev_dsc);
        bool heartbeat_due = (millis() - last_hb_ms) >= WIFI_HEARTBEAT_MS;

        if (state_changed || heartbeat_due) {
            WifiLogEvent ev;
            ev.ts_ms        = f.ts_ms;
            ev.seq          = f.seq;
            ev.v1 = f.v[0]; ev.v2 = f.v[1]; ev.v3 = f.v[2];
            ev.T_surf       = f.T_surf;
            ev.current      = f.current;
            ev.SOC_pct      = f.SOC_pct;
            ev.fault_bits   = (uint16_t)g_faults;
            ev.est_severity = DS.system_severity;
            ev.gt_severity  = f.gt_severity;
            ev.soc_est      = g_soc_est;
            ev.soh_est      = g_soh_est;
            ev.action_tier  = DS.action_tier;
            ev.chg_open     = g_chg_open;
            ev.dsc_open     = g_dsc_open;
            ev.ground_truth = f.ground_truth;
            ev.is_heartbeat = heartbeat_due && !state_changed;
            strncpy(ev.outcome, outcome, sizeof(ev.outcome) - 1);
            ev.outcome[sizeof(ev.outcome) - 1] = '\0';

            xQueueSend(xWifiLogQ, &ev, 0);   // non-blocking: drop if queue full

            prev_fault_bits = g_faults;
            prev_chg        = g_chg_open;
            prev_dsc        = g_dsc_open;
            if (heartbeat_due) last_hb_ms = millis();
        }
    }
}
