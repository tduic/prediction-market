#!/bin/bash
# Paste this entire file into the VM terminal.
# Safe to run multiple times — all operations are idempotent.
set -euo pipefail

# ── Runtime configuration ─────────────────────────────────────────────────────
# /data/predictor/.env holds NON-SECRET runtime settings only; the deploy
# workflow merges them in with deploy/env_merge.py. Credentials (exchange keys,
# DASHBOARD_PASSWORD, ALERT_DISCORD_WEBHOOK_URL) live in GCP Secret Manager and
# are never written to .env. Never add plaintext secrets here.
if [ ! -f /data/predictor/.env ]; then
  sudo touch /data/predictor/.env
  sudo chown predictor:predictor /data/predictor/.env
  sudo chmod 600 /data/predictor/.env
  echo "Warning: /data/predictor/.env is empty. Run a deploy to populate runtime settings." >&2
fi

# ── Discord notify helper ─────────────────────────────────────────────────────
# The webhook is a Secret Manager secret (alert-discord-webhook-url, the GCP
# name of ALERT_DISCORD_WEBHOOK_URL), not a .env value — the old
# `source .env; curl "$DISCORD_WEBHOOK_URL"` posted to an empty URL.
sudo tee /data/predictor/notify-discord.sh > /dev/null << 'EOF'
#!/bin/bash
# Usage: notify-discord.sh "message"
# The webhook URL is a credential: it is handed to curl as a config file on
# stdin (`-K -`), never as an argument, so it does not show up in `ps`.
set -euo pipefail
URL="${ALERT_DISCORD_WEBHOOK_URL:-}"
if [ -z "$URL" ]; then
  if ! command -v gcloud >/dev/null 2>&1; then
    echo "notify-discord: ALERT_DISCORD_WEBHOOK_URL unset and gcloud not installed" >&2
    exit 1
  fi
  ERR="$(mktemp)"
  trap 'rm -f "$ERR"' EXIT
  if ! URL="$(gcloud secrets versions access latest --secret=alert-discord-webhook-url \
      --project="${GCP_PROJECT_ID:-predictor-trading-2026}" 2>"$ERR")"; then
    echo "notify-discord: Secret Manager lookup of alert-discord-webhook-url failed:" >&2
    cat "$ERR" >&2
    exit 1
  fi
fi
if [ -z "$URL" ]; then
  echo "notify-discord: alert-discord-webhook-url is empty" >&2
  exit 1
fi
MSG="$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps({"content": sys.stdin.read()}))')"
# curl config syntax: escape backslashes and double quotes inside the value.
URL_ESC="$(printf '%s' "$URL" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
printf 'url = "%s"\n' "$URL_ESC" \
  | curl -sS --fail -K - -X POST -H 'Content-Type: application/json' -d "$MSG" >/dev/null
EOF

# ── Combined memory file ───────────────────────────────────────────────────────
sudo mkdir -p /data/predictor/.claude
sudo tee /data/predictor/CLAUDE.md > /dev/null << 'EOF'
# predictor-vm — Autonomous Trading Agent

## Identity
I am an autonomous trading system improvement agent running on predictor-vm (GCP).
I have unrestricted internet access from this VM.
/data/predictor is a persistent attached disk — it survives VM redeploys.
Everything else (home dir, apt packages, npm globals) is wiped on redeploy.
After any redeploy run: bash /data/predictor/setup-claude.sh

