#!/bin/bash
# Install and configure Telegraf to ship metrics to a remote InfluxDB
# Usage: sudo bash setup-agent.sh [--url URL] [--token TOKEN] [--org ORG] [--bucket BUCKET]
# Example: sudo bash setup-agent.sh --url http://vps-1:8086 --token "abc..." --org Valpatel --bucket servers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../templates"

TELEGRAF_VERSION="1.33.1-1"
INFLUXDB_URL=""
INFLUXDB_TOKEN=""
INFLUXDB_ORG=""
INFLUXDB_BUCKET=""

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --url)     INFLUXDB_URL="$2";    shift 2 ;;
        --token)   INFLUXDB_TOKEN="$2";  shift 2 ;;
        --org)     INFLUXDB_ORG="$2";    shift 2 ;;
        --bucket)  INFLUXDB_BUCKET="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: sudo bash setup-agent.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --url URL        InfluxDB URL (e.g., http://vps-1:8086)"
            echo "  --token TOKEN    InfluxDB write token"
            echo "  --org ORG        InfluxDB organization"
            echo "  --bucket BUCKET  InfluxDB bucket"
            echo "  --help           Show this help"
            echo ""
            echo "If options are not provided, you will be prompted interactively."
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Prompt for missing values
if [ -z "$INFLUXDB_URL" ]; then
    read -p "InfluxDB URL (e.g., http://vps-1:8086): " INFLUXDB_URL
fi
if [ -z "$INFLUXDB_TOKEN" ]; then
    read -s -p "InfluxDB write token: " INFLUXDB_TOKEN
    echo
fi
if [ -z "$INFLUXDB_ORG" ]; then
    read -p "InfluxDB organization: " INFLUXDB_ORG
fi
if [ -z "$INFLUXDB_BUCKET" ]; then
    read -p "InfluxDB bucket [servers]: " INFLUXDB_BUCKET
    INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-servers}"
fi

if [ -z "$INFLUXDB_URL" ] || [ -z "$INFLUXDB_TOKEN" ] || [ -z "$INFLUXDB_ORG" ]; then
    echo "Error: URL, token, and org are required."
    exit 1
fi

echo "============================================"
echo "Setting up Telegraf Agent"
echo "============================================"
echo "  InfluxDB:     $INFLUXDB_URL"
echo "  Organization: $INFLUXDB_ORG"
echo "  Bucket:       $INFLUXDB_BUCKET"
echo "  Hostname:     $(hostname)"
echo ""

# Step 1: Install Telegraf
echo "[1/4] Installing Telegraf ${TELEGRAF_VERSION}..."
if command -v telegraf &> /dev/null; then
    echo "  Telegraf already installed: $(telegraf --version)"
else
    ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$ARCH" in
        amd64|x86_64) DEB_ARCH="amd64" ;;
        arm64|aarch64) DEB_ARCH="arm64" ;;
        *) echo "Error: Unsupported architecture: $ARCH"; exit 1 ;;
    esac

    wget -q "https://dl.influxdata.com/telegraf/releases/telegraf_${TELEGRAF_VERSION}_${DEB_ARCH}.deb" -O /tmp/telegraf.deb
    dpkg -i /tmp/telegraf.deb
    rm /tmp/telegraf.deb
    echo "  Installed: $(telegraf --version)"
fi

# Step 2: Deploy config from template
echo "[2/4] Deploying Telegraf config..."
if [ -f /etc/telegraf/telegraf.conf ]; then
    cp /etc/telegraf/telegraf.conf /etc/telegraf/telegraf.conf.bak
    echo "  Backed up existing config to telegraf.conf.bak"
fi

sed -e "s|__INFLUXDB_URL__|${INFLUXDB_URL}|g" \
    -e "s|__INFLUXDB_ORG__|${INFLUXDB_ORG}|g" \
    -e "s|__INFLUXDB_BUCKET__|${INFLUXDB_BUCKET}|g" \
    "$TEMPLATE_DIR/telegraf-agent.conf.template" > /etc/telegraf/telegraf.conf

# Step 3: Store token in systemd drop-in (not in config file)
echo "[3/4] Storing token in systemd environment..."
mkdir -p /etc/systemd/system/telegraf.service.d
cat > /etc/systemd/system/telegraf.service.d/token.conf << EOF
[Service]
Environment="INFLUX_TOKEN=${INFLUXDB_TOKEN}"
EOF
chmod 600 /etc/systemd/system/telegraf.service.d/token.conf
systemctl daemon-reload

# Step 4: Enable and start
echo "[4/4] Starting Telegraf..."
systemctl enable telegraf
systemctl restart telegraf

# Verify
sleep 2
if systemctl is-active telegraf &>/dev/null; then
    echo ""
    echo "============================================"
    echo "Telegraf agent running!"
    echo "============================================"
    echo ""
    echo "  Config:   /etc/telegraf/telegraf.conf"
    echo "  Token:    /etc/systemd/system/telegraf.service.d/token.conf"
    echo "  Shipping: $(hostname) → $INFLUXDB_URL ($INFLUXDB_ORG/$INFLUXDB_BUCKET)"
    echo ""
    echo "Useful commands:"
    echo "  systemctl status telegraf      # Check status"
    echo "  journalctl -u telegraf -f      # Tail logs"
    echo "  telegraf --test                 # Dry-run metrics collection"
    echo ""
else
    echo ""
    echo "Error: Telegraf failed to start. Check logs:"
    echo "  journalctl -u telegraf --no-pager -n 20"
    exit 1
fi
