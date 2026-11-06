#!/usr/bin/env bash
# Reset the demo state: wipe both executors' state files and reload the ledger from data/acme.
# With --rooms, also remove the plan rooms a previous run opened (Gateway and Mattermost).
# The demo rooms, agents and people stay; `scripts/switch_stack.sh reset` wipes those.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
if [[ "${FORCE:-}" != "1" ]]; then
  read -r -p "Delete executor/*.db and reload the ledger from data/acme? [y/N] " yn
  [[ "$yn" == "y" || "$yn" == "Y" ]] || { echo "aborted"; exit 1; }
fi
scripts/dev_stack.sh stop >/dev/null 2>&1 || true
rm -f executor/switch.db executor/switch.db-* executor/ledger.db executor/ledger.db-* executor/books.db executor/books.db-* executor/state.db executor/state.db-*
uv run sindarin ledger import | tail -1
if [[ "${1:-}" == "--rooms" || "${ROOMS:-}" == "1" ]]; then uv run python scripts/reset_plan_rooms.py; fi
echo "removed executor state and reloaded the ledger. restart with scripts/dev_stack.sh start [--fake]"
