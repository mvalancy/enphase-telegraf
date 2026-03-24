#!/usr/bin/env python3
"""Telegraf-style collector: Enphase MQTT live stream → InfluxDB.

Connects to the Enphase MQTT live stream and writes power data to your
own InfluxDB instance. No Docker needed — just this script + 4 pip packages.

Usage:
    pip install requests paho-mqtt protobuf influxdb-client

    export ENPHASE_EMAIL=you@example.com
    export ENPHASE_PASSWORD=yourpassword
    export INFLUXDB_URL=http://localhost:8086
    export INFLUXDB_TOKEN=your-token
    export INFLUXDB_ORG=your-org
    export INFLUXDB_BUCKET=enphase

    python3 examples/mqtt_to_influxdb.py

Data is written as InfluxDB line protocol:
    enphase_power,source=mqtt pv_power_w=3200.0,grid_power_w=-1500.0,...
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
    parser = argparse.ArgumentParser(description="Enphase MQTT → InfluxDB")
    parser.add_argument("--email", default=os.environ.get("ENPHASE_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("ENPHASE_PASSWORD", ""))
    parser.add_argument("--serial", default=os.environ.get("ENPHASE_SERIAL", ""))
    parser.add_argument("--influx-url", default=os.environ.get("INFLUXDB_URL", "http://localhost:8086"))
    parser.add_argument("--influx-token", default=os.environ.get("INFLUXDB_TOKEN", ""))
    parser.add_argument("--influx-org", default=os.environ.get("INFLUXDB_ORG", ""))
    parser.add_argument("--influx-bucket", default=os.environ.get("INFLUXDB_BUCKET", "enphase"))
    parser.add_argument("--measurement", default="enphase_power")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("Set ENPHASE_EMAIL and ENPHASE_PASSWORD")
        sys.exit(1)

    # InfluxDB setup
    try:
        from influxdb_client import InfluxDBClient, Point, WritePrecision
        from influxdb_client.client.write_api import SYNCHRONOUS
    except ImportError:
        print("Install influxdb-client: pip install influxdb-client")
        sys.exit(1)

    influx = InfluxDBClient(url=args.influx_url, token=args.influx_token, org=args.influx_org)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    # Verify connection
    try:
        health = influx.health()
        print(f"InfluxDB: {health.status} at {args.influx_url}")
    except Exception as e:
        print(f"InfluxDB connection failed: {e}")
        sys.exit(1)

    # Enlighten login
    print(f"Logging in as {args.email}...")
    client = EnlightenClient(args.email, args.password)
    client.login()
    print(f"Logged in — site_id={client._session.site_id}")

    # Discover serial
    serial = args.serial
    if not serial:
        print("Discovering gateway serial...")
        devices = client.get_devices()
        if isinstance(devices, dict):
            for dtype, devs in devices.items():
                if isinstance(devs, list):
                    for d in devs:
                        if isinstance(d, dict) and (d.get("device_type") == "envoy" or dtype == "envoys"):
                            serial = d.get("serial_num") or d.get("serial_number")
                            if serial:
                                break
                if serial:
                    break
        if not serial:
            print("Could not discover serial. Pass --serial explicitly.")
            sys.exit(1)
        print(f"Serial: {serial}")

    # Stream → InfluxDB
    msg_count = 0
    write_errors = 0

    def on_data(msg):
        nonlocal msg_count, write_errors
        try:
            point = Point(args.measurement).tag("source", "mqtt").tag("serial", serial)
            for field in ("pv_power_w", "grid_power_w", "load_power_w",
                          "storage_power_w", "generator_power_w"):
                val = msg.get(field)
                if val is not None:
                    point = point.field(field, float(val))
            soc = msg.get("soc")
            if soc is not None:
                point = point.field("soc", float(soc))
            batt_mode = msg.get("batt_mode")
            if batt_mode:
                point = point.field("batt_mode", str(batt_mode))

            write_api.write(bucket=args.influx_bucket, record=point)
            msg_count += 1
            if msg_count % 60 == 0:
                print(f"  {msg_count} points written ({write_errors} errors)")
        except Exception as e:
            write_errors += 1
            if write_errors <= 5 or write_errors % 100 == 0:
                print(f"  Write error #{write_errors}: {e}")

    def on_status(msg):
        print(f"  [{msg}]")

    print(f"\nStreaming to InfluxDB {args.influx_url}/{args.influx_bucket}...")
    print(f"Measurement: {args.measurement}, tags: source=mqtt, serial={serial}\n")

    stream = LiveStreamClient(client)
    try:
        stream.start(serial, on_data=on_data, on_status=on_status)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\nStopping... {msg_count} points written, {write_errors} errors")
        stream.stop()
        influx.close()


if __name__ == "__main__":
    main()
