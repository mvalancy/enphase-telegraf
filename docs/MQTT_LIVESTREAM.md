# Enphase MQTT Live Stream Protocol Reference

Technical reference for the Enphase Enlighten real-time data stream. This
document covers the full connection lifecycle, protobuf wire format, field
mapping to InfluxDB, and schema validation logic. Written for developers who
want to understand or reimplement the MQTT client.

Source files:

- `src/enphase_cloud/livestream.py` -- MQTT client, credential fetch, protobuf decode
- `src/enphase_telegraf.py` -- field mapping, anomaly detection, schema validation
- `proto/DataMsg.proto`, `proto/MeterSummaryData.proto`, `proto/HemsStreamMessage.proto`

---

## 1. Overview

The Enphase Enlighten MQTT live stream delivers real-time power telemetry from
an Enphase IQ Gateway (formerly Envoy) through AWS IoT Core. Messages arrive
at approximately 1 Hz and contain protobuf-encoded snapshots of the entire
system state: per-phase active and apparent power for every meter source (solar,
battery, grid, load, generator), battery state-of-charge, relay states, dry
contact states, microinverter counts, and grid outage indicators.

**Update rate:** ~1 message per second.

**Data freshness:** Sub-second latency from the gateway to the MQTT broker.
This is dramatically fresher than the Enlighten cloud REST API, which has
polling intervals of 2--60 minutes depending on the endpoint.

**Complementary data sources:** The MQTT stream provides instantaneous power
readings only. Cumulative energy (kWh), per-inverter production, battery
cycle counts, and configuration data come from the cloud API endpoints polled
on separate schedules.

---

## 2. Connection Flow

### 2.1 Credential Acquisition

Two HTTP requests fetch the credentials needed to connect to the MQTT broker.
Both require an authenticated Enlighten session (cookie-based auth with
`_enlighten_4_session`).

#### Live Stream Credentials

```
GET https://enlighten.enphaseenergy.com/pv/aws_sigv4/livestream.json?serial_num={serial}
```

Parameters:
- `serial_num` -- the IQ Gateway serial number (e.g., `122312345678`)

Response (JSON):
```json
{
  "aws_iot_endpoint": "a]example.iot.us-east-1.amazonaws.com",
  "aws_authorizer": "aws-lambda-authoriser-prod",
  "aws_token_key": "enph_token",
  "aws_token_value": "<signed JWT>",
  "aws_digest": "<base64 HMAC signature>",
  "live_stream_topic": "/sites/{site_id}/live/update"
}
```

The `aws_iot_endpoint` is the AWS IoT Core hostname. The `aws_authorizer`,
`aws_token_key`, `aws_token_value`, and `aws_digest` fields are used to
construct the custom authorizer parameters for the WebSocket connection.
The `live_stream_topic` is the MQTT topic that carries protobuf `DataMsg`
payloads.

#### Response Stream Credentials

```
GET https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/mqttSignedUrl/{site_id}
```

Headers:
- Standard Enlighten auth headers
- `username` -- the Enlighten user ID (from the session)

Response (JSON):
```json
{
  "topic": "/sites/{site_id}/response/...",
  ...
}
```

The `topic` field is the MQTT topic for response-stream messages. This topic
carries JSON-encoded payloads (not protobuf) for battery configuration
responses and similar out-of-band data.

### 2.2 MQTT Connection

The client connects to AWS IoT Core over MQTT-over-WebSocket on port 443 with
TLS. The connection parameters are:

| Parameter       | Value                                          |
|-----------------|------------------------------------------------|
| Transport       | WebSocket (`transport="websockets"`)           |
| Port            | 443                                            |
| TLS             | Enabled (`client.tls_set()` -- system CA bundle) |
| Keep-alive      | 60 seconds (Paho default, matches React app)   |
| Client ID       | `em-paho-mqtt-{epoch_ms}` (e.g., `em-paho-mqtt-1711234567890`) |

**Client ID format:** The React app uses `em-paho-mqtt-` followed by
`moment().valueOf()` (milliseconds since epoch). The Python client replicates
this with `int(time.time() * 1000)`.

