# source-watch — 按配置跟踪信息源的新内容 → 抓取 → 摘要

本目录是独立项目：`config.toml` 里每个源一节，总入口按 `every` 调度对应模块，产物落在
`output/<名字>/`，最后由 cron 任务把当日产物写成摘要、建成飞书文档并发到 `investment` 群。
本地产物只落在本目录内。

## 命令

```bash
python3 scripts/source_watch.py run                 # 跑所有到期的源
python3 scripts/source_watch.py run --only NAME     # 只跑一个源（可重复）
python3 scripts/source_watch.py run --force         # 忽略更新频率，本轮必跑
python3 scripts/source_watch.py run --json          # 每个源 + 产物文件列表（cron 用）
python3 scripts/source_watch.py list                # 频率、上次成功、下次到期
python3 scripts/source_watch.py reset NAME          # 清去重状态，产物与 last_success 保留
python3 scripts/source_watch.py x --screens 2       # 临时读 X（不落盘、不动游标）
python3 scripts/source_watch.py wx --count 2        # 临时读公众号（同上）
python3 scripts/source_watch.py yt --channel @x --limit 5   # 临时列 YouTube 条目（同上）
python3 -m unittest discover -s tests -v            # 调度与模块的单元测试
```

## 落点

- `config.toml` 源清单（`type` / `every` / 平台参数），源名同时是产物目录名
- `last_success.json` 每个源上一次确实抓到内容的时间；没产出就不推进
- `pending.json` 还没被摘要的产物清单；`run` 追加，摘要发出后 `pending --clear`
- `output/<名字>/state.json` 模块私有的去重游标与待处理队列
- `output/<名字>/transcripts/`、`meta/` YouTube 产物；`timeline/*.md` X 产物；`articles/*.md` 微信产物
- `output/summaries/<YYYY-MM-DD-HHMMSS>.md` 每次摘要一份（cron 任务写入，不追加）
- 飞书：摘要 → 云文档（`--as bot` 新建，默认组织内可读）→ `investment` 群发正文（链接附末尾）
- `TASK.md` 每日任务 prompt 的唯一来源；cron 任务配置里的 prompt 只写「读取本文件并照做」

## 约定

- 脚本不调度、不驻留：外部定时器负责触发，`every` 只用于跳过；未到期时一次 `run` 约 0.1s，
  不联网也不转写。
- 到期判断只在 `Ctx.due()` 一处；模块自己调用它，跳过就返回空列表，主流程不替模块决定。
- 只有本轮有产物才更新 `last_success.json`；全无产出时人类可读输出只有一行「无新条目」。
- 摘要消费的是 `pending.json`（`run --json` 会把它一起输出），所以手动提前跑的产物也能被补摘；
  摘要写完并发出后才 `pending --clear`。群消息与最终回执都直接给正文，不只给文件/链接。
- 空内容不落盘：`ctx.write()` 对空串返回 `None`，入口再过滤零字节文件。
- 单个源失败只标 `error` 并继续其它源；退出码 `0` 正常 / `1` 有源失败 / `2` 配置错误。
- 模块只暴露 `run(ctx) -> list[Path]`，平台细节不出模块；新增平台 = 加模块 + 在 `MODULES` 登记。
- 参数优先级：命令行 > 配置文件 > 内置默认。子命令（`x` / `wx` / `yt`）把对应小节的配置当默认值，
  命令行显式给出时才覆盖，没给的那项继续用配置。
- 首次运行只把现有条目标记为已读（`primed`），不处理历史；本轮跑不完的条目留队列下轮继续。
- YouTube / X / 微信都是单节源：`channels` / `tabs` / `accounts` 只是入口列表，整体只有一个
  `every`、一条 `last_success`；YouTube 的预算与队列也跨频道共享。三个源的 `seen` 都只保留最近
  `seen_keep_days`（默认 60 天），旧格式（纯 ID 列表）在加载时自动升级并裁剪。X 的产物合并成
  一个按入口分节的文件，YouTube 与微信仍是一条/一篇一个文件。
- 摘要语言跟随原文。YouTube 转写与公众号文章按 `~/.agents/prompts/long-article-summary.md` 做详细
  总结（原文的 15%–30%、六段结构、附质量审计），每个条目以原始链接开头；X 先给要点总结，
  再附按热门度（点赞+转发+回复）排序的表格：作者 / 时间 / 内容概述 / 点赞 / 回复 / 链接。
  只有 meta 的长视频写「标题 + 简介要点（未转写）」。
- 转写引擎按内容语言自动选：≥8 个中日韩字符用 `sensevoice`，否则 `whisper`。
- X 与微信模块直接调 shuttle 的 `desk`（同目录 `scripts/shuttle_client.py`，http → docker →
  local 自动挑），不 subshell 到别的脚本。
- X 每轮开一个**全新**的窗口，窗口里只保持一个 x.com 标签；抓完 `ctrl+w` 关掉唯一标签（标签没了
  窗口随之关闭），再用窗口 id 确认它真的消失。默认 Edge：用 `--app=<url>` 开无地址栏的 app 窗口，
  标题只有页面名（`Home / X`），而 Chromium 普通窗口的标题带 profile 名、会和用户自己开着的窗口
  完全重名；app 窗口没有地址栏，换页面走 `swap_window`（先开新窗口再关旧的）。`--browser firefox`
  换回 Firefox 的 `--new-window` 普通窗口。
  读取一律 `snapshot id=<窗口 id>`：同一进程里的窗口可以同名，标题挑不准。发按键、关闭之前先用标题
  探测确认 `window=` 挑中的就是本轮窗口，不是就报错停手，不动用户的标签。
- Firefox 的窗口树里包含**所有**标签页的文档（后台文档 `visible` 但不带 `showing`），所以解析
  必须先按活动文档裁剪，否则会读到后台旧标签的帖子；新窗口的树里还可能残留一个 `New Tab` 文档，
  活动文档找不到选中态时按文档名以 `/ X` 结尾兜底；页内时间线标签是 SPA 异步渲染的，选中时
  在活动文档里找 `tab` 节点并点击，用选中态或首帖变化确认；找不到就告警，不冒险按坐标点。
- 微信必须已登录，模块只读窗口、不启动也不重启它。
- 不要手改 `last_success.json` 与 `output/*/state.json`；要重跑用 `run --only NAME --force`，
  要清游标用 `reset NAME`。
