#!/usr/bin/env python3
"""Collect an X timeline through Firefox's accessibility tree.

Reads the visible posts (author, time, full text, engagement) from the AT-SPI tree and
scrolls a few times to gather more. No CDP, no JS injection: it is the same path an
agent uses through shuttle.

Usage: bin/x-timeline.py [--tab Following] [--screens 7]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")


def desk(*args):
    proc = subprocess.run(
        ["docker", "exec", CONTAINER, "shuttle", "desk", *args],
        capture_output=True, text=True,
    )
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(f"{args} failed: {proc.stderr.strip()}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


ARTICLE = re.compile(r'^- article "(.*)" \[id=([^\]]+)\]$', re.S)


def window_title():
    for w in desk("windows"):
        if "firefox" in str(w.get("app", "")):
            return w.get("title"), w.get("id")
    raise RuntimeError("no firefox window")


def find_tab(name):
    res = desk("find", f"name={name}")
    for node in res.get("found", []):
        if node.get("role") == "tab":
            return node["id"]
    return None


def articles(title):
    snap = desk("snapshot", f"window={title}", "text=true", "max_chars=80000")
    found = []
    for line in (snap.get("text") or "").splitlines():
        m = ARTICLE.match(line.strip())
        if m:
            found.append((m.group(2), m.group(1)))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", default="Following")
    ap.add_argument("--screens", type=int, default=7)
    args = ap.parse_args()

    title, _ = window_title()
    tab = find_tab(args.tab)
    if tab:
        print(f"按 {args.tab} 标签 ({tab})", file=sys.stderr)
        desk("press", f"id={tab}")
        time.sleep(6)

    seen = {}
    for i in range(args.screens):
        title, _ = window_title()
        got = articles(title)
        new = [a for a in got if a[0] not in seen]
        for aid, text in got:
            seen.setdefault(aid, text)
        print(f"第 {i+1} 屏: {len(got)} 条，新增 {len(new)} 条（累计 {len(seen)}）", file=sys.stderr)
        if i < args.screens - 1:
            desk("scroll", "direction=down", "amount=8", f"window={title}")
            time.sleep(3.5)

    out = []
    for aid, text in seen.items():
        out.append({"id": aid, "text": text})
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
