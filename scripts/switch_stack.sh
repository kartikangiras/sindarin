#!/usr/bin/env bash
# Run Switch with its bundled Mattermost (DDD §1.3, §16.1): the standalone Docker Compose stack
# from github.com/sandbox-quantum/switch with switch-core, PostgreSQL, the Gateway and, under the
# `collab` profile, Mattermost plus the seeder that registers the Mattermost bridge as the default.
#
#   scripts/switch_stack.sh up      # clone (shallow), write .env with random secrets, pull, start
#   scripts/switch_stack.sh status
#   scripts/switch_stack.sh down    # stop (keeps volumes)
#   scripts/switch_stack.sh reset   # stop and delete volumes (rooms, messages, agents, users)
#
# Then:  uv run sindarin bootstrap --stack-env "$STACK_DIR/.env"
# Ports: bridge :8000, Gateway :3000, Mattermost :8065 (override in $STACK_DIR/.env).
set -euo pipefail

STACK_DIR="${STACK_DIR:-$HOME/.sindarin/switch-stack}"
SRC_DIR="${SRC_DIR:-$STACK_DIR/src}"
SWITCH_VERSION="${SWITCH_VERSION:-latest}"
POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-55432}"
PROFILES="${PROFILES:-gateway collab}"
COMPOSE=(docker compose -f "$STACK_DIR/standalone-docker-compose.yml" --project-directory "$STACK_DIR" -p sindarin-switch)
for p in $PROFILES; do COMPOSE+=(--profile "$p"); done

cmd="${1:-up}"
case "$cmd" in
  up)
    mkdir -p "$STACK_DIR"
    if [[ ! -d "$SRC_DIR/.git" ]]; then
      git clone -q --depth 1 https://github.com/sandbox-quantum/switch "$SRC_DIR"
    fi
    cp "$SRC_DIR/deploy/local/standalone-docker-compose.yml" "$STACK_DIR/"
    if [[ ! -f "$STACK_DIR/.env" ]]; then
      cp "$SRC_DIR/.env.example" "$STACK_DIR/.env"
      for k in DB_PASSWORD DB_OWNER_PASSWORD AGENT_REGISTRATION_TOKEN GATEWAY_ADMIN_PASSWORD \
               MATTERMOST_ADMIN_PASSWORD MATTERMOST_USER_PASSWORD; do
        v=$(openssl rand -hex 24); sed -i.bak "s|^${k}=.*|${k}=${v}|" "$STACK_DIR/.env"
      done
      sed -i.bak "s|^SECRET_KEYS=.*|SECRET_KEYS=local:$(openssl rand -hex 32)|" "$STACK_DIR/.env"
      sed -i.bak "s|^SWITCH_VERSION=.*|SWITCH_VERSION=${SWITCH_VERSION}|" "$STACK_DIR/.env"
      sed -i.bak "s|^POSTGRES_HOST_PORT=.*|POSTGRES_HOST_PORT=${POSTGRES_HOST_PORT}|" "$STACK_DIR/.env"
      rm -f "$STACK_DIR/.env.bak"; chmod 600 "$STACK_DIR/.env"
      echo "wrote $STACK_DIR/.env (secrets generated; gateway admin: $(grep '^GATEWAY_ADMIN_EMAIL=' "$STACK_DIR/.env" | cut -d= -f2-))"
    fi
    # an older stack env may still carry the v2 feature flag; the documented path does not need it
    sed -i.bak 's|^FEATURE_FLAGS_ENABLED=agent_chats$|FEATURE_FLAGS_ENABLED=|' "$STACK_DIR/.env" && rm -f "$STACK_DIR/.env.bak"
    for p in 8000 3000 8065 "$POSTGRES_HOST_PORT"; do
      if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then echo "port $p is busy; override it in $STACK_DIR/.env" >&2; fi
    done
    "${COMPOSE[@]}" pull
    "${COMPOSE[@]}" up -d
    echo "waiting for the bridge and Mattermost..."
    for _ in $(seq 1 120); do
      if curl -fsS http://localhost:8000/health >/dev/null 2>&1 && curl -fsS http://localhost:8065/api/v4/system/ping >/dev/null 2>&1; then
        echo "bridge up: http://localhost:8000 · Mattermost: http://localhost:8065 · Gateway: http://localhost:3000"; break
      fi
      sleep 2
    done
    "${COMPOSE[@]}" ps
    echo "next: uv run sindarin bootstrap --stack-env \"$STACK_DIR/.env\""
    ;;
  status) "${COMPOSE[@]}" ps ;;
  down) "${COMPOSE[@]}" down ;;
  reset) "${COMPOSE[@]}" down -v ;;
  *) echo "usage: $0 up|status|down|reset" >&2; exit 2 ;;
esac
