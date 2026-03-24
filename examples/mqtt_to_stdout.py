#!/usr/bin/env python3
"""Stream live Enphase power data to stdout.

The simplest possible example — prints one line per second with
solar, grid, home, battery power and SOC.

Usage:
    export ENPHASE_EMAIL=you@example.com
    export ENPHASE_PASSWORD=yourpassword
    python3 examples/mqtt_to_stdout.py

Or with arguments:
    python3 examples/mqtt_to_stdout.py --email you@example.com --password yourpassword
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from enphase_cloud.enlighten import EnlightenClient
from enphase_cloud.livestream import LiveStreamClient


def main():
    parser = argparse.ArgumentParser(description="Stream Enphase live power data")
    parser.add_argument("--email", default=os.environ.get("ENPHASE_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("ENPHASE_PASSWORD", ""))
    parser.add_argument("--serial", default=os.environ.get("ENPHASE_SERIAL", ""),
                        help="Gateway serial (auto-discovered if not set)")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("Set ENPHASE_EMAIL and ENPHASE_PASSWORD env vars, or use --email/--password")
        sys.exit(1)

    # Login
    print(f"Logging in as {args.email}...")
    client = EnlightenClient(args.email, args.password)
    client.login()
    print(f"Logged in — site_id={client._session.site_id}")

    # Discover serial if not provided
    serial = args.serial
    if not serial:
        print("Discovering gateway serial from cloud...")
        devices = client.get_devices()
        # Look for envoy in device list
        if isinstance(devices, dict):
            for dtype, devs in devices.items():
                if isinstance(devs, list):
                    for d in devs:
                        if isinstance(d, dict) and (d.get("device_type") == "envoy" or dtype == "envoys"):
                            serial = d.get("serial_num") or d.get("serial_number") or d.get("sn")
                            if serial:
                                break
                if serial:
                    break
        if not serial:
            print("Could not discover serial. Pass --serial explicitly.")
            sys.exit(1)
        print(f"Discovered serial: {serial}")

    # Stream
    print(f"\nStreaming live data for {serial}... (Ctrl+C to stop)\n")
    print(f"{'Time':>10}  {'Solar':>8}  {'Grid':>8}  {'Home':>8}  {'Battery':>8}  {'SOC':>5}")
    print("-" * 60)

    def on_data(msg):
        t = time.strftime("%H:%M:%S")
        pv = msg.get("pv_power_w", 0)
        grid = msg.get("grid_power_w", 0)
        load = msg.get("load_power_w", 0)
        batt = msg.get("storage_power_w", 0)
        soc = msg.get("soc", 0)
        print(f"{t:>10}  {pv:>7.0f}W  {grid:>7.0f}W  {load:>7.0f}W  {batt:>7.0f}W  {soc:>4}%")

    def on_status(msg):
        print(f"  [{msg}]")

    stream = LiveStreamClient(client)
    try:
        stream.start(serial, on_data=on_data, on_status=on_status)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        stream.stop()


if __name__ == "__main__":
    main()
