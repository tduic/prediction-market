#!/usr/bin/env bash
# =============================================================================
# push.sh — Sync code to the VM and (re)install the systemd service.
#
# Run from your LOCAL machine any time you want to deploy an update:
#   bash deploy/push.sh
#
# First deploy:
#   bash deploy/push.sh --with-env    # also copies your local .env file
#
# Flags:
#   --with-env    Copy .env from the project root to /data/predictor/.env on VM
#   --restart     Restart the service after push (default: yes)
#   --no-restart  Push code without restarting the running service
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$SCRIPT_DIR/config.env"

# ── Parse flags ───────────────────────────────────────────────────────────────
WITH_ENV=false
DO_RESTART=true

for arg in "$@"; do
  case "$arg" in
    --with-env)    WITH_ENV=true ;;
    --restart)     DO_RESTART=true ;;
    --no-restart)  DO_RESTART=false ;;
    *) echo "Unknown flag: $arg"; exit 1 ;;
  esac
done

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: deploy/config.env not found. Run provision.sh first."
  exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${PROJECT_ID:?}"
: "${ZONE:?}"
: "${VM_NAME:?}"

SSH_TARGET="$VM_NAME"
GCE_SSH="gcloud compute ssh $SSH_TARGET --zone=$ZONE --project=$PROJECT_ID --command"

echo "=================================================="
echo "  Pushing to: $VM_NAME ($PROJECT_ID / $ZONE)"
echo "=================================================="

# ── 1. Sync code ─────────────────────────────────────────────────────────────
echo ""
echo "[1/5] Packaging and uploading code..."

# Tar locally (excludes heavy dirs), scp the tarball, extract on VM
TARBALL="/tmp/predictor-deploy-$$.tar.gz"
tar czf "$TARBALL" \
  -C "$PROJECT_ROOT" \
  --exclude='.git' \
  --exclude='venv' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='*.db' \
  --exclude='*.db-shm' \
  --exclude='*.db-wal' \
  --exclude='deploy/config.env' \
  --exclude='deploy/*-key.json' \
  --exclude='dashboard/node_modules' \
  .

gcloud compute scp \
  --zone="$ZONE" \
  --project="$PROJECT_ID" \
  "$TARBALL" \
  "$VM_NAME:/tmp/predictor-deploy.tar.gz"

rm -f "$TARBALL"

# Extract and rsync into place on the VM
$GCE_SSH "sudo mkdir -p /tmp/predictor-staging \
  && sudo tar xzf /tmp/predictor-deploy.tar.gz -C /tmp/predictor-staging \
  && sudo rm -rf /data/predictor/prediction-market/dashboard/dist \
  && sudo rsync -a --delete \
    --exclude='.env' \
    --exclude='*.db' \
    --exclude='*.db-shm' \
    --exclude='*.db-wal' \
    --exclude='venv' \
    --exclude='node_modules' \
    --exclude='dashboard/node_modules' \
    /tmp/predictor-staging/ /data/predictor/prediction-market/ \
  && sudo chown -R predictor:predictor /data/predictor/prediction-market \
  && sudo rm -rf /tmp/predictor-staging /tmp/predictor-deploy.tar.gz"

# ── 2. Copy .env (first deploy only, or when --with-env is passed) ────────────
if [[ "$WITH_ENV" == "true" ]]; then
  echo ""
  echo "[2/5] Copying .env to VM..."
  if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
    echo "ERROR: .env not found at $PROJECT_ROOT/.env"
    echo "Create it from config/settings.example.env and fill in your credentials."
    exit 1
  fi
  gcloud compute scp \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    "$PROJECT_ROOT/.env" \
    "$VM_NAME:/tmp/predictor.env"
  $GCE_SSH "sudo mv /tmp/predictor.env /data/predictor/.env \
    && sudo chown predictor:predictor /data/predictor/.env \
    && sudo chmod 600 /data/predictor/.env"
  echo "    .env copied to /data/predictor/.env"
else
  echo ""
  echo "[2/5] Skipping .env (pass --with-env to copy it)"
  # Verify it exists on VM
  ENV_EXISTS=$($GCE_SSH "test -f /data/predictor/.env && echo yes || echo no" 2>/dev/null || echo "no")
  if [[ "$ENV_EXISTS" == "no" ]]; then
    echo ""
    echo "  WARNING: /data/predictor/.env does not exist on the VM."
    echo "  Run: bash deploy/push.sh --with-env"
    echo "  The service will fail to start without it."
  fi
fi

# ── 3. Create / update Python venv ───────────────────────────────────────────
echo ""
echo "[3/5] Installing Python dependencies..."
$GCE_SSH "cd /data/predictor/prediction-market \
  && sudo -u predictor python3 -m venv /data/predictor/venv --clear 2>/dev/null || sudo -u predictor python3 -m venv /data/predictor/venv \
  && sudo -u predictor /data/predictor/venv/bin/pip install --quiet --upgrade pip \
  && sudo -u predictor /data/predictor/venv/bin/pip install --quiet -r requirements.txt \
  && sudo -u predictor /data/predictor/venv/bin/pip install --quiet --force-reinstall --no-deps typing_extensions"

# ── 4. Build dashboard frontend (locally, then shipped with code) ─────────────
echo ""
echo "[4/5] Building dashboard frontend..."
if command -v npm &>/dev/null && [ -f "$PROJECT_ROOT/dashboard/package.json" ]; then
  (cd "$PROJECT_ROOT/dashboard" && npm install --silent && npm run build --silent) && \
    echo "    Dashboard built and will be deployed with code." || \
    echo "    WARNING: Dashboard build failed — API will work but no frontend."
else
  echo "    npm not found locally — skipping build (deploy existing dist/ if present)"
fi

# ── 5. Install and (re)start systemd service ──────────────────────────────────
echo ""
echo "[5/5] Installing systemd service..."
$GCE_SSH "sudo cp /data/predictor/prediction-market/deploy/predictor.service \
  /etc/systemd/system/predictor.service \
  && sudo systemctl daemon-reload \
  && sudo systemctl enable predictor"

if [[ "$DO_RESTART" == "true" ]]; then
  echo "    Restarting service..."
  $GCE_SSH "sudo systemctl restart predictor"
  sleep 3
  STATUS=$($GCE_SSH "sudo systemctl is-active predictor" 2>/dev/null || echo "unknown")
  echo "    Service status: $STATUS"
  if [[ "$STATUS" != "active" ]]; then
    echo ""
    echo "  Service did not start cleanly. Check logs with:"
    echo "  gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT_ID"
    echo "  Then: sudo journalctl -u predictor -n 50"
  fi
else
  echo "    Code updated. Service NOT restarted (--no-restart)."
fi

echo ""
echo "=================================================="
echo "  Deploy complete!"
echo ""
echo "  View logs:   gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT_ID"
echo "               sudo journalctl -u predictor -f"
echo ""
echo "  Dashboard:   SSH tunnel on your local machine:"
echo "               gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT_ID \\"
echo "                 -- -L 8000:localhost:8000"
echo "               Then open: http://localhost:8000"
echo "=================================================="
