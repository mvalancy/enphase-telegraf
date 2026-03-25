"""Shared fixtures for enphase-telegraf test suite."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ── Real credentials (from environment or .env) ─────────────────
def _load_env():
    """Load .env file if it exists, but don't override existing env vars."""
    env_candidates = [
        Path(__file__).parent.parent / ".env",
        Path("/opt/enphase-local/.env"),
    ]
    for env_path in env_candidates:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())
            break

_load_env()

# ── Credentials fixtures ─────────────────────────────────────────

@pytest.fixture(scope="session")
def enphase_email():
    return os.environ.get("ENPHASE_EMAIL", "")

@pytest.fixture(scope="session")
def enphase_password():
    return os.environ.get("ENPHASE_PASSWORD", "")

@pytest.fixture(scope="session")
def gateway_serial():
    return os.environ.get("GATEWAY_1_SERIAL", "482525046373")

@pytest.fixture(scope="session")
def influx_url():
    """InfluxDB URL (Tailscale IP). Ignores .env Docker hostname."""
    url = os.environ.get("INFLUXDB_URL", "")
    # The .env may have a Docker hostname like http://influxdb:8086 — skip that
    if not url or "influxdb:" in url:
        # Read from monitoring-credentials.txt instead
        creds = Path.home() / "monitoring-credentials.txt"
        if creds.exists():
            for line in creds.read_text().splitlines():
                if line.strip().startswith("URL:") and "8086" in line:
                    url = line.split("URL:")[1].strip()
                    break
    if not url or "influxdb:" in url:
        url = "http://100.79.60.48:8086"
    return url

@pytest.fixture(scope="session")
def influx_admin_token():
    """Admin all-access token from monitoring-credentials.txt."""
    tok = os.environ.get("INFLUXDB_ADMIN_TOKEN", "")
    if not tok:
        creds = Path.home() / "monitoring-credentials.txt"
        if creds.exists():
            lines = creds.read_text().splitlines()
            for i, line in enumerate(lines):
                if "Admin (all-access)" in line and i + 1 < len(lines):
                    tok = lines[i + 1].strip()
                    break
    return tok

@pytest.fixture(scope="session")
def influx_energy_token():
    """Energy bucket write token."""
    tok = os.environ.get("INFLUX_ENERGY_TOKEN", "")
    if not tok:
        creds = Path.home() / "monitoring-credentials.txt"
        if creds.exists():
            lines = creds.read_text().splitlines()
            for i, line in enumerate(lines):
                if "Telegraf energy" in line and i + 1 < len(lines):
                    tok = lines[i + 1].strip()
                    break
    return tok

@pytest.fixture(scope="session")
def influx_org():
    return "Valpatel"

@pytest.fixture(scope="session")
def influx_bucket():
    return "energy"


# ── Sample data fixtures ─────────────────────────────────────────

@pytest.fixture
def sample_today_json():
    """Minimal today.json structure with realistic data."""
    base_ts = 1711270800  # 2024-03-24 05:00:00 UTC
    intervals = []
    for i in range(96):  # 96 x 15-min = 24 hours
        ts = base_ts + i * 900
        hour = (i * 15 // 60) % 24
        # Simulate solar curve: peak at noon
        solar = max(0, 3000 * (1 - ((hour - 12) / 6) ** 2)) if 6 <= hour <= 18 else 0
        consumption = 800 + 200 * (1 if 7 <= hour <= 22 else 0)
        grid = consumption - solar
        intervals.append({
            "end_at": ts,
            "production": round(solar, 1),
            "consumption": round(consumption, 1),
            "grid_home": round(max(grid, 0), 1),
            "solar_grid": round(max(-grid, 0), 1),
            "solar_home": round(min(solar, consumption), 1),
            "solar_battery": 0.0,
            "battery_home": 0.0,
            "battery_grid": 0.0,
            "grid_battery": 0.0,
        })

    return {
        "_cloned_date": "2024-03-24",
        "_cloned_at": time.time(),
        "stats": [{
            "totals": {
                "production": 18238.0,
                "consumption": 22450.0,
                "charge": 1200.0,
                "discharge": 800.0,
                "solar_home": 12500.0,
                "solar_battery": 1200.0,
                "solar_grid": 4538.0,
                "battery_home": 800.0,
                "battery_grid": 0.0,
                "grid_home": 9150.0,
                "grid_battery": 0.0,
            },
            "intervals": intervals,
        }],
        "battery_details": {
            "aggregate_soc": 85,
            "estimated_time": 420,
            "last_24h_consumption": 22.45,
        },
        "batteryConfig": {
            "battery_backup_percentage": 20,
            "very_low_soc": 5,
            "charge_from_grid": False,
            "severe_weather_watch": "disabled",
            "usage": "self-consumption",
        },
    }


@pytest.fixture
def sample_interval():
    """Single 15-min interval."""
    return {
        "end_at": 1711310400,
        "production": 2800.5,
        "consumption": 1050.3,
        "grid_home": 0.0,
        "solar_grid": 1750.2,
        "solar_home": 1050.3,
        "solar_battery": 0.0,
        "battery_home": 0.0,
        "battery_grid": 0.0,
        "grid_battery": 0.0,
    }


@pytest.fixture
def tmp_history_dir(tmp_path, sample_today_json):
    """Temp dir with a few day_*.json files."""
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    for i in range(3):
        day = f"2024-03-{24 + i:02d}"
        data = dict(sample_today_json)
        data["_cloned_date"] = day
        (history_dir / f"day_{day}.json").write_text(json.dumps(data))
    return history_dir
