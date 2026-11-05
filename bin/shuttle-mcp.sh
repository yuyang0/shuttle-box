#!/usr/bin/env bash
# stdio MCP bridge to shuttle inside the container, for MCP clients that should not
# care whether the container happens to be running.
#
# It starts an existing container when needed, waits until the desktop session has
# actually come up (the container writes ~/.cache/box/ready at the end of its startup
# and removes it when it starts, so a stale marker cannot be mistaken for this boot),
# then execs `shuttle mcp` so this process becomes the stdio transport.
#
# Register it as the MCP server command:
#   claude mcp add shuttle -- /path/to/bin/shuttle-mcp.sh
#
# SHUTTLE_CONTAINER picks another container name; DOCKER_HOST is honoured, so a
# remote daemon works too. The container itself is created by docker run / compose.
set -euo pipefail

CONTAINER="${SHUTTLE_CONTAINER:-shuttle-box}"
READY="${SHUTTLE_READY_PATH:-/home/box/.cache/box/ready}"
TIMEOUT="${SHUTTLE_START_TIMEOUT:-45}"
COMPOSE_FILE="${SHUTTLE_COMPOSE_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker-compose.yml}"

# stdout carries the MCP stream, so every message of ours goes to stderr.

# Serialise create/start: two agents opening a session at the same moment must not
# race on `docker compose up`.
if command -v flock > /dev/null 2>&1; then
    exec 9> "${TMPDIR:-/tmp}/shuttle-mcp-${CONTAINER}.lock"
    flock 9
fi

if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
    if [ -f "$COMPOSE_FILE" ] && docker compose version > /dev/null 2>&1; then
        echo "shuttle-mcp: creating container $CONTAINER with $COMPOSE_FILE" >&2
        docker compose -f "$COMPOSE_FILE" up -d >&2 || {
            echo "shuttle-mcp: 'docker compose up -d' failed; see the output above." >&2
            exit 1
        }
    else
        echo "shuttle-mcp: no container named '$CONTAINER', and no compose file at" >&2
        echo "shuttle-mcp: $COMPOSE_FILE (set SHUTTLE_COMPOSE_FILE or SHUTTLE_CONTAINER)." >&2
        exit 1
    fi
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    echo "shuttle-mcp: starting container $CONTAINER" >&2
    docker start "$CONTAINER" > /dev/null
fi

if command -v flock > /dev/null 2>&1; then
    flock -u 9
    exec 9>&-
fi

for _ in $(seq 1 "$TIMEOUT"); do
    if docker exec "$CONTAINER" test -f "$READY" 2>/dev/null; then
        exec docker exec -i "$CONTAINER" shuttle mcp
    fi
    sleep 1
done

echo "shuttle-mcp: $CONTAINER did not finish starting within ${TIMEOUT}s." >&2
echo "shuttle-mcp: check 'docker exec $CONTAINER cat /home/box/.cache/box/logs/supervisor.log'" >&2
exit 1
