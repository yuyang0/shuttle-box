# AGENTS.md

Project instructions for coding agents on **shuttle-box**.

## What this is

A container running an XFCE desktop (TigerVNC `Xvnc` + noVNC) with Firefox, plus
[shuttle](https://github.com/loomhand/shuttle) so an agent outside the container drives that
desktop over MCP. The image is published to ghcr.io; this repo is the build recipe, the runtime
scripts and the host-side helpers.

| Part | Files | Boundary |
|---|---|---|
| Desktop container | `Dockerfile`, `scripts/`, `assets/`, `docker-compose.yml` | own X server, Firefox via AT-SPI, shuttle over a unix socket |
| Host tooling | `Makefile`, `bin/`, `quickstart.sh` | docker and MCP over HTTP |
| Package pipeline | `.github/workflows/build.yml` | ghcr.io, tag pushes only |

The only state outliving the container is the volume `box-home` at `/home/box`: the Firefox
profile (cookies, logins) and shuttle's `config.toml`.

## Layout

```
Dockerfile              Ubuntu 24.04 + XFCE + Firefox + upstream noVNC/websockify + shuttle
docker-compose.yml      host network, seccomp=unconfined, shm 2g, volume, env pass-through
quickstart.sh           standalone: writes its compose + .env, pulls, starts, prints MCP config
Makefile                build/up/recreate/status/logs/shell/browser/shot/backup/restore/mcp-test
.env                    secrets (gitignored, 600): OPENROUTER_API_KEY, SHUTTLE_MCP_TOKEN
assets/                 firefox.desktop, policies.json, novnc-index.html (redirect, resize=scale)
scripts/entrypoint.sh   root: dirs, stale locks, VNC password, renders config.toml, hands over
scripts/start-desktop   Xvnc -> xfce -> AT-SPI wake -> shuttle serve -> mcp --http -> websockify
scripts/firefox.sh      /usr/local/bin/firefox: writes user.js (proxy, lang, Terms) then execs
bin/shuttle-mcp.sh      stdio MCP bridge over docker exec, starts the container on demand
bin/mcp-drive.py        small MCP client (tools/status/screen/windows/launch/open-url/shot)
bin/x-timeline.py       X timelines via the a11y tree: navigation, tabs, JSON/MD
bin/x-links.py          post permalinks via AT-SPI Hyperlink.GetURI (needs pyatspi)
bin/firefox-profile.py  profile backup/restore/list, sqlite backup API
README.md               English default; README.zh-CN.md mirrors it
```

## Commands

```bash
make build              # mirror + host proxy by default; PROXY= APT_MIRROR= to go direct
make pull && make up    # published image (TAG defaults to latest)
make recreate && make mcp-test && make status
make backup             # Firefox profile -> ./backups
```

## Invariants

- **The Firefox profile is irreplaceable** (`/home/box/.mozilla/firefox/agent.default`). Never
  delete or overwrite it: the launcher only creates a missing one, `entrypoint.sh` removes just
  `lock`/`.parentlock`. `make backup` first; test destructively with `FIREFOX_PROFILE_DIR=/tmp/...`.
- **A second instance changes `DISPLAY`, `VNC_PORT`, `NOVNC_PORT` and `MCP_HTTP_PORT` together** —
  host networking collides on all four.
- **Firefox comes from Mozilla's apt repository** (Ubuntu's package is a snap stub) and is driven
  through its AT-SPI tree; there is no CDP and no DevTools endpoint anywhere.
- **MCP is HTTP** (`shuttle mcp --http 127.0.0.1:8765`) and requires `SHUTTLE_MCP_TOKEN`; it
  refuses to start without one, even on loopback. A stdio `docker exec` child dies with the
  container and leaves the client holding a dead pipe.
- **Readiness is `/home/box/.cache/box/ready`**, not the presence of `desk.sock`: that file
  survives restarts and yields a daemon without the D-Bus session.
- **`config.toml` is rendered only when missing**; editing it sticks, changing `SHUTTLE_*` env
  needs `SHUTTLE_CONFIG_REGENERATE=1`.
- **`SHUTTLE_VERSION` lives in the Dockerfile `ARG` only**; the Makefile reads it from there.
- **CI builds on tags only**; a push to `main` builds nothing. Local builds need `--network=host`
  plus mirror and proxy, the runner needs none.
- **noVNC/websockify stay upstream** (pinned sha256, venv); the distro packages pull in nodejs
  and python3-numpy.
- **Secrets never enter the repo**: `.env` is gitignored (600), so is `backups/`.

## Verifying a change

```bash
make build && make recreate
make mcp-test                                   # HTTP MCP handshake
docker exec shuttle-box shuttle desk windows    # xfwm4/xfce4-panel/xfdesktop/firefox-bin
make -s browser URL=https://example.com && make status && bin/mcp-drive.py shot /tmp/x.png
```

Anything touching the desktop also needs one model-backed call, e.g.
`gui_plan {"steps":[{"wait":"..."},{"expect":"..."}]}`, which exercises the `decide` path that
plain `desk` calls never reach.

## Conventions

- Docs are written twice: `README.md` (English, the GitHub default) and `README.zh-CN.md`,
  cross-linked at the top.
- Commit messages in English. Runtime scripts run as `box`, so no root assumptions.
- Prefer a Makefile target over a hand-typed command, and add a target when a workflow repeats.
