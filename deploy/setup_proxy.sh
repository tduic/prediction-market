#!/usr/bin/env bash
# =============================================================================
# setup_proxy.sh — Bootstrap the EU proxy VM with a SOCKS5 proxy.
#
# Run from your LOCAL machine after provision_proxy.sh:
#   bash deploy/setup_proxy.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: deploy/config.env not found."
  exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${PROJECT_ID:?}"
: "${PROXY_ZONE:?Run provision_proxy.sh first}"
: "${PROXY_VM:?}"
: "${PROXY_PORT:=1080}"
: "${EXTERNAL_IP:?}"

echo "=== Uploading proxy init script ==="
gcloud compute scp "$SCRIPT_DIR/proxy_init_remote.sh" \
  "$PROXY_VM:/tmp/proxy_init_remote.sh" \
  --zone="$PROXY_ZONE" --project="$PROJECT_ID"

echo "=== Running proxy bootstrap on VM ==="
gcloud compute ssh "$PROXY_VM" \
  --zone="$PROXY_ZONE" \
  --project="$PROJECT_ID" \
  --command="sudo PROXY_PORT=$PROXY_PORT ALLOWED_IP=$EXTERNAL_IP bash /tmp/proxy_init_remote.sh"

echo ""
echo "=== Proxy setup complete! ==="
echo ""
echo "  SOCKS5 proxy running at $PROXY_IP:$PROXY_PORT"
echo "  Only accepts connections from $EXTERNAL_IP (main trading VM)"
echo ""
echo "  Add to your .env file:"
echo "    POLYMARKET_PROXY=socks5://$PROXY_IP:$PROXY_PORT"
echo ""
echo "  Then redeploy: bash deploy/push.sh"
