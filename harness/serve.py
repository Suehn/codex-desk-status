#!/usr/bin/env python3
"""MOCK HOST harness server — browser acceptance only, never production.

Builds SYNTHETIC fixture logs (tests/fixture_builder.py) in a temp dir, starts
the real desk-status MCP server over stdio against them, and serves:

    /host.html      mock MCP Apps host page (iframe + postMessage bridge)
    /mock-host.js   the mock host logic
    /panel.html     ui/dist/panel.html (the production bundle, unmodified)
    /fixtures.json  {role: session id} for the synthetic sessions
    POST /rpc       one JSON-RPC request forwarded to the MCP server

Usage:  python3 harness/serve.py [--port 8765] [--edge] [--git] [--real-logs]
--edge adds the boundary fixtures (tests/fixture_builder.build_edge_fixtures):
low values, long-context cost, missing cache counts, a model switch, many
subagents, a session with no responses, quota readings from another session;
with --git the multi-agent session's cwd is a throwaway repository.
--real-logs points the backend at the default local Codex roots instead of
fixtures (for a private manual look only; screenshots of it must not be
published).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fixture_builder import build_edge_fixtures, build_harness_fixtures, make_git_fixture  # noqa: E402

STATIC = {
    "/host.html": (os.path.join(HERE, "host.html"), "text/html; charset=utf-8"),
    "/mock-host.js": (os.path.join(HERE, "mock-host.js"), "text/javascript; charset=utf-8"),
    "/panel.html": (os.path.join(ROOT, "ui", "dist", "panel.html"), "text/html; charset=utf-8"),
}


class McpProcess:
    def __init__(self, argv):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=ROOT)
        self.lock = threading.Lock()
        self.next_id = 1
        self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {
            "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
            "clientInfo": {"name": "mock-host-harness", "version": "0"}})
        self.notify("notifications/initialized")

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        with self.lock:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()

    def request(self, method, params):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("MCP server exited")
                msg = json.loads(line)
                if msg.get("id") == rid:
                    return msg

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def make_handler(mcp: McpProcess, fixtures: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                path = "/host.html"
            if path == "/fixtures.json":
                return self._send(200, json.dumps(fixtures).encode(), "application/json")
            if path in STATIC:
                fp, ctype = STATIC[path]
                with open(fp, "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/rpc":
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            reply = mcp.request(req.get("method"), req.get("params") or {})
            self._send(200, json.dumps(reply, ensure_ascii=False).encode(), "application/json")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--real-logs", action="store_true")
    ap.add_argument("--git", action="store_true", help="enable read-only git status (default off for determinism)")
    ap.add_argument("--edge", action="store_true", help="add the boundary fixtures (see module doc)")
    ns = ap.parse_args(argv)
    server_argv = [sys.executable, "-B", os.path.join(ROOT, "server", "desk_status_server.py"), "--log-level", "error"]
    fixtures = {"mode": "real-logs"}
    tmp = None
    if not ns.real_logs:
        tmp = tempfile.mkdtemp(prefix="desk-status-fixture-")
        a, b = os.path.join(tmp, "root-a"), os.path.join(tmp, "root-b")
        now_ms = int(time.time() * 1000)
        fixtures = build_harness_fixtures(a, b, now_ms)
        fixtures["mode"] = "synthetic-fixture"
        server_argv += ["--sessions-root", a, "--sessions-root", b]
        if ns.edge:
            c = os.path.join(tmp, "root-edge")
            git_cwd = make_git_fixture(tmp) if ns.git else None
            fixtures.update(build_edge_fixtures(c, now_ms, git_cwd))
            server_argv += ["--sessions-root", c]
    if not ns.git:
        server_argv.append("--no-git")
    mcp = McpProcess(server_argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", ns.port), make_handler(mcp, fixtures))
    print(json.dumps({"url": f"http://127.0.0.1:{httpd.server_address[1]}/host.html", "fixtures": fixtures,
                      "fixtureDir": tmp}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mcp.close()


if __name__ == "__main__":
    main()
