# Measurement Types — Rosetta Stone

Every InfluxDB measurement and field explained: what it means physically, where
it comes from, and what the values look like. You should be able to browse
InfluxDB with just this document.

All measurements use the `enphase_` prefix. All use the `serial` tag (gateway
serial number) as the primary key. The `source` tag tells you where the data
came from.

---

## Three data sources

| Source tag | Protocol | Update rate | What it gives you | Required? |
|-----------|----------|------------|-------------------|-----------|
| `mqtt` | AWS IoT WebSocket → protobuf | ~1 msg/sec | Real-time power, SOC, relay states, dry contacts, inverter counts | Default on |
| `cloud` | Enlighten cloud API (20 endpoints) | 2–120 min | Daily energy, battery health, device inventory, config | Default on |
| `local` | HTTPS to IQ Gateway on LAN | 1–60 sec | SSE meter stream, per-inverter data, CT readings, grid relay, tariff | Opt-in |
| `bus` | Direct CAN / RS485 / Zigbee | ~10 ms | Same as local but bypasses gateway HTTP — higher resolution, no deadlock risk | Future |

Most users run **mqtt + cloud** only. Local gateway adds deeper detail but
requires LAN access and respects strict rate limits (~6 req/min).

### How values flow

```
MQTT protobuf (1/sec)       ─┐
Cloud API response (2-120m)  ─┤──→  enphase-mqtt.py  ──→  stdout (line protocol)  ──→  Telegraf  ──→  InfluxDB
Local gateway poll (1-60m)   ─┘
```

---

# Primary Measurements

These are the measurements you'll query most. They cover power, energy, battery,
configuration, and system health.

---

## enphase_power

**The main event — instantaneous power flowing through your system right now.**

How many watts are your panels making? How much is the grid supplying? Is the
battery charging or discharging? This measurement answers all of that.

**Update rate:** ~1/sec (mqtt), ~2 min (cloud), ~1 sec (local SSE)
**Tags:** `serial`, `source` (mqtt | cloud | local)

### Core power (watts)

| Field | Unit | Sign | Sources | What it means |
|-------|------|------|---------|---------------|
| `solar_w` | W | ≥ 0 | mqtt, cloud, local | **Solar panel output right now.** 0 at night, peaks at solar noon. Typical: 0–10,000 W. |
| `grid_w` | W | + = buying, − = selling | mqtt, cloud, local | **Power to/from the utility.** Positive = importing (costs $). Negative = exporting surplus (earns credits). |
| `consumption_w` | W | ≥ 0 | mqtt, cloud, local | **Total home power draw.** Everything plugged in. Should ≈ solar + grid + battery. |
| `battery_w` | W | + = discharging, − = charging | mqtt, cloud, local | **Battery power flow.** Positive = powering home. Negative = absorbing solar/grid. |
| `generator_w` | W | ≥ 0 | mqtt | **Generator output** (if installed). 0 if no generator. |

**Energy balance:** `consumption_w ≈ solar_w + grid_w + battery_w + generator_w`

### Apparent power (volt-amps)

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `solar_va` | VA | mqtt | Apparent power from solar (real + reactive combined). |
| `grid_va` | VA | mqtt | Apparent power from grid. |
| `consumption_va` | VA | mqtt | Apparent power of home loads. |
| `battery_va` | VA | mqtt | Apparent power from battery. |
| `generator_va` | VA | mqtt | Apparent power from generator. |

> **Real vs. apparent power:** AC motors (fridge, HVAC) draw more current than
> their wattage alone because current and voltage get out of phase. Real power
> (`_w`) does useful work and is what you pay for. Apparent power (`_va`) is
> what the wires carry. The ratio is power factor: `W / VA = PF`. Typical
> homes: 0.85–0.95.

### Per-phase power (split-phase L1/L2)

US residential is 240V split-phase: two 120V legs. Large appliances (dryer,
oven, HVAC) use both legs. Small appliances use one.

| Field | Unit | Sources |
|-------|------|---------|
| `solar_l1_w`, `solar_l2_w` | W | mqtt |
| `grid_l1_w`, `grid_l2_w` | W | mqtt |
| `consumption_l1_w`, `consumption_l2_w` | W | mqtt |
| `battery_l1_w`, `battery_l2_w` | W | mqtt |
| `solar_l1_va` … `battery_l2_va` | VA | mqtt |

### Battery charge level

| Field | Unit | Range | Sources | What it means |
|-------|------|-------|---------|---------------|
| `soc` | % | 0–100 | mqtt, cloud | **State of charge** — how full the battery is. 100% = full. Won't discharge below the backup reserve (typically 10–30%). |

### Microinverter counts

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `inverters_total` | count | mqtt | Total microinverters installed (one per panel). |
| `inverters_producing` | count | mqtt | How many are producing now. 0 at night. Less than total during daytime = shading/issues. |

### Grid outage detection

