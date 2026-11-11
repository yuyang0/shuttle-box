"""微信公众号：通过 shuttle 读公众号文章。

微信 Linux 客户端是 Qt 外壳 + 内嵌 Chromium（WeChatAppEx）：搜索框和标题栏属于外壳，
公众号页面与正文由 Chromium 渲染，段落都在它的 AT-SPI 树里，所以整条链路（搜索 → 打开
公众号 → 列文章 → 读一篇）都能脚本化，不需要 OCR。

两种源：
    account = "Kevin策略研究"     按账号读最新 count 篇
    feed = true                   读「公众号」消息流里最新的一篇

产物：`articles/<date>-<id>.md`，一篇一个文件；正文已经是文字，不需要转写。

临时读一次（不落盘、不动游标）：

    cd scripts && python3 -m modules.wechat --account "Kevin策略研究" --count 2 --format md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .base import Ctx, desk, now_iso, refresh_seen

DEFAULT_ACCOUNT = "Kevin策略研究"
ARTICLE_URL = re.compile(r"^https://mp\.weixin\.qq\.com/s")
TOKEN = re.compile(r"/s/([A-Za-z0-9_-]{6,})")
# 页面外壳：UI 文案与提示语，不属于正文。
NOISE = re.compile(r"^(元宝提供|由微信提供翻译支持.*|问AI|预览时标签不可点|阅读原文|留言|"
                   r"写留言|赞|在看|分享|全部|贴图|文章|视频号|置顶|已关注|私信|"
                   r"选择要提醒的账号|暂无评论|微信扫一扫.*)$")


# ───────────────────────── 窗口与导航 ─────────────────────────

def windows():
    return desk("windows")


def find(name, pid=None, role=None):
    args = ["find", f"name={name}"]
    if pid:
        args.append(f"pid={pid}")
    try:
        found = desk(*args).get("found") or []
    except RuntimeError:
        return []
    return [n for n in found if role is None or n.get("role") == role]


def press(found):
    if not found:
        return False
    desk("act", f"id={found[0]['id']}", "action=click")
    return True


def click_node(node_id):
    node = desk("read", f"id={node_id}")
    bounds = node.get("bounds") if isinstance(node, dict) else None
    if not bounds or len(bounds) != 4 or bounds[2] <= 0 or bounds[3] <= 0:
        return False
    desk("click", f"x={bounds[0] + bounds[2] // 2}", f"y={bounds[1] + bounds[3] // 2}")
    return True


def require_wechat():
    """微信必须已经打开并登录：本模块从不启动、重启或杀掉它。

    判据是它的窗口（进程列表会匹配到检查命令自身的命令行），而没有登录界面的客户端没有用。
    """
    try:
        return shell_window()
    except RuntimeError as error:
        raise RuntimeError(f"{error} — open WeChat and log in, then run this again") from error


def shell_window():
    """Qt 外壳：导航工具栏在它的树里。"""
    for win in windows():
        if win.get("app") != "wechat":
            continue
        try:
            text = desk("snapshot", f"id={win['id']}", "text=true",
                        "max_chars=4000").get("text") or ""
        except RuntimeError:
            continue
        if 'toolbar "导航"' in text:
            return win
    raise RuntimeError("WeChat shell window not found (is WeChat running and logged in?)")


def focus_shell(shell, attempts=4):
    """把微信提到最前并确认真的在前面：点偏了会点到面板上。"""
    for _ in range(attempts):
        # 标题经常是空串（AGENTS 里写明按 pid 寻址），传 window="" 反而找不到窗口
        if shell.get("title"):
            desk("focus", f"window={shell['title']}", f"pid={shell['pid']}")
        else:
            desk("focus", f"pid={shell['pid']}")
        time.sleep(1)
        active = [w for w in windows() if w.get("active")]
        if active and "wechat" in str(active[0].get("app", "")).lower():
            return True
    return False


def search_page(account, attempts=3):
    """输入账号名，打开微信的搜索页（账号卡片就在这页）。"""
    shell = shell_window()
    if not focus_shell(shell):
        raise RuntimeError("could not raise the WeChat window")

    text = desk("snapshot", f"id={shell['id']}", "text=true", "max_chars=8000").get("text") or ""
    boxes = re.findall(r'- textbox "搜索" \[id=([^\]]+)\]', text)
    if not boxes:
        raise RuntimeError("search box not found")

    for _ in range(attempts):
        if not any(click_node(box) for box in boxes):
            raise RuntimeError("no 搜索 textbox with usable bounds")
        time.sleep(0.6)
        # 不指定窗口：输入会进当前焦点框；指定窗口反而会打到别处。
        desk("key", "keys=ctrl+a")
        desk("key", "keys=BackSpace")
        desk("type", f"text={account}")
        time.sleep(2.5)
        desk("key", "keys=Down")       # 第一条建议就是该账号
        time.sleep(0.5)
        desk("key", "keys=Return")
        deadline = time.time() + 20
        while time.time() < deadline:
            for win in windows():
                if win.get("app") != "WeChatAppEx":
                    continue
                try:
                    page = desk("snapshot", f"id={win['id']}", "text=true",
                                "max_chars=20000").get("text") or ""
                except RuntimeError:
                    continue
                if "搜一搜" in page or ("相关搜索" in page and account in page):
                    time.sleep(2)
                    return win["pid"]
            time.sleep(1)
    raise RuntimeError(f"search page for {account!r} never appeared")


def open_account_card(pid, account):
    """搜索页上的账号卡片是进公众号主页的入口。"""
    for node in find(account, pid=pid):
        if node.get("role") in ("link", "button") and click_node(node["id"]):
            time.sleep(4)
            return
    # 卡片通常就在搜索框下方这个位置。
    desk("click", "x=600", "y=310")
    time.sleep(4)


def card_titles(pid, account="", skip=()):
    """主页头部下方的文章卡片，从上到下：[(节点 id, 标题, bounds)]。"""
    snap = desk("snapshot", f"pid={pid}", "urls=true", "max_chars=200000")
    cards = []
    for node in snap.get("nodes") or []:
        if node.get("role") != "text":
            continue
        name = (node.get("name") or "").strip()
        if len(name) < 8 or name in skip or NOISE.match(name):
            continue
        if account and account in name:
            continue
        if any(mark in name for mark in ("篇原创", "朋友关注", "已关注", "北京", "金融分析师")):
            continue
        try:
            read = desk("read", f"id={node['id']}")
        except RuntimeError:
            continue
        bounds = read.get("bounds") if isinstance(read, dict) else None
        # 卡片在头部与标签行之下。
        if bounds and len(bounds) == 4 and bounds[2] > 40 and bounds[3] > 8 and bounds[1] > 400:
            cards.append((node["id"], name, bounds))
    cards.sort(key=lambda item: item[2][1])
    return cards


def collect_cards(pid, account, want, scrolls=10):
    """文章卡片，滚动加载更多：[(标题, 节点 id), ...]，按列表顺序。"""
    seen = {}
    for index in range(scrolls):
        for node_id, title, _ in card_titles(pid, account=account):
            seen.setdefault(title, node_id)
        if len(seen) >= want:
            break
        desk("scroll", f"pid={pid}", "direction=down", "amount=8")
        time.sleep(1.5)
    return list(seen.items())


def page_paragraphs(pid):
    """页面正文段落（按文本子节点归组），以及页面上的永久链接。"""
    snap = desk("snapshot", f"pid={pid}", "urls=true", "max_chars=200000")
    nodes = snap.get("nodes") or []
    children = {}
    for node in nodes:
        children.setdefault(node.get("parent", -1), []).append(node)

    def text_of(ref):
        out = []
        for child in children.get(ref, []):
            if child.get("role") == "text" and (child.get("name") or "").strip():
                out.append(child["name"])
            if child.get("role") in ("text", "paragraph", "group", "listitem"):
                out.extend(text_of(child["ref"]))
        return out

    paragraphs = []
    for node in nodes:
        if node.get("role") != "paragraph":
            continue
        text = "".join(text_of(node["ref"])).strip()
        if text and not NOISE.match(text):
            paragraphs.append(text)
    url = next((n["url"] for n in nodes if ARTICLE_URL.match(n.get("url") or "")), None)
    return paragraphs, url


def article_heading(pid):
    """文章自己的标题，比卡片标题可靠。"""
    snap = desk("snapshot", f"pid={pid}", "urls=true", "max_chars=200000")
    for node in snap.get("nodes") or []:
        if node.get("role") == "heading" and (node.get("name") or "").strip():
            return node["name"].strip()
    return None


def read_article(pid, scrolls=8):
    """当前打开的文章：标题、永久链接、全部段落（边滚边读）。"""
    paragraphs, url, seen = [], None, set()
    for index in range(scrolls):
        current, found_url = page_paragraphs(pid)
        url = url or found_url
        for text in current:
            if text not in seen:
                seen.add(text)
                paragraphs.append(text)
        if index < scrolls - 1:
            desk("scroll", f"pid={pid}", "direction=down", "amount=10")
            time.sleep(1.5)
    return article_heading(pid), url, paragraphs


def close_tab(pid, wait=1.5):
    """关掉当前浏览器标签；窗口已经没了就返回 False。"""
    wins = [w for w in windows() if w.get("app") == "WeChatAppEx" and w.get("pid") == pid]
    if not wins:
        return False
    title = wins[0].get("title")
    if title:
        desk("focus", f"window={title}", f"pid={pid}")
    else:
        desk("focus", f"pid={pid}")       # 空标题窗口只能按 pid 找
    desk("key", "keys=ctrl+w")
    time.sleep(wait)
    return True


def tab_count(pid):
    """标签关闭按钮是唯一能看出客户端开了几个标签的东西。"""
    wins = [w for w in windows() if w.get("app") == "WeChatAppEx" and w.get("pid") == pid]
    if not wins:
        return 0
    snap = desk("snapshot", f"id={wins[0]['id']}", "text=true", "max_chars=20000")
    return (snap.get("text") or "").count('button "关闭"')


def browser_pid():
    """当前承载页面的 Chromium 进程；没有窗口时返回 None。

    不按标题过滤：WeChatAppEx 的窗口标题经常是空串，加了过滤会把「有窗口」误判成「没窗口」。
    """
    return next((w["pid"] for w in windows() if w.get("app") == "WeChatAppEx"), None)


def close_to_baseline(pid, baseline, label=""):
    """把本轮打开的标签关回基线，最多试 4 次；关不动就停，别误关客户端自带的标签。

    标签是清理动作，失败只告警：不能因为窗口标题变化或桌面抖动丢掉已经读完的正文。
    """
    if pid is None:
        return
    try:
        for _ in range(4):
            if tab_count(pid) <= baseline or not close_tab(pid):
                break
        print(f"   {label} tabs -> {tab_count(pid)}（基线 {baseline}）", file=sys.stderr)
    except RuntimeError as error:
        print(f"   {label}: 关标签跳过（{error}）", file=sys.stderr)


def close_stale_tabs(limit=20):
    """关掉以前运行留下的标签，保留客户端自带的那个。

    这个客户端上每个 ctrl+w 让「关闭」按钮少一个，四个对应一个标签外加窗口装饰，
    所以停在四就是还剩一个标签。
    """
    pid = next((w["pid"] for w in windows()
                if w.get("app") == "WeChatAppEx" and w.get("title")), None)
    if pid is None:
        return
    before = tab_count(pid)
    for _ in range(limit):
        if tab_count(pid) <= 4:
            break
        if not close_tab(pid):
            break
    print(f"   tabs {before} -> {tab_count(pid)}", file=sys.stderr)


def following_feed():
    """打开「公众号」会话，它汇总了关注的账号最近发布的内容。"""
    shell = shell_window()
    if not focus_shell(shell):
        raise RuntimeError("could not raise the WeChat window")
    for attempt in range(3):
        if not focus_shell(shell):
            time.sleep(1.5)
            continue
        snap = desk("snapshot", f"id={shell['id']}", "urls=true", "max_chars=200000")
        for node in snap.get("nodes") or []:
            if node.get("role") != "listitem":
                continue
            if not (node.get("name") or "").startswith("公众号"):
                continue
            read = desk("read", f"id={node['id']}")
            bounds = read.get("bounds") if isinstance(read, dict) else None
            if not bounds or bounds[2] <= 100:
                continue
            desk("click", f"x={bounds[0] + bounds[2] // 2}",
                 f"y={bounds[1] + bounds[3] // 2}")
            time.sleep(4)
            if newest_card_area(shell):
                return shell
        time.sleep(1)
    raise RuntimeError("the 公众号 conversation did not open its message pane")


def newest_card_area(shell):
    """消息面板是个 document 节点，第一张卡片贴着顶部。"""
    snap = desk("snapshot", f"id={shell['id']}", "urls=true", "max_chars=200000")
    for node in snap.get("nodes") or []:
        if node.get("role") != "document":
            continue
        read = desk("read", f"id={node['id']}")
        bounds = read.get("bounds") if isinstance(read, dict) else None
        if bounds and bounds[2] > 200 and bounds[3] > 200 and bounds[0] > 400:
            return bounds
    return None


def open_newest_card(shell):
    area = newest_card_area(shell)
    if area is None:
        raise RuntimeError("could not locate the message pane")
    x, y, w, _ = area
    desk("click", f"x={x + w // 2}", f"y={y + 40}")
    time.sleep(6)


# ───────────────────────── 读文章 ─────────────────────────

def read_articles(accounts: list[str] | None = None, feed: bool = False, count: int = 1,
                  scroll: int = 8, keep_tabs: bool = False) -> list[dict]:
    """读一批账号（或公众号消息流）的文章；每个账号各自关闭自己开的标签。"""
    require_wechat()
    baseline_pid = browser_pid()
    baseline = max(tab_count(baseline_pid), 4) if baseline_pid else 4
    if feed:
        shell = following_feed()
        open_newest_card(shell)
        pid = next((w["pid"] for w in windows()
                    if w.get("app") == "WeChatAppEx" and w.get("title")), None)
        if pid is None:
            raise RuntimeError("no browser window opened the article")
        heading, url, paragraphs = read_article(pid, scrolls=scroll)
        print(f"   公众号 feed · {heading or '(no heading)'}: {len(paragraphs)} 段",
              file=sys.stderr)
        if not keep_tabs:
            close_to_baseline(pid, baseline, "公众号 feed")
        return [{"account": "公众号 feed", "title": heading or "",
                 "url": url, "paragraphs": paragraphs}]

    articles: list[dict] = []
    for account in accounts or []:
        articles.extend(read_account_articles(account, count=count, scroll=scroll,
                                              keep_tabs=keep_tabs, baseline=baseline))
    return articles


def read_account_articles(account: str, count: int = 1, scroll: int = 8,
                          keep_tabs: bool = False, baseline: int = 0) -> list[dict]:
    """一个账号最新 count 篇；读不到就少读，不抛（单账号失败不该拖垮其它账号）。"""
    articles: list[dict] = []
    pid = None
    for index in range(max(1, count)):
        pid = search_page(account)
        open_account_card(pid, account)
        if not press(find("文章", pid=pid, role="link") or find("文章", pid=pid)):
            # 客户端可能会重建浏览器窗口，试试其它窗口。
            for other in {w["pid"] for w in windows() if w.get("app") == "WeChatAppEx"}:
                if other != pid and press(find("文章", pid=other, role="link")
                                          or find("文章", pid=other)):
                    pid = other
                    break
        time.sleep(3)
        cards = collect_cards(pid, account, want=index + 1)
        if len(cards) <= index:
            print(f"warning: {account}: only {len(cards)} article card(s) reachable",
                  file=sys.stderr)
            break
        title, node_id = cards[index]
        if not click_node(node_id):
            print(f"warning: {account}: {title!r} has no clickable bounds", file=sys.stderr)
            break
        time.sleep(5)
        heading, url, paragraphs = read_article(pid, scrolls=scroll)
        name = heading or title
        if articles and name == articles[-1]["title"]:
            print(f"warning: {account}: the next card did not open a different article",
                  file=sys.stderr)
            break
        articles.append({"account": account, "title": name, "url": url,
                         "paragraphs": paragraphs})
        print(f"   {account} · {name}: {len(paragraphs)} 段", file=sys.stderr)

    if not keep_tabs and articles and pid is not None:
        close_to_baseline(pid, baseline, account)
    return articles


# ───────────────────────── 去重与落盘 ─────────────────────────

def article_id(account: str, article: dict) -> str:
    """优先用微信永久令牌；老式查询串用 mid+idx；都没有按账号+标题哈希。"""
    url = article.get("url") or ""
    match = TOKEN.search(url)
    if match:
        return match.group(1)
    query = parse_qs(urlparse(url).query)
    if query.get("mid") and query.get("idx"):
        return f"{query['mid'][0]}_{query['idx'][0]}"
    seed = f"{account}\n{article.get('title', '')}"
    return "t" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def render(account: str, article: dict) -> str:
    lines = [f"# {article.get('title') or '(无标题)'}", ""]
    if account:
        lines.append(f"- 公众号：{account}")
    lines.append(f"- 链接：{article.get('url') or '(微信未暴露永久链接)'}")
    lines.append(f"- 抓取：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append("")
    paragraphs = [p.strip() for p in article.get("paragraphs") or [] if p.strip()]
    lines.extend(paragraphs or ["(正文为空)"])
    return "\n".join(lines).rstrip() + "\n"


def run(ctx: Ctx) -> list[Path]:
    if not ctx.due():
        return []

    feed = bool(ctx.config.get("feed", False))
    accounts = [str(a).strip() for a in (ctx.config.get("accounts") or []) if str(a).strip()]
    if feed and accounts:
        raise RuntimeError("feed 与 accounts 只能选一个")
    if not feed and not accounts:
        raise RuntimeError("缺少 accounts，或设 feed = true 读公众号消息流")

    articles = read_articles(accounts=accounts, feed=feed,
                             count=int(ctx.config.get("count", 3)),
                             scroll=int(ctx.config.get("scroll", 8)),
                             keep_tabs=bool(ctx.config.get("keep_tabs", False)))
    items: dict[str, dict] = {}
    for article in articles:
        if article:
            items.setdefault(article_id(article["account"], article), article)

    state = ctx.state()
    refresh_seen(ctx, state)
    if not state.get("primed"):
        for iid in items:
            state["seen"][iid] = now_iso()
        state["primed"] = True
        ctx.save_state(state)
        print(f"[prime] {ctx.name}: 把当前 {len(items)} 篇文章标记为已读", file=sys.stderr)
        return []

    seen = set(state["seen"])
    fresh = {i: art for i, art in items.items() if i not in seen}
    if not fresh:
        return []

    written: list[Path] = []
    day = datetime.now().strftime("%Y-%m-%d")
    for iid, article in fresh.items():
        output = ctx.write(f"articles/{day}-{iid}.md", render(article["account"], article))
        if output is None:
            continue
        written.append(output)
        state["seen"][iid] = now_iso()
        ctx.save_state(state)  # 逐篇落盘
    if written:
        print(f"- 新增 {len(written)} 篇文章", file=sys.stderr)
    return written


def to_markdown(groups: list[dict]) -> str:
    out = []
    for entry in groups:
        out.append(f"# {entry['account']}\n")
        if not entry["articles"]:
            out.append("(no article read)\n")
            continue
        for article in entry["articles"]:
            out.append(f"## {article['title']}")
            out.append(f"- URL: {article.get('url') or '(no canonical URL exposed)'}")
            out.append("")
            out.extend(article["paragraphs"])
            out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None, config: dict | None = None) -> int:
    config = config or {}
    ap = argparse.ArgumentParser(description="读公众号文章（不落盘，供临时查看）")
    ap.add_argument("--account", action="append", metavar="NAME[,NAME...]",
                    help="公众号名；逗号分隔或重复传（默认取配置 accounts）")
    ap.add_argument("--count", type=int, default=None, help="读该账号最新几篇（默认取配置，否则 1）")
    ap.add_argument("--scroll", type=int, default=None, help="每篇滚动几屏（默认取配置，否则 8）")
    ap.add_argument("--following", action="store_true",
                    help="读「公众号」消息流里最新的一篇")
    ap.add_argument("--close-tabs", action="store_true",
                    help="先关掉以前运行留下的浏览器标签，保留一个")
    ap.add_argument("--keep-tabs", action="store_true",
                    help="保留本次打开的标签（默认关闭）")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args(argv)

    # 优先级：命令行 > 配置文件 > 内置默认。
    if args.following:
        accounts: list[str] = []
    elif args.account is not None:
        accounts = [part.strip() for value in args.account
                    for part in value.split(",") if part.strip()]
    else:
        accounts = [str(a).strip() for a in (config.get("accounts") or []) if str(a).strip()]
        accounts = accounts or [DEFAULT_ACCOUNT]
    if not args.following and not accounts:
        raise RuntimeError("no account name given")
    count = args.count if args.count is not None else int(config.get("count", 1))
    scroll = args.scroll if args.scroll is not None else int(config.get("scroll", 8))
    keep_tabs = args.keep_tabs or bool(config.get("keep_tabs", False))

    require_wechat()
    if args.close_tabs:
        close_stale_tabs()

    articles = read_articles(accounts=accounts, feed=args.following, count=count,
                             scroll=scroll, keep_tabs=keep_tabs)
    results: list[dict] = []
    for article in articles:
        group = next((g for g in results if g["account"] == article["account"]), None)
        if group is None:
            group = {"account": article["account"], "articles": []}
            results.append(group)
        group["articles"].append(article)

    if args.format == "md":
        print(to_markdown(results))
    else:
        print(json.dumps({"accounts": results}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        sys.exit(f"wx: {error}")
