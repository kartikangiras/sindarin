#!/usr/bin/env bash
# Run the six Sindarin processes in the background (DDD §16.1): two executors, four agents.
#   scripts/dev_stack.sh start [--fake]   # --fake: agents on the stand-in model (no model key)
#   scripts/dev_stack.sh stop
#   scripts/dev_stack.sh status
# Logs: executor/<name>.log
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
EXECUTORS=(switch github)
AGENTS=(product coder reviewer release)

start_one() { # name, pattern, command...
  local name="$1" pat="$2"; shift 2
  if pgrep -f "$pat" >/dev/null; then echo "$name: already running"; return; fi
  nohup "$@" > "executor/$name.log" 2>&1 &
  echo "$name: started (executor/$name.log)"
}

case "${1:-status}" in
  start)
    shift
    if [[ "${1:-}" == "--fake" || "${1:-}" == "--fake-policy" ]]; then export SINDARIN_MODEL=fake; fi
    for e in "${EXECUTORS[@]}"; do
      start_one "sindarin-$e" "sindarin-exec run --executor $e" uv run sindarin-exec run --executor "$e"
    done
    for a in "${AGENTS[@]}"; do
      start_one "$a" "sindarin-agent run $a" uv run sindarin-agent run "$a"
    done
    echo "model: ${SINDARIN_MODEL:-claude}"
    sleep 3; "$0" status ;;
  stop)
    for e in "${EXECUTORS[@]}"; do pkill -f "sindarin-exec run --executor $e" 2>/dev/null && echo "sindarin-$e stopped" || true; done
    for a in "${AGENTS[@]}"; do pkill -f "sindarin-agent run $a" 2>/dev/null && echo "$a stopped" || true; done ;;
  status)
    for e in "${EXECUTORS[@]}"; do pgrep -f "sindarin-exec run --executor $e" >/dev/null && echo "sindarin-$e: running" || echo "sindarin-$e: stopped"; done
    for a in "${AGENTS[@]}"; do pgrep -f "sindarin-agent run $a" >/dev/null && echo "$a: running" || echo "$a: stopped"; done ;;
  *) echo "usage: $0 start [--fake] | stop | status" >&2; exit 2 ;;
esac
