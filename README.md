# shuttle-box

一个 Ubuntu 24.04 + XFCE4 的容器桌面：浏览器里能看到画面，容器外的 agent 能通过 MCP 驱动它。

- 桌面：XFCE4 跑在 TigerVNC 的 `Xvnc` 上（`:1`，默认 1280×800），没有 Wayland
- 网页看画面：noVNC + websockify，`http://<host>:16080/`（自动连接 + 缩放适配）
- 浏览器：Mozilla Firefox（官方 deb 源，Ubuntu 自带的是 snap 占位装不了），有 AT-SPI 树
- 驱动层：[shuttle](https://github.com/loomhand/shuttle) v0.1.38，装于 `/opt/loomhand/shuttle`，`shuttle` 在 PATH
- 接入方式：`docker exec -i <容器> shuttle mcp`（stdio，由 `bin/shuttle-mcp.sh` 包装）

## 已验证

| 项 | 结果 |
|---|---|
| `http://127.0.0.1:16080/`（跳 noVNC）与 `/vnc.html` | 200 |
| `shuttle desk windows` | 列出 xfwm4 / xfce4-panel / xfdesktop / firefox-bin |
| Firefox 无障碍树 | 可读：`desk find`/`desk press` 能命中并点击页面按钮 |
| `desk key` + `desk type` 真实输入 | 输入 `https://example.com` 并回车，页面成功加载 |
| `desk shot` | 1920×1080 PNG |
| `docker exec -i ... shuttle mcp` | initialize（shuttle 0.1.38 / 协议 2025-06-18）、tools/list（8 个工具）、tools/call 均正常 |
| 容器健康检查 | `healthy`（desk.sock 存在 + noVNC 200） |

## 常用命令（Makefile）

```bash
make help            # 列出所有目标与可覆盖变量
```

| 目标 | 做什么 |
|---|---|
| `make build` | 构建镜像，自带 apt 镜像源与本机代理（`PROXY=` 可关掉） |
| `make up` / `make down` / `make recreate` | compose 起停；`up` 会等到桌面就绪 |
| `make status` | 健康状态、桌面尺寸、窗口列表 |
| `make logs` | 跟踪容器内 `supervisor.log` |
| `make shell` | 进容器（用户 `box`） |
| `make browser` | 打开 Firefox 并访问 `URL`（默认 `https://www.baidu.com`） |
| `make shot` | 截图到 `./shot.png` |
| `make screen` / `make windows` | 当前桌面尺寸 / 窗口列表 |
| `make tools` / `make mcp-test` | 列 MCP 工具 / 跑一次 initialize+tools/list |
| `make resize GEOMETRY=1600x1200` | 运行期换分辨率 |
| `make backup` | 备份 Firefox profile（cookies/登录态）到 `./backups` |
| `make restore` | 从最近一次备份恢复（`FILE=` 指定） |
| `make clean` | 删容器，**保留卷**（登录态不丢） |
| `make clean-volume FORCE=1` | 连卷删，会先自动备份 |

可覆盖变量：`IMAGE` `TAG` `CONTAINER` `APT_MIRROR` `PROXY` `SHUTTLE_VERSION` `URL` `GEOMETRY`。例如：

```bash
make build TAG=dev PROXY=            # 无代理、不同 tag
make browser URL=https://github.com
make status CONTAINER=my-box
```

`make browser` / `make shot` 背后是 [bin/mcp-drive.py](bin/mcp-drive.py)——一个直接走 MCP 的小工具，也可以单独用：

```bash
bin/mcp-drive.py tools
bin/mcp-drive.py open-url https://example.com
bin/mcp-drive.py shot out.png
```

## 构建

最省事的是 `make build`（等价于下面带镜像源和代理的那条）。手工构建：

```bash
cd box
docker build -t shuttle-box:0.1.38 .
```

国内网络下，`apt` 与 GitHub 各自是瓶颈，按需叠加下面两个参数：

```bash
docker build --network=host \
  --build-arg APT_MIRROR=http://mirrors.ustc.edu.cn/ubuntu \
  --build-arg HTTP_PROXY=http://127.0.0.1:7890 \
  --build-arg HTTPS_PROXY=http://127.0.0.1:7890 \
  --build-arg http_proxy=http://127.0.0.1:7890 \
  --build-arg https_proxy=http://127.0.0.1:7890 \
  --build-arg NO_PROXY=localhost,127.0.0.1,mirrors.ustc.edu.cn \
  --build-arg no_proxy=localhost,127.0.0.1,mirrors.ustc.edu.cn \
  -t shuttle-box:0.1.38 .
```

为什么代理必须配 `--network=host`：本机代理通常只监听 `127.0.0.1`，网桥里的构建容器够不到（`172.17.0.1:7890`、`host.docker.internal:7890` 都连不上）；而构建容器**直连** GitHub 会被限速到 7–22 KB/s，67 MB 的 shuttle 包会长时间卡住，加 `--network=host` 后走代理约 1.7 MB/s。

`HTTP_PROXY` 等是 Docker 预定义构建参数，不需要在 Dockerfile 里声明；`NO_PROXY` 里放 apt 镜像源，让它直连而不绕代理。

不想挂代理时，也可以换一个 release 镜像源：

```bash
docker build --build-arg SHUTTLE_RELEASE_BASE=https://ghfast.top/https://github.com/loomhand/shuttle/releases/download \
  -t shuttle-box:0.1.38 .
```

构建参数：

| ARG | 默认 | 说明 |
|---|---|---|
| `APT_MIRROR` | 官方源 | 换 apt 源，例如 `http://mirrors.ustc.edu.cn/ubuntu`（https 会自动降级为 http，因为基础镜像没有 CA 包；apt 靠 GPG 校验包，不依赖 TLS） |
| `SHUTTLE_VERSION` | `0.1.38` | shuttle release 版本 |
| `SHUTTLE_RELEASE_BASE` | GitHub releases | 下载基址，可指镜像/加速站 |
| `USER_NAME` / `USER_UID` / `USER_GID` | `box` / `1000` / `1000` | 桌面用户 |

## 运行

```bash
docker run -d --name shuttle-box \
  --network=host --shm-size=2g \
  -e NOVNC_PORT=16080 \
  -e BROWSER_PROXY=http://127.0.0.1:7890 \
  -e OPENROUTER_API_KEY=sk-or-... \
  shuttle-box:0.1.38
```

或者 `docker compose up -d`。

`--shm-size=2g` 建议给足：默认 64 MB 的 `/dev/shm` 下浏览器容易崩。

打开 <http://localhost:16080/> 就能看到桌面（根路径会带 `autoconnect=1&resize=scale` 跳到 noVNC）。网页端默认只绑 `127.0.0.1`，VNC 的 5901 也只绑回环——要在别的机器上看，给 `NOVNC_BIND=0.0.0.0`（或显式留空）后重启。

容器以非 root 用户 `box`（uid 1000）运行。要把浏览器配置、shuttle 配置持久化，挂一个卷到 `/home/box`。

## 时区与语言

默认对齐中国大陆：`TZ=Asia/Shanghai`、`LANG`/`LC_ALL=zh_CN.UTF-8`，Firefox 用 `intl.accept_languages=zh-CN,zh,en`。

实测（在容器里打开一个回声页面，用 AT-SPI 读回）：

| 项 | 值 |
|---|---|
| `Accept-Language` | `zh-CN,zh;q=0.9,en;q=0.8` |
| `Intl.DateTimeFormat().resolvedOptions().timeZone` | `Asia/Shanghai` |
| `resolvedOptions().locale` / `navigator.language` | `zh-CN` |
| `navigator.languages` | `zh-CN,zh,en` |
| `new Date().toString()` | `Wed Nov 04 2026 22:13:09 GMT+0800 (中国标准时间)` |
| 容器 `date` | `2026年 11月 04日 星期三 22:11:45 CST` |

可覆盖：

```bash
-e CONTAINER_TZ=Asia/Tokyo -e CONTAINER_LANG=ja_JP.UTF-8 \
-e BROWSER_ACCEPT_LANG=ja-JP,ja,en
```

compose 里刻意用 `CONTAINER_TZ` / `CONTAINER_LANG` 而不是 `TZ` / `LANG`：`${VAR:-default}` 会取运行 `docker compose` 那个 shell 的环境变量，宿主机上的 `LANG=C.UTF-8` 会把容器默认值顶掉（踩过一次）。

新增语言时镜像里得先有对应 locale（`/etc/locale.gen` + `locale-gen`）；目前只生成了 `zh_CN.UTF-8`。

## 登录态保护

Firefox 的 cookie 和保存的密码都在卷里的 `/home/box/.mozilla/firefox/agent.default`。这套东西围绕"别把它弄丢"：

| 操作 | 会不会动登录态 |
|---|---|
| `make rebuild` / `docker compose up -d --force-recreate` | 不会，卷保留 |
| 重建镜像（`make build`） | 不会 |
| `make clean` | **不会**（只删容器，卷保留） |
| `make clean-volume` | 会，所以要先 `FORCE=1`，且会先自动备份 |
| `docker volume rm box_box-home` | 会，无法拦截 |

其它保障：

- 包装脚本只在 profile **目录不存在**时才创建它，从不覆盖已有 profile；`entrypoint.sh` 只删 `lock` / `.parentlock` 两个锁文件，不碰 `cookies.sqlite`。
- `make backup` 用 sqlite3 的 backup API 复制每个 `*.sqlite`，所以 Firefox 正在运行时也能拿到一致副本；归档里含 `cookies.sqlite`、`key4.db`、`logins.db`、`prefs.js`，跳过缓存与会话恢复数据，约 4 MB。
- `make restore` 先停 Firefox，把现有 profile 挪成 `agent.default.bak-<时间>` 再解包，恢复失败也能退回。
- `bin/firefox-profile.py list` 看历史备份。

## 让浏览器走宿主机代理

Firefox **不读** `http_proxy` / `HTTPS_PROXY`：镜像里的 `firefox` 包装脚本在每次启动时把 `BROWSER_PROXY` 写成 profile 的 `user.js`（`network.proxy.*`），所以改代理只要改环境变量、重启容器即可：

```bash
docker run -d --name shuttle-box --network=host --shm-size=2g \
  -e NOVNC_PORT=16080 \
  -e BROWSER_PROXY=http://127.0.0.1:7890 \
  shuttle-box:0.1.38
```

真正的障碍是**地址可达性**，不是浏览器配置：

| 你的代理监听 | 容器里怎么够到 | 要不要 host 网络 |
|---|---|---|
| 只有 `127.0.0.1`（Clash 默认） | 容器内的 `127.0.0.1` 是它自己，只能 `--network=host` 共享宿主机网络 | 要 |
| 开了 Allow LAN / 监听 `0.0.0.0` | 用 `http://172.17.0.1:7890`，或 `--add-host=host.docker.internal:host-gateway` 后用 `host.docker.internal` | 不要 |
| 只有 `127.0.0.1`，但不想用 host 网络 | 宿主机做一次转发：`docker run -d --name fwd --network=host alpine/socat TCP-LISTEN:7891,fork,reuseaddr TCP:127.0.0.1:7890`，容器里用 `http://172.17.0.1:7891` | 不要 |

三种都实测过：host 网络下 `curl -x http://127.0.0.1:7890` 返回 200，Firefox 打开 `ifconfig.me` 显示代理出口 IP 且 new tab 天气组件显示洛杉矶；桥接网络直连 `172.17.0.1:7890` 返回 000；加上 socat 转发器后同一条命令返回 200。

`--network=host` 的副作用要清楚：

- `-p` 不再生效，noVNC 直接落在宿主机的 16080 上；端口冲突时用 `NOVNC_PORT` 换。
- 网页端默认只绑 `127.0.0.1`；要从别的机器访问就设 `NOVNC_BIND=0.0.0.0`（或留空 = 所有网卡），或者走 SSH 隧道：`ssh -L 16080:127.0.0.1:16080 host`。
- 容器与宿主机共享网络栈，容器看到的 `127.0.0.1` 就是宿主机。

`BROWSER_PROXY` 只影响浏览器，不影响 shuttle 自己调模型。模型端点请用能从容器直达的 `SHUTTLE_BASE_URL`；shuttle 的 HTTP 客户端是否读 `HTTPS_PROXY` 没有验证过，别依赖。

## 分辨率与自适应

| 层级 | 做法 | 实测 |
|---|---|---|
| 启动尺寸 | `-e SCREEN_GEOMETRY=1920x1080`（配 `SCREEN_DEPTH`） | ✅ Xvnc 初始屏，默认 1280×800 |
| 画面缩放适配窗口（推荐） | `http://<host>:16080/`，根路径已默认 `resize=scale` | ✅ 纯客户端等比缩放，远端桌面不动 |
| 运行期改真实尺寸 | `docker exec shuttle-box xrandr --output VNC-0 --mode 1600x1200` | ✅ 档位见 `xrandr` 输出；1280×800 实测过 |
| 让远端跟随浏览器窗口 | `?resize=remote`（RFB `SetDesktopSize`） | ❌ 当前 Xvnc 收到该报文直接断开连接，不可依赖 |
| 任意尺寸硬改 | `xrandr --fb 1280x800` | ⚠️ 尺寸确实变了，但输出被置成 disconnected，别用 |

所以「自适应浏览器窗口」的可行解是 `resize=scale`：画面永远按窗口等比缩放，没有滚动条；需要真正不同的桌面尺寸时，用 `SCREEN_GEOMETRY` 或 `xrandr --output VNC-0 --mode`（固定档位列表）。

### 全屏观看

默认桌面 1280×800，配 `resize=scale` 后在浏览器里按 **F11** 或点 noVNC 工具栏的 ⛶ 就是全屏、无滚动条。全屏需要用户手势，所以没有 URL 参数可以代劳。

比例对不上时会有细边（letterbox）：要正好铺满 16:9 屏，就把 `SCREEN_GEOMETRY` 设成 `1360x768` 或 `1280x720`；想要更多竖向空间就留在 1280×800 或 `1400x1050`。

noVNC 的 Scaling Mode 三项对应关系：Local Scaling = `resize=scale`（默认，不改远端）、Remote Resizing = `resize=remote`（本镜像不可用）、None = `resize=off`（1:1 + 滚动条）。

改完不需要重启：shuttle 的 `desk screen` / `desk shot` / `screenshot` 都按当前尺寸返回，agent 看到的就是新分辨率。

`resize=remote` 那条路已按最乐观的方式试过：显式给 Xvnc 加了 `-AcceptSetDesktopSize 1`，并用裸 RFB 客户端发过结构合法的 22 字节 SetDesktopSize（screen id 用 0 和 1 都试了），服务端都是静默断开、帧缓冲不变。要用这个方法得换支持 SetDesktopSize 的 X server。

## 让 agent 接入（默认：stdio over docker exec）

`shuttle mcp` 本身就是 stdio JSON-RPC，`docker exec -i` 把它桥到宿主机，不需要额外代理、也不开端口。每个 agent 会话一个 `mcp` 进程，它们都连到容器内同一个 `shuttle serve`，由它统一持有桌面。

### 容器不需要手工启动

`docker exec` 只能作用于**正在运行**的容器，容器没跑时客户端会拿到 `container ... is not running`。三种做法，按省心程度排序：

1. **交给 MCP 客户端**：命令指向 `bin/shuttle-mcp.sh`（下文推荐用法）。它按需处理三件事——容器不存在就用仓库里的 `docker-compose.yml` 建、存在但停止就 `docker start`、然后等就绪标记出现再 exec `shuttle mcp`。实测：容器已在跑时约 0.7s 转手；停止状态 1.7s 拉起并完成一次调用；完全不存在时 2.2s 从零到返回结果。两个客户端同时首次调用会用 `flock` 串行化，不会撞车。
2. **交给重启策略**：`docker compose up -d`（compose 里已设 `restart: unless-stopped`），之后只要 docker 守护进程在，容器就一直在，连 `docker start` 都不会发生。
3. **自己保证**：直接用 `docker exec -i shuttle-box shuttle mcp`，容器没起就失败。

注册 MCP 本身不需要容器在跑：`claude mcp add` 只是写配置，连接发生在第一次真正调用工具时。

就绪标记是 `/home/box/.cache/box/ready`：容器每次启动先删掉它，所有进程起来后才写入。不要用 `desk.sock` 是否存在判断就绪——那个 socket 文件在容器可写层里跨重启残留，会骗过 `test -S`，于是 `shuttle mcp` 撞上僵尸 socket 后自己拉起一个拿不到 D-Bus 会话的 daemon，报 `connect the session bus: ... No such file or directory`。

包装脚本认这些环境变量：`SHUTTLE_CONTAINER`（默认 `shuttle-box`）、`SHUTTLE_COMPOSE_FILE`（默认仓库根的 `docker-compose.yml`）、`SHUTTLE_READY_PATH`、`SHUTTLE_START_TIMEOUT`（默认 45s）。

### 客户端配置

Claude Code（推荐上面的包装脚本，容器停了会自动拉起）：

```bash
claude mcp add shuttle -- /path/to/box/bin/shuttle-mcp.sh
```

不想要自动拉起，就直接走 docker：

```bash
claude mcp add shuttle -- docker exec -i shuttle-box shuttle mcp
```

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.shuttle]
command = "/path/to/box/bin/shuttle-mcp.sh"
```

Cursor（`~/.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "shuttle": {
      "command": "/path/to/box/bin/shuttle-mcp.sh"
    }
  }
}
```

客户端不在 docker 宿主机上时，让 docker CLI 远程连过去即可，例如在这些配置里给 `docker` 带上 `-H ssh://user@host`，或直接设 `DOCKER_HOST=ssh://user@host`。

