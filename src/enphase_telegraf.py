#!/usr/bin/env python3
"""
enphase-telegraf — Stream Enphase solar+battery data to InfluxDB via Telegraf

Connects to your Enphase system via two cloud data sources:
  1. Enlighten API — 20 endpoints polled on smart schedules (2min–2hr)
  2. MQTT live stream — protobuf power data at ~1 message/second

Outputs InfluxDB line protocol to stdout. Runs forever with auto-reconnect.
Designed for Telegraf's execd input plugin, but works standalone too.

See docs/MEASUREMENT_TYPES.md for the complete field reference.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

# ── Globals ────────────────────────────────────────────────
_serial = ""
_client = None       # EnlightenClient
_stream = None       # LiveStreamClient
_running = True
_verbose = False
_stdout_lock = threading.Lock()

# Counters
_mqtt_messages = 0
_mqtt_errors = 0
_cloud_fetches = 0
_cloud_errors = 0
_auth_failures = 0
_start_time = time.time()

# State tracking (only emit when changed)
_last_batt_mode = None
_last_grid_relay = None
_last_gen_relay = None
_last_reserve_pct = None
_last_dry_contacts: dict[str, int] = {}  # {contact_id: state_int}

# Schema tracking (detect protobuf changes)
_expected_protocol_ver = 1
_known_fields: set | None = None
_unknown_enums_seen: set = set()


# ── Enum maps (protobuf → int, -1 = unknown) ──────────────

BATT_MODE_MAP = {
    "BATT_MODE_FULL_BACKUP":  0,
    "BATT_MODE_SELF_CONS":    1,
    "BATT_MODE_SAVINGS":      2,
}
GRID_RELAY_MAP = {
    "OPER_RELAY_OPEN":       1,
    "OPER_RELAY_CLOSED":     2,
    "OPER_RELAY_OFFGRID_AC_GRID_PRESENT":        3,
    "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD":   4,
    "OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID":  5,
    "OPER_RELAY_GEN_OPEN":       6,
    "OPER_RELAY_GEN_CLOSED":     7,
    "OPER_RELAY_GEN_STARTUP":    8,
    "OPER_RELAY_GEN_SYNC_READY": 9,
    "OPER_RELAY_GEN_AC_STABLE":  10,
    "OPER_RELAY_GEN_AC_UNSTABLE": 11,
}
DRY_CONTACT_STATE_MAP = {
    "DC_RELAY_STATE_INVALID": -1,
    "DC_RELAY_OFF": 0,
    "DC_RELAY_ON": 1,
}

def _enum_int(m: dict, name: str) -> int:
    return m.get(name, -1)


# ── Error rate limiter ─────────────────────────────────────
_error_backoff: dict[str, dict] = {}

def _should_emit_error(component: str, message: str) -> bool:
    now = time.time()
    state = _error_backoff.get(component)
    if not state:
        _error_backoff[component] = {"last_emit": now, "interval": 60, "message": message}
        return True
    if now - state["last_emit"] >= state["interval"]:
        state["last_emit"] = now
        state["interval"] = min(state["interval"] * 2, 3600)
        state["message"] = message
        return True
    return False

def _clear_error(component: str):
    _error_backoff.pop(component, None)


# ── Output ─────────────────────────────────────────────────

def log(msg: str):
    if _verbose:
        print(f"[enphase-telegraf] {msg}", file=sys.stderr, flush=True)

def warn(msg: str):
    print(f"[enphase-telegraf] WARNING: {msg}", file=sys.stderr, flush=True)

def error(msg: str):
    print(f"[enphase-telegraf] ERROR: {msg}", file=sys.stderr, flush=True)


def _esc_tag(s: str) -> str:
    return s.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

def _esc_field_str(s: str) -> str:
    """Escape a string field value for line protocol. Must handle quotes AND newlines."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")


