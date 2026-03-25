#!/bin/bash
# Install and initialize InfluxDB 2.x, binding to the Tailscale IP
# Usage: sudo bash setup-collector.sh [--org ORG] [--bucket BUCKET] [--retention DAYS]
# Example: sudo bash setup-collector.sh --org Valpatel --bucket servers --retention 30

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../templates"

INFLUXDB_VERSION="2.7.12-3"
INFLUX_ORG=""
INFLUX_BUCKET=""
RETENTION_DAYS="30"
CREDS_DIR=""

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
        --creds-dir) CREDS_DIR="$2";       shift 2 ;;
        --help|-h)
            echo "Usage: sudo bash setup-collector.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --org ORG          InfluxDB organization name"
            echo "  --bucket BUCKET    Initial bucket name [servers]"
            echo "  --retention DAYS   Data retention in days [30]"
            echo "  --creds-dir DIR    Where to save credentials [caller's home]"
            echo "  --help             Show this help"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Require Tailscale
if ! command -v tailscale &> /dev/null; then
    echo "Error: Tailscale is not installed."
    echo "InfluxDB binds to the Tailscale IP for network isolation."
    echo "Install Tailscale first: https://tailscale.com/download/linux"
    exit 1
fi

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null)
if [ -z "$TAILSCALE_IP" ]; then
    echo "Error: Could not detect Tailscale IPv4 address."
    echo "Is Tailscale connected? Run: tailscale status"
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

# Determine credentials save location
if [ -z "$CREDS_DIR" ]; then
    # Use the home directory of the user who invoked sudo
    CREDS_DIR="$(eval echo ~"${SUDO_USER:-root}")"
fi
CREDS_FILE="$CREDS_DIR/monitoring-credentials.txt"

echo "============================================"
echo "Setting up InfluxDB Collector"
echo "============================================"
echo "  Tailscale IP:  $TAILSCALE_IP"
echo "  Organization:  $INFLUX_ORG"
echo "  Bucket:        $INFLUX_BUCKET"
echo "  Retention:     ${RETENTION_DAYS} days"
echo "  Credentials:   $CREDS_FILE"
echo ""

# Step 1: Install InfluxDB
echo "[1/5] Installing InfluxDB ${INFLUXDB_VERSION}..."
if command -v influxd &> /dev/null; then
    echo "  InfluxDB already installed: $(influxd version 2>/dev/null | head -1)"
else
    ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$ARCH" in
        amd64|x86_64) DEB_ARCH="amd64" ;;
        arm64|aarch64) DEB_ARCH="arm64" ;;
        *) echo "Error: Unsupported architecture: $ARCH"; exit 1 ;;
    esac

    wget -q "https://dl.influxdata.com/influxdb/releases/influxdb2_${INFLUXDB_VERSION}_${DEB_ARCH}.deb" -O /tmp/influxdb2.deb
    dpkg -i /tmp/influxdb2.deb
    rm /tmp/influxdb2.deb
fi

# Step 2: Configure bind address
echo "[2/5] Configuring InfluxDB to bind to $TAILSCALE_IP:8086..."
sed -e "s|__TAILSCALE_IP__|${TAILSCALE_IP}|g" \
    "$TEMPLATE_DIR/influxdb.toml.template" > /etc/influxdb/config.toml

# Step 3: Start InfluxDB
echo "[3/5] Starting InfluxDB..."
systemctl enable influxdb
systemctl restart influxdb

# Wait for InfluxDB to be ready
echo "  Waiting for InfluxDB..."
for _i in $(seq 1 30); do
    if curl -s "http://${TAILSCALE_IP}:8086/health" | grep -q '"status":"pass"'; then
        break
    fi
    sleep 1
done

if ! curl -s "http://${TAILSCALE_IP}:8086/health" | grep -q '"status":"pass"'; then
    echo "Error: InfluxDB failed to start. Check logs:"
    echo "  journalctl -u influxdb --no-pager -n 20"
    exit 1
fi

# Step 4: Initialize via API
echo "[4/5] Initializing InfluxDB..."

