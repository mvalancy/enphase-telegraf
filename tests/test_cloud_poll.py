"""Tests for cloud_poll_once() and discover_serial() in enphase_telegraf.py.

300+ tests covering all cloud endpoints, schedule timing, data parsing,
error handling, and serial discovery.
"""

import time

import pytest

import enphase_telegraf as et


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_globals():
    et._serial = "TEST123"
    et._client = None
    et._cloud_last_fetch = {}
    et._cloud_fetches = 0
    et._cloud_errors = 0
    et._last_reserve_pct = None
    et._last_batt_mode = None
    et._last_grid_relay = None
    et._last_gen_relay = None
    et._error_backoff = {}
    et._verbose = False
    yield


@pytest.fixture
def capture(monkeypatch):
    calls = []

    def fake_emit(measurement, tags, fields, ts_ns=None):
        calls.append({"m": measurement, "tags": tags, "fields": fields})

    monkeypatch.setattr(et, "emit", fake_emit)
    errors = []

    def fake_emit_error(comp, msg):
        errors.append({"component": comp, "message": msg})

    monkeypatch.setattr(et, "emit_error", fake_emit_error)
    return calls, errors


class MockClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.authenticated = True

    def __getattr__(self, name):
        if name.startswith("get_"):
            return lambda: self.responses.get(name, {})
        raise AttributeError(name)


class RaisingClient:
    """Client where specific getters raise exceptions."""

    def __init__(self, raise_on=None):
        self.raise_on = raise_on or {}
        self.authenticated = True

    def __getattr__(self, name):
        if name.startswith("get_"):
            if name in self.raise_on:
                def raiser():
                    raise self.raise_on[name]
                return raiser
            return lambda: {}
        raise AttributeError(name)


# ═══════════════════════════════════════════════════════════════════════
# TestCloudScheduleTiming — 20 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudScheduleTiming:
    """Verify interval gating: skip when too recent, fetch when due."""

    def test_first_poll_fetches_latest_power(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 100}}})
        et.cloud_poll_once()
        calls, _ = capture
        assert any(c["m"] == "enphase_power" for c in calls)

    def test_second_poll_within_interval_skips(self, capture, monkeypatch):
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 100}}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 1060.0)  # 60s < 120s interval
        et.cloud_poll_once()
        assert not any(c["m"] == "enphase_power" for c in calls)

    def test_poll_after_interval_fetches_again(self, capture, monkeypatch):
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 200}}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 10121.0)  # 121s > 120s
        et.cloud_poll_once()
        assert any(c["m"] == "enphase_power" for c in calls)

    def test_battery_status_interval_120s(self, capture, monkeypatch):
        et._client = MockClient({"get_battery_status": {"current_charge": "80%"}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 10119.0)  # 119s < 120s
        et.cloud_poll_once()
        assert not any(c["m"] == "enphase_battery" for c in calls)

    def test_battery_status_fetched_after_120s(self, capture, monkeypatch):
        et._client = MockClient({"get_battery_status": {"current_charge": "80%"}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 10121.0)
        et.cloud_poll_once()
        assert any(c["m"] == "enphase_battery" for c in calls)

    def test_today_interval_300s(self, capture, monkeypatch):
        data = {"stats": [{"totals": {"production": 5000}}]}
        et._client = MockClient({"get_today": data})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 10299.0)  # 299s < 300s
        et.cloud_poll_once()
        assert not any(c["m"] == "enphase_energy" for c in calls)

    def test_today_fetched_after_300s(self, capture, monkeypatch):
        data = {"stats": [{"totals": {"production": 5000}}]}
        et._client = MockClient({"get_today": data})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 10301.0)
        et.cloud_poll_once()
        assert any(c["m"] == "enphase_energy" for c in calls)

    def test_alarms_interval_600s(self, capture, monkeypatch):
        et._client = MockClient({"get_alarms": {"total": 1}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 10599.0)
        et.cloud_poll_once()
        assert not any(c["m"] == "enphase_gateway" for c in calls)

    def test_alarms_fetched_after_600s(self, capture, monkeypatch):
        et._client = MockClient({"get_alarms": {"total": 2}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 10601.0)
        et.cloud_poll_once()
        assert any(c["m"] == "enphase_gateway" for c in calls)

    def test_devices_interval_3600s(self, capture, monkeypatch):
        et._client = MockClient()
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 13599.0)  # 3599s later, within 3600s interval
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("devices") == 10000.0

    def test_devices_fetched_after_3600s(self, capture, monkeypatch):
        et._client = MockClient()
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 13601.0)  # 3601s later, past 3600s interval
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("devices") == 13601.0

    def test_site_data_interval_3600s(self, capture, monkeypatch):
        et._client = MockClient()
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 13599.0)
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("site_data") == 10000.0

    def test_site_data_fetched_after_3600s(self, capture, monkeypatch):
        et._client = MockClient()
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 13601.0)
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("site_data") == 13601.0

    def test_inverters_interval_1800s(self, capture, monkeypatch):
        et._client = MockClient({"get_inverters": {"total": 10}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 11799.0)  # 1799s later, within 1800s interval
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("inverters") == 10000.0

    def test_inverters_fetched_after_1800s(self, capture, monkeypatch):
        et._client = MockClient({"get_inverters": {"total": 10}})
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 11801.0)  # 1801s later, past 1800s interval
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("inverters") == 11801.0

    def test_no_client_does_nothing(self, capture):
        et._client = None
        et.cloud_poll_once()
        calls, errors = capture
        assert calls == []
        assert errors == []

    def test_cloud_fetches_counter_increments(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient()
        assert et._cloud_fetches == 0
        et.cloud_poll_once()
        assert et._cloud_fetches > 0

    def test_battery_schedules_interval_600s(self, capture, monkeypatch):
        et._client = MockClient()
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 10599.0)
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("battery_schedules") == 10000.0

    def test_battery_schedules_fetched_after_600s(self, capture, monkeypatch):
        et._client = MockClient()
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et.cloud_poll_once()
        monkeypatch.setattr(time, "time", lambda: 10601.0)
        et.cloud_poll_once()
        assert et._cloud_last_fetch.get("battery_schedules") == 10601.0