def emit(measurement: str, tags: dict, fields: dict, ts_ns: int | None = None):
    """Write one InfluxDB line protocol line to stdout. Nothing else ever touches stdout."""
    if not fields:
        return
    if ts_ns is None:
        ts_ns = int(time.time() * 1_000_000_000)

    tag_str = ""
    for k, v in sorted(tags.items()):
        if v is not None and v != "":
            tag_str += f",{_esc_tag(k)}={_esc_tag(str(v))}"

    parts = []
    for k, v in sorted(fields.items()):
        if v is None:
            continue
        if isinstance(v, bool):
            parts.append(f"{k}={1 if v else 0}i")
            bool_str = "true" if v else "false"
            parts.append(f'{k}_str="{bool_str}"')
        elif isinstance(v, int):
            parts.append(f"{k}={v}i")
        elif isinstance(v, float):
            parts.append(f"{k}={v}")
        elif isinstance(v, str):
            parts.append(f'{k}="{_esc_field_str(v)}"')
    if not parts:
        return

    with _stdout_lock:
        print(f"{measurement}{tag_str} {','.join(parts)} {ts_ns}", flush=True)


def emit_error(component: str, message: str):
    if _should_emit_error(component, message):
        backoff = _error_backoff[component]["interval"]
        emit("enphase_error", {"serial": _serial, "component": component},
             {"message": message, "next_retry_s": backoff})


# ── Schema check ─────────────────────────────────────────

def _check_schema(msg: dict):
    global _known_fields

    proto_ver = msg.get("protocol_ver")
    if proto_ver is not None and proto_ver != _expected_protocol_ver:
        emit_error("proto_version",
                   f"protocol_ver changed from {_expected_protocol_ver} to {proto_ver}")
        emit("enphase_error", {"serial": _serial, "component": "proto_version"}, {
            "proto_version_mismatch": 1,
            "expected": _expected_protocol_ver,
            "actual": int(proto_ver),
        })

    fields_present = msg.get("_fields_present")
    if fields_present:
        if _known_fields is None:
            _known_fields = set(fields_present)
            log(f"Proto schema baseline: {sorted(_known_fields)}")
        else:
            new_fields = fields_present - _known_fields
            missing_fields = _known_fields - fields_present
            if new_fields:
                _known_fields |= new_fields
                emit_error("proto_new_fields",
                           f"New protobuf fields: {','.join(sorted(new_fields))}")
            if missing_fields:
                emit_error("proto_missing_fields",
                           f"Missing protobuf fields: {','.join(sorted(missing_fields))}")

    for field_name, enum_map in [("batt_mode", BATT_MODE_MAP),
                                  ("grid_relay", GRID_RELAY_MAP),
                                  ("gen_relay", GRID_RELAY_MAP)]:
        val = msg.get(field_name)
        if val and val not in enum_map and val not in _unknown_enums_seen:
            _unknown_enums_seen.add(val)
            emit_error("proto_unknown_enum", f"Unknown {field_name}: {val}")


# ── MQTT Handler ──────────────────────────────────────────

