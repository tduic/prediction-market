#!/bin/bash
# Paste this entire file into the VM terminal.
# Safe to run multiple times — all operations are idempotent.
set -euo pipefail

# ── Credentials ───────────────────────────────────────────────────────────────
# .env is written by the deploy workflow from GitHub Actions secrets.
# Never add plaintext secrets here.
if [ ! -f /data/predictor/.env ]; then
  sudo touch /data/predictor/.env
  sudo chmod 600 /data/predictor/.env
  echo "Warning: /data/predictor/.env is empty. Run a deploy to populate secrets." >&2
fi

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
| Credentials | /data/predictor/.env (sourced by ~/.bashrc) |
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
Entry point: `scripts/paper_trading_session.py --refresh --stream --dashboard --dashboard-port 8000`

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
| scripts/paper_trading_session.py | Main session loop |
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
source /data/predictor/.env
curl -s -X POST "$DISCORD_WEBHOOK_URL" -H 'Content-Type: application/json' \
  -d '{"content": "message here"}'
```
GitHub repo: tduic/prediction-market (GH_TOKEN in /data/predictor/.env)

## Known Fixes (v1.1.0)
- reconciliation.py: platform via signal_id JOIN, not strategy LIKE
- router.py: asyncio.gather return_exceptions=True + REJECTED wrapping
- paper_trading_session.py: P3 assign_strategy threshold >= 0.10
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
  source /data/predictor/.env
  curl -s -X POST "$DISCORD_WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"content\": \"**Daily agent** <version>: <what was fixed>\"}"
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
  scripts/dashboard_api.py, scripts/paper_trading_session.py

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
  source /data/predictor/.env
  curl -s -X POST "$DISCORD_WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"content\": \"**Weekly agent** <version> shipped\n\n<bullet summary of changes>\"}"
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
sudo touch "$LOG" && sudo chmod 666 "$LOG"
echo "=== setup-claude.sh started $(date) ===" | tee -a "$LOG"

export NVM_DIR=/data/predictor/.nvm
if [ ! -f "$NVM_DIR/nvm.sh" ]; then
  echo "Installing nvm..." | tee -a "$LOG"
  sudo mkdir -p "$NVM_DIR" && sudo chmod 777 "$NVM_DIR"
  unset NVM_DIR
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh \
    | NVM_DIR=/data/predictor/.nvm bash >> "$LOG" 2>&1
  export NVM_DIR=/data/predictor/.nvm
fi
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

source /data/predictor/.env

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
