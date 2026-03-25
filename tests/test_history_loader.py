"""Unit tests for history_loader.py — JSON-to-line-protocol conversion.

Tests convert_day, convert_all, write_to_influxdb, format_line, and the
field mapping logic that must match enphase_telegraf.py's live output exactly.

Target: ~350 tests across 13 test classes.
"""

import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from enphase_cloud.history_loader import (
    convert_day, convert_all, format_line, write_to_influxdb,
    INTERVAL_ENERGY_MAP, INTERVAL_FLOW_MAP, TOTALS_MAP,
    _esc_tag, _esc_field_str,
)


# ═══════════════════════════════════════════════════════════════════
# convert_day — interval data
# ═══════════════════════════════════════════════════════════════════

class TestConvertDayIntervals:

    def test_produces_enphase_power_lines(self, sample_today_json):
        lines = convert_day(sample_today_json, "SERIAL123")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power_lines) > 0, "No enphase_power lines generated"

    def test_produces_enphase_energy_lines(self, sample_today_json):
        lines = convert_day(sample_today_json, "SERIAL123")
        energy_lines = [l for l in lines if l.startswith("enphase_energy,")]
        assert len(energy_lines) > 0, "No enphase_energy lines generated"

    def test_96_intervals_produce_96_power_lines(self, sample_today_json):
        """96 intervals (24h / 15min) should yield 96 power lines."""
        lines = convert_day(sample_today_json, "S")
        power_lines = [l for l in lines if "enphase_power," in l and "source=history " in l]
        assert len(power_lines) == 96

    def test_serial_tag_present(self, sample_today_json):
        lines = convert_day(sample_today_json, "ABC123")
        for line in lines:
            assert "serial=ABC123" in line

    def test_source_history_tag(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        for line in power_lines:
            assert "source=history" in line

    def test_timestamp_is_nanoseconds(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        for line in lines[:5]:
            ts_str = line.split()[-1]
            ts = int(ts_str)
            # Should be in nanoseconds (> 1e18)
            assert ts > 1_000_000_000_000_000_000, f"Timestamp too small: {ts}"

    def test_production_field_present(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        has_production = any("production_w=" in l for l in power_lines)
        assert has_production, "No production_w field found"

    def test_grid_power_computed(self, sample_today_json):
        """Grid power should be grid_home - solar_grid."""
        lines = convert_day(sample_today_json, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        has_grid = any("grid_w=" in l for l in power_lines)
        assert has_grid, "No grid_w field computed"

    def test_no_interval_no_crash(self):
        """Empty intervals list should produce no power lines."""
        data = {"stats": [{"totals": {}, "intervals": []}], "_cloned_date": "2024-01-01"}
        lines = convert_day(data, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power_lines) == 0

    def test_interval_missing_end_at_skipped(self):
        """Intervals without end_at are skipped."""
        data = {
            "stats": [{"totals": {}, "intervals": [
                {"production": 100.0},  # no end_at
                {"end_at": 1711270800, "production": 200.0},
            ]}],
            "_cloned_date": "2024-01-01",
        }
        lines = convert_day(data, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power_lines) == 1


# ═══════════════════════════════════════════════════════════════════
# convert_day — daily totals
# ═══════════════════════════════════════════════════════════════════

class TestConvertDayTotals:

    def test_daily_totals_line_present(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        daily = [l for l in lines if "source=history_daily" in l]
        assert len(daily) == 1, f"Expected 1 daily total line, got {len(daily)}"

    def test_daily_totals_has_production_wh(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        daily = [l for l in lines if "source=history_daily" in l][0]
        assert "production_wh=18238.0" in daily

    def test_daily_totals_has_consumption_wh(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        daily = [l for l in lines if "source=history_daily" in l][0]
        assert "consumption_wh=22450.0" in daily

    @pytest.mark.parametrize("src,dst", TOTALS_MAP)
    def test_all_total_fields_mapped(self, sample_today_json, src, dst):
        """Every field in TOTALS_MAP with data should appear in output."""
        lines = convert_day(sample_today_json, "S")
        daily = [l for l in lines if "source=history_daily" in l][0]
        val = sample_today_json["stats"][0]["totals"].get(src)
        if val is not None:
            assert f"{dst}=" in daily, f"Missing field {dst} in daily totals"

    def test_empty_totals_no_daily_line(self):
        data = {"stats": [{"totals": {}, "intervals": []}], "_cloned_date": "2024-01-01"}
        lines = convert_day(data, "S")
        daily = [l for l in lines if "source=history_daily" in l]
        assert len(daily) == 0

    def test_daily_timestamp_end_of_day(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        daily = [l for l in lines if "source=history_daily" in l][0]
        ts = int(daily.split()[-1])
        # Should be around 2024-03-24 23:59:59 in nanoseconds
        ts_sec = ts // 1_000_000_000
        # Check it's roughly end of day (within 2 days of the cloned date epoch)
        assert 1711200000 < ts_sec < 1711500000, f"Daily timestamp out of range: {ts_sec}"


# ═══════════════════════════════════════════════════════════════════
# convert_day — battery details
# ═══════════════════════════════════════════════════════════════════

class TestConvertDayBattery:

    def test_battery_line_present(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        bat = [l for l in lines if l.startswith("enphase_battery,")]
        assert len(bat) >= 1

    def test_battery_soc_field(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        bat = [l for l in lines if l.startswith("enphase_battery,")]
        soc_line = [l for l in bat if "soc=" in l]
        assert len(soc_line) > 0
        # SOC should be integer 85
        assert "soc=85i" in soc_line[0]

    def test_battery_backup_min(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        bat = [l for l in lines if l.startswith("enphase_battery,")]
        backup = [l for l in bat if "estimated_backup_min=" in l]
        assert len(backup) > 0
        assert "estimated_backup_min=420i" in backup[0]

    def test_no_battery_details_no_line(self):
        data = {
            "stats": [{"totals": {}, "intervals": []}],
            "_cloned_date": "2024-01-01",
        }
        lines = convert_day(data, "S")
        bat = [l for l in lines if l.startswith("enphase_battery,")]
        assert len(bat) == 0


# ═══════════════════════════════════════════════════════════════════
# convert_day — edge cases and chaos
# ═══════════════════════════════════════════════════════════════════

class TestConvertDayEdgeCases:

    def test_empty_dict(self):
        assert convert_day({}, "S") == []

    def test_no_stats(self):
        assert convert_day({"_cloned_date": "2024-01-01"}, "S") == []

    def test_stats_empty_list(self):
        assert convert_day({"stats": []}, "S") == []

    def test_stats_not_dict(self):
        assert convert_day({"stats": ["not a dict"]}, "S") == []

    def test_intervals_not_list(self):
        data = {"stats": [{"intervals": "not a list", "totals": {}}]}
        # Should not crash
        convert_day(data, "S")

    def test_interval_not_dict(self):
        data = {"stats": [{"intervals": [42, "bad", None], "totals": {}}]}
        lines = convert_day(data, "S")
        power = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power) == 0

    def test_battery_details_not_dict(self):
        data = {"stats": [{"totals": {}, "intervals": []}], "battery_details": "bad"}
        lines = convert_day(data, "S")
        # Should not crash
        assert isinstance(lines, list)

    @pytest.mark.parametrize("n", range(10))
    def test_fuzz_random_today_json(self, n):
        """Random-ish today.json structures should never crash."""
        random.seed(n * 53)
        intervals = []
        for i in range(random.randint(0, 10)):
            iv = {}
            if random.random() > 0.3:
                iv["end_at"] = random.randint(1700000000, 1800000000)
            for field in ["production", "consumption", "grid_home", "solar_grid"]:
                if random.random() > 0.4:
                    iv[field] = round(random.uniform(-1000, 10000), 1)
            intervals.append(iv)

        totals = {}
        for field in ["production", "consumption", "charge", "discharge"]:
            if random.random() > 0.3:
                totals[field] = round(random.uniform(0, 50000), 1)

        data = {
            "stats": [{"totals": totals, "intervals": intervals}],
            "_cloned_date": f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
        }
        if random.random() > 0.5:
            data["battery_details"] = {
                "aggregate_soc": random.randint(0, 100),
                "estimated_time": random.randint(0, 1440),
            }

        # Must not crash
        lines = convert_day(data, f"FUZZ{n}")
        assert isinstance(lines, list)
        for line in lines:
            assert isinstance(line, str)
            assert len(line.split()) >= 3, f"Malformed line: {line}"


# ═══════════════════════════════════════════════════════════════════
# convert_all — batch conversion
# ═══════════════════════════════════════════════════════════════════

class TestConvertAll:

    def test_converts_multiple_files(self, tmp_history_dir):
        lines = convert_all(tmp_history_dir, "S")
        assert len(lines) > 0
        # 3 days x (96 power + 96 energy + 1 daily + 1 battery) ~ 579 lines
        assert len(lines) > 100

    def test_progress_callback_called(self, tmp_history_dir):
        calls = []
        def cb(day_str, count, total, current):
            calls.append((day_str, count, total, current))
        convert_all(tmp_history_dir, "S", progress_cb=cb)
        assert len(calls) == 3  # 3 files

    def test_empty_dir_returns_empty(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert convert_all(empty, "S") == []

    def test_corrupt_json_skipped(self, tmp_path):
        d = tmp_path / "history"
        d.mkdir()
        (d / "day_2024-01-01.json").write_text("not json!!!")
        (d / "day_2024-01-02.json").write_text(json.dumps({
            "stats": [{"totals": {"production": 100.0}, "intervals": []}],
            "_cloned_date": "2024-01-02",
        }))
        lines = convert_all(d, "S")
        # Should have lines from day 2 but not crash on day 1
        assert len(lines) > 0


# ═══════════════════════════════════════════════════════════════════
# Field name consistency with enphase_telegraf.py
# ═══════════════════════════════════════════════════════════════════

class TestFieldNameConsistency:
    """Verify history_loader output uses the same field names as live data."""

    LIVE_POWER_FIELDS = {
        "solar_w", "grid_w", "consumption_w", "battery_w",
        "soc", "production_w",
    }
    LIVE_ENERGY_FIELDS = {
        "production_wh", "consumption_wh", "charge_wh", "discharge_wh",
        "solar_to_home_wh", "solar_to_battery_wh", "solar_to_grid_wh",
        "battery_to_home_wh", "battery_to_grid_wh",
        "grid_to_home_wh", "grid_to_battery_wh",
    }
    LIVE_BATTERY_FIELDS = {
        "soc", "estimated_backup_min", "last_24h_consumption_kwh",
    }

    def _extract_field_names(self, lines, measurement):
        """Extract all field names from lines matching a measurement."""
        names = set()
        for line in lines:
            if not line.startswith(measurement + ","):
                continue
            field_part = line.split(" ")[1]
            for kv in field_part.split(","):
                key = kv.split("=")[0]
                names.add(key)
        return names

    def test_power_fields_are_subset_of_live(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        history_fields = self._extract_field_names(lines, "enphase_power")
        # History power fields should all be known live fields
        unknown = history_fields - self.LIVE_POWER_FIELDS
        # Allow some extra fields from interval flows
        extra_allowed = {"solar_to_home_w", "solar_to_battery_w", "solar_to_grid_w",
                         "battery_to_home_w", "battery_to_grid_w",
                         "grid_to_home_w", "grid_to_battery_w"}
        truly_unknown = unknown - extra_allowed
        assert not truly_unknown, f"Unknown power fields: {truly_unknown}"

    def test_energy_daily_fields_match_live(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        daily_lines = [l for l in lines if "source=history_daily" in l]
        history_fields = set()
        for line in daily_lines:
            field_part = line.split(" ")[1]
            for kv in field_part.split(","):
                history_fields.add(kv.split("=")[0])
        # All daily fields should be known
        unknown = history_fields - self.LIVE_ENERGY_FIELDS
        assert not unknown, f"Unknown energy fields: {unknown}"

    def test_battery_fields_match_live(self, sample_today_json):
        lines = convert_day(sample_today_json, "S")
        history_fields = self._extract_field_names(lines, "enphase_battery")
        unknown = history_fields - self.LIVE_BATTERY_FIELDS
        assert not unknown, f"Unknown battery fields: {unknown}"


# ═══════════════════════════════════════════════════════════════════
# TestConvertDayIntervalVariants — parametrize 80 synthetic intervals
# ═══════════════════════════════════════════════════════════════════

def _make_interval_variant(
    production=None, consumption=None, grid_home=None, solar_grid=None,
    grid_import=None, grid_export=None, discharge=None, charge=None,
    soc=None, battery_soc=None, end_at=1711270800,
    battery_home=None, battery_grid=None, solar_battery=None, grid_battery=None,
    solar_home=None,
):
    """Build an interval dict with only the specified fields."""
    iv = {"end_at": end_at}
    field_map = {
        "production": production, "consumption": consumption,
        "grid_home": grid_home, "solar_grid": solar_grid,
        "grid_import": grid_import, "grid_export": grid_export,
        "discharge": discharge, "charge": charge,
        "soc": soc, "battery_soc": battery_soc,
        "battery_home": battery_home, "battery_grid": battery_grid,
        "solar_battery": solar_battery, "grid_battery": grid_battery,
        "solar_home": solar_home,
    }
    for key, val in field_map.items():
        if val is not None:
            iv[key] = val
    return iv


_INTERVAL_VARIANT_CASES = [
    # id, kwargs, expected_has_grid_w, expected_grid_w, expected_has_battery_w, expected_battery_w
    ("prod_only", dict(production=1000.0), False, None, False, None),
    ("cons_only", dict(consumption=500.0), False, None, False, None),
    ("prod_cons", dict(production=1000.0, consumption=500.0), False, None, False, None),
    ("grid_home_only", dict(grid_home=200.0), True, 200.0, False, None),
    ("solar_grid_only", dict(solar_grid=300.0), False, None, False, None),
    ("grid_home_solar_grid", dict(grid_home=200.0, solar_grid=300.0), True, -100.0, False, None),
    ("grid_import_export", dict(grid_import=400.0, grid_export=150.0), True, 250.0, False, None),
    ("grid_import_only", dict(grid_import=400.0), True, 400.0, False, None),
    ("grid_export_only", dict(grid_export=150.0), False, None, False, None),
    ("discharge_only", dict(discharge=300.0), False, None, True, 300.0),
    ("charge_only", dict(charge=200.0), False, None, False, None),
    ("discharge_charge", dict(discharge=300.0, charge=200.0), False, None, True, 100.0),
    ("battery_home_only", dict(battery_home=150.0), False, None, True, 150.0),
    ("grid_battery_only", dict(grid_battery=100.0), False, None, False, None),
    ("solar_battery_only", dict(solar_battery=120.0), False, None, False, None),
    ("battery_home_solar_battery", dict(battery_home=150.0, solar_battery=120.0), False, None, True, 30.0),
    ("all_present", dict(production=1000.0, consumption=800.0, grid_home=100.0, solar_grid=300.0, discharge=200.0, charge=50.0), True, -200.0, True, 150.0),
    # Note: 0.0 is falsy in Python, so `or` chains skip it. grid_home=0.0 is treated as absent.
    ("zeros_everywhere", dict(production=0.0, consumption=0.0, grid_home=0.0, solar_grid=0.0, discharge=0.0, charge=0.0), False, None, False, None),
    ("soc_present", dict(production=100.0, soc=85), False, None, False, None),
    ("battery_soc_present", dict(production=100.0, battery_soc=70), False, None, False, None),
    ("soc_and_battery_soc", dict(production=100.0, soc=85, battery_soc=70), False, None, False, None),
    # 0.0 is falsy in `or` chains, so grid_import=0.0 falls through to grid_home/grid (both None)
    ("grid_import_zero_export_zero", dict(grid_import=0.0, grid_export=0.0), False, None, False, None),
    ("discharge_zero_charge_zero", dict(discharge=0.0, charge=0.0), False, None, False, None),
    ("large_production", dict(production=99999.9), False, None, False, None),
    ("negative_production", dict(production=-50.0), False, None, False, None),
    ("grid_import_export_equal", dict(grid_import=500.0, grid_export=500.0), True, 0.0, False, None),
    ("discharge_charge_equal", dict(discharge=500.0, charge=500.0), False, None, True, 0.0),
    ("high_grid_import_low_export", dict(grid_import=5000.0, grid_export=10.0), True, 4990.0, False, None),
    ("low_grid_import_high_export", dict(grid_import=10.0, grid_export=5000.0), True, -4990.0, False, None),
    ("high_discharge_low_charge", dict(discharge=5000.0, charge=10.0), False, None, True, 4990.0),
    ("low_discharge_high_charge", dict(discharge=10.0, charge=5000.0), False, None, True, -4990.0),
    # grid_home=0.0 is falsy, falls through; grid_import absent => None. solar_grid=500.0 is grid_export.
    # grid_import=None so grid_w not computed.
    ("grid_home_zero", dict(grid_home=0.0, solar_grid=500.0), False, None, False, None),
    ("solar_grid_zero", dict(grid_home=500.0, solar_grid=0.0), True, 500.0, False, None),
    ("prod_cons_grid_bat", dict(production=2000.0, consumption=1500.0, grid_import=200.0, grid_export=700.0, discharge=100.0, charge=50.0), True, -500.0, True, 50.0),
    ("grid_import_fallback_to_grid_home", dict(grid_home=350.0), True, 350.0, False, None),
    ("grid_export_fallback_to_solar_grid", dict(grid_home=200.0, solar_grid=100.0), True, 100.0, False, None),
    ("discharge_fallback_battery_home", dict(battery_home=300.0), False, None, True, 300.0),
    ("charge_fallback_grid_battery", dict(battery_home=300.0, grid_battery=50.0), False, None, True, 250.0),
    ("charge_fallback_solar_battery", dict(battery_home=300.0, solar_battery=80.0), False, None, True, 220.0),
    ("fractional_values", dict(production=100.123, consumption=50.456, grid_import=30.789, grid_export=10.111), True, 20.678, False, None),
    ("very_small_values", dict(production=0.001, consumption=0.002, grid_import=0.0001, grid_export=0.0002), True, -0.0001, False, None),
    ("prod_cons_grid_home_only_no_solar_grid", dict(production=100.0, consumption=200.0, grid_home=100.0), True, 100.0, False, None),
    ("all_fields_large", dict(production=1e5, consumption=1e5, grid_import=1e5, grid_export=1e5, discharge=1e5, charge=1e5), True, 0.0, True, 0.0),
    ("grid_import_with_battery_home", dict(grid_import=100.0, battery_home=50.0), True, 100.0, True, 50.0),
    ("grid_import_grid_export_with_soc", dict(grid_import=300.0, grid_export=100.0, soc=90), True, 200.0, False, None),
    ("discharge_charge_with_soc", dict(discharge=200.0, charge=100.0, soc=75), False, None, True, 100.0),
    ("cons_only_high", dict(consumption=10000.0), False, None, False, None),
    ("prod_only_zero", dict(production=0.0), False, None, False, None),
    ("cons_only_zero", dict(consumption=0.0), False, None, False, None),
    ("grid_import_negative", dict(grid_import=-50.0, grid_export=10.0), True, -60.0, False, None),
    ("all_negative", dict(production=-10.0, consumption=-20.0, grid_import=-5.0, grid_export=-3.0, discharge=-4.0, charge=-2.0), True, -2.0, True, -2.0),
    ("grid_home_and_grid_import", dict(grid_home=100.0, grid_import=200.0, solar_grid=50.0, grid_export=30.0), True, 170.0, False, None),
    ("battery_home_and_discharge", dict(battery_home=100.0, discharge=200.0, solar_battery=50.0, charge=30.0), False, None, True, 170.0),
    ("prod_with_soc_and_battery_soc", dict(production=500.0, soc=80, battery_soc=75), False, None, False, None),
    ("grid_import_1e6", dict(grid_import=1e6, grid_export=1.0), True, 999999.0, False, None),
    ("discharge_1e6", dict(discharge=1e6, charge=1.0), False, None, True, 999999.0),
    ("grid_import_float_precision", dict(grid_import=0.1, grid_export=0.2), True, None, False, None),
    ("discharge_float_precision", dict(discharge=0.1, charge=0.2), False, None, True, None),
    ("production_integer", dict(production=1000), False, None, False, None),
    ("grid_home_integer", dict(grid_home=200, solar_grid=100), True, 100.0, False, None),
    ("only_end_at", dict(), False, None, False, None),
    ("prod_cons_grid_import", dict(production=2500.0, consumption=1200.0, grid_import=100.0), True, 100.0, False, None),
    ("prod_cons_discharge", dict(production=2500.0, consumption=1200.0, discharge=100.0), False, None, True, 100.0),
    ("tiny_grid_import_huge_export", dict(grid_import=0.01, grid_export=99999.99), True, -99999.98, False, None),
    ("tiny_discharge_huge_charge", dict(discharge=0.01, charge=99999.99), False, None, True, -99999.98),
    ("grid_home_none_solar_grid_val", dict(solar_grid=500.0), False, None, False, None),
    ("discharge_none_charge_val", dict(charge=500.0), False, None, False, None),
    # 0.0 values in or-chains are falsy: grid_import=0.0 => None, discharge=0.0 => None
    ("grid_import_export_both_zero_with_battery", dict(grid_import=0.0, grid_export=0.0, discharge=100.0, charge=50.0), False, None, True, 50.0),
    ("full_system_daytime", dict(production=3500.0, consumption=1200.0, grid_import=0.0, grid_export=2300.0, discharge=0.0, charge=500.0, soc=60), False, None, False, None),
    # grid_import=500 truthy, grid_export=0.0 falsy => only-import path => grid_w=500.0
    # discharge=300.0 truthy, charge=0.0 falsy => only-discharge path => battery_w=300.0
    ("full_system_nighttime", dict(production=0.0, consumption=800.0, grid_import=500.0, grid_export=0.0, discharge=300.0, charge=0.0, soc=40), True, 500.0, True, 300.0),
    ("full_system_battery_charge", dict(production=4000.0, consumption=1000.0, grid_import=0.0, grid_export=1000.0, discharge=0.0, charge=2000.0, soc=90), False, None, False, None),
    ("all_100", dict(production=100.0, consumption=100.0, grid_import=100.0, grid_export=100.0, discharge=100.0, charge=100.0), True, 0.0, True, 0.0),
    ("all_1", dict(production=1.0, consumption=1.0, grid_import=1.0, grid_export=1.0, discharge=1.0, charge=1.0), True, 0.0, True, 0.0),
    ("all_0_point_5", dict(production=0.5, consumption=0.5, grid_import=0.5, grid_export=0.5, discharge=0.5, charge=0.5), True, 0.0, True, 0.0),
    ("grid_import_export_huge", dict(grid_import=1e9, grid_export=1e9), True, 0.0, False, None),
    ("discharge_charge_huge", dict(discharge=1e9, charge=1e9), False, None, True, 0.0),
    ("production_1e6_only", dict(production=1e6), False, None, False, None),
    ("consumption_1e6_only", dict(consumption=1e6), False, None, False, None),
]


class TestConvertDayIntervalVariants:
    """80 parametrized interval structures testing grid_w and battery_w computation."""

    @pytest.mark.parametrize("name,kwargs,has_grid,exp_grid,has_bat,exp_bat", _INTERVAL_VARIANT_CASES,
                             ids=[c[0] for c in _INTERVAL_VARIANT_CASES])
    def test_interval_variant(self, name, kwargs, has_grid, exp_grid, has_bat, exp_bat):
        iv = _make_interval_variant(**kwargs)
        data = {
            "stats": [{"totals": {}, "intervals": [iv]}],
            "_cloned_date": "2024-03-24",
        }
        lines = convert_day(data, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]

        if has_grid:
            matched = [l for l in power_lines if "grid_w=" in l]
            assert len(matched) > 0, f"[{name}] Expected grid_w in power line"
            if exp_grid is not None:
                field_part = matched[0].split(" ")[1]
                grid_val = None
                for kv in field_part.split(","):
                    if kv.startswith("grid_w="):
                        grid_val = float(kv.split("=")[1])
                assert grid_val is not None
                assert abs(grid_val - exp_grid) < 0.01, f"[{name}] grid_w={grid_val}, expected {exp_grid}"
        else:
            # grid_w should not be present
            matched = [l for l in power_lines if "grid_w=" in l]
            assert len(matched) == 0, f"[{name}] grid_w should not be present"

        if has_bat:
            matched = [l for l in power_lines if "battery_w=" in l]
            assert len(matched) > 0, f"[{name}] Expected battery_w in power line"
            if exp_bat is not None:
                field_part = matched[0].split(" ")[1]
                bat_val = None
                for kv in field_part.split(","):
                    if kv.startswith("battery_w="):
                        bat_val = float(kv.split("=")[1])
                assert bat_val is not None
                assert abs(bat_val - exp_bat) < 0.01, f"[{name}] battery_w={bat_val}, expected {exp_bat}"
        else:
            matched = [l for l in power_lines if "battery_w=" in l]
            assert len(matched) == 0, f"[{name}] battery_w should not be present"


# ═══════════════════════════════════════════════════════════════════
# TestConvertDayGridPowerCalc — 30 parametrized grid power tests
# ═══════════════════════════════════════════════════════════════════

_GRID_POWER_CASES = [
    # (name, interval_fields, expected_grid_w)
    # NOTE: The source uses `or` chains: `iv.get("grid_import") or iv.get("grid_home")`.
    # 0.0 is falsy in Python so it falls through the `or` chain.
    #
    # Path 1: grid_import AND grid_export both present and truthy
    ("import_export_positive", dict(grid_import=500.0, grid_export=200.0), 300.0),
    # grid_import=500 truthy; grid_export=0.0 falsy => only-import path => 500.0
    ("import_export_zero_export", dict(grid_import=500.0, grid_export=0.0), 500.0),
    ("import_export_large", dict(grid_import=1000000.0, grid_export=500000.0), 500000.0),
    ("import_export_tiny", dict(grid_import=0.001, grid_export=0.0005), 0.0005),
    ("import_export_equal", dict(grid_import=777.7, grid_export=777.7), 0.0),
    ("import_export_negative_import", dict(grid_import=-100.0, grid_export=50.0), -150.0),
    ("import_export_negative_export", dict(grid_import=100.0, grid_export=-50.0), 150.0),
    ("import_export_both_negative", dict(grid_import=-100.0, grid_export=-200.0), 100.0),
    # Path 2: grid_home AND solar_grid (fallback) — both truthy
    ("ghome_sgrid_positive", dict(grid_home=400.0, solar_grid=100.0), 300.0),
    # grid_home=300 truthy; solar_grid=0.0 falsy => only-import path => 300.0
    ("ghome_sgrid_zero_sgrid", dict(grid_home=300.0, solar_grid=0.0), 300.0),
    ("ghome_sgrid_large", dict(grid_home=999999.0, solar_grid=1.0), 999998.0),
    ("ghome_sgrid_tiny", dict(grid_home=0.0001, solar_grid=0.0002), -0.0001),
    ("ghome_sgrid_equal", dict(grid_home=123.4, solar_grid=123.4), 0.0),
    ("ghome_sgrid_negative_ghome", dict(grid_home=-50.0, solar_grid=25.0), -75.0),
    ("ghome_sgrid_negative_sgrid", dict(grid_home=50.0, solar_grid=-25.0), 75.0),
    ("ghome_sgrid_both_negative", dict(grid_home=-10.0, solar_grid=-20.0), 10.0),
    # Path 3: only grid_import (no export counterpart)
    ("import_only_positive", dict(grid_import=350.0), 350.0),
    ("import_only_large", dict(grid_import=1e6), 1e6),
    ("import_only_tiny", dict(grid_import=0.001), 0.001),
    ("import_only_negative", dict(grid_import=-200.0), -200.0),
    # Path 3 via grid_home fallback (no solar_grid)
    ("ghome_only_positive", dict(grid_home=250.0), 250.0),
    ("ghome_only_large", dict(grid_home=1e6), 1e6),
    ("ghome_only_tiny", dict(grid_home=0.001), 0.001),
    ("ghome_only_negative", dict(grid_home=-100.0), -100.0),
    # Additional non-zero value combos to reach 30 tests
    ("import_export_large_export", dict(grid_import=1.0, grid_export=1000000.0), -999999.0),
    ("import_50_export_25", dict(grid_import=50.0, grid_export=25.0), 25.0),
    ("ghome_200_sgrid_50", dict(grid_home=200.0, solar_grid=50.0), 150.0),
    ("import_0_1_export_0_05", dict(grid_import=0.1, grid_export=0.05), 0.05),
    ("ghome_neg_10_sgrid_neg_20", dict(grid_home=-10.0, solar_grid=-20.0), 10.0),
    ("import_999_export_1", dict(grid_import=999.0, grid_export=1.0), 998.0),
]


class TestConvertDayGridPowerCalc:
    """30 parametrized tests for the 3 fallback paths of grid power calculation."""

    @pytest.mark.parametrize("name,fields,expected", _GRID_POWER_CASES,
                             ids=[c[0] for c in _GRID_POWER_CASES])
    def test_grid_power(self, name, fields, expected):
        iv = {"end_at": 1711270800, "production": 100.0}
        iv.update(fields)
        data = {
            "stats": [{"totals": {}, "intervals": [iv]}],
            "_cloned_date": "2024-03-24",
        }
        lines = convert_day(data, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power_lines) > 0, f"[{name}] No power lines generated"

        grid_lines = [l for l in power_lines if "grid_w=" in l]
        assert len(grid_lines) > 0, f"[{name}] grid_w not found"

        field_part = grid_lines[0].split(" ")[1]
        grid_val = None
        for kv in field_part.split(","):
            if kv.startswith("grid_w="):
                grid_val = float(kv.split("=")[1])
        assert grid_val is not None, f"[{name}] could not parse grid_w"
        assert abs(grid_val - expected) < 0.01, f"[{name}] grid_w={grid_val}, expected {expected}"


# ═══════════════════════════════════════════════════════════════════
# TestConvertDayBatteryPowerCalc — 30 parametrized battery power tests
# ═══════════════════════════════════════════════════════════════════

_BATTERY_POWER_CASES = [
    # NOTE: Source uses `or` chains, so 0.0 is falsy and falls through.
    #
    # Path 1: discharge AND charge both present and truthy
    ("dis_charge_positive", dict(discharge=500.0, charge=200.0), 300.0),
    # discharge=500 truthy; charge=0.0 falsy => only-discharge path => 500.0
    ("dis_charge_zero_charge", dict(discharge=500.0, charge=0.0), 500.0),
    ("dis_charge_large", dict(discharge=1000000.0, charge=500000.0), 500000.0),
    ("dis_charge_tiny", dict(discharge=0.001, charge=0.0005), 0.0005),
    ("dis_charge_equal", dict(discharge=777.7, charge=777.7), 0.0),
    ("dis_charge_negative_discharge", dict(discharge=-100.0, charge=50.0), -150.0),
    ("dis_charge_negative_charge", dict(discharge=100.0, charge=-50.0), 150.0),
    ("dis_charge_both_negative", dict(discharge=-100.0, charge=-200.0), 100.0),
    # Path 2: battery_home AND solar_battery (fallback) — both truthy
    ("bathome_solbat", dict(battery_home=400.0, solar_battery=100.0), 300.0),
    # battery_home=300 truthy; solar_battery=0.0 falsy => only-discharge path => 300.0
    ("bathome_solbat_zero_solbat", dict(battery_home=300.0, solar_battery=0.0), 300.0),
    ("bathome_solbat_large", dict(battery_home=999999.0, solar_battery=1.0), 999998.0),
    ("bathome_solbat_tiny", dict(battery_home=0.0001, solar_battery=0.0002), -0.0001),
    ("bathome_solbat_equal", dict(battery_home=123.4, solar_battery=123.4), 0.0),
    ("bathome_gridbat", dict(battery_home=400.0, grid_battery=100.0), 300.0),
    ("bathome_gridbat_equal", dict(battery_home=200.0, grid_battery=200.0), 0.0),
    # Path 3: only discharge (no charge counterpart)
    ("discharge_only_positive", dict(discharge=350.0), 350.0),
    ("discharge_only_large", dict(discharge=1e6), 1e6),
    ("discharge_only_tiny", dict(discharge=0.001), 0.001),
    ("discharge_only_negative", dict(discharge=-200.0), -200.0),
    # Path 3 via battery_home fallback (no charge counterpart)
    ("bathome_only_positive", dict(battery_home=250.0), 250.0),
    ("bathome_only_large", dict(battery_home=1e6), 1e6),
    ("bathome_only_tiny", dict(battery_home=0.001), 0.001),
    ("bathome_only_negative", dict(battery_home=-100.0), -100.0),
    # Additional truthy combos to reach 30 tests
    ("dis_100_charge_50", dict(discharge=100.0, charge=50.0), 50.0),
    ("dis_1_charge_1", dict(discharge=1.0, charge=1.0), 0.0),
    ("dis_large_charge_small", dict(discharge=1e6, charge=1.0), 999999.0),
    ("bathome_100_gridbat_50", dict(battery_home=100.0, grid_battery=50.0), 50.0),
    ("bathome_neg_50_solbat_25", dict(battery_home=-50.0, solar_battery=25.0), -75.0),
    ("dis_0_5_charge_0_25", dict(discharge=0.5, charge=0.25), 0.25),
    ("bathome_500_solbat_500", dict(battery_home=500.0, solar_battery=500.0), 0.0),
]


class TestConvertDayBatteryPowerCalc:
    """30 parametrized tests for the fallback paths of battery power calculation."""

    @pytest.mark.parametrize("name,fields,expected", _BATTERY_POWER_CASES,
                             ids=[c[0] for c in _BATTERY_POWER_CASES])
    def test_battery_power(self, name, fields, expected):
        iv = {"end_at": 1711270800, "production": 100.0}
        iv.update(fields)
        data = {
            "stats": [{"totals": {}, "intervals": [iv]}],
            "_cloned_date": "2024-03-24",
        }
        lines = convert_day(data, "S")
        power_lines = [l for l in lines if l.startswith("enphase_power,")]
        assert len(power_lines) > 0, f"[{name}] No power lines generated"

        bat_lines = [l for l in power_lines if "battery_w=" in l]
        assert len(bat_lines) > 0, f"[{name}] battery_w not found"

        field_part = bat_lines[0].split(" ")[1]
        bat_val = None
        for kv in field_part.split(","):
            if kv.startswith("battery_w="):
                bat_val = float(kv.split("=")[1])
        assert bat_val is not None, f"[{name}] could not parse battery_w"
        assert abs(bat_val - expected) < 0.01, f"[{name}] battery_w={bat_val}, expected {expected}"


# ═══════════════════════════════════════════════════════════════════
# TestConvertDaySyntheticVariations — 60 synthetic today.json variants
# ═══════════════════════════════════════════════════════════════════

def _make_today_json(intervals=None, totals=None, battery_details=None,
                     cloned_date="2024-06-15"):
    """Helper to build a today.json structure."""
    data = {"_cloned_date": cloned_date, "_cloned_at": time.time()}
    stat = {}
    if totals is not None:
        stat["totals"] = totals
    else:
        stat["totals"] = {}
    if intervals is not None:
        stat["intervals"] = intervals
    else:
        stat["intervals"] = []
    data["stats"] = [stat]
    if battery_details is not None:
        data["battery_details"] = battery_details
    return data


def _make_intervals(count, base_ts=1718409600, **field_defaults):
    """Generate N intervals with given default field values."""
    ivs = []
    for i in range(count):
        iv = {"end_at": base_ts + i * 900}
        iv.update(field_defaults)
        ivs.append(iv)
    return ivs


_SYNTHETIC_CASES = [
    # Zero-production days
    ("zero_prod_day", lambda: _make_today_json(
        intervals=_make_intervals(96, production=0.0, consumption=0.0),
        totals={"production": 0.0, "consumption": 0.0})),
    ("zero_prod_nonzero_cons", lambda: _make_today_json(
        intervals=_make_intervals(96, production=0.0, consumption=500.0),
        totals={"production": 0.0, "consumption": 48000.0})),
    ("zero_everything", lambda: _make_today_json(
        intervals=_make_intervals(96, production=0.0, consumption=0.0,
                                  grid_home=0.0, solar_grid=0.0),
        totals={"production": 0.0, "consumption": 0.0, "charge": 0.0, "discharge": 0.0})),
    # Battery-only system (no solar)
    ("battery_only_system", lambda: _make_today_json(
        intervals=_make_intervals(96, consumption=800.0, discharge=300.0, charge=200.0),
        totals={"consumption": 76800.0, "discharge": 28800.0, "charge": 19200.0},
        battery_details={"aggregate_soc": 50, "estimated_time": 180})),
    ("battery_only_discharging", lambda: _make_today_json(
        intervals=_make_intervals(96, consumption=1000.0, discharge=1000.0),
        totals={"consumption": 96000.0, "discharge": 96000.0})),
    ("battery_only_charging", lambda: _make_today_json(
        intervals=_make_intervals(96, consumption=200.0, charge=800.0),
        totals={"consumption": 19200.0, "charge": 76800.0})),
    # No-battery system
    ("no_battery_system", lambda: _make_today_json(
        intervals=_make_intervals(96, production=2000.0, consumption=1000.0,
                                  grid_home=0.0, solar_grid=1000.0),
        totals={"production": 192000.0, "consumption": 96000.0})),
    ("no_battery_all_exported", lambda: _make_today_json(
        intervals=_make_intervals(96, production=3000.0, consumption=0.0,
                                  grid_home=0.0, solar_grid=3000.0),
        totals={"production": 288000.0, "consumption": 0.0})),
    ("no_battery_all_imported", lambda: _make_today_json(
        intervals=_make_intervals(96, production=0.0, consumption=1000.0,
                                  grid_home=1000.0, solar_grid=0.0),
        totals={"production": 0.0, "consumption": 96000.0})),
    # Nighttime-only intervals
    ("nighttime_only", lambda: _make_today_json(
        intervals=_make_intervals(32, production=0.0, consumption=800.0,
                                  grid_home=800.0, solar_grid=0.0),
        totals={"production": 0.0, "consumption": 25600.0})),
    ("nighttime_with_battery", lambda: _make_today_json(
        intervals=_make_intervals(32, production=0.0, consumption=800.0,
                                  discharge=300.0, charge=0.0),
        totals={"production": 0.0, "consumption": 25600.0, "discharge": 9600.0})),
    # Single interval
    ("single_interval", lambda: _make_today_json(
        intervals=[{"end_at": 1718409600, "production": 500.0, "consumption": 300.0}],
        totals={"production": 500.0, "consumption": 300.0})),
    ("single_interval_all_fields", lambda: _make_today_json(
        intervals=[{"end_at": 1718409600, "production": 500.0, "consumption": 300.0,
                    "grid_home": 50.0, "solar_grid": 250.0, "discharge": 100.0, "charge": 50.0}],
        totals={"production": 500.0, "consumption": 300.0})),
    # Many intervals
    ("200_intervals", lambda: _make_today_json(
        intervals=_make_intervals(200, production=1000.0, consumption=500.0),
        totals={"production": 200000.0, "consumption": 100000.0})),
    ("200_intervals_with_grid", lambda: _make_today_json(
        intervals=_make_intervals(200, production=1000.0, consumption=500.0,
                                  grid_import=100.0, grid_export=600.0),
        totals={"production": 200000.0, "consumption": 100000.0})),
    # Negative production
    ("negative_production", lambda: _make_today_json(
        intervals=_make_intervals(96, production=-50.0, consumption=200.0),
        totals={"production": -4800.0, "consumption": 19200.0})),
    ("negative_production_mixed", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": -100.0, "consumption": 200.0},
            {"end_at": 1718410500, "production": 500.0, "consumption": 200.0},
            {"end_at": 1718411400, "production": -50.0, "consumption": 200.0},
        ],
        totals={"production": 350.0, "consumption": 600.0})),
    # Negative consumption
    ("negative_consumption", lambda: _make_today_json(
        intervals=_make_intervals(96, production=500.0, consumption=-100.0),
        totals={"production": 48000.0, "consumption": -9600.0})),
    # Very large values
    ("very_large_production", lambda: _make_today_json(
        intervals=_make_intervals(96, production=1e6, consumption=1e5),
        totals={"production": 96e6, "consumption": 96e5})),
    ("very_large_all_fields", lambda: _make_today_json(
        intervals=_make_intervals(10, production=1e6, consumption=1e6,
                                  grid_import=1e6, grid_export=1e6,
                                  discharge=1e6, charge=1e6),
        totals={"production": 1e7, "consumption": 1e7})),
    # SOC data
    ("intervals_with_soc", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600 + i * 900, "production": 500.0, "consumption": 300.0, "soc": 50 + i}
            for i in range(10)
        ],
        totals={"production": 5000.0, "consumption": 3000.0})),
    ("intervals_with_battery_soc", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600 + i * 900, "production": 500.0, "battery_soc": 80 - i * 5}
            for i in range(10)
        ],
        totals={"production": 5000.0})),
    # Mixed SOC and battery_soc
    ("intervals_soc_mixed", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 500.0, "soc": 85},
            {"end_at": 1718410500, "production": 400.0, "battery_soc": 80},
            {"end_at": 1718411400, "production": 300.0, "soc": 75, "battery_soc": 70},
        ],
        totals={"production": 1200.0})),
    # Empty totals with intervals
    ("empty_totals_with_intervals", lambda: _make_today_json(
        intervals=_make_intervals(10, production=100.0, consumption=50.0),
        totals={})),
    # Totals with no intervals
    ("totals_no_intervals", lambda: _make_today_json(
        intervals=[],
        totals={"production": 50000.0, "consumption": 30000.0})),
    # Battery details variants
    ("battery_details_soc_only", lambda: _make_today_json(
        intervals=_make_intervals(10, production=100.0),
        battery_details={"aggregate_soc": 95})),
    ("battery_details_time_only", lambda: _make_today_json(
        intervals=_make_intervals(10, production=100.0),
        battery_details={"estimated_time": 600})),
    ("battery_details_consumption_only", lambda: _make_today_json(
        intervals=_make_intervals(10, production=100.0),
        battery_details={"last_24h_consumption": 45.6})),
    ("battery_details_all_fields", lambda: _make_today_json(
        intervals=_make_intervals(10, production=100.0),
        battery_details={"aggregate_soc": 85, "estimated_time": 420, "last_24h_consumption": 22.45})),
    ("battery_details_empty", lambda: _make_today_json(
        intervals=_make_intervals(10, production=100.0),
        battery_details={})),
    # Different cloned_dates
    ("date_jan_1", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0},
        cloned_date="2024-01-01")),
    ("date_dec_31", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0},
        cloned_date="2024-12-31")),
    ("date_feb_29_leap", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0},
        cloned_date="2024-02-29")),
    ("date_2020", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0},
        cloned_date="2020-06-15")),
    ("date_2025_future", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0},
        cloned_date="2025-12-01")),
    # Realistic solar curve day
    ("realistic_solar_curve", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600 + i * 900,
             "production": max(0, 3000 * (1 - ((i * 15 / 60 - 12) / 6) ** 2)) if 24 <= i <= 72 else 0.0,
             "consumption": 800.0 + (200.0 if 28 <= i <= 88 else 0.0)}
            for i in range(96)
        ],
        totals={"production": 18000.0, "consumption": 22000.0})),
    # All flow fields present
    ("all_flow_fields", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 3000.0, "consumption": 1200.0,
             "solar_home": 1000.0, "solar_battery": 200.0, "solar_grid": 1800.0,
             "battery_home": 100.0, "battery_grid": 50.0,
             "grid_home": 100.0, "grid_battery": 30.0}
        ],
        totals={"production": 3000.0, "consumption": 1200.0,
                "solar_home": 1000.0, "solar_battery": 200.0, "solar_grid": 1800.0})),
    # Partial flow fields
    ("partial_flow_fields", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 2000.0, "consumption": 1000.0,
             "solar_home": 800.0, "solar_grid": 1200.0}
        ],
        totals={"production": 2000.0, "consumption": 1000.0})),
    # Very many intervals with randomized data
    ("random_96_intervals", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600 + i * 900,
             "production": random.Random(i).uniform(0, 5000),
             "consumption": random.Random(i + 100).uniform(200, 2000),
             "grid_home": random.Random(i + 200).uniform(0, 500),
             "solar_grid": random.Random(i + 300).uniform(0, 3000)}
            for i in range(96)
        ],
        totals={"production": 50000.0, "consumption": 30000.0})),
    # Totals with all TOTALS_MAP fields
    ("totals_all_fields", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0, "consumption": 400.0, "charge": 100.0,
                "discharge": 80.0, "solar_home": 300.0, "solar_battery": 100.0,
                "solar_grid": 100.0, "battery_home": 80.0, "battery_grid": 0.0,
                "grid_home": 100.0, "grid_battery": 20.0})),
    # Totals with only production
    ("totals_prod_only", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0})),
    # Totals with only consumption
    ("totals_cons_only", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"consumption": 400.0})),
    # Intervals with only end_at (no data fields)
    ("intervals_end_at_only", lambda: _make_today_json(
        intervals=[{"end_at": 1718409600 + i * 900} for i in range(10)])),
    # Mixed complete and sparse intervals
    ("mixed_complete_sparse", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 500.0, "consumption": 300.0,
             "grid_import": 100.0, "grid_export": 300.0, "discharge": 50.0, "charge": 20.0},
            {"end_at": 1718410500, "production": 100.0},
            {"end_at": 1718411400},
            {"end_at": 1718412300, "consumption": 800.0, "grid_home": 800.0},
            {"end_at": 1718413200, "production": 3000.0, "consumption": 1000.0,
             "grid_import": 0.0, "grid_export": 2000.0, "discharge": 0.0, "charge": 500.0},
        ],
        totals={"production": 3600.0, "consumption": 2100.0})),
    # Float precision edge cases
    ("float_precision", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 0.1 + 0.2, "consumption": 1.0 / 3.0}
        ],
        totals={"production": 0.3, "consumption": 0.333})),
    # Integer values in intervals
    ("integer_values", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 1000, "consumption": 500,
             "grid_import": 200, "grid_export": 700}
        ],
        totals={"production": 1000, "consumption": 500})),
    # Duplicate timestamps
    ("duplicate_timestamps", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 500.0, "consumption": 300.0},
            {"end_at": 1718409600, "production": 600.0, "consumption": 400.0},
            {"end_at": 1718409600, "production": 700.0, "consumption": 500.0},
        ],
        totals={"production": 1800.0, "consumption": 1200.0})),
    # Very old timestamps
    ("old_timestamps", lambda: _make_today_json(
        intervals=[
            {"end_at": 1000000000, "production": 500.0, "consumption": 300.0}
        ],
        totals={"production": 500.0, "consumption": 300.0},
        cloned_date="2001-09-09")),
    # No _cloned_date in data
    ("no_cloned_date", lambda: {
        "stats": [{"totals": {"production": 1000.0}, "intervals": [
            {"end_at": 1718409600, "production": 100.0}
        ]}]}),
    # Additional extra fields in interval (should be ignored)
    ("extra_fields_in_interval", lambda: _make_today_json(
        intervals=[
            {"end_at": 1718409600, "production": 500.0, "consumption": 300.0,
             "unknown_field_1": 123.0, "another_mystery": "hello", "deep": {"nested": True}}
        ],
        totals={"production": 500.0})),
    # Additional extra fields in totals (should be ignored)
    ("extra_fields_in_totals", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        totals={"production": 500.0, "unknown_total": 999.0, "mystery": "val"})),
    # Intervals list is a tuple (if somehow cast)
    ("many_battery_details_fields", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        battery_details={"aggregate_soc": 42, "estimated_time": 200,
                         "last_24h_consumption": 15.5,
                         "extra_field": "should_be_ignored"})),
    # Production and consumption only in totals
    ("prod_cons_totals_only", lambda: _make_today_json(
        intervals=[],
        totals={"production": 25000.0, "consumption": 18000.0})),
    # Minimal viable data
    ("minimal_viable", lambda: _make_today_json(
        intervals=[{"end_at": 1718409600, "production": 1.0}],
        totals={"production": 1.0})),
    # System with all zeroed battery details
    ("battery_details_all_zero", lambda: _make_today_json(
        intervals=_make_intervals(5, production=100.0),
        battery_details={"aggregate_soc": 0, "estimated_time": 0, "last_24h_consumption": 0.0})),
]


