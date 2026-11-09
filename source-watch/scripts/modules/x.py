"""X 时间线：通过 shuttle 驱动 Firefox，读 Home / Following 的帖子，按 status id 去重后落盘。

产物：`timeline/<date>-<HHMMSS>.md`，一次运行一个文件，只有本轮确有新帖才写。

临时读一份（不落盘、不动游标）：

    cd scripts && python3 -m modules.x --tab Following --screens 2 --format md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .base import Ctx, desk

CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")
X_TAB = re.compile(r"/ X$")          # Firefox 的标签标题以站点名结尾
TAB = re.compile(r'^- tab "(.*)" \[id=([^\]]+)\]$')
STATUS = re.compile(r"/(?:i/web/)?status/(\d+)(?:[?#].*)?$")
START_URL = "https://x.com/home"
HANDLE = re.compile(r"@([A-Za-z0-9_]{1,15})")
TIME = re.compile(
    r"(Just now|"
    r"\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\s*(?:ago)?|"
    r"\d+\s*[smhd]\b(?:\s*ago)?|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}|"
    r"\d{1,2}:\d{2}\s*(?:AM|PM))"
)
REPLY_TO = re.compile(r"^Replying to ((?:@[A-Za-z0-9_]{1,15}\s*)+)")
ENGAGEMENT_TAIL = re.compile(
    r"((?:\d[\d,]*\s*(?:repl(?:y|ies)|reposts?|likes?|bookmarks?|views?|quotes?)"
    r"(?:,\s*)?)+)$"
)
COUNT = re.compile(r"(\d[\d,]*)\s*(repl(?:y|ies)|reposts?|likes?|bookmarks?|views?|quotes?)")
NAME_NOISE = re.compile(r"\s*Provides details about protected accounts\.?", re.I)
VISIBLE_URL = re.compile(r"(?:https?://|(?<![\w@.])(?:x|twitter)\.com/)[^\s\u2026]+")
HOME_TITLES = ("Home / X", "首页 / X")
SEEN_CAP = 5000


# ───────────────────────── Firefox / 时间线 ─────────────────────────

def firefox_windows(needle=None):
    found = []
    for win in desk("windows"):
        if "firefox" not in str(win.get("app", "")).lower():
            continue
        title = win.get("title") or ""
        if needle and needle.lower() not in title.lower():
            continue
        found.append(win)
    return found


def is_x_tab(name):
    return bool(name) and bool(X_TAB.search(name))


def active_tab_is(name, title):
    return title == f"{name} — Mozilla Firefox"


def firefox_running():
    """进程活着但还没窗口时启动会开出第二个实例。"""
    if shutil.which("docker") and not os.path.exists("/.dockerenv"):
        proc = subprocess.run(["docker", "exec", CONTAINER, "pgrep", "-x", "firefox-bin"],
                              capture_output=True, text=True)
        return proc.returncode == 0
    return subprocess.run(["pgrep", "-x", "firefox-bin"], capture_output=True).returncode == 0


def tabs_of(title):
    snap = desk("snapshot", f"window={title}", "text=true", "max_chars=12000")
    tabs = []
    for line in (snap.get("text") or "").splitlines():
        match = TAB.match(line.strip())
        if match:
            tabs.append((match.group(1), match.group(2)))
    return tabs


def current_window(needle=None, attempts=40):
    for _ in range(attempts):
        wins = firefox_windows(needle)
        if wins:
            return wins[0]["title"]
        time.sleep(0.5)
    raise RuntimeError("no Firefox window")


def wait_tab_active(name, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = firefox_windows()
        if win and active_tab_is(name, win[0].get("title") or ""):
            return True
        time.sleep(0.5)
    return False


def ensure_firefox(needle=None, timeout=60):
    wins = firefox_windows(needle)
    if wins:
        return wins[0]["title"]

    deadline = time.time() + timeout
    if firefox_running():
        while time.time() < deadline:
            wins = firefox_windows(needle)
            if wins:
                return wins[0]["title"]
            time.sleep(1)
        raise RuntimeError("Firefox is running but has no window")

    desk("launch", "app=firefox", f'args=["{START_URL}"]')
    while time.time() < deadline:
        wins = firefox_windows(needle)
        if wins:
            time.sleep(5)      # 等页面加载完，ensure_x_tab 才能判断标签
            return wins[0]["title"]
        time.sleep(1)
    raise RuntimeError("Firefox did not start")


def ensure_x_tab(title, timeout=40):
    """让一个 x.com 标签成为当前标签；一个都没有就开一个。"""
    title = current_window()
    for name, node in tabs_of(title):
        if not is_x_tab(name):
            continue
        if active_tab_is(name, title):
            return title
        title = current_window()
        desk("focus", f"window={title}")
        desk("act", f"id={node}", "action=click")
        if wait_tab_active(name):
            return current_window()
        print(f"warning: could not switch to tab {name!r}", file=sys.stderr)
        return title

    print(f"no x.com tab, opening {START_URL}", file=sys.stderr)
    title = current_window()
    desk("focus", f"window={title}")
    desk("key", f"window={title}", "keys=ctrl+t")
    time.sleep(1.5)
    title = current_window()
    desk("type", f"window={title}", f"text={START_URL}")
    desk("key", f"window={title}", "keys=Return")
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = current_window()
        for name, node in tabs_of(title):
            if is_x_tab(name):
                if not active_tab_is(name, title):
                    desk("act", f"id={node}", "action=click")
                    wait_tab_active(name)
                return current_window()
        time.sleep(1)
    raise RuntimeError(f"no x.com tab after opening {START_URL}")


def click_home(title, timeout=30):
    """用站内快捷键回 Home，失败再点 Home 链接。"""
    before = title
    for name in ("Home", "首页"):
        title = current_window()
        desk("focus", f"window={title}")
        desk("key", f"window={title}", "keys=g")
        time.sleep(0.3)
        desk("key", f"window={title}", "keys=h")
        time.sleep(2)
        title = current_window()
        if any(title.startswith(t) for t in HOME_TITLES):
            return title
        try:
            found = desk("find", f"name={name}").get("found") or []
        except RuntimeError:
            found = []
        for node in found:
            if node.get("role") == "link":
                desk("act", f"id={node['id']}", "action=click")
                time.sleep(3)
                title = current_window()
                if any(title.startswith(t) for t in HOME_TITLES):
                    return title
                break
    print(f"warning: could not reach Home (title={title!r}, was {before!r})", file=sys.stderr)
    return title


def open_url(title, url, timeout=30):
    """在已聚焦的窗口里用地址栏打开一个 x.com 页面，等帖子出现。"""
    title = current_window()
    desk("focus", f"window={title}")
    desk("key", f"window={title}", "keys=ctrl+l")
    time.sleep(0.5)
    desk("type", f"window={title}", f"text={url}")
    desk("key", f"window={title}", "keys=Return")
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = current_window()
        try:
            if posts_in(snapshot(title)["nodes"]):
                return title
        except (RuntimeError, KeyError):
            pass
        time.sleep(1)
    raise RuntimeError(f"页面没有加载出帖子：{url}")


def snapshot(title, chars=200000):
    """一次结构化读取：每个节点都带链接目标 URL。"""
    return desk("snapshot", f"window={title}", "urls=true", f"max_chars={chars}")


def posts_in(nodes):
    """按文档顺序返回每个帖子的 (节点 id, 可访问名, permalink)。"""
    children = {}
    for node in nodes:
        children.setdefault(node.get("parent", -1), []).append(node)

    found = []
    for node in nodes:
        if node.get("role") != "article":
            continue
        url = None
        stack = list(children.get(node["ref"], []))
        while stack and url is None:
            current = stack.pop()
            if current.get("role") == "link":
                target = current.get("url") or ""
                if STATUS.search(target):
                    url = target
                    continue
            if current.get("role") != "article":      # 引用帖有自己的链接
                stack.extend(children.get(current["ref"], []))
        found.append((node.get("id"), node.get("name") or "", url))
    return found


def wait_for_posts(title, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if posts_in(snapshot(title)["nodes"]):
                return True
        except (RuntimeError, KeyError):
            pass
        time.sleep(1)
    return False


def first_post(title):
    posts = posts_in(snapshot(title)["nodes"])
    return posts[0][1] if posts else None


def select_tab(title, tab, timeout=20):
    """选页内时间线标签（如 Following）；标题不变，靠选中态和首帖变化确认。"""
    if not tab:
        return
    desk("key", f"window={title}", "keys=ctrl+Home")
    time.sleep(1.5)
    before = first_post(title)
    deadline = time.time() + timeout
    while True:
        try:
            found = desk("find", f"name={tab}").get("found") or []
        except RuntimeError:
            found = []
        for node in found:
            if node.get("role") != "tab":
                continue
            if "selected" in (node.get("states") or []):
                return
            try:
                desk("act", f"id={node['id']}", "action=click")
            except RuntimeError:
                desk("press", f"id={node['id']}")
            switch_deadline = time.time() + 15
            while time.time() < switch_deadline:
                try:
                    again = desk("find", f"name={tab}").get("found") or []
                except RuntimeError:
                    again = []
                if any("selected" in (n.get("states") or []) for n in again):
                    time.sleep(1.5)
                    return
                now = first_post(title)
                if now and now != before:
                    time.sleep(1.5)
                    return
                time.sleep(1)
            print(f"warning: tab {tab!r} did not change the timeline", file=sys.stderr)
            return
        if time.time() >= deadline:
            print(f"warning: no tab named {tab!r}", file=sys.stderr)
            return
        time.sleep(1)


def collect(title, screens, pause=3.5):
    """滚动读取时间线，每条只留一次并带上 permalink。"""
    seen = {}
    for index in range(screens):
        if index == 0:
            wait_for_posts(title)
        for node_id, name, url in posts_in(snapshot(title)["nodes"]):
            if node_id in seen:
                if url and not seen[node_id]["url"]:
                    seen[node_id]["url"] = url
            else:
                seen[node_id] = {"name": name, "url": url}
        if index < screens - 1:
            desk("scroll", "direction=down", "amount=8", f"window={title}")
            time.sleep(pause)
    return list(seen.values())


def parse(raw):
    """把可访问名拆成字段；原始串始终保留。"""
    post = {}
    rest = raw

    engagement = {}
    tail = ENGAGEMENT_TAIL.search(rest)
    if tail:
        for value, kind in COUNT.findall(tail.group(1)):
            if kind.startswith("repl"):
                key = "replies"
            elif kind.startswith("repost"):
                key = "reposts"
            elif kind.startswith("like"):
                key = "likes"
            elif kind.startswith("bookmark"):
                key = "bookmarks"
            elif kind.startswith("view"):
                key = "views"
            else:
                key = "quotes"
            engagement[key] = int(value.replace(",", ""))
        rest = rest[: tail.start()].rstrip()
    if engagement:
        post["engagement"] = engagement

    handles = HANDLE.findall(rest)
    if handles:
        post["handle"] = handles[0]

    if " reposted " in rest:
        reposter, rest = rest.split(" reposted ", 1)
        post["reposted_by"] = reposter.strip()
        rest = rest.lstrip()

    times = TIME.findall(rest)
    if times:
        post["time"] = times[0].strip()
        rest = rest.replace(post["time"], "", 1)

    if post.get("handle"):
        head = f"@{post['handle']}"
        if head in rest:
            author, rest = rest.split(head, 1)
            author = NAME_NOISE.sub("", author.replace("Verified account", ""))
            post["author"] = " ".join(author.split())
            rest = rest.lstrip()

    reply = REPLY_TO.match(rest)
    if reply:
        post["reply_to"] = " ".join(reply.group(1).split())
        rest = rest[reply.end():].lstrip()

    if rest:
        if " Quote " in rest:
            text, quote = rest.split(" Quote ", 1)
            post["text"] = text.strip()
            post["quote"] = quote.strip()
        else:
            post["text"] = rest.strip()

    # 纯图片/视频帖没有正文，保留可访问名，别静默丢内容。
    post.setdefault("text", "")
    haystack = post["text"] + " " + post.get("quote", "")
    urls = [u.rstrip(".,;:)]}") for u in VISIBLE_URL.findall(haystack)]
    if urls:
        post["urls"] = urls
    if post.get("handle"):
        post["profile"] = f"https://x.com/{post['handle']}"
    if not post["text"]:
        post["raw"] = raw
    return post


def handle_of(value: str) -> str:
    """`@foo`、`foo`、`https://x.com/foo` 都归一成 `foo`。"""
    text = str(value).strip()
    match = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", text)
    if match:
        return match.group(1)
    return text.lstrip("@")


def parse_items(items: list[dict]) -> list[dict]:
    return [parse(item["name"]) | ({"url": item["url"]} if item.get("url") else {})
            for item in items]


def read_home(tab: str = "", screens: int = 6, window: str | None = None) -> tuple[str, list[dict]]:
    """打开（必要时启动）Firefox，回 Home，切到指定页内标签并读取帖子。"""
    title = ensure_firefox(window)
    title = ensure_x_tab(title)
    title = click_home(title)
    select_tab(title, tab)
    title = firefox_windows()[0]["title"]
    return title, parse_items(collect(title, screens))


def read_account(handle: str, screens: int = 6, window: str | None = None) -> tuple[str, list[dict]]:
    """打开某账号的主页时间线并读取帖子。"""
    title = ensure_firefox(window)
    title = ensure_x_tab(title)
    title = open_url(title, f"https://x.com/{handle_of(handle)}")
    return title, parse_items(collect(title, screens))


# ───────────────────────── 去重与落盘 ─────────────────────────

def post_id(post: dict) -> str:
    """permalink 里的状态 ID 最稳；没有 permalink 的帖子用内容哈希兜底。"""
    match = re.search(r"/status/(\d+)", post.get("url") or "")
    if match:
        return match.group(1)
    seed = "\n".join(str(post.get(key, "")) for key in ("handle", "author", "text", "quote"))
    return "h" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def render_post(post: dict) -> list[str]:
    who = post.get("author") or "(unknown)"
    handle = f" (@{post['handle']})" if post.get("handle") else ""
    when = f" · {post['time']}" if post.get("time") else ""
    lines = [f"### {who}{handle}{when}"]
    if post.get("reposted_by"):
        lines.append(f"- 转发自：{post['reposted_by']}")
    text = (post.get("text") or "").strip()
    if text:
        lines += ["", text]
    if post.get("quote"):
        lines += ["", f"> 引用：{post['quote']}"]
    if post.get("url"):
        lines += ["", f"- 链接：{post['url']}"]
    for extra in post.get("urls") or []:
        lines.append(f"- 文中链接：{extra if extra.startswith('http') else 'https://' + extra}")
    engagement = post.get("engagement") or {}
    if engagement:
        lines.append("- 互动：" + "，".join(f"{k} {v:,}" for k, v in engagement.items()))
    if not text and post.get("raw"):
        lines += ["", f"- 原文（无正文）：{post['raw']}"]
    lines.append("")
    return lines


def render_sections(title: str, sections: list[tuple[str, list[dict]]]) -> str:
    lines = [f"# {title}", ""]
    for label, posts in sections:
        lines += [f"## {label}", ""]
        for post in posts:
            lines += render_post(post)
    return "\n".join(lines).rstrip() + "\n"


def sections_of(config: dict) -> list[tuple[str, str]]:
    """配置里的 tabs / accounts 展开成 [(kind, value)]；两个都空就读 Home 当前那一栏。"""
    tabs = [str(t).strip() for t in (config.get("tabs") or []) if str(t).strip()]
    accounts = [str(a).strip() for a in (config.get("accounts") or []) if str(a).strip()]
    plan = [("tab", tab) for tab in tabs] + [("account", account) for account in accounts]
    return plan or [("tab", "")]


def run(ctx: Ctx) -> list[Path]:
    if not ctx.due():
        return []

    screens = int(ctx.config.get("screens", 6))
    window = ctx.config.get("window")
    sections: list[tuple[str, list[dict]]] = []
    for kind, value in sections_of(ctx.config):
        if kind == "tab":
            _, posts = read_home(value, screens, window)
            sections.append((f"Home · {value}" if value else "Home", posts))
        else:
            _, posts = read_account(value, screens, window)
            sections.append((f"@{handle_of(value)}", posts))

    unique: dict[str, dict] = {}
    for label, posts in sections:
        for post in posts:
            unique.setdefault(post_id(post), {**post, "section": label})

    state = ctx.state()
    state.setdefault("seen", [])
    state.setdefault("primed", False)
    if not state["primed"]:
        state["seen"] = (state["seen"] + [i for i in unique if i not in set(state["seen"])])[-SEEN_CAP:]
        state["primed"] = True
        ctx.save_state(state)
        print(f"[prime] {ctx.name}: 把当前 {len(unique)} 条帖子标记为已读", file=sys.stderr)
        return []

    seen = set(state["seen"])
    fresh = {i: post for i, post in unique.items() if i not in seen}
    if not fresh:
        return []

    grouped: dict[str, list[dict]] = {}
    for iid, post in unique.items():
        if iid in fresh:
            grouped.setdefault(post["section"], []).append(post)

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    output = ctx.write(f"timeline/{stamp}.md", render_sections(ctx.name, list(grouped.items())))
    if output is None:
        return []

    state["seen"] = (state["seen"] + list(fresh))[-SEEN_CAP:]
    ctx.save_state(state)
    print(f"- 新增 {len(fresh)} 条帖子 → {output.name}", file=sys.stderr)
    return [output]


def main(argv: list[str] | None = None, config: dict | None = None) -> int:
    config = config or {}
    ap = argparse.ArgumentParser(description="读 X 时间线（不落盘，供临时查看）")
    ap.add_argument("--tab", action="append", metavar="NAME",
                    help="Home 的页内标签，如 Following；可重复（默认取配置 tabs）")
    ap.add_argument("--account", action="append", metavar="HANDLE",
                    help="账号时间线，如 @foo；可重复（默认取配置 accounts）")
    ap.add_argument("--screens", type=int, default=None,
                    help=f"每屏滚动次数（默认取配置，否则 {6}）")
    ap.add_argument("--window", help="按标题子串挑 Firefox 窗口")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args(argv)

    # 优先级：命令行 > 配置文件 > 内置默认。
    tabs = ([t.strip() for t in args.tab if t.strip()] if args.tab is not None
            else [str(t).strip() for t in (config.get("tabs") or []) if str(t).strip()])
    accounts = ([a.strip() for a in args.account if a.strip()] if args.account is not None
                else [str(a).strip() for a in (config.get("accounts") or []) if str(a).strip()])
    screens = args.screens if args.screens is not None else int(config.get("screens", 6))
    window = args.window if args.window is not None else config.get("window")
    plan = [("tab", t) for t in tabs] + [("account", a) for a in accounts] or [("tab", "")]
    sections = []
    for kind, value in plan:
        if kind == "tab":
            _, posts = read_home(value, screens, window)
            sections.append((f"Home · {value}" if value else "Home", posts))
        else:
            _, posts = read_account(value, screens, window)
            sections.append((f"@{handle_of(value)}", posts))

    if args.format == "md":
        print(render_sections("X", sections))
    elif len(sections) == 1:
        print(json.dumps(sections[0][1], ensure_ascii=False, indent=1))
    else:
        print(json.dumps([{"source": label, "posts": posts} for label, posts in sections],
                         ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        sys.exit(f"x: {error}")
