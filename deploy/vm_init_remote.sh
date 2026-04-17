#!/usr/bin/env bash
# =============================================================================
# vm_init_remote.sh — Runs as root ON the VM.
# Uploaded and executed by vm_setup.sh. Do not run this directly.
# =============================================================================
set -euo pipefail

DATA_DEVICE="/dev/disk/by-id/google-predictor-data"
DATA_MOUNT="/data"
APP_DIR="$DATA_MOUNT/predictor/prediction-market"
PREDICTOR_USER="predictor"

log() { echo ""; echo ">>> $*"; }

# ── 1. Mount persistent data disk ────────────────────────────────────────────
log "Mounting persistent data disk at $DATA_MOUNT"
mkdir -p "$DATA_MOUNT"

if ! mountpoint -q "$DATA_MOUNT"; then
  # Format only if not already formatted
  if ! blkid "$DATA_DEVICE" &>/dev/null; then
    echo "    Formatting new disk..."
    mkfs.ext4 -F "$DATA_DEVICE"
  fi
  mount "$DATA_DEVICE" "$DATA_MOUNT"
fi

# Persist mount across reboots
FSTAB_ENTRY="$DATA_DEVICE $DATA_MOUNT ext4 defaults,nofail 0 2"
if ! grep -qF "$DATA_DEVICE" /etc/fstab; then
  echo "$FSTAB_ENTRY" >> /etc/fstab
fi

mkdir -p "$DATA_MOUNT/predictor"

# ── 2. Create predictor user ──────────────────────────────────────────────────
log "Creating system user: $PREDICTOR_USER"
if ! id "$PREDICTOR_USER" &>/dev/null; then
  useradd --system --shell /bin/bash --home-dir "$DATA_MOUNT/predictor" \
    --no-create-home "$PREDICTOR_USER"
fi
chown -R "$PREDICTOR_USER:$PREDICTOR_USER" "$DATA_MOUNT/predictor"

# ── 3. System packages ────────────────────────────────────────────────────────
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  software-properties-common \
  git \
  curl \
  wget \
  build-essential \
  libssl-dev \
  libffi-dev \
  tmux \
  htop \
  rsync

# ── 4. Python 3.12 (deadsnakes PPA) ──────────────────────────────────────────
log "Installing Python 3.12"
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv python3.12-dev python3-pip

# Make python3.12 the default python3
update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1
update-alternatives --set python3 /usr/bin/python3.12
ln -sf /usr/bin/python3.12 /usr/local/bin/python

echo "    Python version: $(python3 --version)"

# ── 5. Node.js 18 ─────────────────────────────────────────────────────────────
log "Installing Node.js 18"
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash - 2>/dev/null
  apt-get install -y -qq nodejs
fi
echo "    Node version: $(node --version)"

# ── 6. Prepare app directory ──────────────────────────────────────────────────
log "Preparing app directory: $APP_DIR"
mkdir -p "$APP_DIR"
chown -R "$PREDICTOR_USER:$PREDICTOR_USER" "$DATA_MOUNT/predictor"

echo ""
echo "==================================================="
echo "  VM bootstrap complete."
echo "  App directory: $APP_DIR"
echo "  Run 'bash deploy/push.sh' from your local machine"
echo "==================================================="