| Field | Values | Sources | What it means |
|-------|--------|---------|---------------|
| `grid_outage` | 0/1 | mqtt | **1 = grid is down.** System islanded on battery + solar. |
| `grid_update_ongoing` | 0/1 | mqtt | 1 = grid relay state change in progress. Transient. |

### Cloud-only fields (source=cloud)

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `available_energy_kwh` | kWh | cloud | Usable energy remaining in battery. |
| `max_capacity_kwh` | kWh | cloud | Total battery capacity (e.g., 10.0 for two IQ Battery 5P). |

### Metadata

| Field | Values | Sources | What it means |
|-------|--------|---------|---------------|
| `protocol_ver` | int (currently 1) | mqtt | Protobuf schema version. If this changes, check `enphase_error`. |

### Source-specific raw field names

| Proto/API field (raw) | → Unified field | Conversion |
|-----------------------|----------------|------------|
| `pv_power_w` | `solar_w` | Direct (already in watts) |
| `grid_power_w` | `grid_w` | Direct |
| `load_power_w` | `consumption_w` | Direct |
| `storage_power_w` | `battery_w` | Direct |
| `generator_power_w` | `generator_w` | Direct |
| `MeterSummaryData.pv.agg_p_mw` | `solar_w` | ÷ 1000 (milliwatts → watts) |
| `meter_soc` | `soc` | Direct (this is the ACTUAL charge level) |
| `backup_soc` (DataMsg field 5) | → `enphase_config.backup_reserve_pct` | This is the SETTING, not the charge |
| SSE `production` ph-a/ph-b `p` | `solar_w` | Sum of phases per meter_type |
| SSE `net-consumption` `p` | `grid_w` | Sum of phases |
| SSE `total-consumption` `p` | `consumption_w` | Sum of phases |
| `/production.json` `wNow` (type=eim) | `solar_w` | Direct |

---

## enphase_energy

**Cumulative energy totals — how many watt-hours flowed today and over the
system lifetime.** Like an odometer vs. the speedometer in `enphase_power`.

**Update rate:** 5 min (today endpoint), 60 min (site_data)
**Tags:** `serial`

### Daily totals (reset at midnight)

| Field | Unit | Source | What it means |
|-------|------|--------|---------------|
| `production_wh` | Wh | cloud (stats[0].totals.production) | Total solar energy generated today. |
| `consumption_wh` | Wh | cloud (stats[0].totals.consumption) | Total energy consumed today. |

### Energy flow breakdown — where did each watt-hour go?

| Field | Unit | Source | What it means |
|-------|------|--------|---------------|
| `solar_to_home_wh` | Wh | cloud (totals.solar_home) | Solar consumed directly by home (free!). |
| `solar_to_battery_wh` | Wh | cloud (totals.solar_battery) | Solar stored in battery for later. |
| `solar_to_grid_wh` | Wh | cloud (totals.solar_grid) | Solar exported to grid (earns credits). |
| `battery_to_home_wh` | Wh | cloud (totals.battery_home) | Battery energy powering home. |
| `battery_to_grid_wh` | Wh | cloud (totals.battery_grid) | Battery exported to grid (rare, TOU arbitrage). |
| `grid_to_home_wh` | Wh | cloud (totals.grid_home) | Grid energy consumed (costs money). |
| `grid_to_battery_wh` | Wh | cloud (totals.grid_battery) | Grid energy charging battery (costs money). |
| `charge_wh` | Wh | cloud (totals.charge) | Total into battery (= solar_to_battery + grid_to_battery). |
| `discharge_wh` | Wh | cloud (totals.discharge) | Total out of battery (= battery_to_home + battery_to_grid). |

### Lifetime totals (never reset)

| Field | Unit | Source | What it means |
|-------|------|--------|---------------|
| `lifetime_production_wh` | Wh | cloud (site_data.module.lifetime.lifetimeEnergy.value) | Total solar since installation. |
| `lifetime_consumption_wh` | Wh | cloud (site_data.module.lifetime.lifetimeEnergy.consumed) | Total consumption since installation. |

---

## enphase_battery

**Battery system health, capacity, and degradation.** Goes beyond the simple SOC
in `enphase_power` to show cycle count, state of health, temperature, and
estimated backup time.

**Update rate:** 2 min (cloud), 2 min (local)
**Tags:** `serial`, `source` (cloud | local)

### Charge and capacity

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `soc` | % | cloud, local | Battery charge level (same as enphase_power.soc, different source). |
| `available_energy_kwh` | kWh | cloud | Usable energy remaining (above the reserve). |
| `max_capacity_kwh` | kWh | cloud | Total capacity. Decreases slowly over years. |
| `available_power_kw` | kW | cloud | Max discharge rate right now (limited by temp, SOC). |
| `max_power_kw` | kW | cloud | Rated max discharge (e.g., 3.84 kW per IQ Battery 5P). |
| `agg_avail_energy_wh` | Wh | local | Same as available_energy_kwh but in Wh (from gateway). |
| `agg_backup_energy_wh` | Wh | local | Energy reserved for backup. |
| `max_energy_wh` | Wh | local | Total capacity in Wh (e.g., 10,080 for 2× IQ Battery 5P). |
| `configured_backup_soc` | % | local | Backup reserve setting (from gateway, not cloud). |
| `real_power_w` | W | local | Current charge/discharge rate. + = discharging, − = charging. |
| `apparent_power_va` | VA | local | Apparent charge/discharge power. |
| `device_count` | count | local | Number of battery units. |