### 2.3 Authentication via Username Field

AWS IoT custom authorizer parameters are passed in the MQTT `username` field.
This is necessary because Paho's Python WebSocket transport does not correctly
forward URL query parameters.

The username string is constructed as:

```
?x-amz-customauthorizer-name={authorizer}
&{token_key}={token_value}
&site-id={site_id}
&x-amz-customauthorizer-signature={url_encode(digest)}
```

Concrete example:

```
?x-amz-customauthorizer-name=aws-lambda-authoriser-prod
&enph_token=eyJhbGciOiJIUzI1N...
&site-id=12345
&x-amz-customauthorizer-signature=abc123%2Bdef456%3D%3D
```

The `digest` value is URL-encoded (via `urllib.parse.quote`) because it may
contain `+`, `/`, and `=` characters from its base64 encoding. The MQTT
password field is not set.

### 2.4 Topic Subscription

After a successful connection (`reason_code == 0` in the `on_connect`
callback), the client subscribes to both topics:

| Topic              | Payload Format | QoS |
|--------------------|----------------|-----|
| `live_stream_topic`   | Protobuf (`DataMsg`) | 1 |
| `response_stream_topic` | JSON | 1 |

Both use QoS 1 (at least once delivery), matching the React app behavior.

The live stream topic delivers the high-frequency power data. The response
stream topic delivers JSON messages related to battery configuration and
other control-plane responses.

---

## 3. Session Management

### 3.1 Session Lifecycle

MQTT sessions have a 15-minute TTL imposed by the AWS IoT custom authorizer.
The client proactively reconnects before expiry to avoid a hard disconnect.

```
    |----- 14 min active -----|-2s-|---- 14 min active -----|
    connect                  disconnect  connect           disconnect
         ^                        ^           ^
    subscribe to topics    credential    subscribe to topics
                           refresh
```

**Timing constants** (reverse-engineered from `main-78975918.adb367c7.js`):

| Constant             | Value  | Purpose                                   |
|----------------------|--------|-------------------------------------------|
| `SESSION_DURATION_S` | 840s   | Disconnect after 14 minutes               |
| `SESSION_GAP_S`      | 2s     | Pause between disconnect and reconnect    |
| `CONNECT_TIMEOUT_S`  | 10s    | Max wait for `on_connect` callback        |
| `RECONNECT_DELAY_S`  | 30s    | Backoff after a failed connection attempt  |
| `MQTT_KEEPALIVE_S`   | 60s    | MQTT keep-alive interval (Paho default)   |

### 3.2 Reconnection Logic

The main loop (`_run_loop`) operates as follows:

```python
while running:
    try:
        connect()
        wait up to 10s for on_connect callback
        if not connected:
            disconnect()
            sleep(30)          # RECONNECT_DELAY_S
            continue
        # Stream for 14 minutes
        while running and connected:
            if elapsed > 840s: break
            sleep(1)
        disconnect()
        if running:
            sleep(2)           # SESSION_GAP_S
    except Exception:
        disconnect()
        if running:
            sleep(30)          # RECONNECT_DELAY_S
```

On each reconnect cycle, credentials are re-fetched from the Enlighten API
(both `livestream.json` and `mqttSignedUrl`), ensuring a fresh JWT and HMAC
digest.

### 3.3 Data Staleness Detection

The outer watchdog loop in `enphase_telegraf.py` monitors the time since the
last received MQTT message. If no messages arrive for 300 seconds (5 minutes),
the entire stream is torn down and restarted:

```python
if stream._last_message_time > 0:
    age = time.time() - stream._last_message_time
    if age > 300 and stream._running:
        # emit error, break out of inner loop, restart stream
```

This catches cases where the MQTT connection appears healthy (no disconnect
callback) but the broker has stopped delivering messages.

### 3.4 Other React App Constants

These additional constants were found in the React app source but are not
used by this collector:

| Constant                 | Value | Meaning                                    |
|--------------------------|-------|--------------------------------------------|
| `this.timeOut`           | 10000 | 10s data staleness timeout (UI-side only)  |
| `maxMqttResponseCount`   | 3     | Some views disconnect after 3 messages     |
| `log_live_status`        | --    | Analytics POST, not a data trigger         |

