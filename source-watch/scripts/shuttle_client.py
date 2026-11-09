#!/usr/bin/env python3
"""One MCP client for shuttle, over HTTP or stdio.

The container publishes the desktop as MCP over HTTP (`SHUTTLE_MCP_TOKEN`, port 8765) and also
speaks MCP over stdio (`shuttle mcp`). Host tools import this module so the same command works
from the host and from inside the container:

    http        POST JSON-RPC to http://127.0.0.1:8765/mcp with a bearer token
    docker      `docker exec -i <container> shuttle mcp`, from the host without HTTP
    local       `shuttle mcp` as a child, inside the container

Pick one with --transport or SHUTTLE_TRANSPORT; auto tries http, then docker, then local.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")
HOST_DEFAULT = "http://127.0.0.1:8765/mcp"


def env_token():
    token = os.environ.get("SHUTTLE_MCP_TOKEN", "").strip()
    if token:
        return token
    # 从本文件向上找 .env：模块可能住在 source-watch/scripts/ 或 bin/ 底下。
    directory = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        try:
            with open(os.path.join(directory, ".env"), encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("SHUTTLE_MCP_TOKEN="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        directory = os.path.dirname(directory)
    return ""


def env_url():
    if os.environ.get("SHUTTLE_MCP_URL"):
        return os.environ["SHUTTLE_MCP_URL"]
    port = os.environ.get("MCP_HTTP_PORT") or os.environ.get("MCP_PORT")
    return f"http://127.0.0.1:{port}/mcp" if port else HOST_DEFAULT


class Http:
    """Streamable HTTP: one POST per request, JSON or SSE in the answer."""

    name = "http"

    def __init__(self, url=None, token=None, timeout=120):
        self.url = url or env_url()
        self.token = token if token is not None else env_token()
        self.timeout = timeout
        self.next_id = 0
        self.session = None

    def _post(self, payload):
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            self.session = response.headers.get("Mcp-Session-Id") or self.session
            raw = response.read().decode("utf-8", "replace")
            kind = response.headers.get("Content-Type", "")
        if "text/event-stream" in kind:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return {}
        return json.loads(raw) if raw.strip() else {}

    def request(self, method, params=None):
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": self.next_id, "method": method,
                   "params": params or {}}
        answer = self._post(payload)
        if "error" in answer:
            raise RuntimeError(f"{method}: {answer['error']}")
        return answer.get("result", {})

    def start(self):
        self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "shuttle-tools", "version": "0"},
        })
        try:                                  # tell the server we are ready
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        return self

    def close(self):
        return None


class Stdio:
    """A child `shuttle mcp`, either local or through docker exec."""

    name = "stdio"

    def __init__(self, command):
        self.command = command
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        rid = self.next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(self.proc.stderr.read(4000).strip() or "stdio closed")
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if message.get("id") == rid:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def start(self):
        self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "shuttle-tools", "version": "0"},
        })
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                          "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        return self

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Client:
    """Wraps the transport and the two tools these scripts use."""

    def __init__(self, transport="auto", url=None, token=None, container=None):
        self.transport = self._connect(transport, url, token, container or CONTAINER)
        self.backend.start()

    def _connect(self, transport, url, token, container):
        order = [transport] if transport and transport != "auto" else ["http", "docker", "local"]
        problems = []
        for kind in order:
            try:
                if kind == "http":
                    return self._wrap(Http(url, token).start())
                if kind == "docker":
                    if not shutil.which("docker"):
                        raise RuntimeError("docker not on PATH")
                    return self._wrap(Stdio(["docker", "exec", "-i", container,
                                             "shuttle", "mcp"]).start())
                if kind == "local":
                    if not shutil.which("shuttle"):
                        raise RuntimeError("shuttle not on PATH")
                    return self._wrap(Stdio(["shuttle", "mcp"]).start())
                raise RuntimeError(f"unknown transport {kind!r}")
            except Exception as error:
                problems.append(f"{kind}: {error}")
        raise RuntimeError("no MCP transport worked — " + "; ".join(problems))

    def _wrap(self, backend):
        self.backend = backend
        return backend

    @property
    def name(self):
        return self.backend.name

    def tool(self, name, arguments=None):
        result = self.backend.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "".join(part.get("text", "") for part in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(f"{name}: {text}")
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    def desk(self, verb, **args):
        """One desk verb; values keep the types the wire expects."""
        return self.tool("desk", {"verb": verb, "args": args})

    def close(self):
        self.backend.close()


def typed(value):
    """A command line value as the type the desk wire expects."""
    if isinstance(value, bool):
        return value
    text = str(value)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text[:1] in ("[", "{") or text in ("null", "true", "false"):
        try:                                  # `args=[...]` is a list on the wire, not a string
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def desk_call(client, *args):
    """CLI-shaped `desk VERB k=v ...` calls mapped onto the MCP desk tool."""
    if not args:
        raise RuntimeError("desk_call needs a verb")
    verb, rest = args[0], args[1:]
    kwargs = {}
    for item in rest:
        key, _, value = item.partition("=")
        kwargs[key] = typed(value)
    return client.desk(verb, **kwargs)


def main():
    """Small smoke test: scripts/shuttle_client.py [--transport http|docker|local] status"""
    import argparse
    ap = argparse.ArgumentParser(description="check which MCP transport works")
    ap.add_argument("--transport", default="auto",
                    choices=("auto", "http", "docker", "local"))
    ap.add_argument("--url")
    ap.add_argument("--token")
    args = ap.parse_args()

    client = Client(args.transport, args.url, args.token)
    try:
        status = client.tool("status")
        print(f"transport: {client.name}")
        print(json.dumps(status, ensure_ascii=False)[:400])
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
