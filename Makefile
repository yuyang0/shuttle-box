SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

# 本地运行默认用 registry 上的 latest；构建时会额外打一个带 git 描述的 tag 便于追溯
IMAGE               ?= ghcr.io/yuyang0/shuttle-box
TAG                 ?= latest
REF                 := $(IMAGE):$(TAG)
BUILD_TAG           ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
CONTAINER           ?= shuttle-box
# 跟着 Dockerfile 的 ARG 走，避免两处版本各自漂移
SHUTTLE_VERSION     ?= $(shell sed -n "s/^ARG SHUTTLE_VERSION=//p" Dockerfile)

# 国内网络两个瓶颈：apt 走镜像，GitHub（shuttle release）走本机代理。
# 代理只监听 127.0.0.1，所以构建固定用 --network=host；PROXY= 可整体关掉。
APT_MIRROR          ?= http://mirrors.ustc.edu.cn/ubuntu
PROXY               ?= http://127.0.0.1:7890
# no_proxy 只认主机名，所以从镜像源里取出 host（去掉 scheme 和路径）
APT_HOST            ?= $(firstword $(subst /, ,$(patsubst http://%,%,$(patsubst https://%,%,$(APT_MIRROR)))))
NO_PROXY_LIST       ?= localhost,127.0.0.1,$(APT_HOST)

SHUTTLE_RELEASE_BASE ?=

BUILD_ARGS := --build-arg APT_MIRROR=$(APT_MIRROR)
ifneq ($(strip $(SHUTTLE_VERSION)),)
BUILD_ARGS += --build-arg SHUTTLE_VERSION=$(SHUTTLE_VERSION)
endif
ifneq ($(strip $(SHUTTLE_RELEASE_BASE)),)
BUILD_ARGS += --build-arg SHUTTLE_RELEASE_BASE=$(SHUTTLE_RELEASE_BASE)
endif
ifneq ($(strip $(PROXY)),)
BUILD_ARGS += --build-arg HTTP_PROXY=$(PROXY) \
              --build-arg HTTPS_PROXY=$(PROXY) \
              --build-arg http_proxy=$(PROXY) \
              --build-arg https_proxy=$(PROXY) \
              --build-arg NO_PROXY=$(NO_PROXY_LIST) \
              --build-arg no_proxy=$(NO_PROXY_LIST)
endif

# 桌面参数（运行期，走 compose 的环境变量）
URL                 ?= https://www.baidu.com
GEOMETRY            ?= 1600x1200
NOVNC_PORT          ?= 16080
MCP_HTTP_PORT       ?= 8765

.PHONY: help build build-http up down restart recreate wait-ready logs logs-raw status shell \
        tools mcp-test screen windows browser shot timeline resize stop backup restore \
        clean clean-volume clean-image

define HELP_TEXT
shuttle-box

  构建
    make build                构建 $(REF)（apt 镜像 + 本机代理）
    make build PROXY=         不加代理构建

  运行
    make up                   docker compose up -d 并等到桌面就绪
    make down                 停止并移除容器
    make recreate             强制重建容器（改了 compose 后用）
    make logs                 跟踪容器内的 supervisor.log
    make status               健康状态 + 桌面尺寸 + 窗口列表
    make shell                进容器（用户 box）

  驱动（都走 MCP，容器没起会自动拉起）
    make tools                列出 MCP 工具
    make mcp-test             MCP over HTTP 握手冒烟测试
    make mcp-url              打印 MCP 端点与 Authorization 头
    make browser              打开 Firefox 并访问 $(URL)
    make screen               当前桌面尺寸
    make windows              当前窗口列表
    make shot                 截图到 ./shot.png
    make timeline             抓当前 X 时间线正文 -> ./timeline.json
    make resize GEOMETRY=…    运行期换分辨率

  清理
    make stop                 停止容器（保留卷）
    make backup               备份 Firefox profile（cookies/登录态）到 ./backups
    make restore              从最近一次备份恢复（restore FILE=路径 可指定）
    make clean                删除容器，保留卷（登录态不丢）
    make clean-volume         连卷一起删，会先自动备份，且需 FORCE=1
    make clean-image          删除镜像

  可覆盖变量：IMAGE TAG CONTAINER APT_MIRROR PROXY SHUTTLE_VERSION
              URL GEOMETRY NOVNC_PORT

  noVNC: http://127.0.0.1:$(NOVNC_PORT)/
endef

export HELP_TEXT

help:
	@printf '%s\n' "$$HELP_TEXT" || true

build:
	docker build --network=host $(BUILD_ARGS) -t $(REF) -t $(IMAGE):$(BUILD_TAG) .

up:
	IMAGE=$(IMAGE) TAG=$(TAG) docker compose up -d
	$(MAKE) wait-ready

# 用 CI 推到 ghcr 的镜像替换本地构建（默认拉私有包，先 docker login ghcr.io）
pull:
	docker pull $(IMAGE):$(TAG)

wait-ready:
	@echo -n "等待桌面就绪"
	@for _ in $$(seq 1 60); do \
	    if docker exec $(CONTAINER) test -f /home/box/.cache/box/ready 2>/dev/null; then \
	        echo " ok"; exit 0; \
	    fi; \
	    echo -n "."; sleep 1; \
	done; \
	echo; echo "超时：docker exec $(CONTAINER) cat /home/box/.cache/box/logs/supervisor.log" >&2; exit 1

down:
	docker compose down

restart: recreate

recreate:
	IMAGE=$(IMAGE) TAG=$(TAG) docker compose up -d --force-recreate
	$(MAKE) wait-ready

stop:
	docker stop $(CONTAINER)

logs:
	docker exec -it $(CONTAINER) tail -n 80 -f /home/box/.cache/box/logs/supervisor.log

logs-raw:
	docker logs -f --tail 80 $(CONTAINER)

status:
	@docker ps --filter name=$(CONTAINER) --format 'status: {{.Status}}'
	@docker exec $(CONTAINER) shuttle --version
	@docker exec $(CONTAINER) shuttle desk screen
	@docker exec $(CONTAINER) shuttle desk windows | python3 -c 'import json,sys; ws=json.load(sys.stdin); print(f"{len(ws)} windows:", ", ".join(sorted({w.get("app","?") for w in ws})))'

shell:
	docker exec -it $(CONTAINER) bash

tools:
	@bin/mcp-drive.py tools

mcp-test:
	@TOKEN=$$(grep '^SHUTTLE_MCP_TOKEN=' .env | cut -d= -f2); \
	[ -n "$$TOKEN" ] || { echo "SHUTTLE_MCP_TOKEN 未设置（写入 .env 后 docker compose up -d）"; exit 1; }; \
	resp=$$(curl -s -m 10 -X POST "http://127.0.0.1:$(MCP_HTTP_PORT)/mcp" \
	    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
	    -H "Authorization: Bearer $$TOKEN" \
	    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"make","version":"0"}}}'); \
	printf '%s' "$$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  ', d['result']['serverInfo'])"