---

## 4. Protobuf Schema

All protobuf definitions use proto3 syntax. Messages are compiled with
`protoc` and loaded at runtime from `DataMsg_pb2.py` and
`MeterSummaryData_pb2.py`.

### 4.1 DataMsg (top-level message)

```protobuf
// proto/DataMsg.proto
syntax = "proto3";

import "MeterSummaryData.proto";
import "HemsStreamMessage.proto";

message DataMsg {
  int32 protocol_ver = 1;          // Schema version. Expected: 1
  uint64 timestamp = 2;            // DUMMY VALUE (always 1000000) -- see below
  MeterSummaryData meters = 3;     // All meter channels
  BattMode batt_mode = 4;          // Battery operating mode
  int32 backup_soc = 5;            // Backup reserve SETTING (%), not charge level
  repeated DryContactStatus dry_contact_relay_status = 6;
  repeated DryContactName dry_contact_relay_name = 7;
  repeated LoadStatus load_status = 8;
  PowerMatchStatus power_match_status = 9;
}
```

**Critical: the `timestamp` field is unreliable.** Enphase sends a fixed dummy
value (typically `1000000`) in this field. The MQTT delivery time is the
actual timestamp. The collector uses `time.time()` at message arrival as the
authoritative timestamp for all emitted data points.

**`backup_soc` vs `meters.soc`:** These are distinct values.
`backup_soc` (field 5) is the backup reserve *setting* -- the minimum charge
percentage the battery maintains for outage protection (e.g., 20%).
`meters.soc` (inside `MeterSummaryData`, field 6) is the *actual* current
battery charge percentage (e.g., 77%). The collector maps `meters.soc` to
the `soc` field in `enphase_power` and `backup_soc` to `backup_reserve_pct`
in `enphase_config`.

### 4.2 MeterSummaryData

```protobuf
// proto/MeterSummaryData.proto
syntax = "proto3";

message MeterSummaryData {
  MeterChannel pv = 1;             // Solar production
  MeterChannel storage = 2;        // Battery (positive = charging)
  MeterChannel grid = 3;           // Grid import/export
  MeterChannel load = 4;           // House consumption
  MeterSumGridState grid_relay = 5;// Grid relay state
  int32 soc = 6;                   // Actual battery charge (0-100%)
  MeterChannel generator = 7;     // Generator
  MeterSumGridState gen_relay = 8; // Generator relay state
  uint32 phase_count = 9;         // Number of electrical phases (1 or 2 or 3)
  bool is_split_phase = 10;       // True for North American 120/240V split-phase
  GridToggleChannel grid_toggle_check = 14;  // Grid outage detection
}
```

Note: field numbers 11-13 are absent, suggesting reserved or removed fields.
Field 14 (`grid_toggle_check`) skips to 14.

### 4.3 MeterChannel

```protobuf
message MeterChannel {
  int32 agg_p_mw = 1;             // Aggregate active power in MILLIWATTS
  int32 agg_s_mva = 2;            // Aggregate apparent power in MILLIVOLT-AMPS
  repeated int32 agg_p_ph_mw = 3; // Per-phase active power [L1, L2, ...] in mW
  repeated int32 agg_s_ph_mva = 4;// Per-phase apparent power [L1, L2, ...] in mVA
}
```

**Unit conversion:** All power values are transmitted in milliwatts (mW) or
millivolt-amps (mVA). Divide by 1000 to get watts (W) or volt-amps (VA):

```python
result["pv_power_w"] = meters.pv.agg_p_mw / 1000.0
result["pv_apparent_va"] = meters.pv.agg_s_mva / 1000.0
```

The per-phase arrays contain one entry per electrical phase (typically 2 for
North American split-phase systems). Index 0 = L1, index 1 = L2, etc.

### 4.4 GridToggleChannel

