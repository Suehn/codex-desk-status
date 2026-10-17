"""Incremental, bounded, read-only parser for Codex rollout JSONL logs.

Design notes
------------
* Byte-offset incremental reads: each refresh only reads bytes appended since
  the previous refresh. A trailing line without a newline is held back until it
  is completed, so half-written JSON is never parsed.
* Rotation / truncation / replacement is detected via (dev, inode), size shrink
  and a head signature; any of them resets the parser and re-reads the file.
* One thread may be written as a chain of rollout files, ``rollout-<time>-<thread>.jsonl``
  and then ``rollout-<time>-<thread>_<segment>.jsonl`` (discovery verifies the links). A
  ``SessionLog`` reads the chain in order into one state: each file to its end before the next
  is begun; once a later file is begun, an earlier one is not read again (it ends where its
  writer handed over). Responses are counted once by ``response_id`` across the chain.
* Memory is bounded: lines longer than ``MAX_LINE_BYTES`` are skipped, history
  structures are capped deques / ordered dicts, and only a handful of scalar
  fields are retained from each event (never message text).
* Throughput samples come exclusively from ``token_usage_record`` (one per
  completed model response, deduplicated by ``response_id``). ``token_count``
  notifications are only used for context-window fallback and rate limits.

Throughput sample definition (per response)::

    start    = min(started_at_ms) over Reasoning / AgentMessage item_completed
               events of the same turn, logged after the previous response
               boundary and starting at or after that boundary
    end      = timestamp of the token_usage_record line
    duration = end - start
    rate     = usage.output_tokens / duration

The response boundary is the previous token_usage_record of the same turn, or
the turn's task_started event for the first response of a turn.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from .pricing import request_cost

MAX_LINE_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 1 << 20
DEFAULT_READ_BUDGET = 96 << 20
HEAD_SIGNATURE_BYTES = 64
SAMPLE_HISTORY = 240
CONTEXT_HISTORY = 120
RATE_HISTORY = 120
TURN_HISTORY = 128
SEEN_RESPONSE_IDS = 8192
USAGE_BUCKETS = 32
PENDING_ITEMS_PER_TURN = 256
MIN_SAMPLE_DURATION_MS = 200
MAX_SAMPLE_DURATION_MS = 6 * 3600 * 1000
MAX_STRING = 1024

TIMING_ITEM_TYPES = ("Reasoning", "AgentMessage")
SKIP_TOP_LEVEL = {
    b"response_item",
    b"world_state",
    b"inter_agent_communication_metadata",
}
_TOP_TYPE_RE = re.compile(rb'"type"\s*:\s*"([A-Za-z_]+)"')
_ITEM_TYPE_RE = re.compile(rb'"item"\s*:\s*\{\s*"type"\s*:\s*"([A-Za-z_]+)"')
_ORDINAL_RE = re.compile(rb'"ordinal"\s*:\s*(\d+)')
_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?$"
)


# --------------------------------------------------------------------------
# Small value helpers (defensive: logs are untrusted input)
# --------------------------------------------------------------------------

def parse_ts_ms(value: Any) -> Optional[int]:
    """Parse an ISO-8601 timestamp into epoch milliseconds (UTC if naive).

    Works on Python 3.8+: trailing ``Z`` and >6 fractional digits are
    normalised before ``fromisoformat``.
    """
    if not isinstance(value, str) or not (10 <= len(value) <= 40):
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    base, frac, tz = m.group(1), m.group(2) or "", m.group(3) or ""
    frac = (frac + "000000")[:6]
    if tz in ("Z", "z", ""):
        tz = "+00:00"
    elif len(tz) == 5:
        tz = tz[:3] + ":" + tz[3:]
    try:
        dt = datetime.fromisoformat(f"{base.replace(' ', 'T')}.{frac}{tz}")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def as_int(value: Any, minimum: int = 0) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= minimum else None
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        if value.is_integer() and value >= minimum:
            return int(value)
    return None


def as_num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value and abs(value) != float("inf"):
        return float(value)
    return None


def as_str(value: Any, limit: int = MAX_STRING) -> Optional[str]:
    if isinstance(value, str) and value:
        return value[:limit]
    return None


def normalize_usage(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict):
        return None
    out: Dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        v = as_int(raw.get(key))
        if v is not None:
            out[key] = v
    if "total_tokens" not in out and "input_tokens" in out and "output_tokens" in out:
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out or None


def sanitize_rate_limits(raw: Any, log_ts_ms: Optional[int]) -> Optional[Dict[str, Any]]:
    """Keep only numeric window data and a few labels from a rate_limits blob."""
    if not isinstance(raw, dict):
        return None
    windows = []
    for key in ("primary", "secondary"):
        w = raw.get(key)
        if not isinstance(w, dict):
            continue
        used = as_num(w.get("used_percent"))
        minutes = as_int(w.get("window_minutes"), minimum=1)
        resets_at = as_int(w.get("resets_at"), minimum=1)
        resets_at_ms = resets_at * 1000 if resets_at is not None else None
        if resets_at_ms is None:
            # Older schema: relative seconds from the log line timestamp.
            rel = as_int(w.get("resets_in_seconds"))
            if rel is not None and log_ts_ms is not None:
                resets_at_ms = log_ts_ms + rel * 1000
        if used is None and minutes is None:
            continue
        windows.append(
            {
                "key": key,
                "usedPercent": used,
                "windowMinutes": minutes,
                "resetsAt": resets_at_ms,
            }
        )
    if not windows:
        return None
    credits = raw.get("credits")
    credit_info = None
    if isinstance(credits, dict):
        credit_info = {
            "hasCredits": credits.get("has_credits") is True,
            "unlimited": credits.get("unlimited") is True,
        }
    return {
        "windows": windows,
        "planType": as_str(raw.get("plan_type"), 64),
        "limitId": as_str(raw.get("limit_id"), 64),
        "limitName": as_str(raw.get("limit_name"), 64),
        "reachedType": as_str(raw.get("rate_limit_reached_type"), 64),
        "credits": credit_info,
    }


class BoundedDict(OrderedDict):
    """Insertion-ordered dict that evicts its oldest entries beyond ``cap``."""

    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap

    def __setitem__(self, key, value):  # type: ignore[override]
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.cap:
            self.popitem(last=False)


# --------------------------------------------------------------------------
# Parsed state
# --------------------------------------------------------------------------

class SessionState:
    """Everything the panel needs from one rollout file, with bounded memory."""

    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.meta_cwd: Optional[str] = None
        self.started_at_ms: Optional[int] = None
        self.cli_version: Optional[str] = None
        self.originator: Optional[str] = None
        self.account_key: Optional[str] = None  # compared only, never exported

        # Current settings (latest turn_context or thread_settings_applied).
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        self.cwd: Optional[str] = None
        self.settings_source: Optional[str] = None
        # "priority" (renamed Fast by OpenAI) or None for the standard tier; from thread_settings_applied
        self.service_tier: Optional[str] = None

        # Token and cost totals per (model, service tier) over every response read, each response
        # priced at its own rates (pricing.request_cost), so a session that switched models or ran
        # long-context requests adds up the same in its total, its parts and its cost line.
        self.usage_by_model: BoundedDict = BoundedDict(USAGE_BUCKETS)

        self.turn_settings: BoundedDict = BoundedDict(TURN_HISTORY)  # turn_id -> (model, effort)
        self.turns: BoundedDict = BoundedDict(TURN_HISTORY)  # turn_id -> dict
        self.current_turn_id: Optional[str] = None

        self.boundaries: BoundedDict = BoundedDict(TURN_HISTORY)  # turn_id -> boundary ms
        self.pending: BoundedDict = BoundedDict(TURN_HISTORY)  # turn_id -> [start ms]
        self.last_usage_ts_ms: Optional[int] = None

        self.samples: Deque[Dict[str, Any]] = deque(maxlen=SAMPLE_HISTORY)
        self.seen_responses: BoundedDict = BoundedDict(SEEN_RESPONSE_IDS)

        self.latest_usage: Optional[Dict[str, Any]] = None  # from token_usage_record
        self.latest_tc_usage: Optional[Dict[str, Any]] = None  # token_count fallback
        self.context_window: Optional[int] = None
        self.context_history: Deque[Dict[str, int]] = deque(maxlen=CONTEXT_HISTORY)
        self.compacted_at_ms: Optional[int] = None

        self.rate_limits: Optional[Dict[str, Any]] = None
        self.rate_limits_at_ms: Optional[int] = None
        # each change of a window's use, for the quota card's line: {t, w: {minutes: percent used}}
        self.rate_history: Deque[Dict[str, Any]] = deque(maxlen=RATE_HISTORY)

        self.last_event_ms: Optional[int] = None

        self.counters: Dict[str, int] = {
            "lines": 0,
            "parsedLines": 0,
            "badLines": 0,
            "oversizedLines": 0,
            "duplicateUsage": 0,
            "foreignUsage": 0,
            "staleItems": 0,
            "usageRecords": 0,
            "samplesWithoutTiming": 0,
        }

    # ---- event handlers -------------------------------------------------

    def handle(self, obj: Dict[str, Any]) -> None:
        top = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        ts = parse_ts_ms(obj.get("timestamp"))
        if ts is not None and (self.last_event_ms is None or ts > self.last_event_ms):
            self.last_event_ms = ts

        if top == "session_meta":
            self._on_session_meta(payload, ts)
        elif top == "turn_context":
            self._on_turn_context(payload)
        elif top == "token_usage_record":
            self._on_usage_record(payload, ts)
        elif top == "compacted":
            if ts is not None:
                self.compacted_at_ms = ts
        elif top == "event_msg":
            kind = payload.get("type")
            if kind == "item_completed":
                self._on_item_completed(payload)
            elif kind == "token_count":
                self._on_token_count(payload, ts)
            elif kind == "task_started":
                self._on_task_started(payload, ts)
            elif kind == "task_complete":
                self._on_turn_end(payload, ts, "error" if payload.get("error") else "completed")
            elif kind == "turn_aborted":
                self._on_turn_end(payload, ts, "interrupted")
            elif kind == "thread_settings_applied":
                self._on_thread_settings(payload)

    def _on_session_meta(self, p: Dict[str, Any], ts: Optional[int]) -> None:
        sid = as_str(p.get("id"), 64)
        if self.session_id is None and sid:
            self.session_id = sid.lower()
            self.meta_cwd = as_str(p.get("cwd"))
            self.started_at_ms = parse_ts_ms(p.get("timestamp")) or ts
            self.cli_version = as_str(p.get("cli_version"), 64)
            self.originator = as_str(p.get("originator"), 64)
            acct = as_str(p.get("creator_account_id"), 128)
            self.account_key = acct
            if self.cwd is None:
                self.cwd = self.meta_cwd

    def _on_turn_context(self, p: Dict[str, Any]) -> None:
        model = as_str(p.get("model"), 128)
        effort = as_str(p.get("effort"), 32)
        if effort is None:
            cm = p.get("collaboration_mode")
            if isinstance(cm, dict) and isinstance(cm.get("settings"), dict):
                effort = as_str(cm["settings"].get("reasoning_effort"), 32)
        turn_id = as_str(p.get("turn_id"), 128)
        if turn_id and (model or effort):
            self.turn_settings[turn_id] = (model, effort)
        if model:
            self.model = model
        if effort:
            self.effort = effort
        if model or effort:
            self.settings_source = "turn_context"
        cwd = as_str(p.get("cwd"))
        if cwd:
            self.cwd = cwd

    def _on_thread_settings(self, p: Dict[str, Any]) -> None:
        s = p.get("thread_settings")
        if not isinstance(s, dict):
            return
        model = as_str(s.get("model"), 128)
        effort = as_str(s.get("reasoning_effort"), 32)
        if model:
            self.model = model
        if effort:
            self.effort = effort
        if model or effort:
            self.settings_source = "thread_settings"
        if "service_tier" in s:
            self.service_tier = as_str(s.get("service_tier"), 32)
        cwd = as_str(s.get("cwd"))
        if cwd:
            self.cwd = cwd

    def _on_task_started(self, p: Dict[str, Any], ts: Optional[int]) -> None:
        turn_id = as_str(p.get("turn_id"), 128)
        started = ts
        if started is None:
            sec = as_num(p.get("started_at"))
            started = int(sec * 1000) if sec is not None else None
        window = as_int(p.get("model_context_window"), minimum=1)
        if window:
            self.context_window = window
        prev = self.turns.get(self.current_turn_id) if self.current_turn_id else None
        if prev is not None and prev.get("status") == "active" and self.current_turn_id != turn_id:
            # A new turn began without the previous one ending (crash/restart).
            prev["status"] = "superseded"
            prev["endedAt"] = started
        if not turn_id:
            return
        self.turns[turn_id] = {"status": "active", "startedAt": started, "endedAt": None, "reason": None}
        self.current_turn_id = turn_id
        if started is not None:
            self.boundaries[turn_id] = started
        self.pending[turn_id] = []

    def _on_turn_end(self, p: Dict[str, Any], ts: Optional[int], status: str) -> None:
        turn_id = as_str(p.get("turn_id"), 128)
        if not turn_id:
            return
        turn = self.turns.get(turn_id)
        if turn is None:
            turn = {"status": status, "startedAt": None, "endedAt": ts, "reason": None}
            self.turns[turn_id] = turn
        turn["status"] = status
        turn["endedAt"] = ts
        reason = as_str(p.get("reason"), 64)
        if reason:
            turn["reason"] = reason
        self.pending.pop(turn_id, None)

    def _on_item_completed(self, p: Dict[str, Any]) -> None:
        item = p.get("item")
        if not isinstance(item, dict) or item.get("type") not in TIMING_ITEM_TYPES:
            return
        turn_id = as_str(p.get("turn_id"), 128)
        start = as_int(p.get("started_at_ms"), minimum=1)
        done = as_int(p.get("completed_at_ms"), minimum=1)
        if not turn_id or start is None:
            return
        if done is not None and done < start:
            self.counters["staleItems"] += 1
            return
        boundary = self.boundaries.get(turn_id)
        if boundary is not None and start < boundary:
            # Started before the previous response boundary: belongs to an
            # earlier response (or clocks disagree). Never attribute it.
            self.counters["staleItems"] += 1
            return
        bucket = self.pending.get(turn_id)
        if bucket is None:
            bucket = []
            self.pending[turn_id] = bucket
        if len(bucket) < PENDING_ITEMS_PER_TURN:
            bucket.append(start)

    def _on_usage_record(self, p: Dict[str, Any], ts: Optional[int]) -> None:
        self.counters["usageRecords"] += 1
        thread_id = as_str(p.get("thread_id"), 64)
        if thread_id and self.session_id and thread_id.lower() != self.session_id:
            self.counters["foreignUsage"] += 1
            return
        response_id = as_str(p.get("response_id"), 256)
        if response_id:
            if response_id in self.seen_responses:
                self.counters["duplicateUsage"] += 1
                return
            self.seen_responses[response_id] = True
        usage = normalize_usage(p.get("usage"))
        if usage is None:
            return
        turn_id = as_str(p.get("turn_id"), 128)

        if ts is not None and usage.get("total_tokens") is not None:
            self.latest_usage = {"usage": usage, "at": ts, "responseId": response_id, "turnId": turn_id}
            self.context_history.append(
                {"t": ts, "used": usage["total_tokens"], "window": self.context_window or 0}
            )

        # ---- throughput sample ----
        model, effort, label_source = None, None, None
        if turn_id and turn_id in self.turn_settings:
            model, effort = self.turn_settings[turn_id]
            label_source = "turn_context"
        elif self.model or self.effort:
            model, effort = self.model, self.effort
            label_source = self.settings_source or "inferred"

        starts = self.pending.get(turn_id, []) if turn_id else []
        boundary = self.boundaries.get(turn_id) if turn_id else None
        first = None
        for s in starts:
            if boundary is not None and s < boundary:
                continue
            if first is None or s < first:
                first = s
        output = usage.get("output_tokens")
        reason = None
        duration = None
        if ts is None:
            reason = "no_timestamp"
        elif not response_id:
            reason = "no_response_id"
        elif first is None:
            reason = "no_timing"
        else:
            duration = ts - first
            if duration < 0:
                reason = "clock_skew"
            elif duration < MIN_SAMPLE_DURATION_MS:
                reason = "too_short"
            elif duration > MAX_SAMPLE_DURATION_MS:
                reason = "too_long"
            elif not output:
                reason = "no_output"
        if reason == "no_timing":
            self.counters["samplesWithoutTiming"] += 1
        valid = reason is None
        sample = {
            "responseId": response_id,
            "turnId": turn_id,
            "at": ts,
            "model": model,
            "effort": effort,
            "labelSource": label_source,
            "inputTokens": usage.get("input_tokens"),
            "cachedInputTokens": usage.get("cached_input_tokens"),
            "cacheWriteInputTokens": usage.get("cache_write_input_tokens"),
            "serviceTier": self.service_tier,
            "outputTokens": output,
            "reasoningTokens": usage.get("reasoning_output_tokens"),
            "totalTokens": usage.get("total_tokens"),
            "startedAt": first,
            "durationMs": duration if duration is not None and duration >= 0 else None,
            "valid": valid,
            "invalidReason": reason,
            "tokensPerSecond": (output * 1000.0 / duration) if valid and duration else None,
        }
        cost = request_cost(model, self.service_tier, usage)
        sample["cost"] = None if cost is None else cost["read"] + cost["write"] + cost["uncached"] + cost["output"]
        sample["costFlags"] = None if cost is None else (cost["long"], cost["cacheMissing"], cost["usageMissing"])
        self.samples.append(sample)
        self._add_usage(model, self.service_tier, usage, cost)

        # New response boundary for this turn.
        if turn_id and ts is not None:
            self.boundaries[turn_id] = ts
            self.pending[turn_id] = []
        if ts is not None:
            self.last_usage_ts_ms = ts

    def _add_usage(self, model: Optional[str], tier: Optional[str], usage: Dict[str, int],
                   cost: Optional[Dict[str, Any]]) -> None:
        key = (model or "", tier or "")
        b = self.usage_by_model.get(key)
        if b is None:
            b = {"input": 0, "cached": 0, "write": 0, "output": 0, "reasoning": 0, "responses": 0,
                 "priced": cost is not None, "cost": {"read": 0.0, "write": 0.0, "uncached": 0.0, "output": 0.0},
                 "longResponses": 0, "cacheMissing": 0, "usageMissing": 0}
            self.usage_by_model[key] = b
        inp = usage.get("input_tokens") or 0
        cached = min(usage.get("cached_input_tokens") or 0, inp)
        b["input"] += inp
        b["cached"] += cached
        b["write"] += min(usage.get("cache_write_input_tokens") or 0, inp - cached)
        b["output"] += usage.get("output_tokens") or 0
        b["reasoning"] += usage.get("reasoning_output_tokens") or 0
        b["responses"] += 1
        if cost is not None:
            for part in ("read", "write", "uncached", "output"):
                b["cost"][part] += cost[part]
            for flag in ("long", "cacheMissing", "usageMissing"):
                if cost[flag]:
                    b["longResponses" if flag == "long" else flag] += 1
        else:
            # unpriced, but a gap in its usage is still a gap in the token counts shown
            b["cacheMissing"] += 1 if "input_tokens" in usage and "cached_input_tokens" not in usage else 0
            b["usageMissing"] += 1 if "input_tokens" not in usage or "output_tokens" not in usage else 0

    def _on_token_count(self, p: Dict[str, Any], ts: Optional[int]) -> None:
        info = p.get("info")
        if isinstance(info, dict):
            window = as_int(info.get("model_context_window"), minimum=1)
            if window:
                self.context_window = window
            last = normalize_usage(info.get("last_token_usage"))
            if last is not None and ts is not None:
                self.latest_tc_usage = {"usage": last, "at": ts}
        rl = sanitize_rate_limits(p.get("rate_limits"), ts)
        if rl is not None and ts is not None:
            if self.rate_limits_at_ms is None or ts >= self.rate_limits_at_ms:
                self.rate_limits = rl
                self.rate_limits_at_ms = ts
                used = {str(w.get("windowMinutes")): w.get("usedPercent") for w in rl.get("windows", []) if w.get("usedPercent") is not None}
                limit = rl.get("limitId") or rl.get("limitName")
                last = self.rate_history[-1] if self.rate_history else None
                if used and (last is None or last["w"] != used or last.get("id") != limit):
                    self.rate_history.append({"t": ts, "w": used, "id": limit})


# --------------------------------------------------------------------------
# Incremental file reader
# --------------------------------------------------------------------------

def _should_parse(raw: bytes) -> bool:
    """Cheap byte-level pre-filter. Returns False only for lines that are
    certainly irrelevant; anything ambiguous is parsed."""
    head = raw[:256]
    m = _TOP_TYPE_RE.search(head)
    if not m:
        return True
    top = m.group(1)
    if top in SKIP_TOP_LEVEL:
        return False
    if top == b"event_msg" and b'"item_completed"' in head:
        im = _ITEM_TYPE_RE.search(raw[:2048])
        if im is not None and im.group(1).decode("ascii", "replace") not in TIMING_ITEM_TYPES:
            return False
    return True


class _Tail:
    """Read position in one file of a session's chain."""

    __slots__ = ("path", "offset", "identity", "head_sig", "partial", "skipping_oversized", "size", "mtime_ms",
                 "sealed", "late_bytes")

    def __init__(self, path: str) -> None:
        self.path = path
        self.offset = 0
        self.identity: Optional[Tuple[int, int]] = None
        self.head_sig: Optional[bytes] = None
        self.partial = bytearray()
        self.skipping_oversized = False
        self.size = 0
        self.mtime_ms: Optional[int] = None
        # a file a later one continues: read to its end, then left (what it gains after is counted, not read)
        self.sealed = False
        self.late_bytes = 0