### Backup time

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `estimated_backup_min` | min | cloud | How long battery can power your home if grid fails right now. |
| `last_24h_consumption_kwh` | kWh | cloud | Home energy use last 24 hours (used to calculate backup time). |

### Fleet health

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `unit_count` | count | cloud | Number of battery units (e.g., 2 for two IQ Battery 5P). |
| `active_inverters` | count | cloud | Microinverters currently communicating. |
| `total_inverters` | count | cloud | Total microinverters in system. |

### Per-unit degradation (numbered 1–4)

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `cycle_count_1` … `cycle_count_4` | count | cloud | **Charge cycles on each battery.** One cycle = full discharge + recharge. LFP batteries last 4,000+. |
| `soh_1` … `soh_4` | % | cloud | **State of health.** 100% = new. Decreases ~1–2%/year. Below 80% = consider replacement. |

### Grid state (from local gateway)

| Field | Values | Sources | What it means |
|-------|--------|---------|---------------|
| `mains_oper_state` | string | local | "closed" = on-grid, "open" = off-grid. |
| `grid_mode` | string | local | "multimode-ongrid", "grid-tied", etc. |

---

## enphase_config

**System operating mode and settings.** Only written when a value **changes**
(not every second). Tracks battery mode, grid relay state, backup reserve, and
charge-from-grid settings.

**Update rate:** On change only
**Tags:** `serial`, `source` (mqtt | cloud | local)

### Battery operating mode

| Field | Type | Sources | What it means |
|-------|------|---------|---------------|
| `battery_mode` | int | mqtt | Current mode (see enum below). |
| `battery_mode_str` | string | mqtt | Protobuf enum name. |
| `usage_str` | string | cloud | Enlighten's name: "self-consumption", "cost_savings", "backup". |
| `mode` | string | local | Local gateway mode string: "self-consumption", "savings", etc. |

| `battery_mode` | Proto name | What it does |
|----------------|-----------|--------------|
| 0 | `BATT_MODE_FULL_BACKUP` | **Full backup** — holds all charge for outages. |
| 1 | `BATT_MODE_SELF_CONS` | **Self consumption** — solar first, battery second, grid last. |
| 2 | `BATT_MODE_SAVINGS` | **TOU savings** — charges when cheap, discharges when expensive. |
| -1 | *(unmapped)* | Unknown — new mode from firmware update. |

### Grid relay state

The grid relay physically connects/disconnects your home from the utility.

| Field | Type | Sources | What it means |
|-------|------|---------|---------------|
| `grid_relay` | int | mqtt | Grid relay position (see enum below). |
| `grid_relay_str` | string | mqtt | Protobuf enum name. |
| `gen_relay` | int | mqtt | Generator relay position (same enum). |
| `gen_relay_str` | string | mqtt | Generator relay enum name. |
| `mains_admin_state` | string/float | local | What the system **commanded** the relay to do. |
| `mains_oper_state` | string/float | local | What the relay **actually is** doing. |
| `der1_state` … `der3_state` | float | local | Individual DER relay states. |

| `grid_relay` | Proto name | What it means |
|-------------|-----------|---------------|
| 1 | `OPER_RELAY_OPEN` | **Off-grid (islanded).** Running on battery/solar only. |
| 2 | `OPER_RELAY_CLOSED` | **On-grid (normal).** Connected to utility. |
| 3 | `OPER_RELAY_OFFGRID_AC_GRID_PRESENT` | Off-grid but grid voltage detected. Waiting to verify stability. |
| 4 | `OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD` | Grid stable, ready to reconnect. |
| 5 | `OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID` | Reconnecting — syncing voltage/frequency. |
| 6–11 | `OPER_RELAY_GEN_*` | Generator relay states (open/closed/startup/sync/stable/unstable). |
| -1 | *(unmapped)* | Unknown. |

### Battery settings

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `backup_reserve_pct` | % | cloud, local | **Backup reserve** — battery won't discharge below this for daily use. Saved for outages. |
| `very_low_soc_pct` | % | cloud, local | Emergency shutoff threshold. Typically 5–10%. |
| `charge_from_grid` | 0/1 | cloud, local | **1 = battery can charge from grid** (costs money). Useful for TOU arbitrage. |
| `storm_guard` | 0/1 | cloud | **1 = storm guard on.** Auto-charges to 100% before severe weather. |