```protobuf
message GridToggleChannel {
  bool update_ongoing = 1;         // Grid toggle update in progress
  bool grid_outage_status = 2;     // True if grid outage detected
  int32 min_essential_start_time = 3;
  int32 max_essential_end_time = 4;
}
```

This sub-message is conditionally present (`HasField("grid_toggle_check")`).
When present, it indicates active grid state transitions or outage conditions.

### 4.5 Supporting Messages

```protobuf
message DryContactStatus {
  DryContactId id = 1;             // Which dry contact relay
  DryContactRelayState state = 2;  // Current relay state
}

message DryContactName {
  DryContactId id = 1;             // Which dry contact relay
  string load_name = 2;            // User-assigned load name
}

message LoadStatus {
  string id = 1;                   // Load identifier
  string relay_status = 2;         // Relay state as string
  float power = 3;                 // Current power draw (watts)
}

message PowerMatchStatus {
  bool status = 1;                 // Power match active
  uint32 totalPCUCount = 2;       // Total microinverters (PCU = Power Conditioning Unit)
  uint32 runningPCUCount = 3;     // Currently producing microinverters
  bool isSupported = 4;           // System supports power match
}
```

### 4.6 Enums

#### BattMode

| Name                     | Value | Description                              |
|--------------------------|-------|------------------------------------------|
| `BATT_MODE_FULL_BACKUP`  | 0     | Full backup -- battery reserved for outages |
| `BATT_MODE_SELF_CONS`    | 1     | Self-consumption -- minimize grid usage   |
| `BATT_MODE_SAVINGS`      | 2     | Savings mode -- optimize for TOU rates    |
| `BATT_MODE_UNKNOWN`      | -1    | Unknown/unset                             |

#### MeterSumGridState

| Name                                       | Value | Description                            |
|--------------------------------------------|-------|----------------------------------------|
| `OPER_RELAY_UNKNOWN`                        | 0     | Unknown state                          |
| `OPER_RELAY_OPEN`                           | 1     | Grid relay open (islanded)             |
| `OPER_RELAY_CLOSED`                         | 2     | Grid relay closed (grid-tied)          |
| `OPER_RELAY_OFFGRID_AC_GRID_PRESENT`        | 3     | Off-grid but AC grid detected          |
| `OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD`   | 4     | Off-grid, ready to reconnect           |
| `OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID`  | 5     | Waiting to initialize on grid          |
| `OPER_RELAY_GEN_OPEN`                       | 6     | Generator relay open                   |
| `OPER_RELAY_GEN_CLOSED`                     | 7     | Generator relay closed                 |
| `OPER_RELAY_GEN_STARTUP`                    | 8     | Generator starting up                  |
| `OPER_RELAY_GEN_SYNC_READY`                 | 9     | Generator sync ready                   |
| `OPER_RELAY_GEN_AC_STABLE`                  | 10    | Generator AC output stable             |
| `OPER_RELAY_GEN_AC_UNSTABLE`                | 11    | Generator AC output unstable           |

Used for both `grid_relay` and `gen_relay` fields.

#### DryContactId

| Name | Value | Description                    |
|------|-------|--------------------------------|
| `NC1` | 0    | Normally-closed contact 1      |
| `NC2` | 1    | Normally-closed contact 2      |
| `NO1` | 2    | Normally-open contact 1        |
| `NO2` | 3    | Normally-open contact 2        |

#### DryContactRelayState

| Name                      | Value | Description           |
|---------------------------|-------|-----------------------|
| `DC_RELAY_STATE_INVALID`  | 0     | Invalid/unknown state |
| `DC_RELAY_OFF`            | 1     | Relay off             |
| `DC_RELAY_ON`             | 2     | Relay on              |

### 4.7 HemsStreamMessage (supplementary)

```protobuf
// proto/HemsStreamMessage.proto
syntax = "proto3";

import "google/protobuf/timestamp.proto";

message HemsStreamMessage {
  string asset_id = 1;
  google.protobuf.Timestamp timestamp = 2;
  map<string, Value> metrics = 3;
}

message Value {
  oneof kind {
    double number_value = 1;
    string string_value = 2;
    bool bool_value = 3;
  }
}
```

