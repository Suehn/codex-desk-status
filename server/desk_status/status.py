"""Snapshot assembly: binding resolution + metrics derived from SessionState."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from . import SNAPSHOT_SCHEMA, __version__
from .discovery import (
    DEFAULT_KINDS,
    SessionFile,
    SessionIndex,
    normalize_session_id,
    project_name,
    root_label,
    short_id,
)
from .gitinfo import GitCache
from .pricing import PRICES_AS_OF, public_rate, request_cost, speed_mode
from .telemetry import SessionLog, SessionState

THROUGHPUT_GROUP = 5
THROUGHPUT_SERIES = 12  # the speed card's columns: the averaged replies and the ones before them
RECENT_SAMPLES = 16
SESSION_LIST_LIMIT = 20
LOG_CACHE = 6
# a session's subagents and automatic reviews: read like the session itself, from a cache of
# their own so they never push the selected log out
CHILD_LOG_CACHE = 96
MAX_CHILDREN = 64
MAX_CHILD_DEPTH = 4
# responses on the session card's line, the session's own and its children's together
TIMELINE_POINTS = 360
RATE_LIMIT_PEERS = 4
MAX_META_KEYS = 32

# Explicit allowlist of keys that name the *current* thread inside a request's
# ``params._meta``. None is a documented contract, so a value is only used if
# it exactly equals a verified local session id. Keys that may name a parent,
# source or related thread (parentThreadId, relatedThreadId, …) are never
# consulted, and no other key is scanned heuristically.
_NESTED_META_KEYS = ("x-codex-turn-metadata",)
_NESTED_ID_FIELDS = ("thread_id", "threadId")
_FLAT_ID_KEYS = (
    "codex/threadId",
    "codex/thread_id",
    "openai/threadId",
    "openai/thread_id",
    "threadId",
    "thread_id",
)


def extract_host_candidates(meta: Any) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return ([(source, normalized_uuid)], keys_seen). Values never logged."""
    candidates: List[Tuple[str, str]] = []
    keys_seen: List[str] = []
    if not isinstance(meta, dict):
        return candidates, keys_seen
    for k in list(meta.keys())[:MAX_META_KEYS]:
        if isinstance(k, str):
            keys_seen.append(k[:64])
    for nk in _NESTED_META_KEYS:
        nested = meta.get(nk)
        if isinstance(nested, str) and len(nested) <= 65536:
            try:
                nested = json.loads(nested)
            except (ValueError, RecursionError):
                nested = None
        if isinstance(nested, dict):
            for sub in list(nested.keys())[:MAX_META_KEYS]:
                if isinstance(sub, str):
                    keys_seen.append(f"{nk}.{sub[:48]}")
            for field in _NESTED_ID_FIELDS:
                sid = normalize_session_id(nested.get(field))
                if sid:
                    candidates.append((f"{nk}.{field}", sid))
    for fk in _FLAT_ID_KEYS:
        sid = normalize_session_id(meta.get(fk))
        if sid:
            candidates.append((fk, sid))
    seen = set()
    uniq = []
    for src, sid in candidates:
        if (src, sid) not in seen:
            seen.add((src, sid))
            uniq.append((src, sid))
    return uniq, sorted(set(keys_seen))


