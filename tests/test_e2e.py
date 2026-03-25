"""End-to-end tests against real Enphase cloud and InfluxDB.

These tests hit real services:
  - Enphase Enlighten API (login, fetch data)
  - InfluxDB on the local Tailscale network (write, read)
  - enphase_telegraf.py (run and capture output)

Marked with @pytest.mark.e2e — skip with: pytest -m "not e2e"
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def influx_query(url, token, org, query):
    """Run a Flux query against InfluxDB and return the raw CSV response."""
    req = urllib.request.Request(
        f"{url}/api/v2/query?org={org}",
        data=json.dumps({"query": query, "type": "flux"}).encode(),
        method="POST",
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/csv",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def influx_write(url, token, org, bucket, lines):
    """Write line protocol to InfluxDB. Returns True on success."""
    body = "\n".join(lines).encode()
    req = urllib.request.Request(
        f"{url}/api/v2/write?org={org}&bucket={bucket}&precision=ns",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status == 204


def influx_health(url):
    """Check InfluxDB health endpoint."""
    try:
        req = urllib.request.Request(f"{url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "pass"
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# InfluxDB connectivity
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestInfluxDBConnectivity:

    def test_health_endpoint(self, influx_url):
        assert influx_health(influx_url), f"InfluxDB not healthy at {influx_url}"

    def test_write_and_read_back(self, influx_url, influx_admin_token, influx_org, influx_bucket):
        """Write a test point and read it back."""
        pytest.importorskip("urllib.request")
        tag = f"pytest_{int(time.time())}"
        ts = int(time.time() * 1_000_000_000)
        line = f"enphase_test,source=pytest,tag={tag} value=42.0 {ts}"

        ok = influx_write(influx_url, influx_admin_token, influx_org, influx_bucket, [line])
        assert ok, "Write to InfluxDB failed"

        # Read it back
        time.sleep(1)  # InfluxDB needs a moment
        query = f'''
            from(bucket: "{influx_bucket}")
            |> range(start: -5m)
            |> filter(fn: (r) => r._measurement == "enphase_test")
            |> filter(fn: (r) => r.tag == "{tag}")
        '''
        csv = influx_query(influx_url, influx_admin_token, influx_org, query)
        assert "42" in csv, f"Written value not found in query result:\n{csv[:500]}"

    def test_write_batch(self, influx_url, influx_admin_token, influx_org, influx_bucket):
        """Write 100 points in a batch."""
        base_ts = int(time.time() * 1_000_000_000)
        batch_id = f"batch_{int(time.time())}"
        lines = [
            f"enphase_test,source=pytest,batch={batch_id} value={i}.0 {base_ts + i * 1000000}"
            for i in range(100)
        ]
        ok = influx_write(influx_url, influx_admin_token, influx_org, influx_bucket, lines)
        assert ok, "Batch write failed"


# ═══════════════════════════════════════════════════════════════════
# Enphase Enlighten API
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestEnphaseCloud:

    @pytest.fixture(scope="class")
    def client(self, enphase_email, enphase_password):
        if not enphase_email or not enphase_password:
            pytest.skip("No Enphase credentials")
        from enphase_cloud.enlighten import EnlightenClient
        c = EnlightenClient(enphase_email, enphase_password)
        c.login()
        return c

    def test_login_succeeds(self, client):
        assert client.authenticated
        assert client._session.site_id

    def test_get_today(self, client):
        data = client.get_today()
        assert isinstance(data, dict)
        assert "stats" in data, f"Missing 'stats' key. Keys: {list(data.keys())}"

    def test_get_today_has_stats_structure(self, client):
        """Today endpoint should have stats with totals (intervals may be absent at night)."""
        data = client.get_today()
        stats = data.get("stats", [])
        assert len(stats) > 0, f"No stats entries. Keys: {list(data.keys())}"
        stat = stats[0]
        # At minimum, totals should exist
        assert isinstance(stat, dict), f"stats[0] is not a dict: {type(stat)}"
        assert "totals" in stat, f"No totals key. Keys: {list(stat.keys())}"

    def test_get_today_has_totals(self, client):
        data = client.get_today()
        stats = data.get("stats", [])
        totals = stats[0].get("totals", {})
        assert "production" in totals, f"Missing production. Totals keys: {list(totals.keys())}"

    def test_get_battery_status(self, client):
        data = client.get_battery_status()
        assert isinstance(data, dict)

    def test_get_devices(self, client):
        data = client.get_devices()
        assert isinstance(data, dict)

    def test_get_latest_power(self, client):
        data = client.get_latest_power()
        assert isinstance(data, dict)

    def test_get_site_data(self, client):
        data = client.get_site_data()
        assert isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════════
# History conversion → InfluxDB roundtrip
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestHistoryToInfluxDB:
    """Fetch a real day's data, convert it, write to InfluxDB, read back."""

    @pytest.fixture(scope="class")
    def real_today_data(self, enphase_email, enphase_password):
        if not enphase_email or not enphase_password:
            pytest.skip("No Enphase credentials")
        from enphase_cloud.enlighten import EnlightenClient
        c = EnlightenClient(enphase_email, enphase_password)
        c.login()
        return c.get_today()

    def test_real_data_converts_without_error(self, real_today_data, gateway_serial):
        from enphase_cloud.history_loader import convert_day
        lines = convert_day(real_today_data, gateway_serial)
        assert len(lines) > 0, "No lines generated from real today data"
        # Every line should be syntactically valid
        for line in lines[:10]:
            parts = line.split(" ")
            assert len(parts) >= 3, f"Malformed line: {line}"

    def test_real_data_write_to_influxdb(self, real_today_data, gateway_serial,
                                          influx_url, influx_admin_token, influx_org, influx_bucket):
        from enphase_cloud.history_loader import convert_day
        lines = convert_day(real_today_data, gateway_serial)
        assert len(lines) > 0

        # Write a subset (first 10 lines)
        ok = influx_write(influx_url, influx_admin_token, influx_org, influx_bucket, lines[:10])
        assert ok, "Failed to write converted history to InfluxDB"

    def test_written_data_queryable(self, influx_url, influx_admin_token, influx_org, influx_bucket,
                                     gateway_serial):
        """Write known history-tagged data and read it back."""
        from enphase_cloud.history_loader import format_line
        tag_id = f"qtest_{int(time.time())}"
        ts = int(time.time() * 1_000_000_000)
        line = format_line("enphase_power", {"serial": gateway_serial, "source": "history", "qtest": tag_id},
                           {"solar_w": 1234.5}, ts)
        ok = influx_write(influx_url, influx_admin_token, influx_org, influx_bucket, [line])
        assert ok, "Failed to write test data"
        time.sleep(2)

        query = f'''
            from(bucket: "{influx_bucket}")
            |> range(start: -5m)
            |> filter(fn: (r) => r._measurement == "enphase_power" and r.source == "history")
            |> filter(fn: (r) => r.qtest == "{tag_id}")
            |> limit(n: 5)
        '''
        csv = influx_query(influx_url, influx_admin_token, influx_org, query)
        assert "1234.5" in csv or "enphase_power" in csv, f"History data not found:\n{csv[:500]}"


