#!/usr/bin/env bash
# =============================================================================
# vm_setup.sh — Bootstrap the GCE VM.
#
# Run from your LOCAL machine (it SSHes into the VM and runs everything):
#   bash deploy/vm_setup.sh
#
# This is idempotent — safe to run again if something fails partway through.
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

: "${PROJECT_ID:?}"
: "${ZONE:?}"
: "${VM_NAME:?}"

echo "=== Uploading vm_init_remote.sh to VM ==="
gcloud compute scp "$SCRIPT_DIR/vm_init_remote.sh" \
  "$VM_NAME:/tmp/vm_init_remote.sh" \
  --zone="$ZONE" --project="$PROJECT_ID"

echo "=== Running bootstrap on VM (this takes ~5 minutes) ==="
gcloud compute ssh "$VM_NAME" \
  --zone="$ZONE" \
  --project="$PROJECT_ID" \
  --command="sudo bash /tmp/vm_init_remote.sh"

echo ""
echo "=== VM setup complete! ==="
echo "Next step: bash deploy/push.sh"
