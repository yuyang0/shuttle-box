#!/usr/bin/env python3
"""Collect X timelines through Firefox's accessibility tree.

Always the same path: make sure Firefox is running, make sure an x.com tab is the active one,
go to Home, then read the timeline - optionally after selecting a tab such as Following.

One structured snapshot yields both the posts and their permalinks: since shuttle 0.1.41 a
`link` node carries its target URL, so no second reader and no name matching is needed. No CDP
and no JS injection either: this is the accessibility tree an agent drives through shuttle.

Examples
    bin/x-timeline.py                          # Home, whatever tab it shows
    bin/x-timeline.py --tab Following          # Home, then the Following tab
    bin/x-timeline.py --tab Following,Investment --format md
    bin/x-timeline.py --screens 8 > feed.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")

# Firefox shows a tab's title, and an x.com tab always ends with the site name.
X_TAB = re.compile(r"/ X$")
TAB = re.compile(r'^- tab "(.*)" \[id=([^\]]+)\]$')
# A permalink ends at the status id; /analytics, /photo/1 and friends do not qualify.
STATUS = re.compile(r"/(?:i/web/)?status/\d+(?:[?#].*)?$")
START_URL = "https://x.com/home"

HANDLE = re.compile(r"@([A-Za-z0-9_]{1,15})")
# Longer units first: "7 hours ago" must not be read as "7 h".
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
# X appends this to some posts' accessible names; it is not part of the author.
NAME_NOISE = re.compile(r"\s*Provides details about protected accounts\.?", re.I)
# Links that are literally visible in a post's text.
VISIBLE_URL = re.compile(r"(?:https?://|(?<![\w@.])(?:x|twitter)\.com/)[^\s\u2026]+")


def desk(*args):
    proc = subprocess.run(
        ["docker", "exec", CONTAINER, "shuttle", "desk", *args],
        capture_output=True, text=True,
    )
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(f"{args} produced nothing: {proc.stderr.strip()}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


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
    """Firefox titles its window "<tab title> — Mozilla Firefox"."""
    return title == f"{name} — Mozilla Firefox"


def firefox_running():
    """A live process may have no window yet; launching then would start a second instance."""
    proc = subprocess.run(["docker", "exec", CONTAINER, "pgrep", "-x", "firefox-bin"],
                          capture_output=True, text=True)
    return proc.returncode == 0


def tabs_of(title):
    snap = desk("snapshot", f"window={title}", "text=true", "max_chars=12000")
    tabs = []
    for line in (snap.get("text") or "").splitlines():
        match = TAB.match(line.strip())
        if match:
            tabs.append((match.group(1), match.group(2)))
    return tabs


def current_window(needle=None, attempts=40):
    """The window title right now: Firefox rewrites it as a page loads."""
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
    """Reuse a running Firefox, launching one on x.com only when none is alive."""
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = firefox_windows(needle)
        if wins:
            time.sleep(5)      # let the page load before ensure_x_tab judges the tabs
            return wins[0]["title"]
        time.sleep(1)
    raise RuntimeError("Firefox did not start")


def ensure_x_tab(title, timeout=40):
    """Make an x.com tab the active one, opening one when the window has none."""
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


HOME_TITLES = ("Home / X", "首页 / X")


def click_home(title, timeout=30):
    """Go to the home timeline with the site's own shortcut, clicking Home as a fallback."""
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


def snapshot(title, chars=200000):
    """One structured read: every node, with a link's target URL where there is one."""
    return desk("snapshot", f"window={title}", "urls=true", f"max_chars={chars}")


def posts_in(nodes):
    """(node id, accessible name, permalink) for every post, in document order."""
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
            if current.get("role") != "article":      # a quoted post has its own links
                stack.extend(children.get(current["ref"], []))
        found.append((node.get("id"), node.get("name") or "", url))
    return found


def wait_for_posts(title, timeout=20):
    """A timeline is ready when its first read shows at least one post."""
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
    """Select a timeline tab inside the page, such as Following.

    This is an in-page tab, not a browser tab, so the window title does not change: confirm by
    its selected state and by the first post changing.
    """
    if not tab:
        return
    # Another tab keeps whatever scroll position the page had, so compare from the top.
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
    """Scroll through the timeline, keeping each post once with its permalink."""
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
    """Split an accessible name into fields; the raw string is always kept."""
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

    # A post that is only an image or a video has no text body; keep the accessible name so
    # nothing is silently dropped.
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


def parse_many(items, source=None):
    posts = []
    for item in items:
        post = parse(item["name"])
        if item.get("url"):
            post["url"] = item["url"]
        if source:
            post["source"] = source
        posts.append(post)
    return posts


def to_markdown(sections):
    out = []
    for source, posts in sections:
        out.append(f"## {source}\n")
        for index, post in enumerate(posts, 1):
            who = post.get("author") or "(unknown)"
            handle = f" (@{post['handle']})" if post.get("handle") else ""
            when = f" · {post['time']}" if post.get("time") else ""
            out.append(f"{index}. **{who}**{handle}{when}")
            out.append(f"   {(post.get('text') or '').strip()}")
            if post.get("quote"):
                out.append(f"   > {post['quote'][:200]}")
            if post.get("url"):
                out.append(f"   ↗ {post['url']}")
            for url in post.get("urls", []):
                out.append(f"   ↗ {url if url.startswith('http') else 'https://' + url}")
            if post.get("profile") and not (post.get("url") or post.get("urls")):
                out.append(f"   ↗ {post['profile']}")
            if post.get("engagement"):
                stats = ", ".join(f"{k} {v:,}" for k, v in post["engagement"].items())
                out.append(f"   {stats}")
            out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tab", metavar="NAME[,NAME...]",
                    help="timeline tab to select on Home; comma separated to read several "
                         "in one run")
    ap.add_argument("--screens", type=int, default=6,
                    help="screens to scroll per tab (default 6)")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    ap.add_argument("--window", metavar="SUBSTR", help="pick the Firefox window by title")
    args = ap.parse_args()

    tabs = [t.strip() for t in (args.tab or "").split(",") if t.strip()] or [None]

    title = ensure_firefox(args.window)
    title = ensure_x_tab(title)
    title = click_home(title)

    sections = []
    for tab in tabs:
        select_tab(title, tab)
        title = firefox_windows()[0]["title"]
        posts = parse_many(collect(title, args.screens))
        withurl = sum(1 for post in posts if post.get("url"))
        print(f"   {len(posts)} posts, {withurl} with a permalink", file=sys.stderr)
        sections.append((f"{title} [{tab or '-'}]", posts))

    if args.format == "md":
        print(to_markdown(sections))
    elif len(sections) == 1:
        print(json.dumps(sections[0][1], ensure_ascii=False, indent=1))
    else:
        print(json.dumps(
            [{"source": source, "count": len(posts), "posts": posts}
             for source, posts in sections],
            ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        sys.exit(f"x-timeline: {error}")