# ═══════════════════════════════════════════════════════════════════
# enphase_telegraf.py live output
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.timeout(45)
class TestLiveTelegrafOutput:
    """Run enphase_telegraf.py and verify it produces valid line protocol."""

    def _run_telegraf(self, enphase_email, enphase_password, duration=30):
        """Run enphase_telegraf.py for `duration` seconds, return (stdout, stderr)."""
        if not enphase_email or not enphase_password:
            pytest.skip("No Enphase credentials")
        repo = Path(__file__).parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo / "src")
        env["ENPHASE_EMAIL"] = enphase_email
        env["ENPHASE_PASSWORD"] = enphase_password
        try:
            result = subprocess.run(
                [str(repo / "venv" / "bin" / "python3"), str(repo / "src" / "enphase_telegraf.py"), "--verbose"],
                capture_output=True, timeout=duration, env=env,
            )
            return result.stdout.decode(), result.stderr.decode()
        except subprocess.TimeoutExpired as e:
            return (e.stdout or b"").decode(), (e.stderr or b"").decode()

    def test_produces_line_protocol(self, enphase_email, enphase_password):
        stdout, stderr = self._run_telegraf(enphase_email, enphase_password, duration=35)
        lines = [l for l in stdout.strip().splitlines() if l.strip()]
        assert len(lines) > 0, f"No stdout output. stderr:\n{stderr[:1000]}"
        for line in lines[:5]:
            parts = line.split(" ")
            assert len(parts) >= 3, f"Not line protocol: {line}"
            meas = parts[0].split(",")[0]
            assert meas.startswith("enphase_"), f"Unexpected measurement: {meas}"

    def test_output_has_enphase_measurement(self, enphase_email, enphase_password):
        stdout, stderr = self._run_telegraf(enphase_email, enphase_password, duration=35)
        # Should have at least one of: enphase_power, enphase_status, enphase_energy
        has_enphase = any(m in stdout for m in ["enphase_power", "enphase_status", "enphase_energy"])
        assert has_enphase, f"No enphase_ measurement in output.\nstdout: {stdout[:500]}\nstderr: {stderr[:500]}"


# ═══════════════════════════════════════════════════════════════════
# write_to_influxdb function
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestWriteToInfluxDB:
    """Test the history_loader.write_to_influxdb function directly."""

    def test_write_small_batch(self, influx_url, influx_admin_token, influx_org, influx_bucket):
        from enphase_cloud.history_loader import write_to_influxdb
        ts = int(time.time() * 1_000_000_000)
        lines = [
            f"enphase_test,source=pytest_write test_val={i}.0 {ts + i}" for i in range(10)
        ]
        written = write_to_influxdb(lines, influx_url, influx_admin_token, influx_org, influx_bucket)
        assert written == 10

    def test_write_with_progress_callback(self, influx_url, influx_admin_token, influx_org, influx_bucket):
        from enphase_cloud.history_loader import write_to_influxdb
        ts = int(time.time() * 1_000_000_000)
        lines = [
            f"enphase_test,source=pytest_cb val={i}.0 {ts + i}" for i in range(20)
        ]
        progress_calls = []
        def cb(written, total):
            progress_calls.append((written, total))

        written = write_to_influxdb(lines, influx_url, influx_admin_token, influx_org, influx_bucket,
                                     batch_size=10, progress_cb=cb)
        assert written == 20
        assert len(progress_calls) >= 2  # at least 2 batches

    def test_write_bad_token_fails_gracefully(self, influx_url, influx_org, influx_bucket):
        from enphase_cloud.history_loader import write_to_influxdb
        ts = int(time.time() * 1_000_000_000)
        lines = [f"enphase_test,source=pytest_bad val=1.0 {ts}"]
        written = write_to_influxdb(lines, influx_url, "bad-token", influx_org, influx_bucket)
        assert written == 0  # Should fail gracefully, not crash

    def test_write_empty_list(self, influx_url, influx_admin_token, influx_org, influx_bucket):
        from enphase_cloud.history_loader import write_to_influxdb
        written = write_to_influxdb([], influx_url, influx_admin_token, influx_org, influx_bucket)
        assert written == 0
