# Architecture

How enphase-telegraf works internally: data flow, threading model, error
handling, and resilience design.

## Data flow

```
                        Enphase Cloud
                    +------------------+
                    |  AWS IoT MQTT    |  ~1 msg/sec, protobuf
                    |  (WebSocket)     |--+
                    +------------------+  |
                                          |
                    +------------------+  |    +------------------+
                    |  Enlighten REST  |  |    | enphase-telegraf |
                    |  (20 endpoints)  |--+--->|                  |
                    +------------------+       | MQTT thread      |
                                               | Cloud poll thread|----> stdout
                                               | Status thread    |      (line protocol)
                                               +------------------+
                                                        |
                                                        v
                                               +------------------+
                                               |    Telegraf       |
                                               |  (execd input)   |
                                               +------------------+
                                                        |
                                                        v
                                               +------------------+
                                               |    InfluxDB       |
                                               +------------------+
```

The collector runs as a long-lived process. Telegraf launches it via `execd`
and reads its stdout. The process runs three concurrent threads that all write
to a shared stdout (protected by a lock).

## Threading model

```
main thread
  |
  +-- MQTT thread (livestream.py)
  |     Connects to AWS IoT via WebSocket
  |     Receives protobuf DataMsg at ~1Hz
  |     Calls on_mqtt_data() which calls emit()
  |     Auto-reconnects every 14 minutes
  |
  +-- Cloud poll thread (enphase_telegraf.py: cloud_poll_loop)
  |     Sleeps 30s between cycles
  |     Each cycle checks all 9 scheduled endpoints
  |     Fetches only endpoints whose interval has elapsed
  |     Calls emit() for each endpoint's parsed data
  |
  +-- Status thread (enphase_telegraf.py: status_loop)
        Every 30s, emits enphase_status heartbeat
        Reports: uptime, MQTT state, cloud state, error counts
```

All three threads call `emit()` which acquires `_stdout_lock` before writing.
This guarantees every line on stdout is a complete, non-interleaved line
protocol entry.

## Output: line protocol

Every data point is one line of InfluxDB line protocol on stdout:

```
measurement,tag=value,tag=value field=value,field=value timestamp_ns
```

The `emit()` function handles:
- Tag escaping (space, comma, equals, backslash, newline/CR stripped)
- Field formatting (int → `42i`, float → `3.14`, string → `"quoted"`, bool → `1i` + `_str`)
- NaN/Infinity filtering (silently dropped — InfluxDB rejects them)
- Nanosecond timestamps (auto-generated from `time.time()` if not provided)
- Thread-safe stdout writes (single lock)

## Cloud polling schedule

Each cloud endpoint is polled at a fixed interval. Fetches that fail are
retried 60 seconds later (not at the full interval).

| Endpoint | Interval | Measurement(s) |
|----------|----------|-----------------|
| latest_power | 2 min | enphase_power |
| battery_status | 2 min | enphase_battery |
| today | 5 min | enphase_energy, enphase_battery, enphase_config, enphase_gateway |
| events | 5 min | enphase_error |
| alarms | 10 min | enphase_gateway |
| battery_schedules | 10 min | (parsed but not emitted) |
| inverters | 30 min | enphase_inverters |
| devices | 1 hour | (used for serial discovery) |
| site_data | 1 hour | enphase_energy, enphase_gateway |

## Error handling

### Exponential backoff

Errors are rate-limited per component to prevent log spam. Each component
(e.g., `cloud_latest_power`, `mqtt`, `auth`) has independent backoff:

```
First error:  emit immediately, set interval = 60s
After 60s:    emit again, set interval = 120s
After 120s:   emit again, set interval = 240s
...
Cap:          interval maxes out at 3600s (1 hour)
On success:   clear backoff, next error emits immediately
```

The `enphase_error` measurement tracks active problems:

```
enphase_error,serial=X,component=cloud_today message="ConnectionError: ...",next_retry_s=240i
```

### Authentication resilience

- Session TTL is 3600s (1 hour). The client re-authenticates transparently.
- Auth failures use a separate backoff: 10s, 20s, 40s, ... up to 600s (10 min).
- MFA-enabled accounts are detected and raise a clear error (MFA must be disabled).

### MQTT resilience

- Sessions last 15 minutes (AWS IoT limit).
- The client reconnects at 14 minutes (1 minute before expiry).
- 2-second gap between disconnect and reconnect.
- 30-second backoff after a failed connection.
- If no data arrives for 300 seconds, the connection is considered stale and restarted.

### Cloud API resilience

- Failed fetches set `_cloud_last_fetch[endpoint] = now - interval + 60`, scheduling a retry in 60s.
- Each endpoint fails independently — a broken `battery_status` endpoint doesn't affect `today`.
- 0.5s delay between consecutive cloud requests (respectful rate limiting).
- 15-second HTTP timeout per request.

## Schema validation

The collector detects Enphase protobuf schema changes at runtime:

1. **Protocol version** — `DataMsg.protocol_ver` is tracked. A change emits an error.
2. **Field presence** — The first message establishes a baseline of present fields. Subsequent messages report new or missing fields.
3. **Unknown enums** — `batt_mode`, `grid_relay`, `gen_relay` values not in the known map are flagged (once each).

This is important because Enphase can change their protobuf schema at any time.
Schema changes appear as `enphase_error` measurements with components like
`proto_version`, `proto_new_fields`, `proto_missing_fields`, `proto_unknown_enum`.

## State change detection

Some data is only emitted when it changes (to avoid redundant writes):

- **Battery mode** — `enphase_config` emitted only when `batt_mode` differs from last known value
- **Grid relay** — same (open/closed/islanded transitions)
- **Generator relay** — same
- **Backup reserve %** — emitted only when `batteryConfig.battery_backup_percentage` changes
- **Dry contacts** — each contact (NC1, NC2, NO1, NO2) tracked independently, emitted on state change

## Anomaly detection

Power values are sanity-checked before emission:

| Threshold | Action |
|-----------|--------|
| Aggregate power > 100,000 W | Flag as `data_quality` error |
| Per-phase power > 50,000 W | Flag as `data_quality` error |
| SOC outside 0-100% | Flag, do not emit |
| Phase sum diverges > 50% from aggregate | Flag as `data_quality` warning |

Flagged values still emit (for visibility) but also trigger `enphase_error`
measurements so dashboards can alert on data quality issues.

## Signal handling

The process handles SIGTERM and SIGINT for graceful shutdown:
- Sets `_running = False` (stops all loops)
- Calls `stream.stop()` to disconnect MQTT cleanly
- Threads join within a few seconds
- Exit code 0

This is important for Telegraf's `execd` plugin — it sends SIGTERM when
restarting and expects the process to exit cleanly.

## History backfill

`bin/load-history` provides a separate pipeline for historical data:

```
Enlighten today.json API  -->  JSON cache files  -->  line protocol  -->  InfluxDB
(1 request per day)           (.cache/history/)      (convert_day)       (HTTP API)
```

Historical data uses `source=history` and `source=history_daily` tags to
distinguish from live data. This means you can query both together:

```flux
from(bucket: "enphase")
|> filter(fn: (r) => r.source =~ /mqtt|cloud|history/)
```

The download is resumable — progress is tracked in a JSON file and cached days
are skipped on restart. Rate limiting: 1 request per 30 seconds (configurable).
