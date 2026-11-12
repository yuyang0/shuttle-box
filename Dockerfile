# syntax=docker/dockerfile:1

# Ubuntu 24.04 + XFCE4 desktop in a container, driven by shuttle (loomhand/shuttle) over MCP.
#
#   GUI over the web   TigerVNC Xvnc (:1) + noVNC/websockify  ->  http://<host>:16080/
#   MCP for agents     shuttle's stdio proxy, reached as `docker exec -i <container> shuttle mcp`
#
# shuttle's Linux backend needs X11 (XTest typing, MIT-SHM capture) and AT-SPI over
# D-Bus, so the desktop, the accessibility bus and `shuttle serve` all run inside one
# D-Bus session that the entry point owns.
#
# --build-arg APT_MIRROR=https://mirrors.ustc.edu.cn/ubuntu swaps in another apt
# mirror; the default is the upstream archive.

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG APT_MIRROR=
ARG SHUTTLE_VERSION=0.1.48
# Point at a mirror when github.com is slow: e.g.
#   --build-arg SHUTTLE_RELEASE_BASE=https://ghfast.top/https://github.com/loomhand/shuttle/releases/download
ARG SHUTTLE_RELEASE_BASE=https://github.com/loomhand/shuttle/releases/download
ARG USER_NAME=box
ARG USER_UID=1000
ARG USER_GID=1000

ENV LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    TZ=Asia/Shanghai \
    DISPLAY=:1 \
    SCREEN_GEOMETRY=1280x800 \
    SCREEN_DEPTH=24 \
    VNC_PORT=5901 \
    NOVNC_PORT=16080 \
    NOVNC_BIND=127.0.0.1 \
    MCP_HTTP_BIND=127.0.0.1 \
    MCP_HTTP_PORT=8765 \
    NO_AT_BRIDGE=0 \
    GTK_MODULES=atk-bridge \
    QT_ACCESSIBILITY=1

# ---------------------------------------------------------------------------
# Desktop, VNC, accessibility — one apt pass, cached across rebuilds
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
        mirror="${APT_MIRROR%/}"; \
        case "${mirror}" in https://*) mirror="http://${mirror#https://}" ;; esac; \
        sed -i "s|http://archive.ubuntu.com/ubuntu|${mirror}|g; \
                s|http://security.ubuntu.com/ubuntu|${mirror}|g" \
            /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update -o Acquire::Retries=3; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg jq less procps tini xz-utils \
        locales tzdata \
        dbus dbus-x11 at-spi2-core libatk-adaptor \
        xfce4 xfce4-terminal xfce4-taskmanager mousepad xterm \
        x11-utils x11-xserver-utils xauth xdg-utils xdg-user-dirs \
        fonts-dejavu-core fonts-liberation fonts-noto-cjk \
        tigervnc-standalone-server tigervnc-common \
        python3-venv; \
    sed -i "s/^# *zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/" /etc/locale.gen; \
    locale-gen; \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime; \
    echo "Asia/Shanghai" > /etc/timezone

# ---------------------------------------------------------------------------
# noVNC + websockify from upstream. Ubuntu's `novnc` needs node-acorn, so it drags
# in nodejs, and its `websockify` drags in python3-numpy; together that is ~116 MB of
# runtime we never use, and the packages are two releases behind. websockify's
# numpy/redis/jwcrypto requirements are only imported by its token plugins, so
# --no-deps is enough for a plain web proxy.
# ---------------------------------------------------------------------------
ARG NOVNC_VERSION=1.7.0
ARG NOVNC_SHA256=b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6
ARG WEBSOCKIFY_VERSION=0.13.0
ARG NUMPY_VERSION=2.3.4

RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    set -eux; \
    cd /tmp; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 15 \
        -o novnc.tar.gz "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz"; \
    echo "${NOVNC_SHA256}  novnc.tar.gz" | sha256sum -c -; \
    mkdir -p /usr/share/novnc; \
    tar -xzf novnc.tar.gz -C /usr/share/novnc --strip-components=1; \
    rm -rf /usr/share/novnc/.github /usr/share/novnc/docs /usr/share/novnc/tests \
           /usr/share/novnc/po /usr/share/novnc/karma.conf.cjs /usr/share/novnc/eslint.config.mjs; \
    rm -f novnc.tar.gz; \
    python3 -m venv /opt/websockify; \
    /opt/websockify/bin/pip install --no-cache-dir --no-deps \
        "websockify==${WEBSOCKIFY_VERSION}" "numpy==${NUMPY_VERSION}"; \
    ln -sf /opt/websockify/bin/websockify /usr/local/bin/websockify; \
    rm -rf /opt/websockify/lib/python3*/site-packages/pip* \
           /opt/websockify/lib/python3*/site-packages/setuptools* \
           /opt/websockify/lib/python3*/site-packages/_distutils_hack \
           /opt/websockify/lib/python3*/site-packages/distutils-precedence.pth \
           /opt/websockify/bin/pip*; \
    find /opt/websockify/lib/python3*/site-packages/numpy -type d -name tests -prune \
         -exec rm -rf {} + 2>/dev/null || true; \
    websockify --help > /dev/null; \
    test -f /usr/share/novnc/vnc.html

# ---------------------------------------------------------------------------
# Firefox from Mozilla's own apt repository: Ubuntu's `firefox` package is a snap
# stub, and snap does not run in a plain container.
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 15 \
        https://packages.mozilla.org/apt/repo-signing-key.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/packages.mozilla.org.gpg; \
    printf 'deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.gpg] https://packages.mozilla.org/apt mozilla main\n' \
        > /etc/apt/sources.list.d/mozilla.list; \
    printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' \
        > /etc/apt/preferences.d/mozilla; \
    apt-get update -o Acquire::Retries=3; \
    apt-get install -y --no-install-recommends firefox; \
    mkdir -p /etc/firefox/policies; \
    firefox --version

# ---------------------------------------------------------------------------
# Microsoft Edge from Microsoft's own apt repository: the desktop's Chromium, and what the
# source-watch X module drives by default. Its `--app=` windows carry no address bar and
# use the page name as the window title, so they never collide with the user's own Edge
# window. Ubuntu's archive has no Edge package at all.
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    arch="$(dpkg --print-architecture)"; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 15 \
        https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /etc/apt/keyrings/microsoft-edge.gpg; \
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main\n' "${arch}" \
        > /etc/apt/sources.list.d/microsoft-edge.list; \
    apt-get update -o Acquire::Retries=3; \
    apt-get install -y --no-install-recommends microsoft-edge-stable; \
    microsoft-edge --version