### Power export limits (local only)

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `pel_enabled` | 0/1 | local | Power export limiting is active. |
| `pel_limit_w` | W | local | Max export power in watts. |
| `pel_limit_pct` | % | local | Max export as % of rated capacity. |
| `zero_export` | 0/1 | local | 1 = no power may be exported to grid. |

### Charge schedule (local only)

| Field | Type | Sources | What it means |
|-------|------|---------|---------------|
| `schedule_enabled` | 0/1 | local | Charge/discharge scheduling is active. |
| `schedule_count` | count | local | Number of schedule entries. |

---

## enphase_dry_contact

**Load control relay states — physical switches that shed non-essential loads.**

### What are dry contacts?

Dry contacts are **relay outputs** built into the IQ System Controller. They
automatically control external equipment based on grid status, battery level,
or schedule.

**Common uses:**
- **Shed non-essential loads during outages:** Pool pump, EV charger, water
  heater wired through a relay. When grid fails, system disconnects them to
  conserve battery for essentials (fridge, lights, internet).
- **Generator control:** Signal a generator to start when SOC drops below
  a threshold.
- **Load prioritization:** Multiple contacts with different SOC thresholds
  shed loads in priority order.

**The four contacts:**

| Contact | Default state | Rating | Typical use |
|---------|--------------|--------|-------------|
| **NC1** | Normally Closed (on by default) | 120V / 3A | Non-essential load (shed during outage) |
| **NC2** | Normally Closed | 120V / 3A | Second non-essential load |
| **NO1** | Normally Open (off by default) | 240V / 3A | Generator start signal |
| **NO2** | Normally Open | 240V / 3A | Emergency load enable |

> **NC vs NO:** "Normally Closed" = circuit complete during normal operation,
> **opens** (disconnects load) during outage. "Normally Open" = opposite.

**Update rate:** On state change only
**Tags:** `serial`, `contact` (NC1 | NC2 | NO1 | NO2)

### State fields

| Field | Values | Sources | What it means |
|-------|--------|---------|---------------|
| `state` | 0, 1, -1 | mqtt | **0 = off, 1 = on, -1 = invalid.** For NC contacts: off = load disconnected. |
| `state_str` | string | mqtt | Protobuf enum: `DC_RELAY_OFF`, `DC_RELAY_ON`, `DC_RELAY_STATE_INVALID`. |

### Configuration fields (local gateway)

| Field | Values | Sources | What it means |
|-------|--------|---------|---------------|
| `status` | float | local | Relay state (0.0 = open, 1.0 = closed). |
| `type` | string | local | Contact type: "NONE" (unconfigured), "PV", "LOAD", "3RD-PV". |
| `load_name` | string | local | User-assigned name (e.g., "Pool Pump", "EV Charger"). |
| `grid_action` | string | local | What happens on grid failure: "apply", "shed", "schedule", "none". |
| `micro_grid_action` | string | local | What happens during islanding. |
| `gen_action` | string | local | What happens when generator runs. |
| `mode` | string | local | Control mode: "manual" or "soc" (automatic SOC-based). |
| `soc_high` | % | local | Upper SOC threshold — relay energizes above this. |
| `soc_low` | % | local | Lower SOC threshold — relay de-energizes below this. |
| `priority` | int | local | Shed order (lower = shed first). |

---

## enphase_gateway

**IQ Gateway connectivity, firmware, and fleet overview.**

**Update rate:** 5–60 min (cloud), 5–60 min (local)
**Tags:** `serial`, `source` (cloud | local)

### Connectivity (cloud)

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `wifi` | 0/1 | cloud | Gateway connected via WiFi. |
| `cellular` | 0/1 | cloud | Gateway has cellular backup. |
| `ethernet` | 0/1 | cloud | Gateway connected via Ethernet. |
| `status_str` | string | cloud | System status: "normal", "meter_issue", "comm", etc. |
| `alarm_count` | count | cloud | Active alarms (only emitted when > 0). |

### Fleet counts

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `microinverters` | count | cloud | Total microinverters in system. |
| `batteries` | count | cloud | Number of IQ Battery units. |

### Gateway identity (local)

| Field | Type | Sources | What it means |
|-------|------|---------|---------------|
| `serial_number` | string | local (/info.xml) | Gateway serial number. |
| `part_number` | string | local | Gateway model/part number. |
| `software_version` | string | local | Firmware version (e.g., "D8.3.5516"). |
| `build_id` | string | local | Build identifier. |

### Communication health (local)

| Field | Type | Sources | What it means |
|-------|------|---------|---------------|
| `web_comm` | 0/1 | local (/home.json) | Gateway can reach Enlighten cloud. |
| `devices_total` | count | local | Total devices communicating. |
| `devices_comm_level` | 0–100 | local | Overall wireless communication quality. |
| `primary_interface` | string | local | Primary network interface (eth0/wlan0). |

---

## enphase_inverters

**Microinverter fleet health summary.** One row for the whole fleet, not per-inverter.

