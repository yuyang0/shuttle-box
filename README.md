# shuttle-box

An Ubuntu 24.04 + XFCE4 desktop inside a container: you watch it in a browser, and agents
outside the container drive it over MCP.

[中文说明](README.zh-CN.md)

- Desktop: XFCE4 on TigerVNC's `Xvnc` (`:1`, 1280×800 by default), no Wayland
- Watch it: noVNC + websockify on `http://<host>:16080/` (auto-connect, scaled to the window)
- Browser: Mozilla Firefox from Mozilla's own deb repository (Ubuntu's package is a snap stub), with an AT-SPI tree
- Driver: [shuttle](https://github.com/loomhand/shuttle) v0.1.40 in `/opt/loomhand/shuttle`, `shuttle` on PATH
- Agent access: MCP over HTTP at `http://127.0.0.1:8765/mcp` (`shuttle mcp --http` inside the container, bearer token required)

## Quickstart

The host only needs Docker with compose v2. The container brings its own X server and VNC, so
no graphical environment is needed on the host.

### One command

```bash
curl -fsSLO https://raw.githubusercontent.com/yuyang0/shuttle-box/main/quickstart.sh
bash quickstart.sh
```

The script writes its own compose file and `.env` (MCP token generated, mode 600), pulls
`ghcr.io/yuyang0/shuttle-box:latest`, starts the container, waits for the desktop and then
prints the noVNC URL, the MCP URL, the `Authorization` header and a Claude Code registration
command. No clone, no make.

Environment overrides: `TAG` (default `latest`), `IMAGE`, `HOME_DIR` (default
`~/.local/share/shuttle-box`), `CONTAINER`, `NOVNC_PORT`, `MCP_PORT`, `SCREEN_GEOMETRY`,
`OPENROUTER_API_KEY`. To run a second instance next to an existing one, change `DISPLAY` and
`VNC_PORT` together (for example `DISPLAY=:2 VNC_PORT=5902 NOVNC_PORT=16090 MCP_PORT=18765`).

### Step by step

If you prefer to control each step:

```bash
git clone https://github.com/yuyang0/shuttle-box.git
cd shuttle-box

# 1. Required: the bearer token for the MCP endpoint. Optional: a model key, which only
#    affects gui_plan and script_run
echo "SHUTTLE_MCP_TOKEN=$(openssl rand -hex 24)" > .env
echo "OPENROUTER_API_KEY=sk-or-..." >> .env

# 2. Use an image. By default this pulls the published one (public, no login);
#    to build locally instead, uncomment the three lines below and comment out pull/up
# make build                     # in China: USTC apt mirror + local 7890 proxy by default
# make build PROXY= APT_MIRROR=  # without such a proxy, use the upstream sources
# make up
make pull TAG=latest
make up   TAG=latest

# 3. Watch it (the port binds loopback; from another machine use
#    ssh -L 16080:127.0.0.1:16080 <host>)
xdg-open http://127.0.0.1:16080/

# 4. Connect an agent
make mcp-url     # prints the MCP URL and the Authorization header
make mcp-test    # handshake smoke test
```

Pin a version instead of the floating `latest`:
`make pull TAG=v0.1.1 && make up TAG=v0.1.1`.

Note when building locally: `make build` points `:latest` at the image it just built, which
replaces the tag pulled from the registry. To keep the registry's `latest` untouched, use
`make build TAG=dev` and then `make up TAG=dev`.

## Verified

| Check | Result |
|---|---|
| `http://127.0.0.1:16080/` (redirect to noVNC) and `/vnc.html` | 200 |
| `shuttle desk windows` | lists xfwm4 / xfce4-panel / xfdesktop / firefox-bin |
| Firefox accessibility tree | readable: `desk find` / `desk press` hit and click page buttons |
| `desk key` + `desk type` real input | typed `https://example.com` and pressed Return, the page loaded |
| `desk shot` | PNG at the current screen size |
| `POST http://127.0.0.1:8765/mcp` | 401 without a token; with the bearer token initialize (shuttle 0.1.40 / protocol 2025-06-18), tools/list (8 tools) and `gui_plan` all work |
| Reconnect after recreating the container | nothing to do by hand; the next MCP call succeeds |
| Container health check | `healthy` (readiness marker + noVNC 200) |

## Make targets

```bash
make help            # every target and overridable variable
```

| Target | What it does |
|---|---|
| `make build` | build the image, with an apt mirror and the host proxy by default (`PROXY=` disables both) |
| `make up` / `make down` / `make recreate` | compose start and stop; `up` waits for the desktop |
| `make status` | health, screen size, window list |
| `make logs` | follow `supervisor.log` inside the container |
| `make shell` | shell as user `box` |
| `make browser` | open Firefox at `URL` (default `https://www.baidu.com`) |
| `make shot` | screenshot to `./shot.png` |
| `make screen` / `make windows` | current screen size / window list |
| `make tools` / `make mcp-test` | list MCP tools / run an initialize + tools/list round trip |
| `make resize GEOMETRY=1600x1200` | change the resolution at runtime |
| `make backup` | back up the Firefox profile (cookies, logins) to `./backups` |
| `make restore` | restore the newest backup (`FILE=` picks one) |
| `make clean` | remove the container, **keep the volume** (the login survives) |
| `make clean-volume FORCE=1` | remove the volume too, after an automatic backup |

Overridable variables: `IMAGE` `TAG` `CONTAINER` `APT_MIRROR` `PROXY` `SHUTTLE_VERSION` `URL` `GEOMETRY`.

The image name defaults to `ghcr.io/yuyang0/shuttle-box`, the same reference CI pushes, and
`TAG` defaults to `latest`, so `make pull`, `make up` and `make recreate` all use the newest
release from the registry. `make build` tags its result as `:latest` plus `:<git describe>` so
a local build stays traceable. `SHUTTLE_VERSION` is read from the Dockerfile `ARG`, so the two
cannot drift apart. For example:

```bash
make build TAG=dev PROXY=                 # no proxy, different tag
make browser URL=https://github.com
make status CONTAINER=my-box
```

`make browser` and `make shot` go through [bin/mcp-drive.py](bin/mcp-drive.py), a small MCP
client you can also use directly:

```bash
bin/mcp-drive.py tools
bin/mcp-drive.py open-url https://example.com
bin/mcp-drive.py shot out.png
```

## CI and the registry

`.github/workflows/build.yml` builds on GitHub's runners and pushes to GitHub Container
Registry:

- Triggers: **tag pushes only** (any tag name) plus manual `workflow_dispatch`. A commit to `main` builds nothing, so the registry only holds versions someone tagged on purpose.
- Image tags: the git tag verbatim (`v1.2.3` → `v1.2.3`); semver tags also get `1.2.3` and `1.2`, and every build gets `sha-<short>`; `latest` is maintained by metadata-action.
- Cache: `type=gha`, reusing apt / pip / image layers across builds.
- Every build argument is left empty: on a runner, archive.ubuntu.com, the Mozilla repository and the GitHub release are all reachable directly, so neither `APT_MIRROR` nor a proxy is needed.
- After pushing, the image is pulled back and smoke-tested (`shuttle --version`).

The image is public, so pulling needs no login:

```bash
docker pull ghcr.io/yuyang0/shuttle-box:latest
```

`make up` and `make recreate` pass `IMAGE` and `TAG` through to compose.

## Building

`make build` is the easy path (it is the command below plus the mirror and proxy flags).
Building by hand:

```bash
docker build -t ghcr.io/yuyang0/shuttle-box:dev .
```

On a Chinese network, apt and GitHub are separate bottlenecks; stack the flags you need:

```bash
docker build --network=host \
  --build-arg APT_MIRROR=http://mirrors.ustc.edu.cn/ubuntu \
  --build-arg HTTP_PROXY=http://127.0.0.1:7890 \
  --build-arg HTTPS_PROXY=http://127.0.0.1:7890 \
  --build-arg http_proxy=http://127.0.0.1:7890 \
  --build-arg https_proxy=http://127.0.0.1:7890 \
  --build-arg NO_PROXY=localhost,127.0.0.1,mirrors.ustc.edu.cn \
  --build-arg no_proxy=localhost,127.0.0.1,mirrors.ustc.edu.cn \
  -t ghcr.io/yuyang0/shuttle-box:dev .
```

Why the proxy needs `--network=host`: a local proxy usually listens on `127.0.0.1` only, and
a build container on the bridge network cannot reach it (`172.17.0.1:7890` and
`host.docker.internal:7890` both fail). Meanwhile the build container's direct connection to
GitHub is throttled to 7–22 KB/s, so the shuttle archive stalls for a long time; through the
proxy with `--network=host` it runs at about 1.7 MB/s.

`HTTP_PROXY` and friends are Docker predefined build arguments, so the Dockerfile does not
declare them. `NO_PROXY` carries the apt mirror host so apt goes direct instead of through the
proxy.

Without a proxy you can point the download at a release mirror:

```bash
docker build --build-arg SHUTTLE_RELEASE_BASE=https://ghfast.top/https://github.com/loomhand/shuttle/releases/download \
  -t ghcr.io/yuyang0/shuttle-box:dev .
```

Build arguments:

| ARG | Default | Meaning |
|---|---|---|
| `APT_MIRROR` | upstream sources | swap the apt mirror, e.g. `http://mirrors.ustc.edu.cn/ubuntu` (https is downgraded to http because the base image has no CA package; apt verifies packages with GPG, not TLS) |
| `SHUTTLE_VERSION` | `0.1.40` | shuttle release version |
| `SHUTTLE_RELEASE_BASE` | GitHub releases | download base, may point at a mirror or accelerator |
| `USER_NAME` / `USER_UID` / `USER_GID` | `box` / `1000` / `1000` | the desktop user |

## Running

```bash
docker run -d --name shuttle-box \
  --network=host --shm-size=2g \
  -e NOVNC_PORT=16080 \
  -e BROWSER_PROXY=http://127.0.0.1:7890 \
  -e OPENROUTER_API_KEY=sk-or-... \
  ghcr.io/yuyang0/shuttle-box:latest
```

Or `docker compose up -d`.

Give `/dev/shm` room: with the default 64 MB the browser tends to crash, hence `--shm-size=2g`.

Open <http://localhost:16080/> for the desktop (the root path redirects to noVNC with
`autoconnect=1&resize=scale`). The viewer binds `127.0.0.1` by default and so does VNC on 5901;
to watch from another machine either set `NOVNC_BIND=0.0.0.0` (or leave it empty for all
interfaces) and restart, or tunnel.

