#!/usr/bin/env bash
# One command for a reviewer: Switch with Mattermost in Docker, the ledger, the bootstrap,
# the two executors and the three agents. Works with no model key (the agents run on the
# deterministic stand-in); uses Claude when ANTHROPIC_API_KEY is in the root .env.
# One person drives the demo (PEOPLE=me, the default); PEOPLE=cast or PEOPLE=both adds the team.
#
#   scripts/demo.sh            # bring everything up
#   scripts/demo.sh stop       # stop the six processes (Switch keeps running)
#   scripts/demo.sh reset      # stop everything and wipe Switch's data and the executors' state
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
STACK_ENV="${STACK_ENV:-$HOME/.sindarin/switch-stack/.env}"

case "${1:-up}" in
  stop) scripts/dev_stack.sh stop; exit 0 ;;
  reset)
    "$0" stop
    scripts/switch_stack.sh reset
    rm -f executor/*.db executor/*.db-*
    echo "wiped. run scripts/demo.sh to start fresh."
    exit 0 ;;
  up) ;;
  *) echo "usage: $0 [up|stop|reset]" >&2; exit 2 ;;
esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need docker "https://docs.docker.com/get-docker/"; need uv "https://docs.astral.sh/uv/"
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop and run this again." >&2; exit 1; }

echo "▸ python deps";   uv sync -q
echo "▸ switch + mattermost"; PROFILES="gateway collab" scripts/switch_stack.sh up | tail -2

scripts/dev_stack.sh stop >/dev/null 2>&1 || true
echo "▸ ledger";        uv run sindarin ledger import | tail -1
echo "▸ bootstrap";     uv run sindarin bootstrap --stack-env "$STACK_ENV" --people "${PEOPLE:-me}" | grep -E "FAIL|BOOTSTRAP|created|registered|room|board" || true
# payment runs go out by ACH from a free Increase sandbox (no signup, no real money) when it
# is reachable; otherwise they stay a bank file a person confirms. RAIL=file|stripe|increase.
echo "▸ payment rail";  uv run sindarin rail "${RAIL:-increase}" 2>&1 | tail -1 || true

MODEL_FLAG=""
if grep -qsE '^ANTHROPIC_API_KEY=\S' .env; then
  echo "▸ agents: Claude (ANTHROPIC_API_KEY found)"
else
  MODEL_FLAG="--fake"; echo "▸ agents: stand-in model (no ANTHROPIC_API_KEY in .env)"
fi
scripts/dev_stack.sh start $MODEL_FLAG | grep -E "started|running|stopped|model"

uv run sindarin doctor --stack-env "$STACK_ENV" | tail -1

echo
echo "Sindarin is up. Mattermost: http://localhost:8065 (team switch: #acme-finance, #planning)"
echo "You drive it as $(grep -E '^MATTERMOST_USER=' "$STACK_ENV" .env 2>/dev/null | head -1 | cut -d= -f2). A team instead: PEOPLE=cast scripts/demo.sh · stop: scripts/demo.sh stop"
