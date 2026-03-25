#!/bin/bash
# Set up a complete monitoring hub: InfluxDB + Grafana + Telegraf
# Usage: sudo bash setup-hub.sh [--org ORG] [--bucket BUCKET] [--retention DAYS]
# Example: sudo bash setup-hub.sh --org Valpatel --bucket servers --retention 30
#
# This script orchestrates:
#   1. InfluxDB collector (via setup-collector.sh)
#   2. Grafana dashboard (installed and configured here)
#   3. Telegraf agent for self-monitoring (via setup-agent.sh)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../templates"

INFLUX_ORG=""
INFLUX_BUCKET=""
RETENTION_DAYS="30"

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --org)       INFLUX_ORG="$2";      shift 2 ;;
        --bucket)    INFLUX_BUCKET="$2";    shift 2 ;;
        --retention) RETENTION_DAYS="$2";   shift 2 ;;
        --help|-h)
            echo "Usage: sudo bash setup-hub.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --org ORG          InfluxDB organization name"
            echo "  --bucket BUCKET    Initial bucket name [servers]"
            echo "  --retention DAYS   Data retention in days [30]"
            echo "  --help             Show this help"
            echo ""
            echo "Sets up InfluxDB + Grafana + Telegraf on this server."
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Require Tailscale
if ! command -v tailscale &> /dev/null; then
    echo "Error: Tailscale is not installed."
    echo "Install Tailscale first: https://tailscale.com/download/linux"
    exit 1
fi

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null)
if [ -z "$TAILSCALE_IP" ]; then
    echo "Error: Could not detect Tailscale IPv4 address."
    exit 1
fi

# Prompt for missing values
if [ -z "$INFLUX_ORG" ]; then
    read -p "Organization name: " INFLUX_ORG
fi
if [ -z "$INFLUX_BUCKET" ]; then
    read -p "Initial bucket name [servers]: " INFLUX_BUCKET
    INFLUX_BUCKET="${INFLUX_BUCKET:-servers}"
fi

if [ -z "$INFLUX_ORG" ]; then
    echo "Error: Organization name is required."
    exit 1
fi

CREDS_DIR="$(eval echo ~"${SUDO_USER:-root}")"
CREDS_FILE="$CREDS_DIR/monitoring-credentials.txt"

echo "============================================"
echo "Setting up Monitoring Hub"
echo "============================================"
echo "  Tailscale IP:  $TAILSCALE_IP"
echo "  Organization:  $INFLUX_ORG"
echo "  Bucket:        $INFLUX_BUCKET"
echo "  Retention:     ${RETENTION_DAYS} days"
echo ""
echo "Components:"
echo "  1. InfluxDB   → http://${TAILSCALE_IP}:8086"
echo "  2. Grafana    → http://${TAILSCALE_IP}:3000"
echo "  3. Telegraf   → self-monitoring agent"
echo ""

# ── Step 1: InfluxDB ─────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Phase 1: InfluxDB Collector"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
bash "$SCRIPT_DIR/setup-collector.sh" \
    --org "$INFLUX_ORG" \
    --bucket "$INFLUX_BUCKET" \
    --retention "$RETENTION_DAYS" \
    --creds-dir "$CREDS_DIR"

# Read tokens from credentials file
ADMIN_TOKEN=$(grep -A1 "Admin API Token" "$CREDS_FILE" | tail -1)
TELEGRAF_TOKEN=$(grep -A1 "Telegraf Token" "$CREDS_FILE" | tail -1)

# ── Step 2: Grafana ──────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Phase 2: Grafana Dashboard"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Install Grafana
echo "[1/4] Installing Grafana..."
if command -v grafana-server &> /dev/null; then
    echo "  Grafana already installed."
else
    if [ ! -f /etc/apt/keyrings/grafana.gpg ]; then
        curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg 2>/dev/null
        echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
            > /etc/apt/sources.list.d/grafana.list
    fi
    apt-get update -qq
    apt-get install -y grafana
fi

# Configure Grafana bind address
echo "[2/4] Configuring Grafana to bind to ${TAILSCALE_IP}:3000..."
GRAFANA_PASS=$(openssl rand -base64 24)

sed -i "s/^;http_addr =.*$/http_addr = ${TAILSCALE_IP}/" /etc/grafana/grafana.ini
sed -i "s/^http_addr =.*$/http_addr = ${TAILSCALE_IP}/" /etc/grafana/grafana.ini
sed -i "s/^;http_port = 3000$/http_port = 3000/" /etc/grafana/grafana.ini
sed -i "s/^;admin_password = admin$/admin_password = ${GRAFANA_PASS}/" /etc/grafana/grafana.ini
sed -i "s/^admin_password = .*$/admin_password = ${GRAFANA_PASS}/" /etc/grafana/grafana.ini

# Provision InfluxDB data source
echo "[3/4] Provisioning InfluxDB data source..."
sed -e "s|__INFLUXDB_URL__|http://${TAILSCALE_IP}:8086|g" \
    -e "s|__INFLUXDB_ORG__|${INFLUX_ORG}|g" \
    -e "s|__INFLUXDB_BUCKET__|${INFLUX_BUCKET}|g" \
    -e "s|__INFLUXDB_TOKEN__|${ADMIN_TOKEN}|g" \
    "$TEMPLATE_DIR/grafana-datasource.yaml.template" > /etc/grafana/provisioning/datasources/influxdb.yaml
chmod 640 /etc/grafana/provisioning/datasources/influxdb.yaml
chown root:grafana /etc/grafana/provisioning/datasources/influxdb.yaml

# Start Grafana
echo "[4/4] Starting Grafana..."
systemctl enable grafana-server
systemctl restart grafana-server

# Append Grafana credentials to the file
cat >> "$CREDS_FILE" << EOF

--- Grafana ---
URL:      http://${TAILSCALE_IP}:3000
Username: admin
Password: ${GRAFANA_PASS}
EOF

# ── Step 3: Telegraf self-monitoring ─────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Phase 3: Telegraf Self-Monitoring"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
bash "$SCRIPT_DIR/setup-agent.sh" \
    --url "http://${TAILSCALE_IP}:8086" \
    --token "$TELEGRAF_TOKEN" \
    --org "$INFLUX_ORG" \
    --bucket "$INFLUX_BUCKET"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "============================================"
echo "Monitoring Hub Ready!"
echo "============================================"
echo ""
echo "  InfluxDB:  http://${TAILSCALE_IP}:8086"
echo "  Grafana:   http://${TAILSCALE_IP}:3000"
echo "  Telegraf:  shipping $(hostname) metrics"
echo ""
echo "  Credentials: $CREDS_FILE"
echo ""
echo "Next steps:"
echo "  1. Open Grafana at http://${TAILSCALE_IP}:3000"
echo "     Login: admin / (see credentials file)"
echo ""
echo "  2. Deploy Telegraf to other servers:"
echo "     sudo bash setup-agent.sh --url http://${TAILSCALE_IP}:8086 \\"
echo "       --token \"<telegraf-token>\" --org ${INFLUX_ORG} --bucket ${INFLUX_BUCKET}"
echo ""
echo "  3. Create a Grafana dashboard for server metrics"
echo ""
