#!/usr/bin/env bash
# Edge launcher for the container.
#
# Chromium ignores http_proxy / HTTPS_PROXY, and Edge's machine policy directory
# (/etc/opt/edge/policies) is root-owned while the desktop runs as `box`, so the proxy is
# handed over as a command-line flag on every launch instead: BROWSER_PROXY becomes
# --proxy-server, BROWSER_NO_PROXY becomes --proxy-bypass-list. Both desktop entries and
# `desk launch app=microsoft-edge` resolve to this script, so every window is proxied.
set -euo pipefail

REAL_EDGE=/usr/bin/microsoft-edge

# Chromium only publishes an AT-SPI tree on the desktop session bus; a bare `docker exec`
# start leaves shuttle with nothing but pixels. Scripted checks (`--version`, `--headless`)
# need no tree, so they stay quiet.
case " $* " in
    *" --version "* | *" --help "* | *" --headless"* | *" --dump-dom "*) ;;
    *)
        if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
            echo "microsoft-edge: DBUS_SESSION_BUS_ADDRESS is unset; this Edge will have no" >&2
            echo "microsoft-edge: accessibility tree. Start it through the desktop (shuttle's launch)." >&2
        fi
        ;;
esac

# A container profile is brand new on first use: skip the welcome tour and the
# default-browser prompt that would otherwise sit above the page.
args=(--no-first-run --no-default-browser-check)

if [ -n "${BROWSER_PROXY:-}" ]; then
    args+=("--proxy-server=${BROWSER_PROXY%/}")
    bypass="localhost;127.0.0.1"
    if [ -n "${BROWSER_NO_PROXY:-}" ]; then
        bypass="$bypass;$(printf '%s' "${BROWSER_NO_PROXY}" | tr -d '[:space:]' | tr ',' ';')"
    fi
    args+=("--proxy-bypass-list=${bypass}")
fi

exec "$REAL_EDGE" "${args[@]}" "$@"
