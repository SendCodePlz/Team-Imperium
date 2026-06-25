#!/usr/bin/env python3
"""
csv_logger.py  —  Team Imperium  |  Caterpillar Tech Challenge
==============================================================
UDP log receiver for WiFi packets sent by the ESP32's taskWiFiLog task.

Listens on a local UDP port, writes each received packet as one CSV row,
and prints a live summary to the terminal.

USAGE
-----
    python csv_logger.py                          # defaults: port 4210, out wifi_log.csv
    python csv_logger.py --port 4210 --out wifi_log.csv

Run this in a second terminal window while bms_monitor.py runs in the first.
The ESP32 sends a packet whenever fault/contactor state changes, plus a
heartbeat every 5 seconds — so this file won't grow huge.

CSV COLUMNS
-----------
    wall_time         — PC wall-clock timestamp (ISO 8601)
    ts_ms             — dataset timestamp from ESP32
    seq               — packet sequence number
    v1, v2, v3        — cell voltages (V)
    T_surf            — surface temperature (°C)
    current           — pack current (A)
    SOC_pct           — state of charge (%)
    fault_bits        — hex fault bitmask (e.g. 0x0002)
    active_faults     — pipe-separated fault names (e.g. THERMAL_HOT|EIS_FAULT)
    est_severity      — ESP32's computed severity (0.0–1.0)
    gt_severity       — dataset ground-truth severity (0.0–1.0)
    tier              — action tier (NORMAL / INHIBIT_CHG / OPEN_ALL / LATCH_OPEN)
    chg_state         — charge MOSFET state (OPEN / CLOSED)
    dsc_state         — discharge MOSFET state (OPEN / CLOSED)
    ground_truth      — dataset fault flag (0 or 1)
    outcome           — TP / TN / FP / FN
    is_heartbeat      — 1 if periodic heartbeat, 0 if state-change event
"""

import argparse
import csv
import socket
from datetime import datetime, timezone

# Column names matching the UDP payload built by wifi_log.h → buildUdpPayload()
CSV_COLUMNS = [
    "wall_time",
    "ts_ms", "seq",
    "v1", "v2", "v3", "T_surf", "current", "SOC_pct",
    "fault_bits", "active_faults", "est_severity", "gt_severity",
    "soc_est", "soh_est",
    "tier", "chg_state", "dsc_state",
    "ground_truth", "outcome", "is_heartbeat",
]

# UDP payload field order from buildUdpPayload() in wifi_log.h
# ts_ms, seq, v1, v2, v3, T_surf, current, SOC_pct,
# fault_bits_hex, active_faults, est_severity, gt_severity,
# soc_est, soh_est,
# tier, chg_state, dsc_state, ground_truth, outcome, is_heartbeat
UDP_FIELD_COUNT = 20


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="ESP32 WiFi log UDP receiver")
    ap.add_argument("--port", type=int, default=4210,
                    help="UDP port to listen on (must match LOG_SERVER_PORT in config.h)")
    ap.add_argument("--out",  default="wifi_log.csv",
                    help="Output CSV file path")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress per-packet terminal output")
    return ap.parse_args()


def run_logger(port: int = 4210, out: str = "wifi_log.csv",
               stop_ev=None, quiet: bool = False,
               on_packet=None, ready_ev=None) -> tuple[int, int]:
    """Receive ESP32 WiFi UDP packets and write each as a CSV row.

    Runs until `stop_ev` is set (when given) or KeyboardInterrupt. Returns
    (packets_received, fault_events). Designed to be callable both as the
    standalone script's main loop and as a background thread inside monitor.py
    (pass a threading.Event as stop_ev and quiet=True to silence per-packet
    output so it doesn't clash with the live dashboard).

    `on_packet`, if given, is called with the parsed row dict for every valid
    datagram — monitor.py uses this to drive the live dashboard from the data
    the ESP32 sends back over WiFi. `ready_ev`, if given, is set the moment the
    first valid packet arrives (i.e. the ESP32 has joined WiFi) so the caller
    can wait for the link before streaming.
    """
    # Open UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", port))
    except OSError as e:
        print(f"[LOGGER] ERROR: cannot bind to port {port}: {e}")
        print("[LOGGER] Check that no other process is using that port.")
        return 0, 0

    sock.settimeout(1.0)   # 1-second timeout so stop_ev / Ctrl-C are responsive

    # Open CSV
    log_f = open(out, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(log_f, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    log_f.flush()

    print(f"[LOGGER] Listening on UDP port {port}  ->  writing {out}")
    if not quiet:
        print("[LOGGER] Ctrl-C to stop\n")
        print(f"{'WALL TIME':25}  {'ts_ms':>8}  {'FAULTS':30}  {'SEV':>5}  "
              f"{'SOC':>5}  {'SOH':>5}  {'TIER':12}  {'CHG':6}  {'DSC':6}  "
              f"{'OUT':3}  {'HB':2}")
        print("-" * 120)

    pkt_count   = 0
    fault_count = 0

    try:
        while stop_ev is None or not stop_ev.is_set():
            try:
                data, addr = sock.recvfrom(512)
            except socket.timeout:
                continue

            payload = data.decode("utf-8", errors="replace").strip()
            fields  = payload.split(",")

            # Expect exactly UDP_FIELD_COUNT comma-separated values
            if len(fields) < UDP_FIELD_COUNT:
                if not quiet:
                    print(f"[LOGGER] WARN: malformed packet "
                          f"({len(fields)} fields): {payload[:60]}")
                continue

            wall = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

            # Map fields to column names (wall_time prepended, then UDP fields in order)
            row = {
                "wall_time":    wall,
                "ts_ms":        fields[0],
                "seq":          fields[1],
                "v1":           fields[2],
                "v2":           fields[3],
                "v3":           fields[4],
                "T_surf":       fields[5],
                "current":      fields[6],
                "SOC_pct":      fields[7],
                "fault_bits":   fields[8],
                "active_faults":fields[9],
                "est_severity": fields[10],
                "gt_severity":  fields[11],
                "soc_est":      fields[12],
                "soh_est":      fields[13],
                "tier":         fields[14],
                "chg_state":    fields[15],
                "dsc_state":    fields[16],
                "ground_truth": fields[17],
                "outcome":      fields[18],
                "is_heartbeat": fields[19],
            }

            writer.writerow(row)
            log_f.flush()
            pkt_count += 1
            if row["outcome"] in ("TP", "FP"):
                fault_count += 1

            # First valid packet => the ESP32 has joined WiFi and is reachable.
            if ready_ev is not None and not ready_ev.is_set():
                ready_ev.set()
            # Hand the row to the live dashboard (monitor.py --wifi).
            if on_packet is not None:
                try:
                    on_packet(row)
                except Exception:
                    pass

            if not quiet:
                hb_marker = "HB" if fields[19] == "1" else "  "
                print(f"{wall:25}  {fields[0]:>8}  {fields[9]:30}  "
                      f"{fields[10]:>5}  {fields[12]:>5}  {fields[13]:>5}  "
                      f"{fields[14]:12}  {fields[15]:6}  {fields[16]:6}  "
                      f"{fields[18]:3}  {hb_marker}")

    except KeyboardInterrupt:
        pass
    finally:
        log_f.close()
        sock.close()

    print(f"[LOGGER] Done. {pkt_count} packets received, "
          f"{fault_count} fault events. Log: {out}")
    return pkt_count, fault_count


def main() -> None:
    args = parse_args()
    run_logger(port=args.port, out=args.out, stop_ev=None, quiet=args.quiet)


if __name__ == "__main__":
    main()
