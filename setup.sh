#!/usr/bin/env bash
#
# setup.sh — Interactive setup for enphase-telegraf
#
# Usage:
#   ./setup.sh            Interactive mode (recommended)
#   ./setup.sh --full     Skip menu, install everything
#   ./setup.sh --app      Skip menu, app-only (no InfluxDB/Grafana/Telegraf)
#

set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_SCRIPTS="$REPO_DIR/infra/scripts"
CREDS_FILE="$HOME/monitoring-credentials.txt"
TELEGRAF_ENV="/etc/default/telegraf"

# ── Colors & Symbols ────────────────────────────
if [ -t 1 ]; then
    BOLD='\033[1m'
    DIM='\033[2m'
    RESET='\033[0m'
    RED='\033[31m'
    GREEN='\033[32m'
    YELLOW='\033[33m'
    BLUE='\033[34m'
    MAGENTA='\033[35m'
    CYAN='\033[36m'
    WHITE='\033[37m'
    BG_BLUE='\033[44m'
    BG_GREEN='\033[42m'
    BG_RED='\033[41m'
else
    BOLD='' DIM='' RESET='' RED='' GREEN='' YELLOW=''
    BLUE='' MAGENTA='' CYAN='' WHITE='' BG_BLUE='' BG_GREEN='' BG_RED=''
fi

SYM_CHECK="${GREEN}${BOLD}[*]${RESET}"
SYM_ARROW="${CYAN}${BOLD} > ${RESET}"
SYM_OK="${GREEN} + ${RESET}"
SYM_WARN="${YELLOW} ! ${RESET}"
SYM_FAIL="${RED} x ${RESET}"
SYM_DOT="${DIM} . ${RESET}"
SYM_RADIO_ON="${CYAN}${BOLD}(*)${RESET}"
SYM_RADIO_OFF="${DIM}( )${RESET}"

# ── Helper functions ─────────────────────────────
step()    { echo -e "\n${BLUE}${BOLD}--- $* ---${RESET}"; }
ok()      { echo -e "${SYM_OK}$*"; }
warn()    { echo -e "${SYM_WARN}${YELLOW}$*${RESET}"; }
fail()    { echo -e "${SYM_FAIL}${RED}$*${RESET}" >&2; }
dim()     { echo -e "${SYM_DOT}${DIM}$*${RESET}"; }

confirm() {
    local prompt="$1" default="${2:-Y}"
    local yn
    if [ "$default" = "Y" ]; then
        read -rp "$(echo -e "${SYM_ARROW}${BOLD}$prompt${RESET} ${DIM}[Y/n]${RESET} ")" yn
    else
        read -rp "$(echo -e "${SYM_ARROW}${BOLD}$prompt${RESET} ${DIM}[y/N]${RESET} ")" yn
    fi
    yn="${yn:-$default}"
    [[ "$yn" =~ ^[Yy]$ ]]
}

prompt_val() {
    local label="$1" default="$2" varname="$3"
    if [ -n "$default" ]; then
        read -rp "$(echo -e "${SYM_ARROW}${label} ${DIM}[$default]${RESET} ")" "$varname"
        eval "$varname=\"\${$varname:-$default}\""
    else
        read -rp "$(echo -e "${SYM_ARROW}${label} ")" "$varname"
    fi
}

prompt_secret() {
    local label="$1" varname="$2"
    read -rsp "$(echo -e "${SYM_ARROW}${label} ")" "$varname"
    echo
}

spinner() {
    local pid=$1 label="$2"
    local frames=('   ' '.  ' '.. ' '...')
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r${SYM_DOT}${DIM}%s%s${RESET}" "$label" "${frames[$((i % 4))]}"
        sleep 0.3
        i=$((i + 1))
    done
    wait "$pid" 2>/dev/null
    local rc=$?
    printf "\r"
    return $rc
}