def on_mqtt_data(msg: dict):
    global _mqtt_messages, _last_batt_mode, _last_grid_relay, _last_gen_relay
    _mqtt_messages += 1
    _clear_error("mqtt")
    _check_schema(msg)

    # ── Power fields (renamed for clarity — see docs/MEASUREMENT_TYPES.md) ──
    # Proto field names → user-friendly InfluxDB field names
    POWER_MAP = [
        ("pv_power_w",        "solar_w"),
        ("grid_power_w",      "grid_w"),
        ("load_power_w",      "consumption_w"),
        ("storage_power_w",   "battery_w"),
        ("generator_power_w", "generator_w"),
    ]
    VA_MAP = [
        ("pv_apparent_va",        "solar_va"),
        ("grid_apparent_va",      "grid_va"),
        ("load_apparent_va",      "consumption_va"),
        ("storage_apparent_va",   "battery_va"),
        ("generator_apparent_va", "generator_va"),
    ]
    PHASE_PREFIX_MAP = {
        "pv": "solar", "grid": "grid", "load": "consumption",
        "storage": "battery", "generator": "generator",
    }

    fields = {}
    anomalies = {}

    for proto_key, field_name in POWER_MAP:
        val = msg.get(proto_key)
        if val is None:
            continue
        fval = float(val)
        if abs(fval) > 100_000:
            anomalies[f"bad_{field_name}"] = True
        else:
            fields[field_name] = fval

    for proto_key, field_name in VA_MAP:
        val = msg.get(proto_key)
        if val is not None:
            fields[field_name] = float(val)

    # SOC — MeterSummaryData.soc is the ACTUAL battery charge (e.g., 77%)
    # DataMsg.backup_soc is the reserve SETTING (e.g., 20%) → goes to enphase_config
    meter_soc = msg.get("meter_soc")
    if meter_soc is not None:
        soc_int = int(meter_soc)
        if 0 <= soc_int <= 100:
            fields["soc"] = soc_int
        else:
            anomalies["bad_soc"] = True

    # Per-phase power
    for proto_prefix, out_prefix in PHASE_PREFIX_MAP.items():
        phases = msg.get(f"{proto_prefix}_phase_w")
        if phases:
            for i, pw in enumerate(phases):
                fval = float(pw)
                if abs(fval) > 50_000:
                    anomalies[f"bad_{out_prefix}_l{i+1}"] = True
                else:
                    fields[f"{out_prefix}_l{i+1}_w"] = fval
        phases_va = msg.get(f"{proto_prefix}_phase_va")
        if phases_va:
            for i, pva in enumerate(phases_va):
                fields[f"{out_prefix}_l{i+1}_va"] = float(pva)

    # Phase sum consistency check
    for out_prefix in ("solar", "consumption"):
        agg = fields.get(f"{out_prefix}_w")
        l1 = fields.get(f"{out_prefix}_l1_w")
        l2 = fields.get(f"{out_prefix}_l2_w")
        if agg is not None and l1 is not None and l2 is not None:
            phase_sum = l1 + l2
            if abs(phase_sum) > 10 and abs(agg - phase_sum) / abs(phase_sum) > 0.5:
                anomalies[f"bad_{out_prefix}_phase_sum"] = True

    # Microinverter status
    pcu_total = msg.get("pcu_total")
    if pcu_total is not None:
        fields["inverters_total"] = int(pcu_total)
    pcu_running = msg.get("pcu_running")
    if pcu_running is not None:
        fields["inverters_producing"] = int(pcu_running)

    # Grid outage detection
    if msg.get("grid_update_ongoing") is not None:
        fields["grid_update_ongoing"] = int(msg["grid_update_ongoing"])
    if msg.get("grid_outage_status") is not None:
        fields["grid_outage"] = int(msg["grid_outage_status"])

    # Protocol version
    proto_ver = msg.get("protocol_ver")
    if proto_ver is not None:
        fields["protocol_ver"] = int(proto_ver)

    # Timestamp
    ts_raw = msg.get("timestamp")
    ts_ns = int(ts_raw) * 1_000_000_000 if ts_raw else None

    if fields:
        emit("enphase_power", {"serial": _serial, "source": "mqtt"}, fields, ts_ns)

    if anomalies:
        emit_error("data_quality", f"Anomalies: {','.join(sorted(anomalies.keys()))}")

    # ── Dry contacts (emit on state CHANGE only) ──────
    for dc in msg.get("dry_contacts", []):
        dc_id = dc.get("id", "unknown")
        dc_state_str = dc.get("state", "unknown")
        dc_state_int = DRY_CONTACT_STATE_MAP.get(dc_state_str, -1)
        if _last_dry_contacts.get(dc_id) != dc_state_int:
            _last_dry_contacts[dc_id] = dc_state_int
            emit("enphase_dry_contact", {"serial": _serial, "contact": dc_id}, {
                "state": dc_state_int,
                "state_str": dc_state_str,
            }, ts_ns)

    # ── Config/state changes (emit only when value changes) ──
    batt_mode = msg.get("batt_mode")
    if batt_mode and batt_mode != _last_batt_mode:
        _last_batt_mode = batt_mode
        config_fields = {
            "battery_mode": _enum_int(BATT_MODE_MAP, batt_mode),
            "battery_mode_str": batt_mode,
        }
        # backup_soc is the reserve SETTING, not the charge level
        backup_soc = msg.get("soc")
        if backup_soc is not None:
            config_fields["backup_reserve_pct"] = int(backup_soc)
        emit("enphase_config", {"serial": _serial}, config_fields, ts_ns)

    grid_relay = msg.get("grid_relay")
    if grid_relay and grid_relay != _last_grid_relay:
        _last_grid_relay = grid_relay
        emit("enphase_config", {"serial": _serial}, {
            "grid_relay": _enum_int(GRID_RELAY_MAP, grid_relay),
            "grid_relay_str": grid_relay,
        }, ts_ns)

    gen_relay = msg.get("gen_relay")
    if gen_relay and gen_relay != _last_gen_relay:
        _last_gen_relay = gen_relay
        emit("enphase_config", {"serial": _serial}, {
            "gen_relay": _enum_int(GRID_RELAY_MAP, gen_relay),
            "gen_relay_str": gen_relay,
        }, ts_ns)