The container runs as the non-root user `box` (uid 1000). Mount a volume at `/home/box` to
persist browser and shuttle configuration.

## Timezone and language

Defaults match mainland China: `TZ=Asia/Shanghai`, `LANG`/`LC_ALL=zh_CN.UTF-8`, and Firefox
gets `intl.accept_languages=zh-CN,zh,en`.

Measured inside the container by opening a page that echoes the values and reading it back over
AT-SPI:

| Item | Value |
|---|---|
| `Accept-Language` | `zh-CN,zh;q=0.9,en;q=0.8` |
| `Intl.DateTimeFormat().resolvedOptions().timeZone` | `Asia/Shanghai` |
| `resolvedOptions().locale` / `navigator.language` | `zh-CN` |
| `navigator.languages` | `zh-CN,zh,en` |
| `new Date().toString()` | `Wed Nov 04 2026 22:13:09 GMT+0800 (China Standard Time)` |
| Container `date` | `Wed Nov  4 22:11:45 CST 2026` |

To change them:

```bash
-e CONTAINER_TZ=Asia/Tokyo -e CONTAINER_LANG=ja_JP.UTF-8 \
-e BROWSER_ACCEPT_LANG=ja-JP,ja,en
```

compose uses `CONTAINER_TZ` / `CONTAINER_LANG` rather than `TZ` / `LANG` on purpose:
`${VAR:-default}` reads the environment of the shell running `docker compose`, so a host
`LANG=C.UTF-8` would override the container default (learned the hard way).