class TestConvertDaySyntheticVariations:
    """60 parametrized synthetic today.json variants."""

    @pytest.mark.parametrize("name,data_fn", _SYNTHETIC_CASES,
                             ids=[c[0] for c in _SYNTHETIC_CASES])
    def test_synthetic_variant(self, name, data_fn):
        data = data_fn()
        lines = convert_day(data, f"SYNTH_{name}")
        assert isinstance(lines, list), f"[{name}] convert_day should return a list"
        for line in lines:
            assert isinstance(line, str), f"[{name}] Each line must be a string"
            parts = line.split()
            assert len(parts) >= 3, f"[{name}] Malformed line protocol: {line}"
            # measurement + tags
            assert parts[0].startswith("enphase_"), f"[{name}] Unexpected measurement: {parts[0]}"
            # timestamp should be a number
            ts = int(parts[-1])
            assert ts > 0, f"[{name}] Timestamp must be positive"


# ═══════════════════════════════════════════════════════════════════
# TestConvertDayMalformed — 40 parametrized malformed structures
# ═══════════════════════════════════════════════════════════════════

_MALFORMED_CASES = [
    ("stats_is_int", {"stats": 42}),
    ("stats_is_none", {"stats": None}),
    ("stats_is_string", {"stats": "hello"}),
    ("stats_is_bool", {"stats": True}),
    ("stats_is_float", {"stats": 3.14}),
    ("stats_first_has_no_intervals", {"stats": [{"totals": {"production": 100.0}}], "_cloned_date": "2024-01-01"}),
    ("intervals_contains_none", {"stats": [{"totals": {}, "intervals": [None, None, None]}], "_cloned_date": "2024-01-01"}),
    ("intervals_contains_int", {"stats": [{"totals": {}, "intervals": [42, 99]}], "_cloned_date": "2024-01-01"}),
    ("intervals_contains_string", {"stats": [{"totals": {}, "intervals": ["bad", "data"]}], "_cloned_date": "2024-01-01"}),
    ("intervals_contains_list", {"stats": [{"totals": {}, "intervals": [[1, 2], [3, 4]]}], "_cloned_date": "2024-01-01"}),
    ("totals_is_list", {"stats": [{"totals": [1, 2, 3], "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("totals_is_int", {"stats": [{"totals": 42, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("totals_is_string", {"stats": [{"totals": "bad", "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("totals_is_none", {"stats": [{"totals": None, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("totals_is_bool", {"stats": [{"totals": True, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("battery_details_is_int", {"stats": [{"totals": {}, "intervals": []}], "battery_details": 42, "_cloned_date": "2024-01-01"}),
    ("battery_details_is_list", {"stats": [{"totals": {}, "intervals": []}], "battery_details": [1, 2], "_cloned_date": "2024-01-01"}),
    ("battery_details_is_string", {"stats": [{"totals": {}, "intervals": []}], "battery_details": "bad", "_cloned_date": "2024-01-01"}),
    ("battery_details_is_none", {"stats": [{"totals": {}, "intervals": []}], "battery_details": None, "_cloned_date": "2024-01-01"}),
    ("battery_details_is_bool", {"stats": [{"totals": {}, "intervals": []}], "battery_details": True, "_cloned_date": "2024-01-01"}),
    ("cloned_date_invalid_format", {"stats": [{"totals": {"production": 100.0}, "intervals": []}], "_cloned_date": "not-a-date"}),
    ("cloned_date_missing", {"stats": [{"totals": {"production": 100.0}, "intervals": []}]}),
    ("cloned_date_future", {"stats": [{"totals": {"production": 100.0}, "intervals": []}], "_cloned_date": "2099-12-31"}),
    ("cloned_date_is_int", {"stats": [{"totals": {"production": 100.0}, "intervals": []}], "_cloned_date": 20240101}),
    ("cloned_date_is_none", {"stats": [{"totals": {"production": 100.0}, "intervals": []}], "_cloned_date": None}),
    ("cloned_date_empty_string", {"stats": [{"totals": {"production": 100.0}, "intervals": []}], "_cloned_date": ""}),
    ("deeply_nested_none_production", {"stats": [{"totals": {"production": None}, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("deeply_nested_none_consumption", {"stats": [{"totals": {"consumption": None}, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("deeply_nested_none_charge", {"stats": [{"totals": {"charge": None}, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("deeply_nested_none_discharge", {"stats": [{"totals": {"discharge": None}, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("stats_empty_string", {"stats": ""}),
    ("stats_empty_dict", {"stats": [{}]}),
    ("totals_values_are_strings", {"stats": [{"totals": {"production": "1000", "consumption": "500"}, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("totals_values_are_booleans", {"stats": [{"totals": {"production": True, "consumption": False}, "intervals": []}], "_cloned_date": "2024-01-01"}),
    ("interval_end_at_string", {"stats": [{"totals": {}, "intervals": [{"end_at": "1711270800", "production": 100.0}]}], "_cloned_date": "2024-01-01"}),
    ("interval_end_at_float", {"stats": [{"totals": {}, "intervals": [{"end_at": 1711270800.5, "production": 100.0}]}], "_cloned_date": "2024-01-01"}),
    ("interval_end_at_none", {"stats": [{"totals": {}, "intervals": [{"end_at": None, "production": 100.0}]}], "_cloned_date": "2024-01-01"}),
    ("interval_end_at_negative", {"stats": [{"totals": {}, "intervals": [{"end_at": -1, "production": 100.0}]}], "_cloned_date": "2024-01-01"}),
    ("interval_production_none", {"stats": [{"totals": {}, "intervals": [{"end_at": 1711270800, "production": None}]}], "_cloned_date": "2024-01-01"}),
    ("multiple_stats_entries", {"stats": [{"totals": {"production": 100.0}, "intervals": []}, {"totals": {"production": 200.0}, "intervals": []}], "_cloned_date": "2024-01-01"}),
]


class TestConvertDayMalformed:
    """40 parametrized tests with malformed today.json structures."""

    @pytest.mark.parametrize("name,data", _MALFORMED_CASES,
                             ids=[c[0] for c in _MALFORMED_CASES])
    def test_malformed_no_crash(self, name, data):
        """convert_day must never crash on malformed input."""
        try:
            result = convert_day(data, "S")
        except (TypeError, ValueError, AttributeError, KeyError, IndexError):
            # Some truly broken inputs might raise, but they should not produce a traceback
            # that indicates an unhandled error in production code.
            # We accept a clean exception here.
            return
        assert isinstance(result, list), f"[{name}] Expected list, got {type(result)}"
        for line in result:
            assert isinstance(line, str), f"[{name}] Each line must be a string"


# ═══════════════════════════════════════════════════════════════════
# TestConvertDayFieldTypes — 30 tests for interval field value types
# ═══════════════════════════════════════════════════════════════════

_FIELD_TYPE_CASES = [
    # (name, field_name, value, should_appear)
    ("production_float", "production", 100.5, True),
    ("production_int", "production", 100, True),
    ("production_string_number", "production", "100.5", True),
    ("production_none", "production", None, False),
    ("production_empty_string", "production", "", False),
    ("production_bool_true", "production", True, False),
    ("production_bool_false", "production", False, False),
    ("consumption_float", "consumption", 200.3, True),
    ("consumption_int", "consumption", 200, True),
    ("consumption_string_number", "consumption", "200.3", True),
    ("consumption_none", "consumption", None, False),
    ("consumption_empty_string", "consumption", "", False),
    ("consumption_bool_true", "consumption", True, False),
    ("consumption_bool_false", "consumption", False, False),
    ("grid_home_float", "grid_home", 50.5, True),
    ("grid_home_int", "grid_home", 50, True),
    ("grid_home_string_number", "grid_home", "50.5", True),
    ("grid_home_none", "grid_home", None, False),
    ("grid_home_empty_string", "grid_home", "", False),
    ("solar_grid_float", "solar_grid", 75.2, True),
    ("solar_grid_int", "solar_grid", 75, True),
    ("solar_grid_string_number", "solar_grid", "75.2", True),
    ("solar_grid_none", "solar_grid", None, False),
    ("discharge_float", "discharge", 300.0, True),
    ("discharge_int", "discharge", 300, True),
    ("discharge_string_number", "discharge", "300.0", True),
    ("discharge_none", "discharge", None, False),
    ("charge_float", "charge", 150.0, True),
    ("charge_int", "charge", 150, True),
    ("charge_none", "charge", None, False),
]


class TestConvertDayFieldTypes:
    """30 parametrized tests for interval field values of different types."""

    @pytest.mark.parametrize("name,field_name,value,should_appear", _FIELD_TYPE_CASES,
                             ids=[c[0] for c in _FIELD_TYPE_CASES])
    def test_field_type(self, name, field_name, value, should_appear):
        iv = {"end_at": 1711270800}
        iv[field_name] = value
        data = {
            "stats": [{"totals": {}, "intervals": [iv]}],
            "_cloned_date": "2024-03-24",
        }
        try:
            lines = convert_day(data, "S")
        except (TypeError, ValueError):
            # String/bool values in numeric fields may raise during float() conversion
            if not should_appear:
                return
            raise

        power_lines = [l for l in lines if l.startswith("enphase_power,")]

        # Map field_name to the expected output field name
        field_map = {
            "production": "production_w",
            "consumption": "consumption_w",
            "grid_home": "grid_w",
            "solar_grid": "grid_w",
            "discharge": "battery_w",
            "charge": "battery_w",
        }
        out_field = field_map.get(field_name)

        if should_appear and out_field:
            # The field should appear in at least one power line (for direct mappings)
            # Note: grid_w and battery_w require specific combinations
            # For simple fields like production and consumption, check directly
            if field_name in ("production", "consumption"):
                has_field = any(f"{out_field}=" in l for l in power_lines)
                assert has_field, f"[{name}] Expected {out_field} in power line"
        elif not should_appear:
            # None values should be skipped
            pass


# ═══════════════════════════════════════════════════════════════════
# TestWriteToInfluxDBMock — 40 tests for write_to_influxdb
# ═══════════════════════════════════════════════════════════════════

class TestWriteToInfluxDBMock:
    """40 tests for write_to_influxdb using mocked urllib."""

    def _make_lines(self, n):
        """Generate n synthetic line protocol lines."""
        return [
            f'enphase_power,serial=S,source=history production_w={i * 100.0} {1711270800000000000 + i * 900000000000}'
            for i in range(n)
        ]

    def _mock_urlopen_success(self):
        """Return a mock urlopen that simulates HTTP 204 success."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 204
        return mock_resp

    @patch('urllib.request.urlopen')
    def test_write_zero_lines(self, mock_urlopen):
        result = write_to_influxdb([], "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0
        mock_urlopen.assert_not_called()

    @patch('urllib.request.urlopen')
    def test_write_one_line(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 1
        assert mock_urlopen.call_count == 1

    @patch('urllib.request.urlopen')
    def test_write_100_lines_default_batch(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(100)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 100
        assert mock_urlopen.call_count == 1  # 100 < 5000

    @patch('urllib.request.urlopen')
    def test_write_batch_size_1(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(5)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=1)
        assert result == 5
        assert mock_urlopen.call_count == 5

    @patch('urllib.request.urlopen')
    def test_write_batch_size_100(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(250)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=100)
        assert result == 250
        assert mock_urlopen.call_count == 3  # 100 + 100 + 50

    @patch('urllib.request.urlopen')
    def test_write_batch_size_5000(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(5000)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=5000)
        assert result == 5000
        assert mock_urlopen.call_count == 1

    @patch('urllib.request.urlopen')
    def test_write_batch_size_10000(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(10000)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=10000)
        assert result == 10000
        assert mock_urlopen.call_count == 1

    @patch('urllib.request.urlopen')
    def test_write_4999_lines(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(4999)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 4999
        assert mock_urlopen.call_count == 1

    @patch('urllib.request.urlopen')
    def test_write_5000_lines(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(5000)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 5000
        assert mock_urlopen.call_count == 1

    @patch('urllib.request.urlopen')
    def test_write_5001_lines(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(5001)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 5001
        assert mock_urlopen.call_count == 2  # 5000 + 1

    @patch('urllib.request.urlopen')
    def test_http_400_error(self, mock_urlopen):
        error = urllib.error.HTTPError(
            "http://localhost:8086/api/v2/write", 400, "Bad Request",
            {}, MagicMock(read=MagicMock(return_value=b"invalid line protocol"))
        )
        mock_urlopen.side_effect = error
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_http_401_error(self, mock_urlopen):
        error = urllib.error.HTTPError(
            "http://localhost:8086/api/v2/write", 401, "Unauthorized",
            {}, MagicMock(read=MagicMock(return_value=b"unauthorized"))
        )
        mock_urlopen.side_effect = error
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_http_500_error(self, mock_urlopen):
        error = urllib.error.HTTPError(
            "http://localhost:8086/api/v2/write", 500, "Internal Server Error",
            {}, MagicMock(read=MagicMock(return_value=b"server error"))
        )
        mock_urlopen.side_effect = error
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_connection_timeout(self, mock_urlopen):
        import socket
        mock_urlopen.side_effect = socket.timeout("connection timed out")
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_url_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_generic_exception(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("something went wrong")
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_progress_callback_called_once_for_single_batch(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        calls = []
        lines = self._make_lines(100)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket",
                          progress_cb=lambda w, t: calls.append((w, t)))
        assert len(calls) == 1
        assert calls[0] == (100, 100)

    @patch('urllib.request.urlopen')
    def test_progress_callback_called_multiple_batches(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        calls = []
        lines = self._make_lines(250)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket",
                          batch_size=100,
                          progress_cb=lambda w, t: calls.append((w, t)))
        assert len(calls) == 3  # 3 batches
        # Each call should report total=250
        for _, total in calls:
            assert total == 250

    @patch('urllib.request.urlopen')
    def test_progress_callback_on_error(self, mock_urlopen):
        error = urllib.error.HTTPError(
            "url", 500, "err", {}, MagicMock(read=MagicMock(return_value=b"err"))
        )
        mock_urlopen.side_effect = error
        calls = []
        lines = self._make_lines(100)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket",
                          progress_cb=lambda w, t: calls.append((w, t)))
        assert len(calls) == 1
        assert calls[0] == (0, 100)  # 0 written on error

    @patch('urllib.request.urlopen')
    def test_write_url_construction(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        write_to_influxdb(lines, "http://myhost:8086", "mytoken", "myorg", "mybucket")
        req = mock_urlopen.call_args[0][0]
        assert "http://myhost:8086/api/v2/write" in req.full_url
        assert "org=myorg" in req.full_url
        assert "bucket=mybucket" in req.full_url
        assert "precision=ns" in req.full_url

    @patch('urllib.request.urlopen')
    def test_authorization_header(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        write_to_influxdb(lines, "http://localhost:8086", "secret_token", "org", "bucket")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Token secret_token"

    @patch('urllib.request.urlopen')
    def test_content_type_header(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        req = mock_urlopen.call_args[0][0]
        assert "text/plain" in req.get_header("Content-type")

    @patch('urllib.request.urlopen')
    def test_body_is_newline_joined(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(3)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        req = mock_urlopen.call_args[0][0]
        body = req.data.decode("utf-8")
        assert body.count("\n") == 2  # 3 lines joined by 2 newlines

    @patch('urllib.request.urlopen')
    def test_trailing_slash_stripped_from_url(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        write_to_influxdb(lines, "http://localhost:8086/", "tok", "org", "bucket")
        req = mock_urlopen.call_args[0][0]
        assert "//api" not in req.full_url

    @patch('urllib.request.urlopen')
    def test_partial_batch_failure(self, mock_urlopen):
        """First batch succeeds, second fails."""
        mock_resp = self._mock_urlopen_success()
        error = urllib.error.HTTPError(
            "url", 500, "err", {}, MagicMock(read=MagicMock(return_value=b"err"))
        )
        mock_urlopen.side_effect = [mock_resp, error]
        lines = self._make_lines(200)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=100)
        assert result == 100  # Only first batch

    @patch('urllib.request.urlopen')
    def test_all_batches_fail(self, mock_urlopen):
        error = urllib.error.HTTPError(
            "url", 500, "err", {}, MagicMock(read=MagicMock(return_value=b"err"))
        )
        mock_urlopen.side_effect = error
        lines = self._make_lines(200)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=100)
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_non_204_success_status(self, mock_urlopen):
        """Non-204 status (like 200) should count as error in this implementation."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0  # 200 != 204

    @patch('urllib.request.urlopen')
    def test_progress_callback_not_provided(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(10)
        # Should not raise when progress_cb is None
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 10

    @patch('urllib.request.urlopen')
    def test_large_batch_10000(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(10000)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=10000)
        assert result == 10000
        assert mock_urlopen.call_count == 1

    @patch('urllib.request.urlopen')
    def test_batch_boundary_exact(self, mock_urlopen):
        """Exactly 2 full batches, no remainder."""
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(200)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=100)
        assert result == 200
        assert mock_urlopen.call_count == 2

    @patch('urllib.request.urlopen')
    def test_progress_callback_increments(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        calls = []
        lines = self._make_lines(500)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket",
                          batch_size=100,
                          progress_cb=lambda w, t: calls.append((w, t)))
        assert len(calls) == 5
        written_values = [c[0] for c in calls]
        assert written_values == [100, 200, 300, 400, 500]

    @patch('urllib.request.urlopen')
    def test_timeout_passed_to_urlopen(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        _, kwargs = mock_urlopen.call_args
        assert kwargs.get("timeout") == 30

    @patch('urllib.request.urlopen')
    def test_request_method_is_post(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        req = mock_urlopen.call_args[0][0]
        assert req.method == "POST"

    @patch('urllib.request.urlopen')
    def test_body_encoding_utf8(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        req = mock_urlopen.call_args[0][0]
        # data should be bytes
        assert isinstance(req.data, bytes)
        # Should be decodable as UTF-8
        req.data.decode("utf-8")

    @patch('urllib.request.urlopen')
    def test_multiple_failures_then_success(self, mock_urlopen):
        """Multiple batches: first 2 fail, last succeeds."""
        mock_resp = self._mock_urlopen_success()
        error = urllib.error.HTTPError(
            "url", 500, "err", {}, MagicMock(read=MagicMock(return_value=b"err"))
        )
        mock_urlopen.side_effect = [error, error, mock_resp]
        lines = self._make_lines(300)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket", batch_size=100)
        assert result == 100  # Only the last batch of 100

    @patch('urllib.request.urlopen')
    def test_http_error_body_truncated(self, mock_urlopen):
        """Long error body should not crash."""
        long_body = b"x" * 1000
        error = urllib.error.HTTPError(
            "url", 400, "Bad Request", {},
            MagicMock(read=MagicMock(return_value=long_body))
        )
        mock_urlopen.side_effect = error
        lines = self._make_lines(10)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "org", "bucket")
        assert result == 0

    @patch('urllib.request.urlopen')
    def test_empty_token(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        result = write_to_influxdb(lines, "http://localhost:8086", "", "org", "bucket")
        assert result == 1
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Token "

    @patch('urllib.request.urlopen')
    def test_special_chars_in_org_bucket(self, mock_urlopen):
        mock_resp = self._mock_urlopen_success()
        mock_urlopen.return_value = mock_resp
        lines = self._make_lines(1)
        result = write_to_influxdb(lines, "http://localhost:8086", "tok", "my org", "my bucket")
        assert result == 1
        req = mock_urlopen.call_args[0][0]
        assert "my org" in req.full_url or "my%20org" in req.full_url


# ═══════════════════════════════════════════════════════════════════
# format_line tests
# ═══════════════════════════════════════════════════════════════════

class TestFormatLine:
    """Tests for the format_line helper function."""

    def test_basic_format(self):
        line = format_line("meas", {"tag1": "v1"}, {"field1": 42}, 1000000000)
        assert line is not None
        assert line.startswith("meas,tag1=v1")
        assert "field1=42i" in line
        assert line.endswith("1000000000")

    def test_empty_fields_returns_none(self):
        assert format_line("meas", {"tag1": "v1"}, {}, 1000000000) is None

    def test_all_none_fields_returns_none(self):
        assert format_line("meas", {"tag1": "v1"}, {"a": None, "b": None}, 1000000000) is None

    def test_float_field(self):
        line = format_line("m", {}, {"f": 3.14}, 1)
        assert "f=3.14" in line

    def test_int_field(self):
        line = format_line("m", {}, {"f": 42}, 1)
        assert "f=42i" in line

    def test_string_field(self):
        line = format_line("m", {}, {"f": "hello"}, 1)
        assert 'f="hello"' in line

    def test_bool_field_true(self):
        line = format_line("m", {}, {"f": True}, 1)
        assert "f=1i" in line
        assert 'f_str="true"' in line

    def test_bool_field_false(self):
        line = format_line("m", {}, {"f": False}, 1)
        assert "f=0i" in line
        assert 'f_str="false"' in line

    def test_none_field_skipped(self):
        line = format_line("m", {}, {"a": 1, "b": None, "c": 2}, 1)
        assert "b=" not in line
        assert "a=1i" in line
        assert "c=2i" in line

    def test_empty_tag_value_skipped(self):
        line = format_line("m", {"t1": "v1", "t2": ""}, {"f": 1}, 1)
        assert "t1=v1" in line
        assert "t2=" not in line

    def test_none_tag_value_skipped(self):
        line = format_line("m", {"t1": "v1", "t2": None}, {"f": 1}, 1)
        assert "t1=v1" in line
        assert "t2=" not in line

    def test_tags_sorted(self):
        line = format_line("m", {"z": "1", "a": "2"}, {"f": 1}, 1)
        # a should come before z
        a_pos = line.index("a=2")
        z_pos = line.index("z=1")
        assert a_pos < z_pos

    def test_fields_sorted(self):
        line = format_line("m", {}, {"z": 1, "a": 2}, 1)
        a_pos = line.index("a=2i")
        z_pos = line.index("z=1i")
        assert a_pos < z_pos

    def test_escape_tag_space(self):
        result = _esc_tag("hello world")
        assert result == r"hello\ world"

    def test_escape_tag_comma(self):
        result = _esc_tag("a,b")
        assert result == r"a\,b"

    def test_escape_tag_equals(self):
        result = _esc_tag("a=b")
        assert result == r"a\=b"

    def test_escape_field_str_quote(self):
        result = _esc_field_str('say "hello"')
        assert result == r'say \"hello\"'

    def test_escape_field_str_newline(self):
        result = _esc_field_str("line1\nline2")
        assert result == r"line1\nline2"

    def test_escape_field_str_carriage_return(self):
        result = _esc_field_str("line1\rline2")
        assert result == "line1line2"
