#!/usr/bin/env python3
"""Build clean ZIPs into dist/.

    desk-status-plugin-<v>.zip  installable plugin (runtime files only)
    desk-status-source-<v>.zip  full source incl. tests/harness/docs

Allowlist-based: caches, node_modules, VCS data, build output and any
*.jsonl (session logs) are never included. Fails if the UI bundle is stale.
"""

import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
from desk_status import __version__  # noqa: E402

PLUGIN_FILES = [
    "plugin.json", "mcp.json", ".mcp.json", ".codex-plugin/plugin.json",
    "README.md", "LICENSE", "CHANGELOG.md", "docs/metrics.md", "ui/dist/panel.html",
    "server/desk_status_server.py", "server/desk_status/",
    "assets/",
    # installer (package.py supplies its file list)
    "scripts/install.py", "scripts/package.py",
]
SOURCE_FILES = PLUGIN_FILES + [
    "ui/src/", "scripts/build_ui.py", "tests/", "harness/", "docs/",
    ".gitignore", ".agents/", ".github/",
]
EXCLUDE = re.compile(r"(^|/)(__pycache__|node_modules|\.git|dist|\.pytest_cache)(/|$)|\.pyc$|\.jsonl$|\.DS_Store$")


def expand(entries):
    out, seen = [], set()
    for e in entries:
        p = os.path.join(ROOT, e)
        if e.endswith("/"):
            for d, dirs, files in os.walk(p):
                dirs[:] = sorted(x for x in dirs if not EXCLUDE.search(x + "/"))
                for f in sorted(files):
                    rel = os.path.relpath(os.path.join(d, f), ROOT).replace(os.sep, "/")
                    if not EXCLUDE.search(rel) and rel not in seen:
                        seen.add(rel)
                        out.append(rel)
        elif os.path.isfile(p):
            if e not in seen:
                seen.add(e)
                out.append(e)
        else:
            raise SystemExit(f"missing required file: {e}")
    return out


def write_zip(name, files, prefix):
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    path = os.path.join(ROOT, "dist", name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            info = zipfile.ZipInfo(f"{prefix}/{rel}", date_time=(2026, 10, 1, 0, 0, 0))
            info.external_attr = (0o755 if rel.endswith(".py") and rel.startswith(("server/desk_status_server", "scripts/")) else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(ROOT, rel), "rb") as fh:
                z.writestr(info, fh.read())
    return path, len(files)


def main():
    if not os.path.isfile(os.path.join(ROOT, "scripts", "build_ui.py")):
        raise SystemExit("package.py needs the source tree (desk-status-source ZIP)")
    if subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_ui.py"), "--check"]).returncode:
        raise SystemExit("UI bundle out of date: run scripts/build_ui.py first")
    for name, files in (("plugin", expand(PLUGIN_FILES)), ("source", expand(SOURCE_FILES))):
        path, n = write_zip(f"desk-status-{name}-{__version__}.zip", files, "desk-status")
        print(f"{os.path.relpath(path, ROOT)}: {n} files, {os.path.getsize(path)} bytes")


if __name__ == "__main__":
    main()
