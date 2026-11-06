#!/usr/bin/env python3
"""Read the URLs behind X's links, which shuttle's wire does not carry.

shuttle exposes role, name, value, states and bounds of each accessibility node, but not
the URI of a link. AT-SPI has it anyway: every link implements `Hyperlink.GetURI`, and
pyatspi reaches it. That is how a post's permalink is recovered: the timestamp link points
at `https://x.com/<handle>/status/<id>`.

Runs inside the container, where the AT-SPI bus and Firefox live:

    docker exec -i shuttle-box python3 - < bin/x-links.py

Prints JSON: one object per post with its accessibility name, the permalink if any, and
every link it holds.
"""
import json
import os
import re
import subprocess
import sys

# A post permalink ends at the status id; /analytics, /photo/1 and friends do not qualify.
STATUS = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)(?:[?#].*)?$")
TIME = re.compile(r"\d+\s*(?:s|m|h|d)|(?:second|minute|hour|day)s?\s+ago|Just now", re.I)


def session_bus():
    """The desktop's session bus address; it changes on every container start."""
    uid = str(os.getuid())
    for proc in os.listdir("/proc"):
        if not proc.isdigit():
            continue
        try:
            if os.stat(f"/proc/{proc}").st_uid != int(uid):
                continue
            env = open(f"/proc/{proc}/environ", "rb").read().split(b"\0")
        except OSError:
            continue
        for field in env:
            if field.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                return field.split(b"=", 1)[1].decode()
    return None


def main():
    address = session_bus()
    if not address:
        sys.exit("no DBUS_SESSION_BUS_ADDRESS on any of this user's processes")
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = address

    import pyatspi

    app = next((a for a in pyatspi.Registry.getDesktop(0) if a.name == "Firefox"), None)
    if app is None:
        sys.exit("Firefox is not on the accessibility bus")

    def collect(root, roles, limit=20000):
        hits, stack, seen = [], [root], 0
        while stack and seen < limit:
            node = stack.pop()
            seen += 1
            try:
                if node.getRole() in roles:
                    hits.append(node)
            except Exception:
                pass
            try:
                stack.extend(list(node))
            except Exception:
                pass
        return hits

    documents = collect(app, {pyatspi.ROLE_DOCUMENT_WEB, pyatspi.ROLE_DOCUMENT_FRAME})
    posts = []
    for document in documents:
        for article in collect(document, {pyatspi.ROLE_ARTICLE}):
            links = []
            for link in collect(article, {pyatspi.ROLE_LINK}, limit=400):
                try:
                    uri = link.queryHyperlink().getURI(0)
                except Exception:
                    continue
                if uri:
                    links.append({"name": (link.name or "")[:60], "uri": uri})
            candidates = [l for l in links if STATUS.match(l["uri"])]
            timed = [l for l in candidates if TIME.search(l["name"])]
            permalink = (timed or candidates or [{}])[0].get("uri")
            posts.append({"name": (article.name or ""), "permalink": permalink, "links": links})

    json.dump({"bus": bool(address), "documents": len(documents),
               "posts": posts}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
