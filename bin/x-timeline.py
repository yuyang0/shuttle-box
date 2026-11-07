#!/usr/bin/env python3
"""Collect X timelines through Firefox's accessibility tree.

Always the same path: make sure Firefox is running, make sure an x.com tab is the active one,
click Home in the site's navigation, then read the timeline - optionally after selecting a tab
such as Following. No URL to pass, no CDP and no JS injection: this is the accessibility tree an
agent drives through shuttle.

Examples
    bin/x-timeline.py                          # Home, whatever tab it shows
    bin/x-timeline.py --tab Following          # Home, then the Following tab
    bin/x-timeline.py --tab Following,Investment --links --format md
    bin/x-timeline.py --screens 8 > feed.json
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")

ARTICLE = re.compile(r'^- article "(.*)" \[id=([^\]]+)\]$', re.S)
HANDLE = re.compile(r'@([A-Za-z0-9_]{1,15})')
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
# Links that are literally visible in a post's text; the accessibility tree exposes no
# permalink of its own on Linux, so this is all we can recover without clicking.
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


def read_links():
    """Ask AT-SPI for the URI behind each post's timestamp link (see bin/x-links.py)."""
    script = pathlib.Path(__file__).with_name("x-links.py")
    if not script.exists():
        print(f"warning: {script} is missing", file=sys.stderr)
        return {}
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-"],
        input=script.read_text(), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"warning: link reader failed: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return {}
    try:
        posts = json.loads(proc.stdout).get("posts", [])
    except json.JSONDecodeError:
        print("warning: link reader returned no JSON", file=sys.stderr)
        return {}
    return {p["name"]: p["permalink"] for p in posts if p.get("permalink")}


def attach_links(posts, by_name):
    """Attach the permalink of the node with the same accessible name, and nothing else.

    No fallback by handle: one author usually has several posts in a scrape, so guessing would
    attach someone else's post rather than admit the miss.
    """
    matched = 0
    for post in posts:
        url = by_name.get(post.get("_name"))
        if url:
            post["url"] = url
            matched += 1
        post.pop("_name", None)
    return matched


# Firefox shows a tab's title, and an x.com tab always ends with the site name.
X_TAB = re.compile(r"/ X$")
TAB = re.compile(r'^- tab "(.*)" \[id=([^\]]+)\]$')
START_URL = "https://x.com/home"


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


def window_title_of(pid):
    for win in firefox_windows():
        if str(win.get("pid")) == str(pid):
            return win.get("title")
    return None


def active_tab_is(name, title):
    """Firefox titles its window "<tab title> — Mozilla Firefox"."""
    return title == f"{name} — Mozilla Firefox"


def tabs_of(title):
    snap = desk("snapshot", f"window={title}", "text=true", "max_chars=12000")
    tabs = []
    for line in (snap.get("text") or "").splitlines():
        match = TAB.match(line.strip())
        if match:
            tabs.append((match.group(1), match.group(2)))
    return tabs


