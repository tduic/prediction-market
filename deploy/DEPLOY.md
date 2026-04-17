# GCP Deployment Guide

Deploys the trading system to a single GCE VM (e2-medium, Ubuntu 22.04). The VM runs `scripts/trading_session.py` as a systemd service (`predictor.service`) with auto-restart, storing the SQLite database on a separate persistent disk so it survives instance restarts.

**Estimated cost:** ~$35/month for the VM + ~$1/month for storage.

## Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- A Google account with billing available to attach to a new project
- Your `.env` file with API credentials (`cp config/settings.example.env .env` then fill it in)

---

## Step 1: Configure

```bash
cd deploy
cp config.env.example config.env
```

Edit `deploy/config.env` and set a unique `PROJECT_ID` (e.g. `predictor-trading-2025`). Leave everything else as-is unless you have a preference on region.

---

## Step 2: Provision GCP resources

```bash
bash deploy/provision.sh
```

This will:
1. Create the GCP project
2. Pause and ask you to enable billing in the console (one manual step — GCP requires this via UI)
3. Enable the Compute Engine API
4. Create the e2-medium VM in `us-central1-a`
5. Create and attach a 20 GB persistent disk for SQLite
6. Add a firewall rule for SSH
7. Write the VM's external IP back into `deploy/config.env`

---

## Step 3: Bootstrap the VM

```bash
bash deploy/vm_setup.sh
```

SSHes into the VM and installs everything: Python 3.12 (deadsnakes PPA), Node.js 18, and creates a `predictor` system user. Takes about 5 minutes. Safe to re-run if it fails partway through.

---

## Step 4: Deploy the code (first time)

```bash
bash deploy/push.sh --with-env
```

The `--with-env` flag copies your local `.env` to the VM as `/data/predictor/.env` (mode 600, owned by `predictor`). Only needed on first deploy or when credentials change — leave it off for code-only updates.

This will:
1. Rsync the project to `/data/predictor/prediction-market/` on the VM
2. Copy `.env` to `/data/predictor/.env`
3. Create a Python venv at `/data/predictor/venv/` and install all requirements
4. Build the React dashboard frontend (`npm install && npm run build`)
5. Install and start the `predictor` systemd service

---

## EU Proxy for Polymarket (Optional)

Polymarket CLOB API calls can be routed through a SOCKS5 proxy running on a lightweight EU VM (e2-micro in Netherlands, ~$7/month). The proxy only accepts connections from the main trading VM's IP.

```bash
# 1. Create the proxy VM and firewall rule
bash deploy/provision_proxy.sh

# 2. Install Dante SOCKS5 proxy
bash deploy/setup_proxy.sh

# 3. Add to your .env
echo "POLYMARKET_PROXY=socks5://PROXY_IP:1080" >> .env

# 4. Redeploy with updated .env
bash deploy/push.sh --with-env
```

The Polymarket client reads `POLYMARKET_PROXY` from the environment and patches `py-clob-client`'s HTTP layer to route all CLOB API traffic through it. Kalshi traffic is unaffected.

To verify the proxy is working, SSH into the main VM and check the logs:
```bash
sudo journalctl -u predictor | grep -i proxy
```

You should see: `Polymarket HTTP traffic routed through proxy: PROXY_IP:1080`

---

## Checking the service

SSH into the VM:
```bash
gcloud compute ssh predictor-vm --zone=us-central1-a --project=YOUR_PROJECT_ID
```

View live logs:
```bash
sudo journalctl -u predictor -f
```

Check service status:
```bash
sudo systemctl status predictor
```

Stop / start / restart:
```bash
sudo systemctl stop predictor
sudo systemctl start predictor
sudo systemctl restart predictor
```

---

## Viewing the dashboard

The dashboard is bound to `0.0.0.0:8000` and protected by HTTP Basic Auth.

### 1. Set credentials in `.env` on the VM

```bash
# Add to /data/predictor/.env
DASHBOARD_USER=admin          # optional, defaults to "admin"
DASHBOARD_PASSWORD=changeme   # required to enable auth
```

Then redeploy or restart the service:
```bash
bash deploy/push.sh --with-env
```

### 2. Open the GCP firewall

```bash
gcloud compute firewall-rules create allow-dashboard \
  --project=YOUR_PROJECT_ID \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:8000 \
  --source-ranges=YOUR_IP/32 \   # restrict to your IP
  --target-tags=predictor-vm
```

Then open **http://VM_EXTERNAL_IP:8000** in your browser and enter your credentials.