def on_mqtt_status(msg: str):
    global _mqtt_errors
    log(f"MQTT: {msg}")
    if "failed" in msg.lower() or "error" in msg.lower():
        _mqtt_errors += 1
        emit_error("mqtt", msg)


# ── Cloud Polling ─────────────────────────────────────────

CLOUD_SCHEDULE = {
    "latest_power": 120, "battery_status": 120, "today": 300,
    "events": 300, "alarms": 600,
    "devices": 3600, "site_data": 3600, "inverters": 1800,
    "battery_schedules": 600,
}
_cloud_last_fetch: dict[str, float] = {}


def cloud_poll_once():
    global _cloud_fetches, _cloud_errors, _last_reserve_pct
    if not _client:
        return

    now = time.time()
    getter_map = {
        "latest_power": _client.get_latest_power,
        "battery_status": _client.get_battery_status,
        "today": _client.get_today,
        "events": _client.get_events,
        "alarms": _client.get_alarms,
        "battery_settings": _client.get_battery_settings,
        "devices": _client.get_devices,
        "site_data": _client.get_site_data,
        "inverters": _client.get_inverters,
        "battery_schedules": _client.get_battery_schedules,
    }

    for endpoint, interval in CLOUD_SCHEDULE.items():
        last = _cloud_last_fetch.get(endpoint, 0)
        if now - last < interval:
            continue
        getter = getter_map.get(endpoint)
        if not getter:
            continue

        try:
            data = getter()
            _cloud_last_fetch[endpoint] = now
            _cloud_fetches += 1
            _clear_error(f"cloud_{endpoint}")
            log(f"Cloud: fetched {endpoint}")

            if endpoint == "latest_power" and isinstance(data, dict):
                lp = data.get("latest_power", {})
                if isinstance(lp, dict) and lp.get("value") is not None:
                    emit("enphase_power", {"serial": _serial, "source": "cloud"},
                         {"solar_w": float(lp["value"])})

            elif endpoint == "battery_status" and isinstance(data, dict):
                # Rich battery data → dedicated enphase_battery measurement
                bat_fields = {}
                charge = data.get("current_charge")
                if isinstance(charge, str) and "%" in charge:
                    try:
                        bat_fields["soc"] = int(charge.replace("%", "").strip())
                    except ValueError:
                        pass
                elif isinstance(charge, (int, float)):
                    bat_fields["soc"] = int(charge)
                for src, dst in [("available_energy", "available_energy_kwh"),
                                 ("max_capacity", "max_capacity_kwh"),
                                 ("available_power", "available_power_kw"),
                                 ("max_power", "max_power_kw"),
                                 ("included_count", "unit_count"),
                                 ("active_micros", "active_inverters"),
                                 ("total_micros", "total_inverters")]:
                    val = data.get(src)
                    if val is not None:
                        bat_fields[dst] = float(val) if "kwh" in dst or "kw" in dst else int(val)
                # Per-battery cycle count and SOH
                storages = data.get("storages", [])
                for i, s in enumerate(storages[:4]):  # up to 4 batteries
                    n = i + 1
                    cc = s.get("cycle_count")
                    if cc is not None:
                        bat_fields[f"cycle_count_{n}"] = int(cc)
                    soh = s.get("battery_soh")
                    if isinstance(soh, str) and "%" in soh:
                        try:
                            bat_fields[f"soh_{n}"] = int(soh.replace("%", "").strip())
                        except ValueError:
                            pass
                if bat_fields:
                    emit("enphase_battery", {"serial": _serial}, bat_fields)
                # Also emit SOC to enphase_power for unified power+SOC queries
                if "soc" in bat_fields:
                    emit("enphase_power", {"serial": _serial, "source": "cloud"},
                         {"soc": bat_fields["soc"]})

            elif endpoint == "today" and isinstance(data, dict):
                # Totals are inside stats[0].totals, not top-level
                stats = data.get("stats", [])
                totals = stats[0].get("totals", {}) if stats else {}
                fields = {}
                for src, dst in [("production", "production_wh"),
                                 ("consumption", "consumption_wh"),
                                 ("charge", "charge_wh"),
                                 ("discharge", "discharge_wh"),
                                 ("solar_home", "solar_to_home_wh"),
                                 ("solar_battery", "solar_to_battery_wh"),
                                 ("solar_grid", "solar_to_grid_wh"),
                                 ("battery_home", "battery_to_home_wh"),
                                 ("battery_grid", "battery_to_grid_wh"),
                                 ("grid_home", "grid_to_home_wh"),
                                 ("grid_battery", "grid_to_battery_wh")]:
                    val = totals.get(src)
                    if val is not None:
                        fields[dst] = float(val)
                if fields:
                    emit("enphase_energy", {"serial": _serial}, fields)
                # Battery details from today endpoint
                bd = data.get("battery_details", {})
                if isinstance(bd, dict):
                    bat_extra = {}
                    for src, dst in [("aggregate_soc", "soc"),
                                     ("estimated_time", "estimated_backup_min"),
                                     ("last_24h_consumption", "last_24h_consumption_kwh")]:
                        val = bd.get(src)
                        if val is not None:
                            bat_extra[dst] = float(val) if "kwh" in dst else int(val)
                    if bat_extra:
                        emit("enphase_battery", {"serial": _serial}, bat_extra)
                # Battery config from today endpoint
                bc = data.get("batteryConfig", {})
                if isinstance(bc, dict):
                    reserve = bc.get("battery_backup_percentage")
                    if reserve is not None and reserve != _last_reserve_pct:
                        _last_reserve_pct = reserve
                        cfg_fields = {
                            "backup_reserve_pct": int(reserve),
                            "very_low_soc_pct": int(bc.get("very_low_soc", 0)),
                            "charge_from_grid": int(bool(bc.get("charge_from_grid"))),
                            "storm_guard": int(bc.get("severe_weather_watch") == "enabled"),
                        }
                        usage = bc.get("usage")
                        if usage:
                            cfg_fields["usage_str"] = str(usage)
                        emit("enphase_config", {"serial": _serial}, cfg_fields)
                # Connection details
                conn = data.get("connectionDetails", [])
                if isinstance(conn, list) and conn:
                    cd = conn[0] if isinstance(conn[0], dict) else {}
                    emit("enphase_gateway", {"serial": _serial}, {
                        "wifi": int(bool(cd.get("wifi"))),
                        "cellular": int(bool(cd.get("cellular"))),
                        "ethernet": int(bool(cd.get("ethernet"))),
                    })

            elif endpoint == "site_data" and isinstance(data, dict):
                # Lifetime energy totals
                lt = data.get("module", {}).get("lifetime", {}).get("lifetimeEnergy", {})
                if isinstance(lt, dict):
                    fields = {}
                    val = lt.get("value")
                    if val is not None:
                        fields["lifetime_production_wh"] = float(val)
                    cons = lt.get("consumed")
                    if cons is not None:
                        fields["lifetime_consumption_wh"] = float(cons)
                    if fields:
                        emit("enphase_energy", {"serial": _serial}, fields)
                # System status
                detail = data.get("module", {}).get("detail", {}).get("system", {})
                if isinstance(detail, dict):
                    status_code = detail.get("statusCode")
                    if status_code:
                        emit("enphase_gateway", {"serial": _serial}, {
                            "status_str": str(status_code),
                            "microinverters": int(detail.get("microinverters", 0)),
                            "batteries": int(detail.get("encharge", 0)),
                        })

            elif endpoint == "inverters" and isinstance(data, dict):
                total = data.get("total", 0)
                not_rpt = data.get("not_reporting", 0)
                err = data.get("error_count", 0)
                warn_ct = data.get("warning_count", 0)
                emit("enphase_inverters", {"serial": _serial}, {
                    "total": int(total),
                    "not_reporting": int(not_rpt),
                    "error_count": int(err),
                    "warning_count": int(warn_ct),
                    "normal_count": int(data.get("normal_count", 0)),
                })

            elif endpoint == "alarms" and isinstance(data, dict):
                alarm_total = data.get("total", 0)
                if alarm_total > 0:
                    emit("enphase_gateway", {"serial": _serial}, {
                        "alarm_count": int(alarm_total),
                    })

        except Exception as e:
            _cloud_errors += 1
            log(f"Cloud {endpoint}: {type(e).__name__}: {e}")
            emit_error(f"cloud_{endpoint}", f"{type(e).__name__}: {str(e)[:100]}")
            _cloud_last_fetch[endpoint] = now - interval + 60


