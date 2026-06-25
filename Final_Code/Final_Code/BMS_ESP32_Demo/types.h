#pragma once
#include <stdint.h>
#include "config.h"

// =============================================================================
//  types.h  —  Team Imperium | Caterpillar Tech Challenge
//
//  All struct and enum definitions. No function bodies, no logic.
//  Everything that needs a type from this file just includes this header.
// =============================================================================


// ── Fault IDs — bit index in the 16-bit fault word ───────────────────────────
//
//  The level labels (CRITICAL / HIGH / WARNING / ACTION) are logged for human
//  readability but no longer drive contactor logic directly. Hardware response
//  is now driven entirely by the computed severity tier (see ActionTier below).
typedef enum : uint8_t {
    FLT_VOLT_IMBAL    = 0,  // WARNING  — cell voltage spread > 50 mV
    FLT_THERMAL_HOT   = 1,  // CRITICAL — hotspot / micro-short self-heating
    FLT_SENSOR_LOSS   = 2,  // CRITICAL — frame timeout / sequence gap / noise
    FLT_VOLT_GRADIENT = 3,  // HIGH     — dVdt spike (impedance growth)
    FLT_TEMP_GRADIENT = 4,  // HIGH     — dTdt spike (rapid thermal event)
    FLT_TEMP_LOW      = 5,  // HIGH     — low temperature (charge inhibit)
    FLT_GAS_PRESSURE  = 6,  // CRITICAL — pressure build-up / mechanical impact
    FLT_EIS_FAULT     = 7,  // HIGH     — impedance anomaly / micro-short
    FLT_SOC_LOW       = 8,  // WARNING  — SOC approaching end-of-discharge
    FLT_CELL_BALANCE  = 9,  // ACTION   — passive cell balancing required
    // Expanded physical taxonomy (fault_detection.md §3 A,C,E,K). Appended so
    // existing bit positions are unchanged. Keep in sync with bms_state.py.
    FLT_CELL_OV_UV    = 10, // CRITICAL — per-cell over/under-voltage
    FLT_OVERCURRENT   = 11, // CRITICAL — |I| beyond rated continuous/peak
    FLT_RUNAWAY_WARN  = 12, // CRITICAL — thermal-runaway precursor (multi-signal)
    FLT_CONTACT_RES   = 13, // HIGH     — per-group contact / connection resistance
    FLT_COUNT         = 14  // total number of fault channels
} FaultID;


// ── Hardware action tiers — driven by the peak fault severity ─────────────────
typedef enum : uint8_t {
    TIER_NORMAL      = 0,  // severity 0.00–0.30  log + LED only
    TIER_INHIBIT_CHG = 1,  // severity 0.30–0.55  open charge contactor only
    TIER_OPEN_ALL    = 2,  // severity 0.55–0.80  open both contactors
    TIER_LATCH_OPEN  = 3   // severity 0.80–1.00  open both + refuse auto-reset
} ActionTier;


// ── Human-readable name tables ─────────────────────────────────────────────────
static const char* const FAULT_NAMES[FLT_COUNT] = {
    "VOLT_IMBAL",    "THERMAL_HOT",   "SENSOR_LOSS",   "VOLT_GRADIENT",
    "TEMP_GRADIENT", "TEMP_LOW",      "GAS_PRESSURE",  "EIS_FAULT",
    "SOC_LOW",       "CELL_BALANCE",
    "CELL_OV_UV",    "OVERCURRENT",   "RUNAWAY_WARN",  "CONTACT_RES"
};

static const char* const TIER_NAMES[4] = {
    "NORMAL", "INHIBIT_CHG", "OPEN_ALL", "LATCH_OPEN"
};


// ── Incoming sensor frame ──────────────────────────────────────────────────────
//
//  Matches the packet sent by monitor.py / serial_bridge.py.
//  18 mandatory fields + 1 optional (gt_severity):
//
//  $, seq, v1, v2, v3, T_surf, T_rise, current,
//     acoustic_rms, acoustic_kHz, pressure, impact_force,
//     dVdt, dTdt, dPdt, SOC_pct, ground_truth, ts_ms
//     [, gt_severity]
//
//  gt_severity (field 19) defaults to 0.0 when not present, keeping
//  backward compatibility with the original 18-field bridge format.
struct Frame {
    uint16_t seq;
    float    v[3];           // per-cell voltages              [V]
    float    T_surf;         // cell surface temperature       [°C]
    float    T_rise;         // temperature rise above ambient [°C]
    float    current;        // pack current (negative = discharge) [A]
    float    acoustic_rms;   // acoustic RMS amplitude         [g]
    float    acoustic_kHz;   // acoustic peak frequency        [kHz]
    float    pressure;       // internal pressure              [kPa]
    float    impact_force;   // mechanical impact force        [N]
    float    dVdt;           // voltage gradient (pre-computed)[V/s]
    float    dTdt;           // temperature gradient           [°C/s]
    float    dPdt;           // pressure gradient              [kPa/s]
    float    SOC_pct;        // state of charge                [%]
    uint8_t  ground_truth;   // dataset fault_flag (0=Normal, 1=Fault)
    uint32_t ts_ms;          // dataset timestamp              [ms]
    float    gt_severity;    // dataset fault_severity_0_to_1 [0.0–1.0]
    bool     valid;          // false if parse or sanity check failed
};


