"""X 时间线：每轮开一个全新的 Firefox 窗口，读 Home / Following 的帖子，按 status id 去重后落盘。

一次运行 = 一个窗口一个标签：`--new-window` 打开 x.com/home，抓完 `ctrl+w` 关掉标签（只剩一个
标签时窗口随之关闭）。产物：`timeline/<date>-<HHMMSS>.md`，一次运行一个文件，只有本轮确有新帖才写。

临时读一份（不落盘、不动游标）：

    cd scripts && python3 -m modules.x --tab Following --screens 2 --format md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .base import Ctx, desk, now_iso, refresh_seen

START_URL = "https://x.com/home"
LOAD_TIMEOUT = 60                    # 新窗口从出现到帖子入树
STATUS = re.compile(r"/(?:i/web/)?status/(\d+)(?:[?#].*)?$")
X_DOC = re.compile(r"/ X$")          # 活动文档的标题以站点名结尾
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
UNREAD = re.compile(r"^\(\d+\)\s*")          # Firefox 会把未读数写成 "(1) Home / X"


def is_home_title(title):
    return any(UNREAD.sub("", title).startswith(t) for t in HOME_TITLES)


# ───────────────────────── 窗口：每轮一个，用完关掉 ─────────────────────────

class WindowMismatch(RuntimeError):
    """按标题挑到的窗口不是本轮那一个，继续操作就会动到别人的标签。"""


@dataclass
class Window:
    """本轮自己开的那一个 Firefox 窗口。标题随页面变，所以只记窗口 id。"""

    id: str

    def title(self) -> str:
        title = title_of(self.id)
        if title is None:
            raise RuntimeError(f"本轮窗口 {self.id} 已经不在窗口列表里")
        return title


def firefox_windows():
    return [win for win in desk("windows")
            if "firefox" in str(win.get("app", "")).lower()]


def title_of(win_id, wins=None):
    for win in (wins if wins is not None else firefox_windows()):
        if win.get("id") == win_id:
            return win.get("title") or ""
    return None


def wait_new_window(before, timeout=LOAD_TIMEOUT):
    """等一个新的 Firefox 窗口出现；窗口列表里越靠后越新。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fresh = [win for win in firefox_windows()
                 if win.get("id") not in before and (win.get("title") or "").strip()]
        if fresh:
            return fresh[-1]["id"]
        time.sleep(0.5)
    raise RuntimeError(f"{timeout}s 内没有出现新的 Firefox 窗口")


def open_window(url=START_URL, timeout=LOAD_TIMEOUT):
    """开一个全新的 Firefox 窗口（只带这一个 x.com 标签），等帖子真的出现。"""
    before = {win.get("id") for win in firefox_windows()}
    desk("launch", "app=firefox", f'args=["--new-window","{url}"]')
    win = Window(wait_new_window(before, timeout))
    try:
        take_front(win)
        wait_loaded(win, timeout)
    except RuntimeError:
        close_window(win)                 # 没加载出来也别把窗口留在桌面上
        raise
    return win


def window_alive(win):
    return title_of(win.id) is not None


def take_front(win):
    """把本窗口带到最前，并确认按标题挑中的确实是它——不是就停手，别动别人的窗口。"""
    title = win.title()
    if not title.strip():
        raise RuntimeError(f"本轮窗口 {win.id} 还没有标题，先别按标题操作")
    picked = desk("snapshot", f"window={title}", "text=true", "max_chars=200")
    got = (picked.get("window") or {}).get("id")
    if got != win.id:
        raise WindowMismatch(f"按标题挑到了另一个 Firefox 窗口（{got}），本轮窗口是 {win.id}")
    desk("focus", f"window={title}")


