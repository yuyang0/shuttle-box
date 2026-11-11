# source-watch — 按配置跟踪信息源的新内容 → 抓取 → 摘要

## 用途

`config.toml` 里每个 `[sources.<名字>]` 一节就是一个信息源（YouTube 频道、X 时间线、公众号）；
总入口按 `every` 判断该源是否到期，到期就调用它对应的模块抓取，产物落在 `output/<名字>/`。
定时任务（每 4 小时）把本轮产物写成摘要，建成飞书文档并发到 `investment` 群。

## 目录

```
config.toml                     # 源清单：type / every / 平台参数
last_success.json               # 每个源上一次「确实抓到内容」的时间
pending.json                    # 还没被摘要的产物清单（摘要发完再清空）
output/<名字>/                  # 该源的产物与私有状态
  state.json                    #   去重游标、待处理队列（模块自己维护）
  meta/<id>.json                #   YouTube：标题/发布时间/时长/简介
  transcripts/<date>-<id>.txt   #   YouTube：转写稿
  timeline/<date>-<HHMMSS>.md   #   X：本轮新增帖子
  articles/<date>-<id>.md       #   微信：本轮新增文章正文
output/summaries/<YYYY-MM-DD-HHMMSS>.md # 每次摘要一份（定时任务写）
scripts/source_watch.py         # 总入口：读配置、逐源调度、汇总输出
scripts/shuttle_client.py       # MCP 客户端：http / docker exec stdio / local stdio
scripts/modules/base.py         # Ctx：到期判断、产物落盘、源私有状态、desk 调用
scripts/modules/youtube.py      # YouTube 频道模块
scripts/modules/x.py            # X 时间线模块（驱动 Firefox）
scripts/modules/wechat.py       # 公众号模块（驱动微信客户端）
tests/test_source_watch.py      # 调度与模块的单元测试
TASK.md                         # 每日任务 prompt（cron 任务配置只指向它）
```

## 用法

```bash
python3 scripts/source_watch.py run                 # 跑所有到期的源
python3 scripts/source_watch.py run --only NAME     # 只跑一个源（可重复）
python3 scripts/source_watch.py run --force         # 忽略更新频率，本轮必跑
python3 scripts/source_watch.py run --json          # 机器可读：每个源 + 产物文件列表
python3 scripts/source_watch.py list                # 频率、上次成功、下次到期
python3 scripts/source_watch.py reset NAME          # 清掉该源的去重状态（产物保留）
python3 scripts/source_watch.py pending             # 还没被摘要的产物（--clear 清空）
python3 scripts/source_watch.py x --screens 2 --format md    # 临时读一次 X（不落盘、不动游标）
python3 scripts/source_watch.py wx --count 2 --format md     # 临时读一次公众号（同上）
python3 scripts/source_watch.py yt --channel @handle --limit 5   # 临时列一次频道条目（同上）
```

`run` 的退出码：`0` 正常（含全部跳过）、`1` 有源失败、`2` 配置错误。

## 配置

```toml
[defaults]
every = "1d"                 # 公共默认值；源内同名键优先
seen_keep_days = 60          # 已读记录保留多少天（全局默认）

[sources.youtube]
type = "youtube"             # 模块名，见 scripts/modules/
channels = ["UCTAJ0JnR9oYvILu-mtNcufQ"]   # 多个频道写多个，也接受 @handle / 频道 URL
budget_seconds = 2400        # 单轮转写墙钟预算（整个源共享）；0 = 不限
long_media_seconds = 3600    # 超过只落元数据，不转写

[sources.x]
type = "x"
every = "3h"
screens = 8                  # 滚几屏，每屏约 4–8
tabs = []                    # Home 的页内标签，如 ["Following"]；空 = Home 当前那一栏
accounts = []                # 账号时间线，如 ["@handle", ...]
seen_keep_days = 15          # X 推文量大，只记最近 15 天

[sources.wx]
type = "wechat"
every = "23h"
accounts = ["Kevin策略研究"]  # 多个公众号写多个
count = 1                    # 每个账号读最新几篇
seen_keep_days = 15          # 公众号更新快，只记最近 15 天
```