class StatusService:
    def __init__(self, index: SessionIndex, git: GitCache, clock=time.time):
        self.index = index
        self.git = git
        self.clock = clock
        self._logs: "OrderedDict[str, SessionLog]" = OrderedDict()
        self._child_logs: "OrderedDict[str, SessionLog]" = OrderedDict()
        self.last_host_keys: List[str] = []

    # ------------------------------------------------------------------
    # A session's reader is kept by its first file: when its thread runs on into a further file,
    # the same reader reads on into it, from where it stood, and nothing read is read again.
    @staticmethod
    def _cached_log(cache: "OrderedDict[str, SessionLog]", sf: SessionFile, cap: int) -> SessionLog:
        log = cache.get(sf.first)
        if log is None:
            log = SessionLog(sf.segments)
            cache[sf.first] = log
            while len(cache) > cap:
                cache.popitem(last=False)
        else:
            cache.move_to_end(sf.first)
            log.set_paths(sf.segments)
        log.refresh()
        return log

    def _log_for(self, sf: SessionFile) -> SessionLog:
        return self._cached_log(self._logs, sf, LOG_CACHE)

    def _child_log(self, sf: SessionFile) -> SessionLog:
        return self._cached_log(self._child_logs, sf, CHILD_LOG_CACHE)

    @staticmethod
    def _turn_status(st: SessionState) -> str:
        if st.current_turn_id and st.current_turn_id in st.turns:
            return st.turns[st.current_turn_id]["status"]
        return "none"

    @staticmethod
    def _timeline_rows(st: SessionState) -> List[List[Any]]:
        """One row per response: [at, cost in USD or None when unpriced, flags], the flags a sum
        of 1 long context, 2 cache counts missing (priced as uncached), 4 usage missing (only the
        recorded parts priced). The cost is the one the session's usage totals were summed from."""
        return [
            [s["at"], s.get("cost"), sum(bit for bit, on in zip((1, 2, 4), s.get("costFlags") or ()) if on)]
            for s in st.samples
            if s.get("at") is not None
        ]

    def _children(self, sf: SessionFile, st: SessionState) -> List[Dict[str, Any]]:
        """Every thread the session spawned, theirs too (breadth first, bounded), each with its
        token totals per model."""
        out: List[Dict[str, Any]] = []
        seen = {sf.id}
        queue = [(sf.id, 0)]
        since = st.started_at_ms
        while queue and len(out) < MAX_CHILDREN:
            pid, depth = queue.pop(0)
            if depth >= MAX_CHILD_DEPTH:
                continue
            for c in sorted(self.index.children(pid, since), key=lambda c: c.mtime_ms):
                if c.id in seen or len(out) >= MAX_CHILDREN:
                    continue
                seen.add(c.id)
                head = self.index.head(c)
                cs = self._child_log(c).state
                usage = [dict(b, model=k[0] or None, serviceTier=k[1] or None) for k, b in cs.usage_by_model.items()]
                out.append({
                    "id": c.id,
                    "shortId": short_id(c.id),
                    "parentId": pid,
                    "kind": head.get("kind", "subagent"),
                    "name": head.get("agentName"),
                    "path": head.get("agentPath"),
                    "depth": depth + 1,
                    "model": cs.model or head.get("model"),
                    "effort": cs.effort or head.get("effort"),
                    "startedAt": cs.started_at_ms or head.get("startedAt"),
                    "lastEventAt": cs.last_event_ms,
                    "status": self._turn_status(cs),
                    "responses": sum(b["responses"] for b in cs.usage_by_model.values()),
                    "usage": usage,
                })
                queue.append((c.id, depth + 1))
        return out

    def _bindable(self, sid: str) -> bool:
        """A host id binds only to a file whose session_meta.id agrees with
        the id in its filename (a renamed/copied foreign log never does)."""
        sf = self.index.get(sid)
        return sf is not None and self.index.verified(sf)

    def resolve_host(self, meta: Any, hint: Optional[str]) -> Dict[str, Any]:
        candidates, keys = extract_host_candidates(meta)
        self.last_host_keys = keys
        base = {"keysSeen": keys}
        local = sorted({sid for _, sid in candidates if self._bindable(sid)})
        if len(local) > 1:
            # Recognised current-thread keys disagree: never guess.
            return dict(base, status="ambiguous", sessionId=None, source=None, candidateCount=len(local))
        if local:
            src = next(s for s, sid in candidates if sid == local[0])
            return dict(base, status="matched", sessionId=local[0], source=src)
        if candidates:
            # The host named a current thread that is not a verified local
            # session; a relayed hint must not override that.
            return dict(base, status="unmatched", sessionId=None, source=None, candidateCount=len(candidates))
        hint_id = normalize_session_id(hint) if hint else None
        if hint_id and self._bindable(hint_id):
            return dict(base, status="matched", sessionId=hint_id, source="app-relay")
        status = "unmatched" if hint_id else "absent"
        return dict(base, status=status, sessionId=None, source=None, candidateCount=1 if hint_id else 0)

    # ------------------------------------------------------------------
    def snapshot(
        self,
        *,
        manual_id: Optional[str] = None,
        host_meta: Any = None,
        host_hint: Optional[str] = None,
        thread_id: Optional[str] = None,
        include_sessions: bool = False,
        all_kinds: bool = False,
        seq: Optional[int] = None,
        tool: str = "read_status",
    ) -> Dict[str, Any]:
        now_ms = int(self.clock() * 1000)
        host = self.resolve_host(host_meta, host_hint)
        warnings: List[str] = []

        manual = normalize_session_id(manual_id) if manual_id else None
        binding: Dict[str, Any] = {"host": host, "suggestedSessionId": None}
        selected: Optional[SessionFile] = None
        if manual:
            selected = self.index.get(manual)
            if selected is None:
                binding.update(state="manual_missing", sessionId=manual)
                warnings.append("manual_session_missing")
            else:
                binding.update(state="manual", sessionId=manual)
        elif host["status"] == "matched":
            selected = self.index.get(host["sessionId"])
            binding.update(state="host", sessionId=host["sessionId"])
        else:
            binding.update(state="none", sessionId=None)
            if host["status"] == "unmatched":
                warnings.append("host_id_not_local")
            elif host["status"] == "ambiguous":
                warnings.append("host_id_ambiguous")

        kinds = None if all_kinds else DEFAULT_KINDS
        recent = self.index.recent(SESSION_LIST_LIMIT, kinds)
        if binding["state"] == "none" and recent:
            # A suggestion only; never auto-selected.
            binding["suggestedSessionId"] = recent[0].id

        session_block = None
        state: Optional[SessionState] = None
        if selected is not None:
            log = self._log_for(selected)
            state = log.state
            session_block = self._session_block(selected, log, now_ms)
            if session_block["idMismatch"]:
                warnings.append("session_id_mismatch")
            if not log.caught_up:
                warnings.append("catching_up")
            if log.error:
                warnings.append("read_error")
            # the thread's record may be incomplete: never shown as if it were whole
            if selected.forked:
                warnings.append("segments_ambiguous")
            if selected.unjoined:
                warnings.append("segments_unjoined")
            if log.dropped:
                warnings.append("segments_dropped")
            if log.late_bytes > 0:
                warnings.append("segments_late")

        # A thread the session spawned, viewed in its place: only one of the session's own
        thread_block = None
        thread = normalize_session_id(thread_id) if thread_id else None
        if thread and session_block is not None and any(c["id"] == thread for c in session_block["children"]):
            tsf = self.index.get(thread)
            if tsf is not None:
                thread_block = self._session_block(tsf, self._child_log(tsf), now_ms, with_children=False)

        want_list = include_sessions or binding["state"] in ("none", "manual_missing")
        sessions = self._session_list(recent, host.get("sessionId"), binding.get("sessionId")) if want_list else None
        hidden = self.index.count_hidden() if want_list and not all_kinds else 0

        roots = self.index.root_summary()
        if not any(r["exists"] for r in roots):
            warnings.append("no_session_roots")
        if self.index.truncated:
            warnings.append("index_truncated")

        return {
            "schema": SNAPSHOT_SCHEMA,
            "server": {"version": __version__, "time": now_ms},
            "request": {"seq": seq, "tool": tool, "manualSessionId": manual, "threadId": thread},
            "binding": binding,
            "session": session_block,
            "thread": thread_block,
            "rateLimits": self._rate_limits(state, selected, self.index.recent(RATE_LIMIT_PEERS + 1, None), now_ms),
            "pricing": {"asOf": PRICES_AS_OF},
            "sessions": sessions,
            "sessionFilter": {"allKinds": all_kinds, "hidden": hidden},
            "roots": roots,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    def _session_block(self, sf: SessionFile, log: SessionLog, now_ms: int, with_children: bool = True) -> Dict[str, Any]:
        st = log.state
        children = self._children(sf, st) if with_children else []
        # The selectable identity is always the filename id; a differing
        # session_meta.id is surfaced instead of silently adopted.
        sid = sf.id
        cwd = st.cwd or st.meta_cwd
        turn = None
        if st.current_turn_id and st.current_turn_id in st.turns:
            t = st.turns[st.current_turn_id]
            turn = {
                "status": t["status"],
                "turnShortId": short_id(st.current_turn_id),
                "startedAt": t.get("startedAt"),
                "endedAt": t.get("endedAt"),
                "reason": t.get("reason"),
            }
        return {
            "id": sid,
            "shortId": short_id(sid),
            "idMismatch": not self.index.verified(sf) or bool(st.session_id and st.session_id != sf.id),
            "project": project_name(cwd),
            "cwd": cwd,
            "startedAt": st.started_at_ms,
            "cliVersion": st.cli_version,
            "originator": st.originator,
            "model": st.model,
            "effort": st.effort,
            "serviceTier": st.service_tier,
            "settingsSource": st.settings_source,
            # token totals per model and tier, for pricing a session that switched models
            "usage": [
                dict(b, model=k[0] or None, serviceTier=k[1] or None) for k, b in st.usage_by_model.items()
            ],
            "children": children,
            # every response of this thread, oldest first, for the cost line
            "timeline": self._timeline_rows(st)[-TIMELINE_POINTS:],
            "turn": turn or {"status": "none"},
            "lastEventAt": st.last_event_ms,
            "file": {
                "root": root_label(sf.root),
                "size": log.size,
                "mtime": log.mtime_ms,
                "alternates": len(sf.alternates),
                # the files the thread was written to, read as one (1 unless it ran on)
                "segments": len(log.paths),
                "lateBytes": log.late_bytes,
                # files named as this thread's that did not join it; two that would take one place
                "unjoined": sf.unjoined,
                "forked": sf.forked,
                "dropped": log.dropped,
                "caughtUp": log.caught_up,
                "readBytes": log.offset,
                "pendingTailBytes": log.pending_tail_bytes,
                "resets": log.resets,
            },
            "context": self._context(st),
            "cache": self._cache(st),
            "throughput": self._throughput(st),
            "git": self.git.get(cwd),
            "diagnostics": dict(st.counters),
        }

    @staticmethod
    def _context(st: SessionState) -> Dict[str, Any]:
        window = st.context_window
        history = [
            {"t": h["t"], "used": h["used"], "window": h["window"] or window or 0}
            for h in st.context_history
        ]
        latest = st.latest_usage
        source = "token_usage_record"
        if latest is None and st.latest_tc_usage is not None:
            latest = st.latest_tc_usage
            source = "token_count"
        if latest is None:
            return {"state": "no_data", "windowTokens": window, "history": history}
        used = latest["usage"].get("total_tokens")
        at = latest["at"]
        if st.compacted_at_ms is not None and st.compacted_at_ms >= at:
            return {
                "state": "compacted_pending",
                "windowTokens": window,
                "compactedAt": st.compacted_at_ms,
                "previousUsedTokens": used,
                "history": history,
            }
        if not window:
            return {"state": "no_window", "usedTokens": used, "at": at, "source": source, "history": history}
        return {
            "state": "ok",
            "usedTokens": used,
            "windowTokens": window,
            "ratio": (used / window) if used is not None else None,
            "at": at,
            "source": source,
            "compactedAt": st.compacted_at_ms,
            "history": history,
        }

    @staticmethod
    def _cache(st: SessionState) -> Dict[str, Any]:
        """cached_input_tokens / input_tokens. An absent cached count is "not
        recorded", never a zero hit rate; the recent ratio only sums responses
        that carry both numbers and reports how many were usable."""
        latest_rec = st.latest_usage or st.latest_tc_usage
        latest = None
        if latest_rec is not None:
            u = latest_rec["usage"]
            inp, cached = u.get("input_tokens"), u.get("cached_input_tokens")
            if inp is not None:
                complete = cached is not None
                # The cache is read or written when the request is processed, not when
                # the response ends: the response's first timed item is the closest
                # logged moment to that. Absent (no timing), the panel falls back to "at".
                rid = latest_rec.get("responseId")
                sample = None
                if rid:
                    for s in reversed(st.samples):
                        if s.get("responseId") == rid:
                            sample = s
                            break
                latest = {
                    "inputTokens": inp,
                    "cachedInputTokens": cached if complete else None,
                    "cacheWriteInputTokens": u.get("cache_write_input_tokens"),
                    "outputTokens": u.get("output_tokens"),
                    "ratio": (min(cached, inp) / inp) if complete and inp else None,
                    "state": "ok" if complete else "missing_cached",
                    "at": latest_rec["at"],
                    "requestAt": sample.get("startedAt") if sample else None,
                    # what the request ran on: a later switch of either reprices the next one,
                    # and a switch of model starts its cache cold
                    "model": sample.get("model") if sample else None,
                    "serviceTier": sample.get("serviceTier") if sample else None,
                }
        considered = [s for s in st.samples if s.get("inputTokens") is not None][-THROUGHPUT_GROUP:]
        complete = [s for s in considered if s.get("cachedInputTokens") is not None]
        recent = None
        if considered:
            inp = sum(s["inputTokens"] for s in complete)
            cached = sum(min(s["cachedInputTokens"], s["inputTokens"]) for s in complete)
            recent = {
                "responses": len(complete),
                "considered": len(considered),
                "inputTokens": inp if complete else None,
                "cachedInputTokens": cached if complete else None,
                "ratio": (cached / inp) if complete and inp else None,
            }
        # the hit rate of each response that recorded both counts, for the cache card's line
        history = [
            [s["at"], min(s["cachedInputTokens"], s["inputTokens"]) / s["inputTokens"]]
            for s in st.samples
            if s.get("at") is not None and s.get("inputTokens") and s.get("cachedInputTokens") is not None
        ]
        return {"latest": latest, "recent": recent, "history": history,
                "estimate": StatusService._cache_estimate(st, latest)}

    @staticmethod
    def _cache_estimate(st: SessionState, latest: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """What the next request's input might cost, as conditional scenarios at the built-in
        API prices: its prompt taken as this one plus this response, either read from the cache
        (this prompt) or not. Tokens not read are billed as input, or at the cache-write rate
        where they are written to the cache and the model prices writes apart; the log does not
        say beforehand which, so each scenario is a range from the one to the other (the same
        figure where the rates agree). Neither is a prediction of the real next prompt. After a
        switch of model the cache written for the old one is not expected to be read."""
        if not latest or latest.get("inputTokens") is None:
            return None
        ran_on = latest.get("model") or st.model
        nxt = st.model or ran_on
        inp, out = latest["inputTokens"], latest.get("outputTokens") or 0
        prompt = inp + out
        switched = bool(latest.get("model") and st.model and latest["model"] != st.model)
        rate = public_rate(nxt, st.service_tier, prompt)
        if rate is None:
            return {"model": nxt, "tier": st.service_tier, "ranOn": ran_on, "switched": switched, "prompt": prompt, "priced": False}
        est = {
            "model": nxt, "tier": st.service_tier, "ranOn": ran_on, "switched": switched, "prompt": prompt,
            "priced": True, "long": rate["long"], "rate": rate,
            "readTokens": inp, "newTokens": out,
            "cold": prompt * rate["input"] / 1e6,
            "coldWritten": prompt * rate["write"] / 1e6,
            "warm": None if switched else (inp * rate["read"] + out * rate["input"]) / 1e6,
            "warmWritten": None if switched else (inp * rate["read"] + out * rate["write"]) / 1e6,
        }
        # what the cache saved on the last request itself, against sending its input uncached
        if latest.get("cachedInputTokens") is not None:
            usage = {"input_tokens": inp, "cached_input_tokens": latest["cachedInputTokens"],
                     "cache_write_input_tokens": latest.get("cacheWriteInputTokens") or 0, "output_tokens": 0}
            paid = request_cost(ran_on, latest.get("serviceTier"), usage)
            full = request_cost(ran_on, latest.get("serviceTier"), {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": 0})
            if paid is not None and full is not None:
                est["saved"] = {"paid": paid["read"] + paid["write"] + paid["uncached"], "full": full["uncached"]}
        if switched:
            # staying on the model the cache was written for, should it still be there
            prev = public_rate(ran_on, latest.get("serviceTier"), prompt)
            if prev is not None:
                est["stayWarm"] = (inp * prev["read"] + out * prev["input"]) / 1e6
                est["stayWarmWritten"] = (inp * prev["read"] + out * prev["write"]) / 1e6
        return est

    @staticmethod
    def _throughput(st: SessionState) -> Dict[str, Any]:
        samples = list(st.samples)

        def matches(s: Dict[str, Any], model: Optional[str], effort: Optional[str], tier: Optional[str]) -> bool:
            return bool(model and effort and s["model"] == model and s["effort"] == effort
                        and speed_mode(s.get("serviceTier")) == speed_mode(tier))

        def group(model: Optional[str], effort: Optional[str], tier: Optional[str], limit: int) -> List[Dict[str, Any]]:
            """The latest valid samples on this model, effort and tier, newest first, from every
            sample kept, not only the last few: replies without timing in between do not
            push the averaged ones out of view."""
            out: List[Dict[str, Any]] = []
            if model and effort:
                for s in reversed(samples):
                    if s["valid"] and matches(s, model, effort, tier):
                        out.append(s)
                        if len(out) >= limit:
                            break
            return out

        # The model, effort and tier now set; until a response has run on them (a switch just made),
        # the ones the latest timed response ran on, said so rather than shown as no data.
        model, effort, tier = st.model, st.effort, st.service_tier
        drawn = group(model, effort, tier, THROUGHPUT_SERIES)
        current = True
        if not drawn:
            last = next((s for s in reversed(samples) if s["valid"] and s["model"] and s["effort"]), None)
            if last is not None and not matches(last, model, effort, tier):
                model, effort, tier, current = last["model"], last["effort"], last.get("serviceTier"), False
                drawn = group(model, effort, tier, THROUGHPUT_SERIES)
        # the average is the newest of the drawn ones, so every reply it is made of is drawn
        matching = drawn[:THROUGHPUT_GROUP]
        series = [
            {"at": s["at"], "outputTokens": s["outputTokens"], "durationMs": s["durationMs"],
             "tokensPerSecond": s["tokensPerSecond"], "inAggregate": i < len(matching)}
            for i, s in reversed(list(enumerate(drawn)))
        ]
        aggregate = None
        if matching:
            out = sum(s["outputTokens"] for s in matching)
            dur = sum(s["durationMs"] for s in matching)
            aggregate = {
                "samples": len(matching),
                "target": THROUGHPUT_GROUP,
                "outputTokens": out,
                "durationMs": dur,
                "tokensPerSecond": (out * 1000.0 / dur) if dur > 0 else None,
                "from": matching[-1]["at"],
                "to": matching[0]["at"],
            }
        latest = samples[-1] if samples else None
        recent = []  # the last few replies as logged, timed or not, for the latest one's reason
        for s in samples[-RECENT_SAMPLES:]:
            recent.append(
                {
                    "at": s["at"],
                    "outputTokens": s["outputTokens"],
                    "durationMs": s["durationMs"],
                    "tokensPerSecond": s["tokensPerSecond"],
                    "valid": s["valid"],
                    "reason": s["invalidReason"],
                    "matches": matches(s, model, effort, tier),
                }
            )

        def pub(s: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if s is None:
                return None
            return {
                "responseShortId": (s["responseId"] or "")[-8:] or None,
                "at": s["at"],
                "model": s["model"],
                "effort": s["effort"],
                "serviceTier": s.get("serviceTier"),
                "outputTokens": s["outputTokens"],
                "reasoningTokens": s["reasoningTokens"],
                "inputTokens": s["inputTokens"],
                "durationMs": s["durationMs"],
                "tokensPerSecond": s["tokensPerSecond"],
                "valid": s["valid"],
                "reason": s["invalidReason"],
                "matches": matches(s, model, effort, tier),
            }

        return {
            "model": model,
            "effort": effort,
            "serviceTier": tier,
            "current": current,
            "aggregate": aggregate,
            "latest": pub(latest),
            "recent": recent,
            "series": series,
            "validSamples": sum(1 for s in samples if s["valid"]),
            "totalSamples": len(samples),
        }

    def _rate_limits(
        self,
        st: Optional[SessionState],
        selected: Optional[SessionFile],
        recent: List[SessionFile],
        now_ms: int,
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Tuple[int, Dict[str, Any], Optional[str], bool]] = None
        own_account = st.account_key if st is not None else None
        if st is not None and st.rate_limits is not None and st.rate_limits_at_ms is not None:
            best = (st.rate_limits_at_ms, st.rate_limits, st.session_id, True)
        # Rate limits are per account. Peers are only consulted when they are
        # provably the same account as the selected session, or when no
        # session is selected (then the freshest local snapshot is shown).
        if st is None or own_account:
            for sf in recent[:RATE_LIMIT_PEERS]:
                if selected is not None and sf.path == selected.path:
                    continue
                info = self.index.label(sf)
                if info.get("rateLimits") is None or info.get("rateLimitsAt") is None:
                    continue
                if st is not None and info.get("accountKey") != own_account:
                    continue
                if best is None or info["rateLimitsAt"] > best[0]:
                    best = (info["rateLimitsAt"], info["rateLimits"], sf.id, False)
        if best is None:
            return None
        at, rl, sid, own = best
        limit = rl.get("limitId") or rl.get("limitName")
        history = list(st.rate_history) if st is not None else []
        # The reading may come from another of the account's sessions. It joins this session's
        # line only when it is the same limit, its windows are ones the line already draws, and
        # it is newer than the line's last point; else the line stays this session's own record.
        joined = False
        if not own and st is not None and history:
            last = history[-1]
            used = {str(w.get("windowMinutes")): w.get("usedPercent") for w in rl["windows"] if w.get("usedPercent") is not None}
            if limit and last.get("id") == limit and at > last["t"] and used and set(used) <= set(last["w"]):
                history.append({"t": at, "w": used, "id": limit, "from": short_id(sid)})
                joined = True
        windows = []
        for w in rl["windows"]:
            ww = dict(w)
            ww["expired"] = bool(w.get("resetsAt") and w["resetsAt"] <= now_ms)
            windows.append(ww)
        return {
            "at": at,
            "sourceSessionShortId": short_id(sid),
            "fromSelectedSession": own,
            "planType": rl.get("planType"),
            "limitName": rl.get("limitName") or rl.get("limitId"),
            "reachedType": rl.get("reachedType"),
            "credits": rl.get("credits"),
            "limitId": limit,
            "windows": windows,
            "history": history,
            "historyJoined": joined,
        }

    def _session_list(self, recent: List[SessionFile], host_id: Optional[str], bound_id: Optional[str]) -> List[Dict[str, Any]]:
        out = []
        for sf in recent:
            info = self.index.label(sf)
            out.append(
                {
                    "id": sf.id,
                    "shortId": short_id(sf.id),
                    "project": project_name(info.get("cwd")),
                    "cwd": info.get("cwd"),
                    "startedAt": info.get("startedAt"),
                    "lastActivityAt": info.get("lastEventAt") or sf.mtime_ms,
                    "model": info.get("model"),
                    "effort": info.get("effort"),
                    "root": root_label(sf.root),
                    "kind": info.get("kind", "user"),
                    "alternates": len(sf.alternates),
                    "isHost": sf.id == host_id,
                    "verified": self.index.verified(sf),
                    "isSelected": sf.id == bound_id,
                }
            )
        return out