### SSH tunnel (no firewall rule needed)

If you prefer not to open a firewall rule, you can still use the SSH tunnel:

```bash
gcloud compute ssh predictor-vm \
  --zone=us-central1-a \
  --project=YOUR_PROJECT_ID \
  -- -L 8000:localhost:8000
```

Leave that terminal open, then open **http://localhost:8000** in your browser.

---

## Deploying code updates

Any time you change code locally:
```bash
bash deploy/push.sh
```

This rsyncs the code, reinstalls dependencies, rebuilds the dashboard, and restarts the service. The SQLite database on `/data` is excluded from the sync and is never touched by a push.

To push without restarting (e.g. during active trading):
```bash
bash deploy/push.sh --no-restart
```

---

## Database access

The SQLite DB lives at `/data/predictor/prediction-market/prediction_market.db`. To download it locally for inspection:

```bash
gcloud compute scp \
  predictor-vm:/data/predictor/prediction-market/prediction_market.db \
  ./prediction_market_backup.db \
  --zone=us-central1-a --project=YOUR_PROJECT_ID
```

---

## Stopping the soak test / teardown

To stop the service (data preserved on disk):
```bash
gcloud compute ssh predictor-vm --zone=us-central1-a --project=YOUR_PROJECT_ID \
  --command="sudo systemctl stop predictor"
```

To delete all GCP resources when done (this destroys the DB — download it first):
```bash
gcloud compute instances delete predictor-vm --zone=us-central1-a --project=YOUR_PROJECT_ID
gcloud compute disks delete predictor-data --zone=us-central1-a --project=YOUR_PROJECT_ID
```

---

## File layout on the VM

```
/data/predictor/
├── .env                          # Credentials (mode 600, never in git)
├── venv/                         # Python virtual environment
└── prediction-market/            # Project code (rsynced from local)
    ├── prediction_market.db      # SQLite database (persistent disk)
    ├── scripts/
    │   └── trading_session.py
    └── ...
```

---

## Troubleshooting

**Service won't start:**
```bash
sudo journalctl -u predictor -n 100 --no-pager
```

**Dashboard not loading:**
Check that the frontend was built:
```bash
ls /data/predictor/prediction-market/dashboard/dist/
```
If empty, rebuild:
```bash
cd /data/predictor/prediction-market/dashboard && sudo -u predictor npm run build
sudo systemctl restart predictor
```

**Out of disk space:**
```bash
df -h /data
# If full, expand the persistent disk in the GCP console, then:
sudo resize2fs /dev/disk/by-id/google-predictor-data
```

---

## CI/CD: Auto-deploy on GitHub Release

Once the VM is running, you can set up automatic deploys so that every new GitHub Release triggers a deploy to the VM.

### One-time setup

```bash
bash deploy/setup_cicd.sh
```

This creates a `github-deploy` service account in your GCP project with the minimum permissions needed to SSH into the VM and push code. It exports a JSON key file and tells you exactly which GitHub secrets to create.

You need three secrets in your GitHub repo (Settings → Secrets → Actions):

| Secret | Value |
|--------|-------|
| `GCP_PROJECT_ID` | Your GCP project ID |
| `GCP_SA_KEY` | Entire contents of the JSON key file |
| `GCP_ZONE` | `us-central1-a` (optional, this is the default) |

After adding the secrets, **delete the local key file**:
```bash
rm deploy/github-deploy-key.json
```

### How it works

The workflow (`.github/workflows/deploy.yml`) triggers on:
- **New GitHub Release** (published) — the normal path
- **Manual dispatch** — click "Run workflow" in the Actions tab for ad-hoc deploys

Each deploy:
1. Tars the repo (excluding `.git`, `venv`, `node_modules`, DB files)
2. SCPs the tarball to the VM
3. Rsyncs into `/data/predictor/prediction-market/` (preserves `.env` and DB)
4. Installs Python dependencies
5. Rebuilds the dashboard frontend
6. Restarts the systemd service
7. Verifies the service is running (fails the workflow if it isn't)

### Creating a release to trigger deploy

```bash
git tag v0.1.0
git push origin v0.1.0
```

Then on GitHub: Releases → Draft a new release → Choose the tag → Publish.

Or from the CLI:
```bash
gh release create v0.1.0 --title "v0.1.0 — soak test" --notes "Initial paper trading deployment"
```

### Manual deploy from Actions

Go to Actions → "Deploy to GCP" → Run workflow → select branch → Run.

This is useful for deploying a branch without creating a release.
