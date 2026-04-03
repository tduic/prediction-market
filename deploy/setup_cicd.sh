#!/usr/bin/env bash
# =============================================================================
# setup_cicd.sh — Create a GCP service account for GitHub Actions deploys.
#
# Run once from your local machine after provision.sh:
#   bash deploy/setup_cicd.sh
#
# This creates a service account, grants it the minimum permissions needed
# to SSH into the VM and deploy code, and exports a JSON key that you store
# as a GitHub Actions secret.
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
: "${ZONE:=us-central1-a}"
: "${VM_NAME:=predictor-vm}"

SA_NAME="github-deploy"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
KEY_FILE="$SCRIPT_DIR/github-deploy-key.json"

echo "============================================================"
echo "  Setting up GitHub Actions CI/CD"
echo "============================================================"
echo "  Project:   $PROJECT_ID"
echo "  SA:        $SA_EMAIL"
echo "============================================================"
echo ""

# ── 1. Enable IAM API ─────────────────────────────────────────────────────────
echo "[1/5] Enabling IAM API..."
gcloud services enable iam.googleapis.com --project="$PROJECT_ID"

# ── 2. Create service account ─────────────────────────────────────────────────
echo "[2/5] Creating service account: $SA_NAME"
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
  echo "      Already exists — skipping."
else
  gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="GitHub Actions Deploy" \
    --description="Used by GitHub Actions to deploy code to the predictor VM"
fi

# ── 3. Grant minimum required roles ──────────────────────────────────────────
echo "[3/5] Granting IAM roles..."

ROLES=(
  "roles/compute.instanceAdmin.v1"   # SSH into VM, SCP files
  "roles/compute.osLogin"            # OS Login for SSH key management
  "roles/iam.serviceAccountUser"     # Act as service account on the VM
)

for ROLE in "${ROLES[@]}"; do
  echo "      $ROLE"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$ROLE" \
    --condition=None \
    --quiet &>/dev/null
done

# ── 4. Export key JSON ────────────────────────────────────────────────────────
echo "[4/5] Exporting service account key..."
if [[ -f "$KEY_FILE" ]]; then
  echo "      Key file already exists — skipping. Delete it and re-run to regenerate."
else
  gcloud iam service-accounts keys create "$KEY_FILE" \
    --iam-account="$SA_EMAIL" \
    --project="$PROJECT_ID"
  chmod 600 "$KEY_FILE"
  echo "      Saved to: $KEY_FILE"
fi

# ── 5. Instructions ───────────────────────────────────────────────────────────
echo ""
echo "[5/5] Set up GitHub repository secrets"
echo ""
echo "============================================================"
echo "  Go to your GitHub repo → Settings → Secrets and variables"
echo "  → Actions → New repository secret"
echo ""
echo "  Create these three secrets:"
echo ""
echo "  GCP_PROJECT_ID  =  $PROJECT_ID"
echo "  GCP_SA_KEY      =  (paste the entire contents of $KEY_FILE)"
echo "  GCP_ZONE        =  $ZONE          (optional, defaults to us-central1-a)"
echo "  GCP_VM_NAME     =  $VM_NAME       (optional, defaults to predictor-vm)"
echo ""
echo "  To copy the key to clipboard (macOS):"
echo "    cat $KEY_FILE | pbcopy"
echo ""
echo "  IMPORTANT: After adding the secret, delete the local key file:"
echo "    rm $KEY_FILE"
echo ""
echo "============================================================"
echo ""
echo "  Once secrets are set, every GitHub Release will auto-deploy."
echo "  You can also trigger manually from Actions → Deploy to GCP → Run workflow."
echo "============================================================"
