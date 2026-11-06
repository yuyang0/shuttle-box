#!/usr/bin/env python3
"""Collect X timelines through Firefox's accessibility tree.

Reads the visible posts (author, time, text, engagement) from the AT-SPI tree, scrolling a
few times to gather more. No CDP and no JS injection: this is the same path an agent takes
through shuttle.

Examples
    # what is on screen right now
    bin/x-timeline.py

    # navigate first, then scrape
    bin/x-timeline.py --open https://x.com/home --tab Following

    # several pages, comma separated, one tab each, human-readable output
    bin/x-timeline.py --open https://x.com/home,https://x.com/home \
        --tab Following,Investment --format md

    # heavier scrape, structured JSON on stdout
    bin/x-timeline.py --open https://x.com/home --tab Following --screens 8 > feed.json
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
        return []
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-"],
        input=script.read_text(), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"warning: link reader failed: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(proc.stdout).get("posts", [])
    except json.JSONDecodeError:
        print("warning: link reader returned no JSON", file=sys.stderr)
        return []


def attach_links(posts, resolved):
    """Match each collected post to its AT-SPI node: exact name first, then handle."""
    by_name = {r.get("name"): r.get("permalink") for r in resolved if r.get("permalink")}
    by_handle = {}
    for r in resolved:
        permalink = r.get("permalink")
        if not permalink:
            continue
        parts = permalink.split("/")
        if len(parts) > 3:
            by_handle.setdefault(parts[3].lower(), permalink)
    matched = 0
    for post in posts:
        url = by_name.get(post.get("_name"))
        if url is None and post.get("handle"):
            url = by_handle.get(post["handle"].lower())
        if url:
            post["url"] = url
            matched += 1
        post.pop("_name", None)
    return matched


def firefox_window(needle=None):
    for win in desk("windows"):
        if "firefox" not in str(win.get("app", "")):
            continue
        title = win.get("title") or ""
        if needle is None or needle.lower() in title.lower():
            return title
    raise RuntimeError("no matching Firefox window")


def articles(title):
    snap = desk("snapshot", f"window={title}", "text=true", "max_chars=80000")
    found = []
    for line in (snap.get("text") or "").splitlines():
        match = ARTICLE.match(line.strip())
        if match:
            found.append((match.group(2), match.group(1)))
    return found


def wait_ready(title, timeout):
    """A page is ready once it renders at least one post."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if articles(title):
                return True
        except RuntimeError:
            pass
        time.sleep(1)
    return False


def navigate(url, window_needle, timeout):
    title = firefox_window(window_needle)
    desk("focus", f"window={title}")
    desk("key", f"window={title}", "keys=ctrl+l")
    desk("type", f"window={title}", f"text={url}")
    desk("key", f"window={title}", "keys=Return")
    time.sleep(2)
    title = firefox_window(window_needle)
    if not wait_ready(title, timeout):
        print(f"warning: {url} rendered no post within {timeout}s", file=sys.stderr)
    return title


def press_tab(title, tab, timeout=10):
    """Click a home tab; it may render a moment after the first post does."""
    if not tab:
        return
    deadline = time.time() + timeout
    while True:
        try:
            found = desk("find", f"name={tab}").get("found") or []
        except RuntimeError:
            found = []
        for node in found:
            if node.get("role") == "tab":
                desk("press", f"id={node['id']}")
                time.sleep(4)
                return
        if time.time() >= deadline:
            print(f"warning: no tab named {tab!r}", file=sys.stderr)
            return
        time.sleep(1)


def collect(title, screens, pause=3.5):
    seen = {}
    for i in range(screens):
        for article_id, text in articles(title):
            seen.setdefault(article_id, text)
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
    ap.add_argument("--open", metavar="URL[,URL...]",
                    help="navigate to each URL (comma separated) before scraping")
    ap.add_argument("--tab", metavar="NAME[,NAME...]",
                    help="click this tab after each page loads; comma separated and matched by "
                         "position with --open, a single value applies to every page")
    ap.add_argument("--screens", type=int, default=6, help="screens to scroll per page")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    ap.add_argument("--links", action="store_true",
                    help="also resolve each post's permalink through AT-SPI (needs pyatspi)")
    ap.add_argument("--window", metavar="SUBSTR", help="pick the Firefox window by title")
    ap.add_argument("--timeout", type=int, default=20, help="seconds to wait for a page")
    args = ap.parse_args()

    urls = [u.strip() for u in (args.open or "").split(",") if u.strip()]
    tabs = [t.strip() for t in (args.tab or "").split(",") if t.strip()]

    if not urls:
        title = firefox_window(args.window)
        press_tab(title, tabs[0] if tabs else None)
        posts = parse_many(collect(title, args.screens))
        if args.links:
            print(f"   {attach_links(posts, read_links())} of {len(posts)} posts got a permalink",
                  file=sys.stderr)
        if args.format == "md":
            print(to_markdown([("current window", posts)]))
        else:
            print(json.dumps(posts, ensure_ascii=False, indent=1))
        return 0

    sections = []
    for index, url in enumerate(urls):
        if index < len(tabs):
            tab = tabs[index]
        elif len(tabs) == 1:
            tab = tabs[0]
        else:
            tab = None
        print(f"== {url} (tab={tab or '-'})", file=sys.stderr)
        title = navigate(url, args.window, args.timeout)
        press_tab(title, tab)
        posts = parse_many(collect(title, args.screens), source=url)
        if args.links:
            resolved = read_links()
            print(f"   {len(posts)} posts, {attach_links(posts, resolved)} with a permalink",
                  file=sys.stderr)
        else:
            print(f"   {len(posts)} posts", file=sys.stderr)
        sections.append((f"{url} [{tab}]" if tab else url, posts))

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
    sys.exit(main())
