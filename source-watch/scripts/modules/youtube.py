"""YouTube 频道：RSS 列条目 → 取元数据 → 转写。

RSS 是快路径（最近 15 条，带 published）；被限流时回落 yt-dlp 列频道页。
产物：`meta/<id>.json`、`transcripts/<date>-<id>.txt`；约 60 分钟以上的内容只落 meta。

临时列一下最新条目（不落盘、不动游标）：

    cd scripts && python3 -m modules.youtube --channel @handle --limit 5 --format md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .base import Ctx, now_iso, refresh_seen

YTDLP = os.environ.get("SOURCEWATCH_YTDLP", "/home/yangyu/.local/bin/yt-dlp")
TRANSCRIBE = os.environ.get("SOURCEWATCH_TRANSCRIBE", "/home/yangyu/.local/bin/transcribe")
DEFAULT_PROXY = "http://127.0.0.1:7890"
LIST_LIMIT = 15
DEFAULT_BUDGET = 2400
DEFAULT_LONG_MEDIA = 3600
ASR_RATE = 3.8  # 实测：whisper-small / CPU ≈ 3.8× 实时（含下载）


def proxy() -> str | None:
    return os.environ.get("SOURCEWATCH_PROXY", DEFAULT_PROXY) or None


def child_env() -> dict:
    env = dict(os.environ)
    p = proxy()
    if p:
        env.update({"HTTPS_PROXY": p, "HTTP_PROXY": p, "ALL_PROXY": p})
    return env


def opener() -> urllib.request.OpenerDirector:
    handlers = []
    p = proxy()
    if p:
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    handlers.append(urllib.request.HTTPRedirectHandler())
    # 不设置 User-Agent：feeds 端点对显式 UA（含浏览器 UA）返回 404/500。
    return urllib.request.build_opener(*handlers)


def fetch_text(url: str, timeout: int = 30, retries: int = 5) -> str:
    """取页面/feed；RSS 端会间歇性 404/500，重试 + 退避可稳定。"""
    last = None
    for attempt in range(retries):
        try:
            with opener().open(url, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if getattr(exc, "code", None) not in (404, 429, 500, 502, 503):
                break
            time.sleep(min(1.5 * (2 ** attempt), 8))
    raise RuntimeError(f"请求失败（已重试 {retries} 次）：{url} → {last}")


def channel_id(target: str) -> str:
    """接受频道 ID（UC…）或 @handle / 频道 URL。"""
    target = target.strip()
    direct = re.search(r"(UC[0-9A-Za-z_-]{22})", target)
    if direct:
        return direct.group(1)
    url = target if target.startswith("http") else "https://www.youtube.com/" + (
        target if target.startswith("@") else "@" + target)
    html = fetch_text(url)
    m = (re.search(r'"channelId":"(UC[0-9A-Za-z_-]{22})"', html)
         or re.search(r"channel/(UC[0-9A-Za-z_-]{22})", html))
    if not m:
        raise RuntimeError(f"解析不出频道 ID：{target}")
    return m.group(1)


def list_items(uid: str, limit: int = LIST_LIMIT) -> list[dict]:
    """该频道最近的条目，新的在前；RSS 失败回落 yt-dlp。"""
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    try:
        xml = fetch_text(f"https://www.youtube.com/feeds/videos.xml?channel_id={uid}", retries=3)
    except Exception:  # noqa: BLE001  RSS 被限流
        return via_ytdlp(uid, limit)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return via_ytdlp(uid, limit)
    out = []
    for entry in root.findall("a:entry", ns):
        vid = entry.findtext("yt:videoId", default="", namespaces=ns)
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": (entry.findtext("a:title", default="", namespaces=ns) or "").strip(),
            "published": entry.findtext("a:published", default="", namespaces=ns),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out[:limit]


def via_ytdlp(uid: str, limit: int) -> list[dict]:
    out = subprocess.run(
        [YTDLP, "--flat-playlist", "--no-warnings", "--playlist-end", str(limit),
         "--print", "%(id)s\t%(title)s", f"https://www.youtube.com/channel/{uid}/videos"],
        capture_output=True, text=True, timeout=180, env=child_env(),
    )
    if out.returncode != 0:
        tail = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else str(out.returncode)
        raise RuntimeError(f"yt-dlp 列频道失败：{tail}")
    items = []
    for line in out.stdout.splitlines():
        if "\t" not in line:
            continue
        vid, title = (s.strip() for s in line.split("\t", 1))
        if vid:
            items.append({"id": vid, "title": title, "published": "",
                          "url": f"https://www.youtube.com/watch?v={vid}"})
    return items


def item_meta(url: str) -> dict:
    out = subprocess.run(
        [YTDLP, "--skip-download", "--no-warnings",
         "--print", "%(duration)s\t%(upload_date)s\t%(description)s", url],
        capture_output=True, text=True, timeout=120, env=child_env(),
    )
    raw = out.stdout.split("\t", 2)
    dur = int(float(raw[0])) if raw and raw[0].strip() not in ("", "NA") else 0
    upload = raw[1].strip() if len(raw) > 1 else ""
    return {
        "duration": dur,
        "upload_date": "" if upload in ("", "NA") else upload,
        "description": (raw[2] if len(raw) > 2 else "").strip(),
    }


def pick_engine(title: str, desc: str) -> str:
    return "sensevoice" if len(re.findall(r"[\u4e00-\u9fff]", f"{title}{desc}")) >= 8 else "whisper"


def estimate_seconds(duration: int) -> int:
    """估算一条「取元数据 → 转写落盘」的墙钟秒数。

    末项是转写之外的固定开销（yt-dlp 取元数据 / 下音频 / ffmpeg 转码）：实测 1955s 的视频
    整条用 701s，其中转写 515s，差 186s。宁可估多不可估少，预算才拦得住。
    """
    if not duration:
        return 300
    return int(duration / ASR_RATE) + 180


def has_transcript(outdir: Path, iid: str) -> bool:
    """transcribe 只在整段成功后写文件，本模块也只在 rc==0 时改名 ⇒ 文件存在即完成。"""
    return any((outdir / "transcripts").glob(f"????-??-??-{iid}.txt"))


def transcribe(item: dict, ctx: Ctx) -> Path | None:
    """跑 transcribe 并把稿子挪到 transcripts/<date>-<id>.txt；失败返回 None。"""
    engine = pick_engine(item.get("title", ""), item.get("description", ""))
    work = ctx.outdir / "transcripts" / f".work-{item['id']}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        rc = subprocess.run([TRANSCRIBE, item["url"], "--engine", engine, "--out", str(work)],
                            capture_output=True, text=True, env=child_env()).returncode
        produced = sorted(work.glob("*.txt"))
        if rc != 0 or not produced:
            return None
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = ctx.outdir / "transcripts" / f"{day}-{item['id']}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        produced[0].replace(target)
        return target
    finally:
        for junk in work.glob("*"):
            junk.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass


def channels_of(config: dict) -> list[str]:
    return [str(c).strip() for c in (config.get("channels") or []) if str(c).strip()]


def collect_items(channels: list[str], limit: int) -> list[dict]:
    """逐个频道列条目；单个频道失败只告警，全部失败才让整个源报错。"""
    items, failures = [], []
    for target in channels:
        try:
            uid = channel_id(target)
            for item in list_items(uid, limit):
                items.append({**item, "channel": target, "uid": uid})
        except Exception as error:  # noqa: BLE001 一个频道被限流不该放弃其它频道
            failures.append(f"{target}: {type(error).__name__}: {error}")
            print(f"[warn] {target}: 列条目失败 {error}", file=sys.stderr)
    if len(failures) == len(channels):
        raise RuntimeError("所有频道都列不出条目 → " + "；".join(failures))
    return items


def run(ctx: Ctx) -> list[Path]:
    if not ctx.due():
        return []

    channels = channels_of(ctx.config)
    if not channels:
        raise RuntimeError("缺少 channels（频道 ID 或 @handle 列表）")
    limit = int(ctx.config.get("limit", LIST_LIMIT))
    long_media = int(ctx.config.get("long_media_seconds", DEFAULT_LONG_MEDIA))
    budget_cfg = ctx.config.get("budget_seconds", DEFAULT_BUDGET)
    budget = None if budget_cfg in (None, 0) else int(budget_cfg)

    items = collect_items(channels, limit)
    state = ctx.state()
    refresh_seen(ctx, state)
    state.setdefault("queue", [])
    state.setdefault("pending_long", {})

    # 首次见到这个源：把现有条目标记为已读，不回头处理历史。
    if not state.get("primed"):
        for item in items:
            if item.get("id"):
                state["seen"][item["id"]] = item.get("published") or now_iso()
        state["primed"] = True
        ctx.save_state(state)
        print(f"[prime] {ctx.name}: 把 {len(channels)} 个频道的当前 {len(items)} 条标记为已读",
              file=sys.stderr)
        return []

    seen = set(state["seen"])
    # 队列跨轮保留：本轮没跑完（预算/失败）的条目不会因为掉出最近 15 条而丢失。
    queue = {i["id"]: i for i in state["queue"] if i.get("id")}
    for item in items:
        if item.get("id") and item["id"] not in seen:
            queue.setdefault(item["id"], item)
    state["queue"] = list(queue.values())
    ctx.save_state(state)
    if not queue:
        return []

    written: list[Path] = []
    started = time.monotonic()
    for item in list(queue.values()):
        iid = item["id"]
        try:
            meta = item_meta(item["url"])
            item = {**item, **meta}
            path = ctx.write_json(f"meta/{iid}.json", item)
            if path:
                written.append(path)

            duration = int(meta.get("duration") or 0)
            if duration and duration > long_media:
                state["pending_long"][iid] = {
                    "title": item.get("title", ""), "duration": duration,
                    "url": item["url"], "reason": f"> {long_media // 60}min，按策略只留元数据",
                }
                print(f"- {iid} 长内容 {duration // 60}min → 只落元数据", flush=True)
            elif has_transcript(ctx.outdir, iid):
                print(f"- {iid} 已有转写稿 → 跳过转写", flush=True)
            else:
                if budget is not None:
                    remain = budget - (time.monotonic() - started)
                    need = estimate_seconds(duration)
                    if need > remain:
                        print(f"- {iid} 预算只剩 {remain / 60:.1f}min < 估算 {need / 60:.1f}min "
                              f"→ 留待下一轮", flush=True)
                        continue  # 留在队列里，下轮继续
                print(f"- {iid} {item.get('title', '')[:50]} → 转写…", flush=True)
                transcript = transcribe(item, ctx)
                if transcript is None:
                    # 不记已读 ⇒ 下轮还是候选；一次网络抖动不该永久丢掉条目。
                    print(f"- {iid} 转写失败 → 不记已读，下轮重试", flush=True)
                    continue
                written.append(transcript)
                print(f"  ✓ {transcript.name}", flush=True)
        except Exception as exc:  # noqa: BLE001 单条异常不该把整轮拖挂
            print(f"- {iid} 处理异常 → 保留在队列：{type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        queue.pop(iid, None)
        if iid not in seen:
            state["seen"][iid] = item.get("published") or now_iso()
            seen.add(iid)
        state["queue"] = [q for q in state["queue"] if q["id"] != iid]
        ctx.save_state(state)  # 逐条落盘：被硬中断也不丢已完成进度
    print(f"处理 {len(written)} 个文件；队列剩 {len(state['queue'])} 条；"
          f"长内容挂起 {len(state['pending_long'])} 条", file=sys.stderr)
    return written


def render_markdown(sections: list[tuple[str, str, list[dict]]]) -> str:
    lines = []
    for target, uid, items in sections:
        label = uid if target == uid else f"{target} ({uid})"
        lines += [f"# {label}", ""]
        for item in items:
            when = f" · {item['published'][:10]}" if item.get("published") else ""
            lines.append(f"- {item.get('title') or '(无标题)'}{when}")
            lines.append(f"  {item.get('url')}")
            if item.get("duration"):
                lines.append(f"  时长 {item['duration'] // 60}min · 发布 {item.get('upload_date') or '?'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None, config: dict | None = None) -> int:
    config = config or {}
    ap = argparse.ArgumentParser(description="列出 YouTube 频道的最新条目（不落盘，供临时查看）")
    ap.add_argument("--channel", action="append", metavar="TARGET",
                    help="频道 ID / @handle / 频道 URL；可重复（默认取配置 channels）")
    ap.add_argument("--limit", type=int, default=None,
                    help=f"每个频道列几条（默认取配置，否则 {LIST_LIMIT}）")
    ap.add_argument("--meta", action="store_true",
                    help="额外取时长/发布时间/简介（每条一次 yt-dlp，较慢）")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args(argv)

    # 优先级：命令行 > 配置文件 > 内置默认。
    channels = ([str(c).strip() for c in args.channel if str(c).strip()]
                if args.channel is not None
                else [str(c).strip() for c in (config.get("channels") or []) if str(c).strip()])
    if not channels:
        raise RuntimeError("没有频道：用 --channel 指定，或在配置里配 channels")
    limit = args.limit if args.limit is not None else int(config.get("limit", LIST_LIMIT))

    sections = []
    for target in channels:
        uid = channel_id(target)
        items = list_items(uid, limit)
        if args.meta:
            for item in items:
                item.update(item_meta(item["url"]))
        sections.append((target, uid, items))

    if args.format == "md":
        print(render_markdown(sections))
    elif len(sections) == 1:
        print(json.dumps(sections[0][2], ensure_ascii=False, indent=1))
    else:
        print(json.dumps([{"channel": target, "uid": uid, "items": items}
                          for target, uid, items in sections],
                         ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        sys.exit(f"yt: {error}")