def wait_tab_active(name, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = firefox_windows()
        if win and active_tab_is(name, win[0].get("title") or ""):
            return True
        time.sleep(0.5)
    return False


def firefox_running():
    """A live process may have no window yet; launching then would start a second instance."""
    proc = subprocess.run(["docker", "exec", CONTAINER, "pgrep", "-x", "firefox-bin"],
                          capture_output=True, text=True)
    return proc.returncode == 0


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
    for name, node in tabs_of(title):
        if not is_x_tab(name):
            continue
        if active_tab_is(name, title):
            return title
        desk("focus", f"window={title}")
        desk("act", f"id={node}", "action=click")
        if wait_tab_active(name):
            return firefox_windows()[0]["title"]
        print(f"warning: could not switch to tab {name!r}", file=sys.stderr)
        return title

    print(f"no x.com tab, opening {START_URL}", file=sys.stderr)
    desk("focus", f"window={title}")
    desk("key", f"window={title}", "keys=ctrl+t")
    time.sleep(1.5)
    desk("type", f"window={title}", f"text={START_URL}")
    desk("key", f"window={title}", "keys=Return")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for name, node in tabs_of(firefox_windows()[0]["title"]):
            if is_x_tab(name):
                if not active_tab_is(name, firefox_windows()[0]["title"]):
                    desk("act", f"id={node}", "action=click")
                    wait_tab_active(name)
                return firefox_windows()[0]["title"]
        time.sleep(1)
    raise RuntimeError(f"no x.com tab after opening {START_URL}")


HOME_TITLES = ("Home / X", "首页 / X")


def click_home(title, timeout=30):
    """Go to the home timeline with the site's own shortcut, clicking Home as a fallback."""
    desk("focus", f"window={title}")
    before = title
    for name in ("Home", "首页"):
        desk("key", f"window={title}", "keys=g")
        time.sleep(0.3)
        desk("key", f"window={title}", "keys=h")
        time.sleep(2)
        title = firefox_windows()[0]["title"]
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
                title = firefox_windows()[0]["title"]
                if any(title.startswith(t) for t in HOME_TITLES):
                    return title
                break
    print(f"warning: could not reach Home (title={title!r}, was {before!r})", file=sys.stderr)
    return title


def first_article(title):
    found = articles(title)
    return found[0][1] if found else None


def select_tab(title, tab, timeout=20):
    """Select a timeline tab inside the page, such as Following.

    This is an in-page tab, not a browser tab, so the window title does not change: confirm by
    its selected state and by the first post changing.
    """
    if not tab:
        return
    before = first_article(title)
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
            switch_deadline = time.time() + 10
            while time.time() < switch_deadline:
                now = first_article(title)
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


def articles(title):
    """Every post the tree currently shows, as (node id, accessible name)."""
    snap = desk("snapshot", f"window={title}", "text=true", "max_chars=80000")
    found = []
    for line in (snap.get("text") or "").splitlines():
        match = ARTICLE.match(line.strip())
        if match:
            found.append((match.group(2), match.group(1)))
    return found


def collect(title, screens, pause=3.5, links=None):
    seen = {}
    for i in range(screens):
        for article_id, text in articles(title):
            seen.setdefault(article_id, text)
        if links is not None:
            # Posts leave the DOM once scrolled past, so resolve links while they are there.
            for name, permalink in read_links().items():
                links.setdefault(name, permalink)
        if i < screens - 1:
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
            post["author"] = author.replace("Verified account", "").strip()
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

    # A post that is only an image or a video has no text body; keep the accessible name
    # so nothing is silently dropped.
    post.setdefault("text", "")
    haystack = post["text"] + " " + post.get("quote", "")
    urls = [u.rstrip(".,;:)]}") for u in VISIBLE_URL.findall(haystack)]
    if urls:
        post["urls"] = urls
    if post.get("handle"):
        # The profile, not the post: a post permalink needs its status id, which AT-SPI
        # does not carry here.
        post["profile"] = f"https://x.com/{post['handle']}"
    if not post["text"]:
        post["raw"] = raw
    return post


def parse_many(items, source=None):
    posts = [parse(raw) for raw in items]
    for post, raw in zip(posts, items):
        post["_name"] = raw          # kept only until links are attached
    if source:
        for post in posts:
            post["source"] = source
    return posts


def to_markdown(sections):
    out = []
    for source, posts in sections:
        out.append(f"## {source}\n")
        for i, post in enumerate(posts, 1):
            who = post.get("author") or "(unknown)"
            handle = f" (@{post['handle']})" if post.get("handle") else ""
            when = f" · {post['time']}" if post.get("time") else ""
            out.append(f"{i}. **{who}**{handle}{when}")
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
    ap.add_argument("--links", action="store_true",
                    help="also resolve each post's permalink through AT-SPI (needs pyatspi)")
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
        links = {} if args.links else None
        posts = parse_many(collect(title, args.screens, links=links))
        if args.links:
            print(f"   {len(posts)} posts, {attach_links(posts, links)} with a permalink",
                  file=sys.stderr)
        else:
            print(f"   {len(posts)} posts", file=sys.stderr)
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
