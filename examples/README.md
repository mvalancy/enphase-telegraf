# Examples

Standalone scripts using the `enphase_cloud` library.

## Setup

```bash
# From the repo root:
pip install -r requirements.txt

# Or manually:
pip install requests paho-mqtt protobuf
```

## Available Scripts

| Script | Description |
|--------|-------------|
| `mqtt_to_stdout.py` | Print live power data to terminal (~1 line/sec) |
| `mqtt_to_influxdb.py` | Stream MQTT data directly to InfluxDB (no Telegraf) |
| `cloud_scrape.py` | Fetch all 20 cloud endpoints, dump to JSON files |
| `battery_control.py` | Set battery mode, reserve, charge-from-grid via cloud API |

## Using as a Library

```python
from enphase_cloud.enlighten import EnlightenClient
from enphase_cloud.livestream import LiveStreamClient

# Login
client = EnlightenClient("you@example.com", "your-password")
client.login()
print(f"Site ID: {client._session.site_id}")

# Read cloud data
power = client.get_latest_power()
battery = client.get_battery_status()
devices = client.get_devices()

# Control battery
client.set_battery_mode("self-consumption")
client.set_reserve_soc(20)

# Stream live data (~1 msg/sec)
stream = LiveStreamClient(client)
stream.start("your-serial", on_data=lambda d: print(d))
```

Run examples from the repo root with PYTHONPATH set:

```bash
export ENPHASE_EMAIL=you@example.com
export ENPHASE_PASSWORD=yourpassword
PYTHONPATH=src python3 examples/mqtt_to_stdout.py
```