// ── Persistent inter-frame detector state ─────────────────────────────────────
//
//  Zero-initialised once at startup via memset() in setup().
//  Never reset during normal operation — state persists across frames.
struct DetectorState {
    // Persistence counters: fault must hit THR_PERSIST_N consecutive frames
    uint8_t  hot_cnt;                // THERMAL_HOT consecutive-hit counter
    uint8_t  cold_cnt;               // TEMP_LOW consecutive-hit counter
    uint8_t  tgrad_cnt;              // TEMP_GRADIENT consecutive-hit counter

    // Sensor-loss tracking
    uint32_t last_frame_local_ms;    // millis() timestamp of last valid frame
    uint16_t next_expected_seq;      // for sequence-gap detection

    // Welford online statistics on dVdt (EIS / impedance detector)
    uint32_t eis_n;                  // sample count
    float    eis_mean;               // running mean of dVdt
    float    eis_M2;                 // running sum of squared deviations

    // EMA-filtered signals + per-group voltage for the internal-fault detectors
    // (micro-short, overcurrent, contact-resistance). See fault_detection.md §2.
    bool     filt_init;
    float    i_filt;                 // EMA pack current
    float    v_filt;                 // EMA pack-mean voltage (retained for RUL)
    float    vf3[3];                 // EMA per-group voltage (contact-resistance)
    bool     cf_init;
    uint8_t  intf_cnt;               // micro-short persistence
    uint8_t  ovuv_cnt;               // over/under-voltage persistence
    uint8_t  oc_cnt;                 // overcurrent (continuous) persistence
    uint8_t  rw_cnt;                 // runaway-precursor (dTdt) persistence
    uint8_t  contact_cnt;            // contact-resistance persistence

    // Previous-frame values for inter-frame cross-checks
    float    prev_v[3];
    float    prev_T_surf;
    uint32_t prev_ts_ms;
    bool     prev_valid;

    // On-device Coulomb-counting SOC (cross-check against streamed SOC_pct)
    float    soc_cc[3];
    bool     cc_init;

    // Per-cell passive balance flags (set by detect_CellBalance)
    bool     balance_cell[3];

    // Latch: set when severity hits LATCH_OPEN tier; requires manual reset
    bool     latched;

    // Per-fault severity score (0.0–1.0), updated every frame
    float    severity[FLT_COUNT];

    // System-level severity: max across all currently active faults
    float    system_severity;

    // Current hardware action tier
    ActionTier action_tier;
};


// ── Non-blocking buzzer state machine ─────────────────────────────────────────
struct BeepPattern {
    uint32_t freq;           // tone frequency [Hz]; 0 = silence
    uint32_t on_ms;          // tone-on duration [ms]
    uint32_t off_ms;         // silence gap after each tone [ms]
    uint8_t  repeat;         // number of repetitions; 255 = infinite
    uint8_t  count_done;     // (internal) completed repetitions
    uint32_t phase_start;    // (internal) millis() when current phase started
    bool     in_on_phase;    // (internal) true = currently producing tone
    bool     active;         // false = pattern done or not started
};


// ── WiFi log event ─────────────────────────────────────────────────────────────
//
//  Pushed onto xWifiLogQ from taskProcess (via logValidation) when:
//    • any fault bit changes, OR
//    • a contactor state changes, OR
//    • the heartbeat timer fires (every WIFI_HEARTBEAT_MS)
//  taskWiFiLog drains the queue and sends each event as a UDP datagram.
struct WifiLogEvent {
    uint32_t   ts_ms;           // dataset timestamp
    uint16_t   seq;
    float      v1, v2, v3;
    float      T_surf;
    float      current;
    float      SOC_pct;
    uint16_t   fault_bits;
    float      est_severity;    // our computed severity
    float      gt_severity;     // ground-truth severity from dataset
    float      soc_est;         // UKF-estimated SOC [%]
    float      soh_est;         // UKF-estimated SOH [%]
    ActionTier action_tier;
    bool       chg_open;
    bool       dsc_open;
    uint8_t    ground_truth;
    char       outcome[4];      // "TP", "TN", "FP", or "FN"
    bool       is_heartbeat;    // true = periodic heartbeat, not a state change
};