Another language needs its locale in the image (`/etc/locale.gen` + `locale-gen`); only
`zh_CN.UTF-8` is generated today.

## Protecting the login state

Firefox's cookies and saved passwords live in the volume at
`/home/box/.mozilla/firefox/agent.default`. Everything below is about not losing it:

| Operation | Touches the login? |
|---|---|
| `make rebuild` / `docker compose up -d --force-recreate` | no, the volume stays |
| Rebuilding the image (`make build`) | no |
| `make clean` | **no** (removes the container, keeps the volume) |
| `make clean-volume` | yes, hence it needs `FORCE=1`, and it backs up first |
| `docker volume rm box_box-home` | yes, nothing can stop it |

Other guarantees:

- The Firefox launcher creates the profile only when the directory is absent and never overwrites an existing one; `entrypoint.sh` deletes just `lock` and `.parentlock`, never `cookies.sqlite`.
- `make backup` copies each `*.sqlite` through sqlite3's backup API, so a running Firefox still yields a consistent copy. The archive holds `cookies.sqlite`, `key4.db`, `logins.db` and `prefs.js`, skipping caches and session-restore data: about 4 MB.
- `make restore` stops Firefox, renames the current profile to `agent.default.bak-<timestamp>` and then unpacks, so a failed restore can be rolled back.
- `bin/firefox-profile.py list` shows earlier backups.

