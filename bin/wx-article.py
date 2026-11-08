#!/usr/bin/env python3
"""Read WeChat official-account articles through the accessibility tree.

WeChat's Linux client is a Qt shell around an embedded Chromium (WeChatAppEx): the search box
and title bar belong to the shell, while the account page and the article body are rendered by
the Chromium process, whose AT-SPI tree carries every paragraph. That makes the whole path -
search, open the account, list articles, read one - scriptable without OCR.

The article's canonical mp.weixin.qq.com URL is reported as well, but the body is read from the
tree: fetching that URL from outside WeChat answers "环境异常" for any client that did not come
through WeChat's own browser.

Usage
    bin/wx-article.py                                  # newest article of the default account
    bin/wx-article.py --account "Kevin策略研究" --count 3
    bin/wx-article.py --account "某公众号" --format md > out.md

Needs the container running with WeChat logged in.

`--count 1` is the reliable path. The client renders its article list one card at a time and
the tree does not follow a wheel scroll, so asking for several articles repopulates the page per
article and stops as soon as the list runs out of cards it can see.
"""
import argparse
import json
import os
import re
import sys
import time

CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")
DEFAULT_ACCOUNT = "Kevin策略研究"
ARTICLE_URL = re.compile(r"^https://mp\.weixin\.qq\.com/s")
# Chrome of the page: ui labels and prompts that are not part of the body.
NOISE = re.compile(r"^(元宝提供|由微信提供翻译支持.*|问AI|预览时标签不可点|阅读原文|留言|"
                   r"写留言|赞|在看|分享|全部|贴图|文章|视频号|置顶|已关注|私信|"
                   r"选择要提醒的账号|暂无评论|微信扫一扫.*)$")
PROFILE_MARKS = ("全部", "贴图", "视频号")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shuttle_client import Client, desk_call          # noqa: E402

CLIENT = None


def client(transport="auto"):
    global CLIENT
    if CLIENT is None:
        CLIENT = Client(transport)
    return CLIENT


def desk(*args):
    """`desk VERB k=v ...` through MCP, whichever transport is available."""
    return desk_call(client(), *args)


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
    """WeChat must already be open and logged in; this script never starts or restarts it.

    Its window is the real precondition: a process listing matches whatever command line the
    check itself was run from, and a running client without a login screen is not useful.
    """
    try:
        return shell_window()
    except RuntimeError as error:
        raise RuntimeError(f"{error} — open WeChat and log in, then run this again") from error


def shell_window():
    """The Qt shell: the window whose tree holds the nav toolbar."""
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


