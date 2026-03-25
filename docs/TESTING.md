# Testing

The test suite has 2,411 tests covering every layer of the system. Tests run
in 90 seconds and require no external services (except the `e2e` tests which
hit real Enphase cloud and InfluxDB).

## Running tests

```bash
# Full suite
venv/bin/pytest tests/

# Fast: unit tests only (no network)
venv/bin/pytest tests/ -m "not e2e"

# Specific file
venv/bin/pytest tests/test_line_protocol.py

# With HTML report
venv/bin/pytest tests/ --html=test-report.html --self-contained-html

# Verbose with short tracebacks
venv/bin/pytest tests/ -v --tb=short
```

## Test files

| File | Tests | What it covers |
|------|------:|----------------|
| `test_line_protocol.py` | 693 | InfluxDB line protocol formatting. Unicode fuzzing across 50+ scripts (CJK, Arabic, Hebrew, emoji, ZWJ sequences). Adversarial payloads (SQL injection, XSS, null bytes, 1MB strings). Type matrix (int/float/str/bool/None x tag counts). 100-seed hypothesis-style random generation. Cross-implementation validation between `emit()` and `format_line()`. NaN/infinity filtering. Tag newline stripping. |
| `test_mqtt_handler.py` | 417 | MQTT protobuf message handling. All 5 POWER_MAP and 5 VA_MAP field renames with boundary values. Per-phase extraction for 5 sources x multiple phase counts. SOC validation (0-100 range). Inverter counting. Grid events. All 3 enum maps (BATT_MODE, GRID_RELAY, DRY_CONTACT). Config change-only emission. Dry contact state tracking across 4 relays. Schema version checking. Anomaly detection thresholds. |
| `test_history_loader.py` | 374 | JSON-to-line-protocol conversion. 80 interval field combinations for grid/battery power calculation. 60 synthetic today.json variants (zero production, battery-only, nighttime, malformed). 40 intentionally broken structures. Field type coercion. Mock InfluxDB HTTP writes with error codes. |
| `test_cloud_poll.py` | 285 | Cloud API response parsing for all 10 scheduled endpoints. Schedule interval gating. Battery charge format parsing ("85%", 85, "85.5%", None). Deeply nested site_data extraction. Inverter fleet status. Alarm counting. Endpoint error recovery. Serial discovery across 50 response structure variants. |
| `test_history_downloader.py` | 178 | HistoryCloner lifecycle: init, progress tracking, resume from checkpoint, date detection, HTTP fetch with 200/404/500, cached file skipping, rate limiting. Progress state machine transitions. |
| `test_persona_gaps.py` | 168 | Gap coverage from 6 personas: SRE (auth retry, reconnect, signals, clock jumps), Security (credential leaks, injection, file permissions), Data (sign conventions, units, timestamps, energy balance), QA (schema drift, regression, enum maps), Home User (no battery, MFA, wrong password), Installer (idempotency, non-interactive, permissions). |
| `test_setup_deploy.py` | 100 | Shell script validation (bash -n syntax checks). .env loading with 25 format variants. Credential file parsing. CLI argument parsing. Script content verification (setup steps, ASCII art, references). |
| `test_emit.py` | 93 | The `emit()` function: stdout capture, thread safety under contention (2-50 concurrent threads), auto/explicit timestamps, all field type formatting, emit_error with backoff, 50-seed random fuzzing. |
| `test_error_backoff.py` | 83 | Exponential backoff: 60s to 3600s cap. Per-component isolation. Boundary timing (59.999s vs 60.001s). Clear/reset behavior. |
| `test_e2e.py` | 20 | Real services: Enphase cloud login + 7 API endpoints. InfluxDB health check, write/read roundtrip, batch writes. Live enphase_telegraf.py output capture. History conversion to InfluxDB pipeline. |

## Test design philosophy

### Parametrized fuzzing

Most test classes use `@pytest.mark.parametrize` with large input sets. For
example, `TestEscTagUnicode` tests `_esc_tag()` with 101 strings covering every
major Unicode block plus control characters. The assertion is always the same
(no crash, output is str, no unescaped specials) but the inputs cover the full
input space.

### Chaotic failure injection

Tests feed intentionally broken data into every parser:
- Empty dicts, None, wrong types, nested None
- Strings where numbers are expected
- NaN, infinity, negative zero
- SQL injection and XSS payloads in string fields
- Oversized inputs (10KB, 100KB, 1MB strings)

The invariant: **the code never crashes**. It may skip bad data, emit warnings,
or produce no output — but it must not raise an unhandled exception.

### State isolation

`enphase_telegraf.py` uses module-level globals for state tracking. Every test
file that imports it uses an `autouse` fixture to reset all 13 globals before
each test:

```python
@pytest.fixture(autouse=True)
def reset_globals():
    et._serial = "TEST123"
    et._last_batt_mode = None
    et._last_grid_relay = None
    et._last_gen_relay = None
    et._last_reserve_pct = None
    et._last_dry_contacts = {}
    et._error_backoff = {}
    et._known_fields = None
    et._unknown_enums_seen = set()
    ...
    yield
```

### Persona-based coverage

Tests in `test_persona_gaps.py` were designed by thinking as 6 different roles:

- **SRE**: What breaks at 3am? Auth expiration, MQTT disconnect, clock jumps.
- **Security**: Can a malicious payload escape into InfluxDB? Are credentials logged?
- **Data engineer**: Are signs correct? Are units consistent? Do timestamps make sense?
- **QA**: What if Enphase changes their protobuf schema? Their API response format?
- **Home user**: What if I have no battery? What if I type my password wrong?
- **Installer**: Can setup run non-interactively? Is it idempotent? Do permissions work?

## Bugs found by the test suite

The test suite discovered and fixed these bugs in application code:

1. **`int(None)` crash** — Inverter API can return `None` for count fields. `data.get("total", 0)` returns `None` when the key exists with value `None`. Fixed: `data.get("total") or 0`.

2. **`str > int` TypeError** — Alarm API can return `"5"` instead of `5` for total. `"5" > 0` raises TypeError in Python 3. Fixed: wrapped in `try: int(...) except`.

3. **Newlines in tag values** — `_esc_tag()` didn't strip `\n` or `\r`, producing corrupted multi-line output. Fixed: added `.replace("\n", "").replace("\r", "")`.

4. **NaN/Infinity in float fields** — `float('nan')` emitted as `nan`, which InfluxDB rejects silently. Fixed: added `math.isfinite(v)` guard.

## Adding new tests

1. Create a new test class in the appropriate file (or a new file)
2. If testing `enphase_telegraf.py` functions, use the `reset_globals` and `capture_emit` fixture pattern
3. If testing with real services, mark with `@pytest.mark.e2e`
4. Run: `venv/bin/pytest tests/your_test.py -v`
5. Check the full suite still passes: `venv/bin/pytest tests/`