This message type is imported by `DataMsg.proto` but does not appear in the
`DataMsg` message fields. It may be used on other MQTT topics or reserved for
future use. The current collector does not decode `HemsStreamMessage`.

---

## 5. Field Mapping

The `on_mqtt_data` handler in `enphase_telegraf.py` transforms the decoded
protobuf dict into InfluxDB line protocol. Three measurements are emitted:
`enphase_power` (continuous), `enphase_config` (on state change),
and `enphase_dry_contact` (on state change).

### 5.1 POWER_MAP -- Aggregate Active Power

Maps protobuf field names to user-friendly InfluxDB field names:

| Protobuf Key         | InfluxDB Field   | Unit | Source                     |
|----------------------|------------------|------|----------------------------|
| `pv_power_w`         | `solar_w`        | W    | `meters.pv.agg_p_mw / 1000` |
| `grid_power_w`       | `grid_w`         | W    | `meters.grid.agg_p_mw / 1000` |
| `load_power_w`       | `consumption_w`  | W    | `meters.load.agg_p_mw / 1000` |
| `storage_power_w`    | `battery_w`      | W    | `meters.storage.agg_p_mw / 1000` |
| `generator_power_w`  | `generator_w`    | W    | `meters.generator.agg_p_mw / 1000` |

### 5.2 VA_MAP -- Aggregate Apparent Power

| Protobuf Key            | InfluxDB Field     | Unit | Source                        |
|-------------------------|--------------------|------|-------------------------------|
| `pv_apparent_va`        | `solar_va`         | VA   | `meters.pv.agg_s_mva / 1000` |
| `grid_apparent_va`      | `grid_va`          | VA   | `meters.grid.agg_s_mva / 1000` |
| `load_apparent_va`      | `consumption_va`   | VA   | `meters.load.agg_s_mva / 1000` |
| `storage_apparent_va`   | `battery_va`       | VA   | `meters.storage.agg_s_mva / 1000` |
| `generator_apparent_va` | `generator_va`     | VA   | `meters.generator.agg_s_mva / 1000` |

### 5.3 Per-Phase Extraction

Per-phase power is extracted from the `agg_p_ph_mw` and `agg_s_ph_mva`
repeated fields. The array index maps to the phase number (L1, L2, L3):

```python
PHASE_PREFIX_MAP = {
    "pv": "solar", "grid": "grid", "load": "consumption",
    "storage": "battery", "generator": "generator",
}

# Active power per phase:
#   msg["pv_phase_w"][0]  -->  solar_l1_w
#   msg["pv_phase_w"][1]  -->  solar_l2_w
#   msg["grid_phase_w"][0] --> grid_l1_w
#   ...

# Apparent power per phase:
#   msg["pv_phase_va"][0]  --> solar_l1_va
#   msg["pv_phase_va"][1]  --> solar_l2_va
#   ...
```

The naming pattern is `{out_prefix}_l{phase_number}_w` for active power
and `{out_prefix}_l{phase_number}_va` for apparent power, where phase
numbers are 1-indexed.

### 5.4 Additional Power Fields

| Protobuf Key            | InfluxDB Field          | Type  | Notes                        |
|-------------------------|-------------------------|-------|------------------------------|
| `meter_soc`             | `soc`                   | int   | 0-100%, validated range      |
| `pcu_total`             | `inverters_total`       | int   | Total microinverter count    |
| `pcu_running`           | `inverters_producing`   | int   | Currently producing count    |
| `grid_update_ongoing`   | `grid_update_ongoing`   | int   | 0 or 1                      |
| `grid_outage_status`    | `grid_outage`           | int   | 0 or 1                      |
| `protocol_ver`          | `protocol_ver`          | int   | Protobuf schema version     |

All power fields are emitted to the `enphase_power` measurement with tags
`serial={gateway_serial}` and `source=mqtt`.

### 5.5 State Change Detection

Configuration and relay state fields are emitted to `enphase_config` only
when their value changes from the previous message. This prevents writing
identical rows every second for slowly-changing state:

