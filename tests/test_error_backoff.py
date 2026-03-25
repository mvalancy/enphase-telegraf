"""Tests for _should_emit_error, _clear_error, and emit_error in enphase_telegraf.py.

80 tests covering exponential backoff, boundary timing, clear semantics,
and emit_error integration.
"""

import time

import pytest

import enphase_telegraf as et


@pytest.fixture(autouse=True)
def reset_backoff():
    et._error_backoff = {}
    et._serial = "TEST123"
    yield


# ═══════════════════════════════════════════════════════════════════════
# TestShouldEmitError — 50 tests
# ═══════════════════════════════════════════════════════════════════════


class TestShouldEmitError:
    """Test _should_emit_error exponential backoff logic."""

    def test_first_call_returns_true(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert et._should_emit_error("comp_a", "msg") is True

    def test_first_call_creates_state(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("comp_a", "msg")
        state = et._error_backoff["comp_a"]
        assert state["last_emit"] == 1000.0
        assert state["interval"] == 60
        assert state["message"] == "msg"

    def test_second_call_within_60s_returns_false(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("comp_a", "msg")
        monkeypatch.setattr(time, "time", lambda: 1059.0)
        assert et._should_emit_error("comp_a", "msg") is False

    def test_call_after_60s_returns_true(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("comp_a", "msg")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        assert et._should_emit_error("comp_a", "msg") is True

    def test_interval_doubles_after_first_backoff(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("comp_a", "msg")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("comp_a", "msg")
        assert et._error_backoff["comp_a"]["interval"] == 120

    def test_full_backoff_sequence_60_to_120(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 60
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 120

    def test_full_backoff_sequence_120_to_240(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1180.0)  # +120
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 240

    def test_full_backoff_sequence_240_to_480(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        t = 1000.0
        for expected in [120, 240, 480]:
            t += et._error_backoff["c"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 480

    def test_full_backoff_sequence_480_to_960(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        t = 1000.0
        for _ in range(4):
            t += et._error_backoff["c"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 960

    def test_full_backoff_sequence_960_to_1920(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        t = 1000.0
        for _ in range(5):
            t += et._error_backoff["c"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 1920

    def test_full_backoff_sequence_1920_to_3600(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        t = 1000.0
        for _ in range(6):
            t += et._error_backoff["c"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et._should_emit_error("c", "m")
        # 1920 * 2 = 3840 → capped at 3600
        assert et._error_backoff["c"]["interval"] == 3600

    def test_backoff_capped_at_3600(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        t = 1000.0
        for _ in range(10):
            t += et._error_backoff["c"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 3600

    def test_remains_capped_after_many_iterations(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        t = 1000.0
        for _ in range(20):
            t += et._error_backoff["c"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 3600

    def test_boundary_at_exactly_59_999s(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1059.999)
        assert et._should_emit_error("c", "m") is False

    def test_boundary_at_exactly_60_0s(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        assert et._should_emit_error("c", "m") is True

    def test_boundary_at_exactly_60_001s(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.001)
        assert et._should_emit_error("c", "m") is True

    @pytest.mark.parametrize("component", [
        "mqtt", "cloud_latest_power", "cloud_battery_status", "cloud_today",
        "cloud_events", "cloud_alarms", "auth", "discovery",
        "proto_version", "data_quality",
    ])
    def test_first_call_returns_true_per_component(self, monkeypatch, component):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert et._should_emit_error(component, "test") is True

    @pytest.mark.parametrize("component", [
        "mqtt", "cloud_latest_power", "cloud_battery_status", "cloud_today",
        "cloud_events", "cloud_alarms", "auth", "discovery",
        "proto_version", "data_quality",
    ])
    def test_second_call_within_interval_per_component(self, monkeypatch, component):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error(component, "test")
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        assert et._should_emit_error(component, "test") is False

    def test_different_components_independent(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("comp_a", "msg")
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        assert et._should_emit_error("comp_a", "msg") is False
        assert et._should_emit_error("comp_b", "msg") is True

    def test_message_stored_in_state(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "first message")
        assert et._error_backoff["c"]["message"] == "first message"

    def test_message_updated_on_emit(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "first")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("c", "second")
        assert et._error_backoff["c"]["message"] == "second"

    def test_message_not_updated_when_suppressed(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "first")
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        et._should_emit_error("c", "second")
        assert et._error_backoff["c"]["message"] == "first"

    def test_last_emit_updated_on_emit(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["last_emit"] == 1060.0

    def test_last_emit_not_updated_when_suppressed(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["last_emit"] == 1000.0

    def test_boundary_second_interval_119_999s(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("c", "m")
        # Now interval=120, last_emit=1060
        monkeypatch.setattr(time, "time", lambda: 1179.999)
        assert et._should_emit_error("c", "m") is False

    def test_boundary_second_interval_120_0s(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1180.0)
        assert et._should_emit_error("c", "m") is True

    def test_many_suppressed_calls_no_state_change(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        for i in range(50):
            monkeypatch.setattr(time, "time", lambda i=i: 1000.0 + i)
            et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 60
        assert et._error_backoff["c"]["last_emit"] == 1000.0

    def test_interval_not_modified_on_false(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 60

    def test_empty_component_name(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert et._should_emit_error("", "msg") is True

    def test_empty_message(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert et._should_emit_error("c", "") is True

    def test_long_component_name(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        name = "x" * 200
        assert et._should_emit_error(name, "msg") is True
        assert name in et._error_backoff

    def test_long_message(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        msg = "y" * 500
        et._should_emit_error("c", msg)
        assert et._error_backoff["c"]["message"] == msg

    def test_three_components_isolated(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("a", "m")
        et._should_emit_error("b", "m")
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        assert et._should_emit_error("a", "m") is False
        assert et._should_emit_error("b", "m") is False
        assert et._should_emit_error("c", "m") is False
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        assert et._should_emit_error("a", "m") is True
        assert et._should_emit_error("b", "m") is True
        assert et._should_emit_error("c", "m") is True

    def test_rapid_emit_suppressed(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert et._should_emit_error("c", "m") is True
        monkeypatch.setattr(time, "time", lambda: 1000.001)
        assert et._should_emit_error("c", "m") is False

    def test_zero_time(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 0.0)
        assert et._should_emit_error("c", "m") is True
        monkeypatch.setattr(time, "time", lambda: 59.0)
        assert et._should_emit_error("c", "m") is False
        monkeypatch.setattr(time, "time", lambda: 60.0)
        assert et._should_emit_error("c", "m") is True


# ═══════════════════════════════════════════════════════════════════════
# TestClearError — 15 tests
# ═══════════════════════════════════════════════════════════════════════


class TestClearError:
    """Test _clear_error() behavior."""

    def test_clear_existing_removes_state(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        et._clear_error("c")
        assert "c" not in et._error_backoff

    def test_clear_nonexistent_no_crash(self):
        et._clear_error("nonexistent")

    def test_clear_empty_string_key(self):
        et._clear_error("")

    def test_after_clear_next_emit_returns_true(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        assert et._should_emit_error("c", "m") is False
        et._clear_error("c")
        assert et._should_emit_error("c", "m") is True

    def test_after_clear_interval_resets_to_60(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 120
        et._clear_error("c")
        monkeypatch.setattr(time, "time", lambda: 1070.0)
        et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 60

    def test_clear_one_doesnt_affect_another(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("a", "m")
        et._should_emit_error("b", "m")
        et._clear_error("a")
        assert "a" not in et._error_backoff
        assert "b" in et._error_backoff

    def test_clear_with_elevated_backoff(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        t = 1000.0
        for _ in range(5):
            t += et._error_backoff["c"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et._should_emit_error("c", "m")
        assert et._error_backoff["c"]["interval"] == 1920
        et._clear_error("c")
        assert "c" not in et._error_backoff

    def test_double_clear_no_crash(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        et._clear_error("c")
        et._clear_error("c")

    def test_clear_all_components(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        for name in ["a", "b", "c", "d"]:
            et._should_emit_error(name, "m")
        for name in ["a", "b", "c", "d"]:
            et._clear_error(name)
        assert et._error_backoff == {}

    def test_clear_preserves_other_components_state(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("a", "m")
        et._should_emit_error("b", "m")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et._should_emit_error("b", "m")
        et._clear_error("a")
        assert et._error_backoff["b"]["interval"] == 120
        assert et._error_backoff["b"]["last_emit"] == 1060.0

    def test_clear_then_immediate_emit(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        et._clear_error("c")
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert et._should_emit_error("c", "m") is True

    def test_clear_after_suppression(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        monkeypatch.setattr(time, "time", lambda: 1010.0)
        assert et._should_emit_error("c", "m") is False
        et._clear_error("c")
        assert et._should_emit_error("c", "m") is True
        assert et._error_backoff["c"]["interval"] == 60

    def test_clear_with_none_like_key(self):
        # Just verify no crash with unusual but valid string key
        et._clear_error("None")

    def test_clear_resets_message(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "old_message")
        et._clear_error("c")
        et._should_emit_error("c", "new_message")
        assert et._error_backoff["c"]["message"] == "new_message"

    def test_clear_preserves_dict_type(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et._should_emit_error("c", "m")
        et._clear_error("c")
        assert isinstance(et._error_backoff, dict)


# ═══════════════════════════════════════════════════════════════════════
# TestEmitError — 15 tests
# ═══════════════════════════════════════════════════════════════════════


class TestEmitError:
    """Test emit_error() integration with _should_emit_error and emit."""

    def test_first_call_emits(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(
            {"m": m, "tags": t, "fields": f}
        ))
        et.emit_error("comp", "test message")
        assert len(calls) == 1
        assert calls[0]["m"] == "enphase_error"

    def test_suppressed_call_does_not_emit(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(
            {"m": m, "tags": t, "fields": f}
        ))
        et.emit_error("comp", "first")
        calls.clear()
        monkeypatch.setattr(time, "time", lambda: 1030.0)
        et.emit_error("comp", "second")
        assert len(calls) == 0

    def test_emitted_line_includes_next_retry_s(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(f))
        et.emit_error("comp", "msg")
        assert "next_retry_s" in calls[0]
        assert calls[0]["next_retry_s"] == 60

    def test_next_retry_doubles_after_first(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(f))
        et.emit_error("comp", "msg")
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et.emit_error("comp", "msg")
        assert calls[1]["next_retry_s"] == 120

    def test_next_retry_progression(self, monkeypatch):
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(f))
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et.emit_error("comp", "msg")
        t = 1000.0
        expected_retries = [60, 120, 240, 480]
        for i, expected in enumerate(expected_retries):
            assert calls[i]["next_retry_s"] == expected
            t += expected
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et.emit_error("comp", "msg")

    def test_tags_include_component(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(
            {"tags": t}
        ))
        et.emit_error("my_comp", "msg")
        assert calls[0]["tags"]["component"] == "my_comp"

    def test_tags_include_serial(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(
            {"tags": t}
        ))
        et.emit_error("comp", "msg")
        assert calls[0]["tags"]["serial"] == "TEST123"

    def test_fields_include_message(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(f))
        et.emit_error("comp", "hello world")
        assert calls[0]["message"] == "hello world"

    def test_measurement_name(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(m))
        et.emit_error("comp", "msg")
        assert calls[0] == "enphase_error"

    def test_different_components_both_emit(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(t))
        et.emit_error("comp_a", "msg")
        et.emit_error("comp_b", "msg")
        assert len(calls) == 2
        assert calls[0]["component"] == "comp_a"
        assert calls[1]["component"] == "comp_b"

    def test_emit_after_clear_emits_again(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(f))
        et.emit_error("comp", "msg")
        et._clear_error("comp")
        et.emit_error("comp", "msg2")
        assert len(calls) == 2
        # After clear, interval resets to 60
        assert calls[1]["next_retry_s"] == 60

    def test_backoff_capped_at_3600_in_emit(self, monkeypatch):
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(f))
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        et.emit_error("comp", "msg")
        t = 1000.0
        for _ in range(10):
            t += et._error_backoff["comp"]["interval"]
            monkeypatch.setattr(time, "time", lambda t=t: t)
            et.emit_error("comp", "msg")
        # The last emitted next_retry_s should be 3600 (capped)
        assert calls[-1]["next_retry_s"] == 3600

    def test_empty_component_still_works(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(
            {"tags": t}
        ))
        et.emit_error("", "msg")
        assert calls[0]["tags"]["component"] == ""

    def test_empty_message_still_emits(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(f))
        et.emit_error("comp", "")
        assert calls[0]["message"] == ""

    def test_emit_error_uses_should_emit_error_gating(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        calls = []
        monkeypatch.setattr(et, "emit", lambda m, t, f, ts_ns=None: calls.append(1))
        # First call emits
        et.emit_error("c", "m")
        assert len(calls) == 1
        # Within 60s, suppressed
        monkeypatch.setattr(time, "time", lambda: 1059.0)
        et.emit_error("c", "m")
        assert len(calls) == 1
        # After 60s, emits again
        monkeypatch.setattr(time, "time", lambda: 1060.0)
        et.emit_error("c", "m")
        assert len(calls) == 2
        # Within 120s, suppressed
        monkeypatch.setattr(time, "time", lambda: 1179.0)
        et.emit_error("c", "m")
        assert len(calls) == 2
        # After 120s, emits
        monkeypatch.setattr(time, "time", lambda: 1180.0)
        et.emit_error("c", "m")
        assert len(calls) == 3
