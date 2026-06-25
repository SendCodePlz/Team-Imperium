#pragma once

// =============================================================================
//  wifi_log.h  —  Team Imperium | Caterpillar Tech Challenge
//
//  WiFi initialisation and the taskWiFiLog FreeRTOS task.
//
//  This file is completely isolated from fault detection logic. It never
//  reads g_faults, DS, or any detector state directly. All data arrives
//  through xWifiLogQ, populated by logValidation() in detectors.h.
//
//  What it does:
//    1. Connects to WiFi at startup (non-blocking — falls back to no-WiFi mode).
//    2. Blocks on xWifiLogQ waiting for WifiLogEvent structs.
//    3. Builds a compact CSV payload from each event.
//    4. Sends it as a fire-and-forget UDP datagram to LOG_SERVER_IP:LOG_SERVER_PORT.
//    5. Attempts reconnection if WiFi drops before the next send.
//
//  UDP payload format (one datagram per event):
//    ts_ms, seq, v1, v2, v3, T_surf, current, SOC_pct,
//    fault_bits_hex, active_faults, est_severity, gt_severity,
//    soc_est, soh_est,
//    tier, chg_state, dsc_state, ground_truth, outcome, is_heartbeat
//
//  IMPORTANT: Include this file ONLY from BMS_ESP32_Demo.ino.
// =============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "config.h"
#include "types.h"

// Defined in BMS_ESP32_Demo.ino
extern QueueHandle_t xWifiLogQ;

// Module-private objects (one instance, lives in this translation unit)
static WiFiUDP _udp;
static bool    _wifi_ok = false;


// =============================================================================
//  WiFi connection
// =============================================================================

/**
 * Attempt to connect to WiFi. Returns true on success.
 * Blocks up to WIFI_CONNECT_TIMEOUT_MS — safe to call from a FreeRTOS task
 * because it uses vTaskDelay() rather than a busy loop.
 */
static bool initWiFi() {
    Serial.printf("[WIFI] Connecting to \"%s\" ...\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start > WIFI_CONNECT_TIMEOUT_MS) {
            Serial.println("[WIFI] Timeout — WiFi logging disabled for this session.");
            WiFi.disconnect(true);
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }

    Serial.printf("[WIFI] Connected. IP: %s  →  logging to %s:%d\n",
                  WiFi.localIP().toString().c_str(),
                  LOG_SERVER_IP, LOG_SERVER_PORT);
    _udp.begin(LOG_SERVER_PORT);   // bind local port (used for outgoing datagrams)
    return true;
}


// =============================================================================
//  UDP payload builder
// =============================================================================

/**
 * Build the CSV payload string for one WifiLogEvent.
 *
 * Writes into buf[buflen]. Returns the number of bytes written (not including
 * the null terminator). The payload is UTF-8 ASCII — safe to cast to uint8_t*.
 *
 * Format:
 *   ts_ms, seq,
 *   v1, v2, v3, T_surf, current, SOC_pct,
 *   fault_bits_hex, active_fault_names (pipe-separated),
 *   est_severity, gt_severity,
 *   soc_est, soh_est,                  (UKF SOC/SOH estimate, %)
 *   tier, chg_state, dsc_state,
 *   ground_truth, outcome,
 *   is_heartbeat (0/1)
 */
static int buildUdpPayload(const WifiLogEvent& ev, char* buf, size_t buflen) {
    // Build active fault name string
    char names[80];
    names[0] = '\0';
    bool first = true;
    for (uint8_t i = 0; i < FLT_COUNT; i++) {
        if (ev.fault_bits & ((uint16_t)1u << i)) {
            if (!first) strncat(names, "|", sizeof(names) - strlen(names) - 1);
            strncat(names, FAULT_NAMES[i], sizeof(names) - strlen(names) - 1);
            first = false;
        }
    }
    if (first) strncpy(names, "NONE", sizeof(names));

    return snprintf(buf, buflen,
        // timestamp and sequence
        "%lu,%u,"
        // sensor snapshot
        "%.4f,%.4f,%.4f,%.2f,%.3f,%.2f,"
        // fault state
        "0x%04X,%s,%.3f,%.3f,"
        // SOC/SOH estimate (UKF)
        "%.2f,%.2f,"
        // hardware response
        "%s,%s,%s,"
        // validation
        "%d,%s,%d",
        ev.ts_ms, ev.seq,
        ev.v1, ev.v2, ev.v3, ev.T_surf, ev.current, ev.SOC_pct,
        ev.fault_bits, names, ev.est_severity, ev.gt_severity,
        ev.soc_est, ev.soh_est,
        TIER_NAMES[ev.action_tier],
        ev.chg_open ? "OPEN" : "CLOSED",
        ev.dsc_open ? "OPEN" : "CLOSED",
        ev.ground_truth, ev.outcome,
        ev.is_heartbeat ? 1 : 0
    );
}


// =============================================================================
//  FreeRTOS WiFi log task
// =============================================================================

/**
 * taskWiFiLog — Core 0, priority 1
 *
 * Lifecycle:
 *   1. On startup: attempts WiFi connection. Continues regardless of result.
 *   2. Blocks indefinitely on xWifiLogQ.
 *   3. On each event: checks WiFi status; reconnects if needed; sends UDP.
 *   4. If WiFi is unavailable, the event is silently dropped (non-blocking).
 *
 * This task never starves taskRX or taskProcess because:
 *   • It blocks on a queue (yields when no events), and
 *   • It runs on Core 0 alongside taskRX, while taskProcess has Core 1 to itself.
 */
void taskWiFiLog(void* pv) {
    _wifi_ok = initWiFi();

    WifiLogEvent ev;
    static char  payload[320];

    for (;;) {
        // Block until an event arrives from logValidation()
        if (xQueueReceive(xWifiLogQ, &ev, portMAX_DELAY)) {

            // Reconnect if WiFi dropped between events
            if (_wifi_ok && WiFi.status() != WL_CONNECTED) {
                Serial.println("[WIFI] Connection lost — attempting reconnect.");
                _wifi_ok = initWiFi();
            }

            if (_wifi_ok) {
                int len = buildUdpPayload(ev, payload, sizeof(payload));
                if (len > 0 && len < (int)sizeof(payload)) {
                    _udp.beginPacket(LOG_SERVER_IP, LOG_SERVER_PORT);
                    _udp.write((const uint8_t*)payload, (size_t)len);
                    if (!_udp.endPacket()) {
                        Serial.println("[WIFI] UDP send failed.");
                    }
                }
            }
        }
    }
}
