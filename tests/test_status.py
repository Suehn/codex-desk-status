"""Binding resolution and metric honesty — regressions for review findings 1–3.
SYNTHETIC fixtures only."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

from fixture_builder import Rollout, build_harness_fixtures, fixture_uuid, standard_response, usage_wo_cache  # noqa: E402
from desk_status.discovery import SessionIndex  # noqa: E402
from desk_status.gitinfo import GitCache  # noqa: E402
from desk_status.status import StatusService, extract_host_candidates  # noqa: E402

FOREIGN = "abcdef01-2345-4678-89ab-cdef01234567"  # valid UUID, not local


class StatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="desk-status-test-")
        cls.a = os.path.join(cls.tmp, "a")
        cls.b = os.path.join(cls.tmp, "b")
        cls.ids = build_harness_fixtures(cls.a, cls.b, int(time.time() * 1000))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def svc(self):
        return StatusService(SessionIndex([self.a, self.b]), GitCache(enabled=False))

    # ---- finding 1: explicit current-thread keys only, ambiguity unbound ----
    def test_related_and_parent_ids_never_bind(self):
        meta = {"relatedThreadId": self.ids["beta"], "parentThreadId": self.ids["alpha"],
                "sourceSessionId": self.ids["legacy"], "conversation": self.ids["dup"]}
        snap = self.svc().snapshot(host_meta=meta)
        self.assertEqual(snap["binding"]["host"]["status"], "absent")
        self.assertEqual(snap["binding"]["state"], "none")
        self.assertIsNone(snap["session"])

    def test_nested_turn_metadata_parent_is_ignored(self):
        meta = {"x-codex-turn-metadata": json.dumps({"parent_thread_id": self.ids["beta"]})}
        self.assertEqual(self.svc().snapshot(host_meta=meta)["binding"]["state"], "none")
        meta = {"x-codex-turn-metadata": json.dumps({"thread_id": self.ids["beta"]})}
        snap = self.svc().snapshot(host_meta=meta)
        self.assertEqual(snap["binding"]["state"], "host")
        self.assertEqual(snap["binding"]["sessionId"], self.ids["beta"])

    def test_explicit_thread_key_binds(self):
        snap = self.svc().snapshot(host_meta={"threadId": self.ids["alpha"].upper()})
        self.assertEqual(snap["binding"]["state"], "host")
        self.assertEqual(snap["session"]["id"], self.ids["alpha"])

    def test_conflicting_current_thread_keys_are_ambiguous(self):
        meta = {"threadId": self.ids["alpha"], "openai/threadId": self.ids["beta"]}
        snap = self.svc().snapshot(host_meta=meta)
        self.assertEqual(snap["binding"]["host"]["status"], "ambiguous")
        self.assertEqual(snap["binding"]["state"], "none")
        self.assertIn("host_id_ambiguous", snap["warnings"])
        self.assertIsNone(snap["session"])

    def test_agreeing_keys_bind(self):
        meta = {"threadId": self.ids["alpha"], "codex/threadId": self.ids["alpha"]}
        self.assertEqual(self.svc().snapshot(host_meta=meta)["binding"]["state"], "host")

    def test_nonlocal_host_id_is_not_overridden_by_relayed_hint(self):
        snap = self.svc().snapshot(host_meta={"threadId": FOREIGN}, host_hint=self.ids["alpha"])
        self.assertEqual(snap["binding"]["host"]["status"], "unmatched")
        self.assertEqual(snap["binding"]["state"], "none")

    def test_relayed_hint_binds_when_host_says_nothing(self):
        snap = self.svc().snapshot(host_hint=self.ids["alpha"])
        self.assertEqual(snap["binding"]["host"]["source"], "app-relay")
        self.assertEqual(snap["binding"]["state"], "host")

    def test_extract_ignores_unknown_uuid_keys(self):
        cands, keys = extract_host_candidates({"randomKey": self.ids["alpha"], "threadId": "not-a-uuid"})
        self.assertEqual(cands, [])
        self.assertIn("randomKey", keys)

    def test_manual_wins_and_missing_manual(self):
        svc = self.svc()
        snap = svc.snapshot(manual_id=self.ids["legacy"], host_meta={"threadId": self.ids["alpha"]})
        self.assertEqual(snap["binding"]["state"], "manual")
        self.assertEqual(snap["session"]["id"], self.ids["legacy"])
        self.assertEqual(snap["binding"]["host"]["sessionId"], self.ids["alpha"])
        snap = svc.snapshot(manual_id=FOREIGN)
        self.assertEqual(snap["binding"]["state"], "manual_missing")
        self.assertIsNone(snap["session"])

    # ---- finding 2: missing cache is not 0% ----
    def test_missing_cached_tokens_is_not_zero(self):
        snap = self.svc().snapshot(manual_id=self.ids["nocache"])
        latest = snap["session"]["cache"]["latest"]
        self.assertEqual(latest["state"], "missing_cached")
        self.assertIsNone(latest["cachedInputTokens"])
        self.assertIsNone(latest["ratio"])
        recent = snap["session"]["cache"]["recent"]
        self.assertEqual(recent["responses"], 0)
        self.assertIsNone(recent["ratio"])

    def test_true_zero_cache_is_zero(self):
        snap = self.svc().snapshot(manual_id=self.ids["hostile"])
        latest = snap["session"]["cache"]["latest"]
        self.assertEqual(latest["state"], "ok")
        self.assertEqual(latest["cachedInputTokens"], 0)
        self.assertEqual(latest["ratio"], 0.0)

    def test_recent_cache_counts_only_complete_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            r = Rollout(fixture_uuid(11))
            r.start_turn()
            standard_response(r, 0, 1_000, out=10, inp=1_000, cached=900)
            r.item("AgentMessage", 1_000).advance(10)
            usage_wo_cache(r, inp=50_000, out=10)
            standard_response(r, 0, 1_000, out=10, inp=1_000, cached=500)
            r.write(root)
            svc = StatusService(SessionIndex([root]), GitCache(enabled=False))
            recent = svc.snapshot(manual_id=r.id)["session"]["cache"]["recent"]
            self.assertEqual(recent["considered"], 3)
            self.assertEqual(recent["responses"], 2)
            self.assertEqual(recent["inputTokens"], 2_000)
            self.assertAlmostEqual(recent["ratio"], 1_400 / 2_000)

    # ---- finding 3: session_meta.id must match the filename ----
    def test_renamed_foreign_log_never_binds_as_host(self):
        rid = self.ids["renamed"]
        snap = self.svc().snapshot(host_meta={"threadId": rid})
        self.assertEqual(snap["binding"]["host"]["status"], "unmatched")
        self.assertEqual(snap["binding"]["state"], "none")
        snap = self.svc().snapshot(host_hint=rid)
        self.assertEqual(snap["binding"]["state"], "none")

    def test_renamed_foreign_log_manual_is_flagged(self):
        rid = self.ids["renamed"]
        snap = self.svc().snapshot(manual_id=rid, include_sessions=True)
        self.assertEqual(snap["session"]["id"], rid)
        self.assertTrue(snap["session"]["idMismatch"])
        self.assertIn("session_id_mismatch", snap["warnings"])
        row = [s for s in snap["sessions"] if s["id"] == rid][0]
        self.assertFalse(row["verified"])

    # ---- lists & context ----
    def test_session_list_hides_review_by_default(self):
        svc = self.svc()
        ids = [s["id"] for s in svc.snapshot(include_sessions=True)["sessions"]]
        self.assertNotIn(self.ids["review"], ids)
        self.assertIn(self.ids["alpha"], ids)
        ids = [s["id"] for s in svc.snapshot(include_sessions=True, all_kinds=True)["sessions"]]
        self.assertIn(self.ids["review"], ids)

    def test_context_uses_latest_response_not_cumulative(self):
        snap = self.svc().snapshot(manual_id=self.ids["alpha"])
        c = snap["session"]["context"]
        self.assertEqual(c["state"], "ok")
        self.assertEqual(c["windowTokens"], 258_400)
        self.assertAlmostEqual(c["ratio"], c["usedTokens"] / 258_400)

    def test_compacted_context_pending(self):
        c = self.svc().snapshot(manual_id=self.ids["beta"])["session"]["context"]
        self.assertEqual(c["state"], "compacted_pending")

    def test_duplicate_id_newer_copy_wins(self):
        s = self.svc().snapshot(manual_id=self.ids["dup"])["session"]
        self.assertEqual(s["project"], "dup-new")
        self.assertEqual(s["file"]["alternates"], 1)


if __name__ == "__main__":
    unittest.main()