- **优先级**：命令行参数 > 配置文件 > 内置默认。`x` / `wx` / `yt` 这些临时子命令默认读对应
  小节的参数，命令行给了就覆盖对应那一项（没给的仍取配置）。
- `type` 必须存在于 `scripts/modules/__init__.py` 的 `MODULES`；其余键原样传给模块。
- `every` 支持 `30m` / `6h` / `1d` / `1w`，纯数字按秒。
- 源名字同时是产物目录名：`output/<名字>/`；日报写在 `output/summaries/`。

## 调度规则

脚本不自我调度、不驻留：外部定时器决定何时触发 `run`，`every` 只负责让本次触发跳过还没到期的源。
未到期的源在毫秒级返回（实测一次 `run` 约 0.09s），不联网、不启动转写；触发多密都不带来额外开销。

- 到期判断只有一处：`Ctx.due()`。`--force`、从未成功、或距上次成功已满 `every` 即到期；
  模块自己读 `ctx.due()` 决定是否跳过，跳过返回空列表。
- **只有本轮确实产出了文件**才写 `last_success.json`（逐源落盘，中途失败不丢前面的源）。
  没有新内容的源不会推进时间，所以下次调度仍会重新检查。
- 空内容不落盘：`ctx.write()` 对空字符串返回 `None`；入口再兜一层，过滤零字节文件。
- 单个源抛异常只把它标成 `error`，其它源照常跑；最终退出码 1。
- 全都没有产物时，人类可读输出只有一行 `无新条目`。

## 新增一个源模块

在 `scripts/modules/` 写一个模块，只暴露 `run(ctx) -> list[Path]`：

```python
from .base import Ctx

def run(ctx: Ctx) -> list[Path]:
    if not ctx.due():
        return []
    state = ctx.state()                 # output/<名字>/state.json
    ...
    written = []
    for item in new_items:
        written += [p for p in [ctx.write_json(f"meta/{item['id']}.json", item)] if p]
    ctx.save_state(state)
    return written
```

然后在 `scripts/modules/__init__.py` 的 `MODULES` 里登记 `type` 名字。主流程不认识任何平台，
新增平台不需要改 `source_watch.py`。

约定：

- 去重游标放 `ctx.state()`，首次运行先把现有条目标记为已读（`primed`），不要回灌历史。
- 本轮跑不完的条目留在自己的队列里，下轮继续——不要指望源总能把它们再列出来。
- 产物用 `ctx.write()` / `ctx.write_json()` 写进 `ctx.outdir`，返回写出的文件列表。

## YouTube 模块

- **一个源，多个频道**：`channels` 是频道列表（ID、`@handle` 或频道 URL）；整节共用一个
  `every`、一条 `last_success` 和一份转写预算，去重游标与队列也共用。
- **去重**：`state.json` 的 `seen` 记「视频 ID → 发布时间」，只保留最近 `seen_keep_days`
  （默认 60 天）；旧的纯 ID 列表会自动迁移，过期记录在加载时丢弃。
- **发现**：频道 RSS `feeds/videos.xml?channel_id=UC…`（最近 15 条，带 `published`）；被限流时回落
  `yt-dlp --flat-playlist`。单个频道列不出来只告警，全部频道都失败才算这个源出错。
- **转写**：调 `transcribe <url> --engine whisper|sensevoice`；标题/简介含 ≥8 个中日韩字符用
  `sensevoice`，否则 `whisper`。已有同 ID 的稿子就跳过，失败不记已读、下轮重试。
- **长内容**：超过 `long_media_seconds`（默认 60 分钟）只写 meta 并记进 `state.pending_long`。
- **预算**：`budget_seconds` 装不下的条目留在队列里等下一轮；每处理完一条立刻写 `state.json`。
- **网络**：默认走 `http://127.0.0.1:7890`，用 `SOURCEWATCH_PROXY` 覆盖（空串表示直连）。

## X 模块

