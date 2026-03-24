# enphase-telegraf

Stream real-time Enphase solar+battery data to InfluxDB via Telegraf.

Connects to your Enphase system through two cloud data sources — no local
network access to the gateway required:

1. **MQTT live stream** — protobuf power data at ~1 message/second (solar, grid, battery, consumption, per-phase)
2. **Enlighten cloud API** — 20 endpoints polled on smart schedules (energy totals, battery health, device inventory, config)

Outputs InfluxDB line protocol to stdout. Designed for Telegraf's `execd` input
plugin, but works standalone too.

## Quick start

```bash
git clone https://github.com/mvalancy/enphase-telegraf.git
cd enphase-telegraf
./bin/setup        # creates venv, compiles proto, prompts for credentials, tests connection
```

That's it. Data flowing in ~30 seconds.

### Run standalone

```bash
./bin/enphase-telegraf --verbose
```

### Run with Telegraf

```bash
# Set credentials for Telegraf (see conf/telegraf-enphase.conf for options)
echo 'ENPHASE_EMAIL=you@example.com' | sudo tee -a /etc/default/telegraf
echo 'ENPHASE_PASSWORD=yourpassword' | sudo tee -a /etc/default/telegraf
sudo chmod 600 /etc/default/telegraf

# Install the Telegraf config
sudo cp conf/telegraf-enphase.conf /etc/telegraf/telegraf.d/enphase.conf
# Edit /etc/telegraf/telegraf.d/enphase.conf — set the command path and InfluxDB vars
sudo systemctl restart telegraf
```

## What it collects

### Real-time power (~1/sec from MQTT)

| Measurement | Fields | What it tells you |
|-------------|--------|-------------------|
| `enphase_power` | `solar_w`, `grid_w`, `consumption_w`, `battery_w`, `soc` | Instantaneous power flow + battery charge level |
| `enphase_power` | `solar_l1_w`, `solar_l2_w`, ... | Per-phase power (split-phase L1/L2) |
| `enphase_power` | `solar_va`, `grid_va`, ... | Apparent power (volt-amps) |
| `enphase_power` | `inverters_total`, `inverters_producing` | Microinverter fleet status |
| `enphase_power` | `grid_outage` | Grid outage detection (0/1) |

### Daily energy (every 5 min from cloud)

| Measurement | Fields | What it tells you |
|-------------|--------|-------------------|
| `enphase_energy` | `production_wh`, `consumption_wh` | Daily totals |
| `enphase_energy` | `solar_to_home_wh`, `solar_to_grid_wh`, `grid_to_home_wh`, ... | Where every watt-hour went today |
| `enphase_energy` | `lifetime_production_wh`, `lifetime_consumption_wh` | All-time totals |

### Battery health (every 2 min from cloud)

| Measurement | Fields | What it tells you |
|-------------|--------|-------------------|
| `enphase_battery` | `soc`, `available_energy_kwh`, `max_capacity_kwh` | Charge level and capacity |
| `enphase_battery` | `cycle_count_1`, `soh_1`, ... | Per-unit degradation tracking |
| `enphase_battery` | `estimated_backup_min` | How long battery lasts if grid fails |

### Configuration (on change only)

| Measurement | Fields | What it tells you |
|-------------|--------|-------------------|
| `enphase_config` | `battery_mode`, `grid_relay`, `backup_reserve_pct` | Operating mode, grid state, reserve setting |
| `enphase_config` | `charge_from_grid`, `storm_guard` | Charge policy and weather protection |
| `enphase_dry_contact` | `state`, `state_str` | Load control relay states (NC1/NC2/NO1/NO2) |

### System health

| Measurement | Fields | What it tells you |
|-------------|--------|-------------------|
| `enphase_inverters` | `total`, `not_reporting`, `error_count` | Microinverter fleet health |
| `enphase_gateway` | `wifi`, `cellular`, `alarm_count` | Gateway connectivity |
| `enphase_status` | `uptime_s`, `mqtt_connected`, `cloud_ok` | Collector health |
| `enphase_error` | `message`, `component` | Problems needing attention |

See [`docs/MEASUREMENT_TYPES.md`](docs/MEASUREMENT_TYPES.md) for the complete
field reference with units, sign conventions, value ranges, and physical
explanations.

## Sign conventions

| Positive (+) | Negative (−) |
|-------------|-------------|
| Solar producing | — |
| Grid importing (buying) | Grid exporting (selling) |
| Home consuming | — |
| Battery discharging | Battery charging |

## Using as a Python library

The `enphase_cloud` package works standalone for scripting and control:

```python
from enphase_cloud.enlighten import EnlightenClient
from enphase_cloud.livestream import LiveStreamClient

client = EnlightenClient("you@example.com", "your-password")
client.login()

# Read data
power = client.get_latest_power()
battery = client.get_battery_status()

# Control battery
client.set_battery_mode("self-consumption")
client.set_reserve_soc(20)
client.set_charge_from_grid(True)

# Stream live data (~1 msg/sec)
stream = LiveStreamClient(client)
stream.start("your-serial", on_data=lambda d: print(d))
```

See [`examples/`](examples/) for more.

## Project structure

```
bin/
  enphase-telegraf          Shell wrapper (sources .env, sets PYTHONPATH)
  setup                     One-time setup (venv, proto, credentials, test)
conf/
  telegraf-enphase.conf     Drop-in Telegraf config
src/
  enphase_telegraf.py       Telegraf entry point (line protocol to stdout)
  enphase_cloud/            Python package
    enlighten.py            Enlighten API (20 data getters + 6 control methods)
    livestream.py           MQTT protobuf stream (~1Hz real-time data)
    history.py              Historical data downloader
    proto/                  Compiled protobuf schemas
proto/                      Protobuf source files (.proto)
examples/                   Standalone usage scripts
docs/
  MEASUREMENT_TYPES.md      Complete InfluxDB field reference
requirements.txt            3 deps: requests, paho-mqtt, protobuf
```

## Requirements

- Python 3.10+
- Enphase Enlighten account (email + password)
- No MFA (disable in Enphase app if enabled)
- No local network access needed — works entirely via cloud

## How it works

```
Enlighten Cloud ──→ MQTT WebSocket ──→ protobuf decode ──→ InfluxDB line protocol
                ──→ REST API (20 endpoints) ──────────────→ InfluxDB line protocol
                                                                    ↓
                                                              stdout → Telegraf → InfluxDB
```

The MQTT stream provides ~1Hz real-time power data via AWS IoT WebSocket.
Sessions last 14 minutes and auto-reconnect. The cloud API fills in everything
else: daily energy totals, battery health, device inventory, and configuration.

## Legal notice

This project uses reverse-engineered Enphase APIs. It is not affiliated with or
endorsed by Enphase Energy, Inc. Use at your own risk. You are responsible for
ensuring your use complies with Enphase's Terms of Service and applicable laws.

## License

MIT
