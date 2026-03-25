"""Convert cached history JSON (from HistoryCloner) into InfluxDB line protocol.

Reads day_*.json files produced by history.py and outputs the same measurement
names, tags, and field names as enphase_telegraf.py — so historical and live
data are seamlessly queryable together.

Can output to:
  - stdout (for piping to `influx write` or Telegraf)
  - InfluxDB directly via the v2 write API

The today.json response has this structure:
  {
    "stats": [
      {
        "totals": { "production": 18238.0, "consumption": 10230.0, ... },
        "intervals": [
          { "end_at": 1711270800, "production": 0.0, "consumption": 100.0, ... },
          ...  (96 intervals per day at 15-min resolution)
        ]
      }
    ],
    "battery_details": { "aggregate_soc": 85, "estimated_time": 420, ... },
    "batteryConfig": { "battery_backup_percentage": 20, ... },
    ...
  }
"""

import json
import math
import sys
import time
from datetime import date, timedelta
from pathlib import Path


# ── Line protocol formatting (matches enphase_telegraf.py exactly) ─────

def _esc_tag(s: str) -> str:
    """Escape a tag key or value for InfluxDB line protocol."""
    return (s
            .replace("\\", "\\\\")
            .replace(" ", "\\ ")
            .replace(",", "\\,")
            .replace("=", "\\=")
            .replace("\n", "")
            .replace("\r", ""))

def _esc_field_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")

def format_line(measurement: str, tags: dict, fields: dict, ts_ns: int) -> str | None:
    """Format a single InfluxDB line protocol line. Returns None if no fields."""
    if not fields:
        return None

    tag_str = ""
    for k, v in sorted(tags.items()):
        if v is not None and v != "":
            tag_str += f",{_esc_tag(k)}={_esc_tag(str(v))}"

    parts = []
    for k, v in sorted(fields.items()):
        if v is None:
            continue
        if isinstance(v, bool):
            parts.append(f"{k}={1 if v else 0}i")
            parts.append(f'{k}_str="{"true" if v else "false"}"')
        elif isinstance(v, int):
            parts.append(f"{k}={v}i")
        elif isinstance(v, float):
            if math.isfinite(v):
                parts.append(f"{k}={v}")
            # Skip NaN and Infinity — InfluxDB rejects them
        elif isinstance(v, str):
            parts.append(f'{k}="{_esc_field_str(v)}"')

    if not parts:
        return None

    return f"{measurement}{tag_str} {','.join(parts)} {ts_ns}"


# ── Field mappings (same as enphase_telegraf.py today endpoint) ────────

# Interval fields: today.json stats[0].intervals[] entries
INTERVAL_ENERGY_MAP = [
    ("production",     "production_w"),
    ("consumption",    "consumption_w"),
]

INTERVAL_FLOW_MAP = [
    ("solar_home",     "solar_to_home_w"),
    ("solar_battery",  "solar_to_battery_w"),
    ("solar_grid",     "solar_to_grid_w"),
    ("battery_home",   "battery_to_home_w"),
    ("battery_grid",   "battery_to_grid_w"),
    ("grid_home",      "grid_to_home_w"),
    ("grid_battery",   "grid_to_battery_w"),
]

# Daily totals: today.json stats[0].totals
TOTALS_MAP = [
    ("production",     "production_wh"),
    ("consumption",    "consumption_wh"),
    ("charge",         "charge_wh"),
    ("discharge",      "discharge_wh"),
    ("solar_home",     "solar_to_home_wh"),
    ("solar_battery",  "solar_to_battery_wh"),
    ("solar_grid",     "solar_to_grid_wh"),
    ("battery_home",   "battery_to_home_wh"),
    ("battery_grid",   "battery_to_grid_wh"),
    ("grid_home",      "grid_to_home_wh"),
    ("grid_battery",   "grid_to_battery_wh"),
]


