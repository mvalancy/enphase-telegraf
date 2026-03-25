#!/usr/bin/env python3
"""Interactive CLI for downloading Enphase history and loading into InfluxDB.

This is the backend for bin/load-history. It:
  1. Downloads historical data from Enlighten (day by day)
  2. Converts to InfluxDB line protocol (same format as live data)
  3. Writes to InfluxDB or stdout

The download is resumable — it caches each day as a JSON file and skips
days that are already cached.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from enphase_cloud.enlighten import EnlightenClient
from enphase_cloud.history import HistoryCloner
from enphase_cloud.history_loader import convert_all, convert_day, write_to_influxdb

# ── Colors ────────────────────────────────────────
if sys.stderr.isatty():
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    RESET = "\033[0m"
    RED   = "\033[31m"
    GREEN = "\033[32m"
    YELLOW= "\033[33m"
    CYAN  = "\033[36m"
    WHITE = "\033[37m"
else:
    BOLD = DIM = RESET = RED = GREEN = YELLOW = CYAN = WHITE = ""

def ok(msg):    print(f"  {GREEN}+{RESET} {msg}", file=sys.stderr)
def warn(msg):  print(f"  {YELLOW}!{RESET} {YELLOW}{msg}{RESET}", file=sys.stderr)
def fail(msg):  print(f"  {RED}x{RESET} {RED}{msg}{RESET}", file=sys.stderr)
def dim(msg):   print(f"  {DIM}. {msg}{RESET}", file=sys.stderr)
def step(msg):  print(f"\n{CYAN}{BOLD}--- {msg} ---{RESET}", file=sys.stderr)
def info(msg):  print(f"  {msg}", file=sys.stderr)


def progress_bar(current, total, width=30, label=""):
    """Render a progress bar to stderr."""
    if total == 0:
        return
    pct = current / total
    filled = int(width * pct)
    bar = f"{'#' * filled}{'-' * (width - filled)}"
    sys.stderr.write(f"\r  {DIM}[{RESET}{GREEN}{bar}{RESET}{DIM}]{RESET} {current}/{total} {DIM}{label}{RESET}  ")
    sys.stderr.flush()
    if current >= total:
        sys.stderr.write("\n")


def banner():
    print(file=sys.stderr)
    print(f"{YELLOW}{BOLD}", file=sys.stderr, end="")
    print(r"    _  _ _ ____ ___ ____ ____ _   _", file=sys.stderr)
    print(r"    |__| | [__   |  |  | |__/  \_/", file=sys.stderr)
    print(r"    |  | | ___]  |  |__| |  \   |", file=sys.stderr)
    print(f"{RESET}", file=sys.stderr)
    print(f"  {BOLD}{WHITE}  enphase-telegraf  {DIM}history loader{RESET}", file=sys.stderr)
    print(f"  {DIM}  Backfill InfluxDB with historical solar data{RESET}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  {DIM}{'─' * 42}{RESET}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        prog="load-history",
        description="Download Enphase history and load into InfluxDB",
    )
    parser.add_argument("--start", metavar="YYYY-MM-DD",
                        help="Start date (default: auto-detect install date)")
    parser.add_argument("--end", metavar="YYYY-MM-DD",
                        help="End date (default: yesterday)")
    parser.add_argument("--stdout", action="store_true",
                        help="Output line protocol to stdout instead of writing to InfluxDB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download history but don't write to InfluxDB")
    parser.add_argument("--cache-dir", metavar="DIR",
                        help="Cache directory for downloaded JSON (default: .cache/)")
    parser.add_argument("--delay", type=float, default=30.0,
                        help="Seconds between API requests (default: 30)")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Lines per InfluxDB write batch (default: 5000)")
    parser.add_argument("--convert-only", action="store_true",
                        help="Skip download, only convert cached files to InfluxDB")
    args = parser.parse_args()

    # Suppress library logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="[load-history] %(levelname)s: %(message)s")

    if not args.stdout:
        banner()

    # ── Credentials ─────────────────────────────────
    email = os.environ.get("ENPHASE_EMAIL", "")
    password = os.environ.get("ENPHASE_PASSWORD", "")

    if not email or not password:
        fail("ENPHASE_EMAIL and ENPHASE_PASSWORD not set")
        info(f"  {DIM}Run ./setup.sh or source .env first{RESET}")
        sys.exit(1)

    # ── InfluxDB config (for non-stdout mode) ──────
    influx_url = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
    influx_token = os.environ.get("INFLUXDB_TOKEN") or os.environ.get("INFLUX_TOKEN", "")
    influx_org = os.environ.get("INFLUXDB_ORG", "enphase")
    influx_bucket = os.environ.get("INFLUXDB_BUCKET", "enphase")

    # Also try ~/monitoring-credentials.txt
    creds_file = Path.home() / "monitoring-credentials.txt"
    if not influx_token and creds_file.exists():
        try:
            text = creds_file.read_text()
            for line in text.splitlines():
                line = line.strip()
                # The token is on the line after "Telegraf Token" header
                # But it's also usable from the Admin Token line
            # Try to find the admin token (all-access)
            lines_list = text.splitlines()
            for i, line in enumerate(lines_list):
                if "Admin API Token" in line and i + 1 < len(lines_list):
                    candidate = lines_list[i + 1].strip()
                    if candidate and not candidate.startswith("---"):
                        influx_token = candidate
                        break
        except Exception:
            pass

    if not args.stdout and not args.dry_run and not influx_token:
        fail("No InfluxDB token found")
        info(f"  {DIM}Set INFLUXDB_TOKEN or INFLUX_TOKEN env var,")
        info(f"  or ensure ~/monitoring-credentials.txt exists{RESET}")
        sys.exit(1)

    cache_dir = Path(args.cache_dir) if args.cache_dir else Path(".cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Login ──────────────────────────────────────
    if not args.stdout:
        step("Connecting to Enphase")
    dim(f"Logging in as {email}")

    client = EnlightenClient(email, password)
    try:
        client.login()
    except Exception as e:
        fail(f"Login failed: {e}")
        sys.exit(1)

    ok(f"Logged in (site {client._session.site_id})")

    # ── Discover serial ────────────────────────────
    serial = ""
    try:
        devices = client.get_devices()
        if isinstance(devices, dict):
            for group in devices.get("result", []):
                if isinstance(group, dict) and group.get("type") in ("envoy", "gateway"):
                    for d in group.get("devices", []):
                        serial = d.get("serial_number") or d.get("serial_num") or ""
                        if serial:
                            break
                if serial:
                    break
    except Exception:
        pass

    if not serial:
        serial = os.environ.get("ENPHASE_SERIAL", "unknown")
        warn(f"Could not discover serial, using: {serial}")
    else:
        ok(f"Gateway serial: {serial}")

    # ── Download phase ─────────────────────────────
    if not args.convert_only:
        if not args.stdout:
            step("Downloading history")

        cloner = HistoryCloner(client, cache_dir, client._session.site_id)

        start_date = args.start
        if not start_date:
            # Show that we're detecting the start date
            dim("Detecting system install date...")

        if not args.stdout:
            info("")
            dim(f"Rate: 1 request every {args.delay}s (respectful of Enphase servers)")
            dim(f"Cache: {cache_dir / 'history'}/")
            dim("Resume-safe: restart anytime, already-fetched days are skipped")
            info("")

        # Run the cloner (blocking — shows progress)
        total_before = len(list((cache_dir / "history").glob("day_*.json")))

        def download_progress():
            """Monitor download progress in a non-blocking way."""
            while cloner._running:
                status = cloner.status
                if status["days_total"] > 0:
                    progress_bar(
                        status["days_completed"],
                        status["days_total"],
                        label=status.get("current_date", ""),
                    )
                time.sleep(2)

        import threading
        if not args.stdout:
            monitor = threading.Thread(target=download_progress, daemon=True)
            monitor.start()

        cloner.run(start_date=start_date, request_delay=args.delay)

        total_after = len(list((cache_dir / "history").glob("day_*.json")))
        new_days = total_after - total_before

        if not args.stdout:
            info("")
            status = cloner.status
            if status["state"] == "complete":
                ok(f"Download complete: {status['days_completed']} days ({total_after} cached)")
            elif status["state"] == "error":
                warn(f"Download stopped with errors: {status.get('last_error', 'unknown')}")
                warn(f"  {status['days_completed']}/{status['days_total']} days completed")
            if new_days > 0:
                ok(f"New days downloaded: {new_days}")
            elif total_after > 0:
                ok(f"All {total_after} days already cached")

            if status.get("errors", 0) > 0:
                warn(f"Errors: {status['errors']}")

    # ── Convert phase ──────────────────────────────
    history_dir = cache_dir / "history"
    day_files = sorted(history_dir.glob("day_*.json"))

    if not day_files:
        if not args.stdout:
            warn("No history files found to convert")
        sys.exit(0)

    if not args.stdout:
        step("Converting to line protocol")
        dim(f"Processing {len(day_files)} days...")

    def convert_progress(day_str, lines_count, total_files, current_file):
        if not args.stdout:
            progress_bar(current_file, total_files, label=day_str)

    all_lines = convert_all(history_dir, serial, progress_cb=convert_progress)

    if not args.stdout:
        info("")
        ok(f"Generated {len(all_lines):,} line protocol points")

    if not all_lines:
        if not args.stdout:
            warn("No data points generated")
        sys.exit(0)

    # ── Output phase ───────────────────────────────
    if args.stdout:
        # Write to stdout
        for line in all_lines:
            print(line)

    elif args.dry_run:
        step("Dry run — not writing to InfluxDB")
        ok(f"Would write {len(all_lines):,} points to {influx_url}")
        ok(f"  org={influx_org}  bucket={influx_bucket}")
        info("")
        info(f"  {BOLD}Preview (first 5 lines):{RESET}")
        for line in all_lines[:5]:
            info(f"  {DIM}{line}{RESET}")
        if len(all_lines) > 5:
            info(f"  {DIM}... and {len(all_lines) - 5:,} more{RESET}")

    else:
        step("Writing to InfluxDB")
        dim(f"Target: {influx_url}")
        dim(f"Org: {influx_org}  Bucket: {influx_bucket}")
        dim(f"Batch size: {args.batch_size}")
        info("")

        def write_progress(written, total):
            progress_bar(written, total, label="points written")

        written = write_to_influxdb(
            all_lines,
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            batch_size=args.batch_size,
            progress_cb=write_progress,
        )

        info("")
        if written == len(all_lines):
            ok(f"Successfully wrote {written:,} points to InfluxDB")
        elif written > 0:
            warn(f"Wrote {written:,}/{len(all_lines):,} points (some failed)")
        else:
            fail(f"Failed to write any points to InfluxDB")
            sys.exit(1)

    # ── Done ───────────────────────────────────────
    if not args.stdout:
        print(file=sys.stderr)
        print(f"  {GREEN}{BOLD}", file=sys.stderr, end="")
        print(r"    _  _ _ ____ ___ ____ ____ _   _", file=sys.stderr)
        print(r"    |__| | [__   |  |  | |__/  \_/     Done!", file=sys.stderr)
        print(r"    |  | | ___]  |  |__| |  \   |", file=sys.stderr)
        print(f"{RESET}", file=sys.stderr)
        print(f"  {DIM}{'─' * 42}{RESET}", file=sys.stderr)
        print(file=sys.stderr)

        date_range = ""
        if day_files:
            first = day_files[0].stem.replace("day_", "")
            last = day_files[-1].stem.replace("day_", "")
            date_range = f"{first} to {last}"

        if date_range:
            info(f"  {BOLD}Date range{RESET}    {date_range}")
        info(f"  {BOLD}Days{RESET}          {len(day_files)}")
        info(f"  {BOLD}Data points{RESET}   {len(all_lines):,}")
        info(f"  {BOLD}Gateway{RESET}       {serial}")
        info(f"  {BOLD}Cache{RESET}         {history_dir}/")
        if not args.dry_run and not args.stdout:
            info(f"  {BOLD}InfluxDB{RESET}      {influx_url} ({influx_org}/{influx_bucket})")
        print(file=sys.stderr)
        info(f"  {DIM}Data uses source=history tag to distinguish from live data.{RESET}")
        info(f"  {DIM}Query both: WHERE source =~ /mqtt|cloud|history/{RESET}")
        print(file=sys.stderr)


if __name__ == "__main__":
    main()
