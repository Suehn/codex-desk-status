"""A thread written to more than one rollout file (0.2.3): rollout-<time>-<thread>.jsonl, then
rollout-<time>-<thread>_<segment>.jsonl whose session_meta names the thread and, carrying its
history on, a history_base (the file it carries on from, the byte offset and ordinal its history
ends at there). SYNTHETIC fixtures only, in the shape the real files have: every line has an
ordinal; a carried-on file's meta line has the base's end ordinal; the base file may hold an
interrupted turn after the hand-over, never repeated in the next file."""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

from fixture_builder import BASE_MS, Rollout, fixture_uuid, standard_response  # noqa: E402
from desk_status.discovery import SessionIndex  # noqa: E402
from desk_status.gitinfo import GitCache  # noqa: E402
from desk_status.status import StatusService  # noqa: E402
from desk_status.telemetry import SessionLog, parse_ts_ms  # noqa: E402

T = fixture_uuid(0x51)      # the thread
X = fixture_uuid(0x52)      # its second file's segment id
Y = fixture_uuid(0x53)      # a third file's
OTHER = fixture_uuid(0x54)  # another session, for ambiguity
H = 3600_000


def base_thread(start=BASE_MS):
    """Two answered turns on model a, then (after the hand-over point) an interrupted turn that
    still ran one request: the shape real base files end with."""
    r = Rollout(T, cwd="/fixture/projects/segmented", start_ms=start)
    for _ in range(2):
        r.start_turn(model="gpt-6.1-sol", effort="high", window=200_000)
        standard_response(r, 500, 2_000, out=200, inp=20_000, cached=10_000)
        r.complete_turn()
        r.advance(60_000)
    r.thread_settings("gpt-6-astra", "high")
    keep = len(r.lines)  # the history the next file carries on from ends here
    r.start_turn(model="gpt-6.1-sol", effort="high", window=200_000)
    standard_response(r, 400, 1_000, out=50, inp=21_000, cached=20_000)
    r.abort_turn()
    return r, keep


