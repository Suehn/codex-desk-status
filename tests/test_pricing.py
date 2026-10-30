"""Money, retention and provenance: one price per request everywhere, nothing missing read as
zero or as certain, and a quota line continued only by a reading that belongs to it.
SYNTHETIC fixtures only."""

import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

from fixture_builder import build_edge_fixtures  # noqa: E402
from desk_status import pricing  # noqa: E402
from desk_status.discovery import SessionIndex  # noqa: E402
from desk_status.gitinfo import GitCache  # noqa: E402
from desk_status.status import StatusService  # noqa: E402
from desk_status.telemetry import SessionState  # noqa: E402


def total_cost(session):
    return sum(sum(b["cost"].values()) for b in session["usage"] if b["priced"])


class PricingRules(unittest.TestCase):
    def test_terra_61_rates_and_context_boundary(self):
        for tier, expected in (("default", (1, 0.05, 1.25, 5)),
                               ("priority", (2, 0.1, 2.5, 10))):
            with self.subTest(tier=tier):
                short = pricing.rate_for("gpt-6.1-terra", tier, 272_000)
                long = pricing.rate_for("gpt-6.1-terra", tier, 272_001)
                self.assertEqual(tuple(short[:4]), expected)
                self.assertEqual(tuple(long[:4]), (expected[0] * 2, expected[1] * 2,
                                                   expected[2] * 2, expected[3] * 1.5))
                self.assertFalse(short.long)
                self.assertTrue(long.long)

    def test_ultrafast_is_six_times_standard_including_cache_and_long_context(self):
        for model in ("gpt-6-astra", "gpt-6.1-sol"):
            for prompt in (272_000, 272_001):
                with self.subTest(model=model, prompt=prompt):
                    std = pricing.rate_for(model, "default", prompt)
                    ultra = pricing.rate_for(model, "ultrafast", prompt)
                    for base, accelerated in zip(std[:4], ultra[:4]):
                        self.assertAlmostEqual(accelerated, base * 6)
                    self.assertEqual(ultra.tier, "ultrafast")
                    self.assertEqual(ultra.long, std.long)
                    usage = {"input_tokens": prompt, "cached_input_tokens": 100_000,
                             "cache_write_input_tokens": 10_000, "output_tokens": 1000}
                    regular = pricing.request_cost(model, "default", usage)
                    accelerated = pricing.request_cost(model, "ultrafast", usage)
                    for part in ("read", "write", "uncached", "output"):
                        self.assertAlmostEqual(accelerated[part], regular[part] * 6)

    def test_unlisted_model_tiers_are_unpriced_instead_of_standard(self):
        for model, tier in (("gpt-6.1-terra", "ultrafast"), ("gpt-6-astra", "future-tier"),
                            ("gpt-5.1", "fast")):
            self.assertIsNone(pricing.rate_for(model, tier, 1000))
            self.assertIsNone(pricing.request_cost(model, tier, {"input_tokens": 1000, "output_tokens": 10}))

    def test_service_tier_aliases(self):
        self.assertEqual({pricing.speed_mode(t) for t in (None, "default", "standard")}, {"standard"})
        self.assertEqual(pricing.rate_for("gpt-6-astra", "priority", 1000),
                         pricing.rate_for("gpt-6-astra", "fast", 1000))
        self.assertEqual(pricing.speed_mode(" ULTRAFAST "), "ultrafast")

    def test_long_context_is_a_per_model_rule_on_each_request(self):
        self.assertTrue(pricing.is_long("gpt-6-astra", 300_000))
        self.assertFalse(pricing.is_long("gpt-6-astra", 272_000))
        # a model the table does not price as long context never is, however large the prompt
        self.assertFalse(pricing.is_long("gpt-5.2", 900_000))
        self.assertFalse(pricing.is_long("unknown-model", 900_000))
        r = pricing.rate_for("gpt-6-astra", None, 300_000)
        self.assertEqual((r.input, r.read, r.output, r.long), (20, 2, 75, True))

    def test_fast_tier_and_fallbacks(self):
        self.assertEqual(pricing.rate_for("gpt-6-astra", "priority", 1000).input, 20)
        # no Fast long-context rate listed: the Fast rate applies
        self.assertEqual(pricing.rate_for("gpt-5.5", "priority", 400_000).input, 12.5)
        # no write rate listed: writes bill as input
        self.assertEqual(pricing.rate_for("gpt-5.4", None, 1000).write, 2.5)
        self.assertIsNone(pricing.rate_for("fixture-model-a", None, 1000))

    def test_missing_cache_counts_are_an_assumption_and_flagged(self):
        c = pricing.request_cost("gpt-6-sol", None, {"input_tokens": 100_000, "output_tokens": 1000})
        self.assertTrue(c["cacheMissing"])
        self.assertFalse(c["usageMissing"])
        self.assertEqual(c["read"], 0)
        self.assertAlmostEqual(c["uncached"], 100_000 * 2 / 1e6)
        # not an upper bound: the same prompt written to the cache costs more on this model
        written = pricing.request_cost("gpt-6-sol", None, {"input_tokens": 100_000, "cached_input_tokens": 0,
                                                          "cache_write_input_tokens": 100_000, "output_tokens": 1000})
        self.assertGreater(sum(written[k] for k in ("read", "write", "uncached", "output")),
                           sum(c[k] for k in ("read", "write", "uncached", "output")))
        # the write count matters only where writes are priced apart from input
        self.assertTrue(pricing.request_cost("gpt-6-sol", None, {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 1})["cacheMissing"])
        self.assertFalse(pricing.request_cost("gpt-5.4", None, {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 1})["cacheMissing"])
        known = pricing.request_cost("gpt-6-sol", None, {"input_tokens": 100_000, "cached_input_tokens": 0,
                                                        "cache_write_input_tokens": 0, "output_tokens": 1000})
        self.assertFalse(known["cacheMissing"] or known["usageMissing"])

    def test_missing_input_or_output_prices_only_what_was_recorded(self):
        no_out = pricing.request_cost("gpt-6-sol", None, {"input_tokens": 100, "cached_input_tokens": 0, "cache_write_input_tokens": 0})
        self.assertTrue(no_out["usageMissing"])
        self.assertEqual(no_out["output"], 0)
        no_in = pricing.request_cost("gpt-6-sol", None, {"output_tokens": 10})
        self.assertTrue(no_in["usageMissing"])
        self.assertFalse(no_in["cacheMissing"])
        self.assertEqual(no_in["uncached"], 0)

    def test_buckets_sum_the_same_costs_as_the_requests(self):
        st = SessionState()
        st.model, st.effort = "gpt-6-astra", "high"
        small = {"input_tokens": 100_000, "cached_input_tokens": 50_000, "output_tokens": 1000}
        big = {"input_tokens": 300_000, "cached_input_tokens": 100_000, "output_tokens": 10_000}
        for u in (small, big):
            st._add_usage("gpt-6-astra", None, u, pricing.request_cost("gpt-6-astra", None, u))
        b = st.usage_by_model[("gpt-6-astra", "")]
        expected = sum(sum(v for k, v in pricing.request_cost("gpt-6-astra", None, u).items() if k in ("read", "write", "uncached", "output"))
                       for u in (small, big))
        self.assertAlmostEqual(sum(b["cost"].values()), expected, places=9)
        self.assertEqual(b["longResponses"], 1)


class EdgeSnapshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="desk-status-edge-")
        cls.now = int(time.time() * 1000)
        cls.ids = build_edge_fixtures(cls.tmp, cls.now)
        cls.svc = StatusService(SessionIndex([cls.tmp]), GitCache(enabled=False))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def snap(self, role):
        return self.svc.snapshot(manual_id=self.ids[role])

    def test_long_context_total_parts_and_line_agree(self):
        """The audit's case: two 300k requests at gpt-6-astra. Each is long context:
        200k uncached × $20 + 100k cached × $2 + 10k output × $75 = $4.95, twice."""
        s = self.snap("long")["session"]
        self.assertAlmostEqual(total_cost(s), 9.90, places=6)
        self.assertAlmostEqual(sum(r[1] for r in s["timeline"]), 9.90, places=6)
        self.assertEqual([r[2] for r in s["timeline"]], [1, 1])  # long context, nothing missing
        b = s["usage"][0]
        self.assertAlmostEqual(b["cost"]["uncached"], 8.0)
        self.assertAlmostEqual(b["cost"]["read"], 0.4)
        self.assertAlmostEqual(b["cost"]["output"], 1.5)
        self.assertEqual(b["longResponses"], 2)

    def test_cache_recent_counts_only_recorded_samples(self):
        s = self.snap("missing")["session"]
        r = s["cache"]["recent"]
        self.assertEqual((r["responses"], r["considered"]), (1, 5))
        self.assertAlmostEqual(r["ratio"], 1.0)
        self.assertEqual(sum(b["cacheMissing"] for b in s["usage"]), 4)
        self.assertEqual([r[2] for r in s["timeline"]], [2, 2, 2, 2, 0])

    def test_usage_gaps_are_counted_where_the_totals_are(self):
        s = self.snap("gaps")["session"]
        b = s["usage"][0]
        self.assertEqual((b["responses"], b["usageMissing"], b["cacheMissing"]), (4, 2, 1))
        self.assertEqual([r[2] for r in s["timeline"]], [0, 4, 4, 2])
        self.assertAlmostEqual(sum(r[1] for r in s["timeline"]), total_cost(s), places=9)

    def test_cache_estimate_is_two_conditional_scenarios(self):
        s = self.snap("long")["session"]
        e = s["cache"]["estimate"]
        self.assertFalse(e["switched"])
        self.assertEqual(e["prompt"], 310_000)
        self.assertTrue(e["long"])
        self.assertAlmostEqual(e["cold"], 310_000 * 20 / 1e6)
        self.assertAlmostEqual(e["warm"], (300_000 * 2 + 10_000 * 20) / 1e6)
        # tokens not read are input, or writes where they are written: each scenario is a range
        self.assertAlmostEqual(e["coldWritten"], 310_000 * 25 / 1e6)
        self.assertAlmostEqual(e["warmWritten"], (300_000 * 2 + 10_000 * 25) / 1e6)
        # the hit rate itself stays the measured one, however old the request
        self.assertAlmostEqual(s["cache"]["latest"]["ratio"], 1 / 3)

    def test_switch_of_model(self):
        s = self.snap("switched")["session"]
        e = s["cache"]["estimate"]
        self.assertTrue(e["switched"])
        self.assertIsNone(e["warm"])
        self.assertEqual((e["model"], e["ranOn"]), ("gpt-6-astra", "gpt-6.1-sol"))
        self.assertIn("stayWarm", e)
        # no reply on the new model yet: the speed is the previous model's, said so
        t = s["throughput"]
        self.assertFalse(t["current"])
        self.assertEqual((t["model"], t["effort"]), ("gpt-6.1-sol", "xhigh"))
        self.assertTrue(t["aggregate"]["samples"] >= 1)

    def test_speed_average_is_drawn_whole(self):
        """Six timed replies then fourteen with nothing to time: the last sixteen hold only two
        timed ones, yet the average is of five and every one of them is in the series drawn."""
        t = self.snap("untimed")["session"]["throughput"]
        self.assertEqual(t["aggregate"]["samples"], 5)
        self.assertEqual(sum(1 for r in t["recent"] if r["valid"]), 2)
        self.assertEqual(len(t["series"]), 6)
        self.assertEqual([x["inAggregate"] for x in t["series"]], [False] + [True] * 5)
        drawn = [x for x in t["series"] if x["inAggregate"]]
        rate = sum(x["outputTokens"] for x in drawn) * 1000 / sum(x["durationMs"] for x in drawn)
        self.assertAlmostEqual(rate, t["aggregate"]["tokensPerSecond"])  # Σtokens ÷ Σseconds
        self.assertFalse(t["latest"]["valid"])

    def test_quota_line_continued_only_by_the_same_limit(self):
        rl = self.snap("quota-joined")["rateLimits"]
        self.assertFalse(rl["fromSelectedSession"])
        self.assertTrue(rl["historyJoined"])
        last = rl["history"][-1]
        self.assertEqual(last["from"], rl["sourceSessionShortId"])
        self.assertEqual(last["t"], rl["at"])
        self.assertEqual(last["w"]["10080"], rl["windows"][0]["usedPercent"])

        rl = self.snap("quota-apart")["rateLimits"]
        self.assertFalse(rl["fromSelectedSession"])
        self.assertFalse(rl["historyJoined"])
        self.assertEqual(rl["limitId"], "fixture-limit-other")
        self.assertTrue(all("from" not in p for p in rl["history"]))

    def test_no_responses_is_no_data(self):
        s = self.snap("fresh")["session"]
        self.assertEqual(s["context"]["state"], "no_data")
        self.assertIsNone(s["cache"]["latest"])
        self.assertIsNone(s["throughput"]["aggregate"])
        self.assertEqual(s["usage"], [])

    def test_subagents_are_priced_the_same_way(self):
        s = self.snap("multi")["session"]
        self.assertEqual(len(s["children"]), 7)
        for c in s["children"]:
            for b in c["usage"]:
                self.assertIn("cost", b)
        unpriced = [c for c in s["children"] if any(not b["priced"] for b in c["usage"])]
        self.assertEqual(len(unpriced), 1)  # the long fixture model name is not in the table


if __name__ == "__main__":
    unittest.main()
