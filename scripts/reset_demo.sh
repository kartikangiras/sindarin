#!/usr/bin/env bash
# Reset the demo state (DDD §18): wipe both executors' state files and the simulated repository.
# Switch rooms, agents and people stay; `scripts/switch_stack.sh reset` wipes those.
# With a real GitHub repo, close or delete the demo PR and tag by hand (nothing here writes to GitHub).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
if [[ "${FORCE:-}" != "1" ]]; then
  read -r -p "Delete executor/*.db and executor/simulated-github.json? [y/N] " yn
  [[ "$yn" == "y" || "$yn" == "Y" ]] || { echo "aborted"; exit 1; }
fi
scripts/dev_stack.sh stop >/dev/null 2>&1 || true
rm -f executor/switch.db executor/switch.db-* executor/github.db executor/github.db-* executor/simulated-github.json
echo "removed executor state. restart with scripts/dev_stack.sh start [--fake]"
