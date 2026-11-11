# 每日任务 prompt（唯一来源）

本文件是定时任务 `cron_uix7zel`（每 4 小时：00/04/08/12/16/20 点 Asia/Shanghai，cwd `/home/yangyu/mycode/box/source-watch`）
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

3. 读取 `pending` 里列出的文件，按源分节写摘要，语言跟随原文。

   **YouTube 转写与公众号文章**：严格按 `~/.agents/prompts/long-article-summary.md` 的规则做
   详细总结，而不是简单概括——长度约为原文的 15%–30%，按原文顺序覆盖所有实质章节，保留关键
   数字、单位、比较基准与限定条件，区分事实/作者观点/推断/预测，重要结论必须带上依据与推导，
   最后附质量审计（摘要长度比、加权信息覆盖率、论证步骤覆盖率、数据保留率、未解决的遗漏；
   不达标就补全再复查，不得虚报）。

   每个条目**第一行放原始链接**（YouTube 取 `meta/<id>.json` 的 `url`，公众号取
   `articles/*.md` 里的「链接」），然后按上述文件规定的六段结构写：
   1. 核心结论
   2. 分章节详细总结
   3. 关键数据与事实
   4. 核心论证链
   5. 风险、反方观点与不确定性
   6. 质量审计报告

   - 标题、来源、发布时间、时长放在链接之后作为一行元信息（来自 `meta/<id>.json`）。
   - 只有 meta、没有转写稿的长视频：仍然先放链接，写「标题 + 简介要点（未转写）」，
     不要编造章节或论证。

   **X**：先写要点总结，再附一张按热门度排序的原始推文表。

   *要点总结*：3–6 条，按主题归纳本轮推文（同主题的可合并成一条并注明条数），
   保留关键数字与说法，不逐条复述。

   *原始推文表*（另起一行，按热门度从高到低）：

   | 作者 | 时间 | 内容概述 | 点赞 | 回复 | 链接 |
   |---|---|---|---|---|---|

   - 热门度 = 点赞 + 转发 + 回复之和，降序；转发数不在表里，仅用于排序。
     抓不到互动数据的按 0 计，排在末尾。
   - 作者写显示名 + `@handle`；时间用推文里的相对时间；链接用 permalink。
   - 内容概述一到两句话；同一主题合并的行，用组内最高互动量参与排序。
   - 点赞/回复取 `timeline/*.md` 里的互动数据，抓不到的写 `-`。

4. 写入 `output/summaries/<YYYY-MM-DD-HHMMSS>.md`：每次触发一份新文件，不追加、不覆盖旧文件；
   时间戳取当前时间（`date +%Y-%m-%d-%H%M%S`）。文件开头是一级标题（含日期时间），
   然后按源分节（`## YouTube` / `## 微信公众号` / `## X`）。

5. 把这份摘要发出去（**只有第 4 步真的写了内容才做**）：
   a. 建文档（必须在项目根目录执行，`--content` 只收 cwd 相对路径）：
      `lark-cli docs +create --doc-format markdown --content @output/summaries/<YYYY-MM-DD-HHMMSS>.md --title "信息源摘要 <YYYY-MM-DD HH:MM>" --as bot`
      从输出里取 `data.document.url`，记为 URL。
   b. 发群消息：**消息正文就是摘要内容本身**，不是「新增 N 条 + 一个链接」；文档链接只作为末尾一行。
      用 stdin 直接把文件内容和链接拼成一条消息：
      `{ cat output/summaries/<YYYY-MM-DD-HHMMSS>.md; printf '\n\n---\n文档：%s\n' "URL"; } | lark-cli im +messages-send --chat-id oc_8bde92914fd6221c3a2845987d95e0d1 --as bot --idempotency-key "source-watch-<YYYY-MM-DD-HHMMSS>" --markdown -`
      幂等键必须带时间：一天会跑六次，只写日期会让当天第二次之后的群消息被当成重复丢掉。
      如果整份摘要超过 30000 字符，群消息改发「各条目的原始链接 + 核心结论 + 文档链接」，
      完整内容看文档；但不要只发一个链接。
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