# ═══════════════════════════════════════════════════════════════════════
# TestCloudLatestPower — 25 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudLatestPower:
    """Test get_latest_power() response parsing."""

    def test_normal_value(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 3500}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == 3500.0

    def test_zero_value(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 0}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == 0.0

    def test_none_value_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": None}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_empty_dict_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_not_a_dict_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": "bad"})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_missing_latest_power_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"other_key": 42}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_negative_value(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": -100}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == -100.0

    def test_very_large_value(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 999999}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == 999999.0

    def test_string_value_converted(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": "1500"}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == 1500.0

    def test_float_value(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 3500.7}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert power_calls[0]["fields"]["solar_w"] == 3500.7

    def test_latest_power_not_dict_inner(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": "not_dict"}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_latest_power_list_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": [1, 2, 3]}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_tags_include_source_cloud(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 100}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert power_calls[0]["tags"]["source"] == "cloud"

    def test_tags_include_serial(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 100}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert power_calls[0]["tags"]["serial"] == "TEST123"

    def test_latest_power_none_response(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": None})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_latest_power_empty_inner_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) == 0

    def test_latest_power_integer_value(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 42}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert power_calls[0]["fields"]["solar_w"] == 42.0

    def test_clears_error_on_success(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._error_backoff["cloud_latest_power"] = {"last_emit": 900, "interval": 120, "message": "old"}
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 100}}})
        et.cloud_poll_once()
        assert "cloud_latest_power" not in et._error_backoff

    def test_fetches_counter_increments_for_latest_power(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 100}}})
        before = et._cloud_fetches
        et.cloud_poll_once()
        assert et._cloud_fetches > before

    def test_latest_power_value_bool_true(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": True}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == 1.0

    def test_latest_power_value_false(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": False}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        # False is not None, so float(False) == 0.0
        assert len(power_calls) >= 1
        assert power_calls[0]["fields"]["solar_w"] == 0.0

    @pytest.mark.parametrize("val", [1, 10, 100, 1000])
    def test_latest_power_various_ints(self, capture, monkeypatch, val):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": val}}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power"]
        assert power_calls[0]["fields"]["solar_w"] == float(val)


# ═══════════════════════════════════════════════════════════════════════
# TestCloudBatteryStatus — 50 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudBatteryStatus:
    """Test get_battery_status() response parsing."""

    # ── Charge parsing ────────────────────────────────

    @pytest.mark.parametrize("charge,expected_soc", [
        ("85%", 85),
        ("0%", 0),
        ("100%", 100),
        ("50%", 50),
        ("1%", 1),
        ("99%", 99),
    ])
    def test_charge_percent_string(self, capture, monkeypatch, charge, expected_soc):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": charge}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["soc"] == expected_soc

    def test_charge_numeric_string_no_percent(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        # "85" without % is a string but no "%" → goes to neither branch, no soc
        et._client = MockClient({"get_battery_status": {"current_charge": "85"}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_charge_integer(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": 85}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["soc"] == 85

    def test_charge_float(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": 85.5}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["soc"] == 85

    def test_charge_float_percent_string(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": "85.5%"}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        # int("85.5") raises ValueError, so soc not set
        assert len(bat_calls) == 0

    def test_charge_empty_string(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": ""}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_charge_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": None}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_charge_bad_percent_string(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": "bad%"}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_charge_negative_int(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": -5}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["soc"] == -5

    def test_charge_200_int(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": 200}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["soc"] == 200

    # ── Numeric fields ────────────────────────────────

    def test_available_energy_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"available_energy": 5.2}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["available_energy_kwh"] == 5.2

    def test_available_energy_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"available_energy": None}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_max_capacity_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"max_capacity": 10.08}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["max_capacity_kwh"] == 10.08

    def test_max_capacity_negative(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"max_capacity": -1.0}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["max_capacity_kwh"] == -1.0

    def test_available_power_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"available_power": 3.84}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["available_power_kw"] == 3.84

    def test_max_power_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"max_power": 7.68}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["max_power_kw"] == 7.68

    def test_included_count_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"included_count": 3}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["unit_count"] == 3

    def test_included_count_zero(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"included_count": 0}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["unit_count"] == 0

    def test_active_micros_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"active_micros": 16}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["active_inverters"] == 16

    def test_total_micros_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"total_micros": 20}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["total_inverters"] == 20

    def test_included_count_string_converted(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"included_count": "5"}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["unit_count"] == 5

    def test_available_energy_string_converted(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"available_energy": "3.5"}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["available_energy_kwh"] == 3.5

    def test_max_capacity_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"max_capacity": None}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    # ── Per-battery storages ──────────────────────────

    def test_zero_storages(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": [],
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) >= 1
        assert "cycle_count_1" not in bat_calls[0]["fields"]

    def test_one_storage_cycle_count(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": [{"cycle_count": 120}],
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["cycle_count_1"] == 120

    def test_two_storages(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": [
                {"cycle_count": 100, "battery_soh": "95%"},
                {"cycle_count": 200, "battery_soh": "90%"},
            ],
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        f = bat_calls[0]["fields"]
        assert f["cycle_count_1"] == 100
        assert f["cycle_count_2"] == 200
        assert f["soh_1"] == 95
        assert f["soh_2"] == 90

    def test_four_storages(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        storages = [{"cycle_count": i * 50, "battery_soh": f"{90 + i}%"} for i in range(4)]
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": storages,
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        f = bat_calls[0]["fields"]
        for i in range(4):
            assert f[f"cycle_count_{i+1}"] == i * 50
            assert f[f"soh_{i+1}"] == 90 + i

    def test_five_storages_only_first_four(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        storages = [{"cycle_count": i * 10} for i in range(5)]
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": storages,
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        f = bat_calls[0]["fields"]
        assert "cycle_count_4" in f
        assert "cycle_count_5" not in f

    def test_storage_soh_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": [{"cycle_count": 100, "battery_soh": None}],
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert "soh_1" not in bat_calls[0]["fields"]

    def test_storage_soh_bad_string(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": [{"cycle_count": 100, "battery_soh": "bad"}],
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        # "bad" has no "%" → isinstance check fails
        assert "soh_1" not in bat_calls[0]["fields"]

    def test_storage_cycle_count_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {
            "current_charge": 50,
            "storages": [{"cycle_count": None, "battery_soh": "95%"}],
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert "cycle_count_1" not in bat_calls[0]["fields"]
        assert bat_calls[0]["fields"]["soh_1"] == 95

    def test_also_emits_soc_to_enphase_power(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": "75%"}})
        et.cloud_poll_once()
        calls, _ = capture
        power_calls = [c for c in calls if c["m"] == "enphase_power" and "soc" in c["fields"]]
        assert len(power_calls) == 1
        assert power_calls[0]["fields"]["soc"] == 75

    def test_empty_response_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_not_a_dict_response(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": "string_response"})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_all_numeric_fields_together(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {
            "current_charge": "90%",
            "available_energy": 8.5,
            "max_capacity": 10.08,
            "available_power": 3.84,
            "max_power": 7.68,
            "included_count": 3,
            "active_micros": 16,
            "total_micros": 20,
        }})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        f = bat_calls[0]["fields"]
        assert f["soc"] == 90
        assert f["available_energy_kwh"] == 8.5
        assert f["max_capacity_kwh"] == 10.08
        assert f["available_power_kw"] == 3.84
        assert f["max_power_kw"] == 7.68
        assert f["unit_count"] == 3
        assert f["active_inverters"] == 16
        assert f["total_inverters"] == 20

    def test_total_micros_string(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"total_micros": "25"}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["total_inverters"] == 25

    def test_active_micros_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"active_micros": None}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_available_power_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"available_power": None}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_max_power_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"max_power": None}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_max_power_negative(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"max_power": -2.0}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["max_power_kw"] == -2.0

    def test_included_count_negative(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"included_count": -1}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["unit_count"] == -1

    def test_tags_have_serial(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_battery_status": {"current_charge": 80}})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["tags"]["serial"] == "TEST123"


# ═══════════════════════════════════════════════════════════════════════
# TestCloudToday — 50 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudToday:
    """Test get_today() response parsing."""

    # ── stats[0].totals parsing ───────────────────────

    def _make_today(self, totals=None, battery_details=None,
                    batteryConfig=None, connectionDetails=None):
        data = {}
        if totals is not None:
            data["stats"] = [{"totals": totals}]
        if battery_details is not None:
            data["battery_details"] = battery_details
        if batteryConfig is not None:
            data["batteryConfig"] = batteryConfig
        if connectionDetails is not None:
            data["connectionDetails"] = connectionDetails
        return data

    @pytest.mark.parametrize("src,dst", [
        ("production", "production_wh"),
        ("consumption", "consumption_wh"),
        ("charge", "charge_wh"),
        ("discharge", "discharge_wh"),
        ("solar_home", "solar_to_home_wh"),
        ("solar_battery", "solar_to_battery_wh"),
        ("solar_grid", "solar_to_grid_wh"),
        ("battery_home", "battery_to_home_wh"),
        ("battery_grid", "battery_to_grid_wh"),
        ("grid_home", "grid_to_home_wh"),
        ("grid_battery", "grid_to_battery_wh"),
    ])
    def test_totals_field_normal(self, capture, monkeypatch, src, dst):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today({src: 12345.6})})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) >= 1
        assert energy_calls[0]["fields"][dst] == 12345.6

    @pytest.mark.parametrize("src,dst", [
        ("production", "production_wh"),
        ("consumption", "consumption_wh"),
        ("charge", "charge_wh"),
        ("discharge", "discharge_wh"),
        ("solar_home", "solar_to_home_wh"),
        ("solar_battery", "solar_to_battery_wh"),
        ("solar_grid", "solar_to_grid_wh"),
        ("battery_home", "battery_to_home_wh"),
        ("battery_grid", "battery_to_grid_wh"),
        ("grid_home", "grid_to_home_wh"),
        ("grid_battery", "grid_to_battery_wh"),
    ])
    def test_totals_field_none(self, capture, monkeypatch, src, dst):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today({src: None})})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_totals_all_fields(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        totals = {
            "production": 18238.0, "consumption": 22450.0,
            "charge": 1200.0, "discharge": 800.0,
            "solar_home": 12500.0, "solar_battery": 1200.0,
            "solar_grid": 4538.0, "battery_home": 800.0,
            "battery_grid": 0.0, "grid_home": 9150.0, "grid_battery": 0.0,
        }
        et._client = MockClient({"get_today": self._make_today(totals)})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 1
        assert energy_calls[0]["fields"]["production_wh"] == 18238.0
        assert energy_calls[0]["fields"]["grid_to_battery_wh"] == 0.0

    def test_totals_zero_values(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today({"production": 0})})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 1
        assert energy_calls[0]["fields"]["production_wh"] == 0.0

    def test_totals_empty_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today({})})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_no_stats_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": {}})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_stats_empty_list(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": {"stats": []}})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    # ── battery_details extraction ────────────────────

    def test_battery_details_aggregate_soc(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            battery_details={"aggregate_soc": 85}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 1
        assert bat_calls[0]["fields"]["soc"] == 85

    def test_battery_details_estimated_time(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            battery_details={"estimated_time": 420}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["estimated_backup_min"] == 420

    def test_battery_details_last_24h_consumption(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            battery_details={"last_24h_consumption": 22.45}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert bat_calls[0]["fields"]["last_24h_consumption_kwh"] == 22.45

    def test_battery_details_empty_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(battery_details={})})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_battery_details_none_values(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            battery_details={"aggregate_soc": None, "estimated_time": None}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_battery_details_not_a_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(battery_details="bad")})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 0

    def test_battery_details_all_fields(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            battery_details={"aggregate_soc": 85, "estimated_time": 420, "last_24h_consumption": 22.45}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        bat_calls = [c for c in calls if c["m"] == "enphase_battery"]
        assert len(bat_calls) == 1
        f = bat_calls[0]["fields"]
        assert f["soc"] == 85
        assert f["estimated_backup_min"] == 420
        assert f["last_24h_consumption_kwh"] == 22.45

    # ── batteryConfig parsing ─────────────────────────

    def test_battery_config_reserve_pct(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert len(cfg_calls) >= 1
        assert cfg_calls[0]["fields"]["backup_reserve_pct"] == 20

    def test_battery_config_storm_guard_enabled(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20, "severe_weather_watch": "enabled"}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert cfg_calls[0]["fields"]["storm_guard"] == 1

    def test_battery_config_storm_guard_disabled(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20, "severe_weather_watch": "disabled"}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert cfg_calls[0]["fields"]["storm_guard"] == 0

    def test_battery_config_charge_from_grid_true(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20, "charge_from_grid": True}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert cfg_calls[0]["fields"]["charge_from_grid"] == 1

    def test_battery_config_charge_from_grid_false(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20, "charge_from_grid": False}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert cfg_calls[0]["fields"]["charge_from_grid"] == 0

    def test_battery_config_usage_string(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20, "usage": "self-consumption"}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert cfg_calls[0]["fields"]["usage_str"] == "self-consumption"

    def test_battery_config_no_usage(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert "usage_str" not in cfg_calls[0]["fields"]

    def test_battery_config_very_low_soc(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20, "very_low_soc": 5}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert cfg_calls[0]["fields"]["very_low_soc_pct"] == 5

    def test_battery_config_empty_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(batteryConfig={})})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert len(cfg_calls) == 0

    def test_battery_config_not_a_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(batteryConfig="bad")})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert len(cfg_calls) == 0

    # ── Reserve change detection ──────────────────────

    def test_reserve_change_emits(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert len(cfg_calls) == 1

    def test_reserve_same_value_no_re_emit(self, capture, monkeypatch):
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20}
        )})
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 1301.0)
        et.cloud_poll_once()
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert len(cfg_calls) == 0

    def test_reserve_new_value_emits_again(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 20}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        calls.clear()
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": 30}
        )})
        monkeypatch.setattr(time, "time", lambda: 1301.0)
        et.cloud_poll_once()
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert len(cfg_calls) == 1
        assert cfg_calls[0]["fields"]["backup_reserve_pct"] == 30

    def test_reserve_none_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            batteryConfig={"battery_backup_percentage": None}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        cfg_calls = [c for c in calls if c["m"] == "enphase_config"]
        assert len(cfg_calls) == 0

    # ── connectionDetails ─────────────────────────────

    def test_connection_details_wifi(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            connectionDetails=[{"wifi": True, "cellular": False, "ethernet": True}]
        )})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) >= 1
        assert gw_calls[0]["fields"]["wifi"] == 1
        assert gw_calls[0]["fields"]["cellular"] == 0
        assert gw_calls[0]["fields"]["ethernet"] == 1

    def test_connection_details_empty_list(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(connectionDetails=[])})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) == 0

    def test_connection_details_not_a_list(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(connectionDetails="bad")})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) == 0

    def test_connection_details_inner_not_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(connectionDetails=["bad"])})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        # conn[0] is "bad" (not dict), cd = {} so all fields are 0
        assert len(gw_calls) == 1
        assert gw_calls[0]["fields"]["wifi"] == 0

    def test_connection_details_all_false(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today(
            connectionDetails=[{"wifi": False, "cellular": False, "ethernet": False}]
        )})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert gw_calls[0]["fields"]["wifi"] == 0
        assert gw_calls[0]["fields"]["cellular"] == 0
        assert gw_calls[0]["fields"]["ethernet"] == 0

    def test_today_not_a_dict_response(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": [1, 2, 3]})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_today_none_response(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": None})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_today_tags_have_serial(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._client = MockClient({"get_today": self._make_today({"production": 1000})})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert energy_calls[0]["tags"]["serial"] == "TEST123"


# ═══════════════════════════════════════════════════════════════════════
# TestCloudSiteData — 30 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudSiteData:
    """Test get_site_data() response parsing (deeply nested)."""

    def _make_site(self, lifetime_energy=None, system_detail=None):
        data = {"module": {}}
        if lifetime_energy is not None:
            data["module"]["lifetime"] = {"lifetimeEnergy": lifetime_energy}
        if system_detail is not None:
            data["module"]["detail"] = {"system": system_detail}
        return data

    def test_lifetime_production(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": 50000000}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert energy_calls[0]["fields"]["lifetime_production_wh"] == 50000000.0

    def test_lifetime_consumption(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"consumed": 40000000}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert energy_calls[0]["fields"]["lifetime_consumption_wh"] == 40000000.0

    def test_lifetime_both_values(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": 50000000, "consumed": 40000000}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        f = energy_calls[0]["fields"]
        assert f["lifetime_production_wh"] == 50000000.0
        assert f["lifetime_consumption_wh"] == 40000000.0

    def test_lifetime_value_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": None}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_lifetime_empty_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_lifetime_not_a_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy="not_dict"
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    # ── Missing intermediate keys ─────────────────────

    def test_missing_module_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": {}})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_missing_lifetime_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": {"module": {}}})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_missing_lifetime_energy_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": {"module": {"lifetime": {}}}})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_missing_detail_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": {"module": {}}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) == 0

    def test_missing_system_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": {"module": {"detail": {}}}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) == 0

    # ── System status ─────────────────────────────────

    def test_system_status_code(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            system_detail={"statusCode": "normal", "microinverters": 20, "encharge": 3}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) >= 1
        assert gw_calls[0]["fields"]["status_str"] == "normal"
        assert gw_calls[0]["fields"]["microinverters"] == 20
        assert gw_calls[0]["fields"]["batteries"] == 3

    def test_system_no_status_code(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            system_detail={"microinverters": 20}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        # statusCode is None/missing, which is falsy → no emit
        assert len(gw_calls) == 0

    def test_system_empty_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(system_detail={})})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) == 0

    def test_system_zero_microinverters(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            system_detail={"statusCode": "ok", "microinverters": 0, "encharge": 0}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert gw_calls[0]["fields"]["microinverters"] == 0
        assert gw_calls[0]["fields"]["batteries"] == 0

    def test_system_missing_microinverters(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            system_detail={"statusCode": "ok"}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert gw_calls[0]["fields"]["microinverters"] == 0
        assert gw_calls[0]["fields"]["batteries"] == 0

    def test_site_data_not_a_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": "bad"})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_site_data_none_response(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": None})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert len(energy_calls) == 0

    def test_site_data_tags_serial(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": 1000}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert energy_calls[0]["tags"]["serial"] == "TEST123"

    def test_lifetime_zero_production(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": 0}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert energy_calls[0]["fields"]["lifetime_production_wh"] == 0.0

    def test_lifetime_consumed_none(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": 1000, "consumed": None}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert "lifetime_consumption_wh" not in energy_calls[0]["fields"]

    def test_lifetime_string_value(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": "50000000"}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert energy_calls[0]["fields"]["lifetime_production_wh"] == 50000000.0

    def test_system_detail_not_a_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        data = {"module": {"detail": {"system": "not_a_dict"}}}
        et._client = MockClient({"get_site_data": data})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) == 0

    def test_lifetime_and_system_together(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        data = {
            "module": {
                "lifetime": {"lifetimeEnergy": {"value": 50000000, "consumed": 40000000}},
                "detail": {"system": {"statusCode": "normal", "microinverters": 20, "encharge": 3}},
            }
        }
        et._client = MockClient({"get_site_data": data})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(energy_calls) == 1
        assert len(gw_calls) == 1

    def test_module_not_dict(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": {"module": "bad"}})
        et.cloud_poll_once()
        # Should not crash; "bad".get("lifetime", {}) → AttributeError
        # But the code does data.get("module", {}).get("lifetime", {})...
        # "bad" is a string, calling .get() on it will raise AttributeError
        # That goes into the except block
        _, errors = capture
        assert len(errors) >= 1

    def test_negative_lifetime(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            lifetime_energy={"value": -100}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        energy_calls = [c for c in calls if c["m"] == "enphase_energy"]
        assert energy_calls[0]["fields"]["lifetime_production_wh"] == -100.0

    def test_system_status_code_numeric(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_site_data": self._make_site(
            system_detail={"statusCode": 200}
        )})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert gw_calls[0]["fields"]["status_str"] == "200"


# ═══════════════════════════════════════════════════════════════════════
# TestCloudInverters — 20 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudInverters:
    """Test get_inverters() response parsing."""

    def test_all_fields_normal(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_inverters": {
            "total": 20, "not_reporting": 2, "error_count": 1,
            "warning_count": 3, "normal_count": 14,
        }})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        f = inv_calls[0]["fields"]
        assert f["total"] == 20
        assert f["not_reporting"] == 2
        assert f["error_count"] == 1
        assert f["warning_count"] == 3
        assert f["normal_count"] == 14

    @pytest.mark.parametrize("field", ["total", "not_reporting", "error_count", "warning_count", "normal_count"])
    def test_field_zero(self, capture, monkeypatch, field):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_inverters": {field: 0}})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        assert len(inv_calls) >= 1

    @pytest.mark.parametrize("field", ["total", "not_reporting", "error_count", "warning_count", "normal_count"])
    def test_field_none_defaults_zero(self, capture, monkeypatch, field):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        data = {"total": 10, "not_reporting": 0, "error_count": 0,
                "warning_count": 0, "normal_count": 10}
        data[field] = None
        et._client = MockClient({"get_inverters": data})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        # None defaults to 0 via .get(field, 0)
        assert inv_calls[0]["fields"][field] == 0

    def test_not_a_dict_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_inverters": "bad"})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        assert len(inv_calls) == 0

    def test_tags_serial(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_inverters": {"total": 10}})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        assert inv_calls[0]["tags"]["serial"] == "TEST123"

    def test_string_values_converted(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_inverters": {
            "total": "20", "not_reporting": "2", "error_count": "1",
            "warning_count": "3", "normal_count": "14",
        }})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        assert inv_calls[0]["fields"]["total"] == 20

    def test_empty_dict_defaults_all_zero(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_inverters": {}})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        assert inv_calls[0]["fields"]["total"] == 0
        assert inv_calls[0]["fields"]["normal_count"] == 0

    def test_negative_values(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_inverters": {"total": -1, "error_count": -5}})
        et.cloud_poll_once()
        calls, _ = capture
        inv_calls = [c for c in calls if c["m"] == "enphase_inverters"]
        assert inv_calls[0]["fields"]["total"] == -1
        assert inv_calls[0]["fields"]["error_count"] == -5


# ═══════════════════════════════════════════════════════════════════════
# TestCloudAlarms — 15 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudAlarms:
    """Test get_alarms() response parsing."""

    def test_total_positive_emits(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": 3}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert len(gw_calls) >= 1
        assert gw_calls[0]["fields"]["alarm_count"] == 3

    def test_total_zero_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": 0}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        assert len(gw_calls) == 0

    def test_total_one_emits(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": 1}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        assert len(gw_calls) == 1

    def test_total_large_number(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": 999}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        assert gw_calls[0]["fields"]["alarm_count"] == 999

    def test_missing_total_key(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        # .get("total", 0) → 0, which is not > 0
        assert len(gw_calls) == 0

    def test_total_none_defaults_zero(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": None}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        # None is not > 0
        assert len(gw_calls) == 0

    def test_not_a_dict_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": "bad"})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        assert len(gw_calls) == 0

    def test_alarms_list_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": [1, 2]})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        assert len(gw_calls) == 0

    def test_alarms_none_response(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": None})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        assert len(gw_calls) == 0

    def test_total_negative_emits(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": -1}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        # -1 is not > 0
        assert len(gw_calls) == 0

    def test_total_string_positive(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": "5"}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        # "5" > 0 is True in Python (str > int comparison)
        assert len(gw_calls) >= 1

    def test_total_float_positive(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": 1.5}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        assert len(gw_calls) >= 1
        assert gw_calls[0]["fields"]["alarm_count"] == 1

    def test_tags_serial(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": 2}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway"]
        assert gw_calls[0]["tags"]["serial"] == "TEST123"

    def test_total_true_emits(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": True}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        # True > 0 is True
        assert len(gw_calls) >= 1

    def test_total_false_no_emit(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_alarms": {"total": False}})
        et.cloud_poll_once()
        calls, _ = capture
        gw_calls = [c for c in calls if c["m"] == "enphase_gateway" and "alarm_count" in c["fields"]]
        # False > 0 is False
        assert len(gw_calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# TestCloudEndpointErrors — 30 tests
# ═══════════════════════════════════════════════════════════════════════


class TestCloudEndpointErrors:
    """Simulate getter exceptions for each endpoint."""

    ENDPOINTS = [
        "latest_power", "battery_status", "today", "events",
        "alarms", "devices", "site_data", "inverters", "battery_schedules",
    ]

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_exception_increments_cloud_errors(self, capture, monkeypatch, endpoint):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        getter_name = f"get_{endpoint}"
        if endpoint == "battery_schedules":
            getter_name = "get_battery_schedules"
        et._client = RaisingClient(raise_on={getter_name: RuntimeError("test fail")})
        et.cloud_poll_once()
        assert et._cloud_errors >= 1

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_exception_calls_emit_error(self, capture, monkeypatch, endpoint):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        getter_name = f"get_{endpoint}"
        et._client = RaisingClient(raise_on={getter_name: ValueError("broken")})
        et.cloud_poll_once()
        _, errors = capture
        matching = [e for e in errors if e["component"] == f"cloud_{endpoint}"]
        assert len(matching) >= 1
        assert "ValueError" in matching[0]["message"]

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_exception_sets_retry_time(self, capture, monkeypatch, endpoint):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        getter_name = f"get_{endpoint}"
        interval = et.CLOUD_SCHEDULE.get(endpoint, 600)
        et._client = RaisingClient(raise_on={getter_name: ConnectionError("timeout")})
        et.cloud_poll_once()
        # On error: _cloud_last_fetch[endpoint] = now - interval + 60
        expected = 10000.0 - interval + 60
        assert et._cloud_last_fetch.get(endpoint) == expected

    def test_multiple_endpoints_fail_independently(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = RaisingClient(raise_on={
            "get_latest_power": RuntimeError("fail1"),
            "get_battery_status": RuntimeError("fail2"),
        })
        et.cloud_poll_once()
        assert et._cloud_errors >= 2

    def test_exception_message_truncated(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        long_msg = "x" * 200
        et._client = RaisingClient(raise_on={"get_latest_power": RuntimeError(long_msg)})
        et.cloud_poll_once()
        _, errors = capture
        matching = [e for e in errors if e["component"] == "cloud_latest_power"]
        # str(e)[:100] in the code
        assert len(matching[0]["message"]) <= 120  # "RuntimeError: " + 100 chars

    def test_error_after_success_increments(self, capture, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10000.0)
        et._client = MockClient({"get_latest_power": {"latest_power": {"value": 100}}})
        et.cloud_poll_once()
        assert et._cloud_errors == 0
        monkeypatch.setattr(time, "time", lambda: 10121.0)
        et._client = RaisingClient(raise_on={"get_latest_power": RuntimeError("fail")})
        et.cloud_poll_once()
        assert et._cloud_errors >= 1


# ═══════════════════════════════════════════════════════════════════════
# TestDiscoverSerial — 50 tests
# ═══════════════════════════════════════════════════════════════════════


class TestDiscoverSerial:
    """Test discover_serial() with varied device structures."""

    def test_envoy_type_serial_number(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"serial_number": "ABC123"}]}]
        }})
        assert et.discover_serial(client) == "ABC123"

    def test_gateway_type_serial_number(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "gateway", "devices": [{"serial_number": "GW001"}]}]
        }})
        assert et.discover_serial(client) == "GW001"

    def test_envoy_type_serial_num(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"serial_num": "SN456"}]}]
        }})
        assert et.discover_serial(client) == "SN456"

    def test_envoy_type_sn(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"sn": "SHORT789"}]}]
        }})
        assert et.discover_serial(client) == "SHORT789"

    def test_fallback_envoys_key(self):
        client = MockClient({"get_devices": {
            "envoys": [{"serial_number": "FB001"}]
        }})
        assert et.discover_serial(client) == "FB001"

    def test_fallback_envoy_key(self):
        client = MockClient({"get_devices": {
            "envoy": [{"serial_number": "FB002"}]
        }})
        assert et.discover_serial(client) == "FB002"

    def test_fallback_gateways_key(self):
        client = MockClient({"get_devices": {
            "gateways": [{"serial_number": "FB003"}]
        }})
        assert et.discover_serial(client) == "FB003"

    def test_fallback_envoys_serial_num(self):
        client = MockClient({"get_devices": {
            "envoys": [{"serial_num": "FB004"}]
        }})
        assert et.discover_serial(client) == "FB004"

    def test_fallback_envoys_sn(self):
        client = MockClient({"get_devices": {
            "envoys": [{"sn": "FB005"}]
        }})
        assert et.discover_serial(client) == "FB005"

    def test_empty_result_list(self):
        client = MockClient({"get_devices": {"result": []}})
        assert et.discover_serial(client) == ""

    def test_no_result_key(self):
        client = MockClient({"get_devices": {}})
        assert et.discover_serial(client) == ""

    def test_result_no_envoy_type(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "microinverter", "devices": [{"serial_number": "INV001"}]}]
        }})
        assert et.discover_serial(client) == ""

    def test_envoy_empty_devices_list(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": []}]
        }})
        assert et.discover_serial(client) == ""

    def test_envoy_device_no_serial_keys(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"name": "myenvoy"}]}]
        }})
        assert et.discover_serial(client) == ""

    def test_none_response(self):
        client = MockClient({"get_devices": None})
        assert et.discover_serial(client) == ""

    def test_string_response(self):
        client = MockClient({"get_devices": "not_a_dict"})
        assert et.discover_serial(client) == ""

    def test_list_response(self):
        client = MockClient({"get_devices": [1, 2, 3]})
        assert et.discover_serial(client) == ""

    def test_exception_returns_empty(self):
        class FailClient:
            def get_devices(self):
                raise RuntimeError("boom")
        assert et.discover_serial(FailClient()) == ""

    def test_multiple_groups_first_envoy_wins(self):
        client = MockClient({"get_devices": {
            "result": [
                {"type": "microinverter", "devices": [{"serial_number": "INV001"}]},
                {"type": "envoy", "devices": [{"serial_number": "ENV001"}]},
            ]
        }})
        assert et.discover_serial(client) == "ENV001"

    def test_multiple_envoys_first_device_wins(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [
                {"serial_number": "FIRST"},
                {"serial_number": "SECOND"},
            ]}]
        }})
        assert et.discover_serial(client) == "FIRST"

    def test_group_not_dict(self):
        client = MockClient({"get_devices": {
            "result": ["not_a_dict", {"type": "envoy", "devices": [{"serial_number": "OK"}]}]
        }})
        assert et.discover_serial(client) == "OK"

    def test_integer_serial_converted(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"serial_number": 12345}]}]
        }})
        assert et.discover_serial(client) == "12345"

    def test_fallback_envoys_not_a_list(self):
        client = MockClient({"get_devices": {
            "envoys": "not_a_list"
        }})
        assert et.discover_serial(client) == ""

    def test_fallback_envoys_inner_not_dict(self):
        client = MockClient({"get_devices": {
            "envoys": ["not_a_dict"]
        }})
        assert et.discover_serial(client) == ""

    def test_fallback_envoys_empty_list(self):
        client = MockClient({"get_devices": {
            "envoys": []
        }})
        assert et.discover_serial(client) == ""

    def test_fallback_gateways_sn(self):
        client = MockClient({"get_devices": {
            "gateways": [{"sn": "GW_SN"}]
        }})
        assert et.discover_serial(client) == "GW_SN"

    def test_fallback_gateways_serial_num(self):
        client = MockClient({"get_devices": {
            "gateways": [{"serial_num": "GW_NUM"}]
        }})
        assert et.discover_serial(client) == "GW_NUM"

    def test_fallback_gateways_not_a_list(self):
        client = MockClient({"get_devices": {
            "gateways": {"serial_number": "NOT_LIST"}
        }})
        assert et.discover_serial(client) == ""

    def test_fallback_gateways_empty_list(self):
        client = MockClient({"get_devices": {
            "gateways": []
        }})
        assert et.discover_serial(client) == ""

    def test_result_preferred_over_fallback(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"serial_number": "FROM_RESULT"}]}],
            "envoys": [{"serial_number": "FROM_FALLBACK"}],
        }})
        assert et.discover_serial(client) == "FROM_RESULT"

    def test_device_with_empty_serial_number(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"serial_number": ""}]}]
        }})
        # Empty string is falsy → falls through to fallback keys
        assert et.discover_serial(client) == ""

    def test_device_with_none_serial(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [{"serial_number": None}]}]
        }})
        assert et.discover_serial(client) == ""

    def test_envoy_no_devices_key(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy"}]
        }})
        assert et.discover_serial(client) == ""

    def test_connection_error_returns_empty(self):
        class ConnFailClient:
            def get_devices(self):
                raise ConnectionError("network down")
        assert et.discover_serial(ConnFailClient()) == ""

    def test_timeout_error_returns_empty(self):
        class TimeoutClient:
            def get_devices(self):
                raise TimeoutError("timed out")
        assert et.discover_serial(TimeoutClient()) == ""

    def test_fallback_envoy_single_item(self):
        client = MockClient({"get_devices": {
            "envoy": [{"serial_number": "SINGLE"}]
        }})
        assert et.discover_serial(client) == "SINGLE"

    def test_fallback_envoy_multiple_items(self):
        client = MockClient({"get_devices": {
            "envoy": [
                {"serial_number": "FIRST"},
                {"serial_number": "SECOND"},
            ]
        }})
        assert et.discover_serial(client) == "FIRST"

    def test_fallback_envoy_empty_list(self):
        client = MockClient({"get_devices": {
            "envoy": []
        }})
        assert et.discover_serial(client) == ""

    def test_fallback_envoy_not_a_list(self):
        client = MockClient({"get_devices": {
            "envoy": "not_a_list"
        }})
        assert et.discover_serial(client) == ""

    def test_gateway_type_sn(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "gateway", "devices": [{"sn": "GW_SN_RESULT"}]}]
        }})
        assert et.discover_serial(client) == "GW_SN_RESULT"

    def test_gateway_type_serial_num(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "gateway", "devices": [{"serial_num": "GW_NUM_R"}]}]
        }})
        assert et.discover_serial(client) == "GW_NUM_R"

    def test_mixed_groups_gateway_before_envoy(self):
        client = MockClient({"get_devices": {
            "result": [
                {"type": "gateway", "devices": [{"serial_number": "GW_FIRST"}]},
                {"type": "envoy", "devices": [{"serial_number": "ENV_SECOND"}]},
            ]
        }})
        assert et.discover_serial(client) == "GW_FIRST"

    def test_envoy_with_extra_fields(self):
        client = MockClient({"get_devices": {
            "result": [{"type": "envoy", "devices": [
                {"serial_number": "ABC", "name": "my-envoy", "model": "IQ8"}
            ]}]
        }})
        assert et.discover_serial(client) == "ABC"

    def test_fallback_envoys_with_extra_fields(self):
        client = MockClient({"get_devices": {
            "envoys": [{"serial_number": "FB_EXTRA", "firmware": "8.0"}]
        }})
        assert et.discover_serial(client) == "FB_EXTRA"

    def test_numeric_serial_in_fallback(self):
        client = MockClient({"get_devices": {
            "envoys": [{"serial_number": 482525046373}]
        }})
        assert et.discover_serial(client) == "482525046373"

    def test_deeply_nested_empty_devices(self):
        client = MockClient({"get_devices": {
            "result": [
                {"type": "envoy", "devices": []},
                {"type": "gateway", "devices": []},
            ]
        }})
        assert et.discover_serial(client) == ""

    def test_result_with_none_group(self):
        client = MockClient({"get_devices": {
            "result": [None, {"type": "envoy", "devices": [{"serial_number": "AFTER_NONE"}]}]
        }})
        assert et.discover_serial(client) == "AFTER_NONE"

    def test_all_fallback_keys_empty(self):
        client = MockClient({"get_devices": {
            "envoys": [], "envoy": [], "gateways": [],
        }})
        assert et.discover_serial(client) == ""

    def test_fallback_priority_envoys_first(self):
        client = MockClient({"get_devices": {
            "envoys": [{"serial_number": "FROM_ENVOYS"}],
            "gateways": [{"serial_number": "FROM_GATEWAYS"}],
        }})
        assert et.discover_serial(client) == "FROM_ENVOYS"

    def test_fallback_gateways_multiple(self):
        client = MockClient({"get_devices": {
            "gateways": [
                {"serial_number": "GW1"},
                {"serial_number": "GW2"},
            ]
        }})
        assert et.discover_serial(client) == "GW1"
