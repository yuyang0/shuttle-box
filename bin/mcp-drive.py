#!/usr/bin/env python3
"""Drive the container's desktop over shuttle's MCP server.

The transport is bin/shuttle-mcp.sh, so the container is started (or created from
docker-compose.yml) on demand, exactly as an MCP client would do it.

Usage:
  bin/mcp-drive.py tools
  bin/mcp-drive.py status
  bin/mcp-drive.py screen
  bin/mcp-drive.py windows
  bin/mcp-drive.py launch firefox
  bin/mcp-drive.py open-url https://www.baidu.com
  bin/mcp-drive.py shot ./shot.png
"""
import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSPORT = os.path.join(REPO, "bin", "shuttle-mcp.sh")
CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")


sys.path.insert(0, os.path.join(REPO, "source-watch", "scripts"))
from shuttle_client import Client                            # noqa: E402


class MCP:
    """The shared client, kept as a thin alias so the commands below stay unchanged."""

    def __init__(self, transport="auto"):
        self.client = Client(transport)

    def request(self, method, params=None):
        return self.client.backend.request(method, params)

    def tool(self, name, arguments):
        return self.client.tool(name, arguments)

    def desk(self, verb, **args):
        return self.client.desk(verb, **args)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools")
    sub.add_parser("status")
    sub.add_parser("screen")
    sub.add_parser("windows")

    p = sub.add_parser("launch")
    p.add_argument("app")

    p = sub.add_parser("open-url")
    p.add_argument("url")
    p.add_argument("--app", default=None)
    p.add_argument("--settle", type=float, default=1.0, help="pause between UI actions")

    p = sub.add_parser("shot")
    p.add_argument("out")
    p.add_argument("--window", default=None)

    args = ap.parse_args()

    with MCP() as mcp:
        if args.cmd == "tools":
            tools = mcp.request("tools/list")["tools"]
            for t in tools:
                print(t["name"])
        elif args.cmd == "status":
            print(json.dumps(mcp.tool("status", {}), ensure_ascii=False))
        elif args.cmd == "screen":
            print(json.dumps(mcp.desk("screen")))
        elif args.cmd == "windows":
            for w in mcp.desk("windows"):
                print(f"{w.get('app','?'):16} {str(w.get('title',''))[:60]}")
        elif args.cmd == "launch":
            print(mcp.desk("launch", app=args.app)["launched"])
        elif args.cmd == "open-url":
            win = ensure_browser(mcp, args.app)
            mcp.desk("focus", window=win["title"])
            time.sleep(args.settle)
            mcp.desk("key", keys="ctrl+l")
            time.sleep(args.settle)
            mcp.desk("type", text=args.url)
            time.sleep(args.settle)
            mcp.desk("key", keys="Return")
            win = browser_window(mcp, args.app)
            for _ in range(30):
                time.sleep(1)
                now = browser_window(mcp, args.app)
                if now and now["title"] != win["title"]:
                    break
            now = browser_window(mcp, args.app)
            print(now["title"] if now else "n/a")
        elif args.cmd == "shot":
            remote = f"/tmp/mcp-drive-{os.getpid()}.png"
            kwargs = {"path": remote}
            if args.window:
                kwargs["window"] = args.window
            info = mcp.desk("shot", **kwargs)
            out = os.path.abspath(args.out)
            subprocess.run(["docker", "cp", f"{CONTAINER}:{remote}", out], check=True)
            print(f"{info['width']}x{info['height']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