```python
# Battery mode -- emitted only on change
if batt_mode and batt_mode != _last_batt_mode:
    _last_batt_mode = batt_mode
    emit("enphase_config", {"serial": _serial}, {
        "battery_mode": _enum_int(BATT_MODE_MAP, batt_mode),    # int
        "battery_mode_str": batt_mode,                           # string
        "backup_reserve_pct": int(backup_soc),                   # int (0-100)
    })

# Grid relay -- emitted only on change
if grid_relay and grid_relay != _last_grid_relay:
    _last_grid_relay = grid_relay
    emit("enphase_config", {"serial": _serial}, {
        "grid_relay": _enum_int(GRID_RELAY_MAP, grid_relay),
        "grid_relay_str": grid_relay,
    })

# Generator relay -- emitted only on change
if gen_relay and gen_relay != _last_gen_relay:
    _last_gen_relay = gen_relay
    emit("enphase_config", {"serial": _serial}, {
        "gen_relay": _enum_int(GRID_RELAY_MAP, gen_relay),
        "gen_relay_str": gen_relay,
    })
```

Both integer and string representations are stored for each enum. The integer
form enables efficient Flux/InfluxQL filtering; the string form provides
human-readable dashboards. Unknown enum values map to `-1`.

Dry contact states follow the same pattern -- emitted to `enphase_dry_contact`
only when a contact's state differs from the last seen value:

```python
for dc in msg.get("dry_contacts", []):
    dc_id = dc["id"]             # e.g., "NC1"
    dc_state_str = dc["state"]   # e.g., "DC_RELAY_ON"
    dc_state_int = DRY_CONTACT_STATE_MAP.get(dc_state_str, -1)
    if _last_dry_contacts.get(dc_id) != dc_state_int:
        _last_dry_contacts[dc_id] = dc_state_int
        emit("enphase_dry_contact", {"serial": _serial, "contact": dc_id}, {
            "state": dc_state_int,
            "state_str": dc_state_str,
        })
```

### 5.6 Anomaly Detection

The collector applies sanity thresholds before writing power values. Values
exceeding these thresholds are dropped and an `enphase_error` point is emitted
with the `data_quality` component.

**Aggregate power threshold: 100 kW**

```python
for proto_key, field_name in POWER_MAP:
    val = msg.get(proto_key)
    fval = float(val)
    if abs(fval) > 100_000:          # > 100 kW
        anomalies[f"bad_{field_name}"] = True
    else:
        fields[field_name] = fval
```

**Per-phase power threshold: 50 kW**

```python
for i, pw in enumerate(phases):
    fval = float(pw)
    if abs(fval) > 50_000:           # > 50 kW per phase
        anomalies[f"bad_{out_prefix}_l{i+1}"] = True
    else:
        fields[f"{out_prefix}_l{i+1}_w"] = fval
```

**SOC range validation: 0-100%**

```python
soc_int = int(meter_soc)
if 0 <= soc_int <= 100:
    fields["soc"] = soc_int
else:
    anomalies["bad_soc"] = True
```

**Phase sum consistency check:**

For solar and consumption, if both the aggregate and per-phase values are
present, the collector checks whether L1 + L2 is within 50% of the aggregate
(when the phase sum exceeds 10W). A mismatch flags a `bad_{prefix}_phase_sum`
anomaly:

```python
for out_prefix in ("solar", "consumption"):
    agg = fields.get(f"{out_prefix}_w")
    l1 = fields.get(f"{out_prefix}_l1_w")
    l2 = fields.get(f"{out_prefix}_l2_w")
    if agg is not None and l1 is not None and l2 is not None:
        phase_sum = l1 + l2
        if abs(phase_sum) > 10 and abs(agg - phase_sum) / abs(phase_sum) > 0.5:
            anomalies[f"bad_{out_prefix}_phase_sum"] = True
```

All detected anomalies for a given message are aggregated and emitted as a
single error point:

```
enphase_error,serial=122312345678,component=data_quality message="Anomalies: bad_solar_w,bad_soc"
```

---

## 6. Schema Validation

The collector continuously monitors the protobuf stream for schema changes.
This catches Enphase firmware updates that add, remove, or modify protobuf
fields without notice.