def convert_day(data: dict, serial: str) -> list[str]:
    """Convert one day's today.json response into line protocol lines.

    Returns a list of line protocol strings.
    """
    lines = []

    stats = data.get("stats", [])
    if not stats:
        return lines

    stat = stats[0] if isinstance(stats[0], dict) else {}

    # ── 1. Interval data (15-min power readings) → enphase_power ───
    intervals = stat.get("intervals", [])
    for iv in intervals:
        if not isinstance(iv, dict):
            continue

        ts = iv.get("end_at")
        if ts is None:
            continue
        ts_ns = int(ts) * 1_000_000_000

        # Power fields (intervals report average watts over 15-min)
        power_fields = {}
        for src, dst in INTERVAL_ENERGY_MAP:
            val = iv.get(src)
            if val is not None:
                power_fields[dst] = float(val)

        # Grid power: net import (positive) / export (negative)
        # today.json reports grid_import and grid_export separately
        grid_import = iv.get("grid_import") or iv.get("grid_home") or iv.get("grid")
        grid_export = iv.get("grid_export") or iv.get("solar_grid")
        if grid_import is not None and grid_export is not None:
            power_fields["grid_w"] = float(grid_import) - float(grid_export)
        elif grid_import is not None:
            power_fields["grid_w"] = float(grid_import)

        # Battery power: discharge (positive) / charge (negative)
        discharge = iv.get("discharge") or iv.get("battery_home") or iv.get("battery_grid")
        charge = iv.get("charge") or iv.get("grid_battery") or iv.get("solar_battery")
        if discharge is not None and charge is not None:
            power_fields["battery_w"] = float(discharge) - float(charge)
        elif discharge is not None:
            power_fields["battery_w"] = float(discharge)

        # SOC if present
        soc = iv.get("soc") or iv.get("battery_soc")
        if soc is not None:
            power_fields["soc"] = int(soc)

        if power_fields:
            line = format_line("enphase_power", {"serial": serial, "source": "history"}, power_fields, ts_ns)
            if line:
                lines.append(line)

        # Energy flow fields → enphase_energy (per-interval Wh)
        energy_fields = {}
        for src, dst in INTERVAL_FLOW_MAP:
            val = iv.get(src)
            if val is not None:
                energy_fields[dst] = float(val)
        # Also include production/consumption as Wh per interval
        for src, dst in [("production", "production_wh"), ("consumption", "consumption_wh")]:
            val = iv.get(src)
            if val is not None:
                energy_fields[dst] = float(val)

        if energy_fields:
            line = format_line("enphase_energy", {"serial": serial, "source": "history"}, energy_fields, ts_ns)
            if line:
                lines.append(line)

    # ── 2. Daily totals → enphase_energy ───────────────────────────
    totals = stat.get("totals", {})
    if isinstance(totals, dict) and totals:
        # Use end of day as timestamp (23:59:59 of the cloned date)
        day_str = data.get("_cloned_date")
        if day_str:
            try:
                day = date.fromisoformat(day_str)
                # End of day: next day midnight minus 1 second
                eod = int(time.mktime(day.timetuple())) + 86400 - 1
                ts_ns = eod * 1_000_000_000
            except Exception:
                ts_ns = int(time.time() * 1_000_000_000)
        else:
            ts_ns = int(time.time() * 1_000_000_000)

        fields = {}
        for src, dst in TOTALS_MAP:
            val = totals.get(src)
            if val is not None:
                fields[dst] = float(val)

        if fields:
            line = format_line("enphase_energy", {"serial": serial, "source": "history_daily"}, fields, ts_ns)
            if line:
                lines.append(line)

    # ── 3. Battery details → enphase_battery ──────────────────────
    bd = data.get("battery_details", {})
    if isinstance(bd, dict) and bd:
        day_str = data.get("_cloned_date")
        if day_str:
            try:
                day = date.fromisoformat(day_str)
                eod = int(time.mktime(day.timetuple())) + 86400 - 1
                ts_ns = eod * 1_000_000_000
            except Exception:
                ts_ns = int(time.time() * 1_000_000_000)
        else:
            ts_ns = int(time.time() * 1_000_000_000)

        bat_fields = {}
        for src, dst in [("aggregate_soc", "soc"),
                         ("estimated_time", "estimated_backup_min"),
                         ("last_24h_consumption", "last_24h_consumption_kwh")]:
            val = bd.get(src)
            if val is not None:
                bat_fields[dst] = float(val) if "kwh" in dst else int(val)

        if bat_fields:
            line = format_line("enphase_battery", {"serial": serial, "source": "history"}, bat_fields, ts_ns)
            if line:
                lines.append(line)

    return lines


def convert_all(history_dir: Path, serial: str, progress_cb=None) -> list[str]:
    """Convert all cached day_*.json files into line protocol.

    Args:
        history_dir: Directory containing day_YYYY-MM-DD.json files
        serial: Gateway serial number (used as tag)
        progress_cb: Optional callback(day_str, lines_count, total_files, current_file)

    Returns:
        List of all line protocol strings, sorted by timestamp.
    """
    day_files = sorted(history_dir.glob("day_*.json"))
    all_lines = []

    for i, fp in enumerate(day_files):
        try:
            data = json.loads(fp.read_text())
            day_lines = convert_day(data, serial)
            all_lines.extend(day_lines)

            if progress_cb:
                day_str = data.get("_cloned_date", fp.stem.replace("day_", ""))
                progress_cb(day_str, len(day_lines), len(day_files), i + 1)
        except Exception as e:
            print(f"[load-history] WARNING: {fp.name}: {e}", file=sys.stderr)

    return all_lines


def write_to_influxdb(lines: list[str], url: str, token: str, org: str, bucket: str,
                      batch_size: int = 5000, progress_cb=None) -> int:
    """Write line protocol lines directly to InfluxDB v2 API.

    Args:
        lines: List of line protocol strings
        url: InfluxDB URL (e.g., http://localhost:8086)
        token: InfluxDB API token
        org: InfluxDB org name
        bucket: InfluxDB bucket name
        batch_size: Lines per API call
        progress_cb: Optional callback(lines_written, total_lines)

    Returns:
        Number of lines successfully written.
    """
    import urllib.request
    import urllib.error

    total = len(lines)
    written = 0
    errors = 0

    write_url = f"{url.rstrip('/')}/api/v2/write?org={org}&bucket={bucket}&precision=ns"

    for i in range(0, total, batch_size):
        batch = lines[i:i + batch_size]
        body = "\n".join(batch).encode("utf-8")

        req = urllib.request.Request(
            write_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Token {token}",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:
                    written += len(batch)
                else:
                    errors += len(batch)
                    print(f"[load-history] WARNING: InfluxDB returned {resp.status}", file=sys.stderr)
        except urllib.error.HTTPError as e:
            errors += len(batch)
            body_text = e.read().decode("utf-8", errors="replace")[:200]
            print(f"[load-history] ERROR: InfluxDB {e.code}: {body_text}", file=sys.stderr)
        except Exception as e:
            errors += len(batch)
            print(f"[load-history] ERROR: {e}", file=sys.stderr)

        if progress_cb:
            progress_cb(written, total)

    return written