# Check if already set up
SETUP_ALLOWED=$(curl -s "http://${TAILSCALE_IP}:8086/api/v2/setup" | python3 -c "import sys,json; print(json.load(sys.stdin).get('allowed', False))")

if [ "$SETUP_ALLOWED" = "True" ]; then
    # Generate admin password
    ADMIN_PASS=$(openssl rand -base64 24)

    RETENTION_SECONDS=$((RETENTION_DAYS * 86400))

    SETUP_RESPONSE=$(curl -s -X POST "http://${TAILSCALE_IP}:8086/api/v2/setup" \
        -H "Content-Type: application/json" \
        -d "{
            \"username\": \"admin\",
            \"password\": \"${ADMIN_PASS}\",
            \"org\": \"${INFLUX_ORG}\",
            \"bucket\": \"${INFLUX_BUCKET}\",
            \"retentionPeriodSeconds\": ${RETENTION_SECONDS}
        }")

    ADMIN_TOKEN=$(echo "$SETUP_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['auth']['token'])")
    BUCKET_ID=$(echo "$SETUP_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['bucket']['id'])")
    ORG_ID=$(echo "$SETUP_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['org']['id'])")

    echo "  Admin user created."
else
    echo "  InfluxDB is already initialized."
    echo "  To reconfigure, delete /var/lib/influxdb/influxd.bolt and re-run."
    echo ""
    echo "============================================"
    echo "InfluxDB is running at http://${TAILSCALE_IP}:8086"
    echo "============================================"
    exit 0
fi

# Step 5: Create scoped Telegraf write token
echo "[5/5] Creating Telegraf write token..."
TELEGRAF_RESPONSE=$(curl -s -X POST "http://${TAILSCALE_IP}:8086/api/v2/authorizations" \
    -H "Authorization: Token ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{
        \"description\": \"Telegraf agent (write to ${INFLUX_BUCKET})\",
        \"orgID\": \"${ORG_ID}\",
        \"permissions\": [
            {\"action\": \"write\", \"resource\": {\"type\": \"buckets\", \"id\": \"${BUCKET_ID}\", \"orgID\": \"${ORG_ID}\"}},
            {\"action\": \"read\",  \"resource\": {\"type\": \"buckets\", \"id\": \"${BUCKET_ID}\", \"orgID\": \"${ORG_ID}\"}}
        ]
    }")

TELEGRAF_TOKEN=$(echo "$TELEGRAF_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Save credentials
cat > "$CREDS_FILE" << EOF
=== Monitoring Stack Credentials ===
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Accessible only via Tailscale ($TAILSCALE_IP)

--- InfluxDB ---
URL:      http://${TAILSCALE_IP}:8086
Username: admin
Password: ${ADMIN_PASS}
Org:      ${INFLUX_ORG}
Bucket:   ${INFLUX_BUCKET}

--- Admin API Token (all-access) ---
${ADMIN_TOKEN}

--- Telegraf Token (write to ${INFLUX_BUCKET} bucket only) ---
${TELEGRAF_TOKEN}
EOF
chmod 600 "$CREDS_FILE"
if [ -n "$SUDO_USER" ]; then
    chown "$SUDO_USER":"$SUDO_USER" "$CREDS_FILE"
fi

echo ""
echo "============================================"
echo "InfluxDB collector running!"
echo "============================================"
echo ""
echo "  URL:          http://${TAILSCALE_IP}:8086"
echo "  Credentials:  $CREDS_FILE"
echo ""
echo "To configure Telegraf agents on remote servers, use:"
echo ""
echo "  sudo bash setup-agent.sh \\"
echo "    --url http://${TAILSCALE_IP}:8086 \\"
echo "    --token \"$(echo "$TELEGRAF_TOKEN" | head -c 20)...\" \\"
echo "    --org ${INFLUX_ORG} \\"
echo "    --bucket ${INFLUX_BUCKET}"
echo ""
echo "(Full token is in $CREDS_FILE)"
echo ""
