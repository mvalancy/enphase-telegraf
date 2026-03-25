"""Comprehensive tests for MQTT/protobuf message handling in enphase_telegraf.py.

Targets: on_mqtt_data(), POWER_MAP, VA_MAP, BATT_MODE_MAP, GRID_RELAY_MAP,
DRY_CONTACT_STATE_MAP, _enum_int, _check_schema, config change detection,
dry contact tracking, anomaly detection, and all module-level globals.
"""

import sys
import time
from pathlib import Path

import pytest

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import enphase_telegraf as et


# ────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_telegraf_globals():
    """Reset every module-level global between tests for full isolation."""
    et._serial = "TEST123"
    et._last_batt_mode = None
    et._last_grid_relay = None
    et._last_gen_relay = None
    et._last_reserve_pct = None
    et._last_dry_contacts = {}
    et._error_backoff = {}
    et._known_fields = None
    et._unknown_enums_seen = set()
    et._mqtt_messages = 0
    et._mqtt_errors = 0
    et._verbose = False
    yield


@pytest.fixture
def capture_emit(monkeypatch):
    """Capture all emit() and emit_error() calls into a list."""
    calls = []

    def fake_emit(measurement, tags, fields, ts_ns=None):
        calls.append({
            "measurement": measurement,
            "tags": tags,
            "fields": fields,
            "ts_ns": ts_ns,
        })

    def fake_emit_error(comp, msg):
        calls.append({"error": comp, "message": msg})

    monkeypatch.setattr(et, "emit", fake_emit)
    monkeypatch.setattr(et, "emit_error", fake_emit_error)
    return calls


def _power_calls(calls):
    """Filter to only enphase_power measurements."""
    return [c for c in calls if c.get("measurement") == "enphase_power"]


def _config_calls(calls):
    """Filter to only enphase_config measurements."""
    return [c for c in calls if c.get("measurement") == "enphase_config"]


def _error_calls(calls):
    """Filter to only error entries."""
    return [c for c in calls if "error" in c]


def _dry_contact_calls(calls):
    """Filter to only enphase_dry_contact measurements."""
    return [c for c in calls if c.get("measurement") == "enphase_dry_contact"]


# ────────────────────────────────────────────────────────────────
# TestOnMqttDataPowerFields  (60 tests)
# ────────────────────────────────────────────────────────────────

class TestOnMqttDataPowerFields:
    """Test POWER_MAP field extraction and renaming for all 5 power sources."""

    POWER_ENTRIES = [
        ("pv_power_w", "solar_w"),
        ("grid_power_w", "grid_w"),
        ("load_power_w", "consumption_w"),
        ("storage_power_w", "battery_w"),
        ("generator_power_w", "generator_w"),
    ]

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_zero(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 0})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1, f"Expected 1 power emit for {proto_key}=0"
        assert pwr[0]["fields"][field_name] == 0.0

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_positive_int(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 1})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 1.0

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_negative_int(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: -1})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == -1.0

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_positive_float(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 500.5})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 500.5

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_negative_float(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: -500.5})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == -500.5

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_large_valid(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 99999})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 99999.0

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_over_100k_triggers_anomaly(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 100001})
        pwr = _power_calls(capture_emit)
        # Field should NOT be in the power emit (anomaly)
        if pwr:
            assert field_name not in pwr[0]["fields"], \
                f"{field_name} should be absent when value > 100000"
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs), \
            f"Expected data_quality error for {proto_key}=100001"

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_tiny_float(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 0.001})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == pytest.approx(0.001)

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_none_skipped(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: None})
        pwr = _power_calls(capture_emit)
        # No fields should be emitted for None
        if pwr:
            assert field_name not in pwr[0]["fields"]

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_bad_string_raises(self, capture_emit, proto_key, field_name):
        """Non-numeric strings should raise when float() is attempted."""
        with pytest.raises((ValueError, TypeError)):
            et.on_mqtt_data({proto_key: "bad_string"})

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_boundary_exactly_100k(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 100000})
        pwr = _power_calls(capture_emit)
        # abs(100000) > 100_000 is False, so field should be present
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 100000.0

    @pytest.mark.parametrize("proto_key,field_name", POWER_ENTRIES)
    def test_power_negative_over_100k(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: -100001})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert field_name not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert len(errs) > 0


# ────────────────────────────────────────────────────────────────
# TestOnMqttDataVAFields  (30 tests)
# ────────────────────────────────────────────────────────────────

class TestOnMqttDataVAFields:
    """Test VA_MAP field extraction for all 5 apparent-power sources."""

    VA_ENTRIES = [
        ("pv_apparent_va", "solar_va"),
        ("grid_apparent_va", "grid_va"),
        ("load_apparent_va", "consumption_va"),
        ("storage_apparent_va", "battery_va"),
        ("generator_apparent_va", "generator_va"),
    ]

    @pytest.mark.parametrize("proto_key,field_name", VA_ENTRIES)
    def test_va_zero(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 0})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 0.0

    @pytest.mark.parametrize("proto_key,field_name", VA_ENTRIES)
    def test_va_positive_float(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: 100.5})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 100.5

    @pytest.mark.parametrize("proto_key,field_name", VA_ENTRIES)
    def test_va_negative_float(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: -100.5})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == -100.5

    @pytest.mark.parametrize("proto_key,field_name", VA_ENTRIES)
    def test_va_none_skipped(self, capture_emit, proto_key, field_name):
        et.on_mqtt_data({proto_key: None})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert field_name not in pwr[0]["fields"]

    @pytest.mark.parametrize("proto_key,field_name", VA_ENTRIES)
    def test_va_large_value(self, capture_emit, proto_key, field_name):
        """VA fields have NO anomaly check — large values pass through."""
        et.on_mqtt_data({proto_key: 50000})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 50000.0

    @pytest.mark.parametrize("proto_key,field_name", VA_ENTRIES)
    def test_va_very_large_value(self, capture_emit, proto_key, field_name):
        """VA fields have NO anomaly check — even very large values pass."""
        et.on_mqtt_data({proto_key: 99999})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][field_name] == 99999.0


# ────────────────────────────────────────────────────────────────
# TestOnMqttDataPerPhase  (50 tests)
# ────────────────────────────────────────────────────────────────

class TestOnMqttDataPerPhase:
    """Test per-phase power extraction from *_phase_w and *_phase_va lists."""

    PHASE_SOURCES = [
        ("pv", "solar"),
        ("grid", "grid"),
        ("load", "consumption"),
        ("storage", "battery"),
        ("generator", "generator"),
    ]

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_empty_list(self, capture_emit, proto_prefix, out_prefix):
        et.on_mqtt_data({f"{proto_prefix}_phase_w": []})
        pwr = _power_calls(capture_emit)
        # Empty list is falsy, so no phase fields emitted and no power emit
        if pwr:
            assert f"{out_prefix}_l1_w" not in pwr[0]["fields"]

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_single(self, capture_emit, proto_prefix, out_prefix):
        et.on_mqtt_data({f"{proto_prefix}_phase_w": [100]})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][f"{out_prefix}_l1_w"] == 100.0
        assert f"{out_prefix}_l2_w" not in pwr[0]["fields"]

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_two(self, capture_emit, proto_prefix, out_prefix):
        et.on_mqtt_data({f"{proto_prefix}_phase_w": [100, 200]})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][f"{out_prefix}_l1_w"] == 100.0
        assert pwr[0]["fields"][f"{out_prefix}_l2_w"] == 200.0
        assert f"{out_prefix}_l3_w" not in pwr[0]["fields"]

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_three(self, capture_emit, proto_prefix, out_prefix):
        et.on_mqtt_data({f"{proto_prefix}_phase_w": [100, 200, 300]})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][f"{out_prefix}_l1_w"] == 100.0
        assert pwr[0]["fields"][f"{out_prefix}_l2_w"] == 200.0
        assert pwr[0]["fields"][f"{out_prefix}_l3_w"] == 300.0

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_four_uses_all(self, capture_emit, proto_prefix, out_prefix):
        """The code iterates ALL elements; 4th becomes l4."""
        et.on_mqtt_data({f"{proto_prefix}_phase_w": [100, 200, 300, 400]})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][f"{out_prefix}_l1_w"] == 100.0
        assert pwr[0]["fields"][f"{out_prefix}_l2_w"] == 200.0
        assert pwr[0]["fields"][f"{out_prefix}_l3_w"] == 300.0
        assert pwr[0]["fields"][f"{out_prefix}_l4_w"] == 400.0

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_va_three(self, capture_emit, proto_prefix, out_prefix):
        et.on_mqtt_data({f"{proto_prefix}_phase_va": [110, 220, 330]})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][f"{out_prefix}_l1_va"] == 110.0
        assert pwr[0]["fields"][f"{out_prefix}_l2_va"] == 220.0
        assert pwr[0]["fields"][f"{out_prefix}_l3_va"] == 330.0

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_anomaly_over_50k(self, capture_emit, proto_prefix, out_prefix):
        """Per-phase values over 50000 W trigger anomaly."""
        et.on_mqtt_data({f"{proto_prefix}_phase_w": [50001]})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert f"{out_prefix}_l1_w" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert len(errs) > 0

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_boundary_exactly_50k(self, capture_emit, proto_prefix, out_prefix):
        """50000 W exactly: abs(50000) > 50_000 is False, so it passes."""
        et.on_mqtt_data({f"{proto_prefix}_phase_w": [50000]})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"][f"{out_prefix}_l1_w"] == 50000.0

    @pytest.mark.parametrize("proto_prefix,out_prefix", PHASE_SOURCES)
    def test_phase_negative_over_50k(self, capture_emit, proto_prefix, out_prefix):
        et.on_mqtt_data({f"{proto_prefix}_phase_w": [-50001]})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert f"{out_prefix}_l1_w" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert len(errs) > 0


