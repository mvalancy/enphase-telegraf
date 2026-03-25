"""Unit tests for the emit() function in enphase_telegraf.py.

Tests stdout output, thread safety, timestamp generation, field formatting,
and the emit_error wrapper.
"""

import io
import random
import string
import sys
import threading
import time

import pytest
import enphase_telegraf as et


@pytest.fixture(autouse=True)
def reset_emit_state():
    et._serial = "TESTSERIAL"
    et._error_backoff = {}
    et._stdout_lock = threading.Lock()
    yield


@pytest.fixture
def capture_stdout(capsys):
    """Wrapper around capsys that returns a helper object."""
    class CaptureHelper:
        def getvalue(self):
            return capsys.readouterr().out
        def truncate(self, n=0):
            # Read and discard current output
            capsys.readouterr()
        def seek(self, n):
            pass  # no-op for capsys compat
    return CaptureHelper()


# ═══════════════════════════════════════════════════════════════════
# Basic emit behavior
# ═══════════════════════════════════════════════════════════════════

class TestEmitBasic:

    def test_int_field(self, capture_stdout):
        et.emit("m", {}, {"count": 42}, ts_ns=1000)
        assert "count=42i" in capture_stdout.getvalue()

    def test_float_field(self, capture_stdout):
        et.emit("m", {}, {"ratio": 3.14}, ts_ns=1000)
        assert "ratio=3.14" in capture_stdout.getvalue()

    def test_string_field(self, capture_stdout):
        et.emit("m", {}, {"name": "test"}, ts_ns=1000)
        assert 'name="test"' in capture_stdout.getvalue()

    def test_bool_true(self, capture_stdout):
        et.emit("m", {}, {"flag": True}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert "flag=1i" in out
        assert 'flag_str="true"' in out

    def test_bool_false(self, capture_stdout):
        et.emit("m", {}, {"flag": False}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert "flag=0i" in out
        assert 'flag_str="false"' in out

    def test_empty_fields_no_output(self, capture_stdout):
        et.emit("m", {}, {}, ts_ns=1000)
        assert capture_stdout.getvalue() == ""

    def test_all_none_fields_no_output(self, capture_stdout):
        et.emit("m", {}, {"a": None, "b": None}, ts_ns=1000)
        assert capture_stdout.getvalue() == ""

    def test_none_fields_skipped(self, capture_stdout):
        et.emit("m", {}, {"a": 1, "b": None, "c": 3}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert "a=1i" in out
        assert "c=3i" in out
        assert "b=" not in out

    def test_auto_timestamp(self, capture_stdout, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1700000000.0)
        et.emit("m", {}, {"v": 1})
        out = capture_stdout.getvalue()
        assert "1700000000000000000" in out

    def test_explicit_timestamp(self, capture_stdout):
        et.emit("m", {}, {"v": 1}, ts_ns=9876543210)
        assert capture_stdout.getvalue().strip().endswith("9876543210")

    def test_tags_sorted(self, capture_stdout):
        et.emit("m", {"z": "1", "a": "2", "m": "3"}, {"v": 1}, ts_ns=1000)
        line = capture_stdout.getvalue().strip()
        meas_tags = line.split(" ")[0]
        assert meas_tags == "m,a=2,m=3,z=1"

    def test_fields_sorted(self, capture_stdout):
        et.emit("m", {}, {"z_v": 1.0, "a_v": 2.0}, ts_ns=1000)
        line = capture_stdout.getvalue().strip()
        field_part = line.split(" ")[1]
        keys = [kv.split("=")[0] for kv in field_part.split(",")]
        assert keys == sorted(keys)

    def test_empty_tag_value_skipped(self, capture_stdout):
        et.emit("m", {"a": "1", "b": "", "c": None}, {"v": 1}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert ",a=1" in out
        assert ",b=" not in out
        assert ",c=" not in out

    def test_special_chars_in_tag(self, capture_stdout):
        et.emit("m", {"host name": "my server"}, {"v": 1}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert r"host\ name=my\ server" in out

    def test_special_chars_in_field_string(self, capture_stdout):
        et.emit("m", {}, {"msg": 'say "hi"'}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert r'msg="say \"hi\""' in out

    def test_newline_in_field_string(self, capture_stdout):
        et.emit("m", {}, {"msg": "line1\nline2"}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert r"line1\nline2" in out
        # Output should be a single line
        assert out.strip().count("\n") == 0

    def test_output_ends_with_newline(self, capture_stdout):
        et.emit("m", {}, {"v": 1}, ts_ns=1000)
        assert capture_stdout.getvalue().endswith("\n")

    def test_output_is_single_line(self, capture_stdout):
        et.emit("m", {"t": "v"}, {"a": 1, "b": 2.0, "c": "x"}, ts_ns=1000)
        lines = [l for l in capture_stdout.getvalue().split("\n") if l.strip()]
        assert len(lines) == 1

    def test_mixed_types(self, capture_stdout):
        et.emit("m", {"s": "X"}, {"i": 42, "f": 3.14, "s": "hi", "b": True}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert "i=42i" in out
        assert "f=3.14" in out
        assert 's="hi"' in out
        assert "b=1i" in out


# ═══════════════════════════════════════════════════════════════════
# Measurement names
# ═══════════════════════════════════════════════════════════════════

class TestEmitMeasurementNames:

    MEASUREMENTS = [
        "enphase_power", "enphase_energy", "enphase_battery",
        "enphase_config", "enphase_status", "enphase_error",
        "enphase_gateway", "enphase_inverters", "enphase_dry_contact",
        "enphase_meter", "enphase_inverter", "enphase_battery_device",
        "enphase_device", "enphase_test",
    ]

    @pytest.mark.parametrize("measurement", MEASUREMENTS)
    def test_measurement_in_output(self, measurement, capture_stdout):
        et.emit(measurement, {"serial": "X"}, {"v": 1}, ts_ns=1000)
        out = capture_stdout.getvalue()
        assert out.startswith(measurement + ",") or out.startswith(measurement + " ")


# ═══════════════════════════════════════════════════════════════════
# Thread safety
# ═══════════════════════════════════════════════════════════════════

class TestEmitThreadSafety:

    @pytest.mark.parametrize("num_threads", [2, 5, 10, 20])
    def test_no_interleaved_lines(self, num_threads, capsys):
        barrier = threading.Barrier(num_threads)

        def writer(thread_id):
            barrier.wait()
            for i in range(10):
                et.emit("m", {"t": str(thread_id)}, {"i": i}, ts_ns=thread_id * 1000 + i)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        output = capsys.readouterr().out
        lines = [l for l in output.split("\n") if l.strip()]

        # Every line should be a complete line protocol entry
        for line in lines:
            parts = line.split(" ")
            assert len(parts) >= 3, f"Interleaved/garbled line: {line!r}"
            assert parts[0].startswith("m"), f"Bad measurement: {line!r}"
            assert parts[-1].isdigit(), f"Bad timestamp: {line!r}"

        # Should have num_threads * 10 lines
        assert len(lines) == num_threads * 10

    def test_lock_prevents_garbled(self, capsys):
        """Stress test: 50 threads, 20 writes each."""
        def writer():
            for i in range(20):
                et.emit("stress", {"t": "x"}, {"val": random.random()}, ts_ns=int(time.time_ns()))

        threads = [threading.Thread(target=writer) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = [l for l in capsys.readouterr().out.split("\n") if l.strip()]
        assert len(lines) == 1000
        for line in lines:
            assert line.startswith("stress,")


# ═══════════════════════════════════════════════════════════════════
# emit_error
# ═══════════════════════════════════════════════════════════════════

class TestEmitErrorFunction:

    def test_first_error_emits(self, capture_stdout):
        et.emit_error("test_comp", "something broke")
        out = capture_stdout.getvalue()
        assert "enphase_error" in out
        assert "test_comp" in out
        assert "something broke" in out

    def test_second_error_within_interval_suppressed(self, capture_stdout):
        et.emit_error("comp1", "err1")
        first_out = capture_stdout.getvalue()
        assert "enphase_error" in first_out
        # Clear and emit again immediately
        capture_stdout.truncate(0)
        capture_stdout.seek(0)
        et.emit_error("comp1", "err2")
        assert capture_stdout.getvalue() == ""  # suppressed

    def test_different_components_independent(self, capture_stdout):
        et.emit_error("compA", "errA")
        et.emit_error("compB", "errB")
        out = capture_stdout.getvalue()
        assert out.count("enphase_error") == 2

    def test_error_includes_next_retry_s(self, capture_stdout):
        et.emit_error("comp", "msg")
        out = capture_stdout.getvalue()
        assert "next_retry_s=" in out

    def test_backoff_doubles(self, capture_stdout, monkeypatch):
        t = 1000.0
        monkeypatch.setattr(time, "time", lambda: t)

        et.emit_error("comp", "msg")
        assert "enphase_error" in capture_stdout.getvalue()

        # After 60s, should emit again with doubled interval
        t = 1061.0
        monkeypatch.setattr(time, "time", lambda: t)
        capture_stdout.truncate(0)
        capture_stdout.seek(0)
        et.emit_error("comp", "msg")
        out = capture_stdout.getvalue()
        assert "next_retry_s=120i" in out  # doubled from 60


# ═══════════════════════════════════════════════════════════════════
# Fuzz: random emit calls never crash
# ═══════════════════════════════════════════════════════════════════

class TestEmitFuzz:

    @pytest.mark.parametrize("seed", range(50))
    def test_random_emit_never_crashes(self, seed, capsys):
        random.seed(seed)
        measurement = "fuzz_" + "".join(random.choices(string.ascii_lowercase, k=5))
        tags = {f"t{i}": "".join(random.choices(string.ascii_letters + string.digits, k=random.randint(1, 20)))
                for i in range(random.randint(0, 5))}
        fields = {}
        for i in range(random.randint(1, 8)):
            ftype = random.choice(["int", "float", "str", "bool", "none"])
            key = f"f{i}"
            if ftype == "int":
                fields[key] = random.randint(-2**63, 2**63 - 1)
            elif ftype == "float":
                fields[key] = random.uniform(-1e15, 1e15)
            elif ftype == "str":
                fields[key] = "".join(random.choices(string.ascii_letters, k=random.randint(0, 50)))
            elif ftype == "bool":
                fields[key] = random.choice([True, False])
            else:
                fields[key] = None

        # Must not crash
        et.emit(measurement, tags, fields, ts_ns=random.randint(0, 2**63))
        # Output should be empty or valid lines
        out = capsys.readouterr().out
        for line in out.strip().split("\n"):
            if line:
                parts = line.split(" ")
                assert len(parts) >= 3
