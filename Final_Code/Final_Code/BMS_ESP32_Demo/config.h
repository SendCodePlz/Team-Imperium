#pragma once

// =============================================================================
//  config.h  —  Team Imperium | Caterpillar Tech Challenge
//
//  Every hardware pin, detection threshold, WiFi credential, and timing
//  constant lives here. Change hardware setup HERE and nowhere else.
// =============================================================================


// ── Serial ────────────────────────────────────────────────────────────────────
#define SERIAL_BAUD             115200UL


// ── GPIO — ESP32 DOIT DevKit V1, Team Imperium breadboard wiring ─────────────
//  Low-side logic-level MOSFET drivers (IRLZ44N). Driving a gate HIGH turns the
//  MOSFET ON, which both performs the action AND lights the indicator LED wired
//  on that MOSFET's drain — so each gate pin doubles as its LED line.
//  Pack: 3S2P → 3 series cell-groups, monitored as 3 "cells".
#define PIN_MOS_MAIN            14   // Q1 main cutoff      (+ RED fault LED)
#define PIN_BAL1                27   // Q2 cell-1 balance   (+ YELLOW LED)
#define PIN_BAL2                26   // Q3 cell-2 balance   (+ YELLOW LED)
#define PIN_BAL3                25   // Q4 cell-3 balance   (+ YELLOW LED)
#define BAL_PINS                { PIN_BAL1, PIN_BAL2, PIN_BAL3 }

//  Direct indicator LED (no MOSFET): system-OK
#define PIN_LED_GREEN           33   // green — solid ON while healthy

//  Active buzzer driven through a BC547 transistor (digital ON/OFF, not PWM)
#define PIN_BUZZER              32

//  I2C OLED status display (SSD1306 128x64)
#define PIN_OLED_SDA            21
#define PIN_OLED_SCL            22
#define OLED_I2C_ADDR           0x3C
#define OLED_W                  128
#define OLED_H                  64

//  Tactile buttons for fault injection / reset (INPUT_PULLUP, active LOW)
#define PIN_BTN_FAULT1          18   // inject a critical fault (cutoff demo)
#define PIN_BTN_FAULT2          19   // inject a cell-balance / warning condition
#define PIN_BTN_RESET            5   // clear faults + latch (back to healthy)

//  Gate / MOSFET drive level: HIGH = ON (action engaged + indicator LED lit)
#define GATE_ON                 HIGH
#define GATE_OFF                LOW


// ── Voltage thresholds ────────────────────────────────────────────────────────
#define THR_IMBAL_V             0.050f  // [V]   max permitted 3-cell spread
#define THR_V_MAX               4.22f   // [V]   NMC hard overvoltage limit
#define THR_V_MIN               2.50f   // [V]   undervoltage cutoff


// ── Temperature thresholds ────────────────────────────────────────────────────
//  Dataset normal range: T_surf 26.4–35.5 °C (mean 31.7, σ 2.3)
//  Micro-short fault reaches 41 °C; gas build-up fault reaches 45 °C
#define THR_T_SURF_HOT          38.0f   // [°C]  hotspot trigger (absolute — clean separator)
//  IMPORTANT: under normal 1C discharge the cell self-heats up to ~10.5 °C
//  above ambient, so T_rise is NOT a usable fault discriminator here (the
//  micro-short fault's own rise is only ~6.7 °C). The previous 5.0 °C limit
//  tripped THERMAL_HOT on ~75% of normal rows — the #1 false-positive source.
//  Raised above the normal-discharge ceiling so only T_surf drives this fault.
#define THR_T_RISE_FAULT        12.0f   // [°C]  rise above ambient (normal peaks ~10.5)
#define THR_T_COLD              18.0f   // [°C]  low-temp charge inhibit (Li plating risk)
#define THR_DT_DT                1.2f   // [°C/s] rapid thermal gradient — normal σ 0.43
#define THR_PERSIST_N               8   // consecutive frames needed to latch persist faults


// ── Acoustic / mechanical thresholds ─────────────────────────────────────────
//  Normal acoustic_rms: 0.002–0.039 g; impact fault peak: 0.67 g
//  Normal impact_force: ±3 N;          impact fault peak: 5200 N
#define THR_ACOUSTIC_RMS        0.060f  // [g]
#define THR_IMPACT_FORCE        10.0f   // [N]


// ── Pressure thresholds ───────────────────────────────────────────────────────
//  Normal max: 103.8 kPa   gas fault: 119.8 kPa   impact fault: 139.9 kPa
#define THR_PRES_HIGH_KPA      105.0f   // [kPa] gas generation trigger
#define THR_PRES_IMPACT_KPA    115.0f   // [kPa] impact spike trigger
#define THR_DP_DT                0.8f   // [kPa/s] sustained pressure rise rate