mcp-url:
	@printf 'url:    http://127.0.0.1:%s/mcp\nheader: Authorization: Bearer %s\n' \
	    "$(MCP_HTTP_PORT)" "$$(grep '^SHUTTLE_MCP_TOKEN=' .env | cut -d= -f2)"

browser:
	@bin/mcp-drive.py open-url $(URL)

screen:
	@bin/mcp-drive.py screen

windows:
	@bin/mcp-drive.py windows

shot:
	@bin/mcp-drive.py shot ./shot.png

timeline:
	@bin/x-timeline.py > ./timeline.json
	@printf '%s\n' "$$(python3 -c 'import json;print(len(json.load(open("timeline.json"))))') 条 -> ./timeline.json"

resize:
	docker exec $(CONTAINER) xrandr --output VNC-0 --mode $(GEOMETRY)

backup:
	@bin/firefox-profile.py backup

restore:
	@bin/firefox-profile.py restore $(if $(FILE),$(FILE),)

# 默认只删容器：卷里是 Firefox 的登录态和 shuttle 配置
clean:
	docker compose down
	@echo "容器已删除，卷保留（登录态、配置都在）。要连卷删除：make clean-volume FORCE=1"

clean-volume:
	@if [ "$(FORCE)" != "1" ]; then \
	    echo "这会删除卷 box_box-home，Firefox 登录态与 shuttle 配置一起没掉。"; \
	    echo "确认请用：make clean-volume FORCE=1（会先自动备份 profile）"; \
	    exit 1; \
	fi
	docker exec $(CONTAINER) true 2>/dev/null && bin/firefox-profile.py backup || true
	docker compose down -v
	@echo "容器与卷已删除；最近备份在 ./backups（make restore 需要容器已重建）"

clean-image:
	docker rmi $(REF) || true