def cloud_poll_loop():
    time.sleep(10)
    while _running:
        try:
            cloud_poll_once()
        except Exception as e:
            warn(f"Cloud poll error: {e}")
        time.sleep(30)


# ── Status heartbeat ──────────────────────────────────────

def status_loop():
    while _running:
        time.sleep(30)
        emit("enphase_status", {"serial": _serial}, {
            "uptime_s": int(time.time() - _start_time),
            "mqtt_connected": int(bool(_stream and _stream.connected)),
            "mqtt_msg_total": _mqtt_messages,
            "mqtt_err_total": _mqtt_errors,
            "cloud_ok": int(bool(_client and _client.authenticated)),
            "cloud_fetch_total": _cloud_fetches,
            "cloud_err_total": _cloud_errors,
            "auth_err_total": _auth_failures,
        })


# ── Discovery ─────────────────────────────────────────────

def discover_serial(client) -> str:
    try:
        devices = client.get_devices()
        if not isinstance(devices, dict):
            return ""
        for group in devices.get("result", []):
            if not isinstance(group, dict):
                continue
            if group.get("type") in ("envoy", "gateway"):
                for d in group.get("devices", []):
                    serial = d.get("serial_number") or d.get("serial_num") or d.get("sn")
                    if serial:
                        return str(serial)
        for key in ("envoys", "envoy", "gateways"):
            devs = devices.get(key, [])
            if isinstance(devs, list):
                for d in devs:
                    if isinstance(d, dict):
                        serial = d.get("serial_number") or d.get("serial_num") or d.get("sn")
                        if serial:
                            return str(serial)
    except Exception as e:
        warn(f"Discovery failed: {e}")
    return ""


