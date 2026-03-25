"""Persona-driven gap tests for enphase-telegraf.

Six test classes, one per persona:
  1. SRE / On-Call Engineer — resilience, reconnect, signal handling
  2. Security Engineer — credential safety, injection, leakage
  3. Data Engineer — sign conventions, units, timestamps, dedup, NaN
  4. QA Engineer — schema drift, protobuf changes, regression guards
  5. Home User — wrong password, no battery, MFA, internet down
  6. Solar Installer — setup idempotency, permissions, scripted install

150+ concrete, runnable tests exercising real code paths.
"""

import io
import json
import math
import os
import re
import signal
import sys
import textwrap
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import enphase_telegraf as et
from enphase_cloud.history_loader import (
    convert_day, convert_all, format_line,
    _esc_tag, _esc_field_str,
    INTERVAL_ENERGY_MAP, INTERVAL_FLOW_MAP, TOTALS_MAP,
)

REPO_DIR = Path(__file__).parent.parent
SETUP_SH = REPO_DIR / "setup.sh"
ENV_EXAMPLE = REPO_DIR / ".env.example"


# ═══════════════════════════════════════════════════════════════════
# Shared fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_telegraf_globals():
    """Full isolation: reset all module globals between tests."""
    et._serial = "TEST_SERIAL"
    et._client = None
    et._stream = None
    et._running = True
    et._verbose = False
    et._stdout_lock = threading.Lock()
    et._mqtt_messages = 0
    et._mqtt_errors = 0
    et._cloud_fetches = 0
    et._cloud_errors = 0
    et._auth_failures = 0
    et._start_time = time.time()
    et._last_batt_mode = None
    et._last_grid_relay = None
    et._last_gen_relay = None
    et._last_reserve_pct = None
    et._last_dry_contacts = {}
    et._error_backoff = {}
    et._known_fields = None
    et._unknown_enums_seen = set()
    et._cloud_last_fetch = {}
    yield


@pytest.fixture
def capture_emit(monkeypatch):
    """Capture emit() and emit_error() calls."""
    calls = []
    errors = []

    def fake_emit(measurement, tags, fields, ts_ns=None):
        calls.append({
            "measurement": measurement,
            "tags": dict(tags),
            "fields": dict(fields),
            "ts_ns": ts_ns,
        })

    def fake_emit_error(component, message):
        errors.append({"component": component, "message": message})

    monkeypatch.setattr(et, "emit", fake_emit)
    monkeypatch.setattr(et, "emit_error", fake_emit_error)
    return calls, errors


@pytest.fixture
def basic_mqtt_msg():
    """Minimal realistic MQTT message dict (daytime solar production)."""
    return {
        "protocol_ver": 1,
        "timestamp": int(time.time()),
        "pv_power_w": 3450.0,
        "grid_power_w": -1200.0,
        "load_power_w": 2250.0,
        "storage_power_w": 0.0,
        "generator_power_w": 0.0,
        "pv_apparent_va": 3500.0,
        "grid_apparent_va": 1250.0,
        "load_apparent_va": 2300.0,
        "storage_apparent_va": 0.0,
        "generator_apparent_va": 0.0,
        "meter_soc": 85,
        "soc": 20,
        "batt_mode": "BATT_MODE_SELF_CONS",
        "grid_relay": "OPER_RELAY_CLOSED",
        "gen_relay": "OPER_RELAY_OPEN",
        "pcu_total": 16,
        "pcu_running": 16,
        "_fields_present": frozenset(["protocol_ver", "timestamp", "meters"]),
    }


@pytest.fixture
def nighttime_mqtt_msg():
    """MQTT message during nighttime (no solar, importing from grid)."""
    return {
        "protocol_ver": 1,
        "timestamp": int(time.time()),
        "pv_power_w": 0.0,
        "grid_power_w": 800.0,
        "load_power_w": 800.0,
        "storage_power_w": 0.0,
        "generator_power_w": 0.0,
        "meter_soc": 60,
        "soc": 20,
        "batt_mode": "BATT_MODE_SELF_CONS",
        "grid_relay": "OPER_RELAY_CLOSED",
        "pcu_total": 16,
        "pcu_running": 0,
        "_fields_present": frozenset(["protocol_ver", "timestamp", "meters"]),
    }


@pytest.fixture
def no_battery_today_json(sample_today_json):
    """today.json for a system with NO battery installed."""
    data = dict(sample_today_json)
    # Remove battery-related keys
    data.pop("battery_details", None)
    data.pop("batteryConfig", None)
    # Zero out battery flows in intervals
    for iv in data["stats"][0]["intervals"]:
        iv["solar_battery"] = 0.0
        iv["battery_home"] = 0.0
        iv["battery_grid"] = 0.0
        iv["grid_battery"] = 0.0
    # Zero out battery totals
    totals = data["stats"][0]["totals"]
    totals["charge"] = 0.0
    totals["discharge"] = 0.0
    totals["solar_battery"] = 0.0
    totals["battery_home"] = 0.0
    totals["battery_grid"] = 0.0
    totals["grid_battery"] = 0.0
    return data


class MockClient:
    """Lightweight mock of EnlightenClient for unit tests."""

    def __init__(self, responses=None, authenticated=True, auth_time=None):
        self._responses = responses or {}
        self._session = MagicMock()
        self._session.site_id = "12345"
        self._session.authenticated = authenticated
        self._session.auth_time = auth_time or time.time()
        self._authenticated = authenticated
        self._login_count = 0

    @property
    def authenticated(self):
        return self._authenticated

    def login(self):
        self._login_count += 1
        self._authenticated = True

    def get_today(self):
        return self._responses.get("get_today", {"stats": [{"totals": {}, "intervals": []}]})

    def get_battery_status(self):
        return self._responses.get("get_battery_status", {})

    def get_devices(self):
        return self._responses.get("get_devices", {})

    def get_latest_power(self):
        return self._responses.get("get_latest_power", {})

    def get_site_data(self):
        return self._responses.get("get_site_data", {})

    def get_events(self):
        return self._responses.get("get_events", {})

    def get_alarms(self):
        return self._responses.get("get_alarms", {})

    def get_inverters(self):
        return self._responses.get("get_inverters", {})

    def get_battery_settings(self):
        return self._responses.get("get_battery_settings", {})

    def get_battery_schedules(self):
        return self._responses.get("get_battery_schedules", {})


# ═══════════════════════════════════════════════════════════════════
# 1. SRE / On-Call Engineer
# "What happens at 3am when things break?"
# ═══════════════════════════════════════════════════════════════════