# ── System detection ─────────────────────────────
detect_system() {
    HAS_PYTHON=false
    HAS_VENV=false
    HAS_CURL=false
    HAS_TELEGRAF=false
    HAS_INFLUXDB=false
    HAS_GRAFANA=false
    HAS_TAILSCALE=false
    HAS_ENPHASE_CREDS=false
    HAS_INFLUX_CREDS=false
    HAS_APP_VENV=false
    HAS_PROTO=false

    command -v python3 &>/dev/null && HAS_PYTHON=true
    python3 -c "import venv" &>/dev/null 2>&1 && HAS_VENV=true
    command -v curl &>/dev/null && HAS_CURL=true
    command -v telegraf &>/dev/null && HAS_TELEGRAF=true
    (command -v influxd &>/dev/null || command -v influx &>/dev/null) && HAS_INFLUXDB=true
    command -v grafana-server &>/dev/null && HAS_GRAFANA=true
    command -v tailscale &>/dev/null && HAS_TAILSCALE=true
    [ -d "$REPO_DIR/venv" ] && HAS_APP_VENV=true
    [ -f "$REPO_DIR/src/enphase_cloud/proto/DataMsg_pb2.py" ] && HAS_PROTO=true

    if [ -f "$REPO_DIR/.env" ]; then
        local email
        email=$(grep "^ENPHASE_EMAIL=" "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2)
        [ -n "$email" ] && [ "$email" != "you@example.com" ] && HAS_ENPHASE_CREDS=true
    fi

    # Check for InfluxDB credentials in various places
    if [ -f "$CREDS_FILE" ]; then
        local tok
        tok=$(grep -A1 "Telegraf Token" "$CREDS_FILE" 2>/dev/null | tail -1 || true)
        [ -n "$tok" ] && [[ ! "$tok" =~ ^--- ]] && HAS_INFLUX_CREDS=true
    fi
    if [ "$HAS_INFLUX_CREDS" = false ] && [ -f "$TELEGRAF_ENV" ]; then
        (grep -q "^INFLUX_TOKEN=" "$TELEGRAF_ENV" 2>/dev/null || \
         grep -q "^INFLUXDB_TOKEN=" "$TELEGRAF_ENV" 2>/dev/null) && HAS_INFLUX_CREDS=true
    fi
    if [ "$HAS_INFLUX_CREDS" = false ] && [ -f /etc/systemd/system/telegraf.service.d/token.conf ]; then
        grep -q "INFLUX_TOKEN=" /etc/systemd/system/telegraf.service.d/token.conf 2>/dev/null && HAS_INFLUX_CREDS=true
    fi
}

print_status_line() {
    local label="$1" installed="$2"
    if [ "$installed" = true ]; then
        echo -e "    ${GREEN}*${RESET} ${label}  ${DIM}installed${RESET}"
    else
        echo -e "    ${RED}*${RESET} ${label}  ${YELLOW}not found${RESET}"
    fi
}

# ══════════════════════════════════════════════════
# ── BANNER ───────────────────────────────────────
# ══════════════════════════════════════════════════
clear 2>/dev/null || true
echo
echo -e "${YELLOW}${BOLD}"
cat << 'BANNER'
                  .  *  .
               *  .__|__.  *
            .   ./  /|\  \.   .
          *   ./___/ | \___\.   *
        .   ./  _____|_____  \.   .
      *   ./___|_____|_____|___\.   *
          |____|_____|_____|____|
BANNER
echo -e "${RESET}"
echo -e "${BOLD}${WHITE}        enphase-telegraf  ${DIM}v1.0${RESET}"
echo -e "${DIM}     Real-time Enphase solar + battery"
echo -e "       monitoring via Telegraf + InfluxDB${RESET}"
echo
echo -e "${DIM}  ─────────────────────────────────────────${RESET}"

# ══════════════════════════════════════════════════
# ── DETECT ───────────────────────────────────────
# ══════════════════════════════════════════════════
echo
echo -e "  ${BOLD}Scanning system...${RESET}"
echo
detect_system

echo -e "  ${BOLD}Infrastructure:${RESET}"
print_status_line "InfluxDB " "$HAS_INFLUXDB"
print_status_line "Telegraf " "$HAS_TELEGRAF"
print_status_line "Grafana  " "$HAS_GRAFANA"
print_status_line "Tailscale" "$HAS_TAILSCALE"
echo
echo -e "  ${BOLD}Application:${RESET}"
print_status_line "Python 3 " "$HAS_PYTHON"
print_status_line "Venv     " "$HAS_APP_VENV"
print_status_line "Protobuf " "$HAS_PROTO"
echo
echo -e "  ${BOLD}Credentials:${RESET}"
print_status_line "Enphase  " "$HAS_ENPHASE_CREDS"
print_status_line "InfluxDB " "$HAS_INFLUX_CREDS"

echo
echo -e "  ${DIM}─────────────────────────────────────────${RESET}"

# ══════════════════════════════════════════════════
# ── CHOOSE MODE ──────────────────────────────────
# ══════════════════════════════════════════════════

INSTALL_INFRA=false
INSTALL_APP=true

# Handle CLI flags
case "${1:-}" in
    --full) INSTALL_INFRA=true; INSTALL_APP=true ;;
    --app)  INSTALL_INFRA=false; INSTALL_APP=true ;;
    *)
        echo
        echo -e "  ${BOLD}What would you like to set up?${RESET}"
        echo
        echo -e "    ${BOLD}${CYAN}1${RESET}  ${BOLD}Full stack${RESET}  ${DIM}(recommended for new servers)${RESET}"
        echo -e "       ${DIM}Installs InfluxDB, Grafana, Telegraf, and the"
        echo -e "       Enphase collector. Generates all passwords/tokens.${RESET}"
        echo
        echo -e "    ${BOLD}${CYAN}2${RESET}  ${BOLD}App only${RESET}  ${DIM}(Telegraf/InfluxDB already set up)${RESET}"
        echo -e "       ${DIM}Python venv, protobuf, Enphase credentials,"
        echo -e "       and Telegraf input config only.${RESET}"
        echo
        echo -e "    ${BOLD}${CYAN}3${RESET}  ${BOLD}Exit${RESET}"
        echo

        while true; do
            read -rp "$(echo -e "${SYM_ARROW}${BOLD}Choose [1/2/3]:${RESET} ")" CHOICE
            case "$CHOICE" in
                1) INSTALL_INFRA=true;  INSTALL_APP=true;  break ;;
                2) INSTALL_INFRA=false; INSTALL_APP=true;  break ;;
                3) echo; echo -e "  ${DIM}Bye!${RESET}"; echo; exit 0 ;;
                *) echo -e "${SYM_WARN}Enter 1, 2, or 3" ;;
            esac
        done
        ;;
