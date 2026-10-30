"""Telemetry parsing and throughput math on SYNTHETIC fixtures only."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

from fixture_builder import Rollout, fixture_uuid, standard_response  # noqa: E402
from desk_status.discovery import SessionIndex  # noqa: E402
from desk_status.gitinfo import GitCache  # noqa: E402
from desk_status.status import StatusService  # noqa: E402
from desk_status.telemetry import SessionLog  # noqa: E402


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rollout-x.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, r):
        write(self.path, r.text())
        log = SessionLog(self.path)
        log.refresh()
        return log

    def throughput(self, log):
        return StatusService._throughput(log.state)

    def test_sample_duration_spans_first_reasoning_item_to_usage(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        r.advance(500)
        standard_response(r, 2_000, 3_000, out=500, inp=1_000, cached=0, tail_wait_ms=300)
        log = self.load(r)
        s = log.state.samples[-1]
        self.assertTrue(s["valid"])
        self.assertEqual(s["durationMs"], 2_000 + 3_000 + 300)
        self.assertAlmostEqual(s["tokensPerSecond"], 500 / 5.3, places=6)

    def test_tool_wait_between_responses_is_excluded(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        standard_response(r, 1_000, 1_000, out=100, inp=1_000, cached=0, tail_wait_ms=0)
        r.tool_call(60_000)  # long tool execution between responses
        r.advance(1_000)
        standard_response(r, 500, 1_500, out=200, inp=2_000, cached=0, tail_wait_ms=0)
        log = self.load(r)
        a, b = list(log.state.samples)[-2:]
        self.assertEqual(a["durationMs"], 2_000)
        self.assertEqual(b["durationMs"], 2_000)  # not 63 s: no off-by-one across the tool call

    def test_weighted_last_five_same_model_and_effort(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn(model="m", effort="high")
        specs = [(1_000, 100), (2_000, 100), (4_000, 900), (1_000, 50), (3_000, 300), (2_000, 400)]
        for dur, out in specs:
            r.advance(100)
            standard_response(r, 0, dur, out=out, inp=1_000, cached=0, tail_wait_ms=0)
        r.turn_context("m", "low")  # same turn switches effort: excluded from the group
        r.advance(100)
        standard_response(r, 0, 1_000, out=5_000, inp=1_000, cached=0, tail_wait_ms=0)
        r.turn_context("m", "high")
        log = self.load(r)
        t = self.throughput(log)
        last5 = specs[-5:]
        expected = sum(o for _, o in last5) * 1000.0 / sum(d for d, _ in last5)
        self.assertEqual(t["aggregate"]["samples"], 5)
        self.assertAlmostEqual(t["aggregate"]["tokensPerSecond"], expected, places=6)
        mean = sum(o * 1000.0 / d for d, o in last5) / 5
        self.assertNotAlmostEqual(t["aggregate"]["tokensPerSecond"], mean, places=2)

    def test_duplicate_usage_and_token_count_are_not_double_counted(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        rid = standard_response(r, 0, 2_000, out=100, inp=1_000, cached=0)
        r.usage(1_000, 0, 100, response_id=rid)  # duplicate record for the same response
        r.token_count({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        log = self.load(r)
        self.assertEqual(len(log.state.samples), 1)
        self.assertEqual(log.state.counters["duplicateUsage"], 1)

    def test_speed_and_cost_follow_each_applied_service_tier(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn(model="gpt-6.1-sol", effort="high")
        for tier, output in (("default", 100), ("priority", 200), ("fast", 200), ("ultrafast", 600)):
            r.thread_settings("gpt-6.1-sol", "high", tier)
            standard_response(r, 0, 1000, out=output, inp=1000, cached=0, tail_wait_ms=0)
        log = self.load(r)
        self.assertEqual([s["serviceTier"] for s in log.state.samples],
                         ["default", "priority", "fast", "ultrafast"])
        t = self.throughput(log)
        self.assertEqual(t["serviceTier"], "ultrafast")
        self.assertEqual(t["aggregate"]["samples"], 1)
        self.assertEqual(t["aggregate"]["tokensPerSecond"], 600)
        self.assertEqual([s["matches"] for s in t["recent"]], [False, False, False, True])
        self.assertAlmostEqual(log.state.samples[-1]["cost"], .012 + .036)
        # priority and fast name the same speed mode and should share a sample group.
        r.thread_settings("gpt-6.1-sol", "high", "priority")
        t = self.throughput(self.load(r))
        self.assertEqual(t["aggregate"]["samples"], 2)
        self.assertEqual(t["aggregate"]["tokensPerSecond"], 200)

    def test_tier_switch_without_response_keeps_previous_speed_label(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn(model="gpt-6-astra", effort="high")
        r.thread_settings("gpt-6-astra", "high", "default")
        standard_response(r, 0, 1000, out=100, inp=1000, cached=0, tail_wait_ms=0)
        r.thread_settings("gpt-6-astra", "high", "ultrafast")
        t = self.throughput(self.load(r))
        self.assertFalse(t["current"])
        self.assertEqual(t["serviceTier"], "default")
        self.assertEqual(t["aggregate"]["tokensPerSecond"], 100)

    def test_late_usage_retains_its_turns_service_tier(self):
        r = Rollout(fixture_uuid(1))
        old = r.start_turn(model="gpt-6.1-terra", effort="high")
        r.thread_settings("gpt-6.1-terra", "high", "default")
        r.start_turn(model="gpt-6-astra", effort="high")
        r.thread_settings("gpt-6-astra", "high", "ultrafast")
        r.item("AgentMessage", 1000, turn_id=old)
        r.usage(1000, 0, 100, turn_id=old)
        s = self.load(r).state.samples[-1]
        self.assertEqual((s["model"], s["serviceTier"]), ("gpt-6.1-terra", "default"))
        self.assertAlmostEqual(s["cost"], .0015)

    def test_explicit_turn_context_tier_overrides_previous_setting(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn(model="gpt-6.1-sol", effort="high")
        r.thread_settings("gpt-6.1-sol", "high", "ultrafast")
        r._emit("turn_context", {"turn_id": r.turn_id, "model": "gpt-6.1-sol", "effort": "high", "service_tier": None})
        standard_response(r, 0, 1000, out=100, inp=1000, cached=0, tail_wait_ms=0)
        s = self.load(r).state.samples[-1]
        self.assertIsNone(s["serviceTier"])
        self.assertAlmostEqual(s["cost"], .003)

    def test_tool_only_response_has_no_timing(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        r.tool_call(500)
        r.usage(1_000, 0, 40)
        log = self.load(r)
        s = log.state.samples[-1]
        self.assertFalse(s["valid"])
        self.assertEqual(s["invalidReason"], "no_timing")

    def test_late_item_from_previous_response_is_not_attributed(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        t0 = r.t
        standard_response(r, 0, 1_000, out=50, inp=100, cached=0, tail_wait_ms=0)
        r.advance(200)
        r.item("AgentMessage", 300, start_ms=t0 + 100)  # started before the boundary
        r.advance(500)
        r.usage(100, 0, 30)
        log = self.load(r)
        self.assertEqual(log.state.samples[-1]["invalidReason"], "no_timing")
        self.assertGreaterEqual(log.state.counters["staleItems"], 1)

    def test_partial_line_is_completed_on_next_refresh(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        standard_response(r, 1_000, 1_000, out=100, inp=1_000, cached=0)
        text = r.text()
        cut = len(text) - 40
        write(self.path, text[:cut])
        log = SessionLog(self.path)
        log.refresh()
        before = len(log.state.samples)
        self.assertGreater(log.pending_tail_bytes, 0)
        write(self.path, text)
        log.refresh()
        self.assertEqual(log.pending_tail_bytes, 0)
        self.assertGreaterEqual(len(log.state.samples), before)
        self.assertEqual(log.state.counters["badLines"], 0)

    def test_malformed_lines_are_counted_and_skipped(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        r.raw('{"timestamp": "2026-09-22T10:00:00Z", "type": "event_msg", "payload": {"type": "tok')
        r.raw("not json at all")
        standard_response(r, 1_000, 1_000, out=100, inp=1_000, cached=0)
        log = self.load(r)
        self.assertGreaterEqual(log.state.counters["badLines"], 2)
        self.assertTrue(log.state.samples[-1]["valid"])

    def test_unchanged_log_reads_zero_new_bytes_and_rotation_resets(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        standard_response(r, 1_000, 1_000, out=100, inp=1_000, cached=0)
        log = self.load(r)
        off = log.offset
        log.refresh()
        self.assertEqual(log.offset, off)
        r2 = Rollout(fixture_uuid(2))
        r2.start_turn()
        write(self.path, r2.text())  # smaller, different content: rotation
        log.refresh()
        self.assertEqual(log.resets, 1)
        self.assertEqual(log.state.session_id, fixture_uuid(2))

    def test_turn_status(self):
        r = Rollout(fixture_uuid(1))
        r.start_turn()
        r.abort_turn()
        log = self.load(r)
        st = log.state
        self.assertEqual(st.turns[st.current_turn_id]["status"], "interrupted")


class ServiceFixtureTest(unittest.TestCase):
    """Rate-limit windows come from data, not assumptions."""

    def test_rate_limit_window_from_data(self):
        from fixture_builder import BASE_MS, rate_limits
        with tempfile.TemporaryDirectory() as root:
            r = Rollout(fixture_uuid(1))
            r.start_turn()
            standard_response(r, 0, 1_000, out=10, inp=100, cached=0,
                              rate_limits=rate_limits(40.0, 10080, BASE_MS + 86400_000))
            r.write(root)
            svc = StatusService(SessionIndex([root]), GitCache(enabled=False), clock=lambda: BASE_MS / 1000 + 3600)
            snap = svc.snapshot(manual_id=r.id)
            w = snap["rateLimits"]["windows"][0]
            self.assertEqual(w["windowMinutes"], 10080)
            self.assertEqual(w["usedPercent"], 40.0)
            self.assertFalse(w["expired"])
            svc2 = StatusService(SessionIndex([root]), GitCache(enabled=False), clock=lambda: BASE_MS / 1000 + 2 * 86400)
            self.assertTrue(svc2.snapshot(manual_id=r.id)["rateLimits"]["windows"][0]["expired"])


if __name__ == "__main__":
    unittest.main()