## Sending the browser through a host proxy

Firefox **does not read** `http_proxy` / `HTTPS_PROXY`. The `firefox` launcher writes
`BROWSER_PROXY` into the profile's `user.js` (`network.proxy.*`) on every start, so changing
the proxy is an environment change plus a container restart:

```bash
docker run -d --name shuttle-box --network=host --shm-size=2g \
  -e NOVNC_PORT=16080 \
  -e BROWSER_PROXY=http://127.0.0.1:7890 \
  ghcr.io/yuyang0/shuttle-box:latest
```

The real obstacle is reachability, not browser configuration:

| Where the proxy listens | How the container reaches it | Host networking? |
|---|---|---|
| `127.0.0.1` only (Clash default) | the container's `127.0.0.1` is its own, so share the host network with `--network=host` | yes |
| Allow LAN / listening on `0.0.0.0` | use `http://172.17.0.1:7890`, or `--add-host=host.docker.internal:host-gateway` and use `host.docker.internal` | no |
| `127.0.0.1` only, but no host networking | forward on the host: `docker run -d --name fwd --network=host alpine/socat TCP-LISTEN:7891,fork,reuseaddr TCP:127.0.0.1:7890`, then use `http://172.17.0.1:7891` in the container | no |

All three were measured: with host networking `curl -x http://127.0.0.1:7890` returns 200 and
Firefox shows the proxy's exit IP on `ifconfig.me`; from a bridge container
`172.17.0.1:7890` returns 000; behind the socat forwarder the same command returns 200.

Know the side effects of `--network=host`:

- `-p` no longer applies; noVNC lands directly on the host's 16080. On a conflict change `NOVNC_PORT`.
- The viewer binds `127.0.0.1` by default. For another machine either set `NOVNC_BIND=0.0.0.0` (or empty for all interfaces) or use an SSH tunnel: `ssh -L 16080:127.0.0.1:16080 host`.
- Container and host share the network stack, so `127.0.0.1` inside the container is the host.

`BROWSER_PROXY` affects the browser only, not shuttle's own model calls. For those, set
`SHUTTLE_BASE_URL` to an endpoint the container can reach; whether shuttle's HTTP client honours
`HTTPS_PROXY` has not been verified, so do not rely on it.

## Screen size