esac

# ══════════════════════════════════════════════════
# ── CONFIRM PLAN ─────────────────────────────────
# ══════════════════════════════════════════════════
echo
echo -e "  ${DIM}─────────────────────────────────────────${RESET}"
echo
echo -e "  ${BOLD}Here's what will happen:${RESET}"
echo

STEP_NUM=0

if [ "$INSTALL_INFRA" = true ]; then
    if [ "$HAS_INFLUX_CREDS" = true ]; then
        echo -e "    ${DIM}~${RESET} ${DIM}InfluxDB + Grafana + Telegraf (already configured)${RESET}"
    else
        STEP_NUM=$((STEP_NUM + 1))
        echo -e "    ${CYAN}$STEP_NUM${RESET}  Install ${BOLD}InfluxDB${RESET} + ${BOLD}Grafana${RESET} + ${BOLD}Telegraf${RESET}"
        echo -e "       ${DIM}Downloads packages, creates admin user with random"
        echo -e "       password, generates API tokens, configures Grafana${RESET}"
        if [ "$HAS_TAILSCALE" = false ]; then
            echo -e "       ${YELLOW}! Tailscale required — install it first${RESET}"
        fi
    fi
    echo
fi

STEP_NUM=$((STEP_NUM + 1))
if [ "$HAS_APP_VENV" = true ] && [ "$HAS_PROTO" = true ]; then
    echo -e "    ${DIM}~${RESET} ${DIM}Python venv + protobuf (already done)${RESET}"
else
    echo -e "    ${CYAN}$STEP_NUM${RESET}  Set up ${BOLD}Python venv${RESET} + compile ${BOLD}protobuf${RESET}"
    echo -e "       ${DIM}Creates virtualenv, installs 3 pip packages${RESET}"
fi
echo