A microinverter converts each solar panel's DC output to AC. Each panel has
its own, so if one fails only that panel goes offline.

**Update rate:** 30 min (cloud), 5 min (local)
**Tags:** `serial`, `source` (cloud | local)

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `total` | count | cloud, local | Total microinverters installed. |
| `not_reporting` | count | cloud | Inverters that haven't communicated recently. |
| `error_count` | count | cloud | Inverters reporting an error. |
| `warning_count` | count | cloud | Inverters reporting a warning. |
| `normal_count` | count | cloud | Inverters operating normally. |
| `producing` | count | local | Inverters currently producing power. |
| `not_producing` | count | local | Inverters not producing (night, shade, fault). |

**Alert rule:** `WHERE not_reporting > 0 OR error_count > 0`

---

## enphase_status

**Collector health heartbeat.** Tells you the data pipeline is running.

**Update rate:** Every 30 sec
**Tags:** `serial`

| Field | Unit | What it means |
|-------|------|---------------|
| `uptime_s` | seconds | Collector process uptime. |
| `mqtt_connected` | 0/1 | MQTT WebSocket connection alive. |
| `mqtt_msg_total` | count | Total MQTT messages since startup. |
| `mqtt_err_total` | count | Total MQTT errors since startup. |
| `cloud_ok` | 0/1 | Enlighten session authenticated. |
| `cloud_fetch_total` | count | Successful cloud API calls since startup. |
| `cloud_err_total` | count | Failed cloud API calls since startup. |
| `auth_err_total` | count | Authentication failures since startup. |

---

## enphase_error

**Problems that need attention.** Rate-limited with exponential backoff:
first immediately, then 60s → 120s → 240s → … → 3600s. Resets when cleared.

**Tags:** `serial`, `component`

| Field | What it means |
|-------|---------------|
| `message` | Human-readable error description. |
| `next_retry_s` | Seconds until re-emitted if still failing. |

### Component values

| Component | What it means |
|-----------|--------------|
| `auth` | Login failed — check email/password, or MFA enabled. |
| `mqtt` | MQTT disconnected or no data for 5+ minutes. |
| `cloud_*` | A specific cloud endpoint failed (e.g., `cloud_events`). |
| `discovery` | Could not find gateway serial from cloud data. |
| `proto_version` | Protobuf protocol_ver changed — Enphase schema update. |
| `proto_new_fields` | New fields in protobuf messages (informational). |
| `proto_missing_fields` | Expected fields disappeared from protobuf. |
| `proto_unknown_enum` | Unknown battery mode or grid relay value. |
| `data_quality` | Power or SOC outside expected range (>100kW or SOC out of 0–100). |

---

# Extended Measurements (local gateway)

These measurements only appear when the local gateway source is enabled. They
provide deeper device-level detail not available from cloud or MQTT.

---

## enphase_meter

**Detailed CT (Current Transformer) clamp readings.** Per-phase voltage,
current, frequency, and power factor — the raw electrical measurements from
the meter hardware.

**Update rate:** ~1/sec (SSE stream) or ~5 min (poll fallback)
**Tags:** `serial`, `meter_type`, `phase`

### Tag values

| Tag | Values | What it means |
|-----|--------|---------------|
| `meter_type` | `production` | Solar generation CT clamp. |
| | `net-consumption` | Grid CT clamp. + = importing, − = exporting. |
| | `total-consumption` | Total home load CT (production + net-consumption). |
| `phase` | `a` | Phase A (L1) — one 120V leg. |
| | `b` | Phase B (L2) — the other 120V leg. |

### Fields

| Field | Unit | What it means |
|-------|------|---------------|
| `power_w` | W | Active (real) power on this phase of this meter. |
| `voltage_v` | V RMS | Line voltage. Normal: 110–130V. |
| `current_a` | A RMS | Current draw. |
| `frequency_hz` | Hz | Grid frequency. Normal: 59.95–60.05 Hz. |
| `apparent_power_va` | VA | Total power demand (real + reactive). Always ≥ power_w. |
| `reactive_power_var` | VAR | Reactive power from motors/transformers. Doesn't do work. |
| `power_factor` | 0–1 | Real/apparent ratio. 1.0 = resistive (heater). 0.8 = lots of motors. |

### Aggregate meter fields (from /ivp/meters/readings, poll only)

| Field | Unit | What it means |
|-------|------|---------------|
| `active_power_w` | W | Total real power across all phases. |
| `wh_delivered` | Wh | Cumulative energy delivered through this meter (lifetime, never resets). |
| `wh_received` | Wh | Cumulative energy received (lifetime). |
| `phase_a_w`, `phase_b_w` | W | Per-phase active power. |
| `phase_a_voltage_v`, `phase_b_voltage_v` | V | Per-phase voltage. |
| `phase_a_current_a`, `phase_b_current_a` | A | Per-phase current. |

---

## enphase_inverter

