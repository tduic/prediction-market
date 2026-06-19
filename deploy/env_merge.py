"""
Merge KEY=VALUE pairs from /tmp/predictor_updates.env into /data/predictor/.env.

Updates existing keys in-place; appends new ones.
Preserves keys not in the update file (e.g. EXECUTION_MODE set manually on VM).
"""

import os
import re

UPDATES_PATH = "/tmp/predictor_updates.env"
ENV_PATH = "/data/predictor/.env"

updates = {}
with open(UPDATES_PATH) as f:
    for line in f.read().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            updates[k.strip()] = v.strip()

existing = ""
if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        existing = f.read()

for key, val in updates.items():
    pat = re.compile(r"^" + re.escape(key) + r"=.*$", re.MULTILINE)
    entry = key + "=" + val
    if pat.search(existing):
        existing = pat.sub(entry, existing)
    else:
        existing = existing.rstrip("\n") + "\n" + entry + "\n"

with open(ENV_PATH, "w") as f:
    f.write(existing)
os.remove(UPDATES_PATH)
