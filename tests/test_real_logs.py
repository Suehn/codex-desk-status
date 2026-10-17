"""Smoke test against the machine's REAL Codex logs (read-only).

Skipped when no local session roots exist, or when DESK_STATUS_SKIP_REAL=1.
Prints only counts/timings/shapes — never paths, ids, cwd or log content.
"""

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

from desk_status.discovery import SessionIndex, resolve_roots  # noqa: E402
from desk_status.gitinfo import GitCache  # noqa: E402
from desk_status.status import StatusService  # noqa: E402


@unittest.skipIf(os.environ.get("DESK_STATUS_SKIP_REAL") == "1", "disabled by env")
class RealLogSmoke(unittest.TestCase):
    def test_real_logs(self):
        roots, _ = resolve_roots([])
        roots = [r for r in roots if os.path.isdir(r)]
        if not roots:
            self.skipTest("no local Codex session roots")
        svc = StatusService(SessionIndex(roots), GitCache(enabled=False))
        t0 = time.perf_counter()
        snap = svc.snapshot(include_sessions=True)
        cold = (time.perf_counter() - t0) * 1000
        sessions = snap["sessions"] or []
        if not sessions:
            self.skipTest("no sessions in local roots")
        sid = sessions[0]["id"]
        t0 = time.perf_counter()
        s1 = svc.snapshot(manual_id=sid)
        first = (time.perf_counter() - t0) * 1000
        log = next(iter(svc._logs.values()))
        offset = log.offset
        t0 = time.perf_counter()
        s2 = svc.snapshot(manual_id=sid)
        warm = (time.perf_counter() - t0) * 1000
        sess = s2["session"]
        self.assertEqual(sess["id"], sid)
        self.assertIn(sess["context"]["state"], ("ok", "no_data", "no_window", "compacted_pending"))
        t = sess["throughput"]
        agg = t["aggregate"]
        if agg:
            self.assertLessEqual(agg["samples"], 5)
            self.assertAlmostEqual(agg["tokensPerSecond"], agg["outputTokens"] * 1000 / agg["durationMs"], places=6)
        # The session may still be growing; only assert monotonic reads.
        self.assertGreaterEqual(log.offset, offset)
        latest_cache = sess["cache"]["latest"]
        print(f"\n[real-log smoke] roots={len(roots)} listed={len(sessions)} cold_list={cold:.0f}ms "
              f"first_read={first:.0f}ms warm={warm:.1f}ms new_bytes_on_reread={log.offset - offset} "
              f"context={sess['context']['state']} valid_samples={t['validSamples']}/{t['totalSamples']} "
              f"agg_samples={agg['samples'] if agg else 0} cache={latest_cache['state'] if latest_cache else None} "
              f"rate_limits={'yes' if s1['rateLimits'] else 'no'} badLines={sess['diagnostics'].get('badLines')} "
              f"chained={sum(1 for f in svc.index._by_id.values() if len(f.segments) > 1)} "
              f"segments={sess['file']['segments']}")


if __name__ == "__main__":
    unittest.main()
