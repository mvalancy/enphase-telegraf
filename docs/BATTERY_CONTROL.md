# Battery control

The `enphase_cloud` Python package can control Enphase IQ Battery settings via
the Enlighten cloud API. No local network access required.

## Supported operations

| Method | What it does |
|--------|-------------|
| `set_battery_mode(mode)` | Switch operating mode |
| `set_reserve_soc(percent)` | Set backup reserve percentage |
| `set_charge_from_grid(enabled)` | Enable/disable grid charging |
| `set_storm_guard(enabled)` | Enable/disable storm guard |
| `create_schedule(...)` | Create charge/discharge schedule |
| `delete_schedule(id)` | Delete a schedule |

## Battery modes

| Mode string | Behavior |
|------------|----------|
| `self-consumption` | Solar powers home first, excess charges battery, overflow to grid |
| `savings` | Time-of-use arbitrage — charge when cheap, discharge when expensive |
| `backup` | Hold full charge for outage protection |
| `economy` | Variant of savings (availability depends on utility/region) |

## Usage

```python
from enphase_cloud.enlighten import EnlightenClient

client = EnlightenClient("you@example.com", "your-password")
client.login()

# Read current state
battery = client.get_battery_status()
print(f"SOC: {battery['current_charge']}")
print(f"Capacity: {battery['max_capacity']} kWh")

settings = client.get_battery_settings()
print(f"Mode: {settings.get('usage')}")
print(f"Reserve: {settings.get('battery_backup_percentage')}%")

# Change mode
client.set_battery_mode("self-consumption")

# Set backup reserve to 20%
client.set_reserve_soc(20)

# Enable charging from grid
client.set_charge_from_grid(True)

# Enable storm guard (auto-charge before storms)
client.set_storm_guard(True)
```

## Schedules

Schedules control battery behavior during specific time windows:

```python
# Charge from grid on weeknights (cheap power)
client.create_schedule(
    schedule_type="CFG",        # Charge From Grid
    start_time="23:00",
    end_time="06:00",
    days=["Mon", "Tue", "Wed", "Thu", "Fri"],
    limit_percent=100,
)

# List existing schedules
schedules = client.get_battery_schedules()
for s in schedules.get("schedules", []):
    print(f"  {s['id']}: {s['schedule_type']} {s['startTime']}-{s['endTime']}")

# Delete a schedule
client.delete_schedule("schedule-id-here")
```

### Schedule types

| Type | Meaning |
|------|---------|
| `CFG` | Charge From Grid — charge battery from grid during this window |
| `DTG` | Discharge To Grid — export battery to grid during this window |
| `RBD` | Reduced Backup Discharge — lower backup reserve during this window |

## CLI tool

The `examples/battery_control.py` script provides a command-line interface:

```bash
# Show current battery state
python3 examples/battery_control.py status

# Change mode
python3 examples/battery_control.py mode self-consumption

# Set reserve
python3 examples/battery_control.py reserve 20

# Toggle charge from grid
python3 examples/battery_control.py cfg on
python3 examples/battery_control.py cfg off

# Toggle storm guard
python3 examples/battery_control.py storm on
```

## API details

All battery control goes through the `batteryConfig` microservice:

```
Base: https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1
```

### Authentication

Battery control requires three auth tokens (more than read-only endpoints):

| Header | Source |
|--------|--------|
| Session cookies | From `/login/login.json` |
| `e-auth-token` | JWT from `/app-api/jwt_token.json` |
| `x-xsrf-token` | XSRF token from `_enlighten_4_session_xsrf` cookie |

The `Origin` header must be set to `https://battery-profile-ui.enphaseenergy.com`.

### Write endpoints

**Set mode / reserve / charge-from-grid / storm guard:**
```
PUT /service/batteryConfig/api/v1/batterySettings/{site_id}
Content-Type: application/json

{"usage": "self-consumption"}
{"battery_backup_percentage": 20}
{"chargeFromGrid": true, "acceptedItcDisclaimer": "2024-01-01T00:00:00-07:00"}
{"severe_weather_watch": "enabled"}
```

**Create schedule:**
```
POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules
Content-Type: application/json

{
    "schedule_type": "CFG",
    "startTime": "2300",
    "endTime": "0600",
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "limit": 100
}
```

**Delete schedule:**
```
POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules/{id}/delete
Content-Type: application/json

{}
```

## Monitoring battery state

The collector emits battery data to InfluxDB from multiple sources:

| Measurement | Source | Update rate | Fields |
|-------------|--------|-------------|--------|
| `enphase_power` | MQTT | ~1/sec | `soc` (charge level %) |
| `enphase_battery` | Cloud API | 2 min | `soc`, `available_energy_kwh`, `max_capacity_kwh`, `cycle_count_N`, `soh_N`, `estimated_backup_min` |
| `enphase_config` | MQTT + Cloud | On change | `battery_mode`, `backup_reserve_pct`, `charge_from_grid`, `storm_guard` |

### SOC vs backup_reserve_pct

These are commonly confused:

- **`soc`** (in `enphase_power` and `enphase_battery`): The actual battery charge level. 85 means the battery has 85% charge right now.
- **`backup_reserve_pct`** (in `enphase_config`): The backup reserve *setting*. 20 means "keep at least 20% charge for outage protection."

In the protobuf stream, `meter_soc` is the actual charge and `backup_soc` is
the setting. The collector maps these to the correct fields.