# ── Main ──────────────────────────────────────────────────

def main():
    global _serial, _client, _stream, _running, _auth_failures, _verbose

    # Route all Python logging to stderr (never stdout).
    # This catches log output from requests, paho-mqtt, protobuf, and our cloud library.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="[enphase-telegraf] %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="enphase-telegraf",
        description="Stream Enphase solar/battery data as InfluxDB line protocol.",
        epilog="""
measurements written to stdout (InfluxDB line protocol):

  enphase_power        Real-time power from MQTT (~1/sec) and cloud (~2min).
                       Fields: solar_w, grid_w, consumption_w, battery_w,
                       soc, per-phase *_l1_w/*_l2_w, *_va, inverters_total,
                       inverters_producing, grid_outage, protocol_ver.
                       Tags: serial, source (mqtt|cloud).

  enphase_energy       Daily energy totals from cloud (every 5min).
                       Fields: production_wh, consumption_wh, charge_wh,
                       discharge_wh, solar_to_home_wh, grid_to_home_wh, etc.

  enphase_battery      Battery details from cloud (every 2min).
                       Fields: soc, available_energy_kwh, max_capacity_kwh,
                       cycle_count_1, soh_1, estimated_backup_min.

  enphase_config       Config/state changes only (not every second).
                       Fields: battery_mode, grid_relay, backup_reserve_pct,
                       charge_from_grid, storm_guard, usage_str.

  enphase_dry_contact  Dry contact relay changes (per contact, on change only).

  enphase_status       Collector health heartbeat (every 30s).

  enphase_error        Errors needing attention (rate-limited, 60s→3600s backoff).

  See docs/MEASUREMENT_TYPES.md for the complete field reference.

examples:
  ./bin/enphase-telegraf --verbose                            # debug mode
  ./bin/enphase-telegraf                                      # Telegraf mode
  ./bin/enphase-telegraf --email X --password Y --serial Z    # explicit config
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --help writes to stderr so Telegraf never sees it
    parser._print_message = lambda msg, file=None: print(msg, file=sys.stderr, end="")

    parser.add_argument("--email", default=os.environ.get("ENPHASE_EMAIL", ""),
                        help="Enlighten account email (or ENPHASE_EMAIL env var)")
    parser.add_argument("--password", default=os.environ.get("ENPHASE_PASSWORD", ""),
                        help="Enlighten account password (or ENPHASE_PASSWORD env var)")
    parser.add_argument("--serial", default=os.environ.get("ENPHASE_SERIAL", ""),
                        help="Gateway serial (or ENPHASE_SERIAL, auto-discovered if not set)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print status messages to stderr")
    args = parser.parse_args()
    _verbose = args.verbose

    if _verbose:
        logging.getLogger().setLevel(logging.INFO)

    if not args.email or not args.password:
        error("Credentials required. Set ENPHASE_EMAIL/ENPHASE_PASSWORD or use --email/--password.")
        parser.print_usage(sys.stderr)
        sys.exit(1)

    # ── Setup ──────────────────────────────────────────
    try:
        from enphase_cloud.enlighten import EnlightenClient, AuthError, MFARequired
        from enphase_cloud.livestream import LiveStreamClient
    except ImportError as e:
        error(f"Missing dependency: {e}")
        error("Run: pip install requests paho-mqtt protobuf")
        error("And ensure enphase_cloud package is on your PYTHONPATH (the src/ directory).")
        sys.exit(1)

    def shutdown(signum, frame):
        global _running
        log("Shutting down...")
        _running = False
        if _stream:
            _stream.stop()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # ── Login (retries with backoff) ───────────────────
    backoff = 10
    while _running:
        try:
            log(f"Logging in as {args.email}...")
            client = EnlightenClient(args.email, args.password)
            client.login()
            _client = client
            _clear_error("auth")
            log(f"Logged in — site_id={client._session.site_id}")
            break
        except AuthError as e:
            _auth_failures += 1
            error(f"Login failed: {e}")
            error(f"Check your email and password. Retrying in {backoff}s...")
            emit_error("auth", str(e))
            time.sleep(backoff)
            backoff = min(backoff * 2, 600)
        except MFARequired:
            _auth_failures += 1
            error("MFA is enabled on your Enphase account.")
            error("Disable it in the Enphase app: Account > Security > Two-Factor Authentication")
            emit_error("auth", "MFA enabled — disable in Enphase app")
            time.sleep(300)
        except Exception as e:
            _auth_failures += 1
            error(f"Login error: {e}")
            emit_error("auth", f"{type(e).__name__}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

    if not _running:
        return

    # ── Discover serial ────────────────────────────────
    serial = args.serial
    if not serial:
        log("Discovering gateway serial...")
        serial = discover_serial(_client)
        if not serial:
            error("Could not find your gateway serial number.")
            error("Set it explicitly with --serial or ENPHASE_SERIAL.")
            emit_error("discovery", "Could not discover gateway serial")
            sys.exit(1)
    _serial = serial
    log(f"Gateway serial: {_serial}")

    # ── Start background threads ───────────────────────
    threading.Thread(target=cloud_poll_loop, daemon=True, name="cloud-poll").start()
    threading.Thread(target=status_loop, daemon=True, name="status").start()

    # ── MQTT stream (main loop) ────────────────────────
    while _running:
        try:
            log(f"Starting MQTT stream for {_serial}...")
            stream = LiveStreamClient(_client)
            _stream = stream
            stream.start(_serial, on_data=on_mqtt_data, on_status=on_mqtt_status)

            while _running:
                time.sleep(10)
                if not _client.authenticated:
                    log("Session expired, re-authenticating...")
                    try:
                        _client.login()
                        _clear_error("auth")
                        log("Re-authenticated")
                    except Exception as e:
                        _auth_failures += 1
                        warn(f"Re-auth failed: {e}")
                        emit_error("auth", f"Re-auth: {e}")

                if _stream and _stream._last_message_time > 0:
                    age = time.time() - _stream._last_message_time
                    if age > 300 and _stream._running:
                        warn(f"No MQTT data for {int(age)}s — restarting stream")
                        emit_error("mqtt", f"No messages for {int(age)}s, restarting")
                        break

        except Exception as e:
            warn(f"Stream error: {e}")
            emit_error("mqtt", f"{type(e).__name__}: {e}")

        if _stream:
            _stream.stop()
            _stream = None
        if _running:
            log("Reconnecting in 10s...")
            time.sleep(10)

    log(f"Stopped. {_mqtt_messages} messages processed.")


if __name__ == "__main__":
    main()
