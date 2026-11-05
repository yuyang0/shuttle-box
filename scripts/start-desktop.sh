#!/usr/bin/env bash
# Runs inside the container's single D-Bus session (see entrypoint.sh) and brings up,
# in order: Xvnc -> XFCE session -> AT-SPI bus -> shuttle daemon -> noVNC (+ optional
# HTTP MCP bridge). Every long-lived process is supervised and restarted on exit.
set -uo pipefail

LOG_DIR="${LOG_DIR:-$HOME/.cache/box/logs}"
READY_MARKER="${READY_MARKER:-$HOME/.cache/box/ready}"
mkdir -p "$LOG_DIR"
rm -f "$READY_MARKER"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG_DIR/supervisor.log"; }

: "${SCREEN_GEOMETRY:=1920x1080}"
: "${SCREEN_DEPTH:=24}"
: "${VNC_PORT:=5901}"
: "${NOVNC_PORT:=16080}"
# empty means every interface; 127.0.0.1 (the default) keeps the viewer local
: "${NOVNC_BIND-127.0.0.1}"
export DISPLAY="${DISPLAY:-:1}"

say "session bus ${DBUS_SESSION_BUS_ADDRESS:-unset}, display ${DISPLAY}, screen ${SCREEN_GEOMETRY}x${SCREEN_DEPTH}"

# --- 1. X server (Xvnc: one process for both X11 and VNC) -------------------
vnc_security=(-SecurityTypes None)
if [ -f "$HOME/.vnc/passwd" ]; then
    vnc_security=(-SecurityTypes VncAuth -PasswordFile "$HOME/.vnc/passwd")
fi

Xvnc "$DISPLAY" \
    -geometry "$SCREEN_GEOMETRY" \
    -depth "$SCREEN_DEPTH" \
    -rfbport "$VNC_PORT" \
    -localhost \
    -desktop "shuttle-box" \
    -AlwaysShared \
    -AcceptSetDesktopSize 1 \
    -nolisten tcp \
    "${vnc_security[@]}" >> "$LOG_DIR/xvnc.log" 2>&1 &

for _ in $(seq 1 100); do
    if xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then break; fi
    sleep 0.1
done
if ! xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then
    say "X server did not come up; see $LOG_DIR/xvnc.log"
    exit 1
fi
say "X server ready on ${DISPLAY}"

# --- 2. Desktop session ------------------------------------------------------
startxfce4 >> "$LOG_DIR/xfce.log" 2>&1 &
say "xfce4-session started"

# --- 3. Accessibility bus ----------------------------------------------------
# The launcher needs DISPLAY when it starts: it then publishes AT_SPI_BUS on the
# root window, which is how shuttle's desk backend finds the bus.
for _ in $(seq 1 100); do
    if dbus-send --session --type=method_call --print-reply \
            --dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress \
            > /dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
if at_spi_bus="$(xprop -display "$DISPLAY" -root AT_SPI_BUS 2>/dev/null | sed 's/^[^=]*= //')"; then
    say "AT-SPI bus ${at_spi_bus}"
else
    say "AT-SPI bus not yet on the root window; apps publish it on first use"
fi

# --- 4. Supervision helper ---------------------------------------------------
pids=()
respawn() {
    local name=$1; shift
    (
        while :; do
            "$@" >> "$LOG_DIR/$name.log" 2>&1
            say "$name exited with status $?, restarting in 2s"
            sleep 2
        done
    ) &
    pids+=("$!")
    say "$name started"
}

cleanup() {
    say "shutting down"
    trap - TERM INT
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
    pkill -TERM -f 'websockify' 2>/dev/null || true
    pkill -TERM -f 'shuttle serve' 2>/dev/null || true
    pkill -TERM -f 'cua-driver' 2>/dev/null || true
    pkill -TERM -f 'xfce' 2>/dev/null || true
    pkill -TERM -f 'Xvnc' 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

# --- 5. shuttle daemon -------------------------------------------------------
# `shuttle mcp` (what agents run) is a thin proxy to this daemon over desk.sock, so
# one daemon serves every agent session.
respawn shuttle-serve shuttle serve

desk_sock="${SHUTTLE_SOCKET:-$XDG_CACHE_HOME/loomhand/shuttle/desk.sock}"
for _ in $(seq 1 150); do
    if [ -S "$desk_sock" ]; then break; fi
    sleep 0.1
done
if [ -S "$desk_sock" ]; then
    say "shuttle daemon listening on ${desk_sock}"
else
    say "warning: ${desk_sock} is missing; see $LOG_DIR/shuttle-serve.log"
fi

# --- 6. noVNC ----------------------------------------------------------------
# NOVNC_BIND pins the viewer to one address; with --network=host the port would
# otherwise land on every host interface.
respawn novnc websockify --web=/usr/share/novnc "${NOVNC_BIND:+${NOVNC_BIND}:}${NOVNC_PORT}" "127.0.0.1:$VNC_PORT"

say "ready: noVNC on :${NOVNC_PORT}/vnc.html, VNC on 127.0.0.1:${VNC_PORT}"; \
    say "MCP is stdio only: docker exec -i <container> shuttle mcp"
# Readiness marker for clients that start the container on demand: it is removed
# above, so a stale one from a previous boot can never be mistaken for this one.
date -Is > "$READY_MARKER"
wait
