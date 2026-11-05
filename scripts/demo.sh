#!/usr/bin/env bash
# One command for a reviewer (DDD §2, §16.1): Switch with Mattermost in Docker, the bootstrap,
# the two executors and the four agents. Works with no GitHub token (sindarin-github simulates
# the repository) and no model key (the agents run on the deterministic stand-in); uses the real
# ones when they are present in executor/github.env and agents/*/.env.
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
    rm -f executor/*.db executor/*.db-* executor/simulated-github.json
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

GITHUB_MODE=real
if ! grep -qE '^GITHUB_TOKEN=\S' executor/github.env 2>/dev/null; then
  GITHUB_MODE=simulated
  echo "▸ no GITHUB_TOKEN in executor/github.env → GitHub is simulated (merges and releases are recorded, not sent)."
fi
scripts/dev_stack.sh stop >/dev/null 2>&1 || true
echo "▸ bootstrap";     uv run sindarin bootstrap --stack-env "$STACK_ENV" --github "$GITHUB_MODE" | grep -E "FAIL|BOOTSTRAP|created|registered|room|board" || true

MODEL_FLAG=""
if grep -qE '^ANTHROPIC_API_KEY=\S' agents/product/.env 2>/dev/null; then
  echo "▸ agents: Claude (ANTHROPIC_API_KEY found)"
else
  MODEL_FLAG="--fake"; echo "▸ agents: stand-in model (no ANTHROPIC_API_KEY in agents/*/.env)"
fi
scripts/dev_stack.sh start $MODEL_FLAG | grep -E "started|running|stopped|model"

uv run sindarin doctor --stack-env "$STACK_ENV" | tail -1

echo
echo "Sindarin is up. Mattermost: http://localhost:8065 (team switch: #acme-shared, #product, #releases)"
echo "Demo people: isaac, leonardo, john, vincent · password Sindarin-demo-1 · stop: scripts/demo.sh stop"
