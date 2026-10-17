#!/usr/bin/env python3
"""Stage desk-status into a local Codex plugin marketplace (safe by default).

    python3 scripts/install.py --marketplace DIR            # stage only
    python3 scripts/install.py --marketplace DIR --register # also run `codex plugin ...`
    python3 scripts/install.py --marketplace DIR --uninstall  # remove only this plugin

Staging copies the plugin's runtime files to DIR/plugins/desk-status and adds
(or updates) only the "desk-status" entry in DIR/.agents/plugins/marketplace.json;
other entries in an existing marketplace file are preserved. Nothing outside
DIR is touched unless --register is given, which runs:

    codex plugin marketplace add DIR --json
    codex plugin add desk-status@<marketplace-name> --json

Those commands write to $CODEX_HOME (default ~/.codex). Set CODEX_HOME to a
temporary directory to try it in isolation.

--uninstall deletes only DIR/plugins/desk-status and the desk-status entry in
the marketplace file; the file itself is deleted only when no entries remain.
Run `codex plugin remove ...` first (printed, or executed with --register).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from package import PLUGIN_FILES, expand  # noqa: E402

PLUGIN = "desk-status"
DEFAULT_MARKET = "desk-status-local"


def set_python(dest: str, python: str) -> None:
    """Point the staged MCP configs at a different interpreter command."""
    for rel in ("mcp.json", ".mcp.json"):
        path = os.path.join(dest, rel)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["mcpServers"][PLUGIN]["command"] = python
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")


def stage(market_dir: str, market_name: str, python: str = "python3") -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", python):
        raise SystemExit("--python must be an executable name on PATH, such as python3, python or py")
    market_dir = os.path.abspath(market_dir)
    dest = os.path.join(market_dir, "plugins", PLUGIN)
    src_real, dest_real = os.path.realpath(ROOT), os.path.realpath(dest)
    if (dest_real == src_real or dest_real.startswith(src_real + os.sep)
            or src_real.startswith(dest_real + os.sep)):
        raise SystemExit("refusing to stage into a directory overlapping the source tree")
    if os.path.islink(dest):
        raise SystemExit("refusing to replace a symlinked plugin directory")
    if os.path.isdir(dest):
        marker = os.path.join(dest, "plugin.json")
        try:
            with open(marker, encoding="utf-8") as fh:
                if json.load(fh).get("name") != PLUGIN:
                    raise ValueError
        except (OSError, ValueError):
            raise SystemExit(f"{dest} exists and is not a desk-status copy; not overwriting")
        shutil.rmtree(dest)
    for rel in expand(PLUGIN_FILES):
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), out)
    if python != "python3":
        set_python(dest, python)

    mpath = os.path.join(market_dir, ".agents", "plugins", "marketplace.json")
    os.makedirs(os.path.dirname(mpath), exist_ok=True)
    data = {"name": market_name, "interface": {"displayName": "本机插件"}, "plugins": []}
    if os.path.isfile(mpath):
        with open(mpath, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("plugins", [])
    entry = {
        "name": PLUGIN,
        "source": {"source": "local", "path": f"./plugins/{PLUGIN}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    data["plugins"] = [p for p in data["plugins"] if p.get("name") != PLUGIN] + [entry]
    tmp = mpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, mpath)
    return data["name"]


def unstage(market_dir: str) -> tuple:
    market_dir = os.path.abspath(market_dir)
    dest = os.path.join(market_dir, "plugins", PLUGIN)
    if os.path.isdir(dest):
        with open(os.path.join(dest, "plugin.json"), encoding="utf-8") as fh:
            if json.load(fh).get("name") != PLUGIN:
                raise SystemExit(f"{dest} is not a desk-status copy; not deleting")
        shutil.rmtree(dest)
    mpath = os.path.join(market_dir, ".agents", "plugins", "marketplace.json")
    name, empty = DEFAULT_MARKET, False
    if os.path.isfile(mpath):
        with open(mpath, encoding="utf-8") as fh:
            data = json.load(fh)
        name = data.get("name", name)
        data["plugins"] = [p for p in data.get("plugins", []) if p.get("name") != PLUGIN]
        empty = not data["plugins"]
        if empty:
            os.remove(mpath)
        else:
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
    return name, empty


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--marketplace", required=True, help="marketplace root directory to create/update")
    ap.add_argument("--name", default=DEFAULT_MARKET, help="marketplace name for a new marketplace file")
    ap.add_argument("--register", action="store_true", help="also run codex plugin marketplace add / plugin add")
    ap.add_argument("--codex", default="codex", help="codex executable")
    ap.add_argument("--python", default="python3", help="interpreter command written into the staged MCP config (Windows: python or py)")
    ap.add_argument("--uninstall", action="store_true", help="remove only this plugin from the marketplace directory")
    ns = ap.parse_args()
    if ns.uninstall:
        name, empty = unstage(ns.marketplace)
        cmds = [[ns.codex, "plugin", "remove", f"{PLUGIN}@{name}", "--json"]]
        if empty:
            cmds.append([ns.codex, "plugin", "marketplace", "remove", name, "--json"])
        print(f"removed {PLUGIN} from {os.path.abspath(ns.marketplace)}" + ("; marketplace file now empty and deleted" if empty else "; other entries kept"))
        if not ns.register:
            print("also run (writes to $CODEX_HOME):")
            for c in cmds:
                print("  " + " ".join(c))
            return 0
        for c in cmds:
            print("$ " + " ".join(c), flush=True)
            r = subprocess.run(c)
            if r.returncode:
                print(f"failed ({r.returncode}): {' '.join(c)}", file=sys.stderr)
                return r.returncode
        return 0
    name = stage(ns.marketplace, ns.name, ns.python)
    root = os.path.abspath(ns.marketplace)
    print(f"staged {PLUGIN} into {root} (marketplace '{name}')")
    cmds = [[ns.codex, "plugin", "marketplace", "add", root, "--json"],
            [ns.codex, "plugin", "add", f"{PLUGIN}@{name}", "--json"]]
    if not ns.register:
        print("next steps (writes to $CODEX_HOME, default ~/.codex):")
        for c in cmds:
            print("  " + " ".join(c))
        return 0
    for c in cmds:
        print("$ " + " ".join(c), flush=True)
        r = subprocess.run(c)
        if r.returncode:
            return r.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