class SessionLog:
    """Incrementally tails one session: a rollout file, or the chain of files one thread was
    written to (oldest first), into one SessionState."""

    def __init__(self, paths: Any) -> None:
        self.paths: List[str] = [paths] if isinstance(paths, str) else list(paths)
        # the chain once read lost a file (one dropped or no longer checking out): what was
        # counted from it is gone from the totals, said so for as long as this reader lives
        self.dropped = False
        self._reset()

    @property
    def path(self) -> str:
        return self.paths[-1]

    def _reset(self) -> None:
        self.tails: List[_Tail] = [_Tail(p) for p in self.paths]
        self.state = SessionState()
        self.resets = getattr(self, "resets", -1) + 1
        self.caught_up = False
        self.error: Optional[str] = None

    def set_paths(self, paths: List[str]) -> None:
        """The chain as discovery now sees it. A file added after the last is read on from where
        the chain stands; any other change (a file gone, another order) reads it all again."""
        paths = list(paths)
        if paths == self.paths:
            return
        if paths[: len(self.paths)] == self.paths:
            self.paths = paths
            self.tails += [_Tail(p) for p in paths[len(self.tails):]]
            self.caught_up = False
            return
        self.paths = paths
        self.dropped = True
        self._reset()

    # totals over the chain, as the single file's were
    @property
    def offset(self) -> int:
        return sum(t.offset for t in self.tails)

    @property
    def size(self) -> int:
        return sum(t.size for t in self.tails)

    @property
    def mtime_ms(self) -> Optional[int]:
        return self.tails[-1].mtime_ms

    @property
    def late_bytes(self) -> int:
        return sum(t.late_bytes for t in self.tails)

    def refresh(self, budget: int = DEFAULT_READ_BUDGET) -> None:
        for attempt in (0, 1):
            rotated = self._refresh(budget)
            if not rotated:
                return
            self._reset()  # a file of the chain was replaced: read the chain again, once

    def _refresh(self, budget: int) -> bool:
        self.error = None
        last = len(self.tails) - 1
        for i, t in enumerate(self.tails):
            try:
                st = os.stat(t.path)
            except OSError as exc:
                self.error = f"stat_failed:{exc.__class__.__name__}"
                self.caught_up = False
                return False
            identity = (st.st_dev, st.st_ino) if st.st_ino else None
            if t.identity is not None and identity is not None and identity != t.identity:
                return True
            if st.st_size < t.offset:
                return True
            if t.head_sig is not None:
                sig = self._read_head(t.path)
                if sig is not None and sig[: len(t.head_sig)] != t.head_sig:
                    return True
            t.identity = identity
            t.size = st.st_size
            t.mtime_ms = int(st.st_mtime * 1000)
            if t.sealed:
                t.late_bytes = st.st_size - t.offset
                continue
            if t.offset < st.st_size:
                try:
                    with open(t.path, "rb") as fh:
                        fh.seek(t.offset)
                        remaining = min(budget, st.st_size - t.offset)
                        while remaining > 0:
                            chunk = fh.read(min(READ_CHUNK_BYTES, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            budget -= len(chunk)
                            t.offset += len(chunk)
                            self._feed(t, chunk)
                except OSError as exc:
                    self.error = f"read_failed:{exc.__class__.__name__}"
                    self.caught_up = False
                    return False
                if t.head_sig is None:
                    t.head_sig = self._read_head(t.path)
            if t.offset < st.st_size:
                # the budget ran out: the files after this one wait for the next refresh
                self.caught_up = False
                return False
            if i < last:
                # a later file continues this one: what came before the hand-over is all read
                if t.partial:
                    t.partial.clear()
                t.sealed = True
        self.caught_up = True
        return False

    @staticmethod
    def _read_head(path: str) -> Optional[bytes]:
        try:
            with open(path, "rb") as fh:
                return fh.read(HEAD_SIGNATURE_BYTES)
        except OSError:
            return None

    def _feed(self, t: _Tail, chunk: bytes) -> None:
        start = 0
        while True:
            nl = chunk.find(b"\n", start)
            if nl < 0:
                rest = chunk[start:]
                if t.skipping_oversized:
                    return
                if len(t.partial) + len(rest) > MAX_LINE_BYTES:
                    t.partial.clear()
                    t.skipping_oversized = True
                    self.state.counters["oversizedLines"] += 1
                    return
                t.partial += rest
                return
            piece = chunk[start:nl]
            start = nl + 1
            if t.skipping_oversized:
                t.skipping_oversized = False
                continue
            if t.partial:
                if len(t.partial) + len(piece) > MAX_LINE_BYTES:
                    t.partial.clear()
                    self.state.counters["oversizedLines"] += 1
                    continue
                t.partial += piece
                line = bytes(t.partial)
                t.partial.clear()
            else:
                if len(piece) > MAX_LINE_BYTES:
                    self.state.counters["oversizedLines"] += 1
                    continue
                line = piece
            self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        if not line.strip():
            return
        counters = self.state.counters
        counters["lines"] += 1
        if not _should_parse(line):
            return
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            counters["badLines"] += 1
            return
        if not isinstance(obj, dict):
            counters["badLines"] += 1
            return
        counters["parsedLines"] += 1
        try:
            self.state.handle(obj)
        except Exception:  # defensive: one weird line must not kill the session
            counters["badLines"] += 1

    @property
    def pending_tail_bytes(self) -> int:
        return sum(len(t.partial) for t in self.tails)


# --------------------------------------------------------------------------
# Lightweight head / tail probes for discovery labels and rate limits
# --------------------------------------------------------------------------

def read_first_line(path: str, cap: int = 2 * 1024 * 1024) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            buf = bytearray()
            while len(buf) < cap:
                chunk = fh.read(65536)
                if not chunk:
                    break
                nl = chunk.find(b"\n")
                if nl >= 0:
                    buf += chunk[:nl]
                    return bytes(buf)
                buf += chunk
    except OSError:
        return None
    return None


def session_kind(source: Any, thread_source: Any) -> str:
    ts = thread_source if isinstance(thread_source, str) else None
    if ts == "guardian_review":
        return "review"
    if ts == "subagent" or isinstance(source, dict):
        return "subagent"
    if ts == "agent_created_thread":
        return "agent"
    return "user"


def probe_head(path: str, scan_bytes: int = 1 << 20) -> Dict[str, Any]:
    """Return minimal session_meta fields from the first line, plus the first
    turn_context model/effort found within the first ``scan_bytes``."""
    line = read_first_line(path)
    out: Dict[str, Any] = {}
    if not line:
        return out
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return out
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return out
    p = obj.get("payload")
    if not isinstance(p, dict):
        return out
    out["id"] = (as_str(p.get("id"), 64) or "").lower() or None
    out["sessionId"] = (as_str(p.get("session_id"), 64) or "").lower() or None
    # the line's place in the thread's history (0 on a history's first file), and, on a file that
    # continues another, where the history it carries on from ends: in the file named by
    # thread_id (the thread's own id names its first file, a segment id that segment's file), the
    # bytes before end_byte_offset, whose last line has the ordinal before end_ordinal_exclusive
    out["ordinal"] = as_int(obj.get("ordinal"))
    hb = p.get("history_base")
    if isinstance(hb, dict):
        base_id = (as_str(hb.get("thread_id"), 64) or "").lower() or None
        end_ord = as_int(hb.get("end_ordinal_exclusive"), minimum=1)
        end_off = as_int(hb.get("end_byte_offset"), minimum=1)
        out["historyBase"] = {"fileId": base_id, "endOrdinal": end_ord, "endOffset": end_off} \
            if base_id and end_ord and end_off else {"invalid": True}
    out["cwd"] = as_str(p.get("cwd"))
    out["startedAt"] = parse_ts_ms(p.get("timestamp")) or parse_ts_ms(obj.get("timestamp"))
    out["cliVersion"] = as_str(p.get("cli_version"), 64)
    out["accountKey"] = as_str(p.get("creator_account_id"), 128)
    out["kind"] = session_kind(p.get("source"), p.get("thread_source"))
    # A subagent or an automatic review names the thread that spawned it; a spawned agent also
    # its nickname, its task path (/root/ui_polish) and how deep it sits under the user's chat.
    src = p.get("source")
    sub = src.get("subagent") if isinstance(src, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    if not isinstance(spawn, dict):
        spawn = {}
    parent = p.get("parent_thread_id") or spawn.get("parent_thread_id")
    out["parentId"] = (as_str(parent, 64) or "").lower() or None
    out["agentName"] = as_str(p.get("agent_nickname") or spawn.get("agent_nickname"), 64)
    out["agentPath"] = as_str(p.get("agent_path") or spawn.get("agent_path"), 256)
    depth = spawn.get("depth")
    out["depth"] = depth if isinstance(depth, int) and 0 < depth < 64 else None
    try:
        with open(path, "rb") as fh:
            fh.seek(len(line) + 1)
            data = fh.read(scan_bytes)
    except OSError:
        return out
    for raw in data.split(b"\n")[:-1]:
        if b'"turn_context"' not in raw[:256]:
            continue
        try:
            tc = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        tp = tc.get("payload") if isinstance(tc, dict) else None
        if isinstance(tp, dict):
            out["model"] = as_str(tp.get("model"), 128)
            out["effort"] = as_str(tp.get("effort"), 32)
            break
    return out


def ordinal_ending_at(path: str, offset: int, scan_cap: int = MAX_LINE_BYTES) -> Optional[int]:
    """The ``ordinal`` of the line that ends exactly at byte ``offset`` of ``path`` (its newline
    the byte before), or None: not at a line's end, past the file, no ordinal, or a line longer
    than ``scan_cap``. Reads back from the offset to the line's start, then its first bytes."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if offset <= 0 or offset > fh.tell():
                return None
            fh.seek(offset - 1)
            if fh.read(1) != b"\n":
                return None
            end = offset - 1  # the newline
            pos, start = end, 0
            while pos > 0 and end - pos < scan_cap:
                step = min(65536, pos)
                fh.seek(pos - step)
                buf = fh.read(step)
                nl = buf.rfind(b"\n")
                if nl >= 0:
                    start = pos - step + nl + 1
                    break
                pos -= step
            else:
                if pos > 0:
                    return None
            fh.seek(start)
            head = fh.read(min(512, end - start))
    except OSError:
        return None
    m = _ORDINAL_RE.search(head)
    return int(m.group(1)) if m else None


def probe_tail(path: str, size: int, window: int = 384 * 1024) -> Dict[str, Any]:
    """Scan the last ``window`` bytes for latest settings and rate limits."""
    out: Dict[str, Any] = {}
    try:
        with open(path, "rb") as fh:
            start = max(0, size - window)
            fh.seek(start)
            data = fh.read(window)
    except OSError:
        return out
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]  # first piece is probably a partial line
    if data and not data.endswith(b"\n") and lines:
        lines = lines[:-1]  # in-progress line
    for raw in reversed(lines):
        if not raw.strip():
            continue
        head = raw[:256]
        wants_settings = "model" not in out and (
            b'"turn_context"' in head or b'"thread_settings_applied"' in head
        )
        wants_rl = "rateLimits" not in out and b'"token_count"' in head
        wants_ts = "lastEventAt" not in out
        if not (wants_settings or wants_rl or wants_ts):
            if "model" in out and "rateLimits" in out:
                break
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        ts = parse_ts_ms(obj.get("timestamp"))
        if wants_ts and ts is not None:
            out["lastEventAt"] = ts
        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if wants_settings:
            if obj.get("type") == "turn_context":
                out["model"] = as_str(p.get("model"), 128)
                out["effort"] = as_str(p.get("effort"), 32)
            elif p.get("type") == "thread_settings_applied" and isinstance(p.get("thread_settings"), dict):
                s = p["thread_settings"]
                out["model"] = as_str(s.get("model"), 128)
                out["effort"] = as_str(s.get("reasoning_effort"), 32)
        if wants_rl and p.get("type") == "token_count":
            rl = sanitize_rate_limits(p.get("rate_limits"), ts)
            if rl is not None and ts is not None:
                out["rateLimits"] = rl
                out["rateLimitsAt"] = ts
    return out