# ────────────────────────────────────────────────────────────────
# TestOnMqttDataSOC  (20 tests)
# ────────────────────────────────────────────────────────────────

class TestOnMqttDataSOC:
    """Test meter_soc handling: valid range 0-100, anomaly outside."""

    def test_soc_zero(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 0})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 0

    def test_soc_one(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 1})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 1

    def test_soc_fifty(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 50})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 50

    def test_soc_ninety_nine(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 99})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 99

    def test_soc_hundred(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 100})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 100

    def test_soc_negative_one_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": -1})
        pwr = _power_calls(capture_emit)
        # SOC should not be present
        if pwr:
            assert "soc" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_soc_101_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 101})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "soc" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_soc_200_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 200})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "soc" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_soc_none_skipped(self, capture_emit):
        et.on_mqtt_data({"meter_soc": None})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "soc" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        # None should not trigger anomaly
        assert not any("data_quality" in str(e) for e in errs)

    def test_soc_float_truncated(self, capture_emit):
        """0.5 is int()-ed to 0, which is valid."""
        et.on_mqtt_data({"meter_soc": 0.5})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 0

    def test_soc_bad_string_raises(self, capture_emit):
        with pytest.raises((ValueError, TypeError)):
            et.on_mqtt_data({"meter_soc": "bad"})

    def test_soc_is_integer_type(self, capture_emit):
        """SOC should be emitted as int, not float."""
        et.on_mqtt_data({"meter_soc": 77})
        pwr = _power_calls(capture_emit)
        assert isinstance(pwr[0]["fields"]["soc"], int)

    def test_soc_float_99_9_truncates_to_99(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 99.9})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["soc"] == 99

    def test_soc_large_negative_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": -100})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "soc" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert len(errs) > 0

    def test_soc_with_other_fields(self, capture_emit):
        """SOC emitted alongside power fields."""
        et.on_mqtt_data({"meter_soc": 55, "pv_power_w": 3000})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 55
        assert pwr[0]["fields"]["solar_w"] == 3000.0

    def test_soc_boundary_zero_is_valid(self, capture_emit):
        """0 is in range 0-100."""
        et.on_mqtt_data({"meter_soc": 0})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["soc"] == 0
        errs = _error_calls(capture_emit)
        assert not any("data_quality" in str(e) for e in errs)

    def test_soc_boundary_100_is_valid(self, capture_emit):
        """100 is in range 0-100."""
        et.on_mqtt_data({"meter_soc": 100})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["soc"] == 100
        errs = _error_calls(capture_emit)
        assert not any("data_quality" in str(e) for e in errs)

    def test_soc_string_numeric_raises(self, capture_emit):
        """String '50' will pass int() but the code does int(meter_soc)."""
        # int("50") = 50 which is valid, so this actually works
        et.on_mqtt_data({"meter_soc": "50"})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["soc"] == 50

    def test_soc_1000_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 1000})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "soc" not in pwr[0]["fields"]

    def test_soc_missing_key(self, capture_emit):
        """No meter_soc key at all."""
        et.on_mqtt_data({"pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert "soc" not in pwr[0]["fields"]


# ────────────────────────────────────────────────────────────────
# TestOnMqttDataInverterCount  (15 tests)
# ────────────────────────────────────────────────────────────────

class TestOnMqttDataInverterCount:
    """Test pcu_total/pcu_running → inverters_total/inverters_producing."""

    def test_both_present(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 20, "pcu_running": 18})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["inverters_total"] == 20
        assert pwr[0]["fields"]["inverters_producing"] == 18

    def test_both_zero(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 0, "pcu_running": 0})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["inverters_total"] == 0
        assert pwr[0]["fields"]["inverters_producing"] == 0

    def test_both_none_no_fields(self, capture_emit):
        et.on_mqtt_data({"pcu_total": None, "pcu_running": None})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "inverters_total" not in pwr[0]["fields"]
            assert "inverters_producing" not in pwr[0]["fields"]

    def test_total_present_running_none(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 10, "pcu_running": None})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["inverters_total"] == 10
        assert "inverters_producing" not in pwr[0]["fields"]

    def test_total_none_running_present(self, capture_emit):
        et.on_mqtt_data({"pcu_total": None, "pcu_running": 5})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert "inverters_total" not in pwr[0]["fields"]
        assert pwr[0]["fields"]["inverters_producing"] == 5

    def test_single_inverter(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 1, "pcu_running": 1})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["inverters_total"] == 1
        assert pwr[0]["fields"]["inverters_producing"] == 1

    def test_large_system(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 100, "pcu_running": 0})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["inverters_total"] == 100
        assert pwr[0]["fields"]["inverters_producing"] == 0

    def test_missing_keys_entirely(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert "inverters_total" not in pwr[0]["fields"]
        assert "inverters_producing" not in pwr[0]["fields"]

    def test_inverter_count_is_int(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 20.7, "pcu_running": 18.3})
        pwr = _power_calls(capture_emit)
        assert isinstance(pwr[0]["fields"]["inverters_total"], int)
        assert isinstance(pwr[0]["fields"]["inverters_producing"], int)

    def test_inverter_count_float_truncated(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 5.9})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["inverters_total"] == 5

    def test_total_only_key_present(self, capture_emit):
        et.on_mqtt_data({"pcu_total": 15})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["inverters_total"] == 15
        assert "inverters_producing" not in pwr[0]["fields"]

    def test_running_only_key_present(self, capture_emit):
        et.on_mqtt_data({"pcu_running": 8})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["inverters_producing"] == 8
        assert "inverters_total" not in pwr[0]["fields"]

    def test_combined_with_power(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 5000, "pcu_total": 20, "pcu_running": 20})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_w"] == 5000.0
        assert pwr[0]["fields"]["inverters_total"] == 20
        assert pwr[0]["fields"]["inverters_producing"] == 20

    def test_string_numeric_pcu(self, capture_emit):
        """int('10') = 10, this works."""
        et.on_mqtt_data({"pcu_total": "10"})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["inverters_total"] == 10

    def test_zero_total_nonzero_running(self, capture_emit):
        """Edge case: more running than total."""
        et.on_mqtt_data({"pcu_total": 0, "pcu_running": 5})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["inverters_total"] == 0
        assert pwr[0]["fields"]["inverters_producing"] == 5


# ────────────────────────────────────────────────────────────────
# TestOnMqttDataGridEvents  (15 tests)
# ────────────────────────────────────────────────────────────────

