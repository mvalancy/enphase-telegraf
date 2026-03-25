# Enphase Enlighten Cloud API Reference

Reverse-engineered API reference for the Enphase Enlighten portal endpoints used
by this project. These are **undocumented internal APIs** consumed by the
Enlighten mobile app and web UI -- not the official Enphase Developer API. They
can change without notice.

Primary source: `src/enphase_cloud/enlighten.py` and the barneyonline/ha-enphase-energy
reverse engineering project.

---

## Table of Contents

1. [Base URLs](#base-urls)
2. [Authentication](#authentication)
3. [Request Conventions](#request-conventions)
4. [Endpoint Reference](#endpoint-reference)
   - [Power and Energy](#power-and-energy)
   - [Battery](#battery)
   - [System and Devices](#system-and-devices)
   - [Events and Alarms](#events-and-alarms)
   - [Dashboard](#dashboard)
   - [EV / HEMS (Optional)](#ev--hems-optional)
   - [Live Streaming](#live-streaming)
   - [Gateway Token](#gateway-token)
5. [Battery Control Endpoints](#battery-control-endpoints)
6. [Polling Schedule](#polling-schedule)
7. [Rate Limiting and Reliability](#rate-limiting-and-reliability)
8. [Response Gotchas](#response-gotchas)

---

## Base URLs

| Constant | URL | Used for |
|----------|-----|----------|
| `BASE_URL` | `https://enlighten.enphaseenergy.com` | All portal endpoints, authentication, battery config services |
| `ENTREZ_URL` | `https://entrez.enphaseenergy.com` | Entrez auth (defined but not directly used in polling) |
| HEMS | `https://hems-integration.enphaseenergy.com` | Home energy management, live stream |

---

## Authentication

### Overview

The auth flow mimics what the Enlighten mobile app does. There is no OAuth or
API key. You authenticate with email/password, receive session cookies, then
acquire a JWT token for service endpoints. The entire flow requires three HTTP
requests.

### Step 1: Login

```
POST https://enlighten.enphaseenergy.com/login/login.json
Content-Type: application/x-www-form-urlencoded
Accept: application/json
User-Agent: EnphaseLocal/1.0
```

**Form body:**

| Parameter | Value |
|-----------|-------|
| `user[email]` | Account email address |
| `user[password]` | Account password |

**Important:** Send with `allow_redirects=False`. Enlighten may 302 redirect on
success; you want the JSON response, not the HTML redirect target.

**Response (200):**

```json
{
  "session_id": "abc123def456...",
  "user_id": 12345678,
  "manager_token": "...",
  "mfa_required": false
}
```

**Response (401):** Invalid credentials.

**MFA detection:** If `mfa_required` is `true` in the response body, the account
has two-factor authentication enabled. The next step would be
`POST /app-api/validate_login_otp`, but this project does **not** support MFA
and raises `MFARequired` immediately. Automated integrations require MFA to be
disabled on the Enphase account.

**What this gives you:**
- Session cookies set by the server (stored in the `requests.Session` cookie jar)
- `user_id` (or `manager_token` as fallback) -- needed for battery config endpoints
- `session_id` -- maintained server-side

### Step 2: Discover Sites

```
GET https://enlighten.enphaseenergy.com/app-api/search_sites.json?searchText=&favourite=false
Accept: application/json
Cookie: <session cookies from Step 1>
```

**Response (200):**

```json
{
  "sites": [
    {
      "id": 1234567,
      "name": "My Home",
      "address": "123 Solar St",
      "timezone": "America/Los_Angeles",
      "status": "normal",
      ...
    }
  ]
}
```

The first site's `id` becomes the `site_id` used in all subsequent endpoint
URLs. Multi-site accounts use `sites[0]`.

### Step 3: Acquire JWT Token

```
GET https://enlighten.enphaseenergy.com/app-api/jwt_token.json
Accept: application/json
Cookie: <session cookies from Step 1>
```

**Response (200):**

```json
{
  "token": "eyJhbGciOi..."
}
```

This JWT is sent as the `e-auth-token` header on all subsequent data requests.

### Step 4: Extract XSRF Token

The XSRF token is extracted from the session cookie jar after login:

```python
xsrf = session.cookies.get("_enlighten_4_session_xsrf", "")
```

This is sent as the `x-xsrf-token` header on all subsequent requests.

### Session TTL and Auto-Refresh

Sessions expire after **3600 seconds (1 hour)**. The client tracks
`auth_time` and re-runs the full login flow when the session is stale. Every
call to `_ensure_auth()` checks:

```python
if time.time() - self._session.auth_time > self.SESSION_TTL:
    self.login()  # full re-authentication
```

There is no token refresh endpoint. Re-authentication is a complete repeat of
Steps 1-4.

### Headers Summary

Every authenticated request includes these headers:

| Header | Value | When |
|--------|-------|------|
| `Accept` | `application/json` | Always (set on session) |
| `User-Agent` | `EnphaseLocal/1.0` | Always (set on session) |
| `e-auth-token` | JWT from Step 3 | When JWT is available |
| `x-xsrf-token` | Cookie value of `_enlighten_4_session_xsrf` | When XSRF token is available |
| `Cookie` | Session cookies | Automatic (managed by `requests.Session`) |

Battery control endpoints add additional headers -- see
[Battery Control Endpoints](#battery-control-endpoints).

---

## Request Conventions

- All GET/POST requests go through `_get()` / `_post()` helper methods.
- Every request sleeps **0.5 seconds** before executing (`time.sleep(0.5)`).
- Timeout is **15 seconds** per request.
- Auth is checked before every request via `_ensure_auth()`.
- `{sid}` in URL patterns below means `self._session.site_id`.
- `{uid}` means `self._session.user_id`.

---

## Endpoint Reference

### Power and Energy

#### 1. Latest Power

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/app-api/{sid}/get_latest_power` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 120s |
| **InfluxDB measurement** | `enphase_power` (tag: `source=cloud`) |

**Response:**

```json
{
  "latest_power": {
    "value": 4523.0,
    "unit": "W",
    "timestamp": 1700000000
  }
}
```

**Parsing:** The project extracts `data["latest_power"]["value"]` and emits it
as `solar_w`. Only emitted if `value` is not None.

**Quirks:** The nested `latest_power` key contains the actual data object. If
the gateway has not reported recently, `value` may be stale or null.

---

#### 2. Today (Daily Stats)

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/pv/systems/{sid}/today.json` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 300s |
| **InfluxDB measurements** | `enphase_energy`, `enphase_battery`, `enphase_config`, `enphase_gateway` |

This is the richest single endpoint. It returns daily energy totals, battery
details, battery configuration, and gateway connection info all in one response.

**Response (abbreviated):**

```json
{
  "stats": [
    {
      "totals": {
        "production": 15432.0,
        "consumption": 22100.0,
        "charge": 5000.0,
        "discharge": 3200.0,
        "solar_home": 10432.0,
        "solar_battery": 5000.0,
        "solar_grid": 0.0,
        "battery_home": 3200.0,
        "battery_grid": 0.0,
        "grid_home": 8468.0,
        "grid_battery": 0.0
      },
      "intervals": [ ... ]
    }
  ],
  "battery_details": {
    "aggregate_soc": 72,
    "estimated_time": 480,
    "last_24h_consumption": 18.5
  },
  "batteryConfig": {
    "battery_backup_percentage": 20,
    "very_low_soc": 5,
    "charge_from_grid": false,
    "severe_weather_watch": "disabled",
    "usage": "self-consumption"
  },
  "connectionDetails": [
    {
      "wifi": true,
      "cellular": false,
      "ethernet": true
    }
  ]
}
```

**Parsing -- energy totals:** Totals are at `stats[0].totals`, **not** at the
top level. See [Response Gotchas](#response-gotchas). Mapped fields:

| Source key | InfluxDB field | Measurement |
|------------|---------------|-------------|
| `totals.production` | `production_wh` | `enphase_energy` |
| `totals.consumption` | `consumption_wh` | `enphase_energy` |
| `totals.charge` | `charge_wh` | `enphase_energy` |
| `totals.discharge` | `discharge_wh` | `enphase_energy` |
| `totals.solar_home` | `solar_to_home_wh` | `enphase_energy` |
| `totals.solar_battery` | `solar_to_battery_wh` | `enphase_energy` |
| `totals.solar_grid` | `solar_to_grid_wh` | `enphase_energy` |
| `totals.battery_home` | `battery_to_home_wh` | `enphase_energy` |
| `totals.battery_grid` | `battery_to_grid_wh` | `enphase_energy` |
| `totals.grid_home` | `grid_to_home_wh` | `enphase_energy` |
| `totals.grid_battery` | `grid_to_battery_wh` | `enphase_energy` |

**Parsing -- battery details:**

| Source key | InfluxDB field | Measurement |
|------------|---------------|-------------|
| `battery_details.aggregate_soc` | `soc` | `enphase_battery` |
| `battery_details.estimated_time` | `estimated_backup_min` | `enphase_battery` |
| `battery_details.last_24h_consumption` | `last_24h_consumption_kwh` | `enphase_battery` |

**Parsing -- battery config:** Only emitted when `battery_backup_percentage`
changes from the last seen value (change-detection to avoid noise).

| Source key | InfluxDB field | Measurement |
|------------|---------------|-------------|
| `batteryConfig.battery_backup_percentage` | `backup_reserve_pct` | `enphase_config` |
| `batteryConfig.very_low_soc` | `very_low_soc_pct` | `enphase_config` |
| `batteryConfig.charge_from_grid` | `charge_from_grid` (0/1) | `enphase_config` |
| `batteryConfig.severe_weather_watch` | `storm_guard` (0/1) | `enphase_config` |
| `batteryConfig.usage` | `usage_str` | `enphase_config` |

**Parsing -- connection details:** Extracted from `connectionDetails[0]`.

| Source key | InfluxDB field | Measurement |
|------------|---------------|-------------|
| `connectionDetails[0].wifi` | `wifi` (0/1) | `enphase_gateway` |
| `connectionDetails[0].cellular` | `cellular` (0/1) | `enphase_gateway` |
| `connectionDetails[0].ethernet` | `ethernet` (0/1) | `enphase_gateway` |

---

#### 3. Lifetime Energy

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/pv/systems/{sid}/lifetime_energy` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None directly (lifetime data comes from `site_data` instead) |

**Response:**

```json
{
  "lifetime_energy": 12345678.0,
  "unit": "Wh"
}
```

**Quirks:** This endpoint exists and is called by `scrape_all()`, but the
regular polling loop gets lifetime energy from the `site_data` endpoint instead.

---

#### 4. Site Data

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/app-api/{sid}/data.json?app=1&device_status=non_retired&is_mobile=0` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 3600s |
| **InfluxDB measurements** | `enphase_energy`, `enphase_gateway` |

The main portal data endpoint. Returns everything the mobile app home screen
needs.

**Query parameters:**

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `app` | `1` | Signals the mobile app context |
| `device_status` | `non_retired` | Exclude decommissioned devices |
| `is_mobile` | `0` | Desktop-format response |

**Response (abbreviated):**

```json
{
  "module": {
    "lifetime": {
      "lifetimeEnergy": {
        "value": 12345678.0,
        "consumed": 18000000.0
      }
    },
    "detail": {
      "system": {
        "statusCode": "normal",
        "microinverters": 20,
        "encharge": 2
      }
    }
  },
  "timezone": "America/Los_Angeles",
  ...
}
```

**Parsing:**

| Source path | InfluxDB field | Measurement |
|-------------|---------------|-------------|
| `module.lifetime.lifetimeEnergy.value` | `lifetime_production_wh` | `enphase_energy` |
| `module.lifetime.lifetimeEnergy.consumed` | `lifetime_consumption_wh` | `enphase_energy` |
| `module.detail.system.statusCode` | `status_str` | `enphase_gateway` |
| `module.detail.system.microinverters` | `microinverters` | `enphase_gateway` |
| `module.detail.system.encharge` | `batteries` | `enphase_gateway` |

---

### Battery

#### 5. Battery Status

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/pv/settings/{sid}/battery_status.json` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 120s |
| **InfluxDB measurements** | `enphase_battery`, `enphase_power` (SOC echo) |

**Response:**

```json
{
  "current_charge": "85%",
  "available_energy": 7.2,
  "max_capacity": 10.08,
  "available_power": 3.84,
  "max_power": 3.84,
  "included_count": 2,
  "active_micros": 20,
  "total_micros": 20,
  "storages": [
    {
      "serial_number": "12345678901234",
      "cycle_count": 312,
      "battery_soh": "98%"
    },
    {
      "serial_number": "12345678901235",
      "cycle_count": 298,
      "battery_soh": "97%"
    }
  ]
}
```

**Parsing:**

| Source key | InfluxDB field | Type coercion | Measurement |
|------------|---------------|---------------|-------------|
| `current_charge` | `soc` | Parse "85%" string or raw int | `enphase_battery` |
| `available_energy` | `available_energy_kwh` | `float()` | `enphase_battery` |
| `max_capacity` | `max_capacity_kwh` | `float()` | `enphase_battery` |
| `available_power` | `available_power_kw` | `float()` | `enphase_battery` |
| `max_power` | `max_power_kw` | `float()` | `enphase_battery` |
| `included_count` | `unit_count` | `int()` | `enphase_battery` |
| `active_micros` | `active_inverters` | `int()` | `enphase_battery` |
| `total_micros` | `total_inverters` | `int()` | `enphase_battery` |
| `storages[N].cycle_count` | `cycle_count_1` ... `cycle_count_4` | `int()` | `enphase_battery` |
| `storages[N].battery_soh` | `soh_1` ... `soh_4` | Parse "98%" string | `enphase_battery` |

SOC is also echoed to `enphase_power` (tag `source=cloud`) so that unified
power+SOC queries work without joining measurements.

**Quirks:** `current_charge` can be a string like `"85%"` or `"85.5%"`, a bare
integer like `85`, or a float like `85.5`. The parser strips `%` and converts.
`battery_soh` has the same format variance. See
[Response Gotchas](#response-gotchas).

---

#### 6. Battery Backup History

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/app-api/{sid}/battery_backup_history.json` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None (informational, not parsed into line protocol) |

**Response:**

```json
{
  "events": [
    {
      "start_time": 1700000000,
      "end_time": 1700003600,
      "duration_seconds": 3600,
      "type": "grid_outage"
    }
  ]
}
```

---

#### 7. Battery Settings

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{sid}?userId={uid}&source=enho` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` directly (config data comes from `today` endpoint in normal polling) |
| **InfluxDB measurement** | Used for battery control operations and `scrape_all` |

**Query parameters:**

| Parameter | Value |
|-----------|-------|
| `userId` | The `user_id` from login |
| `source` | `enho` (Enphase Homeowner) |

**Response:**

```json
{
  "usage": "self-consumption",
  "battery_backup_percentage": 20,
  "very_low_soc": 5,
  "charge_from_grid": false,
  "severe_weather_watch": "disabled",
  "chargeFromGrid": false,
  "storm_guard_enabled": false
}
```

---

#### 8. Battery Schedules

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/batteryConfig/api/v1/battery/sites/{sid}/schedules` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 600s |
| **InfluxDB measurement** | None (informational, not parsed into line protocol) |

**Response:**

```json
{
  "schedules": [
    {
      "id": "sched-uuid-1234",
      "scheduleType": "CFG",
      "startTime": "01:00",
      "endTime": "06:00",
      "days": [0, 1, 2, 3, 4, 5, 6],
      "limit": 0,
      "timezone": "America/Los_Angeles"
    }
  ]
}
```

**Schedule types:**

| Type | Meaning |
|------|---------|
| `CFG` | Charge From Grid |
| `DTG` | Discharge To Grid |
| `RBD` | Reserve Battery Discharge |

---

### System and Devices

#### 9. Devices

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/app-api/{sid}/devices.json` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 3600s |
| **InfluxDB measurement** | None directly (used for serial number discovery) |

**Response:**

```json
{
  "result": [
    {
      "type": "envoy",
      "devices": [
        {
          "serial_number": "122312345678",
          "status": "normal",
          "firmware": "8.2.1234"
        }
      ]
    },
    {
      "type": "inverter",
      "devices": [ ... ]
    },
    {
      "type": "encharge",
      "devices": [ ... ]
    }
  ]
}
```

The project uses this endpoint for gateway serial number discovery. It walks the
`result` array looking for entries with `type` of `"envoy"` or `"gateway"`, then
extracts `serial_number`, `serial_num`, or `sn` (whichever is present).

---

#### 10. Inverters

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/app-api/{sid}/inverters.json` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 1800s |
| **InfluxDB measurement** | `enphase_inverters` |

**Response:**

```json
{
  "total": 20,
  "not_reporting": 0,
  "error_count": 0,
  "warning_count": 0,
  "normal_count": 20,
  "inverters": [
    {
      "serial_number": "123456789012",
      "last_report_watts": 250,
      "status": "normal"
    }
  ]
}
```

**Parsing:**

| Source key | InfluxDB field | Measurement |
|------------|---------------|-------------|
| `total` | `total` | `enphase_inverters` |
| `not_reporting` | `not_reporting` | `enphase_inverters` |
| `error_count` | `error_count` | `enphase_inverters` |
| `warning_count` | `warning_count` | `enphase_inverters` |
| `normal_count` | `normal_count` | `enphase_inverters` |

**Quirks:** All values default to `0` if missing (`data.get("field") or 0`).

---

#### 11. Grid Eligibility

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/app-api/{sid}/grid_control_check.json` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

Returns whether grid services / grid control features are available for the
site. Informational only.

---

### Events and Alarms

#### 12. Events

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/events-platform-service/v1.0/{sid}/events/homeowner` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 300s |
| **InfluxDB measurement** | None (fetched for monitoring but not currently emitted to line protocol) |

**Response:**

```json
{
  "events": [
    {
      "event_id": "evt-1234",
      "type": "grid_outage",
      "start_time": "2024-01-15T10:30:00Z",
      "end_time": "2024-01-15T11:15:00Z",
      "status": "resolved",
      "description": "Grid outage detected"
    }
  ]
}
```

---

#### 13. Alarms

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{sid}/alarms` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | 600s |
| **InfluxDB measurement** | `enphase_gateway` |

**Response:**

```json
{
  "total": 1,
  "alarms": [
    {
      "alarm_id": "alm-5678",
      "severity": "warning",
      "device_serial": "122312345678",
      "message": "Communication issue with microinverter"
    }
  ]
}
```

**Parsing:** Only emits when `total > 0`:

| Source key | InfluxDB field | Measurement |
|------------|---------------|-------------|
| `total` | `alarm_count` | `enphase_gateway` |

**Quirks:** `total` can be `None`, `0`, or a string. The parser wraps the
conversion in `int(data.get("total", 0) or 0)` with a try/except to handle all
variants.

---

### Dashboard

#### 14. Dashboard Summary

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/system_dashboard/api_internal/cs/sites/{sid}/summary` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

System dashboard summary. Returns high-level system health. Used by `scrape_all`
for diagnostics.

---

#### 15. Dashboard Status

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{sid}/status` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

System health, error counts, and device status overview.

---

#### 16. Device Tree

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{sid}/devices-tree` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

Returns the device communication tree -- which devices communicate through which
gateway/relay.

---

### EV / HEMS (Optional)

These endpoints return `None` on HTTP error instead of raising. They are
no-ops on systems without the corresponding hardware.

#### 17. EV Charger Status

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/evse_controller/{sid}/ev_chargers/status` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

Returns current EV charger state (connected, charging, idle). Returns `None` if
no EV charger is installed.

---

#### 18. EV Charger Summary

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/service/evse_controller/api/v2/{sid}/ev_chargers/summary` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

Charging session history and energy totals.

---

#### 19. HEMS Devices

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `https://hems-integration.enphaseenergy.com/api/v1/hems/{sid}/hems-devices` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

**Note:** This is the only read endpoint that hits a different base URL
(`hems-integration.enphaseenergy.com` instead of `enlighten.enphaseenergy.com`).

Returns HEMS device inventory (IQ Energy Router, heat pumps, etc.). Returns
`None` if no HEMS devices are installed.

---

### Live Streaming

#### 20. Livestream Flags

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/app-api/{sid}/show_livestream` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | Not in `CLOUD_SCHEDULE` (scraped via `scrape_all` only) |
| **InfluxDB measurement** | None |

Checks whether live streaming is available for this site.

#### Live Status (Supplemental)

Not counted in the 20 endpoint inventory as it is a composite operation with
fallback. Uses two different approaches:

**Primary -- HEMS live stream:**

```
PUT https://hems-integration.enphaseenergy.com/api/v1/hems/{sid}/live-stream/status
Content-Type: application/json
Body: {"status": true}
```

**Fallback -- SSE subscribe:**

```
GET {BASE_URL}/service/evse_sse/subscribeEvent?key={sid}
```

Both return `None` on failure.

---

### Gateway Token

#### Entrez Auth Token

| | |
|-|-|
| **Method** | `GET` |
| **URL** | `{BASE_URL}/entrez-auth-token?serial_num={serial}` |
| **Auth** | Session cookies + `e-auth-token` + `x-xsrf-token` |
| **Poll interval** | On demand |
| **InfluxDB measurement** | None |

Returns a JWT for authenticating directly with the local IQ Gateway. Used when
the project needs to make local API calls and must obtain a token via the cloud
(rather than using local credentials).

---

## Battery Control Endpoints

All battery control methods use `PUT` to the `batteryConfig` service. They share
common headers and differ only in the JSON body.

### Common Request Properties

```
PUT {BASE_URL}/service/batteryConfig/api/v1/batterySettings/{sid}?userId={uid}&source=enho
```

**Headers:**

| Header | Value |
|--------|-------|
| `Accept` | `application/json` |
| `User-Agent` | `EnphaseLocal/1.0` |
| `e-auth-token` | JWT token |
| `x-xsrf-token` | XSRF token from cookies |
| `Content-Type` | `application/json` |
| `Origin` | `https://battery-profile-ui.enphaseenergy.com` |
| `username` | `user_id` from login |

The `Origin` header is required -- the batteryConfig service validates it. The
`username` header carries the user ID (not email).

### 1. Set Battery Mode

**Body:**

```json
{"usage": "self-consumption"}
```

Valid `usage` values:

| Value | Meaning |
|-------|---------|
| `self-consumption` | Maximize self-consumption of solar |
| `savings` | Time-of-use cost savings |
| `backup` | Full backup -- hold charge for outages |
| `economy` | Economy mode |

### 2. Set Reserve SOC

**Body:**

```json
{"battery_backup_percentage": 20}
```

Integer value 0-100. This is the minimum charge level the battery maintains for
backup purposes during normal operation.

### 3. Set Charge From Grid

**Enable:**

```json
{
  "chargeFromGrid": true,
  "acceptedItcDisclaimer": "2024-01-15T10:30:00.000000"
}
```

**Disable:**

```json
{"chargeFromGrid": false}
```

When enabling, `acceptedItcDisclaimer` is set to the current ISO 8601 timestamp.
This acknowledges the ITC (Investment Tax Credit) disclaimer -- charging from
grid may affect solar tax credit eligibility.

### 4. Set Storm Guard

**Body:**

```json
{"severe_weather_watch": "enabled"}
```

or

```json
{"severe_weather_watch": "disabled"}
```

Note the string values `"enabled"` / `"disabled"`, not boolean.

### 5. Create Schedule

```
POST {BASE_URL}/service/batteryConfig/api/v1/battery/sites/{sid}/schedules
```

**Body:**

```json
{
  "timezone": "America/Los_Angeles",
  "startTime": "01:00",
  "endTime": "06:00",
  "limit": 0,
  "scheduleType": "CFG",
  "days": [0, 1, 2, 3, 4, 5, 6]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `timezone` | string | IANA timezone (should match site timezone) |
| `startTime` | string | `HH:MM` format |
| `endTime` | string | `HH:MM` format |
| `limit` | int | Power limit in watts (0 = unlimited) |
| `scheduleType` | string | `CFG` (charge from grid), `DTG` (discharge to grid), `RBD` (reserve battery discharge) |
| `days` | int[] | Day numbers: 0=Sunday, 1=Monday, ... 6=Saturday |

### 6. Delete Schedule

```
POST {BASE_URL}/service/batteryConfig/api/v1/battery/sites/{sid}/schedules/{schedule_id}/delete
```

**Body:**

```json
{}
```

Note: This is a `POST` with an empty JSON body, not a `DELETE`.

### EV Charger Control (Supplemental)

Not part of the batteryConfig service but included here for completeness:

**Start charging:**

```
POST {BASE_URL}/service/evse_controller/{sid}/ev_chargers/{serial}/start_charging
```

**Stop charging:**

```
POST {BASE_URL}/service/evse_controller/{sid}/ev_chargers/{serial}/stop_charging
```

Both return `None` on failure (charger not installed).

---

## Polling Schedule

The project polls cloud endpoints on staggered intervals to balance data
freshness against rate limiting. This is defined in `CLOUD_SCHEDULE`:

| Endpoint | Interval | Rationale |
|----------|----------|-----------|
| `latest_power` | 120s | Near-real-time power (cloud updates lag ~2 min anyway) |
| `battery_status` | 120s | SOC, capacity, per-unit health |
| `today` | 300s | Daily energy totals, battery config, connectivity |
| `events` | 300s | Outage and system events |
| `alarms` | 600s | Standing alarms (slow-changing) |
| `battery_schedules` | 600s | Charge/discharge schedules (slow-changing) |
| `inverters` | 1800s | Microinverter fleet status |
| `devices` | 3600s | Device inventory (rarely changes) |
| `site_data` | 3600s | Lifetime totals, system overview |

Endpoints not listed here (battery_backup_history, grid_eligibility,
dashboard_summary, dashboard_status, device_tree, ev_charger_status,
ev_charger_summary, hems_devices, livestream_flags, lifetime_energy) are only
fetched by `scrape_all()` and are not part of the regular polling loop.

### Polling Loop Behavior

- The poll loop runs every **30 seconds** (`cloud_poll_loop` sleeps 30s between
  iterations).
- On startup, there is a **10-second delay** before the first poll cycle.
- Within each cycle, every endpoint whose interval has elapsed is fetched
  sequentially.
- Each individual request has a **0.5s sleep** before execution (built into
  `_get()`).
- On error, the next retry for that endpoint is scheduled at
  `now - interval + 60` -- meaning it retries after **60 seconds** regardless of
  the normal interval. This prevents hammering a failing endpoint while still
  retrying sooner than the full interval.

---

## Rate Limiting and Reliability

### What We Know About Enlighten Rate Limits

Enphase does not publish rate limits for these internal APIs. The following is
based on observed behavior:

- **No documented rate limit.** There is no `X-RateLimit-*` header in responses.
- **The 0.5s inter-request delay** (`time.sleep(0.5)` in `_get()` and `_post()`)
  is a self-imposed courtesy delay to avoid triggering any server-side
  throttling. With 9 endpoints in the regular poll schedule and 0.5s delay each,
  a full poll cycle takes ~4.5 seconds of network time.
- **Session-based throttling** may exist. Aggressive polling (sub-second) has
  been observed to cause 429 or 503 responses from some Enlighten service
  endpoints in other projects.
- **The 15-second timeout** per request catches hung connections. Enlighten
  service endpoints occasionally take 5-10 seconds to respond, particularly the
  `batteryConfig` and `system_dashboard` services.

### Error Handling Strategy

1. **Per-endpoint error isolation.** A failure on one endpoint does not prevent
   others from being polled.
2. **Retry backoff.** On error, the failed endpoint is scheduled for retry in 60
   seconds (`_cloud_last_fetch[endpoint] = now - interval + 60`).
3. **Error counters.** `_cloud_errors` tracks total failures; individual errors
   are emitted to `enphase_error` via `emit_error()` and cleared on next success
   via `_clear_error()`.
4. **Session re-authentication.** If the session is older than 3600s, the next
   request triggers a full re-login. HTTP 401 responses during login raise
   `AuthError`.
5. **Optional endpoints return None.** EV charger, HEMS, and live status
   endpoints catch all exceptions and return `None` rather than failing the poll
   cycle.

### Request Timeout

All requests use a **15-second timeout**:

```python
resp = self._session.session.get(url, headers=self._headers(), timeout=15, **kwargs)
```

---

## Response Gotchas

These are the known parsing pitfalls discovered through reverse engineering.
Each one has bitten at least one integration.

### 1. `current_charge` has three possible formats

The `battery_status.json` endpoint returns `current_charge` in inconsistent
formats:

| Observed value | Type | Example |
|----------------|------|---------|
| `"85%"` | string with percent sign | Most common |
| `"85.5%"` | string with decimal and percent | Seen on some firmware |
| `85` | bare integer | Occasionally returned |
| `85.5` | bare float | Occasionally returned |

The parser handles all variants:

```python
if isinstance(charge, str) and "%" in charge:
    bat_fields["soc"] = int(charge.replace("%", "").strip())
elif isinstance(charge, (int, float)):
    bat_fields["soc"] = int(charge)
```

Note that string values with decimals (e.g., `"85.5%"`) are truncated to int
after stripping the percent sign. This means `"85.5%"` becomes `85` via
`int("85.5")` -- which will actually raise `ValueError`. In practice, observed
values have been whole numbers.

### 2. `battery_soh` has the same format variance

Per-battery state of health in `storages[N].battery_soh` can be `"98%"` (string)
or a numeric value. Same parsing strategy as `current_charge`.

### 3. Today's totals are nested inside `stats[0].totals`

The `today.json` response puts energy totals at `stats[0].totals`, **not** at
the top level of the response. This is the most common mistake when first
integrating:

```python
# WRONG:
totals = data.get("totals", {})

# RIGHT:
stats = data.get("stats", [])
totals = stats[0].get("totals", {}) if stats else {}
```

If `stats` is an empty list, there are no totals for today (e.g., the system
just came online).

### 4. `batteryConfig` fields overlap with similar but different names

The battery settings response uses both `charge_from_grid` (boolean, in
`today.json`'s `batteryConfig`) and `chargeFromGrid` (camelCase, in the
`batterySettings` PUT body). They mean the same thing but use different naming
conventions depending on which endpoint you hit.

Similarly, `severe_weather_watch` is a string (`"enabled"` / `"disabled"`), not
a boolean. The project converts it to an integer `0`/`1` for InfluxDB:

```python
"storm_guard": int(bc.get("severe_weather_watch") == "enabled")
```

### 5. Alarm `total` can be None or non-numeric

The alarms endpoint sometimes returns `null` for the `total` field instead of
`0`. The parser guards against this:

```python
try:
    alarm_total = int(data.get("total", 0) or 0)
except (TypeError, ValueError):
    alarm_total = 0
```

### 6. Inverter counts default to 0

All fields in the inverters response (`total`, `not_reporting`, `error_count`,
`warning_count`, `normal_count`) can be `None`. The parser uses
`data.get("field") or 0` to coalesce.

### 7. `connectionDetails` is an array, not an object

Gateway connection info is at `connectionDetails[0]`, not `connectionDetails`
directly. An empty array means no connection data is available.

### 8. Lifetime energy is doubly nested

Production lifetime is at `module.lifetime.lifetimeEnergy.value` -- three levels
of nesting. Consumption is at `module.lifetime.lifetimeEnergy.consumed` (a
sibling key, not in a sub-object).

### 9. Site data requires three query parameters

The `data.json` endpoint returns different response shapes depending on the
query parameters. Omitting `app=1` or `device_status=non_retired` may return a
truncated response or include retired devices.

### 10. Schedule deletion uses POST, not DELETE

The `delete_schedule` method sends a `POST` to
`.../schedules/{schedule_id}/delete` with an empty JSON body `{}`. It does not
use the HTTP `DELETE` method.
