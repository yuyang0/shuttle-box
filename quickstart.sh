#!/usr/bin/env bash
# Quickstart for shuttle-box: pull the published image, start it with docker compose and
# print the MCP configuration an agent needs. It writes its own compose file, so it does
# not need the repository, and it does not use make.
#
#   ./quickstart.sh                     # pull :latest and start
#   TAG=v0.1.0 ./quickstart.sh          # pin a version
#   HOME_DIR=~/box ./quickstart.sh      # keep the generated compose file elsewhere
#
# Environment:
#   IMAGE        image reference           (default ghcr.io/yuyang0/shuttle-box)
#   TAG          image tag                 (default latest)
#   HOME_DIR     where the compose file lives (default ~/.local/share/shuttle-box)
#   CONTAINER    container name            (default shuttle-box)
#   NOVNC_PORT   noVNC on the host         (default 16080)
#   MCP_PORT     MCP over HTTP on the host (default 8765)
#   DISPLAY/VNC_PORT   X display and VNC port, change both to run a second instance
#   OPENROUTER_API_KEY optional: enables gui_plan's and script_run's model calls

set -euo pipefail

IMAGE=${IMAGE:-ghcr.io/yuyang0/shuttle-box}
TAG=${TAG:-latest}
HOME_DIR=${HOME_DIR:-$HOME/.local/share/shuttle-box}
CONTAINER=${CONTAINER:-shuttle-box}
NOVNC_PORT=${NOVNC_PORT:-16080}
MCP_PORT=${MCP_PORT:-8765}
DISPLAY=${DISPLAY:-:1}
VNC_PORT=${VNC_PORT:-5901}
SCREEN_GEOMETRY=${SCREEN_GEOMETRY:-1280x800}
READY_TIMEOUT=${READY_TIMEOUT:-90}

say() { printf '%s\n' "$*"; }
die() { printf 'quickstart: %s\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
command -v docker >/dev/null || die "docker not found"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found"
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon (permissions?)"

compose() {
    docker compose --project-directory "$HOME_DIR" --env-file "$HOME_DIR/.env" \
        -f "$HOME_DIR/docker-compose.yml" "$@"
}

# --- generated project -----------------------------------------------------
mkdir -p "$HOME_DIR"

if [ ! -f "$HOME_DIR/.env" ]; then
    token=$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(24))')
    cat > "$HOME_DIR/.env" <<EOF
SHUTTLE_MCP_TOKEN=$token
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
EOF
    chmod 600 "$HOME_DIR/.env"
    say "created $HOME_DIR/.env with a new MCP token"
fi

# One token for the whole run: an explicit SHUTTLE_MCP_TOKEN wins and is written back,
# otherwise the file's value is used. Everything below compares against this, not against
# the environment, which may not carry it at all.
if [ -n "${SHUTTLE_MCP_TOKEN:-}" ]; then
    grep -qx "SHUTTLE_MCP_TOKEN=$SHUTTLE_MCP_TOKEN" "$HOME_DIR/.env" 2>/dev/null \
        || sed -i "s|^SHUTTLE_MCP_TOKEN=.*|SHUTTLE_MCP_TOKEN=$SHUTTLE_MCP_TOKEN|" "$HOME_DIR/.env"
fi
TOKEN=$(sed -n 's/^SHUTTLE_MCP_TOKEN=//p' "$HOME_DIR/.env" | head -1)
[ -n "$TOKEN" ] || die "$HOME_DIR/.env has no SHUTTLE_MCP_TOKEN"
export SHUTTLE_MCP_TOKEN="$TOKEN"

# Variables already in .env win over defaults baked into the compose file below.
cat > "$HOME_DIR/docker-compose.yml" <<'YAML'
services:
  box:
    image: ${IMAGE}:${TAG}
    container_name: ${CONTAINER}
    # Host networking so the browser can reach a proxy listening on the host's 127.0.0.1.
    network_mode: host
    # Firefox builds a user namespace for its content sandbox, which the default seccomp
    # profile blocks.
    security_opt:
      - seccomp=unconfined
    shm_size: "2gb"
    environment:
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
      - SHUTTLE_MCP_TOKEN=${SHUTTLE_MCP_TOKEN:-}
      - MCP_HTTP_PORT=${MCP_PORT}
      - MCP_HTTP_BIND=127.0.0.1
      - DISPLAY=${DISPLAY}
      - VNC_PORT=${VNC_PORT}
      - NOVNC_PORT=${NOVNC_PORT}
      - NOVNC_BIND=127.0.0.1
      - SCREEN_GEOMETRY=${SCREEN_GEOMETRY}
      - TZ=${CONTAINER_TZ:-Asia/Shanghai}
      - LANG=${CONTAINER_LANG:-zh_CN.UTF-8}
      - LC_ALL=${CONTAINER_LANG:-zh_CN.UTF-8}
      - BROWSER_ACCEPT_LANG=${BROWSER_ACCEPT_LANG:-zh-CN,zh,en}
      - BROWSER_PROXY=${BROWSER_PROXY-http://127.0.0.1:7890}
      - BROWSER_NO_PROXY=${BROWSER_NO_PROXY:-}
    volumes:
      - box-home:/home/box
    restart: unless-stopped
    stop_grace_period: 15s

volumes:
  box-home:
YAML

# The generated compose file reads these from the environment, not from .env, so they
# travel through explicitly.
export IMAGE TAG CONTAINER NOVNC_PORT MCP_PORT DISPLAY VNC_PORT SCREEN_GEOMETRY

# --- start -----------------------------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    running_token=$(docker inspect "$CONTAINER" \
        --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^SHUTTLE_MCP_TOKEN=//p' | head -1)
    if [ "$running_token" != "$TOKEN" ]; then
        say "the container holds a different MCP token; recreating it"
        compose up -d --force-recreate
    else
        say "container $CONTAINER already exists; starting it instead of recreating"
        docker start "$CONTAINER" >/dev/null
    fi
else
    say "pulling ${IMAGE}:${TAG}"
    compose pull --quiet
    say "starting $CONTAINER"
    compose up -d
fi

say "waiting for the desktop to become ready (up to ${READY_TIMEOUT}s)"
deadline=$((SECONDS + READY_TIMEOUT))
until docker exec "$CONTAINER" test -f /home/box/.cache/box/ready 2>/dev/null; do
    if [ $SECONDS -ge $deadline ]; then
        docker logs --tail 30 "$CONTAINER" >&2 || true
        die "the desktop did not become ready; logs above, or: docker logs $CONTAINER"
    fi
    sleep 2
done
say "ready"

# --- report ----------------------------------------------------------------
token=$TOKEN
url="http://127.0.0.1:${MCP_PORT}/mcp"

cat <<EOF

  viewer   http://127.0.0.1:${NOVNC_PORT}/
  MCP url  ${url}
  header   Authorization: Bearer ${token}

  Claude Code
    claude mcp add --transport http shuttle ${url} \\
      --header "Authorization: Bearer ${token}"

  DSH / other Streamable HTTP clients
    type = http, url = $url, header Authorization: Bearer <token above>

  compose file  $HOME_DIR/docker-compose.yml
  stop          docker compose --project-directory $HOME_DIR -f $HOME_DIR/docker-compose.yml down

EOF
