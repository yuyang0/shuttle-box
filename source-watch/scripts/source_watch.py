#!/usr/bin/env python3
"""source-watch 总入口：读 config.toml，逐个信息源运行对应模块。

用法：
    source_watch.py run [--only NAME]... [--force] [--json]
    source_watch.py list                  # 源、频率、上次成功、下次到期
    source_watch.py reset NAME            # 清掉某个源的去重状态（重新从当前条目开始）
    source_watch.py pending [--clear]     # 还没被摘要的产物；摘要写完再清
    source_watch.py x [--tab Following] [--screens 2] [--format md]
                                          # 临时读一次 X 时间线（不落盘、不动游标）
    source_watch.py wx [--account 名字] [--count 2] [--format md]
                                          # 临时读一次公众号文章（同上）
    source_watch.py yt --channel @handle --limit 5 [--format md]
                                          # 临时列一次 YouTube 频道的最新条目

调度：每个源在 config.toml 里配 `every`（30m / 6h / 1d / 1w）。模块自己用
`ctx.due()` 判断是否跳过——距上一次“确实抓到内容”未满 every 就跳过。上次成功时间
持久化在 `last_success.json`，只有本轮有产物才更新。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from modules import MODULES                                      # noqa: E402
from modules.base import ConfigError, Ctx, parse_duration, parse_time  # noqa: E402

ROOT = Path(os.environ.get("SOURCEWATCH_ROOT") or SCRIPT_DIR.parent)
CONFIG = ROOT / "config.toml"
LAST_SUCCESS = ROOT / "last_success.json"
PENDING = ROOT / "pending.json"
OUTPUT_DIR = ROOT / "output"


# ───────────────────────── 配置与持久化 ─────────────────────────

def load_config() -> dict[str, dict]:
    """读 config.toml；[defaults] 并入每个源，源内同名键优先。"""
    import tomllib

    if not CONFIG.exists():
        raise ConfigError(f"缺少配置文件：{CONFIG}")
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    defaults = data.pop("defaults", {})
    sections = data.pop("sources", None)
    if not isinstance(sections, dict) or not sections:
        raise ConfigError("config.toml 里没有 [sources.<名字>] 小节")

    sources: dict[str, dict] = {}
    for name, section in sections.items():
        if not isinstance(section, dict):
            raise ConfigError(f"{name}: 必须是 [sources.{name}] 这样的小节")
        cfg = {**defaults, **section}
        type_name = str(cfg.get("type") or "").strip()
        if not type_name:
            raise ConfigError(f"{name}: 缺少 type（可选：{', '.join(MODULES)}）")
        if type_name not in MODULES:
            raise ConfigError(f"{name}: 未知 type {type_name!r}（可选：{', '.join(MODULES)}）")
        if not cfg.get("every"):
            raise ConfigError(f"{name}: 缺少 every（示例：1d、6h）")
        parse_duration(cfg["every"])  # 早失败：别等到跑起来才发现单位写错
        sources[name] = cfg
    return sources


def load_last() -> dict:
    if not LAST_SUCCESS.exists():
        return {}
    try:
        data = json.loads(LAST_SUCCESS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"last_success.json 不是合法 JSON：{error}") from error
    return data if isinstance(data, dict) else {}


def save_last(data: dict) -> None:
    LAST_SUCCESS.parent.mkdir(parents=True, exist_ok=True)
    tmp = LAST_SUCCESS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(LAST_SUCCESS)


def load_pending() -> list[str]:
    if not PENDING.exists():
        return []
    try:
        data = json.loads(PENDING.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"pending.json 不是合法 JSON：{error}") from error
    return [str(x) for x in data] if isinstance(data, list) else []


def save_pending(files: list[str]) -> None:
    tmp = PENDING.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(files, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PENDING)


def add_pending(files: list[str]) -> None:
    current = load_pending()
    known = set(current)
    fresh = [f for f in files if f not in known]
    if fresh:
        save_pending(current + fresh)


def display_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def local_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S%z") if value else "从未"


def human_delta(delta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


# ───────────────────────── 子命令 ─────────────────────────

def cmd_run(args) -> int:
    sources = load_config()
    selected = args.only or list(sources)
    for name in selected:
        if name not in sources:
            raise ConfigError(f"没有这个源：{name}（可选：{', '.join(sources)}）")

    last = load_last()
    results = []
    for name in selected:
        cfg = sources[name]
        ctx = Ctx(name=name, config=cfg, outdir=OUTPUT_DIR / name,
                  last_success=parse_time(last.get(name)), force=args.force)
        started = datetime.now().astimezone()
        due = ctx.due()
        try:
            produced = MODULES[cfg["type"]].run(ctx) or []
            files = [Path(p) for p in produced]
            kept = []
            for path in files:
                if not path.exists():
                    print(f"[warn] {name}: 模块返回了不存在的文件 {path}", file=sys.stderr)
                    continue
                if path.stat().st_size == 0:
                    print(f"[warn] {name}: 忽略空文件 {path}", file=sys.stderr)
                    continue
                kept.append(path)
            error = None
        except Exception as exc:  # noqa: BLE001 一个源失败不能拖垮其它源
            kept, error = [], f"{type(exc).__name__}: {exc}"

        if kept:
            last[name] = started.isoformat(timespec="seconds")
            save_last(last)          # 逐源落盘：中途崩了不丢前面源的成功时间
            add_pending([display_path(path) for path in kept])
            status = "ok"
        elif error:
            status = "error"
        elif due:
            status = "empty"
        else:
            status = "skip"

        results.append({
            "name": name,
            "type": cfg["type"],
            "every": str(cfg["every"]),
            "due": due,
            "status": status,
            "last_success": local_time(ctx.last_success),
            "outputs": [display_path(p) for p in kept],
            "error": error,
        })

    pending = load_pending()
    if args.json:
        print(json.dumps({"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                          "sources": results, "pending": pending},
                         ensure_ascii=False, indent=1))
    else:
        _print_human(results, pending)

    return 1 if any(r["status"] == "error" for r in results) else 0


def _print_human(results: list[dict], pending: list[str]) -> None:
    produced = [r for r in results if r["outputs"]]
    if not produced and not any(r["status"] == "error" for r in results):
        print("无新条目")
        if pending:
            print(f"待摘要 {len(pending)} 个文件（pending.json）")
        return
    for row in results:
        head = f"{row['name']}  ({row['type']}, every {row['every']})  上次成功 {row['last_success']}"
        if row["status"] == "ok":
            print(head)
            print(f"  抓取 {len(row['outputs'])} 个文件：")
            for path in row["outputs"]:
                print(f"    {path}")
        elif row["status"] == "skip":
            print(f"{head}  跳过（未到期）")
        elif row["status"] == "empty":
            print(f"{head}  无产出")
        else:
            print(f"{head}  失败：{row['error']}", file=sys.stderr)
    if pending:
        print(f"待摘要 {len(pending)} 个文件（pending.json）")


def cmd_list(args) -> int:
    sources = load_config()
    last = load_last()
    for name, cfg in sources.items():
        succeeded = parse_time(last.get(name))
        ctx = Ctx(name=name, config=cfg, outdir=OUTPUT_DIR / name, last_success=succeeded)
        due_at = ctx.due_at()
        if due_at is None:
            when = "首次运行即到期"
        elif ctx.due():
            when = f"已到期（{local_time(due_at)} 起可跑）"
        else:
            when = f"下次到期 {local_time(due_at)}（还剩 {human_delta(due_at - ctx.now)}）"
        state_file = OUTPUT_DIR / name / "state.json"
        print(f"- {name}  ({cfg['type']}, every {cfg['every']})  上次成功 {local_time(succeeded)}")
        print(f"    {when}")
        print(f"    输出目录 {OUTPUT_DIR / name}"
              f"{'' if state_file.exists() else '（还没有状态文件）'}")
    return 0


def cmd_pending(args) -> int:
    files = load_pending()
    if args.clear:
        if files:
            save_pending([])
            print(f"已清空 {len(files)} 个待摘要文件")
        else:
            print("没有待摘要文件")
        return 0
    if args.json:
        print(json.dumps(files, ensure_ascii=False, indent=1))
    elif files:
        for path in files:
            print(f"- {path}")
    else:
        print("没有待摘要文件")
    return 0


def cmd_reset(args) -> int:
    sources = load_config()
    if args.name not in sources:
        raise ConfigError(f"没有这个源：{args.name}（可选：{', '.join(sources)}）")
    state_file = OUTPUT_DIR / args.name / "state.json"
    if state_file.exists():
        state_file.unlink()
        print(f"已清空 {args.name} 的去重状态（{state_file.relative_to(ROOT)}）")
    else:
        print(f"{args.name} 没有去重状态，无需清空")
    print("产物文件与 last_success.json 不在清理范围内；要重跑用 run --only "
          f"{args.name} --force")
    return 0


MODULE_CLI = {"x": "x", "wx": "wechat", "yt": "youtube"}   # 子命令 → 模块


def main(argv: list[str] | None = None) -> int:
    # x / wx 的参数原样交给模块自己的 CLI；argparse 的 REMAINDER 带子命令时吃不住以
    # `--` 开头的选项，所以在解析前分流。
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in MODULE_CLI:
        type_name = MODULE_CLI[argv[0]]
        module = MODULES[type_name]
        if not hasattr(module, "main"):
            print(f"{argv[0]} 模块没有临时读取入口", file=sys.stderr)
            return 2
        # 子命令拿对应配置小节当默认值；命令行参数优先，配置读不了就只用命令行。
        section: dict = {}
        try:
            section = next((cfg for cfg in load_config().values()
                            if cfg.get("type") == type_name), {})
        except ConfigError as error:
            print(f"[warn] 配置读不了，只用命令行参数：{error}", file=sys.stderr)
        return module.main(argv[1:], config=section)

    ap = argparse.ArgumentParser(
        description="按 config.toml 逐个信息源抓取新内容",
        epilog="临时读取（不落盘、不动游标）：source_watch.py x … / wx … / yt …")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="运行信息源（可只跑一个）")
    p.add_argument("--only", action="append", metavar="NAME",
                   help="只运行这个源，可重复；默认全部")
    p.add_argument("--force", action="store_true", help="忽略更新频率，本轮必跑")
    p.add_argument("--json", action="store_true", help="机器可读输出（含每个源的产物列表）")
    p.set_defaults(func=cmd_run)

    sub.add_parser("list", help="列出源、频率与下次到期时间").set_defaults(func=cmd_list)

    p = sub.add_parser("pending", help="查看/清空待摘要的文件")
    p.add_argument("--clear", action="store_true", help="清空（摘要写完并发出后再调）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("reset", help="清掉某个源的去重状态")
    p.add_argument("name")
    p.set_defaults(func=cmd_reset)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
