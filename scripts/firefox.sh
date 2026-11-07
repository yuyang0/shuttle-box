#!/usr/bin/env bash
# Firefox launcher for the container.
#
# Firefox ignores http_proxy / HTTPS_PROXY, so the proxy goes into the profile's
# user.js, rewritten on every launch from BROWSER_PROXY. One fixed profile is used so
# cookies and logins survive a restart of the container.
set -euo pipefail

REAL_FIREFOX=/usr/bin/firefox
PROFILE_DIR="${FIREFOX_PROFILE_DIR:-$HOME/.mozilla/firefox/agent.default}"

# Firefox only publishes an AT-SPI tree when it runs on the desktop session bus, which
# is what shuttle reads. Starting it outside that bus (a bare `docker exec`) gives a
# browser shuttle can only drive by pixels.
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    echo "firefox: DBUS_SESSION_BUS_ADDRESS is unset; this Firefox will have no" >&2
    echo "firefox: accessibility tree. Start it through the desktop (shuttle's launch, or make browser)." >&2
fi

mkdir -p "$PROFILE_DIR"
# A stale lock left by a killed instance makes Firefox refuse to start. Remove it only when no
# instance is running: clearing it under a live instance lets a second one share the profile,
# and the two then rewrite cookies.sqlite under each other (an X login was lost that way).
if ! pgrep -x firefox-bin >/dev/null 2>&1; then
    rm -f "$PROFILE_DIR/lock" "$PROFILE_DIR/.parentlock"
fi


proxy="${BROWSER_PROXY:-}"
languages="${BROWSER_ACCEPT_LANG:-zh-CN,zh,en}"

{
    echo '// written by /usr/local/bin/firefox on every launch'
    if [ -n "$proxy" ]; then
        hostport="${proxy#*://}"
        hostport="${hostport%%/*}"
        host="${hostport%:*}"
        port="${hostport##*:}"
        if [ "$port" = "$host" ]; then
            port=8080
        fi
        case "$port" in
            '' | *[!0-9]*) port=8080 ;;
        esac
        echo 'user_pref("network.proxy.type", 1);'
        echo "user_pref(\"network.proxy.http\", \"${host}\");"
        echo "user_pref(\"network.proxy.http_port\", ${port});"
        echo "user_pref(\"network.proxy.ssl\", \"${host}\");"
        echo "user_pref(\"network.proxy.ssl_port\", ${port});"
        echo 'user_pref("network.proxy.share_proxy_settings", true);'
        echo "user_pref(\"network.proxy.no_proxies_on\", \"${BROWSER_NO_PROXY:-localhost, 127.0.0.1}\");"
    else
        echo 'user_pref("network.proxy.type", 5);'
    fi
    echo "user_pref(\"intl.accept_languages\", \"${languages}\");"
    # Firefox greets a new profile with a "Welcome to Firefox / Terms of Use" dialog that
    # sits above the page and swallows agent input until someone clicks Continue. These
    # two prefs record that consent, so a fresh profile is usable immediately.
    echo 'user_pref("termsofuse.acceptedDate", "1793838206821");'
    echo 'user_pref("termsofuse.acceptedVersion", 4);' 
    # A container desktop wants a browser window, not onboarding: skip the first-run
    # screens and the default-browser prompt that would otherwise swallow the first clicks.
    echo 'user_pref("browser.aboutwelcome.enabled", false);'
    echo 'user_pref("browser.shell.checkDefaultBrowser", false);'
    echo 'user_pref("browser.startup.homepage_override.mstone", "ignore");'
    echo 'user_pref("datareporting.policy.dataSubmissionEnabled", false);'
    echo 'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);'
    echo 'user_pref("browser.privatebrowsing.autostart", false);'
} > "$PROFILE_DIR/user.js"

exec "$REAL_FIREFOX" -profile "$PROFILE_DIR" "$@"