def wait_gone(win, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not window_alive(win):
            return True
        time.sleep(0.5)
    return not window_alive(win)


def close_window(win, timeout=30):
    """关掉本轮窗口：唯一标签一次 `ctrl+w` 即关窗；还剩标签就点它自己的关闭按钮。

    收尾这一步不抛异常：它多半跑在 finally 里，抛出去会盖掉真正的失败原因。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not window_alive(win):
            return True
        try:
            take_front(win)
            desk("key", f"window={win.title()}", "keys=ctrl+w")
        except RuntimeError as error:
            print(f"warning: 关窗快捷键没发出去（{error}），改用窗口自己的关闭按钮",
                  file=sys.stderr)
            break
        if wait_gone(win, 10):
            return True
    if window_alive(win):
        try:
            if click_close_button(win):
                return wait_gone(win, 10)
        except RuntimeError as error:
            print(f"warning: 点关闭按钮失败（{error}）", file=sys.stderr)
    if window_alive(win):
        print(f"warning: 没能关掉本轮窗口 {win.id}", file=sys.stderr)
        return False
    return True


def click_close_button(win, timeout=10):
    """点这个窗口自己的关闭按钮：按节点 id，不按坐标，也不碰键盘焦点。"""
    nodes = snapshot(win, chars=60000).get("nodes") or []
    by_ref = {node.get("ref"): node for node in nodes}
    for node in nodes:
        if node.get("role") != "button" or (node.get("name") or "").strip() != "Close":
            continue
        if not node.get("bounds"):
            continue
        parent, chrome = node.get("parent"), False
        while parent not in (None, -1):
            ancestor = by_ref.get(parent)
            if ancestor is None:
                break
            role = ancestor.get("role")
            if role in ("document", "internal frame"):     # 页面里的 Close，不是窗口的
                break
            if role == "toolbar":
                chrome = True
                break
            parent = ancestor.get("parent")
        if chrome:
            desk("act", f"id={node['id']}", "action=click")
            return wait_gone(win, timeout)
    return False


# ───────────────────────── Firefox / 时间线 ─────────────────────────


def go_home(win):
    """回到 Home；已经在这一页就不动。"""
    title = win.title()
    if is_home_title(title):
        return title
    return open_url(win, START_URL)


def open_url(win, url, timeout=40):
    """在地址栏里打开一个页面，等当前文档真的换成新页再返回。"""
    before = document_name(snapshot(win).get("nodes") or [])
    take_front(win)
    title = win.title()
    desk("key", f"window={title}", "keys=ctrl+l")
    time.sleep(0.5)
    desk("type", f"window={title}", f"text={url}")
    desk("key", f"window={title}", "keys=Return")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        try:
            nodes = snapshot(win).get("nodes") or []
        except WindowMismatch:
            raise
        except RuntimeError:
            continue
        name = document_name(nodes)
        if name and name != before and posts_in(nodes):
            return win.title()
    raise RuntimeError(f"页面没有加载出帖子：{url}")


def snapshot(win, chars=200000, bounds=False):
    """按窗口 id 精确读一棵树：同一进程里同名窗口按标题挑不准。"""
    args = [f"id={win.id}", "urls=true", f"max_chars={chars}"]
    if bounds:
        args.append("bounds=true")
    snap = desk("snapshot", *args)
    got = (snap.get("window") or {}).get("id")
    if got is not None and got != win.id:
        raise WindowMismatch(f"读到的窗口不是本轮那个（{got} != {win.id}）")
    return snap


def active_document(nodes):
    """窗口树里包含所有标签页的文档；正在显示的那个带 showing（被聚焦的还有 focused）。"""
    docs = [n for n in nodes if n.get("role") == "document"]
    if not docs:
        return None
    for state in ("focused", "showing"):
        for node in docs:
            if state in (node.get("states") or []):
                return node.get("ref")
    for node in docs:                     # 新窗口的树里会残留一个 New Tab 文档
        if X_DOC.search(node.get("name") or ""):
            return node.get("ref")
    return docs[-1].get("ref")


def document_name(nodes):
    """当前文档的可访问名，也就是这一页的标题。"""
    ref = active_document(nodes)
    for node in nodes:
        if node.get("ref") == ref:
            return (node.get("name") or "").strip()
    return ""


def under_document(nodes, ref):
    by_ref = {n.get("ref"): n for n in nodes}
    kept = []
    for node in nodes:
        parent = node.get("parent")
        while parent is not None and parent != -1:
            if parent == ref:
                kept.append(node)
                break
            ancestor = by_ref.get(parent)
            parent = ancestor.get("parent") if ancestor else None
    return kept


def posts_in(nodes):
    """按文档顺序返回当前标签页里每个帖子的 (节点 id, 可访问名, permalink)。"""
    ref = active_document(nodes)
    if ref is not None:
        nodes = under_document(nodes, ref)     # 后台标签的文档也在这棵树里，必须只读当前的
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


def wait_loaded(win, timeout=LOAD_TIMEOUT):
    """新窗口的壳先出来，页面内容随后才到；等到当前文档里真的有帖子为止。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if posts_in(snapshot(win).get("nodes") or []):
                return True
        except WindowMismatch:
            raise
        except RuntimeError:
            pass                          # 加载中 AT-SPI 会短暂读不到，下一轮再试
        time.sleep(1)
    raise RuntimeError(f"{timeout}s 内窗口 {win.id} 没有加载出帖子")


def wait_for_posts(win, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if posts_in(snapshot(win).get("nodes") or []):
                return True
        except WindowMismatch:
            raise
        except RuntimeError:
            pass
        time.sleep(1)
    return False


def first_post(win):
    posts = posts_in(snapshot(win).get("nodes") or [])
    return posts[0][1] if posts else None


def active_nodes(win):
    """当前窗口的树里，只取当前标签页的那部分。"""
    nodes = snapshot(win).get("nodes") or []
    ref = active_document(nodes)
    return under_document(nodes, ref) if ref is not None else nodes


def select_tab(win, tab, timeout=40):
    """选当前标签页里的页内时间线；点完要等内容真的换掉才算切到（选中态先出现、内容后到）。"""
    if not tab:
        return True
    before = first_post(win)
    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        node = next((n for n in active_nodes(win)
                     if n.get("role") == "tab" and (n.get("name") or "").strip() == tab), None)
        if node is None:
            time.sleep(1)
            continue
        selected = "selected" in (node.get("states") or [])
        if selected:
            now = first_post(win)
            if not clicked:                       # 本来就在这一栏，等内容出来就行
                if now:
                    return True
            elif now and now != before:           # 我们点的：内容换掉了才算切到
                return True
        elif not clicked:
            take_front(win)
            desk("act", f"id={node['id']}", "action=click")
            clicked = True
        time.sleep(1.5)
    print(f"warning: 切不到时间线 {tab!r}", file=sys.stderr)
    return False


def scroll_page(win, amount=8, direction="down", point=None):
    """滚动正文：默认落在视口中间那条帖子上；point 可指定，用来换点重试。"""
    point = point or page_point(win)
    if point is None:
        return False
    x, y = point
    take_front(win)                       # 先把自己带到最上层，滚轮才落在它身上
    desk("scroll", f"direction={direction}", f"amount={amount}", f"x={x}", f"y={y}")
    return True


def feed_center():
    """正文中央：滚轮落点被帖子里的媒体/嵌套容器吃掉时的备用点。"""
    screen = desk("screen")
    width, height = int(screen.get("width", 1280)), int(screen.get("height", 800))
    return int(width * 0.45), int(height * 0.55)


def page_point(win):
    """时间线正文里一个“看得见”的点：取视口内最靠中间的帖子 permalink 链接。

    滚轮按屏幕坐标落点，落在视口外等于没滚；帖子的 bounds 常留在屏外（虚拟列表只回收
    一部分），所以只看视口内的，实在没有才退回正文中央。
    """
    nodes = snapshot(win, bounds=True).get("nodes") or []
    ref = active_document(nodes)
    inside = under_document(nodes, ref) if ref is not None else nodes
    links = [node for node in inside
             if node.get("role") == "link" and node.get("bounds")
             and STATUS.search(node.get("url") or "")]
    if not links:
        return None
    screen = desk("screen")
    width, height = int(screen.get("width", 1280)), int(screen.get("height", 800))

    def middle_y(node):
        return node["bounds"][1] + node["bounds"][3] / 2

    visible = [node for node in links if 120 <= middle_y(node) <= height - 120]
    if not visible:
        return int(width * 0.45), int(height * 0.55)
    pick = min(visible, key=lambda node: abs(middle_y(node) - height / 2))
    x, y, w, h = pick["bounds"]
    return int(x + w / 2), int(y + h / 2)


def page_down(win):
    """滚轮没推动时的兜底：键盘翻页，走的是文档自己的滚动。"""
    take_front(win)
    desk("key", f"window={win.title()}", "keys=Page_Down")


def collect(win, screens, pause=3.5):
    """滚动读取时间线：每条只留一次并带上 permalink；连续两屏没有新帖就提前收工。"""
    seen = {}
    idle = 0
    count = 0
    for index in range(screens):
        if index == 0:
            wait_for_posts(win)
        before_first = first_post(win)
        for node_id, name, url in posts_in(snapshot(win).get("nodes") or []):
            if node_id in seen:
                if url and not seen[node_id]["url"]:
                    seen[node_id]["url"] = url
            else:
                seen[node_id] = {"name": name, "url": url}
        idle = 0 if len(seen) > count else idle + 1
        count = len(seen)
        if index == screens - 1 or idle >= 2:
            break
        if not scroll_page(win):                  # 正文还没出来：等它，别把滚轮甩到导航上
            wait_for_posts(win)
            time.sleep(1)
            continue
        time.sleep(pause)
        if first_post(win) == before_first:       # 滚轮被帖子里的东西吃掉：换正文中央，再不行键盘
            scroll_page(win, point=feed_center())
            time.sleep(pause)
            if first_post(win) == before_first:
                page_down(win)
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


def read_home(win: Window, tab: str = "", screens: int = 6) -> tuple[str, list[dict]]:
    """在本轮窗口里回 Home，切到指定页内标签并读取帖子。"""
    go_home(win)
    select_tab(win, tab)
    return win.title(), parse_items(collect(win, screens))


def read_account(win: Window, handle: str, screens: int = 6) -> tuple[str, list[dict]]:
    """在本轮窗口里打开某账号的主页时间线并读取帖子。"""
    open_url(win, f"https://x.com/{handle_of(handle)}")
    return win.title(), parse_items(collect(win, screens))


def read_sections(win: Window, plan: list[tuple[str, str]], screens: int):
    sections: list[tuple[str, list[dict]]] = []
    for kind, value in plan:
        if kind == "tab":
            _, posts = read_home(win, value, screens)
            sections.append((f"Home · {value}" if value else "Home", posts))
        else:
            _, posts = read_account(win, value, screens)
            sections.append((f"@{handle_of(value)}", posts))
    return sections


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
    plan = sections_of(ctx.config)
    win = open_window()
    try:
        sections = read_sections(win, plan, screens)
    finally:
        close_window(win)                 # 抓到一半出错也把这一轮开的窗口收掉

    unique: dict[str, dict] = {}
    for label, posts in sections:
        for post in posts:
            unique.setdefault(post_id(post), {**post, "section": label})

    state = ctx.state()
    refresh_seen(ctx, state)              # 只保留 seen_keep_days 天内的记录
    if not state.get("primed"):
        for iid in unique:
            state["seen"][iid] = now_iso()
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

    for iid in fresh:
        state["seen"][iid] = now_iso()
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
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args(argv)

    # 优先级：命令行 > 配置文件 > 内置默认。
    tabs = ([t.strip() for t in args.tab if t.strip()] if args.tab is not None
            else [str(t).strip() for t in (config.get("tabs") or []) if str(t).strip()])
    accounts = ([a.strip() for a in args.account if a.strip()] if args.account is not None
                else [str(a).strip() for a in (config.get("accounts") or []) if str(a).strip()])
    screens = args.screens if args.screens is not None else int(config.get("screens", 6))
    plan = [("tab", t) for t in tabs] + [("account", a) for a in accounts] or [("tab", "")]

    win = open_window()
    try:
        sections = read_sections(win, plan, screens)
    finally:
        close_window(win)

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