def wait_window(title, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for win in windows():
            if win.get("title") == title:
                return win
        time.sleep(1)
    return None


def content_pid(timeout=20):
    """The Chromium process rendering the account page or the article."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for win in windows():
            if win.get("app") != "WeChatAppEx":
                continue
            try:
                text = desk("snapshot", f"id={win['id']}", "text=true",
                            "max_chars=20000").get("text") or ""
            except RuntimeError:
                continue
            if sum(mark in text for mark in PROFILE_MARKS) >= 2 or "paragraph" in text.lower():
                return win["pid"]
            if len(text.splitlines()) > 60:      # an article page
                return win["pid"]
        time.sleep(1)
    raise RuntimeError("no WeChatAppEx window with page content")


def focus_shell(shell, attempts=4):
    """Raise WeChat and confirm it really is in front: a click elsewhere hits the panel."""
    for _ in range(attempts):
        desk("focus", f"window={shell.get('title') or ''}", f"pid={shell['pid']}")
        time.sleep(1)
        active = [w for w in windows() if w.get("active")]
        if active and "wechat" in str(active[0].get("app", "")).lower():
            return True
    return False


def search_page(account, attempts=3):
    """Type the account and open WeChat's search page, which lists the account card."""
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
        # Untargeted input goes to the focused box; naming a window sent the text elsewhere.
        desk("key", "keys=ctrl+a")
        desk("key", "keys=BackSpace")
        desk("type", f"text={account}")
        time.sleep(2.5)
        desk("key", "keys=Down")       # the first suggestion is the account
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
    """On the search page, the account card is the entry into its profile."""
    for node in find(account, pid=pid):
        if node.get("role") in ("link", "button") and click_node(node["id"]):
            time.sleep(4)
            return
    # Fall back to the card's usual place under the search box.
    desk("click", "x=600", "y=310")
    time.sleep(4)


def card_titles(pid, account="", skip=()):
    """Article cards below the profile header, top to bottom: (node id, title, bounds)."""
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
        # Below the header and the tab row, where the cards start.
        if bounds and len(bounds) == 4 and bounds[2] > 40 and bounds[3] > 8 and bounds[1] > 400:
            cards.append((node["id"], name, bounds))
    cards.sort(key=lambda item: item[2][1])
    return cards


def collect_cards(pid, account, want, scrolls=10):
    """Article cards, loading more by scrolling: [(title, node id), ...] in feed order."""
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
    """Paragraph text of the page, grouped from its text children."""
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
    """The article's own title, which beats the card text it was clicked from."""
    snap = desk("snapshot", f"pid={pid}", "urls=true", "max_chars=200000")
    for node in snap.get("nodes") or []:
        if node.get("role") == "heading" and (node.get("name") or "").strip():
            return node["name"].strip()
    return None


def read_article(pid, scrolls=8):
    """The open article: its title, canonical URL and every paragraph, scrolling as it loads."""
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
    """Close the active browser tab; returns False when the browser window is gone."""
    wins = [w for w in windows() if w.get("app") == "WeChatAppEx" and w.get("pid") == pid]
    if not wins:
        return False
    desk("focus", f"window={wins[0].get('title') or ''}", f"pid={pid}")
    desk("key", "keys=ctrl+w")
    time.sleep(wait)
    return True


def tab_count(pid):
    """Tab close buttons are the only way to see how many tabs the client has open."""
    wins = [w for w in windows() if w.get("app") == "WeChatAppEx" and w.get("pid") == pid]
    if not wins:
        return 0
    snap = desk("snapshot", f"id={wins[0]['id']}", "text=true", "max_chars=20000")
    return (snap.get("text") or "").count('button "关闭"')


def close_stale_tabs(limit=20):
    """Close the tabs earlier runs left open, keeping the one the client starts with.

    Measured on this client: each ctrl+w drops the 关闭 button count by one, and four of them
    belong to a single tab plus the window chrome, so stopping at four leaves one tab.
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
    """Open the 公众号 conversation, which collects what the accounts you follow published."""
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
    """The message pane is a document node; its first card sits just below the top edge."""
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


def to_markdown(results):
    out = []
    for entry in results:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", action="append", metavar="NAME[,NAME...]",
                    help="official account name; comma separated or repeated")
    ap.add_argument("--count", type=int, default=1, help="how many of the newest articles")
    ap.add_argument("--scroll", type=int, default=8, help="screens to scroll per article")
    ap.add_argument("--following", action="store_true",
                    help="read the newest article from the 公众号 feed instead of one account")
    ap.add_argument("--close-tabs", action="store_true",
                    help="close the browser tabs earlier runs left open, keeping one")
    ap.add_argument("--keep-tabs", action="store_true",
                    help="leave the browser tabs this run opened (default: close them)")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args()

    accounts = []
    for value in args.account or [DEFAULT_ACCOUNT]:
        accounts.extend(part.strip() for part in value.split(",") if part.strip())
    if not accounts:
        raise RuntimeError("no account name given")

    require_wechat()
    if args.close_tabs:
        close_stale_tabs()
    results = []
    for account in accounts:
        if args.following:
            shell = following_feed()
            open_newest_card(shell)
            pid = next((w["pid"] for w in windows()
                        if w.get("app") == "WeChatAppEx" and w.get("title")), None)
            if pid is None:
                raise RuntimeError("no browser window opened the article")
            heading, url, paragraphs = read_article(pid, scrolls=args.scroll)
            print(f"   公众号 feed · {heading or '(no heading)'}: {len(paragraphs)} 段",
                  file=sys.stderr)
            results.append({"account": "公众号 feed",
                            "articles": [{"title": heading or "", "url": url,
                                          "paragraphs": paragraphs}]})
            if not args.keep_tabs:
                close_tab(pid)
            continue

        articles = []
        for index in range(max(1, args.count)):
            pid = search_page(account)
            open_account_card(pid, account)
            if not press(find("文章", pid=pid, role="link") or find("文章", pid=pid)):
                # The client may have re-created its browser window; try the others.
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
            heading, url, paragraphs = read_article(pid, scrolls=args.scroll)
            name = heading or title
            if articles and name == articles[-1]["title"]:
                print(f"warning: {account}: the next card did not open a different article",
                      file=sys.stderr)
                break
            articles.append({"title": name, "url": url, "paragraphs": paragraphs})
            print(f"   {account} · {name}: {len(paragraphs)} 段", file=sys.stderr)
        if not args.keep_tabs and articles:
            # This account opened a search page and one article tab; close what it added.
            before = tab_count(pid)
            for _ in range(min(2, max(0, before - 1))):
                if not close_tab(pid):
                    break
            print(f"   {account}: tabs {before} -> {tab_count(pid)}", file=sys.stderr)
        results.append({"account": account, "articles": articles})

    if args.format == "md":
        print(to_markdown(results))
    else:
        print(json.dumps({"accounts": results}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        sys.exit(f"wx-article: {error}")