// ── Voltage gradient thresholds ───────────────────────────────────────────────
//  Normal 3-sigma: ±0.07 V/s;  impedance growth fault: 0.53 V/s;  noise fault: 0.94 V/s
#define THR_DV_DT_HIGH          0.28f   // [V/s] impedance / contact fault trigger
#define THR_DV_DT_CRITICAL      0.60f   // [V/s] sensor noise / loose lead trigger


// ── EIS / internal impedance detection ───────────────────────────────────────
#define THR_EIS_ZSCORE           4.0f   // Welford z-score threshold for impedance anomaly
#define EIS_WARMUP_N               30   // frames before z-score is trusted


// ── Internal-fault filters & micro-short (fault_detection.md §2/§3 I) ─────────
//  Raw v/current are too noisy per-frame; an EMA exposes the small SUSTAINED
//  offsets internal faults produce. Micro-short requires current AND acoustic
//  (two independent channels) so a drifting current sensor alone can't trip it.
//  MUST stay in sync with detector_sim.py.
#define EMA_ALPHA                0.10f   // EMA rate (~1 s @10 Hz)
#define MIN_LOAD_A               0.50f   // [A] resistance only observable under load
#define THR_INTF_PERSIST            3    // frames the micro-short branch must hold
#define THR_MICRO_I_FILT         2.95f   // [A] |filtered current| (demo-specific)
#define THR_MICRO_ACOUSTIC      0.025f   // [g] corroborating acoustic emission
//  NOTE: impedance growth is NOT a per-frame fault — it is multi-cycle aging,
//  surfaced via RUL power-fade. ukf_ocv()/UKF_R0/UKF_I_NOM feed _r_excess().


// ── New physical guard detectors (fault_detection.md §3 A,C,E,K) ──────────────
//  Limits from cell spec, NOT shaved to this dataset → inert on the constant-1C
//  demo, protect a real pack. MUST stay in sync with detector_sim.py.
#define V_CELL_MAX               4.20f   // [V] per-cell over-voltage limit
#define V_CELL_MIN               2.50f   // [V] per-cell under-voltage limit
#define OVUV_PERSIST                3    // frames out-of-window before tripping
#define I_RATED_CONT             5.00f   // [A] continuous current rating (~1.7C)
#define I_RATED_PEAK            10.00f   // [A] instantaneous peak current rating
#define OC_CONT_PERSIST            20    // frames over continuous rating (~2 s)
#define THR_CONTACT_DR_OHM      0.020f   // [Ω] per-group excess-R divergence


// ── SOC / SOH thresholds ──────────────────────────────────────────────────────
#define THR_SOC_WARN_PCT        15.0f   // [%] warn at low SOC
#define THR_SOC_CUTOFF_PCT       5.0f   // [%] hard cutoff — open both contactors
#define CELL_CAPACITY_AH         2.9f   // [Ah] nominal cell capacity for Coulomb counting


// ── Timing ────────────────────────────────────────────────────────────────────
#define SENSOR_TIMEOUT_MS      3000UL   // [ms] no valid frame → sensor-loss fault


// ── Severity action tiers ─────────────────────────────────────────────────────
//  These map our computed severity score (0.0–1.0) to a hardware response tier.
//  Calibrated from the dataset's fault_severity_0_to_1 ground-truth column.
//
//   0.00 – SEV_INHIBIT_LO  → NORMAL      log + LED, no contactor action
//   SEV_INHIBIT_LO  – ...  → INHIBIT_CHG open charge contactor only
//   SEV_OPEN_ALL_LO – ...  → OPEN_ALL    open both contactors
//   SEV_LATCH_LO    – 1.0  → LATCH_OPEN  open both + require manual reset
#define SEV_INHIBIT_LO          0.30f
#define SEV_OPEN_ALL_LO         0.55f
#define SEV_LATCH_LO            0.80f


// ── WiFi — UDP log server (csv_logger.py on the PC) ──────────────────────────
#define WIFI_SSID               "xxxxxx"
#define WIFI_PASS               "xxxxxx"
#define LOG_SERVER_IP           "192.168.31.178"  // PC LAN IP (run: ip a / ipconfig)
#define LOG_SERVER_PORT         4210
#define WIFI_CONNECT_TIMEOUT_MS 10000UL
#define WIFI_HEARTBEAT_MS         100UL           // heartbeat packet interval —
                                                  // ~1 per frame so monitor.py's
                                                  // --wifi dashboard plots live
                                                  // (was 5000; raise if WiFi floods)