def next_file(base, keep, seg=X, start_gap=30_000, responses=3, inp=60_000):
    s = base.carry_on(seg, keep, start_ms=base.t + start_gap, cwd="/fixture/projects/segmented")
    s.thread_settings("gpt-6-astra", "medium")
    for i in range(responses):
        s.start_turn(model="gpt-6-astra", effort="medium", window=258_400)
        standard_response(s, 600, 3_000, out=300 + i, inp=inp + 1_000 * i, cached=inp // 2, window=258_400)
        s.complete_turn()
        s.advance(30_000)
    return s


def usage_records(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            o = json.loads(line)
            if o["type"] == "token_usage_record":
                out.append(o["payload"]["response_id"])
    return out


class SegmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="desk-status-seg-")
        self.root = os.path.join(self.tmp, "sessions")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def svc(self):
        return StatusService(SessionIndex([self.root]), GitCache(enabled=False))

    def write_pair(self, **kw):
        base, keep = base_thread()
        p1 = base.write(self.root, day="2026/09/22", name_time="2026-09-22T10-00-00")
        seg = next_file(base, keep, **kw)
        p2 = seg.write(self.root, day="2026/09/22", name_time="2026-09-22T10-20-00")
        return base, seg, p1, p2

    @staticmethod
    def cost(snap_session):
        return sum(sum(u["cost"].values()) for u in snap_session["usage"] if u["priced"])

    @staticmethod
    def responses(snap_session):
        return sum(u["responses"] for u in snap_session["usage"])

    # ---- the format as it is -------------------------------------------------
    def test_empty_interrupted_base_then_fresh_and_carried_segments(self):
        # Codex Desktop 0.164 can leave only an aborted startup in the base file.
        # All real requests live in a fresh segment and a later history_base segment.
        base = Rollout(T)
        base.start_turn(model="gpt-6-astra", effort="max", window=380_000)
        base.abort_turn()
        base.write(self.root, name_time="2026-10-25T10-00-00")
        svc = self.svc()
        before = svc.snapshot(host_meta={"threadId": T})["session"]
        self.assertEqual(before["turn"]["status"], "interrupted")
        self.assertEqual(before["context"]["state"], "no_data")
        self.assertEqual(self.responses(before), 0)

        fresh = Rollout(T, segment_id=X, start_ms=base.t + 4000, turn_n=base.turn_n)
        fresh.start_turn(model="gpt-6-astra", effort="max", window=380_000)
        standard_response(fresh, 500, 2000, out=500, inp=80_000, cached=60_000, window=380_000)
        fresh.complete_turn()
        fresh.write(self.root, name_time="2026-10-25T10-00-04")
        carried = fresh.carry_on(Y, len(fresh.lines), start_ms=fresh.t + 60_000)
        carried.start_turn(model="gpt-6-astra", effort="max", window=380_000)
        standard_response(carried, 1000, 3000, out=1000, inp=100_000, cached=90_000, window=380_000)
        carried.write(self.root, name_time="2026-10-25T10-01-08")
        svc.index._scanned_at = 0.0
        snap = svc.snapshot(host_meta={"threadId": T}, include_sessions=True)
        after = snap["session"]
        self.assertEqual(after["file"]["segments"], 3)
        self.assertEqual(after["turn"]["status"], "active")
        self.assertEqual(after["context"]["usedTokens"], 101_000)
        self.assertEqual(after["cache"]["latest"]["ratio"], 0.9)
        self.assertEqual(after["throughput"]["aggregate"]["samples"], 2)
        self.assertGreater(self.cost(after), 0)
        self.assertEqual(self.responses(after), 2)
        self.assertEqual(self.responses(svc.snapshot(host_meta={"threadId": T})["session"]), 2)
        self.assertEqual([row["id"] for row in snap["sessions"]], [T])
        self.assertEqual(snap["warnings"], [])

    def test_a_carried_on_file_is_the_same_session(self):
        base, seg, p1, p2 = self.write_pair()
        svc = self.svc()
        snap = svc.snapshot(host_meta={"threadId": T}, include_sessions=True)
        s = snap["session"]
        self.assertEqual(snap["binding"]["state"], "host")
        self.assertEqual(s["id"], T)
        self.assertFalse(s["idMismatch"])
        self.assertEqual(s["file"]["segments"], 2)
        self.assertTrue(s["file"]["caughtUp"])
        self.assertEqual(s["file"]["readBytes"], os.path.getsize(p1) + os.path.getsize(p2))
        # listed once, by the thread's id; the segment id is not a session of its own
        ids = [x["id"] for x in snap["sessions"]]
        self.assertEqual(ids.count(T), 1)
        self.assertNotIn(X, ids)
        self.assertTrue([x for x in snap["sessions"] if x["id"] == T][0]["verified"])
        self.assertEqual(snap["warnings"], [])

    def test_history_and_the_newest_state_both_hold(self):
        base, seg, p1, p2 = self.write_pair()
        s = self.svc().snapshot(host_meta={"threadId": T})["session"]
        # every request of both files, the interrupted turn's too, each once
        self.assertEqual(self.responses(s), len(usage_records(p1)) + len(usage_records(p2)))
        self.assertEqual(self.responses(s), 3 + 3)
        self.assertEqual(s["diagnostics"]["duplicateUsage"], 0)
        # the cost is the two files' own costs added, no less (the base's history kept)
        apart = 0.0
        for p in (p1, p2):
            log = SessionLog(p)
            log.refresh()
            apart += sum(sum(b["cost"].values()) for b in log.state.usage_by_model.values() if b["priced"])
        self.assertGreater(apart, 0)
        self.assertAlmostEqual(self.cost(s), apart, places=9)
        # the session began with its first file; what it runs on now is the newest file's
        self.assertEqual(s["startedAt"], BASE_MS)
        self.assertEqual((s["model"], s["effort"]), ("gpt-6-astra", "medium"))
        c = s["context"]
        self.assertEqual(c["state"], "ok")
        self.assertEqual(c["windowTokens"], 258_400)
        self.assertEqual(c["usedTokens"], 62_000 + 302)
        with open(p2, encoding="utf-8") as fh:
            newest = max(parse_ts_ms(json.loads(line)["timestamp"]) for line in fh if line.strip())
        self.assertEqual(s["lastEventAt"], newest)
        t = s["throughput"]
        self.assertEqual((t["model"], t["effort"]), ("gpt-6-astra", "medium"))
        self.assertEqual(t["aggregate"]["samples"], 3)
        self.assertEqual(s["turn"]["status"], "completed")
        # the cost line has every request, in order
        self.assertEqual(len(s["timeline"]), 6)
        self.assertEqual([r[0] for r in s["timeline"]], sorted(r[0] for r in s["timeline"]))

    def test_snapshots_do_not_add_up_twice(self):
        base, seg, p1, p2 = self.write_pair()
        svc = self.svc()
        first = svc.snapshot(host_meta={"threadId": T})["session"]
        log = svc._logs[p1]
        read = log.offset
        for _ in range(3):
            again = svc.snapshot(host_meta={"threadId": T})["session"]
        self.assertEqual(log.offset, read)  # no new bytes, nothing read again
        self.assertEqual(self.responses(again), self.responses(first))
        self.assertAlmostEqual(self.cost(again), self.cost(first), places=12)
        self.assertEqual(again["file"]["resets"], 0)
        # the newest file grows: only its new bytes are read, its request added once
        seg.start_turn(model="gpt-6-astra", effort="medium", window=258_400)
        standard_response(seg, 600, 3_000, out=500, inp=70_000, cached=35_000, window=258_400)
        seg.complete_turn()
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write(seg.text())
        grown = svc.snapshot(host_meta={"threadId": T})["session"]
        self.assertEqual(self.responses(grown), self.responses(first) + 1)
        self.assertEqual(grown["context"]["usedTokens"], 70_500)
        self.assertEqual(log.offset, os.path.getsize(p1) + os.path.getsize(p2))
        self.assertEqual(grown["file"]["resets"], 0)

    def test_a_file_appearing_later_is_read_on_into(self):
        base, keep = base_thread()
        p1 = base.write(self.root, name_time="2026-09-22T10-00-00")
        svc = self.svc()
        before = svc.snapshot(host_meta={"threadId": T})["session"]
        self.assertEqual(before["file"]["segments"], 1)
        log = svc._logs[p1]
        read = log.offset
        seg = next_file(base, keep)
        seg.write(self.root, name_time="2026-09-22T10-20-00")
        svc.index._scanned_at = 0.0  # the index's next scan, as its TTL would bring
        after = svc.snapshot(host_meta={"threadId": T})["session"]
        self.assertIs(svc._logs[p1], log)
        self.assertEqual(after["file"]["segments"], 2)
        self.assertEqual(after["file"]["resets"], 0)
        self.assertGreater(log.offset, read)
        self.assertEqual(self.responses(after), self.responses(before) + 3)
        self.assertEqual(after["model"], "gpt-6-astra")

    def test_three_files_each_naming_the_one_before(self):
        base, keep = base_thread()
        base.write(self.root, name_time="2026-09-22T10-00-00")
        s1 = next_file(base, keep, responses=2)
        s1.abort_turn()
        keep1 = len(s1.lines)
        s1.start_turn(model="gpt-6-astra", effort="medium", window=258_400)  # interrupted
        s1.write(self.root, name_time="2026-09-22T10-20-00")
        # the third file names the second by its segment id; its ordinals run on from there
        s2 = s1.carry_on(Y, keep1, start_ms=s1.t + 10_000)
        self.assertEqual(s2.lines and json.loads(s2.lines[0])["payload"]["history_base"]["thread_id"], X)
        s2.start_turn(model="gpt-6-sol", effort="low", window=300_000)
        standard_response(s2, 500, 2_000, out=90, inp=80_000, cached=40_000, window=300_000)
        s2.complete_turn()
        s2.write(self.root, day="2026/09/23", name_time="2026-09-23T00-10-00")  # the next day's folder
        snap = self.svc().snapshot(host_meta={"threadId": T}, include_sessions=True)
        s = snap["session"]
        self.assertEqual(s["file"]["segments"], 3)
        self.assertEqual(self.responses(s), 3 + 2 + 1)
        self.assertEqual(s["model"], "gpt-6-sol")
        self.assertEqual(s["context"]["windowTokens"], 300_000)
        self.assertEqual([x["id"] for x in snap["sessions"]], [T])

    def test_a_history_begun_afresh_joins_by_its_meta(self):
        # a further file without history_base starts at ordinal 0, after the one before it
        base, _ = base_thread()
        base.write(self.root, name_time="2026-09-22T10-00-00")
        fresh = Rollout(T, segment_id=X, start_ms=base.t + 60_000, cwd="/fixture/projects/segmented",
                        resp_n=base.resp_n, turn_n=base.turn_n)
        fresh.start_turn(model="gpt-6-astra", effort="medium", window=258_400)
        standard_response(fresh, 500, 2_000, out=70, inp=9_000, cached=0, window=258_400)
        fresh.complete_turn()
        fresh.write(self.root, name_time="2026-09-22T10-30-00")
        s = self.svc().snapshot(host_meta={"threadId": T})["session"]
        self.assertEqual(s["file"]["segments"], 2)
        self.assertEqual(self.responses(s), 3 + 1)
        self.assertEqual(s["context"]["usedTokens"], 9_070)

    # ---- what must not join ------------------------------------------------------
    def assert_apart(self, snap_kw=None):
        svc = self.svc()
        snap = svc.snapshot(host_meta={"threadId": T}, include_sessions=True)
        s = snap["session"]
        self.assertEqual(s["id"], T)
        self.assertEqual(s["file"]["segments"], 1)  # the thread's own file only
        self.assertEqual(self.responses(s), 3)
        row = [x for x in snap["sessions"] if x["id"] == X]
        self.assertEqual(len(row), 1)  # still there to choose by hand, under its own id
        self.assertFalse(row[0]["verified"])
        # and never bound for the host
        bad = svc.snapshot(host_meta={"threadId": X})
        self.assertEqual(bad["binding"]["state"], "none")
        self.assertEqual(bad["binding"]["host"]["status"], "unmatched")
        manual = svc.snapshot(manual_id=X)["session"]
        self.assertTrue(manual["idMismatch"])
        # the thread does not stop at its older data in silence: it says its record may be short
        self.assertIn("segments_unjoined", snap["warnings"])
        self.assertEqual(s["file"]["unjoined"], 1)

    def rewrite_meta(self, path, change):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        meta = json.loads(lines[0])
        change(meta)
        lines[0] = json.dumps(meta, separators=(",", ":"))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    def test_a_byte_offset_off_the_line_end_does_not_join(self):
        base, seg, p1, p2 = self.write_pair()
        self.rewrite_meta(p2, lambda m: m["payload"]["history_base"].update(
            end_byte_offset=m["payload"]["history_base"]["end_byte_offset"] - 7))
        self.assert_apart()

    def test_an_offset_at_another_line_end_does_not_join(self):
        # at a line's end, but not the line before the ordinal it names
        base, keep = base_thread()
        p1 = base.write(self.root, name_time="2026-09-22T10-00-00")
        seg = next_file(base, keep)
        p2 = seg.write(self.root, name_time="2026-09-22T10-20-00")
        wrong = base.history_base(keep - 1)["end_byte_offset"]
        self.rewrite_meta(p2, lambda m: m["payload"]["history_base"].update(end_byte_offset=wrong))
        self.assert_apart()

    def test_an_ordinal_that_does_not_run_on_does_not_join(self):
        base, seg, p1, p2 = self.write_pair()
        self.rewrite_meta(p2, lambda m: m.update(ordinal=m["ordinal"] + 1))
        self.assert_apart()

    def test_a_base_named_elsewhere_does_not_join(self):
        base, seg, p1, p2 = self.write_pair()
        self.rewrite_meta(p2, lambda m: m["payload"]["history_base"].update(thread_id=OTHER))
        self.assert_apart()

    def test_a_copied_file_of_another_thread_does_not_join(self):
        # named as this thread's next file, its meta another thread's (a renamed or copied log)
        base, seg, p1, p2 = self.write_pair()
        self.rewrite_meta(p2, lambda m: m["payload"].update(id=OTHER, session_id=OTHER))
        self.assert_apart()

    def test_a_base_in_another_root_does_not_join(self):
        base, keep = base_thread()
        base.write(self.root, name_time="2026-09-22T10-00-00")
        other_root = os.path.join(self.tmp, "elsewhere")
        next_file(base, keep).write(other_root, name_time="2026-09-22T10-20-00")
        svc = StatusService(SessionIndex([self.root, other_root]), GitCache(enabled=False))
        snap = svc.snapshot(host_meta={"threadId": T})
        self.assertEqual(snap["session"]["file"]["segments"], 1)
        self.assertIn("segments_unjoined", snap["warnings"])

    def test_a_renamed_foreign_first_file_takes_no_segments(self):
        # the thread's "first file" is a renamed copy of another thread: nothing joins it
        base, seg, p1, p2 = self.write_pair()
        self.rewrite_meta(p1, lambda m: m["payload"].update(id=OTHER, session_id=OTHER))
        svc = self.svc()
        snap = svc.snapshot(host_meta={"threadId": T}, include_sessions=True)
        self.assertEqual(snap["binding"]["state"], "none")
        self.assertEqual(snap["binding"]["host"]["status"], "unmatched")

    def test_host_keys_that_disagree_stay_ambiguous(self):
        self.write_pair()
        other = Rollout(OTHER, cwd="/fixture/projects/other", start_ms=BASE_MS + H)
        other.start_turn()
        standard_response(other, 500, 2_000, out=40, inp=5_000, cached=0)
        other.complete_turn()
        other.write(self.root, name_time="2026-09-22T11-00-00")
        snap = self.svc().snapshot(host_meta={"threadId": T, "openai/threadId": OTHER})
        self.assertEqual(snap["binding"]["host"]["status"], "ambiguous")
        self.assertIsNone(snap["session"])
        # the segment id beside the thread's names nothing more: the thread binds
        snap = self.svc().snapshot(host_meta={"threadId": T, "openai/threadId": X})
        self.assertEqual(snap["binding"]["state"], "host")
        self.assertEqual(snap["session"]["id"], T)

    def test_a_handed_over_file_is_not_read_on(self):
        # once the next file is read, bytes the earlier one gains are counted, not read
        base, seg, p1, p2 = self.write_pair()
        svc = self.svc()
        first = svc.snapshot(host_meta={"threadId": T})["session"]
        with open(p1, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": "2026-09-22T12:00:00.000Z", "ordinal": 999, "type": "event_msg",
                                 "payload": {"type": "task_started", "turn_id": "late"}}) + "\n")
        snap = svc.snapshot(host_meta={"threadId": T})
        later = snap["session"]
        self.assertGreater(later["file"]["lateBytes"], 0)
        # the older file has records not read: the snapshot says the data may be short
        self.assertIn("segments_late", snap["warnings"])
        self.assertEqual(self.responses(later), self.responses(first))
        self.assertEqual(later["turn"]["status"], first["turn"]["status"])


    # ---- 0.2.3 review M2: each file joins the one just before it, and no copy joins -----
    def copy_as(self, src, seg_id, name_time):
        dst = os.path.join(os.path.dirname(src), f"rollout-{name_time}-{T}_{seg_id}.jsonl")
        shutil.copyfile(src, dst)
        return dst

    def test_a_copy_of_the_first_file_does_not_join_or_freeze_it(self):
        # the first file copied whole under any segment name: same meta, ordinal 0, the same
        # start; a history begun afresh must begin after the last file began, so it does not join
        base, keep = base_thread()
        p1 = base.write(self.root, name_time="2026-09-22T10-00-00")
        self.copy_as(p1, OTHER, "2026-09-22T10-05-00")
        svc = self.svc()
        snap = svc.snapshot(host_meta={"threadId": T}, include_sessions=True)
        s = snap["session"]
        self.assertEqual(s["file"]["segments"], 1)
        self.assertIn("segments_unjoined", snap["warnings"])
        self.assertIn(OTHER, [x["id"] for x in snap["sessions"]])
        # the first file goes on being written, and is read: not frozen at the copy
        base.start_turn(model="gpt-6.1-sol", effort="high", window=200_000)
        standard_response(base, 500, 2_000, out=80, inp=30_000, cached=20_000)
        base.complete_turn()
        with open(p1, "w", encoding="utf-8") as fh:
            fh.write(base.text())
        grown = svc.snapshot(host_meta={"threadId": T})["session"]
        self.assertEqual(self.responses(grown), self.responses(s) + 1)
        self.assertEqual(grown["context"]["usedTokens"], 30_080)
        self.assertEqual(grown["file"]["lateBytes"], 0)

    def test_a_copy_of_the_first_file_beside_a_real_segment(self):
        base, seg, p1, p2 = self.write_pair()
        self.copy_as(p1, OTHER, "2026-09-22T10-05-00")
        svc = self.svc()
        snap = svc.snapshot(host_meta={"threadId": T})
        s = snap["session"]
        self.assertEqual(s["file"]["segments"], 2)  # the real segment still joins
        self.assertEqual(self.responses(s), 6)
        self.assertEqual(s["model"], "gpt-6-astra")
        self.assertIn("segments_unjoined", snap["warnings"])
        # and the segment being written is read on
        seg.start_turn(model="gpt-6-astra", effort="medium", window=258_400)
        standard_response(seg, 600, 3_000, out=500, inp=70_000, cached=35_000, window=258_400)
        seg.complete_turn()
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write(seg.text())
        self.assertEqual(self.responses(svc.snapshot(host_meta={"threadId": T})["session"]), 7)

    def test_a_copy_of_a_segment_joins_neither_and_says_so(self):
        # a legal segment copied under another segment name: two files carry the same history on
        base, seg, p1, p2 = self.write_pair()
        self.copy_as(p2, OTHER, "2026-09-22T10-25-00")
        svc = self.svc()
        snap = svc.snapshot(host_meta={"threadId": T}, include_sessions=True)
        s = snap["session"]
        self.assertEqual(s["file"]["segments"], 1)  # never two carried-on files in one chain
        self.assertTrue(s["file"]["forked"])
        self.assertIn("segments_ambiguous", snap["warnings"])
        self.assertIn("segments_unjoined", snap["warnings"])
        ids = [x["id"] for x in snap["sessions"]]
        self.assertIn(X, ids)
        self.assertIn(OTHER, ids)
        # the first file is not sealed by either: what is written to it is still read
        self.assertEqual(s["file"]["lateBytes"], 0)
        self.assertTrue(s["file"]["caughtUp"])

    def test_a_carried_on_file_must_name_the_one_just_before(self):
        # a third file whose history_base checks out against the FIRST file, not the second
        base, keep = base_thread()
        base.write(self.root, name_time="2026-09-22T10-00-00")
        s1 = next_file(base, keep)
        s1.write(self.root, name_time="2026-09-22T10-20-00")
        s2 = base.carry_on(Y, keep, start_ms=s1.t + 10_000)
        s2.start_turn(model="gpt-6-sol", effort="low", window=300_000)
        standard_response(s2, 500, 2_000, out=90, inp=80_000, cached=40_000, window=300_000)
        s2.write(self.root, name_time="2026-09-22T11-00-00")
        snap = self.svc().snapshot(host_meta={"threadId": T})
        # two files carry the first one's history on: a fork, joined by neither
        self.assertEqual(snap["session"]["file"]["segments"], 1)
        self.assertIn("segments_ambiguous", snap["warnings"])

    def test_a_history_begun_afresh_must_begin_later(self):
        base, _ = base_thread()
        base.write(self.root, name_time="2026-09-22T10-00-00")
        same = Rollout(T, segment_id=X, start_ms=BASE_MS, cwd="/fixture/projects/segmented",
                       resp_n=base.resp_n, turn_n=base.turn_n)
        same.start_turn(model="gpt-6-astra", effort="medium", window=258_400)
        standard_response(same, 500, 2_000, out=70, inp=9_000, cached=0, window=258_400)
        same.write(self.root, name_time="2026-09-22T10-30-00")
        snap = self.svc().snapshot(host_meta={"threadId": T})
        self.assertEqual(snap["session"]["file"]["segments"], 1)
        self.assertIn("segments_unjoined", snap["warnings"])

    # ---- 0.2.3 review M1: a chain that loses a file says so ----------------------------
    def test_a_chain_that_loses_a_file_says_so(self):
        base, seg, p1, p2 = self.write_pair()
        svc = self.svc()
        self.assertEqual(svc.snapshot(host_meta={"threadId": T})["warnings"], [])
        # the segment put back in its place no longer checking out (a new file at the path)
        with open(p2, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        meta = json.loads(lines[0])
        meta["payload"]["history_base"]["end_byte_offset"] -= 7
        lines[0] = json.dumps(meta, separators=(",", ":"))
        tmp = p2 + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        os.replace(tmp, p2)
        svc.index._scanned_at = 0.0
        snap = svc.snapshot(host_meta={"threadId": T})
        self.assertEqual(snap["session"]["file"]["segments"], 1)
        self.assertIn("segments_unjoined", snap["warnings"])
        self.assertIn("segments_dropped", snap["warnings"])
        # removed altogether: the totals fall back, never in silence
        os.remove(p2)
        svc.index._scanned_at = 0.0
        snap = svc.snapshot(host_meta={"threadId": T})
        self.assertEqual(self.responses(snap["session"]), 3)
        self.assertNotIn("segments_unjoined", snap["warnings"])
        self.assertIn("segments_dropped", snap["warnings"])

    def test_a_segment_still_being_created_is_waited_for(self):
        base, seg, p1, p2 = self.write_pair()
        with open(p2, encoding="utf-8") as fh:
            whole = fh.read()
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write(whole[:200])  # the first line not yet whole, the file just written
        svc = self.svc()
        snap = svc.snapshot(host_meta={"threadId": T}, include_sessions=True)
        self.assertEqual(snap["warnings"], [])
        self.assertNotIn(X, [x["id"] for x in snap["sessions"]])
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write(whole)
        svc.index._scanned_at = 0.0
        snap = svc.snapshot(host_meta={"threadId": T})
        self.assertEqual(snap["session"]["file"]["segments"], 2)
        self.assertEqual(snap["warnings"], [])


if __name__ == "__main__":
    unittest.main()