## 需要 HTTP 接入时

镜像只提供 stdio（`docker exec -i <容器> shuttle mcp`）。如果客户端既不能执行 `docker`，也不能走 SSH，可以在**宿主机**上临时套一层 HTTP，不用改镜像：

```bash
npx -y supergateway --stdio "docker exec -i shuttle-box shuttle mcp" \
  --outputTransport streamableHttp --port 8080 --host 127.0.0.1
```

客户端填 `http://<host>:8080/mcp`。加 `--apiKey <key>` 让请求必须带 `Authorization: Bearer <key>`。

远程机器更简单的是走 SSH，不额外开端口：

```bash
# MCP 客户端里就是这条命令
ssh <host> docker exec -i shuttle-box shuttle mcp
```

## 模型配置

shuttle 的所有工具里，只有需要"看/判断"的步骤会调模型（`gui_plan` 的选择与校验、`script_run` 里的 `desk.decide/look/ask/ground`）；`desk`、`screenshot` 这些确定性工具不需要 key。

首次启动会把环境变量渲染成 `/home/box/.config/loomhand/shuttle/config.toml`（0600）。之后以该文件为准；改完重启容器（daemon 只在启动时读它），或设 `SHUTTLE_CONFIG_REGENERATE=1` 重新生成。

| 变量 | 默认 | 说明 |
|---|---|---|
| `OPENROUTER_API_KEY` | — | 默认 provider 的 key |
| `SHUTTLE_API_KEY` | — | 直接写进 config 的 key（等价于上一条，但落在文件里） |
| `SHUTTLE_API_KEY_ENV` | `OPENROUTER_API_KEY` | 告诉 shuttle 从哪个环境变量取 key |
| `SHUTTLE_BASE_URL` | `https://openrouter.ai/api/v1` | 任意 OpenAI 兼容端点 |
| `SHUTTLE_DECISIONS_URL` | 由 base_url 推导 | `decide` 用的 decisions 端点；非 OpenRouter 时默认 `<base>/decisions` |
| `SHUTTLE_DECIDE_API` | OpenRouter 下 `decisions`，否则 `chat` | 没有 decisions 端点就设 `chat`（概率由模型自报，未校准，会更容易升级/点错） |
| `SHUTTLE_DECIDE_MODEL` / `SHUTTLE_GROUND_MODEL` / `SHUTTLE_ASK_MODEL` | 见 upstream 默认 | 四个角色里的三个；`verify` 默认跟随 `decide` |
| `SHUTTLE_TIMEOUT_MS` | `60000` | 单次模型调用超时 |