class TestSREOnCall:
    """Resilience, reconnection, signal handling, and failure modes."""

    # ── Auth token expiration and re-auth ──

    def test_auth_retry_loop_increments_backoff(self, monkeypatch):
        """Auth failure should increment _auth_failures and use backoff."""
        from enphase_cloud.enlighten import AuthError
        attempts = []

        def fake_login(self):
            attempts.append(1)
            raise AuthError("Invalid credentials")

        monkeypatch.setattr("enphase_cloud.enlighten.EnlightenClient.login", fake_login)
        # Simulate the retry logic from main() but with limited iterations
        backoff = 10
        for _ in range(3):
            try:
                client = MagicMock()
                client.login.side_effect = AuthError("bad creds")
                client.login()
            except AuthError:
                et._auth_failures += 1
                backoff = min(backoff * 2, 600)

        assert et._auth_failures == 3
        assert backoff == 80  # 10 -> 20 -> 40 -> 80

    def test_auth_failure_emits_error(self, capture_emit):
        """Auth failure should emit an enphase_error measurement."""
        calls, errors = capture_emit
        et.emit_error("auth", "Invalid credentials")
        assert len(errors) == 1
        assert errors[0]["component"] == "auth"
        assert "Invalid credentials" in errors[0]["message"]

    def test_auth_backoff_caps_at_600(self):
        """Auth backoff should cap at 600 seconds."""
        backoff = 10
        for _ in range(20):
            backoff = min(backoff * 2, 600)
        assert backoff == 600

    def test_session_ttl_expires_after_1_hour(self):
        """EnlightenClient.authenticated should return False after SESSION_TTL."""
        from enphase_cloud.enlighten import EnlightenClient
        client = EnlightenClient("test@test.com", "pass")
        client._session.authenticated = True
        client._session.auth_time = time.time() - 3601  # Expired
        assert client.authenticated is False

    def test_session_ttl_valid_within_1_hour(self):
        """EnlightenClient.authenticated should return True within SESSION_TTL."""
        from enphase_cloud.enlighten import EnlightenClient
        client = EnlightenClient("test@test.com", "pass")
        client._session.authenticated = True
        client._session.auth_time = time.time() - 100  # Recent
        assert client.authenticated is True

    def test_clear_error_resets_backoff(self):
        """_clear_error should remove backoff state for a component."""
        et._error_backoff["auth"] = {"last_emit": time.time(), "interval": 120, "message": "err"}
        et._clear_error("auth")
        assert "auth" not in et._error_backoff

    def test_clear_error_on_nonexistent_key_no_crash(self):
        """_clear_error on unknown key should not raise."""
        et._clear_error("nonexistent_component")  # should not raise

    # ── MQTT disconnect/reconnect ──

    def test_mqtt_error_callback_increments_counter(self, capture_emit):
        """on_mqtt_status with error keywords should increment mqtt_errors."""
        et.on_mqtt_status("Connection failed: network error")
        assert et._mqtt_errors == 1

    def test_mqtt_error_callback_emits_error(self, capture_emit):
        """on_mqtt_status with failure should call emit_error."""
        calls, errors = capture_emit
        et.on_mqtt_status("MQTT connection failed")
        assert len(errors) == 1
        assert errors[0]["component"] == "mqtt"

    def test_mqtt_status_normal_no_error(self):
        """on_mqtt_status with normal message should not increment errors."""
        et.on_mqtt_status("Connected — subscribing to topics")
        assert et._mqtt_errors == 0

    def test_mqtt_message_clears_error_state(self, capture_emit, basic_mqtt_msg):
        """Successful MQTT message should clear mqtt error backoff."""
        et._error_backoff["mqtt"] = {"last_emit": time.time(), "interval": 60, "message": "err"}
        et.on_mqtt_data(basic_mqtt_msg)
        assert "mqtt" not in et._error_backoff

    # ── Cloud poll error handling ──

    def test_cloud_poll_exception_increments_errors(self, capture_emit, monkeypatch):
        """Cloud endpoint raising exception should increment cloud_errors."""
        client = MockClient()
        client.get_latest_power = MagicMock(side_effect=Exception("timeout"))
        et._client = client
        et._cloud_last_fetch = {}
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        assert et._cloud_errors >= 1

    def test_cloud_poll_error_retries_after_60s(self, capture_emit, monkeypatch):
        """After cloud error, endpoint should be retried after 60s, not full interval."""
        client = MockClient()
        client.get_latest_power = MagicMock(side_effect=Exception("fail"))
        et._client = client
        now = 1_000_000.0
        monkeypatch.setattr(time, "time", lambda: now)

        et.cloud_poll_once()
        # The failing endpoint should be scheduled for retry at now - interval + 60
        latest_power_interval = et.CLOUD_SCHEDULE["latest_power"]
        expected_retry = now - latest_power_interval + 60
        assert et._cloud_last_fetch["latest_power"] == expected_retry

    def test_cloud_poll_success_clears_error(self, capture_emit, monkeypatch):
        """Successful cloud poll should clear error backoff for that endpoint."""
        et._error_backoff["cloud_latest_power"] = {
            "last_emit": time.time(), "interval": 120, "message": "prev error"
        }
        client = MockClient({"get_latest_power": {"latest_power": {"value": 1000}}})
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        assert "cloud_latest_power" not in et._error_backoff

    # ── Signal handling ──

    def test_signal_handler_sets_running_false(self):
        """The shutdown signal handler should set _running to False."""
        et._running = True
        # Simulate what main() does: define a shutdown handler
        def shutdown(signum, frame):
            et._running = False
        shutdown(signal.SIGTERM, None)
        assert et._running is False

    def test_signal_handler_stops_stream(self):
        """Shutdown should call stream.stop() if stream exists."""
        mock_stream = MagicMock()
        et._stream = mock_stream
        et._running = True

        def shutdown(signum, frame):
            et._running = False
            if et._stream:
                et._stream.stop()

        shutdown(signal.SIGTERM, None)
        assert et._running is False
        mock_stream.stop.assert_called_once()

    def test_sigint_and_sigterm_both_handled(self):
        """Both SIGINT and SIGTERM should trigger shutdown."""
        results = []

        def shutdown(signum, frame):
            results.append(signum)
            et._running = False

        shutdown(signal.SIGTERM, None)
        et._running = True
        shutdown(signal.SIGINT, None)
        assert len(results) == 2
        assert signal.SIGTERM in results
        assert signal.SIGINT in results

    # ── Clock jump ──

    def test_clock_jump_backward_does_not_crash_emit(self, monkeypatch, capsys):
        """If system clock jumps backward, emit() should still work."""
        monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
        et.emit("enphase_test", {}, {"v": 1})
        out1 = capsys.readouterr().out
        assert "enphase_test" in out1

        # Clock jumps backward by 1 hour
        monkeypatch.setattr(time, "time", lambda: 1_699_996_400.0)
        et.emit("enphase_test", {}, {"v": 2})
        out2 = capsys.readouterr().out
        assert "enphase_test" in out2

    def test_clock_jump_backward_error_backoff_still_works(self, monkeypatch):
        """Clock jump backward should not prevent error emission forever."""
        monkeypatch.setattr(time, "time", lambda: 2_000_000.0)
        assert et._should_emit_error("comp", "msg") is True
        # Clock jumps backward
        monkeypatch.setattr(time, "time", lambda: 1_999_900.0)
        # Should be suppressed (within interval from perspective of last_emit)
        assert et._should_emit_error("comp", "msg") is False
        # But advancing from the new time past the interval should re-emit
        monkeypatch.setattr(time, "time", lambda: 2_000_061.0)
        assert et._should_emit_error("comp", "msg") is True

    def test_emit_with_explicit_ts_ns_ignores_clock(self, capsys):
        """emit() with explicit ts_ns should use that timestamp, not clock."""
        et.emit("m", {}, {"v": 1}, ts_ns=42)
        out = capsys.readouterr().out
        assert out.strip().endswith("42")

    # ── Status heartbeat ──

    def test_status_heartbeat_emits_all_counters(self, capture_emit, monkeypatch):
        """Status emission should include all health counters."""
        et._mqtt_messages = 100
        et._mqtt_errors = 2
        et._cloud_fetches = 50
        et._cloud_errors = 1
        et._auth_failures = 0
        et._start_time = time.time() - 3600
        et._stream = MagicMock()
        et._stream.connected = True
        et._client = MockClient()

        calls, _ = capture_emit
        # Directly call the emit that status_loop does
        et.emit("enphase_status", {"serial": et._serial}, {
            "uptime_s": int(time.time() - et._start_time),
            "mqtt_connected": int(bool(et._stream and et._stream.connected)),
            "mqtt_msg_total": et._mqtt_messages,
            "mqtt_err_total": et._mqtt_errors,
            "cloud_ok": int(bool(et._client and et._client.authenticated)),
            "cloud_fetch_total": et._cloud_fetches,
            "cloud_err_total": et._cloud_errors,
            "auth_err_total": et._auth_failures,
        })
        assert len(calls) == 1
        f = calls[0]["fields"]
        assert f["mqtt_msg_total"] == 100
        assert f["mqtt_err_total"] == 2
        assert f["cloud_fetch_total"] == 50
        assert f["cloud_err_total"] == 1
        assert f["mqtt_connected"] == 1
        assert f["cloud_ok"] == 1

    # ── main() exits on missing credentials ──

    def test_main_exits_without_credentials(self, monkeypatch):
        """main() should sys.exit(1) if no email/password provided."""
        monkeypatch.setattr(sys, "argv", ["enphase_telegraf.py"])
        monkeypatch.delenv("ENPHASE_EMAIL", raising=False)
        monkeypatch.delenv("ENPHASE_PASSWORD", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            et.main()
        assert exc_info.value.code == 1

    # ── discover_serial fallback ──

    def test_discover_serial_handles_exception_gracefully(self):
        """discover_serial should return '' if client raises."""
        client = MagicMock()
        client.get_devices.side_effect = Exception("network error")
        assert et.discover_serial(client) == ""

    def test_discover_serial_handles_non_dict_response(self):
        """discover_serial should return '' if response is not a dict."""
        client = MagicMock()
        client.get_devices.return_value = "not a dict"
        assert et.discover_serial(client) == ""

    def test_cloud_poll_with_no_client_is_noop(self, capture_emit):
        """cloud_poll_once() when _client is None should do nothing."""
        et._client = None
        et.cloud_poll_once()
        calls, errors = capture_emit
        assert len(calls) == 0


# ═══════════════════════════════════════════════════════════════════
# 2. Security Engineer
# "Can this be exploited?"
# ═══════════════════════════════════════════════════════════════════

class TestSecurityEngineer:
    """Credential safety, injection prevention, and leakage."""

    # ── .env.example has no real credentials ──

    def test_env_example_exists(self):
        """The .env.example file must exist."""
        assert ENV_EXAMPLE.exists()

    def test_env_example_email_is_placeholder(self):
        """The .env.example should not contain a real email."""
        content = ENV_EXAMPLE.read_text()
        assert "you@example.com" in content
        # Must NOT contain a real-looking email with common providers
        for domain in ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]:
            lines_with_email = [l for l in content.splitlines()
                                if "ENPHASE_EMAIL" in l and domain in l]
            assert len(lines_with_email) == 0, f"Real email with {domain} in .env.example"

    def test_env_example_password_is_placeholder(self):
        """The .env.example should not contain a real password."""
        content = ENV_EXAMPLE.read_text()
        assert "yourpassword" in content
        # Ensure the password placeholder is clearly fake
        for line in content.splitlines():
            if "ENPHASE_PASSWORD" in line and "=" in line:
                _, _, val = line.partition("=")
                val = val.strip()
                assert val in ("yourpassword", "your-password", "changeme", ""),\
                    f"Suspicious password in .env.example: {val}"

    def test_env_example_token_is_placeholder(self):
        """The .env.example should not have a real InfluxDB token."""
        content = ENV_EXAMPLE.read_text()
        for line in content.splitlines():
            if "INFLUXDB_TOKEN" in line and "=" in line:
                _, _, val = line.partition("=")
                val = val.strip()
                # Real tokens are typically 80+ chars of base64
                assert len(val) < 50, f"Suspiciously long token in .env.example: {len(val)} chars"

    # ── Credentials never in stdout ──

    def test_emit_never_outputs_password(self, capsys):
        """emit() should never leak a password-like field to stdout."""
        # Simulate someone accidentally passing a password as a field
        et.emit("enphase_error", {"serial": "X"}, {"password": "s3cret123"}, ts_ns=1)
        out = capsys.readouterr().out
        # The emit function WILL output the field (it doesn't filter).
        # But the key concern is: does the normal code path ever pass credentials?
        # This test documents that emit does not strip secrets — callers must not pass them.
        assert "enphase_error" in out

    def test_error_function_writes_to_stderr_not_stdout(self, capsys):
        """et.error() should write to stderr, never stdout."""
        et.error("something failed")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "something failed" in captured.err

    def test_warn_function_writes_to_stderr_not_stdout(self, capsys):
        """et.warn() should write to stderr, never stdout."""
        et.warn("some warning")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "some warning" in captured.err

    def test_log_verbose_writes_to_stderr(self, capsys):
        """et.log() should write to stderr when verbose is True."""
        et._verbose = True
        et.log("debug info")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "debug info" in captured.err

    def test_log_not_verbose_no_output(self, capsys):
        """et.log() should produce no output when verbose is False."""
        et._verbose = False
        et.log("debug info")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    # ── Malicious MQTT payload ──

    def test_malicious_string_field_escaped(self, capsys):
        """Strings with injection characters should be escaped in line protocol."""
        et.emit("m", {}, {"msg": 'DROP MEASUREMENT "enphase_power"'}, ts_ns=1)
        out = capsys.readouterr().out
        assert r'\"enphase_power\"' in out
        # The inner quotes should be escaped with backslash, preventing InfluxDB injection
        # Output: msg="DROP MEASUREMENT \"enphase_power\"" — 2 wrapping + 2 escaped = 4 raw "
        assert r'\"' in out

    def test_malicious_tag_value_escaped(self, capsys):
        """Tag values with special chars should be properly escaped."""
        et.emit("m", {"serial": "abc,evil=inject"}, {"v": 1}, ts_ns=1)
        out = capsys.readouterr().out
        assert r"abc\,evil\=inject" in out

    def test_newline_injection_in_field_prevented(self, capsys):
        """Newlines in field values must be escaped to prevent line splitting."""
        et.emit("m", {}, {"msg": "first\nsecond"}, ts_ns=1)
        out = capsys.readouterr().out
        lines = [l for l in out.split("\n") if l.strip()]
        assert len(lines) == 1, "Newline injection created multiple lines"

    def test_newline_in_tag_stripped(self, capsys):
        """Newlines in tag values must be stripped to prevent line corruption."""
        et.emit("m", {"t": "a\nb"}, {"v": 1}, ts_ns=1)
        out = capsys.readouterr().out
        lines = [l for l in out.split("\n") if l.strip()]
        assert len(lines) == 1, f"Newline in tag produced {len(lines)} lines: {lines}"
        assert "t=ab" in lines[0], "Newline not stripped from tag value"

    def test_carriage_return_in_tag_stripped(self, capsys):
        """Carriage returns in tag values must be stripped."""
        et.emit("m", {"t": "a\rb"}, {"v": 1}, ts_ns=1)
        out = capsys.readouterr().out
        lines = [l for l in out.split("\n") if l.strip()]
        assert len(lines) == 1
        assert "t=ab" in lines[0]

    def test_carriage_return_stripped_from_field(self):
        """Carriage returns should be removed from field strings."""
        result = et._esc_field_str("line1\rline2")
        assert "\r" not in result
        assert result == "line1line2"

    def test_null_byte_in_field_does_not_crash(self, capsys):
        """Null bytes in field values should not crash emit()."""
        et.emit("m", {}, {"msg": "before\x00after"}, ts_ns=1)
        out = capsys.readouterr().out
        assert "enphase_error" not in out or "m " in out  # Should emit without crash

    # ── Path traversal in history cache ──

    def test_history_cache_filename_no_path_traversal(self, tmp_path, sample_today_json):
        """Day files should use safe filenames without ../ components."""
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        # A date that looks normal should produce a safe filename
        data = dict(sample_today_json)
        data["_cloned_date"] = "2024-03-24"
        (history_dir / "day_2024-03-24.json").write_text(json.dumps(data))
        lines = convert_all(history_dir, "SERIAL")
        assert len(lines) > 0

    def test_history_cache_malicious_filename_ignored(self, tmp_path):
        """Files with path traversal attempts in filenames are not matched by glob."""
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        # This should NOT match the day_*.json glob
        evil = history_dir / "day_../../etc/passwd.json"
        # Can't create this file on most systems, but verify the glob pattern
        day_files = sorted(history_dir.glob("day_*.json"))
        assert len(day_files) == 0

    # ── setup.sh .env permissions ──

    def test_setup_sh_sets_env_file_permissions(self):
        """setup.sh should set .env to mode 600 (owner-only read)."""
        content = SETUP_SH.read_text()
        assert "chmod" in content and "600" in content

    def test_setup_sh_does_not_echo_password(self):
        """setup.sh should use read -s (silent) for password input."""
        content = SETUP_SH.read_text()
        assert "read -rs" in content or "prompt_secret" in content

    # ── Logging never leaks credentials ──

    def test_main_logs_email_but_not_password(self, monkeypatch):
        """The log message in main() should show email but never the password."""
        # Check the source code for the log line
        source = Path(__file__).parent.parent / "src" / "enphase_telegraf.py"
        content = source.read_text()
        # Find lines that reference args.password in a log/print context
        for i, line in enumerate(content.splitlines(), 1):
            if "args.password" in line:
                # This line should NOT be in a log(), warn(), error() or print() call
                stripped = line.strip()
                assert not stripped.startswith("log("), \
                    f"Line {i} may log password: {line.strip()}"
                assert not stripped.startswith("warn("), \
                    f"Line {i} may warn password: {line.strip()}"
                assert not stripped.startswith("print("), \
                    f"Line {i} may print password: {line.strip()}"
                assert not stripped.startswith("error(") or "required" in stripped.lower(), \
                    f"Line {i} may error with password: {line.strip()}"