### 6.1 Protocol Version Check

The expected protocol version is hardcoded as `1`. If the received
`protocol_ver` differs, an error is emitted:

```python
_expected_protocol_ver = 1

if proto_ver is not None and proto_ver != _expected_protocol_ver:
    emit_error("proto_version",
               f"protocol_ver changed from {_expected_protocol_ver} to {proto_ver}")
    emit("enphase_error", {"serial": _serial, "component": "proto_version"}, {
        "proto_version_mismatch": 1,
        "expected": _expected_protocol_ver,
        "actual": int(proto_ver),
    })
```

### 6.2 Field Presence Tracking

The `_fields_present` set (populated by `msg.ListFields()` during protobuf
decode) is compared against a baseline established from the first message.
New or missing fields trigger alerts:

```python
# First message: establish baseline
if _known_fields is None:
    _known_fields = set(fields_present)

# Subsequent messages: detect changes
new_fields = fields_present - _known_fields
missing_fields = _known_fields - fields_present

if new_fields:
    _known_fields |= new_fields
    emit_error("proto_new_fields",
               f"New protobuf fields: {','.join(sorted(new_fields))}")

if missing_fields:
    emit_error("proto_missing_fields",
               f"Missing protobuf fields: {','.join(sorted(missing_fields))}")
```

Note: the baseline is updated when new fields appear (`_known_fields |= new_fields`),
so a new field only triggers one alert. Missing fields are reported each time
they are absent (subject to the error rate limiter).

### 6.3 Unknown Enum Detection

Enum string values are validated against the known maps. An unknown value
(one not present in `BATT_MODE_MAP` or `GRID_RELAY_MAP`) triggers a one-time
alert:

```python
for field_name, enum_map in [("batt_mode", BATT_MODE_MAP),
                              ("grid_relay", GRID_RELAY_MAP),
                              ("gen_relay", GRID_RELAY_MAP)]:
    val = msg.get(field_name)
    if val and val not in enum_map and val not in _unknown_enums_seen:
        _unknown_enums_seen.add(val)
        emit_error("proto_unknown_enum", f"Unknown {field_name}: {val}")
```

Unknown enums are tracked in `_unknown_enums_seen` to avoid repeated alerts.
The enum is still stored as its string name (via `_enum_name`) and mapped to
`-1` in the integer representation.

### 6.4 Error Rate Limiting

All schema validation errors pass through a rate limiter with exponential
backoff. The first occurrence is always emitted. Subsequent identical errors
are suppressed until the backoff interval expires, starting at 60 seconds and
doubling up to a maximum of 3600 seconds (1 hour):

```python
_error_backoff[component] = {"last_emit": now, "interval": 60}
# Next emission: after 60s, then 120s, 240s, ..., up to 3600s
```

---

## Appendix: Message Flow Summary

```
Enlighten API                  AWS IoT Core                  Collector
    |                              |                              |
    |<-- GET livestream.json ------|                              |
    |--- JWT + topic ------------->|                              |
    |<-- GET mqttSignedUrl --------|                              |
    |--- response topic ---------->|                              |
    |                              |<-- MQTT CONNECT (WSS:443) ---|
    |                              |    username = auth params     |
    |                              |    client_id = em-paho-mqtt-* |
    |                              |--- CONNACK ------------------>|
    |                              |<-- SUBSCRIBE (QoS 1) --------|
    |                              |    live_stream_topic          |
    |                              |    response_stream_topic      |
    |                              |                               |
    |                              |--- DataMsg (protobuf, ~1Hz)->|
    |                              |--- DataMsg ----------------->|  --> decode
    |                              |--- DataMsg ----------------->|  --> emit line protocol
    |                              |    ...                        |
    |                              |    (840s elapsed)             |
    |                              |<-- DISCONNECT ---------------|
    |                              |    (2s gap)                   |
    |<-- GET livestream.json ------|                               |
    |--- fresh JWT + topic ------->|                               |
    |                              |<-- MQTT CONNECT -------------|
    |                              |    (cycle repeats)            |
```