用本地或国内可达的 OpenAI 兼容服务（vLLM / Ollama / 自建网关）时：

```bash
docker run -d --name shuttle-box --network=host --shm-size=2g \
  -e SHUTTLE_BASE_URL=http://192.168.1.10:8000/v1 \
  -e SHUTTLE_DECIDE_MODEL=qwen2.5-vl-7b-instruct \
  -e SHUTTLE_GROUND_MODEL=qwen2.5-vl-7b-instruct \
  -e SHUTTLE_ASK_MODEL=qwen2.5-vl-7b-instruct \
  shuttle-box:0.1.38
```

实测容器内直连 `openrouter.ai` 可用（HTTP 200，约 130 KB/s）。

## 安全

shuttle 会真的操作桌面上的程序，这里的边界就是容器本身。

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHUTTLE_SCRIPT_RUN` | `true` | 关掉后 `script_run` 不可用（它执行任意 JS/TS，权限等于桌面用户） |
| `SHUTTLE_SAFE_APPS` | 空（不限） | 应用白名单，逗号分隔，例如 `SHUTTLE_SAFE_APPS=firefox-bin` |
| `SHUTTLE_DESK_INPUT` | `foreground` | `background` / `auto` 在 Linux 上会退化成 foreground |
| `VNC_PASSWORD` | 空 | 设了就启用 VNC 密码（noVNC 登录时同样要求） |

其余：

- `shuttle stop` / `docker exec shuttle-box shuttle stop` 可随时停手，`shuttle resume` 恢复
- Linux 后端目前**没有** kill 热键、指示器和 confirm 弹窗（只有 stop/resume、`script_run` 开关和白名单）
- 浏览器不再需要 `--no-sandbox`：Firefox 的内容进程沙箱要建用户命名空间，Docker 默认 seccomp 会挡（`Failed to move to new namespace: Operation not permitted`），compose 里用 `security_opt: seccomp=unconfined` 放开，浏览器自身的沙箱正常生效
- 代价是 seccomp 过滤对整个容器失效，不只是浏览器
- Firefox 的首次运行弹窗（Terms of Use）用 profile 模板绕过，不用每次点

| `/home/box/.cache/box/ready` | 启动完成标记：全部进程起来后才写入，MCP 包装脚本据此判断就绪 |
| `/home/box/.cache/loomhand/shuttle/desk.sock` | shuttle daemon 的 desk 套接字 |
| `/home/box/.config/loomhand/shuttle/config.toml` | shuttle 配置（0600） |
| `/home/box/.cache/box/logs/` | `supervisor.log`、`xvnc.log`、`xfce.log`、`shuttle-serve.log`、`novnc.log`（有 HTTP 桥时还有 `mcp-http.log`） |
| `/opt/loomhand/shuttle/` | shuttle 程序、`cua-driver`、内置 Node、runtime |
| `/usr/share/novnc` | noVNC 1.7（上游 tarball，非发行版包） |
| `/opt/websockify` | websockify 的 venv（去掉发行版带来的 numpy/nodejs 依赖链） |
| `/usr/local/bin/firefox` | Firefox 包装脚本（profile 播种、代理、语言、无障碍告警） |

## 排查

| 症状 | 看哪里 |
|---|---|
| 网页打不开 / 黑屏 | `docker exec shuttle-box tail -50 ~/.cache/box/logs/xvnc.log`；`docker exec shuttle-box cat ~/.cache/box/logs/supervisor.log` |
| `desk windows` 返回空 | AT-SPI 没起来：`docker exec shuttle-box xprop -root AT_SPI_BUS` 应有值；再看 `xfce.log` |
| Firefox 没有元素树 | 用 `firefox` 包装脚本（走桌面会话总线）启动，别用裸 `docker exec` 起，那样没有 `DBUS_SESSION_BUS_ADDRESS` |
| Firefox 卡在“Welcome to Firefox / Terms of Use”弹窗 | 确认 profile 里的 `user.js` 含 `termsofuse.acceptedDate` / `acceptedVersion` 两条（包装脚本每次启动重写） |
| 画面显示但很慢 | `Xvnc` 无 GLX，浏览器走软件渲染；可调小 `SCREEN_GEOMETRY` |
| 模型调用超时/失败 | 容器内 `curl -I $SHUTTLE_BASE_URL`；模型端点必须从容器内可达 |
| 构建卡在下载 shuttle | 见上面「构建」的代理 / `SHUTTLE_RELEASE_BASE` 说明 |
| MCP 客户端报 `container ... is not running` | 容器没起：`docker compose up -d`，或把客户端命令换成 `bin/shuttle-mcp.sh` |
| `connect the session bus: ... No such file or directory` | 有 `shuttle` 进程在容器外被拉起来（没继承 D-Bus 会话）：等就绪标记出现再连，或直接用 `bin/shuttle-mcp.sh` |
| 容器 `healthy` 但 `desk windows` 为空 | XFCE 还没起完；等 `/home/box/.cache/box/ready` 出现 |
| 网页上画面很小 / 出现滚动条 | 根路径已默认 `resize=scale`；全屏按 F11 或点 noVNC 工具栏的 ⛶ |
| 改了 `SCREEN_GEOMETRY` 但桌面尺寸没变 | 容器会删掉 XFCE 保存的 `displays.xml`，若仍不生效请 `docker compose up -d --force-recreate` |
| Firefox 起了就退出 / 报 already running | 持久化卷里的 profile 锁残留（`~/.mozilla/firefox/*/lock`），容器启动时已自动清理 |
