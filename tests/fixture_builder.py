"""Synthetic Codex rollout builder for tests and the browser harness.

FIXTURE DATA ONLY. Nothing here is copied from real logs: ids are generated,
paths live under /fixture, model names start with ``fixture-``, and message
items carry no text.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

FIXTURE_MARK = "FIXTURE"
BASE_MS = 1_790_000_000_000  # fixed epoch for deterministic fixtures


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def fixture_uuid(n: int) -> str:
    """Valid lowercase UUID with a recognisable fixture prefix (f1f7...)."""
    return f"f1f70000-0000-7000-8000-{n:012x}"


class Rollout:
    def __init__(self, session_id: str, cwd: str = "/fixture/projects/alpha", start_ms: int = BASE_MS,
                 thread_source: str = "user", source: Any = "vscode", account: str = "fixture-account-1",
                 segment_id: Optional[str] = None, history_base: Optional[Dict[str, Any]] = None,
                 ordinal: int = 0, resp_n: int = 0, turn_n: int = 0):
        self.id = session_id
        self.segment_id = segment_id  # a further file of the thread: rollout-<time>-<id>_<segment>.jsonl
        self.t = start_ms
        self.lines: List[str] = []
        self.ordinal = ordinal
        self.turn_n = turn_n
        self.resp_n = resp_n
        self.turn_id: Optional[str] = None
        meta_extra: Dict[str, Any] = {"history_mode": "paginated"}
        if history_base is not None:
            meta_extra["history_base"] = history_base
        self._emit("session_meta", dict(meta_extra, **{
            "id": session_id,
            "session_id": session_id,
            "timestamp": iso(start_ms),
            "cwd": cwd,
            "originator": "fixture-originator",
            "cli_version": "0.0.0-fixture",
            "source": source,
            "thread_source": thread_source,
            "creator_account_id": account,
            "base_instructions": {"text": FIXTURE_MARK + " instructions placeholder"},
        }))

    # -- primitives -------------------------------------------------------
    def _emit(self, top: str, payload: Dict[str, Any], ts: Optional[int] = None) -> None:
        obj = {"timestamp": iso(self.t if ts is None else ts), "ordinal": self.ordinal, "type": top, "payload": payload}
        self.ordinal += 1
        self.lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def advance(self, ms: int) -> "Rollout":
        self.t += ms
        return self

    def raw(self, line: str) -> "Rollout":
        self.lines.append(line)
        return self

    # -- events -----------------------------------------------------------
    def start_turn(self, model: str = "fixture-model-a", effort: str = "high", window: int = 200_000,
                   cwd: Optional[str] = None, emit_context: bool = True) -> str:
        self.turn_n += 1
        self.turn_id = f"fixture-turn-{self.turn_n:04d}"
        self._emit("event_msg", {"type": "task_started", "turn_id": self.turn_id,
                                 "started_at": self.t // 1000, "model_context_window": window})
        if emit_context:
            self.advance(40)
            p = {"turn_id": self.turn_id, "model": model, "effort": effort}
            if cwd:
                p["cwd"] = cwd
            self._emit("turn_context", p)
        self.advance(20)
        self.item("UserMessage", 0)
        return self.turn_id

    def turn_context(self, model: str, effort: str) -> "Rollout":
        self._emit("turn_context", {"turn_id": self.turn_id, "model": model, "effort": effort})
        return self

    def thread_settings(self, model: str, effort: str) -> "Rollout":
        self._emit("event_msg", {"type": "thread_settings_applied", "thread_id": self.id,
                                 "thread_settings": {"model": model, "reasoning_effort": effort}})
        return self

    def item(self, kind: str, duration_ms: int, start_ms: Optional[int] = None, turn_id: Optional[str] = None) -> "Rollout":
        start = self.t if start_ms is None else start_ms
        end = start + duration_ms
        self.t = max(self.t, end)
        self._emit("event_msg", {"type": "item_completed", "thread_id": self.id, "turn_id": turn_id or self.turn_id,
                                 "item": {"type": kind, "id": f"fixture-item-{self.ordinal}"},
                                 "started_at_ms": start, "completed_at_ms": end})
        return self

    def usage(self, inp: int, cached: int, out: int, reasoning: int = 0, response_id: Optional[str] = None,
              turn_id: Optional[str] = None, thread_id: Optional[str] = None, with_token_count: bool = True,
              rate_limits: Optional[Dict[str, Any]] = None, window: int = 200_000) -> str:
        self.resp_n += 1
        rid = response_id or f"fixture-resp-{self.resp_n:05d}"
        # the shape real logs record: every count present, cache writes included
        usage = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0, "output_tokens": out,
                 "reasoning_output_tokens": reasoning, "total_tokens": inp + out}
        self._emit("token_usage_record", {"thread_id": thread_id or self.id, "turn_id": turn_id or self.turn_id,
                                          "session_id": self.id, "response_id": rid, "usage": usage,
                                          "turn_token_usage": usage, "thread_token_usage": usage})
        if with_token_count:
            self.advance(2)
            self.token_count(usage, window=window, rate_limits=rate_limits)
        return rid

    def token_count(self, last: Optional[Dict[str, int]] = None, window: int = 200_000,
                    rate_limits: Optional[Dict[str, Any]] = None) -> "Rollout":
        payload: Dict[str, Any] = {"type": "token_count"}
        if last is not None:
            payload["info"] = {"last_token_usage": last, "total_token_usage": last, "model_context_window": window}
        payload["rate_limits"] = rate_limits
        self._emit("event_msg", payload)
        return self

    def tool_call(self, duration_ms: int) -> "Rollout":
        start = self.t
        self.item("CommandExecution", duration_ms, start_ms=start)
        return self

    def complete_turn(self, error: bool = False) -> "Rollout":
        p: Dict[str, Any] = {"type": "task_complete", "turn_id": self.turn_id}
        if error:
            p["error"] = {"message": FIXTURE_MARK}
        self._emit("event_msg", p)
        return self

    def abort_turn(self, reason: str = "interrupted") -> "Rollout":
        self._emit("event_msg", {"type": "turn_aborted", "turn_id": self.turn_id, "reason": reason})
        return self

    def compacted(self) -> "Rollout":
        self._emit("compacted", {"message": "", "replacement_history": [], "window_number": 2})
        return self

    def response_item(self) -> "Rollout":
        self._emit("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": FIXTURE_MARK}]})
        return self

    # -- output -------------------------------------------------------------
    def text(self, trailing_newline: bool = True) -> str:
        return "\n".join(self.lines) + ("\n" if trailing_newline else "")

    def history_base(self, keep_lines: int, file_id: Optional[str] = None) -> Dict[str, Any]:
        """Where a further file's history carries on from this one, as Codex records it: the
        file's id (the thread's for its first file, else its segment id), the ordinal after the
        last line kept and the byte offset of that line's end."""
        kept = self.lines[:keep_lines]
        end_ordinal = json.loads(kept[-1])["ordinal"] + 1
        return {"thread_id": file_id or self.segment_id or self.id, "end_ordinal_exclusive": end_ordinal,
                "end_byte_offset": len(("\n".join(kept) + "\n").encode("utf-8"))}

    def carry_on(self, segment_id: str, keep_lines: int, start_ms: int, **kw: Any) -> "Rollout":
        """The thread's next file, its history carrying on from this file's first ``keep_lines``
        lines (the lines after them, an interrupted turn, stay this file's own)."""
        hb = self.history_base(keep_lines)
        return Rollout(self.id, segment_id=segment_id, history_base=hb, ordinal=hb["end_ordinal_exclusive"],
                       start_ms=start_ms, resp_n=self.resp_n, turn_n=self.turn_n, **kw)

    def write(self, root: str, day: str = "2026/09/22", name_time: str = "2026-09-22T10-00-00") -> str:
        d = os.path.join(root, *day.split("/"))
        os.makedirs(d, exist_ok=True)
        name = self.id + (f"_{self.segment_id}" if self.segment_id else "")
        path = os.path.join(d, f"rollout-{name_time}-{name}.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.text())
        os.utime(path, (self.t / 1000, self.t / 1000))
        return path


def rate_limits(used: float = 34.0, window_minutes: int = 10080, resets_at_ms: Optional[int] = None,
                secondary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "limit_id": "fixture-limit",
        "limit_name": None,
        "primary": {"used_percent": used, "window_minutes": window_minutes,
                    "resets_at": (resets_at_ms or BASE_MS + 3 * 86400_000) // 1000},
        "secondary": secondary,
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "plan_type": "fixture-plan",
        "rate_limit_reached_type": None,
    }


def standard_response(r: Rollout, reasoning_ms: int, message_ms: int, out: int, inp: int, cached: int,
                      tail_wait_ms: int = 300, **kw) -> str:
    """One model response: Reasoning item, AgentMessage item, then usage."""
    if reasoning_ms:
        r.item("Reasoning", reasoning_ms)
    if message_ms:
        r.item("AgentMessage", message_ms)
    r.advance(tail_wait_ms)
    return r.usage(inp, cached, out, **kw)


def build_harness_fixtures(root_a: str, root_b: str, now_ms: int) -> Dict[str, str]:
    """Fixture set used by the browser harness and integration tests.

    Returns a mapping of role -> session id. ``now_ms`` anchors timestamps so
    relative times look plausible in screenshots.
    """
    ids = {}
    start = now_ms - 50 * 60_000

    # alpha: healthy session with several responses and a running turn.
    alpha = Rollout(fixture_uuid(1), cwd="/fixture/projects/clepsydra-alpha", start_ms=start)
    alpha.start_turn(model="fixture-model-a", effort="high", window=258_400)
    ctx = 24_000
    for i in range(9):
        alpha.advance(800)
        rid = standard_response(alpha, 2_000 + i * 150, 6_000 + (i % 3) * 900, out=240 + i * 23,
                                inp=ctx, cached=int(ctx * 0.93), reasoning=60 + i,
                                rate_limits=rate_limits(31 + i * 0.4, 10080, now_ms + 3 * 86400_000 + 5 * 3600_000,
                                                        secondary={"used_percent": 12.0, "window_minutes": 300,
                                                                   "resets_at": (now_ms + 2 * 3600_000) // 1000}),
                                window=258_400)
        alpha.tool_call(1_200)
        ctx += 9_500
    alpha.advance(500)
    alpha.usage(ctx, ctx - 400, 1, with_token_count=True, window=258_400)  # 1-token response, no timing
    alpha.complete_turn()
    alpha.advance(30_000)
    alpha.start_turn(model="fixture-model-a", effort="high", window=258_400)
    alpha.advance(600)
    standard_response(alpha, 1_800, 5_200, out=310, inp=ctx + 2_000, cached=ctx, reasoning=40, window=258_400,
                      rate_limits=rate_limits(35.0, 10080, now_ms + 3 * 86400_000 + 5 * 3600_000,
                                              secondary={"used_percent": 13.0, "window_minutes": 300,
                                                         "resets_at": (now_ms + 2 * 3600_000) // 1000}))
    alpha.t = max(alpha.t, now_ms - 4_000)
    alpha.item("Reasoning", 1_500)  # current turn still active
    ids["alpha"] = alpha.id
    alpha.write(root_a, name_time="2026-09-22T09-10-00")

    # beta: compacted after its last response (context pending).
    beta = Rollout(fixture_uuid(2), cwd="/fixture/projects/beta-docs", start_ms=start - 3600_000)
    beta.start_turn(model="fixture-model-b", effort="medium", window=128_000)
    standard_response(beta, 1_000, 4_000, out=420, inp=96_000, cached=60_000, window=128_000)
    beta.advance(2_000).compacted()
    beta.complete_turn()
    beta.t = now_ms - 25 * 60_000
    ids["beta"] = beta.id
    beta.write(root_a, name_time="2026-09-22T08-00-00")

    # hostile: log-derived strings that would execute if rendered as HTML.
    hostile_cwd = '/fixture/<img src=x onerror="window.__xss=1">/"><script>window.__xss=2</script>'
    hostile = Rollout(fixture_uuid(3), cwd=hostile_cwd, start_ms=start - 2 * 3600_000)
    hostile.start_turn(model='fixture-<b onmouseover=alert(1)>model', effort='high"><svg onload=alert(2)>', window=100_000)
    standard_response(hostile, 1_000, 3_000, out=100, inp=10_000, cached=0, window=100_000)
    hostile.abort_turn()
    hostile.t = now_ms - 3 * 3600_000
    ids["hostile"] = hostile.id
    hostile.write(root_a, name_time="2026-09-22T07-00-00")

    # review sub-session: hidden from the default list.
    review = Rollout(fixture_uuid(4), cwd="/fixture/projects/clepsydra-alpha", start_ms=start,
                     thread_source="guardian_review", source={"subagent": "fixture"})
    review.start_turn(model="fixture-review", effort="low")
    standard_response(review, 0, 1_000, out=20, inp=5_000, cached=0)
    review.complete_turn()
    review.t = now_ms - 60_000
    ids["review"] = review.id
    review.write(root_a, name_time="2026-09-22T09-30-00")

    # legacy: old schema with token_count only (no token_usage_record).
    legacy = Rollout(fixture_uuid(5), cwd="C:\\fixture\\legacy-win", start_ms=start - 5 * 3600_000)
    legacy.start_turn(model="fixture-legacy", effort="medium", window=128_000)
    legacy.token_count({"input_tokens": 40_000, "cached_input_tokens": 10_000, "output_tokens": 800,
                        "reasoning_output_tokens": 0, "total_tokens": 40_800}, window=128_000,
                       rate_limits=rate_limits(70.0, 300, now_ms - 3600_000))
    legacy.complete_turn()
    legacy.t = now_ms - 6 * 3600_000
    ids["legacy"] = legacy.id
    legacy.write(root_b, name_time="2026-09-22T04-00-00")

    # duplicate id across roots: the newer copy in root_b wins.
    dup_old = Rollout(fixture_uuid(6), cwd="/fixture/projects/dup-old", start_ms=start - 8 * 3600_000)
    dup_old.start_turn(model="fixture-model-a", effort="low")
    standard_response(dup_old, 0, 2_000, out=50, inp=1_000, cached=0)
    dup_old.t = now_ms - 9 * 3600_000
    dup_old.write(root_a, name_time="2026-09-21T23-00-00")
    dup_new = Rollout(fixture_uuid(6), cwd="/fixture/projects/dup-new", start_ms=start - 8 * 3600_000)
    dup_new.start_turn(model="fixture-model-a", effort="low")
    standard_response(dup_new, 0, 2_000, out=50, inp=1_000, cached=0)
    dup_new.advance(1000)
    standard_response(dup_new, 0, 2_000, out=60, inp=2_000, cached=500)
    dup_new.t = now_ms - 8 * 3600_000
    ids["dup"] = dup_new.id
    dup_new.write(root_b, name_time="2026-09-21T23-00-00")

    # nocache: usage records without cached_input_tokens (must read as "not
    # recorded", never as a 0% hit rate).
    nocache = Rollout(fixture_uuid(7), cwd="/fixture/projects/no-cache-field", start_ms=start - 4 * 3600_000)
    nocache.start_turn(model="fixture-model-a", effort="high", window=200_000)
    nocache.item("Reasoning", 1_000).item("AgentMessage", 3_000).advance(200)
    usage_wo_cache(nocache, inp=50_000, out=300)
    nocache.complete_turn()
    nocache.t = now_ms - 4 * 3600_000
    ids["nocache"] = nocache.id
    nocache.write(root_b, name_time="2026-09-22T05-00-00")

    # renamed: a copy of another session's content stored under a different
    # UUID filename. Its session_meta.id disagrees with the filename.
    renamed_id = fixture_uuid(8)
    foreign = Rollout(fixture_uuid(9), cwd="/fixture/projects/foreign-copy", start_ms=start - 7 * 3600_000)
    foreign.start_turn(model="fixture-model-a", effort="high")
    standard_response(foreign, 500, 2_000, out=120, inp=8_000, cached=4_000)
    foreign.complete_turn()
    foreign.t = now_ms - 7 * 3600_000
    foreign.id = renamed_id  # only the filename changes; content keeps fixture_uuid(9)
    ids["renamed"] = renamed_id
    foreign.write(root_b, name_time="2026-09-22T02-00-00")
    return ids


def usage_wo_cache(r: Rollout, inp: int, out: int) -> str:
    """A token_usage_record whose usage has no cached_input_tokens field."""
    r.resp_n += 1
    rid = f"fixture-resp-{r.resp_n:05d}"
    usage = {"input_tokens": inp, "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": inp + out}
    r._emit("token_usage_record", {"thread_id": r.id, "turn_id": r.turn_id, "response_id": rid, "usage": usage})
    return rid


def usage_full(r: Rollout, inp: int, cached: Optional[int], out: int, response_id: Optional[str] = None,
               rate_limits: Optional[Dict[str, Any]] = None, window: int = 200_000) -> str:
    """A token_usage_record, then its token_count; cached=None leaves out both cache counts."""
    r.resp_n += 1
    rid = response_id or f"fixture-resp-{r.resp_n:05d}"
    usage: Dict[str, Any] = {"input_tokens": inp, "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": inp + out}
    if cached is not None:
        usage["cached_input_tokens"] = cached
        usage["cache_write_input_tokens"] = 0
    r._emit("token_usage_record", {"thread_id": r.id, "turn_id": r.turn_id, "response_id": rid, "usage": usage})
    r.advance(2)
    r.token_count(usage, window=window, rate_limits=rate_limits)
    return rid


def usage_raw(r: Rollout, usage: Dict[str, Any], window: int = 200_000) -> str:
    """A token_usage_record carrying exactly the usage fields given, then its token_count."""
    r.resp_n += 1
    rid = f"fixture-resp-{r.resp_n:05d}"
    r._emit("token_usage_record", {"thread_id": r.id, "turn_id": r.turn_id, "response_id": rid, "usage": usage})
    r.advance(2)
    r.token_count(usage, window=window)
    return rid


def edge_limits(used: float, now_ms: int, limit_id: str = "fixture-limit", window_minutes: int = 10080,
                elapsed: float = 0.56, secondary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A quota reading whose window is ``elapsed`` of the way through at ``now_ms``."""
    resets = now_ms + int((1 - elapsed) * window_minutes * 60_000)
    rl = rate_limits(used, window_minutes, resets, secondary=secondary)
    rl["limit_id"] = limit_id
    return rl


EDGE_IDS = {
    "low": 0x10, "long": 0x11, "missing": 0x12, "switched": 0x13, "multi": 0x14, "fresh": 0x15,
    "quota-joined": 0x16, "quota-joined-peer": 0x17, "quota-apart": 0x18, "quota-apart-peer": 0x19,
    "gaps": 0x1A, "many": 0x1B, "untimed": 0x1C,
}


def make_git_fixture(root: str) -> Optional[str]:
    """A throwaway repository for the footer: a long branch one commit ahead of its upstream,
    two files staged, one modified, three untracked. Needs git; None without it."""
    import shutil
    import subprocess
    if not shutil.which("git"):
        return None
    repo = os.path.join(root, "projects", "clepsydra-edge-git")
    remote = os.path.join(root, "remote.git")
    os.makedirs(repo, exist_ok=True)
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0")
    ident = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]

    def git(*args, cwd=repo):
        subprocess.run(["git", *ident, *args], cwd=cwd, env=env, check=True, capture_output=True)

    branch = "feature/status-panel/a-very-long-branch-name-for-the-footer"
    git("init", "--bare", remote, cwd=root)
    git("init", "-b", branch)
    for name, text in (("a.txt", "a\n"), ("b.txt", "b\n")):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(text)
    git("add", "a.txt", "b.txt")
    git("commit", "-m", "fixture: first")
    git("remote", "add", "origin", remote)
    git("push", "-u", "origin", branch)
    with open(os.path.join(repo, "c.txt"), "w") as fh:
        fh.write("c\n")
    git("add", "c.txt")
    git("commit", "-m", "fixture: ahead")
    for name in ("d.txt", "e.txt"):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(name)
        git("add", name)
    with open(os.path.join(repo, "a.txt"), "a") as fh:
        fh.write("changed\n")
    for name in ("u1.txt", "u2.txt", "u3.txt"):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(name)
    return repo


def build_edge_fixtures(root: str, now_ms: int, git_cwd: Optional[str] = None) -> Dict[str, str]:
    """Boundary states for the browser harness and tests: tiny and near-full quantities, long-
    context pricing, missing cache counts, a model switch, many subagents with long names, a
    session with no responses, and quota readings from another session that may or may not
    continue this one's line. Returns role -> session id; ids are fixed (EDGE_IDS)."""
    ids: Dict[str, str] = {}
    min_ = 60_000

    # low: 1% of the context, 98% of the quota used, a 0% cache hit last, one very slow reply
    low = Rollout(fixture_uuid(EDGE_IDS["low"]), cwd="/fixture/projects/low-values", start_ms=now_ms - 40 * min_)
    low.start_turn(model="gpt-6.1-sol", effort="high", window=100_000)
    rates = [(800, 6_000), (900, 6_500), (60, 9_000), (850, 6_100), (5, 900)]
    for i, (out, ms) in enumerate(rates):
        low.advance(5 * min_)
        low.item("Reasoning", ms // 3).item("AgentMessage", ms - ms // 3).advance(200)
        usage_full(low, inp=1_000, cached=0 if i == len(rates) - 1 else 600, out=out, window=100_000,
                   rate_limits=edge_limits(97.0 + i * 0.25, now_ms, window_minutes=300, elapsed=0.9))
    low.complete_turn()
    ids["low"] = low.id
    low.write(root, name_time="2026-09-22T10-00-00")

    # long: two long-context requests on a model the table prices that way; the last 42 minutes
    # ago, past the 30-minute minimum retention
    long_ = Rollout(fixture_uuid(EDGE_IDS["long"]), cwd="/fixture/projects/long-context", start_ms=now_ms - 50 * min_)
    long_.start_turn(model="gpt-6-astra", effort="high", window=400_000)
    long_.item("Reasoning", 4_000).item("AgentMessage", 8_000).advance(300)
    usage_full(long_, inp=300_000, cached=100_000, out=10_000, window=400_000)
    long_.t = now_ms - 42 * min_ - 15_000
    long_.item("Reasoning", 4_000).item("AgentMessage", 8_000).advance(300)
    usage_full(long_, inp=300_000, cached=100_000, out=10_000, window=400_000)
    long_.complete_turn()
    ids["long"] = long_.id
    long_.write(root, name_time="2026-09-22T10-01-00")

    # missing: five requests, only the last recorded its cached tokens; the last 10 minutes ago
    miss = Rollout(fixture_uuid(EDGE_IDS["missing"]), cwd="/fixture/projects/missing-samples", start_ms=now_ms - 30 * min_)
    miss.start_turn(model="gpt-5.6-sol", effort="medium", window=258_400)
    for i in range(5):
        miss.advance(3 * min_)
        miss.item("Reasoning", 1_500).item("AgentMessage", 4_000).advance(200)
        usage_full(miss, inp=40_000 + i * 2_000, cached=(40_000 + i * 2_000) if i == 4 else None, out=400, window=258_400)
    miss.t = max(miss.t, now_ms - 10 * min_)
    miss.complete_turn()
    ids["missing"] = miss.id
    miss.write(root, name_time="2026-09-22T10-02-00")

    # switched: replies on one model, then the thread set to another with no reply on it yet
    sw = Rollout(fixture_uuid(EDGE_IDS["switched"]), cwd="/fixture/projects/model-switch", start_ms=now_ms - 25 * min_)
    sw.start_turn(model="gpt-6.1-sol", effort="xhigh", window=380_000)
    for i in range(7):
        sw.advance(2 * min_)
        standard_response(sw, 2_000 + i * 200, 5_000 + (i % 3) * 700, out=300 + i * 40, inp=180_000 + i * 9_000,
                          cached=178_000 + i * 9_000, window=380_000)
    sw.complete_turn()
    sw.advance(30_000).thread_settings("gpt-6-astra", "xhigh")
    ids["switched"] = sw.id
    sw.write(root, name_time="2026-09-22T10-03-00")

    # multi: a main agent with six subagents of long names and tasks, and an automatic review
    long_model = "fixture-model-with-an-unusually-long-name-2026-preview"
    multi = Rollout(fixture_uuid(EDGE_IDS["multi"]),
                    cwd=git_cwd or "/fixture/projects/a-project-with-a-rather-long-directory-name-for-the-menu",
                    start_ms=now_ms - 90 * min_)
    multi.start_turn(model="gpt-6-sol", effort="high", window=258_400)
    for i in range(6):
        multi.advance(4 * min_)
        standard_response(multi, 1_500, 4_500, out=420 + i * 30, inp=60_000 + i * 8_000, cached=55_000 + i * 8_000, window=258_400)
    multi.t = max(multi.t, now_ms - 3_000)
    multi.item("Reasoning", 1_200)  # still working
    ids["multi"] = multi.id
    multi.write(root, name_time="2026-09-22T10-04-00")
    names = ["Euclid", "Hypatia-the-very-thorough-reviewer-of-everything", "Noether", "Ramanujan", "Lovelace", "Turing"]
    tasks = ["/root/ui_polish", "/root/audit/a_task_with_a_long_name_that_needs_truncating_in_the_picker",
             "/root/pricing", "/root/cache_copy", "/root/fixtures", "/root/release_notes"]
    for k, (name, task) in enumerate(zip(names, tasks)):
        cid = fixture_uuid(0x20 + k)
        src = {"subagent": {"thread_spawn": {"parent_thread_id": multi.id, "agent_nickname": name, "agent_path": task, "depth": 1}}}
        child = Rollout(cid, cwd=multi_cwd(git_cwd), start_ms=now_ms - (80 - k * 9) * min_, thread_source="subagent", source=src)
        child.start_turn(model=long_model if k == 1 else "gpt-6-luna", effort="medium", window=128_000)
        for j in range(2 + k):
            child.advance(min_)
            standard_response(child, 800, 2_500, out=200 + 15 * j, inp=20_000 + 4_000 * j, cached=15_000 + 4_000 * j, window=128_000)
        if k != 0:
            child.complete_turn()
        else:
            child.t = max(child.t, now_ms - 5_000)
            child.item("Reasoning", 1_000)
        child.write(root, name_time=f"2026-09-22T10-1{k}-00")
    review = Rollout(fixture_uuid(0x26), cwd=multi_cwd(git_cwd), start_ms=now_ms - 20 * min_,
                     thread_source="guardian_review", source={"subagent": {"thread_spawn": {"parent_thread_id": multi.id}}})
    review.start_turn(model="gpt-6-luna", effort="low")
    standard_response(review, 0, 1_000, out=40, inp=8_000, cached=0)
    review.complete_turn()
    review.write(root, name_time="2026-09-22T10-16-00")

    # fresh: a session that has not finished a response
    fresh = Rollout(fixture_uuid(EDGE_IDS["fresh"]), cwd="/fixture/projects/just-started", start_ms=now_ms - 2 * min_)
    fresh.start_turn(model="gpt-6-sol", effort="high", window=258_400)
    fresh.item("Reasoning", 800)
    ids["fresh"] = fresh.id
    fresh.write(root, name_time="2026-09-22T10-05-00")

    # gaps: four requests on a priced model, one complete, one without its output count, one
    # without its input count, one without its cache-write count (a model that prices writes
    # apart from input); only what was recorded can be priced, and the panel must say so
    gaps = Rollout(fixture_uuid(EDGE_IDS["gaps"]), cwd="/fixture/projects/usage-gaps", start_ms=now_ms - 35 * min_)
    gaps.start_turn(model="gpt-6-sol", effort="medium", window=258_400)
    for usage in (
        {"input_tokens": 50_000, "cached_input_tokens": 40_000, "cache_write_input_tokens": 5_000, "output_tokens": 800},
        {"input_tokens": 52_000, "cached_input_tokens": 45_000, "cache_write_input_tokens": 2_000},
        {"cached_input_tokens": 47_000, "cache_write_input_tokens": 1_000, "output_tokens": 600},
        {"input_tokens": 56_000, "cached_input_tokens": 50_000, "output_tokens": 500},
    ):
        gaps.advance(4 * min_)
        gaps.item("Reasoning", 1_200).item("AgentMessage", 3_000).advance(200)
        usage_raw(gaps, dict(usage, reasoning_output_tokens=0), window=258_400)
    gaps.complete_turn()
    gaps.t = max(gaps.t, now_ms - 18 * min_)
    ids["gaps"] = gaps.id
    gaps.write(root, name_time="2026-09-22T10-06-00")

    # many: more requests than a session keeps samples of (telemetry.SAMPLE_HISTORY, 240), so
    # the cost line starts from what came before its first point and still ends at the total
    many = Rollout(fixture_uuid(EDGE_IDS["many"]), cwd="/fixture/projects/a-long-session", start_ms=now_ms - 300 * min_)
    many.start_turn(model="gpt-6-luna", effort="medium", window=128_000)
    for i in range(260):
        many.advance(60_000)
        many.item("AgentMessage", 2_000).advance(100)
        usage_full(many, inp=20_000 + (i % 7) * 1_000, cached=18_000, out=300, window=128_000)
    many.complete_turn()
    many.t = max(many.t, now_ms - 25 * min_)
    ids["many"] = many.id
    many.write(root, name_time="2026-09-22T10-07-00")

    # untimed: six timed replies, then fourteen with nothing to time (tool calls only), more
    # than the last sixteen replies hold; the average's five replies must all still be drawn
    unt = Rollout(fixture_uuid(EDGE_IDS["untimed"]), cwd="/fixture/projects/tool-heavy", start_ms=now_ms - 45 * min_)
    unt.start_turn(model="gpt-6.1-sol", effort="high", window=380_000)
    for i in range(6):
        unt.advance(min_)
        standard_response(unt, 1_200, 3_000 + i * 300, out=500 + i * 60, inp=40_000 + i * 2_000, cached=38_000 + i * 2_000, window=380_000)
    for i in range(14):
        unt.advance(40_000)
        usage_full(unt, inp=52_000 + i * 500, cached=50_000 + i * 500, out=40, window=380_000)
    unt.complete_turn()
    unt.t = max(unt.t, now_ms - 12 * min_)
    ids["untimed"] = unt.id
    unt.write(root, name_time="2026-09-22T10-08-00")

    # quota readings from another session of the same account: the same limit and newer, so the
    # line goes on to it; and, for a second account, a different limit, so it does not
    # the peers are the newest files, so the server's look at the latest few sessions finds them
    def quota_pair(own_role: str, peer_role: str, account: str, peer_limit: str, peer_offset_ms: int) -> None:
        own = Rollout(fixture_uuid(EDGE_IDS[own_role]), cwd=f"/fixture/projects/{own_role}", start_ms=now_ms - 70 * min_, account=account)
        own.start_turn(model="gpt-6-sol", effort="high", window=258_400)
        for i in range(4):
            own.advance(8 * min_)
            standard_response(own, 1_000, 3_000, out=300, inp=30_000 + i * 5_000, cached=28_000 + i * 5_000, window=258_400,
                              rate_limits=edge_limits(40.0 + i * 3, now_ms))
        own.complete_turn()
        own.t = now_ms - 30 * min_
        ids[own_role] = own.id
        own.write(root, name_time="2026-09-22T09-0" + ("1" if own_role == "quota-joined" else "2") + "-00")
        peer = Rollout(fixture_uuid(EDGE_IDS[peer_role]), cwd=f"/fixture/projects/{peer_role}", start_ms=now_ms - 6 * min_, account=account)
        peer.start_turn(model="gpt-6-sol", effort="high", window=258_400)
        standard_response(peer, 1_000, 3_000, out=300, inp=20_000, cached=18_000, window=258_400,
                          rate_limits=edge_limits(57.0, now_ms, limit_id=peer_limit))
        peer.complete_turn()
        peer.t = now_ms - peer_offset_ms
        ids[peer_role] = peer.id
        peer.write(root, name_time="2026-09-22T11-0" + ("1" if own_role == "quota-joined" else "2") + "-00")

    quota_pair("quota-joined", "quota-joined-peer", "fixture-account-edge-a", "fixture-limit", 400)
    quota_pair("quota-apart", "quota-apart-peer", "fixture-account-edge-b", "fixture-limit-other", 300)
    return ids


def multi_cwd(git_cwd: Optional[str]) -> str:
    return git_cwd or "/fixture/projects/a-project-with-a-rather-long-directory-name-for-the-menu"


if __name__ == "__main__":  # pragma: no cover - harness helper
    import sys
    import time

    a, b = sys.argv[1], sys.argv[2]
    now = int(sys.argv[3]) if len(sys.argv) > 3 else int(time.time() * 1000)
    print(json.dumps(build_harness_fixtures(a, b, now)))
