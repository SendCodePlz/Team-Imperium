// =============================================================================
//  BMS_ESP32_Demo.ino  —  Team Imperium | Caterpillar Tech Challenge
//
//  Thin main file. All logic lives in the four headers:
//
//    config.h      Every pin number, threshold, WiFi credential, timing constant
//    types.h       Frame, DetectorState, BeepPattern, WifiLogEvent structs
//    detectors.h   10 fault detectors, severity engine, hardware control
//    wifi_log.h    WiFi task, UDP sender
//
//  This file contains:
//    • Global variable definitions (declared extern in the headers)
//    • Serial frame parser (parseFrame, parseMetadata)
//    • Two FreeRTOS tasks (taskRX, taskProcess)
//    • setup() and loop()
//
//  PC side:
//    python monitor.py --file dataset.xlsx --port /dev/ttyUSB0
//    python monitor.py --file dataset.xlsx --dry-run        (no ESP32 needed)
// =============================================================================

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <Adafruit_GFX.h>          // Library Manager: "Adafruit GFX Library"
#include <Adafruit_SSD1306.h>      // Library Manager: "Adafruit SSD1306"
#include <math.h>
#include <string.h>

#include "config.h"
#include "types.h"
#include "src/ukf_soh/ukf_soh.h"
#include "detectors.h"
#include "wifi_log.h"


// =============================================================================
//  Global variable definitions
//  (declared extern in detectors.h and wifi_log.h — defined exactly once here)
// =============================================================================

volatile uint16_t g_faults   = 0;      // 16-bit fault bitmask
bool              g_chg_open = false;  // charge path inhibited?  (false = OK at boot)
bool              g_dsc_open = false;  // main cutoff engaged?    (false = connected)
DetectorState     DS         = {};     // zero-initialise all persistent state
BeepPattern       g_beep     = {};     // buzzer state machine (inactive at start)

// OLED status display (SSD1306 over I2C). g_oled_ok is false if it didn't init,
// so the rest of the firmware still runs headless.
Adafruit_SSD1306  g_oled(OLED_W, OLED_H, &Wire, -1);
bool              g_oled_ok  = false;

// Joint SOC/SOH estimator (mirror of soc_ukf.py on the PC) + its outputs.
// g_soc_est / g_soh_est are read by logValidation() in detectors.h.
UkfSoh            g_ukf;                // on-device Unscented Kalman Filter
float             g_soc_est  = 100.0f;  // UKF-estimated SOC [%]
float             g_soh_est  = 100.0f;  // UKF-estimated SOH [%]

QueueHandle_t     xFrameQ    = nullptr;  // taskRX → taskProcess
QueueHandle_t     xWifiLogQ  = nullptr;  // taskProcess → taskWiFiLog


// =============================================================================
//  Serial frame parser
// =============================================================================

/** Echo a metadata header line ('#,...') to the serial monitor. */
static void parseMetadata(const char* line) {
    Serial.printf("[META] %s\n", line);
}

/**
 * Parse one data line ('$,...') from the bridge into a Frame struct.
 *
 * Packet format — 18 mandatory fields, 1 optional:
 *   $, seq, v1, v2, v3, T_surf, T_rise, current,
 *      acoustic_rms, acoustic_kHz, pressure, impact_force,
 *      dVdt, dTdt, dPdt, SOC_pct, ground_truth, ts_ms
 *      [, gt_severity]          ← optional field 19; defaults to 0.0
 *
 * Sets f.valid = true on success.
 * Returns false (and leaves f.valid = false) on parse failure or sanity check.
 */
