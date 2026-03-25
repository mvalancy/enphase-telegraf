# Setup guide

Detailed walkthrough of every setup option. For the quick start, see the
[main README](../README.md).

## Prerequisites

- Linux server (Ubuntu/Debian tested; any systemd distro should work)
- Python 3.10+
- Enphase Enlighten account (email + password, MFA must be disabled)
- Tailscale (required if using `setup.sh` full-stack mode for network isolation)

## Option 1: Full interactive setup (`./setup.sh`)

The recommended path for a fresh server. One command does everything:

```bash
git clone https://github.com/mvalancy/enphase-telegraf.git
cd enphase-telegraf
./setup.sh
```

### What the setup script does

The script has an interactive console UI with system detection and a mode
selection menu:

**Mode 1: Full stack** (new server)
- Installs system packages (curl, python3, python3-venv)
- Runs `infra/scripts/setup-hub.sh` which installs InfluxDB, Grafana, and Telegraf
- Generates random admin passwords and API tokens for all services
- Saves all credentials to `~/monitoring-credentials.txt`
- Creates Python venv and compiles protobuf schemas
- Prompts for Enphase email and password
- Installs Telegraf input config pointing to this repo
- Injects Enphase credentials via systemd drop-in
- Runs a 15-second connection test
- Offers to start Telegraf
- Offers to backfill historical data

**Mode 2: App only** (Telegraf/InfluxDB already set up)
- Creates Python venv and compiles protobuf schemas
- Prompts for Enphase credentials
- Installs Telegraf input config (detects existing InfluxDB output config)
- Runs connection test

**CLI flags for non-interactive use:**

```bash
./setup.sh --full    # skip menu, install everything
./setup.sh --app     # skip menu, app only
```

### What gets installed where

| File | Purpose |
|------|---------|
| `./venv/` | Python virtual environment |
| `./.env` | Enphase credentials (mode 600) |
| `~/monitoring-credentials.txt` | All service passwords and tokens (mode 600) |
| `/etc/telegraf/telegraf.d/enphase.conf` | Telegraf input config |
| `/etc/systemd/system/telegraf.service.d/enphase.conf` | Enphase credentials for systemd |

### Idempotency

The script is safe to run multiple times:
- Existing venv is reused (dependencies updated)
- Compiled protobuf schemas are skipped if present
- Existing Enphase credentials in `.env` are reused
- Existing InfluxDB credentials (in `/etc/default/telegraf` or systemd drop-ins) are detected and not prompted for
- Telegraf config is overwritten (this is intentional — the path may have changed)

## Option 2: Python-only setup (`./bin/setup`)

For when you already have Telegraf and InfluxDB configured and just want the
Python collector:

```bash
./bin/setup
```

This does steps 1-4 of the full setup (venv, proto, credentials, test) but
does not touch Telegraf or InfluxDB configuration.

## Option 3: Manual setup

### Python environment

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Compile protobuf (optional — pre-compiled schemas are included)
venv/bin/pip install grpcio-tools
venv/bin/python3 -m grpc_tools.protoc \
    --proto_path=proto --python_out=src/enphase_cloud/proto \
    proto/DataMsg.proto proto/MeterSummaryData.proto proto/HemsStreamMessage.proto
```

### Credentials

Create a `.env` file:

```bash
cat > .env << 'EOF'
ENPHASE_EMAIL=you@example.com
ENPHASE_PASSWORD=yourpassword
EOF
chmod 600 .env
```

### Test standalone

```bash
./bin/enphase-telegraf --verbose
```

You should see line protocol on stdout and status messages on stderr within
15 seconds.

### Telegraf config

```bash
sudo cp conf/telegraf-enphase.conf /etc/telegraf/telegraf.d/enphase.conf
```

Edit the config to set the correct path:

```toml
[[inputs.execd]]
  command = ["/path/to/enphase-telegraf/bin/enphase-telegraf"]
  signal = "none"
  data_format = "influx"
  restart_delay = "30s"