# ---------------------------------------------------------------------------
# WeChat: the official Linux client from Tencent's CDN. It bundles a Chromium runtime
# (XWeb) and uses private libraries, but does not declare its runtime dependencies in
# the control file, so the four it needs are installed before the package itself.
# ---------------------------------------------------------------------------
ARG WECHAT_URL=https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    apt-get update -o Acquire::Retries=3; \
    apt-get install -y --no-install-recommends libatomic1 libnss3 libnspr4; \
    curl -fsSL --noproxy '*' --retry 3 --retry-delay 2 --connect-timeout 15 \
        -o /tmp/wechat.deb "${WECHAT_URL}"; \
    apt-get install -y --no-install-recommends /tmp/wechat.deb; \
    rm -f /tmp/wechat.deb; \
    rm -rf /var/lib/apt/lists/*; \
    test -x /usr/bin/wechat

# ---------------------------------------------------------------------------
# shuttle: official release archive, checksum-verified, unpacked as shipped
# ---------------------------------------------------------------------------
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) shuttle_arch=x64 ;; \
        arm64) shuttle_arch=arm64 ;; \
        *) echo "unsupported architecture ${arch}" >&2; exit 1 ;; \
    esac; \
    asset="shuttle-v${SHUTTLE_VERSION}-linux-${shuttle_arch}.tar.gz"; \
    base="${SHUTTLE_RELEASE_BASE%/}/v${SHUTTLE_VERSION}"; \
    cd /tmp; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 15 \
        -C - -o "${asset}" "${base}/${asset}"; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 15 \
        -o SHA256SUMS "${base}/SHA256SUMS"; \
    grep " ${asset}\$" SHA256SUMS | sha256sum -c -; \
    mkdir -p /opt/loomhand; \
    tar -xzf "${asset}" -C /opt/loomhand; \
    chmod 0755 /opt/loomhand/shuttle/shuttle \
               /opt/loomhand/shuttle/cua-driver \
               /opt/loomhand/shuttle/node; \
    ln -sf /opt/loomhand/shuttle/shuttle /usr/local/bin/shuttle; \
    rm -f "${asset}" SHA256SUMS; \
    shuttle --version

# ---------------------------------------------------------------------------
# Desktop user (replaces the base image's `ubuntu` account at uid 1000)
# ---------------------------------------------------------------------------
RUN set -eux; \
    if getent passwd ubuntu >/dev/null; then userdel -r ubuntu 2>/dev/null || true; fi; \
    if getent group ubuntu >/dev/null; then groupdel ubuntu 2>/dev/null || true; fi; \
    getent group "${USER_GID}" >/dev/null || groupadd -g "${USER_GID}" "${USER_NAME}"; \
    getent passwd "${USER_NAME}" >/dev/null \
        || useradd -m -u "${USER_UID}" -g "${USER_GID}" -s /bin/bash "${USER_NAME}"; \
    mkdir -p "/home/${USER_NAME}/.config" "/home/${USER_NAME}/.cache" \
             "/home/${USER_NAME}/.local/share" "/home/${USER_NAME}/.vnc" \
             "/home/${USER_NAME}/Desktop"; \
    chown -R "${USER_UID}:${USER_GID}" "/home/${USER_NAME}"; \
    chmod 700 "/home/${USER_NAME}/.vnc"

COPY --chmod=0755 scripts/entrypoint.sh /opt/box/entrypoint.sh
COPY --chmod=0755 scripts/start-desktop.sh /opt/box/start-desktop.sh
COPY --chmod=0755 scripts/firefox.sh /usr/local/bin/firefox
COPY --chmod=0755 scripts/microsoft-edge.sh /usr/local/bin/microsoft-edge
COPY --chmod=0644 assets/firefox.desktop /opt/box/firefox.desktop
COPY --chmod=0644 assets/microsoft-edge.desktop /opt/box/microsoft-edge.desktop
COPY --chmod=0644 assets/policies.json /etc/firefox/policies/policies.json
COPY --chmod=0644 assets/novnc-index.html /opt/box/novnc-index.html

RUN set -eux; \
    install -m 0644 /opt/box/firefox.desktop /usr/share/applications/firefox.desktop; \
    install -m 0644 /opt/box/microsoft-edge.desktop /usr/share/applications/microsoft-edge.desktop; \
    install -m 0644 /opt/box/novnc-index.html /usr/share/novnc/index.html; \
    install -m 0755 /opt/box/firefox.desktop "/home/${USER_NAME}/Desktop/firefox.desktop"; \
    chown -R "${USER_UID}:${USER_GID}" "/home/${USER_NAME}/Desktop"

ENV HOME=/home/${USER_NAME}
WORKDIR /home/${USER_NAME}
USER ${USER_NAME}

# noVNC
EXPOSE 16080 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD test -f "$HOME/.cache/box/ready" \
        && curl -fsS -o /dev/null "http://127.0.0.1:${NOVNC_PORT}/vnc.html"

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/box/entrypoint.sh"]