STEP_NUM=$((STEP_NUM + 1))
if [ "$HAS_ENPHASE_CREDS" = true ]; then
    local_email=$(grep "^ENPHASE_EMAIL=" "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2)
    echo -e "    ${DIM}~${RESET} ${DIM}Enphase credentials (using $local_email)${RESET}"
else
    echo -e "    ${CYAN}$STEP_NUM${RESET}  Prompt for ${BOLD}Enphase${RESET} email + password"
    echo -e "       ${DIM}Your Enlighten cloud login — stored in .env (mode 600)${RESET}"
fi
echo

STEP_NUM=$((STEP_NUM + 1))
echo -e "    ${CYAN}$STEP_NUM${RESET}  Install ${BOLD}Telegraf input config${RESET} + test connection"
echo -e "       ${DIM}Adds enphase-telegraf as a Telegraf input plugin,"
echo -e "       runs a 15-second connection test${RESET}"
echo

if ! confirm "Proceed?" "Y"; then
    echo
    echo -e "  ${DIM}Setup cancelled. Run ./setup.sh again when ready.${RESET}"
    echo
    exit 0
fi

# ══════════════════════════════════════════════════
# ── STEP: System packages ────────────────────────
# ══════════════════════════════════════════════════
step "System packages"

NEED_INSTALL=()
[ "$HAS_CURL" = false ]   && NEED_INSTALL+=(curl)
[ "$HAS_PYTHON" = false ]  && NEED_INSTALL+=(python3)
[ "$HAS_VENV" = false ]    && NEED_INSTALL+=(python3-venv)

if [ ${#NEED_INSTALL[@]} -gt 0 ]; then
    dim "Installing: ${NEED_INSTALL[*]}"
    sudo apt-get update -qq &>/dev/null
    sudo apt-get install -y -qq "${NEED_INSTALL[@]}" &>/dev/null
    ok "Installed ${NEED_INSTALL[*]}"
else
    ok "All base packages present"
fi

# ══════════════════════════════════════════════════
# ── STEP: Infrastructure ─────────────────────────
# ══════════════════════════════════════════════════
if [ "$INSTALL_INFRA" = true ] && [ "$HAS_INFLUX_CREDS" = false ]; then
    step "Monitoring stack (InfluxDB + Grafana + Telegraf)"

    if [ "$HAS_TAILSCALE" = false ]; then
        fail "Tailscale is required but not installed."
        echo -e "       ${DIM}The monitoring stack binds to your Tailscale IP"
        echo -e "       for network isolation. Install it first:${RESET}"
        echo -e "       ${CYAN}https://tailscale.com/download/linux${RESET}"
        echo
        if ! confirm "Continue without infra setup? (app-only)" "Y"; then
            exit 1
        fi
        INSTALL_INFRA=false
    else
        echo
        prompt_val "InfluxDB org name" "enphase" INFLUX_ORG
        echo
        dim "This will install and configure InfluxDB, Grafana, and Telegraf."
        dim "Admin passwords and API tokens are auto-generated."
        dim "All credentials saved to ~/monitoring-credentials.txt"
        echo
        sudo bash "$INFRA_SCRIPTS/setup-hub.sh" \
            --org "$INFLUX_ORG" \
            --bucket "enphase" \
            --retention 365
        echo
        ok "Monitoring stack installed"
        # Re-detect after install
        detect_system
    fi
fi

# ══════════════════════════════════════════════════
# ── STEP: Python venv ────────────────────────────
# ══════════════════════════════════════════════════
step "Python environment"

if [ "$HAS_APP_VENV" = true ]; then
    ok "Venv already exists"
else
    dim "Creating virtual environment"
    python3 -m venv "$REPO_DIR/venv"
    ok "Created venv"
fi

dim "Installing dependencies"
"$REPO_DIR/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt" 2>/dev/null
ok "Dependencies: requests, paho-mqtt, protobuf"

# ══════════════════════════════════════════════════
# ── STEP: Protobuf ───────────────────────────────
# ══════════════════════════════════════════════════
step "Protobuf schemas"

PROTO_SRC="$REPO_DIR/proto"
PROTO_DST="$REPO_DIR/src/enphase_cloud/proto"

if [ "$HAS_PROTO" = true ]; then
    ok "Compiled schemas already present"
elif [ -f "$PROTO_SRC/DataMsg.proto" ]; then
    dim "Compiling .proto files"
    "$REPO_DIR/venv/bin/pip" install -q grpcio-tools 2>/dev/null
    "$REPO_DIR/venv/bin/python3" -m grpc_tools.protoc \
        --proto_path="$PROTO_SRC" \
        --python_out="$PROTO_DST" \
        "$PROTO_SRC/DataMsg.proto" \
        "$PROTO_SRC/MeterSummaryData.proto" \
        "$PROTO_SRC/HemsStreamMessage.proto"
    ok "Compiled 3 protobuf schemas"
else
    warn "Proto source files not found — MQTT streaming won't work"
fi

# ══════════════════════════════════════════════════
# ── STEP: Enphase credentials ───────────────────
# ══════════════════════════════════════════════════
step "Enphase credentials"

if [ "$HAS_ENPHASE_CREDS" = true ]; then
    local_email=$(grep "^ENPHASE_EMAIL=" "$REPO_DIR/.env" | cut -d= -f2)
    ok "Using $local_email from .env"
else
    echo
    echo -e "  ${DIM}Enter your Enphase Enlighten account credentials.${RESET}"
    echo -e "  ${DIM}Same login you use at enlighten.enphaseenergy.com${RESET}"
    echo
    prompt_val "Email" "" ENPHASE_EMAIL
    prompt_secret "Password" ENPHASE_PASSWORD

    cat > "$REPO_DIR/.env" << EOF
ENPHASE_EMAIL=$ENPHASE_EMAIL
ENPHASE_PASSWORD=$ENPHASE_PASSWORD
EOF
    chmod 600 "$REPO_DIR/.env"
    ok "Saved to .env (mode 600)"
fi

# Source .env for the rest of the script
set -a; source "$REPO_DIR/.env"; set +a

# ══════════════════════════════════════════════════
# ── STEP: Telegraf config ────────────────────────
# ══════════════════════════════════════════════════
step "Telegraf configuration"

TELEGRAF_CONF="/etc/telegraf/telegraf.d/enphase.conf"

if [ -d /etc/telegraf/telegraf.d ]; then
    # Check if an InfluxDB output already exists in any telegraf config
    HAS_INFLUX_OUTPUT=false
    for conf in /etc/telegraf/telegraf.conf /etc/telegraf/telegraf.d/*.conf; do
        if [ -f "$conf" ] && grep -q "outputs.influxdb" "$conf" 2>/dev/null; then
            HAS_INFLUX_OUTPUT=true
            break
        fi
    done

    if [ "$HAS_INFLUX_OUTPUT" = true ]; then
        sudo tee "$TELEGRAF_CONF" >/dev/null << EOF
# enphase-telegraf input — installed by setup.sh
# InfluxDB output is configured in the main telegraf.conf

[[inputs.execd]]
  command = ["$REPO_DIR/bin/enphase-telegraf"]
  signal = "none"
  data_format = "influx"
  restart_delay = "30s"
EOF
        ok "Installed input config (output already exists)"
    else
        # Read InfluxDB connection info from credentials file
        INFLUXDB_URL=""
        INFLUXDB_ORG=""
        INFLUXDB_BUCKET=""
        if [ -f "$CREDS_FILE" ]; then
            INFLUXDB_URL=$(grep "^URL:" "$CREDS_FILE" | head -1 | awk '{print $2}' || true)
            INFLUXDB_ORG=$(grep "^Org:" "$CREDS_FILE" | head -1 | awk '{print $2}' || true)
            INFLUXDB_BUCKET=$(grep "^Bucket:" "$CREDS_FILE" | head -1 | awk '{print $2}' || true)
        fi

        sudo tee "$TELEGRAF_CONF" >/dev/null << EOF
# enphase-telegraf — installed by setup.sh

[[inputs.execd]]
  command = ["$REPO_DIR/bin/enphase-telegraf"]
  signal = "none"
  data_format = "influx"
  restart_delay = "30s"

[[outputs.influxdb_v2]]
  urls = ["${INFLUXDB_URL:-http://localhost:8086}"]
  token = "\${INFLUX_TOKEN}"
  organization = "${INFLUXDB_ORG:-enphase}"
  bucket = "${INFLUXDB_BUCKET:-enphase}"
EOF
        ok "Installed input + output config"
    fi
else
    warn "Telegraf config dir not found — skipping"
fi

# Inject Enphase credentials via systemd drop-in
TELEGRAF_DROPIN_DIR="/etc/systemd/system/telegraf.service.d"
if [ -d "$TELEGRAF_DROPIN_DIR" ] || [ -d /etc/telegraf ]; then
    sudo mkdir -p "$TELEGRAF_DROPIN_DIR"
    sudo tee "$TELEGRAF_DROPIN_DIR/enphase.conf" >/dev/null << EOF
[Service]
Environment="ENPHASE_EMAIL=${ENPHASE_EMAIL:-}"
Environment="ENPHASE_PASSWORD=${ENPHASE_PASSWORD:-}"
EOF
    sudo chmod 600 "$TELEGRAF_DROPIN_DIR/enphase.conf"
    sudo systemctl daemon-reload 2>/dev/null || true
    ok "Enphase credentials in systemd drop-in"
fi

# ══════════════════════════════════════════════════
# ── STEP: Connection test ────────────────────────
# ══════════════════════════════════════════════════
step "Connection test"

export PYTHONPATH="$REPO_DIR/src"
echo
dim "Connecting to Enphase cloud (15 second test)"

OUTPUT=$("$REPO_DIR/venv/bin/python3" "$REPO_DIR/src/enphase_telegraf.py" --verbose 2>&1 | head -20 &
BGPID=$!
sleep 15
kill $BGPID 2>/dev/null
wait $BGPID 2>/dev/null) || true

SERIAL=""
if echo "$OUTPUT" | grep -q "enphase_power"; then
    ok "Data is flowing from Enphase!"
    SERIAL=$(echo "$OUTPUT" | grep -oP 'serial=\K[0-9]+' | head -1 || true)
    [ -n "$SERIAL" ] && ok "Gateway serial: $SERIAL"
else
    warn "Could not verify data flow"
    warn "Check credentials: ./bin/enphase-telegraf --verbose"
fi

# ══════════════════════════════════════════════════
# ── STEP: Start Telegraf ─────────────────────────
# ══════════════════════════════════════════════════
if command -v telegraf &>/dev/null; then
    step "Start Telegraf"
    echo
    if systemctl is-active telegraf &>/dev/null; then
        if confirm "Restart Telegraf to pick up new config?" "Y"; then
            sudo systemctl restart telegraf
            sleep 2
            if systemctl is-active telegraf &>/dev/null; then
                ok "Telegraf restarted and running"
            else
                warn "Telegraf failed to start — check: journalctl -u telegraf -n 20"
            fi
        fi
    else
        if confirm "Start Telegraf?" "Y"; then
            sudo systemctl enable telegraf 2>/dev/null || true
            sudo systemctl start telegraf
            sleep 2
            if systemctl is-active telegraf &>/dev/null; then
                ok "Telegraf started"
            else
                warn "Telegraf failed to start — check: journalctl -u telegraf -n 20"
            fi
        fi
    fi
fi

# ══════════════════════════════════════════════════
# ── DONE ─────────────────────────────────────────
# ══════════════════════════════════════════════════
echo
echo
echo -e "${GREEN}${BOLD}"
cat << 'DONE_ART'
         .  *  .
      *  .__|__.  *
   .   ./__|__|__.   .          Setup complete!
 *   ./__|__|__|__\.   *
   ./__|__|__|__|__\.
  |__|__|__|__|__|__|
DONE_ART
echo -e "${RESET}"

# Summary box
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || true)
INFLUX_URL="${TAILSCALE_IP:+http://$TAILSCALE_IP:8086}"
GRAFANA_URL="${TAILSCALE_IP:+http://$TAILSCALE_IP:3000}"

echo -e "  ${DIM}============================================${RESET}"
echo

if [ -n "$INFLUX_URL" ]; then
    echo -e "    ${BOLD}InfluxDB${RESET}    ${CYAN}$INFLUX_URL${RESET}"
fi
if [ -n "$GRAFANA_URL" ]; then
    echo -e "    ${BOLD}Grafana${RESET}     ${CYAN}$GRAFANA_URL${RESET}"
fi
if [ -n "$SERIAL" ]; then
    echo -e "    ${BOLD}Gateway${RESET}     $SERIAL"
fi
if [ -f "$CREDS_FILE" ]; then
    echo -e "    ${BOLD}Credentials${RESET} $CREDS_FILE"
fi

echo
echo -e "  ${DIM}============================================${RESET}"
echo
echo -e "  ${BOLD}Useful commands:${RESET}"
echo
echo -e "    ${DIM}\$${RESET} ./bin/enphase-telegraf --verbose"
echo -e "      ${DIM}Run standalone, watch data stream by${RESET}"
echo
echo -e "    ${DIM}\$${RESET} ./bin/load-history"
echo -e "      ${DIM}Backfill InfluxDB with historical data${RESET}"
echo
echo -e "    ${DIM}\$${RESET} journalctl -u telegraf -f"
echo -e "      ${DIM}Watch Telegraf logs in real time${RESET}"
echo
echo -e "    ${DIM}\$${RESET} sudo systemctl status telegraf"
echo -e "      ${DIM}Check if Telegraf is running${RESET}"
echo

# ══════════════════════════════════════════════════
# ── OPTIONAL: Load history ───────────────────────
# ══════════════════════════════════════════════════
echo -e "  ${DIM}─────────────────────────────────────────${RESET}"
echo
echo -e "  ${BOLD}Want to backfill historical data?${RESET}"
echo -e "  ${DIM}Downloads your full solar production history and loads${RESET}"
echo -e "  ${DIM}it into InfluxDB so you can see graphs from day one.${RESET}"
echo -e "  ${DIM}(Takes a few hours — runs at ~2 requests/min to be respectful)${RESET}"
echo
if confirm "Load historical data now?" "N"; then
    "$REPO_DIR/bin/load-history"
fi
echo
