#!/usr/bin/env bash
# =============================================================================
# provision_proxy.sh — Create a lightweight EU SOCKS5 proxy VM.
#
# Polymarket CLOB API calls from the main trading VM are routed through this
# proxy in europe-west4 (Netherlands). The proxy only accepts connections from
# the main VM's external IP — it is not open to the internet.
#
# Run after provision.sh:
#   bash deploy/provision_proxy.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: deploy/config.env not found. Run provision.sh first."
  exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${PROJECT_ID:?PROJECT_ID must be set}"
: "${EXTERNAL_IP:?EXTERNAL_IP must be set — run provision.sh first}"

PROXY_ZONE="${PROXY_ZONE:-europe-west4-a}"
PROXY_VM="${PROXY_VM:-predictor-proxy}"
PROXY_MACHINE="${PROXY_MACHINE:-e2-micro}"
PROXY_PORT="${PROXY_PORT:-1080}"

echo "============================================================"
echo "  Predictor — EU Proxy Provisioning"
echo "============================================================"
echo "  Project:        $PROJECT_ID"
echo "  Proxy VM:       $PROXY_VM ($PROXY_MACHINE)"
echo "  Zone:           $PROXY_ZONE"
echo "  SOCKS5 port:    $PROXY_PORT"
echo "  Allowed source: $EXTERNAL_IP (main trading VM)"
echo "============================================================"
echo ""

# ── 1. Create proxy VM ────────────────────────────────────────────────────────
echo "[1/3] Creating proxy VM: $PROXY_VM"
if gcloud compute instances describe "$PROXY_VM" --zone="$PROXY_ZONE" --project="$PROJECT_ID" &>/dev/null; then
  echo "      Already exists — skipping."
else
  gcloud compute instances create "$PROXY_VM" \
    --project="$PROJECT_ID" \
    --zone="$PROXY_ZONE" \
    --machine-type="$PROXY_MACHINE" \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=10GB \
    --boot-disk-type=pd-standard \
    --boot-disk-auto-delete \
    --tags=predictor-proxy
fi

# ── 2. Firewall: allow SOCKS5 only from main VM ──────────────────────────────
echo ""
echo "[2/3] Creating firewall rule (SOCKS5 from main VM only)..."
RULE_NAME="allow-proxy-from-main-vm"
if gcloud compute firewall-rules describe "$RULE_NAME" --project="$PROJECT_ID" &>/dev/null; then
  echo "      Updating existing rule with current main VM IP..."
  gcloud compute firewall-rules update "$RULE_NAME" \
    --project="$PROJECT_ID" \
    --source-ranges="$EXTERNAL_IP/32" \
    --quiet
else
  gcloud compute firewall-rules create "$RULE_NAME" \
    --project="$PROJECT_ID" \
    --allow="tcp:$PROXY_PORT" \
    --target-tags=predictor-proxy \
    --source-ranges="$EXTERNAL_IP/32" \
    --description="SOCKS5 proxy — only from main trading VM"
fi

# Also ensure SSH is allowed for setup
SSH_RULE="allow-proxy-ssh"
if ! gcloud compute firewall-rules describe "$SSH_RULE" --project="$PROJECT_ID" &>/dev/null; then
  gcloud compute firewall-rules create "$SSH_RULE" \
    --project="$PROJECT_ID" \
    --allow=tcp:22 \
    --target-tags=predictor-proxy \
    --description="SSH access for proxy VM"
fi

# ── 3. Get proxy IP and save config ──────────────────────────────────────────
echo ""
echo "[3/3] Fetching proxy external IP..."
PROXY_IP=$(gcloud compute instances describe "$PROXY_VM" \
  --project="$PROJECT_ID" \
  --zone="$PROXY_ZONE" \
  --format="get(networkInterfaces[0].accessConfigs[0].natIP)")

# Append proxy config
for VAR in PROXY_ZONE PROXY_VM PROXY_IP PROXY_PORT; do
  if grep -q "^${VAR}=" "$CONFIG_FILE"; then
    sed -i.bak "s|^${VAR}=.*|${VAR}=${!VAR}|" "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
  else
    echo "${VAR}=${!VAR}" >> "$CONFIG_FILE"
  fi
done

echo ""
echo "============================================================"
echo "  Proxy provisioned!"
echo "============================================================"
echo "  Proxy IP:   $PROXY_IP"
echo "  SOCKS5:     $PROXY_IP:$PROXY_PORT"
echo ""
echo "  Next: bash deploy/setup_proxy.sh"
echo "============================================================"