class TestOnMqttDataGridEvents:
    """Test grid_outage_status and grid_update_ongoing flags."""

    def test_both_zero(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": 0, "grid_update_ongoing": 0})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 0
        assert pwr[0]["fields"]["grid_update_ongoing"] == 0

    def test_outage_1_update_0(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": 1, "grid_update_ongoing": 0})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 1
        assert pwr[0]["fields"]["grid_update_ongoing"] == 0

    def test_outage_0_update_1(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": 0, "grid_update_ongoing": 1})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 0
        assert pwr[0]["fields"]["grid_update_ongoing"] == 1

    def test_both_1(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": 1, "grid_update_ongoing": 1})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 1
        assert pwr[0]["fields"]["grid_update_ongoing"] == 1

    def test_both_none_no_fields(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": None, "grid_update_ongoing": None})
        pwr = _power_calls(capture_emit)
        # None is not None evaluates to False, so fields skipped
        if pwr:
            assert "grid_outage" not in pwr[0]["fields"]
            assert "grid_update_ongoing" not in pwr[0]["fields"]

    def test_true_false_booleans(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": True, "grid_update_ongoing": False})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 1
        assert pwr[0]["fields"]["grid_update_ongoing"] == 0

    def test_missing_keys(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert "grid_outage" not in pwr[0]["fields"]
        assert "grid_update_ongoing" not in pwr[0]["fields"]

    def test_outage_only(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": 1})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 1
        assert "grid_update_ongoing" not in pwr[0]["fields"]

    def test_update_only(self, capture_emit):
        et.on_mqtt_data({"grid_update_ongoing": 1})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_update_ongoing"] == 1
        assert "grid_outage" not in pwr[0]["fields"]

    def test_grid_outage_int_conversion(self, capture_emit):
        """int(2) = 2, code does int(value)."""
        et.on_mqtt_data({"grid_outage_status": 2})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 2

    def test_grid_zero_is_not_none(self, capture_emit):
        """0 is not None, so it should be emitted."""
        et.on_mqtt_data({"grid_outage_status": 0})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 0

    def test_combined_with_power_and_grid(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 5000,
            "grid_outage_status": 0,
            "grid_update_ongoing": 0,
        })
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_w"] == 5000.0
        assert pwr[0]["fields"]["grid_outage"] == 0

    def test_false_value_grid_outage(self, capture_emit):
        """False is not None, so int(False) = 0."""
        et.on_mqtt_data({"grid_outage_status": False})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 0

    def test_string_zero_grid_update(self, capture_emit):
        """'0' is not None, int('0') = 0."""
        et.on_mqtt_data({"grid_update_ongoing": "0"})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_update_ongoing"] == 0

    def test_grid_outage_combined_with_soc(self, capture_emit):
        et.on_mqtt_data({"grid_outage_status": 1, "meter_soc": 100})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_outage"] == 1
        assert pwr[0]["fields"]["soc"] == 100


# ────────────────────────────────────────────────────────────────
# TestEnumMapping  (40 tests)
# ────────────────────────────────────────────────────────────────

class TestEnumMapping:
    """Test _enum_int with all enum maps."""

    # BATT_MODE_MAP tests
    def test_batt_mode_full_backup(self):
        assert et._enum_int(et.BATT_MODE_MAP, "BATT_MODE_FULL_BACKUP") == 0

    def test_batt_mode_self_cons(self):
        assert et._enum_int(et.BATT_MODE_MAP, "BATT_MODE_SELF_CONS") == 1

    def test_batt_mode_savings(self):
        assert et._enum_int(et.BATT_MODE_MAP, "BATT_MODE_SAVINGS") == 2

    def test_batt_mode_unknown_returns_neg1(self):
        assert et._enum_int(et.BATT_MODE_MAP, "UNKNOWN") == -1

    def test_batt_mode_empty_string_returns_neg1(self):
        assert et._enum_int(et.BATT_MODE_MAP, "") == -1

    def test_batt_mode_none_returns_neg1(self):
        assert et._enum_int(et.BATT_MODE_MAP, None) == -1

    def test_batt_mode_random_string(self):
        assert et._enum_int(et.BATT_MODE_MAP, "BATT_MODE_FANTASY") == -1

    # GRID_RELAY_MAP tests (11 known values)
    def test_grid_relay_open(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_OPEN") == 1

    def test_grid_relay_closed(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_CLOSED") == 2

    def test_grid_relay_offgrid_ac(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_OFFGRID_AC_GRID_PRESENT") == 3

    def test_grid_relay_offgrid_resync(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD") == 4

    def test_grid_relay_waiting_init(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID") == 5

    def test_grid_relay_gen_open(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_GEN_OPEN") == 6

    def test_grid_relay_gen_closed(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_GEN_CLOSED") == 7

    def test_grid_relay_gen_startup(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_GEN_STARTUP") == 8

    def test_grid_relay_gen_sync_ready(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_GEN_SYNC_READY") == 9

    def test_grid_relay_gen_ac_stable(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_GEN_AC_STABLE") == 10

    def test_grid_relay_gen_ac_unstable(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "OPER_RELAY_GEN_AC_UNSTABLE") == 11

    def test_grid_relay_unknown_returns_neg1(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "UNKNOWN") == -1

    def test_grid_relay_none_returns_neg1(self):
        assert et._enum_int(et.GRID_RELAY_MAP, None) == -1

    def test_grid_relay_empty_returns_neg1(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "") == -1

    # DRY_CONTACT_STATE_MAP tests
    def test_dry_contact_invalid(self):
        assert et._enum_int(et.DRY_CONTACT_STATE_MAP, "DC_RELAY_STATE_INVALID") == -1

    def test_dry_contact_off(self):
        assert et._enum_int(et.DRY_CONTACT_STATE_MAP, "DC_RELAY_OFF") == 0

    def test_dry_contact_on(self):
        assert et._enum_int(et.DRY_CONTACT_STATE_MAP, "DC_RELAY_ON") == 1

    def test_dry_contact_unknown_returns_neg1(self):
        assert et._enum_int(et.DRY_CONTACT_STATE_MAP, "UNKNOWN") == -1

    def test_dry_contact_none_returns_neg1(self):
        assert et._enum_int(et.DRY_CONTACT_STATE_MAP, None) == -1

    # Edge cases
    def test_empty_map_returns_neg1(self):
        assert et._enum_int({}, "anything") == -1

    def test_empty_map_none_key(self):
        assert et._enum_int({}, None) == -1

    def test_empty_map_empty_key(self):
        assert et._enum_int({}, "") == -1

    def test_custom_map_hit(self):
        assert et._enum_int({"FOO": 42}, "FOO") == 42

    def test_custom_map_miss(self):
        assert et._enum_int({"FOO": 42}, "BAR") == -1

    def test_case_sensitive_batt_mode(self):
        assert et._enum_int(et.BATT_MODE_MAP, "batt_mode_full_backup") == -1

    def test_case_sensitive_grid_relay(self):
        assert et._enum_int(et.GRID_RELAY_MAP, "oper_relay_open") == -1

    def test_int_key_returns_neg1(self):
        assert et._enum_int(et.BATT_MODE_MAP, 0) == -1

    def test_whitespace_key_returns_neg1(self):
        assert et._enum_int(et.BATT_MODE_MAP, " BATT_MODE_FULL_BACKUP ") == -1

    def test_enum_int_returns_int_type(self):
        result = et._enum_int(et.BATT_MODE_MAP, "BATT_MODE_FULL_BACKUP")
        assert isinstance(result, int)

    def test_enum_int_default_is_neg1(self):
        result = et._enum_int(et.BATT_MODE_MAP, "NONEXISTENT_VALUE_XYZ")
        assert result == -1


# ────────────────────────────────────────────────────────────────
# TestConfigChangeDetection  (30 tests)
# ────────────────────────────────────────────────────────────────

class TestConfigChangeDetection:
    """Test that enphase_config is only emitted when state changes."""

    # ── Battery mode changes ──

    def test_batt_mode_first_emit(self, capture_emit):
        """First batt_mode always emits config."""
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1
        assert cfg[0]["fields"]["battery_mode"] == 0
        assert cfg[0]["fields"]["battery_mode_str"] == "BATT_MODE_FULL_BACKUP"

    def test_batt_mode_repeat_no_emit(self, capture_emit):
        """Same batt_mode twice -> only one config emit."""
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1

    def test_batt_mode_change_emits_again(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_SELF_CONS"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 2
        assert cfg[1]["fields"]["battery_mode"] == 1
        assert cfg[1]["fields"]["battery_mode_str"] == "BATT_MODE_SELF_CONS"

    def test_batt_mode_full_backup_to_self_cons(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_SELF_CONS"})
        cfg = _config_calls(capture_emit)
        assert cfg[0]["fields"]["battery_mode"] == 0
        assert cfg[1]["fields"]["battery_mode"] == 1

    def test_batt_mode_self_cons_to_savings(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_SELF_CONS"})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_SAVINGS"})
        cfg = _config_calls(capture_emit)
        assert cfg[0]["fields"]["battery_mode"] == 1
        assert cfg[1]["fields"]["battery_mode"] == 2

    def test_batt_mode_savings_to_full_backup(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_SAVINGS"})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        cfg = _config_calls(capture_emit)
        assert cfg[0]["fields"]["battery_mode"] == 2
        assert cfg[1]["fields"]["battery_mode"] == 0

    def test_batt_mode_three_changes(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_SELF_CONS"})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_SAVINGS"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 3

    def test_batt_mode_none_no_emit(self, capture_emit):
        """None batt_mode should not trigger config emit."""
        et.on_mqtt_data({"batt_mode": None})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 0

    def test_batt_mode_empty_string_no_emit(self, capture_emit):
        """Empty string is falsy, should not trigger."""
        et.on_mqtt_data({"batt_mode": ""})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 0

    def test_batt_mode_with_backup_soc(self, capture_emit):
        """backup_soc (from 'soc' key) should be included in config."""
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP", "soc": 20})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1
        assert cfg[0]["fields"]["backup_reserve_pct"] == 20

    def test_batt_mode_without_backup_soc(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        cfg = _config_calls(capture_emit)
        assert "backup_reserve_pct" not in cfg[0]["fields"]

    # ── Grid relay changes ──

    def test_grid_relay_first_emit(self, capture_emit):
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_CLOSED"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1
        assert cfg[0]["fields"]["grid_relay"] == 2
        assert cfg[0]["fields"]["grid_relay_str"] == "OPER_RELAY_CLOSED"

    def test_grid_relay_repeat_no_emit(self, capture_emit):
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_CLOSED"})
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_CLOSED"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1

    def test_grid_relay_change(self, capture_emit):
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_CLOSED"})
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_OPEN"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 2
        assert cfg[0]["fields"]["grid_relay"] == 2
        assert cfg[1]["fields"]["grid_relay"] == 1

    def test_grid_relay_none_no_emit(self, capture_emit):
        et.on_mqtt_data({"grid_relay": None})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 0

    def test_grid_relay_empty_string_no_emit(self, capture_emit):
        et.on_mqtt_data({"grid_relay": ""})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 0

    # ── Gen relay changes ──

    def test_gen_relay_first_emit(self, capture_emit):
        et.on_mqtt_data({"gen_relay": "OPER_RELAY_GEN_CLOSED"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1
        assert cfg[0]["fields"]["gen_relay"] == 7
        assert cfg[0]["fields"]["gen_relay_str"] == "OPER_RELAY_GEN_CLOSED"

    def test_gen_relay_repeat_no_emit(self, capture_emit):
        et.on_mqtt_data({"gen_relay": "OPER_RELAY_GEN_CLOSED"})
        et.on_mqtt_data({"gen_relay": "OPER_RELAY_GEN_CLOSED"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1

    def test_gen_relay_change(self, capture_emit):
        et.on_mqtt_data({"gen_relay": "OPER_RELAY_GEN_CLOSED"})
        et.on_mqtt_data({"gen_relay": "OPER_RELAY_GEN_OPEN"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 2
        assert cfg[1]["fields"]["gen_relay"] == 6

    def test_gen_relay_none_no_emit(self, capture_emit):
        et.on_mqtt_data({"gen_relay": None})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 0

    # ── Simultaneous changes ──

    def test_all_three_change_simultaneously(self, capture_emit):
        et.on_mqtt_data({
            "batt_mode": "BATT_MODE_FULL_BACKUP",
            "grid_relay": "OPER_RELAY_CLOSED",
            "gen_relay": "OPER_RELAY_GEN_CLOSED",
        })
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 3

    def test_all_three_repeat_no_emit(self, capture_emit):
        msg = {
            "batt_mode": "BATT_MODE_FULL_BACKUP",
            "grid_relay": "OPER_RELAY_CLOSED",
            "gen_relay": "OPER_RELAY_GEN_CLOSED",
        }
        et.on_mqtt_data(msg)
        et.on_mqtt_data(msg)
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 3  # only from first call

    def test_batt_mode_changes_grid_stays(self, capture_emit):
        et.on_mqtt_data({
            "batt_mode": "BATT_MODE_FULL_BACKUP",
            "grid_relay": "OPER_RELAY_CLOSED",
        })
        et.on_mqtt_data({
            "batt_mode": "BATT_MODE_SELF_CONS",
            "grid_relay": "OPER_RELAY_CLOSED",
        })
        cfg = _config_calls(capture_emit)
        # First: batt_mode + grid_relay = 2 config emits
        # Second: only batt_mode = 1 config emit
        assert len(cfg) == 3

    def test_unknown_batt_mode_still_emitted(self, capture_emit):
        """Unknown batt_mode is still emitted (with -1 int value)."""
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FUTURE_THING"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1
        assert cfg[0]["fields"]["battery_mode"] == -1
        assert cfg[0]["fields"]["battery_mode_str"] == "BATT_MODE_FUTURE_THING"

    def test_unknown_grid_relay_still_emitted(self, capture_emit):
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_FUTURE"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 1
        assert cfg[0]["fields"]["grid_relay"] == -1

    def test_config_includes_serial_tag(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP"})
        cfg = _config_calls(capture_emit)
        assert cfg[0]["tags"]["serial"] == "TEST123"

    def test_config_has_timestamp(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP", "timestamp": 1700000000})
        cfg = _config_calls(capture_emit)
        assert cfg[0]["ts_ns"] == 1700000000 * 1_000_000_000

    def test_batt_mode_cycle_all_three(self, capture_emit):
        """Cycle through all 3 batt modes."""
        for mode in ["BATT_MODE_FULL_BACKUP", "BATT_MODE_SELF_CONS", "BATT_MODE_SAVINGS"]:
            et.on_mqtt_data({"batt_mode": mode})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 3
        assert cfg[0]["fields"]["battery_mode"] == 0
        assert cfg[1]["fields"]["battery_mode"] == 1
        assert cfg[2]["fields"]["battery_mode"] == 2

    def test_grid_relay_cycle_open_closed(self, capture_emit):
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_OPEN"})
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_CLOSED"})
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_OPEN"})
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 3
        assert cfg[0]["fields"]["grid_relay"] == 1
        assert cfg[1]["fields"]["grid_relay"] == 2
        assert cfg[2]["fields"]["grid_relay"] == 1


# ────────────────────────────────────────────────────────────────
# TestDryContactTracking  (25 tests)
# ────────────────────────────────────────────────────────────────

class TestDryContactTracking:
    """Test dry contact state-change-only emission."""

    def test_nc1_relay_off(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_OFF"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["tags"]["contact"] == "NC1"
        assert dc[0]["fields"]["state"] == 0

    def test_nc1_relay_on(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["fields"]["state"] == 1

    def test_nc1_relay_invalid(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_STATE_INVALID"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["fields"]["state"] == -1

    def test_nc2_relay_off(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC2", "state": "DC_RELAY_OFF"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["tags"]["contact"] == "NC2"

    def test_no1_relay_on(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NO1", "state": "DC_RELAY_ON"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["tags"]["contact"] == "NO1"

    def test_no2_relay_off(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NO2", "state": "DC_RELAY_OFF"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["tags"]["contact"] == "NO2"

    def test_same_state_twice_only_one_emit(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_OFF"}]})
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_OFF"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1

    def test_state_change_emits_second(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_OFF"}]})
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 2
        assert dc[0]["fields"]["state"] == 0
        assert dc[1]["fields"]["state"] == 1

    def test_unknown_contact_id(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "UNKNOWN_X", "state": "DC_RELAY_OFF"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["tags"]["contact"] == "UNKNOWN_X"

    def test_empty_dry_contacts_list(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": []})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 0

    def test_missing_dry_contacts_key(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 0

    def test_multiple_contacts_in_single_message(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [
            {"id": "NC1", "state": "DC_RELAY_OFF"},
            {"id": "NC2", "state": "DC_RELAY_ON"},
            {"id": "NO1", "state": "DC_RELAY_OFF"},
            {"id": "NO2", "state": "DC_RELAY_ON"},
        ]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 4

    def test_multiple_contacts_repeat_no_emit(self, capture_emit):
        msg = {"dry_contacts": [
            {"id": "NC1", "state": "DC_RELAY_OFF"},
            {"id": "NC2", "state": "DC_RELAY_ON"},
        ]}
        et.on_mqtt_data(msg)
        et.on_mqtt_data(msg)
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 2  # only first call

    def test_one_contact_changes_other_stays(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [
            {"id": "NC1", "state": "DC_RELAY_OFF"},
            {"id": "NC2", "state": "DC_RELAY_OFF"},
        ]})
        et.on_mqtt_data({"dry_contacts": [
            {"id": "NC1", "state": "DC_RELAY_ON"},
            {"id": "NC2", "state": "DC_RELAY_OFF"},
        ]})
        dc = _dry_contact_calls(capture_emit)
        # First: NC1 + NC2 = 2, Second: only NC1 change = 1
        assert len(dc) == 3

    def test_unknown_state_maps_to_neg1(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "UNKNOWN_STATE"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1
        assert dc[0]["fields"]["state"] == -1
        assert dc[0]["fields"]["state_str"] == "UNKNOWN_STATE"

    def test_dry_contact_includes_state_str(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}]})
        dc = _dry_contact_calls(capture_emit)
        assert dc[0]["fields"]["state_str"] == "DC_RELAY_ON"

    def test_dry_contact_serial_tag(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}]})
        dc = _dry_contact_calls(capture_emit)
        assert dc[0]["tags"]["serial"] == "TEST123"

    def test_dry_contact_with_timestamp(self, capture_emit):
        et.on_mqtt_data({
            "timestamp": 1700000000,
            "dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}],
        })
        dc = _dry_contact_calls(capture_emit)
        assert dc[0]["ts_ns"] == 1700000000 * 1_000_000_000

    def test_missing_id_defaults_to_unknown(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"state": "DC_RELAY_ON"}]})
        dc = _dry_contact_calls(capture_emit)
        assert dc[0]["tags"]["contact"] == "unknown"

    def test_missing_state_defaults_to_unknown(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1"}]})
        dc = _dry_contact_calls(capture_emit)
        assert dc[0]["fields"]["state"] == -1
        assert dc[0]["fields"]["state_str"] == "unknown"

    def test_off_to_on_to_off_cycle(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_OFF"}]})
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}]})
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_OFF"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 3
        assert dc[0]["fields"]["state"] == 0
        assert dc[1]["fields"]["state"] == 1
        assert dc[2]["fields"]["state"] == 0

    def test_invalid_to_off(self, capture_emit):
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_STATE_INVALID"}]})
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "DC_RELAY_OFF"}]})
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 2
        assert dc[0]["fields"]["state"] == -1
        assert dc[1]["fields"]["state"] == 0

    def test_two_unknown_states_same_value(self, capture_emit):
        """Two different unknown state strings that both map to -1 => no second emit."""
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "MYSTERY_A"}]})
        et.on_mqtt_data({"dry_contacts": [{"id": "NC1", "state": "MYSTERY_B"}]})
        dc = _dry_contact_calls(capture_emit)
        # Both map to -1, so second should not emit
        assert len(dc) == 1

    def test_contacts_with_power_data(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 5000,
            "dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}],
        })
        pwr = _power_calls(capture_emit)
        dc = _dry_contact_calls(capture_emit)
        assert len(pwr) == 1
        assert len(dc) == 1


# ────────────────────────────────────────────────────────────────
# TestSchemaCheck  (40 tests)
# ────────────────────────────────────────────────────────────────

class TestSchemaCheck:
    """Test _check_schema: protocol version, field tracking, unknown enums."""

    # ── Protocol version ──

    def test_protocol_ver_match_no_error(self, capture_emit):
        """protocol_ver == 1 (expected) should not emit error."""
        et.on_mqtt_data({"protocol_ver": 1, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_version"]
        assert len(errs) == 0

    def test_protocol_ver_mismatch_emits_error(self, capture_emit):
        """protocol_ver != expected emits error."""
        et.on_mqtt_data({"protocol_ver": 2, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_version"]
        assert len(errs) == 1

    def test_protocol_ver_none_no_error(self, capture_emit):
        et.on_mqtt_data({"protocol_ver": None, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_version"]
        assert len(errs) == 0

    def test_protocol_ver_missing_no_error(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_version"]
        assert len(errs) == 0

    def test_protocol_ver_zero_mismatch(self, capture_emit):
        """0 != 1, should emit error."""
        et.on_mqtt_data({"protocol_ver": 0, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_version"]
        assert len(errs) == 1

    def test_protocol_ver_emitted_in_power(self, capture_emit):
        """protocol_ver should appear as a field in enphase_power."""
        et.on_mqtt_data({"protocol_ver": 1, "pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["protocol_ver"] == 1

    def test_protocol_ver_mismatch_also_emits_enphase_error_measurement(self, capture_emit):
        """Mismatch emits both emit_error and an enphase_error measurement."""
        et.on_mqtt_data({"protocol_ver": 2, "pv_power_w": 100})
        err_measurements = [c for c in capture_emit
                            if c.get("measurement") == "enphase_error"
                            and c.get("tags", {}).get("component") == "proto_version"]
        assert len(err_measurements) >= 1

    # ── _fields_present tracking ──

    def test_fields_present_sets_baseline(self, capture_emit):
        """First message with _fields_present sets _known_fields."""
        et.on_mqtt_data({"_fields_present": {"a", "b", "c"}, "pv_power_w": 100})
        assert et._known_fields == {"a", "b", "c"}

    def test_fields_present_same_no_error(self, capture_emit):
        """Same fields on second call -> no error."""
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 200})
        errs = [c for c in capture_emit if c.get("error") == "proto_new_fields"]
        assert len(errs) == 0
        errs2 = [c for c in capture_emit if c.get("error") == "proto_missing_fields"]
        assert len(errs2) == 0

    def test_new_fields_detected(self, capture_emit):
        """New field on second call -> emit error."""
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "b", "c"}, "pv_power_w": 200})
        errs = [c for c in capture_emit if c.get("error") == "proto_new_fields"]
        assert len(errs) == 1

    def test_missing_fields_detected(self, capture_emit):
        et.on_mqtt_data({"_fields_present": {"a", "b", "c"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 200})
        errs = [c for c in capture_emit if c.get("error") == "proto_missing_fields"]
        assert len(errs) == 1

    def test_new_and_missing_fields_simultaneously(self, capture_emit):
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "c"}, "pv_power_w": 200})
        errs_new = [c for c in capture_emit if c.get("error") == "proto_new_fields"]
        errs_miss = [c for c in capture_emit if c.get("error") == "proto_missing_fields"]
        assert len(errs_new) == 1
        assert len(errs_miss) == 1

    def test_fields_present_none_no_baseline(self, capture_emit):
        """None _fields_present should not set baseline."""
        et.on_mqtt_data({"_fields_present": None, "pv_power_w": 100})
        assert et._known_fields is None

    def test_fields_present_empty_set_is_falsy(self, capture_emit):
        """Empty set is falsy, should not set baseline."""
        et.on_mqtt_data({"_fields_present": set(), "pv_power_w": 100})
        assert et._known_fields is None

    def test_new_fields_added_to_known(self, capture_emit):
        """After detecting new fields, _known_fields should be updated."""
        et.on_mqtt_data({"_fields_present": {"a"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 200})
        assert et._known_fields == {"a", "b"}

    def test_missing_fields_not_removed_from_known(self, capture_emit):
        """Missing fields should not be removed from _known_fields."""
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a"}, "pv_power_w": 200})
        # _known_fields still contains both because new fields are added but missing not removed
        assert et._known_fields == {"a", "b"}

    # ── Unknown enum values ──

    def test_unknown_batt_mode_enum_emits_error(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_UNICORN", "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 1

    def test_known_batt_mode_enum_no_error(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP", "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 0

    def test_unknown_grid_relay_enum_emits_error(self, capture_emit):
        et.on_mqtt_data({"grid_relay": "OPER_RELAY_FUTURE", "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 1

    def test_unknown_gen_relay_enum_emits_error(self, capture_emit):
        et.on_mqtt_data({"gen_relay": "OPER_RELAY_FUTURE", "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 1

    def test_unknown_enum_dedup_same_value_twice(self, capture_emit):
        """Same unknown enum value twice -> only one error."""
        et.on_mqtt_data({"batt_mode": "BATT_MODE_UNICORN", "pv_power_w": 100})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_UNICORN", "pv_power_w": 200})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 1

    def test_unknown_enum_different_values_both_error(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_X", "pv_power_w": 100})
        et.on_mqtt_data({"batt_mode": "BATT_MODE_Y", "pv_power_w": 200})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 2

    def test_unknown_enum_added_to_seen_set(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_X", "pv_power_w": 100})
        assert "BATT_MODE_X" in et._unknown_enums_seen

    def test_known_enum_not_added_to_seen_set(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP", "pv_power_w": 100})
        assert "BATT_MODE_FULL_BACKUP" not in et._unknown_enums_seen

    def test_none_batt_mode_no_unknown_enum_error(self, capture_emit):
        """None/falsy batt_mode should not trigger unknown enum check."""
        et.on_mqtt_data({"batt_mode": None, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 0

    def test_empty_batt_mode_no_unknown_enum_error(self, capture_emit):
        """Empty string is falsy, should not trigger."""
        et.on_mqtt_data({"batt_mode": "", "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 0

    def test_unknown_grid_and_gen_both_detected(self, capture_emit):
        et.on_mqtt_data({
            "grid_relay": "RELAY_FUTURE_A",
            "gen_relay": "RELAY_FUTURE_B",
            "pv_power_w": 100,
        })
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 2

    def test_schema_check_called_every_message(self, capture_emit):
        """_mqtt_messages should increment for each call."""
        et.on_mqtt_data({"pv_power_w": 100})
        et.on_mqtt_data({"pv_power_w": 200})
        assert et._mqtt_messages == 2

    def test_protocol_ver_3_mismatch(self, capture_emit):
        et.on_mqtt_data({"protocol_ver": 3, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_version"]
        assert len(errs) == 1

    def test_multiple_fields_present_updates(self, capture_emit):
        """Successive updates expand _known_fields."""
        et.on_mqtt_data({"_fields_present": {"a"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 200})
        et.on_mqtt_data({"_fields_present": {"a", "b", "c"}, "pv_power_w": 300})
        assert et._known_fields == {"a", "b", "c"}

    def test_known_all_grid_relay_values_no_error(self, capture_emit):
        for val in et.GRID_RELAY_MAP:
            et._last_grid_relay = None
            et.on_mqtt_data({"grid_relay": val, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 0

    def test_known_all_batt_mode_values_no_error(self, capture_emit):
        for val in et.BATT_MODE_MAP:
            et._last_batt_mode = None
            et.on_mqtt_data({"batt_mode": val, "pv_power_w": 100})
        errs = [c for c in capture_emit if c.get("error") == "proto_unknown_enum"]
        assert len(errs) == 0

    def test_fields_present_large_set(self, capture_emit):
        big_set = {f"field_{i}" for i in range(100)}
        et.on_mqtt_data({"_fields_present": big_set, "pv_power_w": 100})
        assert len(et._known_fields) == 100

    def test_fields_present_detects_single_new_field(self, capture_emit):
        et.on_mqtt_data({"_fields_present": {"a", "b", "c"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "b", "c", "d"}, "pv_power_w": 200})
        errs = [c for c in capture_emit if c.get("error") == "proto_new_fields"]
        assert len(errs) == 1
        assert "d" in errs[0]["message"]

    def test_fields_present_detects_single_missing_field(self, capture_emit):
        et.on_mqtt_data({"_fields_present": {"a", "b", "c"}, "pv_power_w": 100})
        et.on_mqtt_data({"_fields_present": {"a", "b"}, "pv_power_w": 200})
        errs = [c for c in capture_emit if c.get("error") == "proto_missing_fields"]
        assert len(errs) == 1
        assert "c" in errs[0]["message"]


# ────────────────────────────────────────────────────────────────
# TestAnomalyDetection  (20 tests)
# ────────────────────────────────────────────────────────────────

class TestAnomalyDetection:
    """Test aggregate and per-phase anomaly thresholds."""

    # ── Aggregate power > 100,000 W ──

    def test_solar_99999_ok(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 99999})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_w"] == 99999.0
        errs = _error_calls(capture_emit)
        assert not any("data_quality" in str(e) for e in errs)

    def test_solar_100001_flagged(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100001})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "solar_w" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_solar_neg_100001_flagged(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": -100001})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "solar_w" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_grid_100001_flagged(self, capture_emit):
        et.on_mqtt_data({"grid_power_w": 100001})
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_consumption_100001_flagged(self, capture_emit):
        et.on_mqtt_data({"load_power_w": 100001})
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_battery_100001_flagged(self, capture_emit):
        et.on_mqtt_data({"storage_power_w": 100001})
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_generator_100001_flagged(self, capture_emit):
        et.on_mqtt_data({"generator_power_w": 100001})
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_exactly_100000_ok(self, capture_emit):
        """abs(100000) > 100_000 is False."""
        et.on_mqtt_data({"pv_power_w": 100000})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_w"] == 100000.0
        errs = _error_calls(capture_emit)
        assert not any("data_quality" in str(e) for e in errs)

    def test_exactly_neg_100000_ok(self, capture_emit):
        et.on_mqtt_data({"grid_power_w": -100000})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["grid_w"] == -100000.0
        errs = _error_calls(capture_emit)
        assert not any("data_quality" in str(e) for e in errs)

    # ── Per-phase > 50,000 W ──

    def test_phase_49999_ok(self, capture_emit):
        et.on_mqtt_data({"pv_phase_w": [49999]})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_l1_w"] == 49999.0

    def test_phase_50001_flagged(self, capture_emit):
        et.on_mqtt_data({"pv_phase_w": [50001]})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "solar_l1_w" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_phase_neg_50001_flagged(self, capture_emit):
        et.on_mqtt_data({"grid_phase_w": [-50001]})
        pwr = _power_calls(capture_emit)
        if pwr:
            assert "grid_l1_w" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_phase_50000_ok(self, capture_emit):
        et.on_mqtt_data({"pv_phase_w": [50000]})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_l1_w"] == 50000.0

    def test_phase_mixed_ok_and_bad(self, capture_emit):
        """One phase ok, one anomalous."""
        et.on_mqtt_data({"pv_phase_w": [1000, 60000]})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_l1_w"] == 1000.0
        assert "solar_l2_w" not in pwr[0]["fields"]
        errs = _error_calls(capture_emit)
        assert len(errs) > 0

    # ── SOC anomalies ──

    def test_soc_neg1_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": -1})
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_soc_101_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 101})
        errs = _error_calls(capture_emit)
        assert any("data_quality" in str(e) for e in errs)

    def test_soc_0_no_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 0})
        errs = _error_calls(capture_emit)
        assert not any("data_quality" in str(e) for e in errs)

    def test_soc_100_no_anomaly(self, capture_emit):
        et.on_mqtt_data({"meter_soc": 100})
        errs = _error_calls(capture_emit)
        assert not any("data_quality" in str(e) for e in errs)

    def test_multiple_anomalies_single_error(self, capture_emit):
        """Multiple anomalies in one message -> one data_quality error with all keys."""
        et.on_mqtt_data({
            "pv_power_w": 200000,
            "grid_power_w": 200000,
            "meter_soc": 200,
        })
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 1
        assert "bad_solar_w" in errs[0]["message"]
        assert "bad_grid_w" in errs[0]["message"]
        assert "bad_soc" in errs[0]["message"]

    def test_no_anomaly_no_error_emit(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 5000, "meter_soc": 50})
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0


# ────────────────────────────────────────────────────────────────
# TestTimestamp  (10 tests)
# ────────────────────────────────────────────────────────────────

class TestTimestamp:
    """Test timestamp conversion from msg to ns."""

    def test_timestamp_present(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100, "timestamp": 1700000000})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["ts_ns"] == 1700000000 * 1_000_000_000

    def test_timestamp_none(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100, "timestamp": None})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["ts_ns"] is None

    def test_timestamp_missing(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["ts_ns"] is None

    def test_timestamp_zero_is_falsy(self, capture_emit):
        """0 is falsy, so ts_ns should be None."""
        et.on_mqtt_data({"pv_power_w": 100, "timestamp": 0})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["ts_ns"] is None

    def test_timestamp_large(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100, "timestamp": 2000000000})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["ts_ns"] == 2000000000 * 1_000_000_000

    def test_timestamp_propagated_to_config(self, capture_emit):
        et.on_mqtt_data({"batt_mode": "BATT_MODE_FULL_BACKUP", "timestamp": 1700000000})
        cfg = _config_calls(capture_emit)
        assert cfg[0]["ts_ns"] == 1700000000 * 1_000_000_000

    def test_timestamp_propagated_to_dry_contact(self, capture_emit):
        et.on_mqtt_data({
            "timestamp": 1700000000,
            "dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}],
        })
        dc = _dry_contact_calls(capture_emit)
        assert dc[0]["ts_ns"] == 1700000000 * 1_000_000_000

    def test_timestamp_float_truncated(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100, "timestamp": 1700000000.5})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["ts_ns"] == 1700000000 * 1_000_000_000

    def test_missing_timestamp_does_not_crash(self, capture_emit):
        """Ensure no KeyError for missing timestamp."""
        et.on_mqtt_data({"pv_power_w": 100})
        assert et._mqtt_messages == 1

    def test_timestamp_string_numeric(self, capture_emit):
        """int('1700000000') works."""
        et.on_mqtt_data({"pv_power_w": 100, "timestamp": "1700000000"})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["ts_ns"] == 1700000000 * 1_000_000_000


# ────────────────────────────────────────────────────────────────
# TestMqttMessageCounter  (5 tests)
# ────────────────────────────────────────────────────────────────

class TestMqttMessageCounter:
    """Test that _mqtt_messages increments on each call."""

    def test_counter_starts_at_zero(self, capture_emit):
        assert et._mqtt_messages == 0

    def test_counter_increments_once(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        assert et._mqtt_messages == 1

    def test_counter_increments_thrice(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        et.on_mqtt_data({"pv_power_w": 200})
        et.on_mqtt_data({"pv_power_w": 300})
        assert et._mqtt_messages == 3

    def test_counter_increments_on_empty_msg(self, capture_emit):
        et.on_mqtt_data({})
        assert et._mqtt_messages == 1

    def test_counter_increments_on_error_msg(self, capture_emit):
        """Even messages that trigger anomalies increment counter."""
        et.on_mqtt_data({"pv_power_w": 999999})
        assert et._mqtt_messages == 1


# ────────────────────────────────────────────────────────────────
# TestEmptyMessage  (5 tests)
# ────────────────────────────────────────────────────────────────

class TestEmptyMessage:
    """Test behavior with empty or minimal messages."""

    def test_empty_dict(self, capture_emit):
        et.on_mqtt_data({})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 0

    def test_only_unknown_keys(self, capture_emit):
        et.on_mqtt_data({"unknown_key_xyz": 42})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 0

    def test_all_none_values(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": None,
            "grid_power_w": None,
            "meter_soc": None,
        })
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 0

    def test_single_valid_field_emits_power(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 1})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1

    def test_tags_include_serial_and_source(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["tags"]["serial"] == "TEST123"
        assert pwr[0]["tags"]["source"] == "mqtt"


# ────────────────────────────────────────────────────────────────
# TestClearError  (5 tests)
# ────────────────────────────────────────────────────────────────

class TestClearError:
    """Test that _clear_error removes backoff state."""

    def test_clear_error_removes_entry(self):
        et._error_backoff["mqtt"] = {"last_emit": time.time(), "interval": 60, "message": "test"}
        et._clear_error("mqtt")
        assert "mqtt" not in et._error_backoff

    def test_clear_error_nonexistent_key(self):
        """Clearing a key that doesn't exist should not raise."""
        et._clear_error("nonexistent_component")

    def test_on_mqtt_data_clears_mqtt_error(self, capture_emit):
        """on_mqtt_data calls _clear_error('mqtt')."""
        et._error_backoff["mqtt"] = {"last_emit": time.time(), "interval": 60, "message": "test"}
        et.on_mqtt_data({"pv_power_w": 100})
        assert "mqtt" not in et._error_backoff

    def test_clear_error_only_removes_specified(self):
        et._error_backoff["mqtt"] = {"last_emit": time.time(), "interval": 60, "message": "a"}
        et._error_backoff["cloud"] = {"last_emit": time.time(), "interval": 60, "message": "b"}
        et._clear_error("mqtt")
        assert "mqtt" not in et._error_backoff
        assert "cloud" in et._error_backoff

    def test_clear_error_idempotent(self):
        et._error_backoff["mqtt"] = {"last_emit": time.time(), "interval": 60, "message": "x"}
        et._clear_error("mqtt")
        et._clear_error("mqtt")
        assert "mqtt" not in et._error_backoff


# ────────────────────────────────────────────────────────────────
# TestShouldEmitError  (10 tests)
# ────────────────────────────────────────────────────────────────

class TestShouldEmitError:
    """Test error rate limiter logic."""

    def test_first_call_returns_true(self):
        assert et._should_emit_error("test_comp", "msg") is True

    def test_immediate_second_call_returns_false(self):
        et._should_emit_error("test_comp", "msg")
        assert et._should_emit_error("test_comp", "msg2") is False

    def test_different_component_returns_true(self):
        et._should_emit_error("comp_a", "msg")
        assert et._should_emit_error("comp_b", "msg") is True

    def test_after_interval_returns_true(self):
        et._should_emit_error("test_comp", "msg")
        # Manually set last_emit to the past
        et._error_backoff["test_comp"]["last_emit"] = time.time() - 120
        assert et._should_emit_error("test_comp", "msg2") is True

    def test_backoff_doubles(self):
        et._should_emit_error("test_comp", "msg")
        assert et._error_backoff["test_comp"]["interval"] == 60
        et._error_backoff["test_comp"]["last_emit"] = time.time() - 120
        et._should_emit_error("test_comp", "msg2")
        assert et._error_backoff["test_comp"]["interval"] == 120

    def test_backoff_caps_at_3600(self):
        et._should_emit_error("test_comp", "msg")
        et._error_backoff["test_comp"]["interval"] = 3600
        et._error_backoff["test_comp"]["last_emit"] = time.time() - 7200
        et._should_emit_error("test_comp", "msg2")
        assert et._error_backoff["test_comp"]["interval"] == 3600

    def test_initial_interval_is_60(self):
        et._should_emit_error("test_comp", "msg")
        assert et._error_backoff["test_comp"]["interval"] == 60

    def test_message_stored(self):
        et._should_emit_error("test_comp", "hello")
        assert et._error_backoff["test_comp"]["message"] == "hello"

    def test_last_emit_set(self):
        before = time.time()
        et._should_emit_error("test_comp", "msg")
        after = time.time()
        assert before <= et._error_backoff["test_comp"]["last_emit"] <= after

    def test_multiple_components_independent(self):
        et._should_emit_error("a", "m1")
        et._should_emit_error("b", "m2")
        assert et._error_backoff["a"]["interval"] == 60
        assert et._error_backoff["b"]["interval"] == 60


# ────────────────────────────────────────────────────────────────
# TestPhaseConsistencyCheck  (10 tests)
# ────────────────────────────────────────────────────────────────

class TestPhaseConsistencyCheck:
    """Test phase sum consistency check for solar and consumption."""

    def test_solar_consistent_no_anomaly(self, capture_emit):
        """solar_w=3000, l1=1500, l2=1500 -> sum matches."""
        et.on_mqtt_data({
            "pv_power_w": 3000,
            "pv_phase_w": [1500, 1500],
        })
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0

    def test_solar_inconsistent_triggers_anomaly(self, capture_emit):
        """solar_w=3000 but l1=100, l2=100 -> sum=200, huge discrepancy."""
        et.on_mqtt_data({
            "pv_power_w": 3000,
            "pv_phase_w": [100, 100],
        })
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 1
        assert "bad_solar_phase_sum" in errs[0]["message"]

    def test_consumption_consistent_no_anomaly(self, capture_emit):
        et.on_mqtt_data({
            "load_power_w": 2000,
            "load_phase_w": [1000, 1000],
        })
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0

    def test_consumption_inconsistent_triggers_anomaly(self, capture_emit):
        et.on_mqtt_data({
            "load_power_w": 5000,
            "load_phase_w": [100, 100],
        })
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 1
        assert "bad_consumption_phase_sum" in errs[0]["message"]

    def test_no_phase_data_no_check(self, capture_emit):
        """Without phase data, consistency check is skipped."""
        et.on_mqtt_data({"pv_power_w": 3000})
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0

    def test_only_l1_no_check(self, capture_emit):
        """Need both l1 and l2 for the check."""
        et.on_mqtt_data({"pv_power_w": 3000, "pv_phase_w": [1500]})
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0

    def test_grid_no_consistency_check(self, capture_emit):
        """Grid is not in the consistency check (only solar and consumption)."""
        et.on_mqtt_data({
            "grid_power_w": 5000,
            "grid_phase_w": [100, 100],
        })
        errs = [c for c in capture_emit
                if c.get("error") == "data_quality"
                and "phase_sum" in c.get("message", "")]
        assert len(errs) == 0

    def test_phase_sum_small_values_no_false_positive(self, capture_emit):
        """When phase_sum is near zero (abs < 10), skip the ratio check."""
        et.on_mqtt_data({
            "pv_power_w": 5,
            "pv_phase_w": [3, 2],
        })
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0

    def test_phase_sum_exactly_at_threshold(self, capture_emit):
        """abs(phase_sum) = 10, difference ratio within 0.5."""
        et.on_mqtt_data({
            "pv_power_w": 10,
            "pv_phase_w": [5, 5],
        })
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0

    def test_three_phases_only_uses_l1_l2(self, capture_emit):
        """Consistency check only uses l1+l2, not l3."""
        et.on_mqtt_data({
            "pv_power_w": 3000,
            "pv_phase_w": [1000, 1000, 1000],
        })
        # l1+l2=2000, agg=3000, diff=1000, ratio=0.5 -> exactly 0.5 is NOT > 0.5
        errs = [c for c in capture_emit if c.get("error") == "data_quality"]
        assert len(errs) == 0


# ────────────────────────────────────────────────────────────────
# TestCombinedMessages  (15 tests)
# ────────────────────────────────────────────────────────────────

class TestCombinedMessages:
    """Test full messages with multiple field types."""

    def test_full_realistic_message(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 5000,
            "grid_power_w": -2000,
            "load_power_w": 3000,
            "storage_power_w": 0,
            "pv_apparent_va": 5100,
            "grid_apparent_va": 2100,
            "meter_soc": 85,
            "pcu_total": 20,
            "pcu_running": 20,
            "grid_outage_status": 0,
            "grid_update_ongoing": 0,
            "protocol_ver": 1,
            "timestamp": 1700000000,
            "batt_mode": "BATT_MODE_SELF_CONS",
            "grid_relay": "OPER_RELAY_CLOSED",
        })
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["solar_w"] == 5000.0
        assert pwr[0]["fields"]["grid_w"] == -2000.0
        assert pwr[0]["fields"]["consumption_w"] == 3000.0
        assert pwr[0]["fields"]["battery_w"] == 0.0
        assert pwr[0]["fields"]["solar_va"] == 5100.0
        assert pwr[0]["fields"]["soc"] == 85
        assert pwr[0]["fields"]["inverters_total"] == 20
        assert pwr[0]["fields"]["grid_outage"] == 0
        assert pwr[0]["ts_ns"] == 1700000000 * 1_000_000_000

        cfg = _config_calls(capture_emit)
        assert len(cfg) == 2  # batt_mode + grid_relay

    def test_power_and_dry_contacts(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 100,
            "dry_contacts": [
                {"id": "NC1", "state": "DC_RELAY_ON"},
                {"id": "NC2", "state": "DC_RELAY_OFF"},
            ],
        })
        pwr = _power_calls(capture_emit)
        dc = _dry_contact_calls(capture_emit)
        assert len(pwr) == 1
        assert len(dc) == 2

    def test_power_config_and_contacts(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 100,
            "batt_mode": "BATT_MODE_SAVINGS",
            "dry_contacts": [{"id": "NO1", "state": "DC_RELAY_ON"}],
        })
        pwr = _power_calls(capture_emit)
        cfg = _config_calls(capture_emit)
        dc = _dry_contact_calls(capture_emit)
        assert len(pwr) == 1
        assert len(cfg) == 1
        assert len(dc) == 1

    def test_all_power_sources_simultaneously(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 5000,
            "grid_power_w": -1000,
            "load_power_w": 4000,
            "storage_power_w": -500,
            "generator_power_w": 0,
        })
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert len(pwr[0]["fields"]) == 5

    def test_all_va_sources_simultaneously(self, capture_emit):
        et.on_mqtt_data({
            "pv_apparent_va": 5100,
            "grid_apparent_va": 1100,
            "load_apparent_va": 4100,
            "storage_apparent_va": 550,
            "generator_apparent_va": 10,
        })
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_va"] == 5100.0
        assert pwr[0]["fields"]["generator_va"] == 10.0

    def test_phases_for_all_sources(self, capture_emit):
        et.on_mqtt_data({
            "pv_phase_w": [1000, 2000],
            "grid_phase_w": [500, 600],
            "load_phase_w": [300, 400],
            "storage_phase_w": [100, 200],
            "generator_phase_w": [50, 60],
        })
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_l1_w"] == 1000.0
        assert pwr[0]["fields"]["solar_l2_w"] == 2000.0
        assert pwr[0]["fields"]["grid_l1_w"] == 500.0
        assert pwr[0]["fields"]["generator_l2_w"] == 60.0

    def test_phases_va_for_all_sources(self, capture_emit):
        et.on_mqtt_data({
            "pv_phase_va": [1100, 2200],
            "grid_phase_va": [550, 660],
        })
        pwr = _power_calls(capture_emit)
        assert pwr[0]["fields"]["solar_l1_va"] == 1100.0
        assert pwr[0]["fields"]["grid_l2_va"] == 660.0

    def test_only_anomalous_fields_no_power_emit(self, capture_emit):
        """If all power fields are anomalous, no enphase_power emit."""
        et.on_mqtt_data({
            "pv_power_w": 200000,
            "grid_power_w": 200000,
        })
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 0

    def test_some_ok_some_anomalous(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 5000,
            "grid_power_w": 200000,
        })
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert pwr[0]["fields"]["solar_w"] == 5000.0
        assert "grid_w" not in pwr[0]["fields"]

    def test_message_counter_with_combined(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 100,
            "batt_mode": "BATT_MODE_FULL_BACKUP",
            "dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}],
        })
        assert et._mqtt_messages == 1

    def test_serial_tag_in_all_measurements(self, capture_emit):
        et.on_mqtt_data({
            "pv_power_w": 100,
            "batt_mode": "BATT_MODE_FULL_BACKUP",
            "dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}],
        })
        for c in capture_emit:
            if "tags" in c:
                assert c["tags"]["serial"] == "TEST123"

    def test_power_source_tag_is_mqtt(self, capture_emit):
        et.on_mqtt_data({"pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert pwr[0]["tags"]["source"] == "mqtt"

    def test_all_config_types_in_one_message(self, capture_emit):
        et.on_mqtt_data({
            "batt_mode": "BATT_MODE_FULL_BACKUP",
            "grid_relay": "OPER_RELAY_CLOSED",
            "gen_relay": "OPER_RELAY_GEN_OPEN",
            "soc": 30,
        })
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 3
        batt_cfg = [c for c in cfg if "battery_mode" in c["fields"]]
        assert len(batt_cfg) == 1
        assert batt_cfg[0]["fields"]["backup_reserve_pct"] == 30

    def test_repeated_full_message_only_first_config(self, capture_emit):
        msg = {
            "pv_power_w": 5000,
            "batt_mode": "BATT_MODE_SELF_CONS",
            "grid_relay": "OPER_RELAY_CLOSED",
            "dry_contacts": [{"id": "NC1", "state": "DC_RELAY_ON"}],
        }
        et.on_mqtt_data(msg)
        et.on_mqtt_data(msg)
        cfg = _config_calls(capture_emit)
        assert len(cfg) == 2  # only from first call
        dc = _dry_contact_calls(capture_emit)
        assert len(dc) == 1  # only from first call
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 2  # both calls

    def test_empty_then_full_message(self, capture_emit):
        et.on_mqtt_data({})
        et.on_mqtt_data({"pv_power_w": 100})
        pwr = _power_calls(capture_emit)
        assert len(pwr) == 1
        assert et._mqtt_messages == 2


# ────────────────────────────────────────────────────────────────
# TestGlobalIsolation  (5 tests)
# ────────────────────────────────────────────────────────────────

class TestGlobalIsolation:
    """Verify that the reset fixture properly isolates tests."""

    def test_serial_is_test_value(self):
        assert et._serial == "TEST123"

    def test_last_batt_mode_is_none(self):
        assert et._last_batt_mode is None

    def test_last_grid_relay_is_none(self):
        assert et._last_grid_relay is None

    def test_mqtt_messages_is_zero(self):
        assert et._mqtt_messages == 0

    def test_known_fields_is_none(self):
        assert et._known_fields is None


# ────────────────────────────────────────────────────────────────
# TestEnumIntParametrized  (additional parametrized tests)
# ────────────────────────────────────────────────────────────────

class TestEnumIntParametrized:
    """Additional parametrized _enum_int tests to reach count targets."""

    @pytest.mark.parametrize("name,expected", [
        ("BATT_MODE_FULL_BACKUP", 0),
        ("BATT_MODE_SELF_CONS", 1),
        ("BATT_MODE_SAVINGS", 2),
        ("NONEXISTENT", -1),
        ("", -1),
        (None, -1),
    ])
    def test_batt_mode_map_parametrized(self, name, expected):
        assert et._enum_int(et.BATT_MODE_MAP, name) == expected

    @pytest.mark.parametrize("name,expected", [
        ("OPER_RELAY_OPEN", 1),
        ("OPER_RELAY_CLOSED", 2),
        ("OPER_RELAY_OFFGRID_AC_GRID_PRESENT", 3),
        ("OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD", 4),
        ("OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID", 5),
        ("OPER_RELAY_GEN_OPEN", 6),
        ("OPER_RELAY_GEN_CLOSED", 7),
        ("OPER_RELAY_GEN_STARTUP", 8),
        ("OPER_RELAY_GEN_SYNC_READY", 9),
        ("OPER_RELAY_GEN_AC_STABLE", 10),
        ("OPER_RELAY_GEN_AC_UNSTABLE", 11),
        ("NONEXISTENT", -1),
    ])
    def test_grid_relay_map_parametrized(self, name, expected):
        assert et._enum_int(et.GRID_RELAY_MAP, name) == expected

    @pytest.mark.parametrize("name,expected", [
        ("DC_RELAY_STATE_INVALID", -1),
        ("DC_RELAY_OFF", 0),
        ("DC_RELAY_ON", 1),
        ("NONEXISTENT", -1),
    ])
    def test_dry_contact_state_map_parametrized(self, name, expected):
        assert et._enum_int(et.DRY_CONTACT_STATE_MAP, name) == expected