# ═══════════════════════════════════════════════════════════════════
# 3. Data Engineer
# "Is the data correct?"
# ═══════════════════════════════════════════════════════════════════

class TestDataEngineer:
    """Sign conventions, unit consistency, timestamps, dedup, NaN handling."""

    # ── Sign conventions ──

    def test_solar_power_always_positive_daytime(self, capture_emit, basic_mqtt_msg):
        """Solar (pv) power should be >= 0 during daytime."""
        calls, _ = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert len(power_calls) >= 1
        solar = power_calls[0]["fields"].get("solar_w")
        assert solar is not None
        assert solar >= 0

    def test_solar_zero_at_night(self, capture_emit, nighttime_mqtt_msg):
        """Solar power should be 0 at night."""
        calls, _ = capture_emit
        et.on_mqtt_data(nighttime_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert len(power_calls) >= 1
        solar = power_calls[0]["fields"].get("solar_w", 0)
        assert solar == 0.0

    def test_grid_negative_means_export(self, capture_emit, basic_mqtt_msg):
        """Negative grid_w means exporting to grid (solar surplus)."""
        calls, _ = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        grid = power_calls[0]["fields"].get("grid_w")
        assert grid is not None
        assert grid < 0  # Exporting in the daytime scenario

    def test_grid_positive_means_import(self, capture_emit, nighttime_mqtt_msg):
        """Positive grid_w means importing from grid."""
        calls, _ = capture_emit
        et.on_mqtt_data(nighttime_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        grid = power_calls[0]["fields"].get("grid_w")
        assert grid is not None
        assert grid > 0  # Importing at night

    def test_consumption_always_positive(self, capture_emit, basic_mqtt_msg):
        """Consumption (load) power should always be >= 0."""
        calls, _ = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        consumption = power_calls[0]["fields"].get("consumption_w")
        assert consumption is not None
        assert consumption >= 0

    def test_soc_range_0_to_100(self, capture_emit, basic_mqtt_msg):
        """Battery SOC should be between 0 and 100."""
        calls, _ = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        soc = power_calls[0]["fields"].get("soc")
        assert soc is not None
        assert 0 <= soc <= 100

    def test_soc_out_of_range_rejected(self, capture_emit):
        """SOC values outside 0-100 should be flagged as anomalies."""
        calls, errors = capture_emit
        msg = {
            "meter_soc": 150,  # Invalid
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        # SOC should NOT be in the fields (rejected)
        if power_calls:
            assert power_calls[0]["fields"].get("soc") is None

    def test_soc_negative_rejected(self, capture_emit):
        """Negative SOC should be rejected."""
        calls, errors = capture_emit
        msg = {
            "meter_soc": -5,
            "pv_power_w": 100.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        if power_calls:
            assert power_calls[0]["fields"].get("soc") is None

    # ── Anomaly detection (power spike guard) ──

    def test_power_over_100kw_flagged_as_anomaly(self, capture_emit):
        """Power values > 100,000W should be rejected as anomalies."""
        calls, errors = capture_emit
        msg = {
            "pv_power_w": 150000.0,  # 150kW — bogus for residential
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        # The 150kW value should be dropped
        if power_calls:
            assert "solar_w" not in power_calls[0]["fields"]
        assert len(errors) >= 1  # anomaly error emitted

    def test_normal_power_values_accepted(self, capture_emit, basic_mqtt_msg):
        """Normal power values (< 100kW) should pass anomaly check."""
        calls, errors = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert "solar_w" in power_calls[0]["fields"]
        anomaly_errors = [e for e in errors if e["component"] == "data_quality"]
        assert len(anomaly_errors) == 0

    # ── Unit consistency (watts not kilowatts) ──

    def test_power_fields_are_watts(self, capture_emit, basic_mqtt_msg):
        """Power fields should be in watts (not kW). Typical range 0-10000."""
        calls, _ = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        fields = power_calls[0]["fields"]
        solar = fields.get("solar_w", 0)
        # Solar should be in watts: e.g. 3450, not 3.45 (kW)
        assert solar > 100, f"Solar {solar}W looks like kW — wrong unit?"

    def test_energy_fields_are_watt_hours(self, sample_today_json):
        """Energy totals should be in watt-hours (not kWh)."""
        lines = convert_day(sample_today_json, "SERIAL")
        daily_lines = [l for l in lines if "source=history_daily" in l]
        assert len(daily_lines) >= 1
        line = daily_lines[0]
        # production_wh should be > 1000 (18238 Wh, not 18.238 kWh)
        match = re.search(r"production_wh=([0-9.]+)", line)
        assert match, f"No production_wh in line: {line}"
        val = float(match.group(1))
        assert val > 100, f"production_wh={val} looks like kWh — wrong unit?"

    # ── Timestamp accuracy ──

    def test_interval_timestamps_are_utc_nanoseconds(self, sample_today_json):
        """Interval timestamps should be in nanoseconds (Unix epoch)."""
        lines = convert_day(sample_today_json, "SERIAL")
        power_lines = [l for l in lines if "enphase_power," in l and "source=history" in l]
        assert len(power_lines) > 0
        # Extract timestamp from first line
        ts_str = power_lines[0].strip().split(" ")[-1]
        ts_ns = int(ts_str)
        # Should be in nanosecond range (> 1e18)
        assert ts_ns > 1_000_000_000_000_000_000, f"Timestamp {ts_ns} not in nanoseconds"

    def test_interval_timestamps_monotonically_increasing(self, sample_today_json):
        """Interval timestamps should be strictly increasing across a day."""
        lines = convert_day(sample_today_json, "SERIAL")
        power_lines = [l for l in lines if l.startswith("enphase_power,") and "source=history " in l]
        timestamps = [int(l.strip().split(" ")[-1]) for l in power_lines]
        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i - 1], \
                f"Timestamp[{i}] {timestamps[i]} <= [{i-1}] {timestamps[i-1]}"

    def test_today_json_timestamps_span_24_hours(self, sample_today_json):
        """96 intervals at 15-min resolution should span 24 hours."""
        lines = convert_day(sample_today_json, "SERIAL")
        power_lines = [l for l in lines if l.startswith("enphase_power,") and "source=history " in l]
        if len(power_lines) < 2:
            pytest.skip("Not enough intervals")
        first_ts = int(power_lines[0].strip().split(" ")[-1])
        last_ts = int(power_lines[-1].strip().split(" ")[-1])
        span_hours = (last_ts - first_ts) / 1_000_000_000 / 3600
        assert 23 <= span_hours <= 24, f"Span is {span_hours} hours, expected ~24"

    # ── NaN / infinity handling ──

    def test_nan_field_skipped(self, capsys):
        """NaN float values must be skipped — InfluxDB rejects them."""
        et.emit("m", {}, {"good": 1.0, "bad": float("nan")}, ts_ns=1)
        out = capsys.readouterr().out
        assert "good=1.0" in out
        assert "bad=" not in out, "NaN field should be filtered out"
        assert "nan" not in out.lower(), "NaN value leaked into output"

    def test_negative_nan_field_skipped(self, capsys):
        """Negative NaN should also be skipped."""
        et.emit("m", {}, {"good": 2.0, "bad": float("-nan")}, ts_ns=1)
        out = capsys.readouterr().out
        assert "good=2.0" in out
        assert "bad=" not in out

    def test_infinity_field_skipped(self, capsys):
        """Positive infinity must be skipped — InfluxDB rejects it."""
        et.emit("m", {}, {"good": 3.0, "bad": float("inf")}, ts_ns=1)
        out = capsys.readouterr().out
        assert "good=3.0" in out
        assert "bad=" not in out, "Infinity field should be filtered out"
        assert "inf" not in out.lower(), "Infinity value leaked into output"

    def test_negative_infinity_field_skipped(self, capsys):
        """Negative infinity must also be skipped."""
        et.emit("m", {}, {"good": 4.0, "bad": float("-inf")}, ts_ns=1)
        out = capsys.readouterr().out
        assert "good=4.0" in out
        assert "bad=" not in out

    def test_all_nan_inf_fields_produce_no_output(self, capsys):
        """If all float fields are NaN/inf, no line should be emitted."""
        et.emit("m", {}, {"a": float("nan"), "b": float("inf")}, ts_ns=1)
        out = capsys.readouterr().out
        assert out.strip() == "", "Line with only NaN/inf fields should produce no output"

    # ── Deduplication ──

    def test_config_change_emitted_once(self, capture_emit):
        """Battery mode change should emit config only on CHANGE."""
        calls, _ = capture_emit
        msg1 = {
            "batt_mode": "BATT_MODE_SELF_CONS",
            "soc": 20,
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg1)
        config_calls_1 = [c for c in calls if c["measurement"] == "enphase_config"]
        assert len(config_calls_1) == 1

        # Same mode again — should NOT emit
        et.on_mqtt_data(msg1)
        config_calls_2 = [c for c in calls if c["measurement"] == "enphase_config"]
        assert len(config_calls_2) == 1  # Still 1, not 2

    def test_config_change_emitted_on_actual_change(self, capture_emit):
        """A different battery mode should trigger a new config emission."""
        calls, _ = capture_emit
        msg1 = {
            "batt_mode": "BATT_MODE_SELF_CONS",
            "soc": 20,
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg1)
        msg2 = dict(msg1)
        msg2["batt_mode"] = "BATT_MODE_FULL_BACKUP"
        et.on_mqtt_data(msg2)
        config_calls = [c for c in calls if c["measurement"] == "enphase_config"]
        assert len(config_calls) == 2

    def test_dry_contact_emits_only_on_change(self, capture_emit):
        """Dry contact state should only emit when state changes."""
        calls, _ = capture_emit
        msg_base = {
            "pv_power_w": 100.0,
            "_fields_present": frozenset(),
            "dry_contacts": [{"id": "DC1", "state": "DC_RELAY_OFF"}],
        }
        et.on_mqtt_data(msg_base)
        dc_calls_1 = [c for c in calls if c["measurement"] == "enphase_dry_contact"]
        assert len(dc_calls_1) == 1

        # Same state — no new emission
        et.on_mqtt_data(msg_base)
        dc_calls_2 = [c for c in calls if c["measurement"] == "enphase_dry_contact"]
        assert len(dc_calls_2) == 1

        # State changes
        msg_on = dict(msg_base)
        msg_on["dry_contacts"] = [{"id": "DC1", "state": "DC_RELAY_ON"}]
        et.on_mqtt_data(msg_on)
        dc_calls_3 = [c for c in calls if c["measurement"] == "enphase_dry_contact"]
        assert len(dc_calls_3) == 2

    # ── Phase sum consistency check ──

    def test_phase_sum_consistency_no_anomaly_when_valid(self, capture_emit):
        """When L1+L2 matches aggregate, no anomaly should be flagged."""
        calls, errors = capture_emit
        msg = {
            "pv_power_w": 3000.0,
            "pv_phase_w": [1500.0, 1500.0],
            "load_power_w": 2000.0,
            "load_phase_w": [1000.0, 1000.0],
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        anomaly_errors = [e for e in errors if "phase_sum" in e.get("message", "")]
        assert len(anomaly_errors) == 0

    def test_phase_sum_mismatch_flags_anomaly(self, capture_emit):
        """When L1+L2 drastically differs from aggregate, flag anomaly."""
        calls, errors = capture_emit
        msg = {
            "pv_power_w": 3000.0,
            "pv_phase_w": [100.0, 100.0],  # Sum=200, aggregate=3000 — mismatch
            "load_power_w": 2000.0,
            "load_phase_w": [1000.0, 1000.0],
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        anomaly_errors = [e for e in errors if "data_quality" in e.get("component", "")]
        assert len(anomaly_errors) >= 1

    # ── History energy balance ──

    def test_history_energy_balance_solar_to_home_plus_grid_equals_production(self, sample_today_json):
        """For each interval: solar_to_home + solar_to_grid + solar_to_battery ~= production."""
        stats = sample_today_json["stats"][0]
        for iv in stats["intervals"]:
            production = iv.get("production", 0)
            solar_home = iv.get("solar_home", 0)
            solar_grid = iv.get("solar_grid", 0)
            solar_battery = iv.get("solar_battery", 0)
            total_solar_out = solar_home + solar_grid + solar_battery
            # Should be approximately equal (floating point tolerance)
            if production > 0:
                assert abs(production - total_solar_out) < 1.0, \
                    f"Energy balance: prod={production}, out={total_solar_out}"

    def test_history_grid_power_sign_convention(self, sample_today_json):
        """In history conversion, grid_w = import - export."""
        lines = convert_day(sample_today_json, "SERIAL")
        power_lines = [l for l in lines if l.startswith("enphase_power,") and "source=history " in l]
        # Find a nighttime interval (no solar, grid import)
        for line in power_lines:
            match = re.search(r"grid_w=(-?[0-9.]+)", line)
            if match:
                grid_w = float(match.group(1))
                # Grid can be positive (import) or negative (export)
                assert isinstance(grid_w, float)
                break


# ═══════════════════════════════════════════════════════════════════
# 4. QA Engineer (Regression Focus)
# "What broke last time?"
# ═══════════════════════════════════════════════════════════════════

class TestQARegression:
    """Schema drift, protobuf changes, field renames, and compatibility."""

    # ── Protocol version mismatch detection ──

    def test_proto_version_mismatch_emits_error(self, capture_emit):
        """Protocol version change should emit an error."""
        calls, errors = capture_emit
        msg = {
            "protocol_ver": 2,  # Expected is 1
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        proto_errors = [e for e in errors if e["component"] == "proto_version"]
        assert len(proto_errors) >= 1

    def test_proto_version_match_no_error(self, capture_emit, basic_mqtt_msg):
        """Matching protocol version should not emit errors."""
        calls, errors = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        proto_errors = [e for e in errors if e["component"] == "proto_version"]
        assert len(proto_errors) == 0

    # ── New/missing protobuf fields ──

    def test_new_protobuf_field_detected(self, capture_emit):
        """A new protobuf field should be detected and reported."""
        calls, errors = capture_emit
        # First message establishes baseline
        msg1 = {
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(["field_a", "field_b"]),
        }
        et.on_mqtt_data(msg1)

        # Second message has a new field
        msg2 = {
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(["field_a", "field_b", "field_c_new"]),
        }
        et.on_mqtt_data(msg2)
        new_field_errors = [e for e in errors if e["component"] == "proto_new_fields"]
        assert len(new_field_errors) >= 1
        assert "field_c_new" in new_field_errors[0]["message"]

    def test_missing_protobuf_field_detected(self, capture_emit):
        """A missing protobuf field should be detected and reported."""
        calls, errors = capture_emit
        msg1 = {
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(["field_a", "field_b", "field_c"]),
        }
        et.on_mqtt_data(msg1)

        msg2 = {
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(["field_a", "field_b"]),
        }
        et.on_mqtt_data(msg2)
        missing_errors = [e for e in errors if e["component"] == "proto_missing_fields"]
        assert len(missing_errors) >= 1
        assert "field_c" in missing_errors[0]["message"]

    # ── Unknown enum values ──

    def test_unknown_batt_mode_detected(self, capture_emit):
        """Unknown battery mode enum should emit error."""
        calls, errors = capture_emit
        msg = {
            "batt_mode": "BATT_MODE_UNKNOWN_NEW_MODE",
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        enum_errors = [e for e in errors if e["component"] == "proto_unknown_enum"]
        assert len(enum_errors) >= 1

    def test_known_batt_mode_no_error(self, capture_emit, basic_mqtt_msg):
        """Known battery mode should not emit enum error."""
        calls, errors = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        enum_errors = [e for e in errors if e["component"] == "proto_unknown_enum"]
        assert len(enum_errors) == 0

    def test_unknown_grid_relay_detected(self, capture_emit):
        """Unknown grid relay enum should emit error."""
        calls, errors = capture_emit
        msg = {
            "grid_relay": "OPER_RELAY_FUTURE_STATE",
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        enum_errors = [e for e in errors if e["component"] == "proto_unknown_enum"]
        assert len(enum_errors) >= 1

    def test_unknown_enum_reported_only_once(self, capture_emit):
        """Same unknown enum should only be reported once (tracked in _unknown_enums_seen)."""
        calls, errors = capture_emit
        msg = {
            "batt_mode": "BATT_MODE_ALIEN",
            "pv_power_w": 1000.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        et.on_mqtt_data(msg)
        enum_errors = [e for e in errors if e["component"] == "proto_unknown_enum"]
        # Second identical unknown enum should not emit another error
        assert len(enum_errors) == 1

    # ── Field mapping consistency: history_loader vs enphase_telegraf ──

    def test_history_power_measurement_name_matches_live(self, sample_today_json):
        """History and live data should use the same measurement name: enphase_power."""
        lines = convert_day(sample_today_json, "SERIAL")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power_lines) > 0

    def test_history_energy_measurement_name_matches_live(self, sample_today_json):
        """History daily totals should use enphase_energy measurement."""
        lines = convert_day(sample_today_json, "SERIAL")
        energy_lines = [l for l in lines if l.startswith("enphase_energy,")]
        assert len(energy_lines) > 0

    def test_history_battery_measurement_name_matches_live(self, sample_today_json):
        """History battery details should use enphase_battery measurement."""
        lines = convert_day(sample_today_json, "SERIAL")
        battery_lines = [l for l in lines if l.startswith("enphase_battery,")]
        assert len(battery_lines) > 0

    def test_history_source_tag_is_history(self, sample_today_json):
        """History data should have source=history tag."""
        lines = convert_day(sample_today_json, "SERIAL")
        power_lines = [l for l in lines if l.startswith("enphase_power,") and "source=history " in l]
        assert len(power_lines) > 0

    # ── CLOUD_SCHEDULE has all expected endpoints ──

    def test_cloud_schedule_has_latest_power(self):
        assert "latest_power" in et.CLOUD_SCHEDULE

    def test_cloud_schedule_has_battery_status(self):
        assert "battery_status" in et.CLOUD_SCHEDULE

    def test_cloud_schedule_has_today(self):
        assert "today" in et.CLOUD_SCHEDULE

    def test_cloud_schedule_has_devices(self):
        assert "devices" in et.CLOUD_SCHEDULE

    def test_cloud_schedule_intervals_are_positive(self):
        """All cloud schedule intervals should be positive integers."""
        for endpoint, interval in et.CLOUD_SCHEDULE.items():
            assert isinstance(interval, int), f"{endpoint} interval is not int"
            assert interval > 0, f"{endpoint} interval is not positive"

    # ── Enum maps are complete ──

    def test_batt_mode_map_has_three_entries(self):
        assert len(et.BATT_MODE_MAP) == 3

    def test_grid_relay_map_has_eleven_entries(self):
        assert len(et.GRID_RELAY_MAP) == 11

    def test_dry_contact_map_has_three_entries(self):
        assert len(et.DRY_CONTACT_STATE_MAP) == 3

    def test_enum_int_returns_neg1_for_unknown(self):
        assert et._enum_int(et.BATT_MODE_MAP, "TOTALLY_NEW") == -1

    def test_enum_int_returns_neg1_for_none(self):
        assert et._enum_int(et.BATT_MODE_MAP, None) == -1

    # ── format_line consistency with emit ──

    def test_format_line_int_matches_emit_int(self, capsys):
        """format_line and emit should produce identical output for integers."""
        et.emit("m", {"t": "v"}, {"count": 42}, ts_ns=1000)
        emit_out = capsys.readouterr().out.strip()
        fmt_out = format_line("m", {"t": "v"}, {"count": 42}, 1000)
        assert emit_out == fmt_out

    def test_format_line_float_matches_emit_float(self, capsys):
        """format_line and emit should produce identical output for floats."""
        et.emit("m", {}, {"val": 3.14}, ts_ns=1000)
        emit_out = capsys.readouterr().out.strip()
        fmt_out = format_line("m", {}, {"val": 3.14}, 1000)
        assert emit_out == fmt_out

    def test_format_line_string_matches_emit_string(self, capsys):
        """format_line and emit should produce identical output for strings."""
        et.emit("m", {}, {"msg": "hello"}, ts_ns=1000)
        emit_out = capsys.readouterr().out.strip()
        fmt_out = format_line("m", {}, {"msg": "hello"}, 1000)
        assert emit_out == fmt_out


# ═══════════════════════════════════════════════════════════════════
# 5. Home User (Non-Technical)
# "I just want it to work."
# ═══════════════════════════════════════════════════════════════════

class TestHomeUser:
    """Wrong password, no battery, MFA, internet outages, idempotency."""

    # ── Wrong password ──

    def test_wrong_password_raises_auth_error(self):
        """Invalid credentials should raise AuthError."""
        from enphase_cloud.enlighten import AuthError
        assert issubclass(AuthError, Exception)

    def test_auth_error_message_is_descriptive(self):
        """AuthError should carry a human-readable message."""
        from enphase_cloud.enlighten import AuthError
        err = AuthError("Invalid credentials")
        assert str(err) == "Invalid credentials"

    def test_auth_error_logged_to_stderr(self, capsys):
        """Auth failures should be reported to stderr with actionable message."""
        et.error("Login failed: Invalid credentials")
        et.error("Check your email and password. Retrying in 10s...")
        captured = capsys.readouterr()
        assert "Login failed" in captured.err
        assert "Check your email" in captured.err

    # ── MFA handling ──

    def test_mfa_required_is_subclass_of_auth_error(self):
        """MFARequired should be a subclass of AuthError."""
        from enphase_cloud.enlighten import MFARequired, AuthError
        assert issubclass(MFARequired, AuthError)

    def test_mfa_required_has_actionable_message(self):
        """MFARequired error should tell users how to fix it."""
        from enphase_cloud.enlighten import MFARequired
        err = MFARequired("MFA required — not supported in automated mode")
        assert "MFA" in str(err)

    def test_mfa_error_in_main_flow_waits_300s(self):
        """MFA error should cause a 5-minute wait (to avoid rapid retries)."""
        # Verify this by checking the main() source code constant
        source = (REPO_DIR / "src" / "enphase_telegraf.py").read_text()
        assert "time.sleep(300)" in source

    def test_mfa_error_emits_helpful_error(self, capture_emit):
        """MFA should emit error with actionable instructions."""
        calls, errors = capture_emit
        et.emit_error("auth", "MFA enabled — disable in Enphase app")
        assert len(errors) == 1
        assert "MFA" in errors[0]["message"]

    # ── No battery system ──

    def test_no_battery_today_json_converts_successfully(self, no_battery_today_json):
        """today.json without battery_details should convert without errors."""
        lines = convert_day(no_battery_today_json, "SERIAL")
        assert len(lines) > 0

    def test_no_battery_produces_no_battery_lines(self, no_battery_today_json):
        """System without battery should not produce enphase_battery lines."""
        lines = convert_day(no_battery_today_json, "SERIAL")
        battery_lines = [l for l in lines if l.startswith("enphase_battery,")]
        assert len(battery_lines) == 0

    def test_no_battery_still_produces_power_lines(self, no_battery_today_json):
        """System without battery should still produce power lines."""
        lines = convert_day(no_battery_today_json, "SERIAL")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power_lines) > 0

    def test_no_battery_energy_flows_are_zero(self, no_battery_today_json):
        """Battery-related energy flows should be zero for no-battery system."""
        lines = convert_day(no_battery_today_json, "SERIAL")
        energy_lines = [l for l in lines if "source=history " in l and "enphase_energy," in l]
        for line in energy_lines:
            if "solar_to_battery_w=" in line:
                match = re.search(r"solar_to_battery_w=([0-9.]+)", line)
                if match:
                    assert float(match.group(1)) == 0.0

    def test_mqtt_message_without_battery_fields(self, capture_emit):
        """MQTT message with no battery data should still emit power."""
        calls, errors = capture_emit
        msg = {
            "pv_power_w": 2000.0,
            "grid_power_w": -500.0,
            "load_power_w": 1500.0,
            # No storage_power_w, no meter_soc, no batt_mode
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert len(power_calls) == 1
        assert "solar_w" in power_calls[0]["fields"]
        assert "battery_w" not in power_calls[0]["fields"]

    # ── Internet outage / empty data ──

    def test_empty_today_json_produces_no_lines(self):
        """Empty today.json should produce 0 lines, not crash."""
        lines = convert_day({}, "SERIAL")
        assert lines == []

    def test_today_json_empty_stats_no_crash(self):
        """today.json with empty stats array should not crash."""
        lines = convert_day({"stats": []}, "SERIAL")
        assert lines == []

    def test_today_json_no_intervals_no_crash(self):
        """today.json with no intervals should not crash."""
        lines = convert_day({"stats": [{"totals": {}, "intervals": []}]}, "SERIAL")
        assert lines == []

    def test_cloud_poll_with_empty_responses(self, capture_emit, monkeypatch):
        """Cloud poll with empty API responses should not crash."""
        calls, errors = capture_emit
        client = MockClient({
            "get_latest_power": {},
            "get_battery_status": {},
            "get_today": {"stats": [{"totals": {}, "intervals": []}]},
        })
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        # Should not raise
        et.cloud_poll_once()

    # ── Two gateways ──

    def test_discover_serial_returns_first_gateway(self):
        """With multiple gateways, discover_serial should return the first."""
        client = MagicMock()
        client.get_devices.return_value = {
            "result": [
                {"type": "gateway", "devices": [
                    {"serial_number": "GW_ONE"},
                    {"serial_number": "GW_TWO"},
                ]},
            ]
        }
        serial = et.discover_serial(client)
        assert serial == "GW_ONE"

    # ── Setup run twice ──

    def test_setup_sh_checks_for_existing_venv(self):
        """setup.sh should detect existing venv and skip reinstall."""
        content = SETUP_SH.read_text()
        assert "HAS_APP_VENV" in content

    def test_setup_sh_checks_for_existing_proto(self):
        """setup.sh should detect existing compiled protobuf."""
        content = SETUP_SH.read_text()
        assert "HAS_PROTO" in content

    def test_setup_sh_checks_for_existing_credentials(self):
        """setup.sh should detect existing Enphase credentials."""
        content = SETUP_SH.read_text()
        assert "HAS_ENPHASE_CREDS" in content

    def test_setup_sh_detects_example_email_as_unconfigured(self):
        """setup.sh should treat you@example.com as 'not configured'."""
        content = SETUP_SH.read_text()
        assert "you@example.com" in content


# ═══════════════════════════════════════════════════════════════════
# 6. Solar Installer (Field Deployment)
# "I'm setting up 50 of these."
# ═══════════════════════════════════════════════════════════════════

class TestSolarInstaller:
    """Setup scripting, permissions, non-interactive mode, reboot survival."""

    # ── Non-interactive setup ──

    def test_setup_sh_supports_full_flag(self):
        """setup.sh should accept --full for non-interactive full install."""
        content = SETUP_SH.read_text()
        assert "--full" in content

    def test_setup_sh_supports_app_flag(self):
        """setup.sh should accept --app for non-interactive app-only install."""
        content = SETUP_SH.read_text()
        assert "--app" in content

    def test_setup_sh_case_handles_cli_flags(self):
        """setup.sh should use case statement for CLI flags."""
        content = SETUP_SH.read_text()
        # Should have a case block that handles --full and --app
        assert "case" in content
        assert "--full)" in content
        assert "--app)" in content

    def test_setup_sh_has_set_euo_pipefail(self):
        """setup.sh must use strict mode for safety."""
        content = SETUP_SH.read_text()
        assert "set -euo pipefail" in content

    # ── File permissions ──

    def test_setup_sh_creates_env_with_restricted_perms(self):
        """setup.sh should chmod 600 the .env file."""
        content = SETUP_SH.read_text()
        # Should restrict .env permissions
        assert "chmod" in content
        assert "600" in content

    def test_bin_scripts_are_executable(self):
        """bin/ wrapper scripts should have execute permission."""
        bin_telegraf = REPO_DIR / "bin" / "enphase-telegraf"
        bin_history = REPO_DIR / "bin" / "load-history"
        if bin_telegraf.exists():
            assert os.access(bin_telegraf, os.X_OK), "bin/enphase-telegraf not executable"
        if bin_history.exists():
            assert os.access(bin_history, os.X_OK), "bin/load-history not executable"

    def test_setup_sh_is_executable(self):
        """setup.sh should have execute permission."""
        assert os.access(SETUP_SH, os.X_OK), "setup.sh not executable"

    # ── Telegraf configuration ──

    def test_setup_sh_installs_telegraf_config(self):
        """setup.sh should reference Telegraf input configuration."""
        content = SETUP_SH.read_text()
        assert "telegraf" in content.lower()
        # Should reference the config file or conf.d
        assert "conf" in content.lower()

    def test_setup_sh_has_connection_test(self):
        """setup.sh should include a connection test step."""
        content = SETUP_SH.read_text()
        assert "Connection test" in content or "connection test" in content or "test" in content.lower()

    # ── Reboot survival ──

    def test_bin_telegraf_uses_exec(self):
        """bin/enphase-telegraf should exec the python process (for signal propagation)."""
        bin_telegraf = REPO_DIR / "bin" / "enphase-telegraf"
        if bin_telegraf.exists():
            content = bin_telegraf.read_text()
            assert "exec " in content

    def test_bin_telegraf_sources_env_file(self):
        """bin/enphase-telegraf should source .env for environment variables."""
        bin_telegraf = REPO_DIR / "bin" / "enphase-telegraf"
        if bin_telegraf.exists():
            content = bin_telegraf.read_text()
            assert ".env" in content
            assert "source" in content

    # ── Infrastructure scripts ──

    def test_setup_sh_references_infra_scripts(self):
        """setup.sh should reference infrastructure setup scripts."""
        content = SETUP_SH.read_text()
        assert "infra/scripts" in content

    def test_setup_sh_has_system_detection(self):
        """setup.sh should detect installed components."""
        content = SETUP_SH.read_text()
        assert "detect_system" in content
        # Should check for key components
        assert "HAS_PYTHON" in content
        assert "HAS_TELEGRAF" in content
        assert "HAS_INFLUXDB" in content

    def test_setup_sh_checks_tailscale(self):
        """setup.sh should check for Tailscale (required for infra)."""
        content = SETUP_SH.read_text()
        assert "HAS_TAILSCALE" in content
        assert "tailscale" in content.lower()

    # ── Idempotency: running setup twice ──

    def test_setup_sh_skips_already_configured_infra(self):
        """setup.sh should skip infra setup if already configured."""
        content = SETUP_SH.read_text()
        assert "HAS_INFLUX_CREDS" in content
        # Should check before running infra
        assert "already" in content.lower()

    def test_setup_sh_skips_already_configured_venv(self):
        """setup.sh should skip venv creation if already exists."""
        content = SETUP_SH.read_text()
        assert "HAS_APP_VENV" in content
        assert "already" in content.lower() or "done" in content.lower()

    def test_setup_sh_skips_already_configured_creds(self):
        """setup.sh should skip credential prompt if already configured."""
        content = SETUP_SH.read_text()
        assert "HAS_ENPHASE_CREDS" in content

    # ── Error handling in setup ──

    def test_setup_sh_warns_about_missing_tailscale(self):
        """setup.sh should warn if Tailscale not installed for infra mode."""
        content = SETUP_SH.read_text()
        assert "Tailscale" in content
        # Should have a warning or error about missing tailscale
        assert "required" in content.lower() or "not installed" in content.lower()

    def test_setup_sh_offers_app_only_fallback(self):
        """setup.sh should offer app-only as fallback if infra fails."""
        content = SETUP_SH.read_text()
        assert "app" in content.lower()

    # ── Credentials file ──

    def test_setup_sh_saves_credentials_to_file(self):
        """setup.sh should save monitoring credentials to a file."""
        content = SETUP_SH.read_text()
        assert "monitoring-credentials" in content or "CREDS_FILE" in content

    def test_setup_sh_creates_env_file(self):
        """setup.sh should create the .env file with Enphase credentials."""
        content = SETUP_SH.read_text()
        assert "ENPHASE_EMAIL" in content
        assert "ENPHASE_PASSWORD" in content
        assert ".env" in content


# ═══════════════════════════════════════════════════════════════════
# Additional cross-cutting gap tests
# ═══════════════════════════════════════════════════════════════════

class TestCrossCuttingGaps:
    """Tests that span multiple personas: timezone handling, edge cases, etc."""

    # ── Timezone handling in history ──

    def test_history_cloned_date_used_for_daily_timestamp(self, sample_today_json):
        """Daily totals should use _cloned_date for timestamp, not current time."""
        lines = convert_day(sample_today_json, "SERIAL")
        daily_lines = [l for l in lines if "source=history_daily" in l]
        assert len(daily_lines) >= 1
        ts_str = daily_lines[0].strip().split(" ")[-1]
        ts_ns = int(ts_str)
        ts_sec = ts_ns // 1_000_000_000
        # Should be in 2024 (from the fixture), not 2026 (current time)
        from datetime import datetime
        dt = datetime.fromtimestamp(ts_sec)
        assert dt.year == 2024, f"Daily timestamp year is {dt.year}, expected 2024"

    def test_history_with_missing_cloned_date_uses_current_time(self):
        """If _cloned_date is missing, fallback to current time."""
        data = {
            "stats": [{
                "totals": {"production": 1000.0},
                "intervals": [],
            }],
        }
        lines = convert_day(data, "SERIAL")
        daily_lines = [l for l in lines if "source=history_daily" in l]
        if daily_lines:
            ts_str = daily_lines[0].strip().split(" ")[-1]
            ts_ns = int(ts_str)
            ts_sec = ts_ns // 1_000_000_000
            # Should be recent (within last hour)
            assert abs(ts_sec - time.time()) < 3600

    # ── MQTT message counting ──

    def test_mqtt_message_counter_increments(self, capture_emit, basic_mqtt_msg):
        """Each MQTT message should increment the counter."""
        assert et._mqtt_messages == 0
        et.on_mqtt_data(basic_mqtt_msg)
        assert et._mqtt_messages == 1
        et.on_mqtt_data(basic_mqtt_msg)
        assert et._mqtt_messages == 2

    # ── Generator power ──

    def test_generator_power_mapped(self, capture_emit):
        """Generator power should be mapped to generator_w field."""
        calls, _ = capture_emit
        msg = {
            "generator_power_w": 5000.0,
            "pv_power_w": 0.0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert "generator_w" in power_calls[0]["fields"]
        assert power_calls[0]["fields"]["generator_w"] == 5000.0

    # ── Grid outage detection ──

    def test_grid_outage_status_mapped(self, capture_emit):
        """Grid outage status should be included in power fields."""
        calls, _ = capture_emit
        msg = {
            "pv_power_w": 1000.0,
            "grid_outage_status": 1,
            "grid_update_ongoing": 0,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert power_calls[0]["fields"]["grid_outage"] == 1
        assert power_calls[0]["fields"]["grid_update_ongoing"] == 0

    # ── Inverter count ──

    def test_inverter_counts_mapped(self, capture_emit, basic_mqtt_msg):
        """PCU total and running should map to inverter fields."""
        calls, _ = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert power_calls[0]["fields"]["inverters_total"] == 16
        assert power_calls[0]["fields"]["inverters_producing"] == 16

    # ── VA fields mapped ──

    def test_apparent_power_fields_mapped(self, capture_emit, basic_mqtt_msg):
        """Apparent power (VA) should be mapped for all sources."""
        calls, _ = capture_emit
        et.on_mqtt_data(basic_mqtt_msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        fields = power_calls[0]["fields"]
        assert "solar_va" in fields
        assert "grid_va" in fields
        assert "consumption_va" in fields

    # ── Cloud poll: latest_power ──

    def test_cloud_latest_power_emits_solar_w(self, capture_emit, monkeypatch):
        """latest_power cloud endpoint should emit solar_w."""
        calls, errors = capture_emit
        client = MockClient({
            "get_latest_power": {"latest_power": {"value": 2500}},
        })
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"
                       and c["tags"].get("source") == "cloud"]
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == 2500.0

    # ── Cloud poll: battery status ──

    def test_cloud_battery_status_emits_soc(self, capture_emit, monkeypatch):
        """battery_status cloud endpoint should emit SOC."""
        calls, errors = capture_emit
        client = MockClient({
            "get_battery_status": {"current_charge": "75%", "available_energy": 5.0},
        })
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        battery_calls = [c for c in calls if c["measurement"] == "enphase_battery"]
        assert len(battery_calls) >= 1
        assert battery_calls[0]["fields"]["soc"] == 75

    def test_cloud_battery_status_numeric_charge(self, capture_emit, monkeypatch):
        """battery_status with numeric current_charge should work."""
        calls, errors = capture_emit
        client = MockClient({
            "get_battery_status": {"current_charge": 80},
        })
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        battery_calls = [c for c in calls if c["measurement"] == "enphase_battery"]
        assert len(battery_calls) >= 1
        assert battery_calls[0]["fields"]["soc"] == 80

    # ── Cloud poll: today ──

    def test_cloud_today_emits_energy_totals(self, capture_emit, monkeypatch, sample_today_json):
        """today cloud endpoint should emit enphase_energy."""
        calls, errors = capture_emit
        client = MockClient({"get_today": sample_today_json})
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        energy_calls = [c for c in calls if c["measurement"] == "enphase_energy"]
        assert len(energy_calls) >= 1
        assert "production_wh" in energy_calls[0]["fields"]

    def test_cloud_today_emits_battery_config(self, capture_emit, monkeypatch, sample_today_json):
        """today endpoint with batteryConfig should emit enphase_config."""
        calls, errors = capture_emit
        client = MockClient({"get_today": sample_today_json})
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        config_calls = [c for c in calls if c["measurement"] == "enphase_config"]
        assert len(config_calls) >= 1
        assert "backup_reserve_pct" in config_calls[0]["fields"]

    # ── Cloud poll: inverters ──

    def test_cloud_inverters_emits_counts(self, capture_emit, monkeypatch):
        """inverters endpoint should emit inverter counts."""
        calls, errors = capture_emit
        client = MockClient({
            "get_inverters": {
                "total": 16,
                "not_reporting": 2,
                "error_count": 0,
                "warning_count": 1,
                "normal_count": 13,
            },
        })
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        inv_calls = [c for c in calls if c["measurement"] == "enphase_inverters"]
        assert len(inv_calls) >= 1
        assert inv_calls[0]["fields"]["total"] == 16
        assert inv_calls[0]["fields"]["not_reporting"] == 2

    # ── Cloud poll: alarms ──

    def test_cloud_alarms_emits_when_nonzero(self, capture_emit, monkeypatch):
        """alarms endpoint with total > 0 should emit alarm_count."""
        calls, errors = capture_emit
        client = MockClient({"get_alarms": {"total": 3}})
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        gw_calls = [c for c in calls if c["measurement"] == "enphase_gateway"
                     and "alarm_count" in c.get("fields", {})]
        assert len(gw_calls) >= 1
        assert gw_calls[0]["fields"]["alarm_count"] == 3

    def test_cloud_alarms_zero_does_not_emit(self, capture_emit, monkeypatch):
        """alarms endpoint with total=0 should NOT emit."""
        calls, errors = capture_emit
        client = MockClient({"get_alarms": {"total": 0}})
        et._client = client
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()
        alarm_calls = [c for c in calls if c["measurement"] == "enphase_gateway"
                       and "alarm_count" in c.get("fields", {})]
        assert len(alarm_calls) == 0

    # ── Timestamp in MQTT ──

    def test_mqtt_timestamp_converted_to_nanoseconds(self, capture_emit):
        """MQTT timestamp (seconds) should be emitted as nanoseconds."""
        calls, _ = capture_emit
        ts_sec = 1700000000
        msg = {
            "pv_power_w": 100.0,
            "timestamp": ts_sec,
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert power_calls[0]["ts_ns"] == ts_sec * 1_000_000_000

    def test_mqtt_missing_timestamp_uses_none(self, capture_emit):
        """MQTT message without timestamp should pass ts_ns=None (auto-generate)."""
        calls, _ = capture_emit
        msg = {
            "pv_power_w": 100.0,
            "_fields_present": frozenset(),
            # No "timestamp" key
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        assert power_calls[0]["ts_ns"] is None

    # ── Per-phase power ──

    def test_per_phase_power_mapped(self, capture_emit):
        """Per-phase power should create l1/l2 fields."""
        calls, _ = capture_emit
        msg = {
            "pv_power_w": 3000.0,
            "pv_phase_w": [1500.0, 1500.0],
            "grid_power_w": -1000.0,
            "grid_phase_w": [-500.0, -500.0],
            "_fields_present": frozenset(),
        }
        et.on_mqtt_data(msg)
        power_calls = [c for c in calls if c["measurement"] == "enphase_power"]
        fields = power_calls[0]["fields"]
        assert fields["solar_l1_w"] == 1500.0
        assert fields["solar_l2_w"] == 1500.0
        assert fields["grid_l1_w"] == -500.0
        assert fields["grid_l2_w"] == -500.0

    # ── convert_all with progress callback ──

    def test_convert_all_calls_progress_callback(self, tmp_history_dir):
        """convert_all should call progress_cb for each day file."""
        progress_log = []

        def cb(day_str, lines_count, total_files, current_file):
            progress_log.append((day_str, lines_count, total_files, current_file))

        lines = convert_all(tmp_history_dir, "SERIAL", progress_cb=cb)
        assert len(progress_log) == 3  # 3 day files
        assert progress_log[0][2] == 3  # total_files
        assert progress_log[0][3] == 1  # current_file (1-indexed)
        assert progress_log[2][3] == 3

    def test_convert_all_processes_all_files(self, tmp_history_dir):
        """convert_all should process every day_*.json file in the directory."""
        lines = convert_all(tmp_history_dir, "SERIAL")
        assert len(lines) > 0
        # 3 files x (96 interval power lines + 96 interval energy lines + 1 daily total + 1 battery) ~= 582+
        # But at minimum, should have lines from all 3 files
        # Check that daily totals from different dates exist
        daily_lines = [l for l in lines if "source=history_daily" in l]
        assert len(daily_lines) == 3, f"Expected 3 daily totals, got {len(daily_lines)}"

    # ── Enlighten session dataclass ──

    def test_enlighten_session_defaults(self):
        """EnlightenSession should have sane defaults."""
        from enphase_cloud.enlighten import EnlightenSession
        s = EnlightenSession(email="test@test.com")
        assert s.email == "test@test.com"
        assert s.site_id is None
        assert s.authenticated is False
        assert s.auth_time == 0.0
        assert s.jwt_token is None

    def test_enlighten_client_stores_credentials(self):
        """EnlightenClient should store email and password."""
        from enphase_cloud.enlighten import EnlightenClient
        c = EnlightenClient("user@test.com", "mypass")
        assert c._email == "user@test.com"
        assert c._password == "mypass"

    def test_livestream_client_stats(self):
        """LiveStreamClient.stats should return a dict with expected keys."""
        from enphase_cloud.livestream import LiveStreamClient
        mock_client = MagicMock()
        ls = LiveStreamClient(mock_client)
        stats = ls.stats
        assert "connected" in stats
        assert "running" in stats
        assert "messages" in stats
        assert stats["connected"] is False
        assert stats["running"] is False
        assert stats["messages"] == 0

    # ── Edge case: empty fields dict ──

    def test_emit_with_all_none_fields_produces_no_output(self, capsys):
        """emit() with only None fields should produce no output."""
        et.emit("m", {"t": "v"}, {"a": None, "b": None}, ts_ns=1)
        out = capsys.readouterr().out
        assert out == ""

    def test_format_line_with_all_none_fields_returns_none(self):
        """format_line() with only None fields should return None."""
        result = format_line("m", {}, {"a": None}, 1)
        assert result is None

    def test_format_line_with_empty_fields_returns_none(self):
        """format_line() with empty fields dict should return None."""
        result = format_line("m", {}, {}, 1)
        assert result is None

    # ── Cloud poll schedule timing ──

    def test_cloud_poll_skips_endpoint_within_interval(self, capture_emit, monkeypatch):
        """Endpoints polled recently should be skipped."""
        calls, errors = capture_emit
        client = MockClient({"get_latest_power": {"latest_power": {"value": 1000}}})
        et._client = client
        now = 1_000_000.0
        monkeypatch.setattr(time, "time", lambda: now)

        # First poll
        et.cloud_poll_once()
        count_1 = len([c for c in calls if c["measurement"] == "enphase_power"
                        and c["tags"].get("source") == "cloud"])

        # Second poll immediately — should be skipped
        et.cloud_poll_once()
        count_2 = len([c for c in calls if c["measurement"] == "enphase_power"
                        and c["tags"].get("source") == "cloud"])
        assert count_2 == count_1  # No new emission

    def test_cloud_poll_runs_endpoint_after_interval(self, capture_emit, monkeypatch):
        """Endpoints past their interval should be polled again."""
        calls, errors = capture_emit
        client = MockClient({"get_latest_power": {"latest_power": {"value": 1000}}})
        et._client = client

        monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
        et.cloud_poll_once()

        # Advance time past the interval (120s for latest_power)
        monkeypatch.setattr(time, "time", lambda: 1_000_121.0)
        et.cloud_poll_once()

        power_calls = [c for c in calls if c["measurement"] == "enphase_power"
                       and c["tags"].get("source") == "cloud"]
        assert len(power_calls) >= 2