| Layer | How | Measured |
|---|---|---|
| Startup size | `-e SCREEN_GEOMETRY=1920x1080` (with `SCREEN_DEPTH`) | ✅ Xvnc's initial screen, 1280×800 by default |
| Scale to the window (recommended) | `http://<host>:16080/`, the root path already sets `resize=scale` | ✅ pure client-side scaling, the remote desktop keeps its size |
| Change the real size at runtime | `docker exec shuttle-box xrandr --output VNC-0 --mode 1600x1200` | ✅ modes are whatever `xrandr` lists; 1280×800 measured |
| Let the remote follow the browser window | `?resize=remote` (RFB `SetDesktopSize`) | ❌ this Xvnc drops the connection on that message; unusable |
| Force an arbitrary size | `xrandr --fb 1280x800` | ⚠️ the size does change but the output is left disconnected; avoid |

So the workable answer to "fit my browser window" is `resize=scale`: the picture scales to the
window with no scrollbars. When you need a genuinely different desktop size, use
`SCREEN_GEOMETRY` or `xrandr --output VNC-0 --mode` from the fixed mode list.

### Full screen

The desktop is 1280×800 by default; with `resize=scale` press **F11** in the browser, or the ⛶
button in the noVNC toolbar, for a fullscreen view without scrollbars. Fullscreen needs a user
gesture, so no URL parameter can do it for you.

Mismatched aspect ratios leave letterbox bars. For a 16:9 screen set `SCREEN_GEOMETRY` to
`1360x768` or `1280x720`; for more vertical room stay at 1280×800 or use `1400x1050`.

noVNC's Scaling Mode maps to: Local Scaling = `resize=scale` (default, leaves the remote
alone), Remote Resizing = `resize=remote` (unavailable in this image), None = `resize=off`
(1:1 with scrollbars).

No restart is needed after a resize: shuttle's `desk screen`, `desk shot` and `screenshot` all
report the current size, so an agent sees the new resolution immediately.

`resize=remote` was tried the optimistic way: Xvnc was started with `-AcceptSetDesktopSize 1`
and a raw RFB client sent a well-formed 22-byte SetDesktopSize (screen id 0 and 1 both tried);
the server silently closed the connection every time and the framebuffer stayed the same. It
needs an X server that implements SetDesktopSize.

## Connecting an agent (MCP over HTTP)

Inside the container, `start-desktop.sh` runs `shuttle mcp --http 127.0.0.1:8765`; container and
host share a network, so clients connect to `http://127.0.0.1:8765/mcp`. The endpoint requires a
bearer token: compose injects `SHUTTLE_MCP_TOKEN` from `.env` into the container, and the client
sends the same value.

```bash
$ make mcp-url          # print the URL and the Authorization header
$ make mcp-test         # handshake smoke test
```

Why HTTP is the default rather than `docker exec -i ... shuttle mcp`: the stdio bridge's child
process dies with the container, so after a recreate (`make recreate`, an image upgrade) the
client holds a dead pipe, seen as `stdio process closed`, until the server is disabled and
enabled by hand. HTTP has no such problem — after a recreate the next call simply succeeds,
because the client only has to retry a request.

### Client configuration

DSH: in the MCP manager, `type = http`, `url = http://127.0.0.1:8765/mcp`, header
`Authorization: Bearer <token>`. It can also be changed with
`PUT /mcp-manager/api/servers/<id>`, which takes effect immediately without restarting DSH.

Claude Code:

```bash
claude mcp add --transport http shuttle http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $SHUTTLE_MCP_TOKEN"
```

