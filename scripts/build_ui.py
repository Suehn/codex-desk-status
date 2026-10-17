#!/usr/bin/env python3
"""Inline ui/src/panel.{css,js} into ui/dist/panel.html (single self-contained file).

Usage:
    python3 scripts/build_ui.py          # write ui/dist/panel.html
    python3 scripts/build_ui.py --check  # exit 1 if dist is out of date
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "ui", "src")
DIST = os.path.join(ROOT, "ui", "dist", "panel.html")

# Anything that would make the panel touch the network or load remote code.
FORBIDDEN = [
    (re.compile(r"https?://(?!www\.w3\.org/2000/svg)", re.I), "absolute http(s) URL"),
    (re.compile(r"@import", re.I), "CSS @import"),
    (re.compile(r"@font-face", re.I), "@font-face"),
    (re.compile(r"\bfetch\s*\("), "fetch()"),
    (re.compile(r"XMLHttpRequest"), "XMLHttpRequest"),
    (re.compile(r"WebSocket"), "WebSocket"),
    (re.compile(r"\.innerHTML\b|insertAdjacentHTML|outerHTML\s*="), "HTML string injection"),
    (re.compile(r"\beval\s*\(|new\s+Function\s*\("), "dynamic code"),
    (re.compile(r"<script[^>]+src=", re.I), "external script"),
    (re.compile(r"<link[^>]+href=", re.I), "external stylesheet/link"),
]


def read(name):
    with open(os.path.join(SRC, name), "r", encoding="utf-8") as fh:
        return fh.read()


def build():
    html, css, js = read("panel.html"), read("panel.css"), read("panel.js")
    if "</style" in css.lower():
        raise SystemExit("panel.css must not contain </style")
    if "</script" in js.lower():
        raise SystemExit("panel.js must not contain </script")
    if html.count("/*__CSS__*/") != 1 or html.count("/*__JS__*/") != 1:
        raise SystemExit("panel.html must contain exactly one /*__CSS__*/ and /*__JS__*/ placeholder")
    out = html.replace("/*__CSS__*/", css.strip()).replace("/*__JS__*/", js.strip())
    problems = [label for rx, label in FORBIDDEN if rx.search(out)]
    if problems:
        raise SystemExit("forbidden constructs in UI bundle: " + ", ".join(problems))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ns = ap.parse_args(argv)
    out = build()
    if ns.check:
        try:
            with open(DIST, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = None
        if current != out:
            print("ui/dist/panel.html is out of date; run scripts/build_ui.py", file=sys.stderr)
            return 1
        print("ui/dist/panel.html is up to date")
        return 0
    os.makedirs(os.path.dirname(DIST), exist_ok=True)
    with open(DIST, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print(f"wrote {os.path.relpath(DIST, ROOT)} ({len(out.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
