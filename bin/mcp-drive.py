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


class MCP:
    def __init__(self):
        self.proc = subprocess.Popen(
            [TRANSPORT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 0

    def _send(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(self.proc.stderr.read(4000).strip() or "transport closed")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    def tool(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(f"{name}: {text}")
        return json.loads(text) if text else None

    def desk(self, verb, **args):
        return self.tool("desk", {"verb": verb, "args": args})

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def __exit__(self, *_exc):
        self.close()
        return False

    def __enter__(self):
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-drive", "version": "1.0.0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self


BROWSER_APPS = {"firefox-bin", "firefox"}


def browser_window(mcp, app=None):
    wanted = {app} if app else BROWSER_APPS
    for w in mcp.desk("windows"):
        if w.get("app") in wanted:
            return w
    return None


def ensure_browser(mcp, app=None, timeout=40):
    win = browser_window(mcp, app)
    if win:
        return win
    mcp.desk("launch", app=app or "firefox")
    for _ in range(timeout):
        time.sleep(1)
        win = browser_window(mcp, app)
        if win:
            return win
    raise RuntimeError(f"no {app} window appeared within {timeout}s")


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
