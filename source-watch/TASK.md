# 每日任务 prompt（唯一来源）

本文件是定时任务 `cron_uix7zel`（每 6 小时：00/06/12/18 点 Asia/Shanghai，cwd `/home/yangyu/mycode/box/source-watch`）
的 prompt 唯一来源：任务配置里的 prompt 只写「读取本文件并照做」，实际步骤全在这里。
要改任务行为就改本文件；任务配置的其它字段（schedule / cwd / 超时 / 重试 / silentRule）不在本文件范围内。

---

执行「信息源新内容摘要」任务，工作目录 `/home/yangyu/mycode/box/source-watch`。

步骤（按序执行）：

1. `python3 scripts/source_watch.py run --json`：**必须用 `run_in_background: true` 起在后台**，拿到 job id
   后立刻用 `job_output` 配 `wait: true` 等它结束。这是「抓取 + 转写」，单条 30–40 分钟的视频约需
   12 分钟，会超过前台命令的 10 分钟上限——所以从一开始就在后台跑，然后反复 `job_output` 等到 job
   明确终态为止（一次 wait 超时不算结束）。**在 job 结束之前不要进入第 2 步，也不要自己重跑。**
   每个源都有自己的墙钟预算，装不下的条目留在队列里等下一轮，这是正常的，不要当成失败。

2. 看 JSON 的 `pending`（待摘要文件清单）与每个源的 `status`。`pending` 为空且没有 `error` →
   只回复一行「无新条目」并结束（`skip` 表示未到期、`empty` 表示没有新内容）；有 `error` 就在回执里
   如实点名该源和错误信息。注意 `pending` 里可能有上一次触发留下的文件（例如手动提前跑的产物），
   这些也要摘，不能只看本轮 `outputs`。

3. 读取 `pending` 里列出的文件，按源分节，为每个条目写一节：
   - 标题 / 来源 / 发布时间 / 时长（来自 `meta/<id>.json`）
   - 3–5 条要点（只依据转写稿，不臆测；引用关键数字要准确）
   - 1 句结论或值得关注之处
   - 只有 meta、没有转写稿的长条目：写「标题 + 简介要点（未转写）」
   - 文字源本身就是正文：X 的 `timeline/*.md` 逐条摘一句要点（同主题可合并），
     微信的 `articles/*.md` 按文章写 3–5 条要点 + 1 句结论
   - 语言跟随原文

4. 写入 `output/summaries/<YYYY-MM-DD-HHMMSS>.md`：每次触发一份新文件，不追加、不覆盖旧文件；
   时间戳取当前时间（`date +%Y-%m-%d-%H%M%S`）。

5. 把这份摘要发出去（**只有第 4 步真的写了内容才做**）：
   a. 建文档（必须在项目根目录执行，`--content` 只收 cwd 相对路径）：
      `lark-cli docs +create --doc-format markdown --content @output/summaries/<YYYY-MM-DD-HHMMSS>.md --title "信息源摘要 <YYYY-MM-DD HH:MM>" --as bot`
      从输出里取 `data.document.url`，记为 URL。
   b. 发群消息：**消息正文就是摘要内容本身**，不是「新增 N 条 + 一个链接」；文档链接只作为末尾一行。
      用 stdin 直接把文件内容和链接拼成一条消息：
      `{ cat output/summaries/<YYYY-MM-DD-HHMMSS>.md; printf '\n\n---\n文档：%s\n' "URL"; } | lark-cli im +messages-send --chat-id oc_8bde92914fd6221c3a2845987d95e0d1 --as bot --idempotency-key "source-watch-<YYYY-MM-DD-HHMMSS>" --markdown -`
      幂等键必须带时间：一天会跑四次，只写日期会让当天第二次之后的群消息被当成重复丢掉。
   四点注意：① 新建文档默认就是「组织内可读」，**不要**再跑 `drive permission.public patch` 或
   `drive +member-add`（它们是 high-risk-write、要 --yes，且完全没必要）；② 只能 `--as bot`（user
   身份未登录）；③ 输出里若出现 `DPoP ... read-only file system` 的 WARN 属正常，会自动回落
   Bearer，忽略即可；④ 群里只发一条消息，不要重复发。

6. 文档和群消息都成功之后才清空 pending：`python3 scripts/source_watch.py pending --clear`。
   任一步失败就**不要**清空，下一轮会重新摘这批文件。

7. 最终回执就是给「和这个定时任务关联的会话」看的：**把摘要正文完整贴进回复**（它会被投递到该
   会话，不能只给文件路径或链接），最后跟一行汇总：处理 N 个条目、跳过 M 个源（未到期/无新内容）、
   摘要文件路径、飞书文档链接。没有新内容时仍然只回一行「无新条目」。

约束：不要修改 `scripts/` 与 `config.toml`；不要 pip 安装依赖；不要回显任何密钥或令牌；
总时长上限 60 分钟，接近上限就停下并如实报告已完成部分。