```

Set credentials for Telegraf's systemd service:

```bash
sudo tee /etc/default/telegraf << 'EOF'
ENPHASE_EMAIL=you@example.com
ENPHASE_PASSWORD=yourpassword
INFLUXDB_URL=http://localhost:8086
INFLUXDB_TOKEN=your-token
INFLUXDB_ORG=your-org
INFLUXDB_BUCKET=enphase
EOF
sudo chmod 600 /etc/default/telegraf
sudo systemctl restart telegraf
```

## Infrastructure scripts

The `infra/` directory contains scripts adapted from
[valpatel-linux-tools](https://github.com/mvalancy/valpatel-linux-tools)
for automated InfluxDB + Grafana + Telegraf deployment.

### setup-hub.sh

Orchestrates a complete monitoring stack:

```bash
sudo bash infra/scripts/setup-hub.sh --org MyOrg --bucket enphase --retention 365
```

1. Runs `setup-collector.sh` (InfluxDB)
2. Installs and configures Grafana (bound to Tailscale IP)
3. Provisions InfluxDB as a Grafana data source
4. Runs `setup-agent.sh` (Telegraf for system metrics)

Requires Tailscale — services bind to the Tailscale IP for network isolation
(not exposed on public interfaces).

### setup-collector.sh

InfluxDB only:

```bash
sudo bash infra/scripts/setup-collector.sh --org MyOrg --bucket enphase --retention 365
```

- Downloads InfluxDB `.deb` (pinned version)
- Configures bind address to Tailscale IP
- Initializes via HTTP API (admin user, random password, org, bucket)
- Creates a scoped Telegraf write token (not the admin token)
- Saves all credentials to `~/monitoring-credentials.txt`

### setup-agent.sh

Telegraf only (for remote servers shipping metrics to a central InfluxDB):

```bash
sudo bash infra/scripts/setup-agent.sh \
    --url http://influxdb-host:8086 \
    --token "telegraf-write-token" \
    --org MyOrg --bucket servers
```

- Downloads Telegraf `.deb` (pinned version)
- Deploys config from template (system metrics: CPU, mem, disk, net)
- Stores token in systemd drop-in (not in config file)

## Historical data backfill

After setup, you can backfill InfluxDB with your full solar production history:

```bash
./bin/load-history
```

Interactive mode with progress bars. Options:

```bash
./bin/load-history --start 2023-01-15    # specific start date
./bin/load-history --stdout              # pipe to influx write CLI
./bin/load-history --dry-run             # download + convert, don't write
./bin/load-history --convert-only        # skip download, convert cached files
./bin/load-history --delay 10            # faster downloads (default 30s between requests)
```

The download is resumable. Cached JSON files are stored in `.cache/history/`
and skipped on subsequent runs.

Historical data uses `source=history` and `source=history_daily` tags.

## Environment variables reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ENPHASE_EMAIL` | Yes | | Enlighten account email |
| `ENPHASE_PASSWORD` | Yes | | Enlighten account password |
| `ENPHASE_SERIAL` | No | Auto-discovered | Gateway serial number |
| `INFLUXDB_URL` | For Telegraf | `http://localhost:8086` | InfluxDB URL |
| `INFLUXDB_TOKEN` | For Telegraf | | InfluxDB API token |
| `INFLUXDB_ORG` | For Telegraf | | InfluxDB organization |
| `INFLUXDB_BUCKET` | For Telegraf | | InfluxDB bucket |

## Troubleshooting

**No data flowing:**
```bash
./bin/enphase-telegraf --verbose    # check stderr for errors
journalctl -u telegraf -f          # check Telegraf logs
```

**Auth errors:**
- Verify email/password work at https://enlighten.enphaseenergy.com
- Disable MFA on your Enphase account (not supported)
- Check `.env` has no trailing spaces or quotes around values

**MQTT not connecting:**
- MQTT uses WebSocket over port 443 — ensure outbound HTTPS is allowed
- The stream requires a valid Enlighten session — check cloud auth first

**InfluxDB write failures:**
- Check token has write permission to the bucket
- Check org name matches exactly (case-sensitive)
- Check InfluxDB is accessible from the Telegraf host
