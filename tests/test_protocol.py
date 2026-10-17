"""Real stdio JSON-RPC against the server process (SYNTHETIC fixture roots)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

from fixture_builder import build_harness_fixtures  # noqa: E402
from desk_status import UI_RESOURCE_URI  # noqa: E402

SERVER = os.path.join(ROOT, "server", "desk_status_server.py")


class StdioServer:
    def __init__(self, roots):
        argv = [sys.executable, "-B", SERVER, "--no-git", "--log-level", "error"]
        for r in roots:
            argv += ["--sessions-root", r]
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def send_raw(self, data: bytes):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def read(self):
        line = self.p.stdout.readline()
        return json.loads(line)

    def call(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw((json.dumps(msg) + "\n").encode())
        return self.read()

    def close(self):
        self.p.stdin.close()
        out = self.p.stdout.read()
        self.p.wait(timeout=5)
        self.p.stderr.close()
        self.p.stdout.close()
        return out


class ProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="desk-status-proto-")
        cls.a, cls.b = os.path.join(cls.tmp, "a"), os.path.join(cls.tmp, "b")
        cls.ids = build_harness_fixtures(cls.a, cls.b, int(time.time() * 1000))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.s = StdioServer([self.a, self.b])
        r = self.s.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.init = r["result"]
        self.s.send_raw(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    def tearDown(self):
        trailing = self.s.close()
        self.assertEqual(trailing, b"", "notifications must not produce output")

    def test_initialize(self):
        self.assertEqual(self.init["protocolVersion"], "2025-11-25")
        self.assertEqual(self.init["serverInfo"]["name"], "desk-status")
        self.assertIn("tools", self.init["capabilities"])

    def test_tools_list_metadata(self):
        tools = {t["name"]: t for t in self.s.call("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), {"open_status", "read_status"})
        op = tools["open_status"]["_meta"]
        self.assertEqual(op["ui"]["resourceUri"], UI_RESOURCE_URI)
        self.assertEqual(op["ui"]["visibility"], ["app"])
        self.assertEqual(op["openai/ui"]["entrypoints"], [{"type": "global"}, {"type": "thread"}])
        rd = tools["read_status"]
        self.assertEqual(rd["_meta"]["ui"]["visibility"], ["app"])
        self.assertTrue(rd["annotations"]["readOnlyHint"])
        self.assertFalse(rd["inputSchema"]["additionalProperties"])

    def test_resource_is_self_contained(self):
        res = self.s.call("resources/list")["result"]["resources"]
        self.assertEqual(res[0]["mimeType"], "text/html;profile=mcp-app")
        c = self.s.call("resources/read", {"uri": UI_RESOURCE_URI})["result"]["contents"][0]
        self.assertEqual(c["mimeType"], "text/html;profile=mcp-app")
        html = c["text"]
        self.assertIsNone(re.search(r"https?://(?!www\.w3\.org/2000/svg)", html))
        self.assertIsNone(re.search(r"\.innerHTML\b|insertAdjacentHTML", html))
        self.assertEqual(c["_meta"]["ui"]["csp"], {"connectDomains": [], "resourceDomains": []})
        err = self.s.call("resources/read", {"uri": "ui://desk-status/../secret"})["error"]
        self.assertEqual(err["code"], -32002)

    def test_open_status_binds_from_explicit_meta(self):
        r = self.s.call("tools/call", {"name": "open_status", "arguments": {}, "_meta": {"threadId": self.ids["alpha"]}})["result"]
        self.assertEqual(r["structuredContent"]["binding"], "host")
        self.assertEqual(r["_meta"]["desk-status/snapshot"]["session"]["id"], self.ids["alpha"])
        r = self.s.call("tools/call", {"name": "open_status", "arguments": {}, "_meta": {"parentThreadId": self.ids["alpha"]}})["result"]
        self.assertEqual(r["structuredContent"]["binding"], "none")

    def test_read_status_and_seq_echo(self):
        r = self.s.call("tools/call", {"name": "read_status", "arguments": {"sessionId": self.ids["beta"], "seq": 7}})["result"]
        sc = r["structuredContent"]
        self.assertEqual(sc["request"]["seq"], 7)
        self.assertEqual(sc["binding"]["state"], "manual")

    def test_path_traversal_and_bad_arguments_rejected(self):
        for args in ({"sessionId": "../../etc/passwd"}, {"sessionId": "/home/x/.codex/sessions/a.jsonl"},
                     {"hostSessionId": "..\\..\\x"}, {"path": "/etc/passwd"}, {"seq": -1}, {"includeSessions": "yes"}):
            r = self.s.call("tools/call", {"name": "read_status", "arguments": args})["result"]
            self.assertTrue(r.get("isError"), args)
            self.assertNotIn("structuredContent", r)

    def test_protocol_errors(self):
        self.assertEqual(self.s.call("tools/call", {"name": "nope", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.s.call("does/not/exist")["error"]["code"], -32601)
        self.s.send_raw(b"{not json\n")
        self.assertEqual(self.s.read()["error"]["code"], -32700)
        self.s.send_raw(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]\n')
        self.assertEqual(self.s.read()["error"]["code"], -32600)
        self.assertEqual(self.s.call("ping")["result"], {})


class StdoutHygieneTest(unittest.TestCase):
    def test_every_stdout_line_is_json_rpc(self):
        with tempfile.TemporaryDirectory() as root:
            p = subprocess.run([sys.executable, "-B", SERVER, "--sessions-root", root, "--no-git"],
                               input=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                                     b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
                               capture_output=True, timeout=20)
            lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)
            for ln in lines:
                self.assertEqual(json.loads(ln)["jsonrpc"], "2.0")
            self.assertTrue(re.search(rb"\[desk-status\]", p.stderr))


if __name__ == "__main__":
    unittest.main()
