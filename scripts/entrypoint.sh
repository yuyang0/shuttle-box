#!/usr/bin/env bash
# Container entry point (PID 1 is tini, which forwards signals here).
#
# It prepares the desktop user's directories, writes shuttle's config.toml from the
# environment when there is none yet, then hands the whole session to one D-Bus
# session bus: the desktop, the AT-SPI bus and `shuttle serve` must share it, or the
# daemon cannot read the accessibility tree.
set -euo pipefail

: "${SCREEN_GEOMETRY:=1920x1080}"
: "${SCREEN_DEPTH:=24}"
: "${VNC_PORT:=5901}"
: "${NOVNC_PORT:=16080}"
export SCREEN_GEOMETRY SCREEN_DEPTH VNC_PORT NOVNC_PORT

HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
export HOME
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"

mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" \
         "$XDG_DATA_HOME" "$HOME/.cache/box" "$HOME/Desktop"
chmod 700 "$XDG_RUNTIME_DIR"

# A persisted /home/box carries XFCE's saved display profile, and xfce4-settings
# re-applies it a few seconds into the session, overriding Xvnc's -geometry. Drop it
# so SCREEN_GEOMETRY stays the single source of truth for the desktop size.
rm -f "$XDG_CONFIG_HOME/xfce4/xfconf/xfce-perchannel-xml/displays.xml"

# Same trap for Firefox: a profile lock left by the previous container names a pid the
# new container may have reused, and Firefox then refuses to start ("already running").
# Nothing is running yet, so the locks are stale by definition.
rm -f "$HOME"/.mozilla/firefox/*/lock "$HOME"/.mozilla/firefox/*/.parentlock 2>/dev/null || true

# Typical GTK/Qt accessibility bridge variables; NO_AT_BRIDGE=0 keeps the bridge on.
export NO_AT_BRIDGE="${NO_AT_BRIDGE:-0}"
export GTK_MODULES="${GTK_MODULES:-atk-bridge}"
export QT_ACCESSIBILITY="${QT_ACCESSIBILITY:-1}"

# ---------------------------------------------------------------------------
# VNC password, when one is asked for
# ---------------------------------------------------------------------------
if [ -n "${VNC_PASSWORD:-}" ]; then
    printf '%s' "$VNC_PASSWORD" | vncpasswd -f > "$HOME/.vnc/passwd"
    chmod 600 "$HOME/.vnc/passwd"
else
    rm -f "$HOME/.vnc/passwd"
fi

# ---------------------------------------------------------------------------
# shuttle config: written once, then owned by the user (edit it in the container)
# ---------------------------------------------------------------------------
shuttle_cfg_dir="$XDG_CONFIG_HOME/loomhand/shuttle"
shuttle_cfg="$shuttle_cfg_dir/config.toml"
mkdir -p "$shuttle_cfg_dir"

if [ ! -f "$shuttle_cfg" ] || [ "${SHUTTLE_CONFIG_REGENERATE:-0}" = "1" ]; then
    base_url="${SHUTTLE_BASE_URL:-https://openrouter.ai/api/v1}"

    if [ -n "${SHUTTLE_DECIDE_API:-}" ]; then
        decide_api="$SHUTTLE_DECIDE_API"
    elif [ "$base_url" = "https://openrouter.ai/api/v1" ]; then
        decide_api=decisions
    else
        decide_api=chat
    fi

    if [ "$base_url" = "https://openrouter.ai/api/v1" ]; then
        default_decisions_url="https://openrouter.ai/api/alpha/decisions"
    else
        default_decisions_url="${base_url%/}/decisions"
    fi
    decisions_url="${SHUTTLE_DECISIONS_URL:-$default_decisions_url}"

    api_key_line=""
    if [ -n "${SHUTTLE_API_KEY:-}" ]; then
        api_key_line="api_key = \"${SHUTTLE_API_KEY}\""
    fi

    apps_line="[]"
    if [ -n "${SHUTTLE_SAFE_APPS:-}" ]; then
        apps_line="[$(printf '%s' "$SHUTTLE_SAFE_APPS" | sed 's/[[:space:]]*,[[:space:]]*/", "/g; s/^/"/; s/$/"/')]"
    fi

    umask 077
    cat > "$shuttle_cfg" <<EOF
# Written by /opt/box/entrypoint.sh on first start. See README.md for every knob.
base_url = "${base_url}"
decisions_url = "${decisions_url}"
api_key_env = "${SHUTTLE_API_KEY_ENV:-OPENROUTER_API_KEY}"
timeout_ms = ${SHUTTLE_TIMEOUT_MS:-60000}
decide_api = "${decide_api}"
${api_key_line}
[models]
decide = "${SHUTTLE_DECIDE_MODEL:-typesafe/jev-1.14}"
ground = "${SHUTTLE_GROUND_MODEL:-qwen/qwen3-vl-235b-a22b-instruct}"
ask = "${SHUTTLE_ASK_MODEL:-anthropic/claude-sonnet-5}"

[safety]
apps = ${apps_line}
kill_hotkey = "off"
indicator = false
confirm = "off"

[tools]
script_run = ${SHUTTLE_SCRIPT_RUN:-true}

[desk]
input = "${SHUTTLE_DESK_INPUT:-foreground}"
EOF
    chmod 600 "$shuttle_cfg"
fi

# ---------------------------------------------------------------------------
# One session bus for the desktop, AT-SPI and the shuttle daemon
# ---------------------------------------------------------------------------
exec dbus-run-session -- /opt/box/start-desktop.sh