## Environment
| Resource | Path |
|---|---|
| Repo | /data/predictor/prediction-market |
| Python venv | /data/predictor/venv/bin/python3 |
| SQLite DB | /data/predictor/prediction-market/*.db |
| NVM | /data/predictor/.nvm (symlinked from ~/.nvm) |
| Claude config | /data/predictor/.claude (symlinked from ~/.claude) |
| Runtime config | /data/predictor/.env (non-secret; sourced by ~/.bashrc) |
| Secrets | GCP Secret Manager (never in .env) |
| Agent logs | /data/predictor/agent-daily.log, agent-weekly.log |
| Setup script | /data/predictor/setup-claude.sh |
| Daily agent | /data/predictor/run-daily-agent.sh |
| Weekly agent | /data/predictor/run-improvement-agent.sh |
| Prompts | /data/predictor/daily-prompt.txt, improvement-prompt.txt |

## Automation Schedule
- Mon–Sat 6am: Haiku quick fix or health check (run-daily-agent.sh)
- Sunday 6am: Sonnet deep analysis and improvement (run-improvement-agent.sh)

## Network
Full internet access. Reaches: discord.com, github.com, googleapis.com, polymarket, kalshi.
Dashboard API runs locally at http://localhost:8000.

---

# Prediction Market Trading System

## Purpose
Autonomous paper trading bot on Polymarket and Kalshi prediction markets.
Generates signals, routes orders, reconciles positions, serves a dashboard API.
Currently paper trading. Goal: validate strategies before going live.

## Architecture
```
core/signals/ → execution/router.py → execution/clients/ → SQLite DB
                                                         ↓
                                          execution/reconciliation.py
                                                         ↓
                                          scripts/dashboard_api.py → :8000
```
Service: `predictor` (systemd), runs as user `predictor`
Entry point: `scripts/trading_session.py --dashboard --dashboard-port 8000 --dashboard-host 0.0.0.0`

## Key Files
| File | Purpose |
|---|---|
| core/config.py | All config — strategies, risk controls, starting_capital |
| core/signals/ | Signal generation logic |
| execution/router.py | Routes signals; asyncio.gather(return_exceptions=True) |
| execution/reconciliation.py | Position counting via signal_id JOIN to orders |
| execution/clients/paper.py | Paper trading client |
| execution/clients/polymarket.py | Polymarket live client |
| execution/clients/kalshi.py | Kalshi live client |
| scripts/trading_session.py | Main session loop |
| scripts/dashboard_api.py | FastAPI: /api/overview /api/risk /api/positions /api/fees |
| deploy/predictor.service | Systemd unit |

## Strategies
Load live values: `from core.config import get_config; cfg = get_config()`
- P1_cross_market_arb — cross-market arbitrage
- P2_momentum — momentum signals
- P3_calibration_bias — calibration bias (min_spread >= 0.10)

## Database
SQLite at `/data/predictor/prediction-market/*.db`
Tables: `signals`, `orders`, `positions`, `order_events`
Platform resolution: `positions.signal_id → orders.signal_id` JOIN — never `strategy LIKE '%polymarket%'`

## Development Workflow
Always before committing:
```bash
/data/predictor/venv/bin/black execution/ scripts/ core/ tests/
/data/predictor/venv/bin/ruff check --fix execution/ scripts/ core/ tests/
/data/predictor/venv/bin/python3 -m pytest tests/ -x -q
```
- Stage only changed files — never `git add -A`
- Commit format: `fix: what and why` or `feat: what and why`
- Patch bump for bug fixes, minor for new features
- Tags trigger deploy: `git tag -a vX.Y.Z -m "..."` then `git push origin vX.Y.Z`

## Constraints
- Never drop or truncate the database
- Never force-push or amend published commits
- Never skip black/ruff/pytest before committing
- Never modify deploy/config.env or .env files
- Fix failing tests — never skip or comment out

## Health Check
```bash
sudo systemctl is-active predictor
sudo journalctl -u predictor -n 30 --no-pager
curl -s http://localhost:8000/api/overview | python3 -m json.tool
```

## Notifications
```bash
/data/predictor/notify-discord.sh "message here"
```
GitHub repo: tduic/prediction-market (GH_TOKEN in /data/predictor/.env)

## Known Fixes (v1.1.0)
- reconciliation.py: platform via signal_id JOIN, not strategy LIKE
- router.py: asyncio.gather return_exceptions=True + REJECTED wrapping
- trading_session.py: P3 assign_strategy threshold >= 0.10
- dashboard_api.py: N+1 Sharpe eliminated; PAPER_CAPITAL from config
- dashboard/src/App.tsx: removed invalid days param from fees/risk calls
- tests/paper/test_reconciliation.py: fixture inserts matching order row
EOF

# ── Daily prompt (Haiku) ───────────────────────────────────────────────────────
sudo tee /data/predictor/daily-prompt.txt > /dev/null << 'EOF'
You are a trading system maintenance agent. Do exactly the steps below in order.
Working directory: /data/predictor/prediction-market
Python: /data/predictor/venv/bin/python3

STEP 1 — HEALTH CHECK
  git pull origin main
  sudo systemctl is-active predictor
  sudo journalctl -u predictor -n 20 --no-pager | grep -iE "error|exception|traceback|critical" | tail -20

STEP 2 — DB ERROR SCAN
Run this and print the output:
  /data/predictor/venv/bin/python3 - << 'PYEOF'
import sqlite3, pathlib
db = next(pathlib.Path("/data/predictor/prediction-market").glob("*.db"), None)
if not db: exit()
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
print("=== Rejected orders (last 24h) ===")
for r in con.execute("SELECT * FROM order_events WHERE status='REJECTED' AND timestamp_utc >= datetime('now','-1 day') LIMIT 20"): print(dict(r))
print("=== Today P&L ===")
for r in con.execute("SELECT strategy, COUNT(*) trades, ROUND(SUM(actual_pnl),4) pnl FROM positions WHERE status='closed' AND created_at >= datetime('now','-1 day') GROUP BY strategy"): print(dict(r))
con.close()
PYEOF

STEP 3 — FIND ONE PROBLEM
Based on the health check and DB scan, identify the single most actionable problem:
- A Python exception in the journal → find and fix root cause
- Rejected orders with a pattern → fix the cause
- A strategy generating zero trades → check config/logic
- If system is healthy, check git log --oneline -10 and pick the smallest unaddressed improvement

If nothing is actionable, output "System healthy, no change needed" and stop.

STEP 4 — IMPLEMENT
Read the relevant file(s) before editing. Make the minimal correct change.
Do not refactor surrounding code or add comments to unchanged lines.

STEP 5 — TEST AND LINT
  cd /data/predictor/prediction-market
  /data/predictor/venv/bin/black execution/ scripts/ core/ tests/
  /data/predictor/venv/bin/ruff check --fix execution/ scripts/ core/ tests/
  /data/predictor/venv/bin/python3 -m pytest tests/ -x -q 2>&1 | tail -20
If tests fail, diagnose and fix. Do not skip failing tests.

STEP 6 — COMMIT AND PUSH
  git add <only changed files>
  git commit -m "fix: <one-line description>"
  git push -u origin main

STEP 7 — TAG
  LAST=$(git tag --list | sort -V | tail -1)
  # increment patch: e.g. v1.1.0 → v1.1.1
  git tag -a <new-version> -m "Daily fix: <description>"
  git push origin <new-version>

STEP 8 — DISCORD
  /data/predictor/notify-discord.sh "**Daily agent** <version>: <what was fixed>"
EOF

# ── Weekly prompt (Sonnet) ────────────────────────────────────────────────────
sudo tee /data/predictor/improvement-prompt.txt > /dev/null << 'EOF'
You are a trading system improvement agent. Run the full weekly improvement cycle below.
Working directory: /data/predictor/prediction-market
Python: /data/predictor/venv/bin/python3

PHASE 1 — HEALTH CHECK
  git pull origin main
  sudo systemctl is-active predictor
  sudo journalctl -u predictor -n 40 --no-pager

PHASE 2 — DB ANALYSIS
  /data/predictor/venv/bin/python3 - << 'PYEOF'
import sqlite3, pathlib
db = next(pathlib.Path("/data/predictor/prediction-market").glob("*.db"), None)
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
print("=== Last 10 signals ===")
for r in con.execute("SELECT id, strategy, created_at, status FROM signals ORDER BY created_at DESC LIMIT 10"): print(dict(r))
print("=== Open positions ===")
for r in con.execute("SELECT id, strategy, status, created_at FROM positions WHERE status='open' ORDER BY created_at DESC LIMIT 10"): print(dict(r))
print("=== 30-day P&L by strategy ===")
for r in con.execute("SELECT strategy, COUNT(*) trades, ROUND(SUM(actual_pnl),4) total, ROUND(AVG(actual_pnl),4) avg FROM positions WHERE status='closed' AND created_at >= datetime('now','-30 days') GROUP BY strategy ORDER BY total DESC"): print(dict(r))
print("=== Recent rejections ===")
for r in con.execute("SELECT * FROM order_events WHERE status='REJECTED' ORDER BY timestamp_utc DESC LIMIT 10"): print(dict(r))
con.close()
PYEOF

PHASE 3 — STRATEGY CONFIG
  /data/predictor/venv/bin/python3 - << 'PYEOF'
from core.config import get_config
cfg = get_config()
print("Starting capital:", cfg.risk_controls.starting_capital)
for name, s in cfg.strategies.items():
    print(f"{name}: enabled={s.enabled}, min_spread={getattr(s,'min_spread',None)}, max_position={getattr(s,'max_position_size',None)}")
PYEOF

PHASE 4 — DASHBOARD CHECK
  curl -s http://localhost:8000/api/overview | python3 -m json.tool
  curl -s http://localhost:8000/api/risk | python3 -m json.tool

PHASE 5 — CODEBASE ANALYSIS
Read these files in full:
  core/config.py, execution/router.py, execution/reconciliation.py,
  scripts/dashboard_api.py, scripts/trading_session.py

Build a ranked list of up to 5 improvements. For each: description, impact, regression risk.
Self-critique the list. Select top 1-3 to implement.

PHASE 6 — IMPLEMENT
For each selected improvement: read relevant file, make minimal correct change.
Do not refactor surrounding code or add docstrings to unchanged lines.

PHASE 7 — TEST AND LINT
  /data/predictor/venv/bin/black execution/ scripts/ core/ tests/
  /data/predictor/venv/bin/ruff check --fix execution/ scripts/ core/ tests/
  /data/predictor/venv/bin/python3 -m pytest tests/ -x -q 2>&1 | tail -30
Fix any failures. Do not skip tests.

PHASE 8 — COMMIT, PUSH, TAG
  git add <changed files only>
  git commit -m "feat/fix: <description>"
  git push -u origin main
  git tag -a <next-version> -m "Weekly: <summary>"
  git push origin <next-version>

PHASE 9 — DISCORD
  /data/predictor/notify-discord.sh "**Weekly agent** <version> shipped

<bullet summary of changes>"
EOF

# ── Daily run script (Haiku) ──────────────────────────────────────────────────
sudo tee /data/predictor/run-daily-agent.sh > /dev/null << 'EOF'
#!/bin/bash
set -euo pipefail
source /data/predictor/.env
export NVM_DIR=/data/predictor/.nvm
[ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"
echo "=== Daily agent started $(date) ===" >> /data/predictor/agent-daily.log
cd /data/predictor/prediction-market
claude --dangerously-skip-permissions \
       --model claude-haiku-4-5-20251001 \
       -p "$(cat /data/predictor/daily-prompt.txt)" \
  >> /data/predictor/agent-daily.log 2>&1
echo "=== Daily agent finished $(date) ===" >> /data/predictor/agent-daily.log
EOF

# ── Weekly run script (Sonnet) ────────────────────────────────────────────────
sudo tee /data/predictor/run-improvement-agent.sh > /dev/null << 'EOF'
#!/bin/bash
set -euo pipefail
source /data/predictor/.env
export NVM_DIR=/data/predictor/.nvm
[ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"
echo "=== Weekly agent started $(date) ===" >> /data/predictor/agent-weekly.log
cd /data/predictor/prediction-market
claude --dangerously-skip-permissions \
       --model claude-sonnet-4-6 \
       -p "$(cat /data/predictor/improvement-prompt.txt)" \
  >> /data/predictor/agent-weekly.log 2>&1
echo "=== Weekly agent finished $(date) ===" >> /data/predictor/agent-weekly.log
EOF

# ── Setup script (run after every redeploy) ───────────────────────────────────
sudo tee /data/predictor/setup-claude.sh > /dev/null << 'EOF'
#!/bin/bash
set -euo pipefail
LOG=/data/predictor/setup.log
# Owned by whoever runs setup (not world-writable).
sudo touch "$LOG" && sudo chown "$(id -un)" "$LOG" && sudo chmod 640 "$LOG"
echo "=== setup-claude.sh started $(date) ===" | tee -a "$LOG"

export NVM_DIR=/data/predictor/.nvm
if [ ! -f "$NVM_DIR/nvm.sh" ]; then
  echo "Installing nvm..." | tee -a "$LOG"
  sudo mkdir -p "$NVM_DIR" && sudo chown "$(id -un)" "$NVM_DIR" && sudo chmod 755 "$NVM_DIR"
  unset NVM_DIR
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh \
    | NVM_DIR=/data/predictor/.nvm bash >> "$LOG" 2>&1
  export NVM_DIR=/data/predictor/.nvm
fi
source "$NVM_DIR/nvm.sh"

nvm ls 20 &>/dev/null || nvm install 20 >> "$LOG" 2>&1
nvm use 20

command -v claude &>/dev/null || npm install -g @anthropic-ai/claude-code >> "$LOG" 2>&1

if [ ! -L "$HOME/.nvm" ]; then
  rm -rf "$HOME/.nvm" 2>/dev/null || true
  ln -s /data/predictor/.nvm "$HOME/.nvm"
fi

if [ ! -L "$HOME/.claude" ]; then
  rm -rf "$HOME/.claude" 2>/dev/null || true
  ln -s /data/predictor/.claude "$HOME/.claude"
fi

PROFILE="$HOME/.bashrc"
grep -q "data/predictor/.env" "$PROFILE" 2>/dev/null || cat >> "$PROFILE" << 'BASHRC'

# ── Persistent predictor-vm setup ──
export NVM_DIR=/data/predictor/.nvm
[ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"
[ -f /data/predictor/.env ] && source /data/predictor/.env
BASHRC

# Self-heal KALSHI_RSA_KEY_PATH in /data/predictor/.env.
# Kalshi market fetch fails with "Is a directory: '.'" when this is unset.
if [ -f /data/predictor/.env ] && ! sudo grep -q '^KALSHI_RSA_KEY_PATH=' /data/predictor/.env; then
  echo "KALSHI_RSA_KEY_PATH=/data/predictor/kalshi_private_key.pem" \
    | sudo tee -a /data/predictor/.env > /dev/null
  sudo chown predictor:predictor /data/predictor/.env
  sudo chmod 600 /data/predictor/.env
  echo "Re-added KALSHI_RSA_KEY_PATH to /data/predictor/.env" | tee -a "$LOG"
  sudo systemctl restart predictor 2>/dev/null || true
fi

# Source .env — use sudo cat in case running as non-predictor user
set -a
eval "$(sudo cat /data/predictor/.env 2>/dev/null || true)"
set +a

EXISTING=$(crontab -l 2>/dev/null || true)
UPDATED="$EXISTING"
echo "$EXISTING" | grep -qF "run-daily-agent"       || UPDATED="$UPDATED
0 6 * * 1-6 /data/predictor/run-daily-agent.sh"
echo "$EXISTING" | grep -qF "run-improvement-agent" || UPDATED="$UPDATED
0 6 * * 0 /data/predictor/run-improvement-agent.sh"
[ "$UPDATED" != "$EXISTING" ] && echo "$UPDATED" | crontab -

touch /data/predictor/agent-daily.log /data/predictor/agent-weekly.log

echo "=== setup-claude.sh done $(date) ===" | tee -a "$LOG"
echo "node: $(node --version) | claude: $(claude --version 2>/dev/null || echo ok) | cron: $(crontab -l | grep -c predictor) jobs"
EOF

# ── Permissions and run ───────────────────────────────────────────────────────
sudo chmod +x \
  /data/predictor/notify-discord.sh \
  /data/predictor/setup-claude.sh \
  /data/predictor/run-daily-agent.sh \
  /data/predictor/run-improvement-agent.sh

sudo rm -f /data/predictor/.claude/CLAUDE.md
sudo rm -f /data/predictor/prediction-market/CLAUDE.md 2>/dev/null || true

bash /data/predictor/setup-claude.sh
source ~/.bashrc

echo ""
echo "All done. Files in /data/predictor:"
ls -la /data/predictor/
