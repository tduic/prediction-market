#!/usr/bin/env bash
# =============================================================================
# provision.sh — Create all GCP resources for the predictor trading system.
#
# Run once from your local machine:
#   cd deploy && cp config.env.example config.env
#   # Edit config.env with your desired PROJECT_ID
#   bash provision.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: $CONFIG_FILE not found."
  echo "Run: cp deploy/config.env.example deploy/config.env"
  echo "Then edit deploy/config.env and set PROJECT_ID."
  exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${PROJECT_ID:?PROJECT_ID must be set in deploy/config.env}"
: "${ZONE:=us-central1-a}"
: "${VM_NAME:=predictor-vm}"
: "${DATA_DISK_NAME:=predictor-data}"
: "${MACHINE_TYPE:=e2-medium}"

REGION="${ZONE%-*}"  # strip last -X to get region

echo "============================================================"
echo "  Predictor Trading — GCP Provisioning"
echo "============================================================"
echo "  Project:    $PROJECT_ID"
echo "  Zone:       $ZONE"
echo "  VM:         $VM_NAME ($MACHINE_TYPE)"
echo "  Data disk:  $DATA_DISK_NAME"
echo "============================================================"
echo ""

# ── 1. Create project ─────────────────────────────────────────────────────────
echo "[1/7] Creating GCP project: $PROJECT_ID"
if gcloud projects describe "$PROJECT_ID" &>/dev/null; then
  echo "      Project already exists — skipping creation."
else
  gcloud projects create "$PROJECT_ID" --name="Predictor Trading"
fi
gcloud config set project "$PROJECT_ID"

# ── 2. Billing ────────────────────────────────────────────────────────────────
echo ""
echo "[2/7] Billing"
echo "      Compute Engine requires billing to be enabled."
echo "      If you haven't already, open this URL and link a billing account:"
echo ""
echo "      https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT_ID"
echo ""
read -rp "      Press Enter once billing is enabled (or Ctrl+C to abort)..."

# ── 3. Enable APIs ────────────────────────────────────────────────────────────
echo ""
echo "[3/7] Enabling Compute Engine API..."
gcloud services enable compute.googleapis.com --project="$PROJECT_ID"

# ── 4. Create VM ──────────────────────────────────────────────────────────────
echo ""
echo "[4/7] Creating VM: $VM_NAME"
if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID" &>/dev/null; then
  echo "      VM already exists — skipping."
else
  gcloud compute instances create "$VM_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-standard \
    --boot-disk-auto-delete \
    --tags=predictor-vm
fi

# ── 5. Create persistent data disk ───────────────────────────────────────────
echo ""
echo "[5/7] Creating persistent data disk: $DATA_DISK_NAME (20 GB)"
if gcloud compute disks describe "$DATA_DISK_NAME" --zone="$ZONE" --project="$PROJECT_ID" &>/dev/null; then
  echo "      Disk already exists — skipping."
else
  gcloud compute disks create "$DATA_DISK_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --size=20GB \
    --type=pd-standard
fi

echo ""
echo "[5b/7] Attaching data disk to VM..."
# Check if already attached
ATTACHED=$(gcloud compute instances describe "$VM_NAME" \
  --zone="$ZONE" --project="$PROJECT_ID" \
  --format="json(disks[].source)" \
  | grep -c "$DATA_DISK_NAME" || true)

if [[ "$ATTACHED" -gt 0 ]]; then
  echo "       Disk already attached — skipping."
else
  gcloud compute instances attach-disk "$VM_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --disk="$DATA_DISK_NAME" \
    --device-name=predictor-data
fi

# ── 6. Firewall rule (SSH only — dashboard via SSH tunnel) ────────────────────
echo ""
echo "[6/7] Creating firewall rule (SSH only)..."
if gcloud compute firewall-rules describe allow-predictor-ssh --project="$PROJECT_ID" &>/dev/null; then
  echo "      Rule already exists — skipping."
else
  gcloud compute firewall-rules create allow-predictor-ssh \
    --project="$PROJECT_ID" \
    --allow=tcp:22 \
    --target-tags=predictor-vm \
    --description="SSH access for predictor trading VM"
fi

# ── 7. Get external IP and save config ───────────────────────────────────────
echo ""
echo "[7/7] Fetching external IP..."
EXTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" \
  --project="$PROJECT_ID" \
  --zone="$ZONE" \
  --format="get(networkInterfaces[0].accessConfigs[0].natIP)")

# Update config.env with the resolved IP
if grep -q "^EXTERNAL_IP=" "$CONFIG_FILE"; then
  sed -i.bak "s|^EXTERNAL_IP=.*|EXTERNAL_IP=$EXTERNAL_IP|" "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
else
  echo "EXTERNAL_IP=$EXTERNAL_IP" >> "$CONFIG_FILE"
fi

echo ""
echo "============================================================"
echo "  Provisioning complete!"
echo "============================================================"
echo "  External IP: $EXTERNAL_IP"
echo "  SSH:         gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT_ID"
echo ""
echo "  Next step:   bash deploy/vm_setup.sh"
echo "============================================================"
