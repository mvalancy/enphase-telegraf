#!/usr/bin/env python3
"""Control Enphase battery via the Enlighten cloud API.

Usage:
    export ENPHASE_EMAIL=you@example.com
    export ENPHASE_PASSWORD=yourpassword

    # Set battery mode
    python3 examples/battery_control.py mode self-consumption
    python3 examples/battery_control.py mode savings
    python3 examples/battery_control.py mode backup

    # Set backup reserve percentage
    python3 examples/battery_control.py reserve 20

    # Toggle charge from grid
    python3 examples/battery_control.py charge-from-grid on
    python3 examples/battery_control.py charge-from-grid off

    # Toggle storm guard
    python3 examples/battery_control.py storm-guard on
    python3 examples/battery_control.py storm-guard off

    # Show current battery status
    python3 examples/battery_control.py status
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from enphase_cloud.enlighten import EnlightenClient


def main():
    email = os.environ.get("ENPHASE_EMAIL", "")
    password = os.environ.get("ENPHASE_PASSWORD", "")
    if not email or not password:
        print("Set ENPHASE_EMAIL and ENPHASE_PASSWORD env vars")
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    client = EnlightenClient(email, password)
    client.login()
    print(f"Logged in — site_id={client._session.site_id}")

    if command == "status":
        status = client.get_battery_status()
        settings = client.get_battery_settings()
        print("\nBattery Status:")
        print(json.dumps(status, indent=2))
        print("\nBattery Settings:")
        print(json.dumps(settings, indent=2))

    elif command == "mode":
        if len(sys.argv) < 3:
            print("Usage: battery_control.py mode <self-consumption|savings|backup|economy>")
            sys.exit(1)
        mode = sys.argv[2]
        result = client.set_battery_mode(mode)
        print(f"Battery mode set to: {mode}")
        print(json.dumps(result, indent=2))

    elif command == "reserve":
        if len(sys.argv) < 3:
            print("Usage: battery_control.py reserve <0-100>")
            sys.exit(1)
        soc = int(sys.argv[2])
        result = client.set_reserve_soc(soc)
        print(f"Backup reserve set to: {soc}%")
        print(json.dumps(result, indent=2))

    elif command == "charge-from-grid":
        if len(sys.argv) < 3:
            print("Usage: battery_control.py charge-from-grid <on|off>")
            sys.exit(1)
        enabled = sys.argv[2].lower() in ("on", "true", "1", "yes")
        result = client.set_charge_from_grid(enabled)
        print(f"Charge from grid: {'enabled' if enabled else 'disabled'}")
        print(json.dumps(result, indent=2))

    elif command == "storm-guard":
        if len(sys.argv) < 3:
            print("Usage: battery_control.py storm-guard <on|off>")
            sys.exit(1)
        enabled = sys.argv[2].lower() in ("on", "true", "1", "yes")
        result = client.set_storm_guard(enabled)
        print(f"Storm guard: {'enabled' if enabled else 'disabled'}")
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
