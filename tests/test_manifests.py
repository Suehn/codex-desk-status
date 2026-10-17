"""Manifest shape and version consistency (stdlib only; mirrors the Agent
Plugins 1.0.0 plugin/mcp schema constraints we rely on)."""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
from desk_status import __version__  # noqa: E402

PLUGIN_KEYS = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords", "extensions"}
STDIO_KEYS = {"type", "command", "args", "env", "cwd"}


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return json.load(fh)


class ManifestTest(unittest.TestCase):
    def test_portable_plugin_json(self):
        p = load("plugin.json")
        self.assertEqual(p["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        self.assertLessEqual(set(p), PLUGIN_KEYS)
        self.assertRegex(p["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertEqual(p["version"], __version__)
        ui = p["extensions"]["com.openai"]["interface"]
        for key in ("composerIcon", "logo"):
            self.assertTrue(ui[key].startswith("./assets/"))
            self.assertTrue(os.path.isfile(os.path.join(ROOT, ui[key])))

    def test_portable_mcp_json(self):
        m = load("mcp.json")
        self.assertEqual(m["$schema"], "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json")
        self.assertEqual(set(m), {"$schema", "mcpServers"})
        s = m["mcpServers"]["desk-status"]
        self.assertLessEqual(set(s), STDIO_KEYS)
        self.assertEqual(s["type"], "stdio")
        self.assertRegex(s["cwd"], r"^(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$))")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, s["args"][-1])))

    def test_codex_compat_overlay_matches(self):
        root, compat = load("plugin.json"), load(".codex-plugin/plugin.json")
        self.assertEqual(compat["name"], root["name"])
        self.assertEqual(compat["version"], root["version"])
        self.assertEqual(compat["interface"], root["extensions"]["com.openai"]["interface"])
        legacy = load(compat["mcpServers"])
        self.assertEqual(legacy["mcpServers"]["desk-status"]["args"], load("mcp.json")["mcpServers"]["desk-status"]["args"])

    def test_ui_version_matches(self):
        with open(os.path.join(ROOT, "ui", "src", "panel.js"), encoding="utf-8") as fh:
            m = re.search(r'APP_INFO = \{ name: "desk-status-panel", version: "([^"]+)" \}', fh.read())
        self.assertEqual(m.group(1), __version__)


if __name__ == "__main__":
    unittest.main()