- **一个源，多个入口**：`tabs` 是 Home 的页内标签（如 `Following`），`accounts` 是具体账号的
  时间线（如 `@handle`）；两个都空就读 Home 当前那一栏。整节共用一个 `every` 和一条
  `last_success`，不按标签或账号分别计时。
- **读取**：经 `scripts/shuttle_client.py` 调 shuttle 的 `desk`，把 Firefox 带到 Home 切标签，
  或直接用地址栏打开 `x.com/<handle>`，滚动读取可访问树里的帖子与 permalink。
- **只读当前标签页**：Firefox 的窗口树同时包含所有标签页的文档，后台文档没有 `showing`；
  解析按活动文档裁剪，否则会把后台旧标签的帖子当成当前页内容。
- **时间线标签**：`tabs` 里的标签（如 `Investment`）是 SPA 异步渲染的，模块会等标签栏出现，
  在活动文档里找对应的 `tab` 节点点击，用选中态或首帖变化确认；找不到就告警。
- **标签页**：窗口里已有 x.com 标签就复用，没有才新开一个；读完不关，下一轮继续用，避免反复开关。
  后台标签的文档也在窗口树里，所以解析只取带 `showing` 的当前文档，否则会读到旧标签的帖子；
  不复用、不切换用户已有的 X 标签，标签总数不变。窗口被用户动过导致数量对不上时，宁可不关
  也不误关用户的页面。
- **去重**：permalink 里的 status id 为主键；没有 permalink 的帖子（纯图片、被限流的卡片）用
  作者+正文哈希兜底，所以同一帖子不会因为相对时间变化被重复记录，多个入口读到同一条也只留一次。
  已读记录只保留最近 `seen_keep_days`（默认 60 天）。
- **临时查看**：`source_watch.py yt --channel … --limit 5` 只列条目，不落盘、不动游标；
  加 `--meta` 才逐条取时长与简介。
- **产物**：一次运行一个 `timeline/<date>-<HHMMSS>.md`，按入口分节（`## Home · Following`、
  `## @handle`），只包含本轮新帖；没有新帖就不写文件。

## 微信模块

- **一个源，多个账号**：`accounts` 是公众号名列表，每个账号读最新 `count` 篇；整节共用一个
  `every` 与一条 `last_success`。`feed = true` 则改读「公众号」消息流最新一篇（与 `accounts`
  互斥）。
- **读取**：驱动微信客户端（Qt 外壳 + 内嵌 Chromium），搜索账号 → 进主页 → 打开「文章」→
  点卡片 → 从 Chromium 的段落节点读正文。微信必须已经打开并登录，模块从不启动或重启它。
- **去重**：优先用 mp.weixin.qq.com 永久令牌，老式查询串用 `mid_idx`，都没有则按账号+标题哈希；
  已读记录只保留最近 `seen_keep_days`（默认 60 天）。
- **产物**：一篇一个 `articles/<date>-<id>.md`；正文已经是文字，不走转写。
- **标签**：默认关掉本轮打开的浏览器标签；`source_watch.py wx --close-tabs` 清理历史遗留。

## 每日摘要任务

cron 任务每 4 小时（00/04/08/12/16/20）执行 `run --json`，摘要的对象是 `pending.json` 里的文件（本轮
新产物 + 以前没摘完的），任务 prompt 的唯一来源是 `TASK.md`（cron 配置里的 prompt 只写「读取
TASK.md 并照做」）。摘要写成 `output/summaries/<YYYY-MM-DD-HHMMSS>.md`、发成飞书文档并转发到
`investment` 群（群消息正文就是摘要本身，文档链接只附在末尾），全部成功后才 `pending --clear`；
中途失败则保留清单，下一轮重摘。最终回执也把摘要正文贴进定时任务关联的会话，不只给文件路径。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试用临时目录和假模块覆盖调度、空产物、错误隔离、`--only`、`--force`，以及 YouTube 模块的
RSS 解析、首次 priming、预算与长内容分流；不依赖网络、桌面或转写引擎。