**Per-microinverter production and status.** One row per panel, so you can
identify underperformers, shading issues, or dead units.

**Update rate:** 5 min (production), 5 min (status)
**Tags:** `serial`, `inverter_serial`

### Production

| Field | Unit | Sources | What it means |
|-------|------|---------|---------------|
| `last_report_watts` | W | local (/api/v1/production/inverters) | Current output. 0 at night. Typical peak: 300–400W. |
| `max_report_watts` | W | local | Highest output today. Compare across panels to spot issues. |
| `last_report_date` | ISO string | local | When this inverter last reported. Stale = possibly offline. |

### Status

| Field | Type | Sources | What it means |
|-------|------|---------|---------------|
| `admin_state` | int | local (/installer/agf/inverters_status.json) | 1=producing, 2=not producing (night), 6=disabled. |
| `admin_state_str` | string | local | Human-readable: "producing", "not_producing", "disabled". |
| `producing` | 0/1 | local | Is this inverter currently producing? |
| `phase` | string | local | Phase assignment (L1/L2 in split-phase). |

---

## enphase_battery_device

**Per-battery-unit hardware status.** Temperature, capacity, DC switch
position. For monitoring individual battery health.

**Update rate:** 2 min
**Tags:** `serial`, `battery_serial`

| Field | Unit | What it means |
|-------|------|---------------|
| `soc` | % | This unit's charge level. |
| `capacity_wh` | Wh | Rated capacity (e.g., 5,040 Wh for IQ Battery 5P). |
| `temperature_c` | °C | Pack temperature. Normal: 15–35°C. Throttles above 45°C. |
| `max_cell_temp_c` | °C | Hottest individual cell. If >50°C, output is throttling. |
| `led_status` | int | Physical LED indicator state. |
| `dc_switch_off` | 0/1 | DC disconnect switch. 1 = physically disconnected (maintenance). |

---

## enphase_device

**Per-device health and communication quality.** Covers microinverters,
batteries, gateways — any device in the system.

**Update rate:** 30 min (device_health), 60 min (inventory)
**Tags:** `serial`, `device_serial`, `device_type`

| Field | Type | What it means |
|-------|------|---------------|
| `producing` | 0/1 | Device currently producing power. |
| `communicating` | 0/1 | Device talking to gateway. |
| `comm_level` | float | Communication quality (0–5 for Zigbee). |
| `admin_state` | float | Configured state. |
| `oper_state` | float | Actual operating state. |
| `provisioned` | 0/1 | Device set up in the system. |
| `operating` | 0/1 | Device in normal operating mode. |
| `device_status` | string | Status text. |
| `part_number` | string | Model/part number. |

---

## enphase_generator

**Generator status (if installed).**

**Update rate:** 30 min
**Tags:** `serial`

| Field | Type | What it means |
|-------|------|---------------|
| `running` | 0/1 | Generator currently running. |
| `admin_state` | float | Configured state. |
| `oper_state` | float | Actual operating state. |
| `admin_mode` | string | Operating mode setting. |

---

## enphase_grid_profile

**Grid interconnection rules (AGF).** Defines voltage/frequency limits for
grid-connect/disconnect per local regulations (IEEE 1547, etc.)

**Update rate:** 60 min
**Tags:** `serial`

| Field | Type | What it means |
|-------|------|---------------|
| `id` | string | Grid code standard ID (e.g., "IEEE1547-2018"). |
| `name` | string | Human-readable name. |
| `grid_code` | string | Standard identifier. |
| `country` | string | Country code. |
| `region` | string | State/region. |
| `profile_count` | count | Number of uploaded grid profiles. |

---

## enphase_comm

**Wireless communication quality between gateway and devices.**

**Update rate:** 5 min
**Tags:** `serial`, `device_type` (pcu | encharge | esub)

| Field | Unit | What it means |
|-------|------|---------------|
| `count` | count | Number of devices of this type. |
| `comm_level` | 0–100 | Overall wireless signal quality. |
| `comm_level_24g` | 0–100 | 2.4 GHz signal quality (encharge only). |
| `comm_level_subg` | 0–100 | Sub-GHz signal quality (encharge only). |

---

## enphase_event

**Gateway event log.** Firmware events, grid outages, meter issues.

**Update rate:** 5 min
**Tags:** `serial`

| Field | Type | What it means |
|-------|------|---------------|
| `event_count` | count | Total events in log. |
| `event_0_msg` … `event_9_msg` | string | Last 10 event messages (newest first). |
| `event_0_id` … `event_9_id` | string | Event type IDs. |
| `event_0_serial` … `event_9_serial` | string | Device serial that caused each event. |

---

## enphase_system

**Deep system internals — firmware, Zigbee, cellular, PEB, sub-panels.**
Mostly for debugging. Fields are **dynamic** (whatever the API returns).

**Update rate:** 30 min
**Tags:** `serial`, `subsystem`

