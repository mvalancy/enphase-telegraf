#!/usr/bin/env python3
"""Scrape all 20 Enlighten cloud endpoints and dump to JSON files.

Usage:
    export ENPHASE_EMAIL=you@example.com
    export ENPHASE_PASSWORD=yourpassword
    python3 examples/cloud_scrape.py
    # Creates output/ directory with one JSON file per endpoint
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from enphase_cloud.enlighten import EnlightenClient


def main():
    email = os.environ.get("ENPHASE_EMAIL", "")
    password = os.environ.get("ENPHASE_PASSWORD", "")
    if not email or not password:
        print("Set ENPHASE_EMAIL and ENPHASE_PASSWORD env vars")
        sys.exit(1)

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    print(f"Logging in as {email}...")
    client = EnlightenClient(email, password)
    client.login()
    print(f"Site ID: {client._session.site_id}\n")

    print("Scraping all endpoints...\n")
    result = client.scrape_all()

    for key, value in result.items():
        if key in ("scraped_at", "site_id"):
            continue
        fp = output_dir / f"{key}.json"
        fp.write_text(json.dumps(value, indent=2))
        size = fp.stat().st_size
        has_error = isinstance(value, dict) and "_error" in value
        status = "ERROR" if has_error else "OK"
        print(f"  {key:30s}  {size:>8,} bytes  {status}")

    print(f"\nDone — {len(result) - 2} endpoints scraped to {output_dir}/")


if __name__ == "__main__":
    main()