Cursor (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "shuttle": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Any other Streamable HTTP client works the same way: the URL plus one `Authorization` header.

### The container has to stay up

HTTP leaves no room for starting the container on demand: if it is not running, the connection
fails. compose sets `restart: unless-stopped`, so as long as the docker daemon runs, the
container does.

### Using stdio instead

`bin/shuttle-mcp.sh` is still there for two cases: a client that can only spawn a command (no
HTTP support), or one that wants the container created when it is missing. It handles three
situations on demand — create from the repository's `docker-compose.yml` when the container does
not exist, `docker start` when it exists but is stopped, then wait for the readiness marker
before `docker exec -i ... shuttle mcp`. Measured: about 0.7 s when the container already runs,
1.7 s from stopped to a completed call, 2.2 s from nothing to a result.

```bash
claude mcp add shuttle -- /path/to/box/bin/shuttle-mcp.sh
```

The readiness marker is `/home/box/.cache/box/ready`: removed at container start and written
only once every process is up. Do not test for `desk.sock` instead — that socket file survives
restarts in the writable layer and fools `test -S`, after which `shuttle mcp` hits a zombie
socket and starts a daemon without the D-Bus session, failing with
`connect the session bus: ... No such file or directory`.

The wrapper reads `SHUTTLE_CONTAINER`, `SHUTTLE_COMPOSE_FILE`, `SHUTTLE_READY_PATH` and
`SHUTTLE_START_TIMEOUT` (default 45 s).

### Clients that are not on the host

With the default `MCP_HTTP_BIND=127.0.0.1` the endpoint is host-local; forward it over SSH
rather than exposing the port:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@host
```

To really open it to the LAN, set `MCP_HTTP_BIND=0.0.0.0` and rotate the token —
`SHUTTLE_MCP_TOKEN` is the only credential, and the traffic is plain HTTP.

## Model configuration

Of shuttle's tools, only the steps that have to see or judge call a model (`gui_plan`'s picks and
checks, and `desk.decide/look/ask/ground` inside `script_run`); the deterministic tools (`desk`,
`screenshot`) need no key at all.

On first start the environment is rendered into
`/home/box/.config/loomhand/shuttle/config.toml` (mode 0600). That file is authoritative
afterwards: restart the container after editing it (the daemon reads it at startup), or set
`SHUTTLE_CONFIG_REGENERATE=1` to have it rewritten.

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | — | key for the default provider |
| `SHUTTLE_API_KEY` | — | key written into the config instead (same effect, stored in the file) |
| `SHUTTLE_API_KEY_ENV` | `OPENROUTER_API_KEY` | which environment variable shuttle reads the key from |
| `SHUTTLE_BASE_URL` | `https://openrouter.ai/api/v1` | any OpenAI-compatible endpoint |
| `SHUTTLE_DECISIONS_URL` | derived from base_url | the decisions endpoint used by `decide`; off OpenRouter it defaults to `<base>/decisions` |
| `SHUTTLE_DECIDE_API` | `decisions` on OpenRouter, else `chat` | set `chat` when there is no decisions endpoint (the model then reports its own uncalibrated probabilities, so expect more escalations and mis-picks) |
| `SHUTTLE_DECIDE_MODEL` / `SHUTTLE_GROUND_MODEL` / `SHUTTLE_ASK_MODEL` | upstream defaults | three of the four roles; `verify` follows `decide` |
| `SHUTTLE_TIMEOUT_MS` | `60000` | per-call model timeout |

For a local or China-reachable OpenAI-compatible service (vLLM / Ollama / your own gateway):

```bash
docker run -d --name shuttle-box --network=host --shm-size=2g \
  -e SHUTTLE_BASE_URL=http://192.168.1.10:8000/v1 \
  -e SHUTTLE_DECIDE_MODEL=qwen2.5-vl-7b-instruct \
  -e SHUTTLE_GROUND_MODEL=qwen2.5-vl-7b-instruct \
  -e SHUTTLE_ASK_MODEL=qwen2.5-vl-7b-instruct \
  ghcr.io/yuyang0/shuttle-box:latest
```

Measured: the container reaches `openrouter.ai` directly (HTTP 200, about 130 KB/s).

## Security

shuttle really drives programs on the desktop, so the boundary here is the container itself.

| Variable | Default | Meaning |
|---|---|---|
| `SHUTTLE_SCRIPT_RUN` | `true` | disable to remove `script_run` (it executes arbitrary JS/TS with the desktop user's rights) |
| `SHUTTLE_SAFE_APPS` | empty (no limit) | app allowlist, comma separated, e.g. `SHUTTLE_SAFE_APPS=firefox-bin` |
| `SHUTTLE_DESK_INPUT` | `foreground` | `background` / `auto` degrade to foreground on Linux |
| `VNC_PASSWORD` | empty | set to require a VNC password (the noVNC page asks for it too) |
| `SHUTTLE_MCP_TOKEN` | empty | the only credential for the MCP HTTP endpoint; empty means the endpoint is not started and a `skipped` line is logged |

Other notes:

- `shuttle stop` (or `docker exec shuttle-box shuttle stop`) halts work at once; `shuttle resume` lifts it.
- The Linux backend has **no** kill hotkey, indicator or confirm dialog (only stop/resume, the `script_run` switch and the allowlist).
- The browser needs no `--no-sandbox`: Firefox's content sandbox creates a user namespace, which Docker's default seccomp profile blocks (`Failed to move to new namespace: Operation not permitted`), so compose uses `security_opt: seccomp=unconfined` and the browser keeps its own sandbox.
- The cost is that seccomp filtering is off for the whole container, not just the browser.
- Firefox's first-run Terms of Use dialog is suppressed by prefs written at launch, so nobody has to click it.

## Paths inside the container

| Path | Purpose |
|---|---|
| `/home/box/.cache/box/ready` | start-up marker, written once every process is up; the MCP wrapper waits for it |
| `/home/box/.cache/loomhand/shuttle/desk.sock` | shuttle daemon's desk socket |
| `/home/box/.config/loomhand/shuttle/config.toml` | shuttle configuration (0600) |
| `/home/box/.cache/box/logs/` | `supervisor.log`, `xvnc.log`, `xfce.log`, `shuttle-serve.log`, `mcp-http.log`, `novnc.log` |
| `/opt/loomhand/shuttle/` | shuttle program, `cua-driver`, bundled Node, runtime |
| `/usr/share/novnc` | noVNC 1.7 (upstream tarball, not a distro package) |
| `/opt/websockify` | websockify's venv, avoiding the distro's numpy/nodejs dependency chain |
| `/usr/local/bin/firefox` | Firefox launcher (proxy, language, accessibility warning) |

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Page does not load / black screen | `docker exec shuttle-box tail -50 ~/.cache/box/logs/xvnc.log`; `docker exec shuttle-box cat ~/.cache/box/logs/supervisor.log` |
| `desk windows` returns nothing | AT-SPI is not up: `docker exec shuttle-box xprop -root AT_SPI_BUS` should print a value; check `xfce.log` |
| Firefox has no element tree | start it with the `firefox` launcher (it uses the desktop session bus); a bare `docker exec` start has no `DBUS_SESSION_BUS_ADDRESS` |
| Firefox stops at the "Welcome to Firefox / Terms of Use" dialog | check that the profile's `user.js` has `termsofuse.acceptedDate` / `acceptedVersion` (the launcher rewrites it every start) |
| Picture shows but is slow | `Xvnc` has no GLX, so the browser uses software rendering; try a smaller `SCREEN_GEOMETRY` |
| Model calls time out or fail | `curl -I $SHUTTLE_BASE_URL` inside the container; the endpoint must be reachable from the container |
| Build stalls downloading shuttle | see the proxy and `SHUTTLE_RELEASE_BASE` notes under Building |
| MCP client reports `container ... is not running` | the container is down: `docker compose up -d`, or use `bin/shuttle-mcp.sh` |
| `connect the session bus: ... No such file or directory` | a `shuttle` process was started outside the desktop session (no D-Bus); wait for the readiness marker, or use `bin/shuttle-mcp.sh` |
| Container is `healthy` but `desk windows` is empty | XFCE has not finished starting; wait for `/home/box/.cache/box/ready` |
| Picture is tiny or has scrollbars | the root path already sets `resize=scale`; press F11 or the ⛶ button for fullscreen |
| Changed `SCREEN_GEOMETRY` but the desktop size did not change | the entrypoint deletes XFCE's saved `displays.xml`; if it still does not apply, `docker compose up -d --force-recreate` |
| Firefox exits immediately or reports "already running" | a stale profile lock in the volume (`~/.mozilla/firefox/*/lock`); the container clears it at startup |