static bool parseFrame(const char* line, Frame& f) {
    memset(&f, 0, sizeof(f));
    f.valid = false;

    if (line[0] != '$') return false;

    // Work on a local copy — strtok() modifies the buffer in-place
    static char buf[512];
    strncpy(buf, line, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    char* tok = strtok(buf, ",");           // consume the leading '$'
    if (!tok) return false;

// Macro helpers: advance to next token and parse as float / int / long
#define NEXT_F(dst)  tok = strtok(nullptr, ","); if (!tok) return false; (dst) = (float)atof(tok)
#define NEXT_I(dst)  tok = strtok(nullptr, ","); if (!tok) return false; (dst) = (uint16_t)atoi(tok)
#define NEXT_U(dst)  tok = strtok(nullptr, ","); if (!tok) return false; (dst) = (uint32_t)atol(tok)

    NEXT_I(f.seq);
    NEXT_F(f.v[0]);  NEXT_F(f.v[1]);  NEXT_F(f.v[2]);
    NEXT_F(f.T_surf);
    NEXT_F(f.T_rise);
    NEXT_F(f.current);
    NEXT_F(f.acoustic_rms);
    NEXT_F(f.acoustic_kHz);
    NEXT_F(f.pressure);
    NEXT_F(f.impact_force);
    NEXT_F(f.dVdt);
    NEXT_F(f.dTdt);
    NEXT_F(f.dPdt);
    NEXT_F(f.SOC_pct);
    NEXT_I(f.ground_truth);
    NEXT_U(f.ts_ms);

    // Field 19 (gt_severity) is optional for backward compatibility
    tok            = strtok(nullptr, ",");
    f.gt_severity  = tok ? (float)atof(tok) : 0.0f;

#undef NEXT_F
#undef NEXT_I
#undef NEXT_U

    // Sanity gates — reject physically impossible values before they reach detectors
    for (int i = 0; i < 3; i++) {
        if (f.v[i] < 1.0f || f.v[i] > 5.5f) return false;
    }
    if (f.T_surf  < -40.0f || f.T_surf  > 150.0f) return false;
    if (f.SOC_pct <   0.0f || f.SOC_pct > 105.0f) return false;

    f.valid = true;
    return true;
}


// =============================================================================
//  FreeRTOS tasks
// =============================================================================

// Serial RX scratch buffer — private to taskRX, no locking needed
static char     rxBuf[512];
static uint16_t rxIdx = 0;

/**
 * taskRX — Core 0, priority 2
 *
 * Reads bytes from USB-UART and assembles them into newline-terminated lines.
 * Dispatches:
 *   '#...' → parseMetadata() (handled inline, not queued)
 *   '$...' → parseFrame() then push Frame onto xFrameQ for taskProcess
 *
 * Runs at higher priority than taskProcess so packet assembly is never
 * delayed by detection work on the other core.
 */
void taskRX(void* pv) {
    for (;;) {
        while (Serial.available()) {
            char c = (char)Serial.read();
            if (c == '\n' || c == '\r') {
                if (rxIdx > 0) {
                    rxBuf[rxIdx] = '\0';
                    if (rxBuf[0] == '#') {
                        parseMetadata(rxBuf);
                    } else {
                        Frame f;
                        parseFrame(rxBuf, f);
                        // Drop silently if queue full — taskProcess is behind
                        xQueueSend(xFrameQ, &f, 0);
                    }
                    rxIdx = 0;
                }
            } else if (rxIdx < sizeof(rxBuf) - 1) {
                rxBuf[rxIdx++] = c;
            }
        }
        vTaskDelay(1);   // yield 1 ms so taskWiFiLog can run on Core 0
    }
}

/**
 * taskProcess — Core 1, priority 1
 *
 * Dequeues frames, runs all 10 detectors, updates hardware, and ACKs.
 * On invalid frames: still runs detect_SensorLoss and updates hardware,
 * because the timeout counter in detect_SensorLoss must tick continuously.
 */
void taskProcess(void* pv) {
    Frame f;
    for (;;) {
        if (xQueueReceive(xFrameQ, &f, portMAX_DELAY)) {
            if (f.valid) {
                // ── Joint SOC/SOH UKF (pack-average voltage + current) ──────
                static uint32_t ukf_prev_ts = 0;
                if (!g_ukf.ready()) g_ukf.begin(f.SOC_pct / 100.0f);
                float dt_s = (ukf_prev_ts != 0)
                           ? (float)(f.ts_ms - ukf_prev_ts) / 1000.0f
                           : 0.1f;
                ukf_prev_ts = f.ts_ms;
                float v_avg = (f.v[0] + f.v[1] + f.v[2]) / 3.0f;
                g_ukf.step(v_avg, f.current, dt_s);
                g_soc_est = g_ukf.socPct();
                g_soh_est = g_ukf.sohPct();

                runAllDetectors(f);
                logValidation(f, /*queue_wifi=*/true);
                Serial.printf("[UKF] soc_est=%.2f  soh_est=%.2f  soc_ds=%.2f\n",
                              g_soc_est, g_soh_est, f.SOC_pct);
                Serial.printf("ACK,%u,%lu\n", f.seq, millis());
            } else {
                // Invalid frame: minimal update to keep loss-detection alive
                detect_SensorLoss(f);
                updateContactors();
                updateLEDs();
                tickBuzzer();
                Serial.printf("NAK,%u\n", f.seq);
            }
        }
    }
}


// =============================================================================
//  OLED status display
// =============================================================================

/** Draw the current BMS state to the OLED. Cheap to call a few times a second. */
static void renderOLED() {
    if (!g_oled_ok) return;

    bool cutoff = (DS.action_tier >= TIER_OPEN_ALL) || DS.latched;

    // Highest-id active fault is the most-severe one in priority order
    const char* fname = "NONE";
    for (int8_t i = FLT_COUNT - 1; i >= 0; i--)
        if (g_faults & ((uint16_t)1u << i)) { fname = FAULT_NAMES[i]; break; }

    g_oled.clearDisplay();
    g_oled.setTextSize(1);
    g_oled.setTextColor(SSD1306_WHITE);

    g_oled.setCursor(0, 0);
    g_oled.println("TEAM IMPERIUM BMS");
    g_oled.drawFastHLine(0, 10, OLED_W, SSD1306_WHITE);

    g_oled.setCursor(0, 14);
    g_oled.printf("SOC %3.0f%%   SOH %3.0f%%", g_soc_est, g_soh_est);
    g_oled.setCursor(0, 24);
    g_oled.printf("Tier: %s", TIER_NAMES[DS.action_tier]);
    g_oled.setCursor(0, 34);
    g_oled.printf("Fault: %s", fname);
    g_oled.setCursor(0, 44);
    g_oled.printf("Main: %s", cutoff ? "CUT-OFF" : "CONNECTED");
    g_oled.setCursor(0, 54);
    g_oled.printf("Bal %d%d%d  %s",
                  DS.balance_cell[0], DS.balance_cell[1], DS.balance_cell[2],
                  DS.latched ? "LATCHED" : "");
    g_oled.display();
}


// =============================================================================
//  Manual fault injection (tactile buttons) — for a no-laptop demo
//
//  These write directly into the fault/severity state so the LEDs, buzzer and
//  OLED react immediately even with no data streaming. When monitor.py IS
//  streaming, the next frame's detectors take over; the RESET button always
//  clears everything (including a latched fault).
// =============================================================================

static void injectCritical() {
    g_faults = (uint16_t)1u << FLT_THERMAL_HOT;
    for (int i = 0; i < FLT_COUNT; i++) DS.severity[i] = 0.0f;
    DS.severity[FLT_THERMAL_HOT] = 0.90f;
    DS.system_severity = 0.90f;
    DS.action_tier     = TIER_OPEN_ALL;
    for (int i = 0; i < 3; i++) DS.balance_cell[i] = false;
    scheduleBuzzer(300, 200, 255);
    Serial.println("[BTN] inject CRITICAL fault (THERMAL_HOT) -> main cut-off");
}

static void injectBalance() {
    g_faults |= (uint16_t)1u << FLT_CELL_BALANCE;
    for (int i = 0; i < 3; i++) DS.balance_cell[i] = true;
    if (DS.action_tier < TIER_INHIBIT_CHG) {
        DS.action_tier     = TIER_INHIBIT_CHG;
        DS.system_severity = 0.35f;
    }
    scheduleBuzzer(200, 100, 2);
    Serial.println("[BTN] inject cell-balance / warning -> yellow LEDs");
}

static void resetFaults() {
    g_faults = 0;
    DS.latched         = false;
    DS.action_tier     = TIER_NORMAL;
    DS.system_severity = 0.0f;
    for (int i = 0; i < FLT_COUNT; i++) DS.severity[i] = 0.0f;
    for (int i = 0; i < 3; i++) DS.balance_cell[i] = false;
    scheduleBuzzer(150, 0, 1);
    Serial.println("[BTN] RESET -> cleared faults + latch");
}

/** Poll the three buttons (active LOW) with simple debounce; fire on press. */
static void pollButtons() {
    static const uint8_t pins[3] = { PIN_BTN_FAULT1, PIN_BTN_FAULT2, PIN_BTN_RESET };
    static bool     prev[3]      = { true, true, true };   // INPUT_PULLUP idles HIGH
    static uint32_t last[3]      = { 0, 0, 0 };
    uint32_t now = millis();
    for (int i = 0; i < 3; i++) {
        bool level = digitalRead(pins[i]);
        if (level != prev[i] && (now - last[i]) > 30) {
            last[i] = now;
            prev[i] = level;
            if (level == LOW) {                  // falling edge = button pressed
                if      (i == 0) injectCritical();
                else if (i == 1) injectBalance();
                else             resetFaults();
            }
        }
    }
}


// =============================================================================
//  setup() and loop()
// =============================================================================

void setup() {
    Serial.begin(SERIAL_BAUD);
    while (!Serial) {}   // wait for USB CDC to enumerate

    // ── GPIO: MOSFET gates / LEDs (outputs), buttons (pulled-up inputs) ────────
    const uint8_t out_pins[] = { PIN_MOS_MAIN, PIN_BAL1, PIN_BAL2, PIN_BAL3,
                                 PIN_LED_GREEN, PIN_BUZZER };
    for (uint8_t p : out_pins) { pinMode(p, OUTPUT); digitalWrite(p, LOW); }
    pinMode(PIN_BTN_FAULT1, INPUT_PULLUP);
    pinMode(PIN_BTN_FAULT2, INPUT_PULLUP);
    pinMode(PIN_BTN_RESET,  INPUT_PULLUP);

    // Boot healthy: battery connected, green ON.
    digitalWrite(PIN_LED_GREEN, HIGH);

    // ── OLED (I2C on GPIO21/22) ────────────────────────────────────────────────
    Wire.begin(PIN_OLED_SDA, PIN_OLED_SCL);
    g_oled_ok = g_oled.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR);
    if (g_oled_ok) {
        g_oled.clearDisplay();
        g_oled.setTextSize(1);
        g_oled.setTextColor(SSD1306_WHITE);
        g_oled.setCursor(0, 0);
        g_oled.println("TEAM IMPERIUM BMS");
        g_oled.println("booting...");
        g_oled.display();
    } else {
        Serial.println("[BMS] WARN: OLED not found at 0x3C — running headless.");
    }

    // ── Active buzzer: two short beeps = boot OK ───────────────────────────────
    scheduleBuzzer(100, 100, 2);

    // ── Detector state ────────────────────────────────────────────────────────
    memset(&DS, 0, sizeof(DS));
    DS.last_frame_local_ms = millis();   // initialise timeout timer

    // ── FreeRTOS queues ───────────────────────────────────────────────────────
    xFrameQ   = xQueueCreate(8,  sizeof(Frame));
    xWifiLogQ = xQueueCreate(16, sizeof(WifiLogEvent));
    configASSERT(xFrameQ   != nullptr);
    configASSERT(xWifiLogQ != nullptr);

    // ── FreeRTOS tasks ────────────────────────────────────────────────────────
    //  Core 0: taskRX (priority 2) + taskWiFiLog (priority 1)
    //  Core 1: taskProcess (priority 1) — dedicated to detection work
    xTaskCreatePinnedToCore(taskRX,      "RX",      4096, nullptr, 2, nullptr, 0);
    xTaskCreatePinnedToCore(taskProcess, "PROC",    8192, nullptr, 1, nullptr, 1);
    xTaskCreatePinnedToCore(taskWiFiLog, "WIFILOG", 4096, nullptr, 1, nullptr, 0);

    // ── Boot messages ─────────────────────────────────────────────────────────
    Serial.println("[BMS] Team Imperium ESP32 BMS — ready.");
    Serial.printf( "[BMS] GPIO: MAIN=%d  BAL=%d/%d/%d  GREEN=%d  BUZ=%d  "
                   "BTN=%d/%d/%d  OLED=SDA%d/SCL%d\n",
                   PIN_MOS_MAIN, PIN_BAL1, PIN_BAL2, PIN_BAL3,
                   PIN_LED_GREEN, PIN_BUZZER,
                   PIN_BTN_FAULT1, PIN_BTN_FAULT2, PIN_BTN_RESET,
                   PIN_OLED_SDA, PIN_OLED_SCL);
    Serial.printf( "[BMS] Severity tiers: INHIBIT>=%.2f  OPEN_ALL>=%.2f  LATCH>=%.2f\n",
                   SEV_INHIBIT_LO, SEV_OPEN_ALL_LO, SEV_LATCH_LO);
    Serial.println("[BMS] Buttons: 18=inject critical  19=inject balance  5=reset");
    Serial.println("[BMS] Waiting for monitor.py (or use buttons for a standalone demo).");
}

void loop() {
    // Detection runs in taskProcess (driven by serial frames). loop() owns the
    // hardware refresh so the board reacts even with no data streaming: it polls
    // the buttons, advances the buzzer, drives the outputs from the current
    // state, and repaints the OLED a few times a second.
    pollButtons();
    tickBuzzer();
    updateContactors();
    updateLEDs();

    static uint32_t last_oled = 0;
    if (millis() - last_oled >= 200) {
        last_oled = millis();
        renderOLED();
    }
    delay(5);
}
