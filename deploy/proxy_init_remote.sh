#!/usr/bin/env bash
# =============================================================================
# proxy_init_remote.sh — Runs as root ON the proxy VM.
# Installs and configures Dante SOCKS5 proxy.
#
# Expected env vars (passed by setup_proxy.sh):
#   PROXY_PORT  — port to listen on (default 1080)
#   ALLOWED_IP  — only allow connections from this IP (main trading VM)
# =============================================================================
set -euo pipefail

: "${PROXY_PORT:=1080}"
: "${ALLOWED_IP:?ALLOWED_IP must be set}"

log() { echo ""; echo ">>> $*"; }

# ── 1. Install Dante SOCKS server ────────────────────────────────────────────
log "Installing Dante SOCKS5 proxy"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq dante-server

# ── 2. Detect external interface ──────────────────────────────────────────────
# GCE VMs typically use ens4 or eth0
EXT_IF=$(ip -o -4 route show to default | awk '{print $5}' | head -1)
EXT_ADDR=$(ip -o -4 addr show dev "$EXT_IF" | awk '{print $4}' | cut -d/ -f1)
log "Detected interface: $EXT_IF ($EXT_ADDR)"

# ── 3. Write Dante config ────────────────────────────────────────────────────
log "Writing /etc/danted.conf"
cat > /etc/danted.conf << EOF
# Dante SOCKS5 proxy — predictor trading system
# Only allows connections from the main trading VM

logoutput: syslog

# Listen on all interfaces, specified port
internal: $EXT_IF port = $PROXY_PORT
external: $EXT_IF

# Authentication: none required (firewall restricts source IP)
socksmethod: none
clientmethod: none

# Client rules — only allow the main trading VM
client pass {
    from: $ALLOWED_IP/32 to: 0.0.0.0/0
    log: connect
}

client block {
    from: 0.0.0.0/0 to: 0.0.0.0/0
    log: connect error
}

# SOCKS rules — allow all outbound from permitted clients
socks pass {
    from: $ALLOWED_IP/32 to: 0.0.0.0/0
    protocol: tcp
    log: connect
}

socks block {
    from: 0.0.0.0/0 to: 0.0.0.0/0
    log: connect error
}
EOF

# ── 4. Enable and start ──────────────────────────────────────────────────────
log "Starting Dante SOCKS proxy"
systemctl enable danted
systemctl restart danted

# Verify it's listening
sleep 2
if ss -tlnp | grep -q ":$PROXY_PORT"; then
  echo "    Dante listening on port $PROXY_PORT"
else
  echo "    ERROR: Dante not listening. Check: journalctl -u danted -n 20"
  exit 1
fi

log "Proxy setup complete"
echo "    Listening on $EXT_ADDR:$PROXY_PORT"
echo "    Allowed source: $ALLOWED_IP"