| `subsystem` tag | Gateway endpoint | What it covers |
|----------------|-----------------|----------------|
| `sc_status` | /ivp/sc/status | Secondary Controller state machine |
| `livedata_status` | /ivp/livedata/status | MQTT streaming status on gateway side |
| `firmware_state` | /ivp/firmware_manager/state | Firmware update progress |
| `firmware_config` | /ivp/firmware_manager/config | Auto-update policy |
| `zigbee_status` | /ivp/zb/status | Zigbee mesh network health |
| `zigbee_pairing` | /ivp/zb/pairing_status | Device pairing mode |
| `peb_status` | /ivp/peb/devstatus | Power Electronics Box status |
| `cellular_status` | /ivp/cellular | Cellular modem signal/carrier |
| `sub_panel` | /inv | IQ System Controller sub-panels |
| `ensemble_secctrl` | /ivp/ensemble/secctrl | Zigbee security controller |
| `ensemble_device` | /ivp/ensemble/device_list | CAN bus device list |
| `ensemble_device_status` | /ivp/ensemble/device_status | Per-device detailed status |
| `ensemble_submodule` | /ivp/ensemble/submod | Battery submodule status |
| `agf_profile_status` | /ivp/ensemble/profile_status | AGF profile application state |
| `arf_multimode` | /ivp/arf/profile/multimode/* | On-grid/off-grid profile params |
| `der_settings` | /ivp/ss/der_settings | DER breaker/backfeed config |
| `pcs_settings` | /ivp/ss/pcs_settings | Power Conditioning System settings |
| `meter_config` | /ivp/meters | CT meter configuration |
| `meter_ct_config` | /ivp/meters/cts | CT clamp direction/turns ratio |
| `network_config` | /admin/lib/network_display.json | DHCP, static IP, DNS |
| `wireless_config` | /admin/lib/wireless_display.json | WiFi SSID, channel |
| `datetime_config` | /admin/lib/date_time_display.json | NTP, timezone |

> **DER settings** define electrical limits: main breaker size, max backfeed
> current, consumption CT placement. Set during installation, rarely changes.
>
> **PCS settings** configure the Power Conditioning System — the electronics
> that convert and manage power between solar, battery, grid, and loads.

---

# Reference

## Naming conventions

| Suffix | Meaning | Example |
|--------|---------|---------|
| `_w` | Active power in watts | `solar_w`, `grid_l1_w` |
| `_va` | Apparent power in volt-amps | `solar_va` |
| `_var` | Reactive power in VAR | `reactive_power_var` |
| `_wh` | Energy in watt-hours | `production_wh` |
| `_kwh` | Energy in kilowatt-hours | `available_energy_kwh` |
| `_kw` | Power in kilowatts | `max_power_kw` |
| `_v` | Voltage in volts | `voltage_v` |
| `_a` | Current in amperes | `current_a` |
| `_hz` | Frequency in hertz | `frequency_hz` |
| `_c` | Temperature in Celsius | `temperature_c` |
| `_pct` | Percentage (0–100) | `backup_reserve_pct` |
| `_min` | Time in minutes | `estimated_backup_min` |
| `_s` | Time in seconds | `uptime_s` |
| `_l1_*`, `_l2_*` | Per-phase (split-phase L1/L2) | `solar_l1_w` |
| `_str` | Human-readable string enum | `battery_mode_str` |
| `_total` | Cumulative counter since startup | `mqtt_msg_total` |

## Sign conventions

| Measurement | Positive (+) | Negative (−) |
|-------------|-------------|-------------|
| Solar | Producing | *(never negative)* |
| Grid | Importing (buying) | Exporting (selling) |
| Consumption | Consuming | *(never negative)* |
| Battery | Discharging (powering home) | Charging (absorbing) |
| Generator | Producing | *(never negative)* |

## Typical value ranges

| Measurement | Normal range | Anomaly threshold |
|-------------|-------------|-------------------|
| Solar power | 0–10,000 W | >100,000 W |
| Grid power | −10,000 to +10,000 W | >100,000 W |
| Consumption | 0–15,000 W | >100,000 W |
| Battery power | −5,000 to +5,000 W | >100,000 W |
| SOC | 0–100% | Outside 0–100 |
| Voltage (per phase) | 110–130 V | <100 or >140 V |
| Frequency | 59.95–60.05 Hz | <59.5 or >60.5 Hz |
| Power factor | 0.80–1.00 | <0.50 |
| Battery temperature | 10–40 °C | <0 or >55 °C |

## Glossary

| Term | What it means |
|------|---------------|
| **SOC** | State of Charge — battery fuel gauge (0–100%) |
| **SOH** | State of Health — battery degradation (100% = new, decreasing over years) |
| **CT** | Current Transformer — clamp-on sensor measuring power on a wire |
| **DER** | Distributed Energy Resource — your solar+battery as a grid-connected asset |
| **PCS** | Power Conditioning System — electronics that convert/manage power |
| **AGF** | Advanced Grid Functions — grid compliance profile (voltage/frequency limits) |
| **Islanding** | Running disconnected from the grid (battery + solar only) |
| **Backfeed** | Pushing power back onto the grid (exporting solar surplus) |
| **TOU** | Time of Use — rate plan where electricity costs different amounts at different times |
| **PCU** | Power Conditioning Unit — Enphase's term for a microinverter |
| **EID** | Endpoint ID — unique identifier for a meter/device in the gateway |
| **Zigbee** | Wireless protocol for gateway ↔ microinverter/battery communication |
| **Split-phase** | US 240V service: two 120V legs (L1/L2) with opposite polarity |
| **LFP** | Lithium Iron Phosphate — battery chemistry in IQ Battery (safer, longer life) |
| **NC/NO** | Normally Closed / Normally Open — dry contact default relay position |
| **CAN bus** | Controller Area Network — wired protocol between gateway and battery units |
| **SSE** | Server-Sent Events — HTTP-based real-time push from gateway to collector |

## Cloud endpoint → measurement mapping

| Cloud endpoint | Interval | Measurement(s) written |
|---------------|----------|----------------------|
| `latest_power` | 120s | `enphase_power` (solar_w) |
| `battery_status` | 120s | `enphase_battery` (soc, capacity, cycles, soh), `enphase_power` (soc) |
| `today` | 300s | `enphase_energy` (daily totals), `enphase_battery` (backup time), `enphase_config` (reserve, storm guard), `enphase_gateway` (wifi, cellular) |
| `events` | 300s | *(cached only — not yet mapped to timeseries)* |
| `alarms` | 600s | `enphase_gateway` (alarm_count, if > 0) |
| `battery_schedules` | 600s | *(cached only)* |
| `inverters` | 1800s | `enphase_inverters` (fleet health) |
| `dashboard_status` | 1800s | *(cached only)* |
| `dashboard_summary` | 1800s | *(cached only)* |
| `site_data` | 3600s | `enphase_energy` (lifetime), `enphase_gateway` (status, fleet counts) |
| `devices` | 3600s | *(triggers auto-discovery of serial + gateway IP)* |
| `battery_settings` | 3600s | *(cached — prefer today.batteryConfig which is fresher)* |
| `battery_backup_history` | 3600s | *(cached only)* |
| `lifetime_energy` | 3600s | *(cached only)* |
| `grid_eligibility` | 7200s | *(cached only)* |
| `device_tree` | 3600s | *(cached only)* |
| `ev_charger_status` | 3600s | *(cached only — optional)* |
| `ev_charger_summary` | 3600s | *(cached only — optional)* |
| `hems_devices` | 3600s | *(cached only — optional)* |
| `livestream_flags` | 3600s | *(cached only)* |

## Local gateway collector → measurement mapping

| Collector | Endpoint | Interval | Measurement(s) |
|-----------|----------|----------|----------------|
| stream | /stream/meter (SSE) | ~1s | `enphase_meter` |
| production | /production.json | 5 min | `enphase_power` |
| meters | /ivp/meters/readings | 2 min | `enphase_meter` |
| battery | /ivp/ensemble/status, /power, /inventory | 2 min | `enphase_battery`, `enphase_battery_device` |
| inverters | /api/v1/production/inverters | 5 min | `enphase_inverter` |
| inverter_status | /installer/agf/inverters_status.json | 5 min | `enphase_inverter`, `enphase_inverters` |
| inventory | /inventory.json | 60 min | `enphase_device` |
| home | /home.json | 5 min | `enphase_gateway`, `enphase_comm` |
| tariff | /admin/lib/tariff.json | 10 min | `enphase_config` |
| gateway_info | /info.xml | 60 min | `enphase_gateway` |
| grid_status | /ivp/ensemble/relay, /dry_contacts, /errors | 10 min | `enphase_config`, `enphase_dry_contact`, `enphase_error` |
| charge_schedule | /ivp/sc/sched, /ivp/ss/pel_settings | 10 min | `enphase_config` |
| device_health | /ivp/eh/devs, /ivp/ensemble/generator | 30 min | `enphase_device`, `enphase_generator` |
| grid_profile | /installer/agf/details.json, index.json, inverters_phase.rb | 30 min | `enphase_grid_profile`, `enphase_inverter` |
| event_log | /datatab/event_dt.rb | 10 min | `enphase_event` |
| ensemble_detail | /ivp/ensemble/device_list, device_status, submod, secctrl, profile_status | 30 min | `enphase_system` |
| system_status | /ivp/sc/status, firmware, zigbee, cellular, /inv, etc. | 30 min | `enphase_system` |
| site_settings | /ivp/ss/der_settings, dry_contact_settings, gen_*, arf, network, etc. | 30 min | `enphase_system`, `enphase_dry_contact`, `enphase_generator` |
| meter_detail | /ivp/meters, /cts, /storage_setting, /reports/* | 10 min | `enphase_system`, `enphase_meter` |
