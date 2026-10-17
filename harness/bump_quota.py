"""Harness helper (not production): append a new quota reading to a running fixture session,
to watch a figure change in place, e.g. 99% then 100% for the ribbon's value slots.

Usage:  python3 harness/bump_quota.py <fixtureDir> <edge role | session id> <used percent>
        (fixtureDir is printed by harness/serve.py; edge roles as in --edge, e.g. low)

It copies the session's last token_count that carried rate limits, sets the primary window's
used percent, stamps it now and appends it; the server picks it up on its next read.
"""

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
from fixture_builder import EDGE_IDS, fixture_uuid, iso  # noqa: E402


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    fixture_dir, role, used = argv[0], argv[1], float(argv[2])
    sid = fixture_uuid(EDGE_IDS[role]) if role in EDGE_IDS else role
    paths = glob.glob(os.path.join(fixture_dir, "**", f"rollout-*-{sid}.jsonl"), recursive=True)
    if not paths:
        sys.exit(f"no log for {role} ({sid}) under {fixture_dir}")
    path = paths[0]
    last, ordinal = None, 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            obj = json.loads(line)
            ordinal = max(ordinal, obj.get("ordinal", 0))
            p = obj.get("payload") or {}
            if p.get("type") == "token_count" and p.get("rate_limits"):
                last = obj
    if last is None:
        sys.exit(f"{role} has no quota reading to continue")
    now = time.time()
    last["timestamp"] = iso(int(now * 1000))
    last["ordinal"] = ordinal + 1
    last["payload"]["rate_limits"]["primary"]["used_percent"] = used
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(last, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{role}: primary used {used:g}% at {time.strftime('%H:%M:%S', time.localtime(now))}")


if __name__ == "__main__":
    main(sys.argv[1:])
