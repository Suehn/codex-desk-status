"""Session discovery: which roots to scan, and an ID -> file index.

Clients never pass paths. They pass a session ID, which must match a strict
UUID pattern *and* be present in this index; the index only contains files
found under the configured roots (symlinks are not followed while walking).

A thread's log may run on in further files, ``rollout-<time>-<thread>_<segment>.jsonl``,
each one's session_meta naming the thread (``id`` and ``session_id``). One that carries on a
history records where: ``history_base`` names the file (the thread id its first file, a segment
id that segment's file) and the byte and ordinal its history ends at there; one that starts a
history afresh has none and begins at ordinal 0. A segment joins its thread's chain only when
all of that holds against the files before it, in the same root; the thread is listed and bound
once, by its id, and read across the chain. A segment that does not hold stays apart, under its
own id and unverified, as a renamed or copied log does, and the thread's snapshot says so (its
record may be incomplete). Each file joins the one just before it: a carried-on history names
that file; a history begun afresh begins after it did. Two files that would take the same place
(a copy of a segment) join neither: the thread stops there, said to be ambiguous.
"""

from __future__ import annotations

import glob
import os
import re
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .telemetry import ordinal_ending_at, probe_head, probe_tail

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_U = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# rollout-<time>-<thread>.jsonl, or rollout-<time>-<thread>_<segment>.jsonl
_ROLLOUT_RE = re.compile(r"^rollout-.*?(" + _U + r")(?:_(" + _U + r"))?\.jsonl$")
MAX_WALK_DEPTH = 5
MAX_FILES = 20000
INDEX_TTL_S = 12.0
FORCED_RESCAN_MIN_INTERVAL_S = 2.0
ROOTS_ENV = "DESK_STATUS_SESSION_ROOTS"
HEAD_SCAN_BYTES = 1 << 20
# a segment whose first line is not yet whole, this young, is waited for (not listed, not judged)
PENDING_HEAD_MS = 30_000
DEFAULT_KINDS: Tuple[str, ...] = ("user", "agent")


def normalize_session_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if UUID_RE.match(v) else None


def short_id(sid: Optional[str]) -> Optional[str]:
    if not sid:
        return None
    return sid[-8:]


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def default_roots(env: Optional[Dict[str, str]] = None, home: Optional[str] = None) -> List[str]:
    """Portable defaults: $CODEX_HOME/sessions, ~/.codex/sessions and, on WSL,
    Windows-side homes under /mnt/c/Users/*/.codex/sessions."""
    env = dict(os.environ if env is None else env)
    home = home or os.path.expanduser("~")
    roots: List[str] = []
    codex_home = env.get("CODEX_HOME")
    if codex_home:
        roots.append(os.path.join(codex_home, "sessions"))
    roots.append(os.path.join(home, ".codex", "sessions"))
    if os.name == "nt":
        profile = env.get("USERPROFILE")
        if profile:
            roots.append(os.path.join(profile, ".codex", "sessions"))
    elif is_wsl() and env.get("DESK_STATUS_WSL_WINDOWS_HOMES", "1") != "0":
        for cand in sorted(glob.glob("/mnt/c/Users/*/.codex/sessions")):
            base = os.path.basename(os.path.dirname(os.path.dirname(cand))).lower()
            if base in ("default", "public", "all users", "default user"):
                continue
            roots.append(cand)
    return roots


def resolve_roots(cli_roots: Iterable[str] = (), env: Optional[Dict[str, str]] = None) -> Tuple[List[str], str]:
    """Explicit roots (CLI, then env) replace defaults entirely."""
    env = dict(os.environ if env is None else env)
    explicit = [r for r in cli_roots if r]
    source = "cli"
    if not explicit and env.get(ROOTS_ENV):
        explicit = [r for r in env[ROOTS_ENV].split(os.pathsep) if r.strip()]
        source = "env"
    if explicit:
        roots = explicit
    else:
        roots = default_roots(env)
        source = "default"
    seen = set()
    out = []
    for r in roots:
        try:
            key = os.path.realpath(os.path.expanduser(r))
        except (OSError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out, source


class SessionFile:
    """A session as listed and bound: its id, and the files it is read from, oldest first
    (``segments``; one, unless the thread ran on into further files). ``path``, ``size`` and
    ``mtime_ms`` are the newest file's, the one being written."""

    __slots__ = ("id", "path", "root", "size", "mtime_ms", "alternates", "segments", "unjoined", "forked", "first_ino")

    def __init__(self, sid: str, path: str, root: str, size: int, mtime_ms: int, ino: Optional[int] = None):
        self.id = sid
        self.path = path
        self.root = root
        self.size = size
        self.mtime_ms = mtime_ms
        self.alternates: List[str] = []
        self.segments: List[str] = [path]
        self.first_ino = ino  # the first file's inode: a file put in its place is read anew
        # files named as this thread's that did not join it (its record may be incomplete), and
        # whether two of them would have taken the same place
        self.unjoined = 0
        self.forked = False

    @property
    def first(self) -> str:
        return self.segments[0]


class SessionIndex:
    def __init__(self, roots: List[str]):
        self.roots = roots
        self._lock = threading.Lock()
        self._by_id: Dict[str, SessionFile] = {}
        self._scanned_at = 0.0
        self._root_counts: Dict[str, int] = {}
        self._label_cache: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
        self._head_cache: Dict[str, Tuple[Optional[int], int, Dict[str, Any]]] = {}
        # (segment, base file, its identity) -> whether the base's bytes end where the segment says
        self._link_cache: Dict[Tuple[Any, ...], bool] = {}
        self.truncated = False

    # -- scanning ---------------------------------------------------------
    def _walk(self, root: str) -> Iterable[Tuple[str, os.stat_result]]:
        if not os.path.isdir(root):
            return
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if depth < MAX_WALK_DEPTH:
                                    stack.append((e.path, depth + 1))
                            elif e.is_file(follow_symlinks=False) and e.name.endswith(".jsonl"):
                                yield e.path, e.stat(follow_symlinks=False)
                        except OSError:
                            continue
            except OSError:
                continue

    def scan(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            age = now - self._scanned_at
            if not force and self._scanned_at and age < INDEX_TTL_S:
                return
            if force and self._scanned_at and age < FORCED_RESCAN_MIN_INTERVAL_S:
                return
            by_id: Dict[str, SessionFile] = {}
            counts: Dict[str, int] = {}
            segments: Dict[Tuple[str, str], List[SessionFile]] = {}
            total = 0
            truncated = False
            for root in self.roots:
                n = 0
                for path, st in self._walk(root):
                    m = _ROLLOUT_RE.match(os.path.basename(path))
                    if not m:
                        continue
                    total += 1
                    if total > MAX_FILES:
                        truncated = True
                        break
                    sid = m.group(1).lower()
                    sf = SessionFile(sid, path, root, st.st_size, int(st.st_mtime * 1000), st.st_ino or None)
                    if m.group(2):
                        # a further file of thread sid: joined to it below, or else kept apart
                        segments.setdefault((sid, root), []).append(sf)
                        continue
                    n += 1
                    prev = by_id.get(sid)
                    if prev is None:
                        by_id[sid] = sf
                    elif (sf.mtime_ms, sf.size) > (prev.mtime_ms, prev.size):
                        sf.alternates = prev.alternates + [prev.path]
                        by_id[sid] = sf
                    else:
                        prev.alternates.append(sf.path)
                counts[root] = n
            now_ms = int(time.time() * 1000)
            aside_of: Dict[str, List[SessionFile]] = {}
            for (sid, root), segs in segments.items():
                # a segment just created, its first line not yet whole: judged next time
                segs = [sf for sf in segs if self.head(sf).get("id") or now_ms - sf.mtime_ms >= PENDING_HEAD_MS]
                base = by_id.get(sid)
                rest = segs
                if base is not None and base.root == root and self.head(base).get("id") == sid:
                    rest, forked = self._chain(base, segs)
                    base.forked = base.forked or forked
                aside_of.setdefault(sid, []).extend(rest)
            for sid, rest in aside_of.items():
                if rest and sid in by_id:
                    by_id[sid].unjoined += len(rest)
                for sf in rest:
                    # not shown to continue its thread here: apart, under its own id, unverified
                    seg_id = _ROLLOUT_RE.match(os.path.basename(sf.path)).group(2).lower()
                    if seg_id not in by_id:
                        sf.id = seg_id
                        by_id[seg_id] = sf
                        counts[root] = counts.get(root, 0) + 1
            self._by_id = by_id
            self._root_counts = counts
            self._scanned_at = time.monotonic()
            self.truncated = truncated
            live = {(sf.path, sf.size, sf.mtime_ms) for sf in by_id.values()}
            if len(self._link_cache) > 4096:
                self._link_cache.clear()
            for key in list(self._label_cache):
                if key not in live:
                    del self._label_cache[key]

    def _chain(self, base: SessionFile, segs: List[SessionFile]) -> Tuple[List[SessionFile], bool]:
        """Join to ``base`` the segments that carry its thread on, each after the file just before
        it; return the ones that do not, and whether two would have taken the same place.

        A segment's session_meta names the thread (id, and session_id where present). It joins
        the chain's last file when it either carries that file's history on (history_base names
        that very file, the segment's first ordinal is the base's end, and the base's line ending
        at the given byte is the one before it) or begins a history afresh (no history_base,
        ordinal 0, begun strictly after the last file began). Of the segments that fit, the first
        to begin joins and the chain goes on from it. Two that fit at once, by beginning together
        or both naming the last file as their base, join neither: the chain stops there."""
        sid = base.id
        left = [(self.head(sf), sf) for sf in segs]
        left.sort(key=lambda hs: (hs[0].get("startedAt") or 0, os.path.basename(hs[1].path)))
        last, last_id = base, sid
        last_start = self.head(base).get("startedAt") or 0
        forked = False
        while left:
            fits = [hs for hs in left if self._continues(sid, last, last_id, last_start, hs[0], hs[1])]
            if not fits:
                break
            first = fits[0][0].get("startedAt") or 0
            together = [hs for hs in fits if (hs[0].get("startedAt") or 0) == first]
            carried = [hs for hs in fits if hs[0].get("historyBase") is not None]
            if len(together) > 1 or len(carried) > 1:
                forked = True
                break
            head, sf = fits[0]
            left.remove(fits[0])
            last, last_id, last_start = sf, _ROLLOUT_RE.match(os.path.basename(sf.path)).group(2).lower(), first
            base.segments.append(sf.path)
            base.path, base.size = sf.path, sf.size
            base.mtime_ms = max(base.mtime_ms, sf.mtime_ms)
        return [sf for _, sf in left], forked

    def _continues(self, sid: str, last: SessionFile, last_id: str, last_start: int,
                   head: Dict[str, Any], seg: SessionFile) -> bool:
        if head.get("id") != sid or head.get("sessionId") not in (None, sid):
            return False
        start = head.get("startedAt") or 0
        hb = head.get("historyBase")
        if hb is None:
            return head.get("ordinal") == 0 and start > last_start
        return hb.get("fileId") == last_id and start >= last_start and head.get("ordinal") == hb.get("endOrdinal") \
            and self._base_ends(seg, last.path, hb["endOffset"], hb["endOrdinal"])

    def _base_ends(self, seg: SessionFile, base_path: str, offset: int, end_ordinal: int) -> bool:
        """The base file's line ending at ``offset`` is the one before ``end_ordinal``. The bytes
        before a hand-over never change, so the answer is kept while both files are the same."""
        try:
            st = os.stat(base_path)
        except OSError:
            return False
        key = (seg.path, seg.first_ino, base_path, (st.st_dev, st.st_ino), offset, end_ordinal)
        ok = self._link_cache.get(key)
        if ok is None:
            ok = ordinal_ending_at(base_path, offset) == end_ordinal - 1
            self._link_cache[key] = ok
        return ok

    def get(self, sid: str) -> Optional[SessionFile]:
        self.scan()
        sf = self._by_id.get(sid)
        if sf is None:
            self.scan(force=True)
            sf = self._by_id.get(sid)
        return sf

    def contains(self, sid: str) -> bool:
        return self.get(sid) is not None

    def verified(self, sf: SessionFile) -> bool:
        """True when the first line's session_meta.id equals the filename id (of the session's
        first file; the files after it were checked as they joined). A missing/unreadable
        session_meta does not verify."""
        return self.head(sf).get("id") == sf.id

    def recent(self, limit: int = 20, kinds: Optional[Tuple[str, ...]] = DEFAULT_KINDS) -> List[SessionFile]:
        """Most recently modified sessions; optionally filtered by kind
        (user / agent / subagent / review). Kind comes from the cached head."""
        self.scan()
        files = sorted(self._by_id.values(), key=lambda f: (f.mtime_ms, f.id), reverse=True)
        if kinds is None:
            return files[:limit]
        out = []
        for sf in files:
            if self.head(sf).get("kind", "user") in kinds:
                out.append(sf)
                if len(out) >= limit:
                    break
        return out

    def count_hidden(self, kinds: Tuple[str, ...] = DEFAULT_KINDS) -> int:
        return sum(1 for sf in self._by_id.values() if self.head(sf).get("kind", "user") not in kinds)

    def root_summary(self) -> List[Dict[str, Any]]:
        self.scan()
        return [
            {
                "path": r,
                "label": root_label(r),
                "exists": os.path.isdir(r),
                "sessions": self._root_counts.get(r, 0),
            }
            for r in self.roots
        ]

    # -- labels -------------------------------------------------------------
    def head(self, sf: SessionFile) -> Dict[str, Any]:
        """session_meta fields of the session's first file; the first line of a
        rollout never changes, so this is cached per path (re-probed only while
        a young file grows without a turn_context yet)."""
        path = sf.segments[0]
        size = sf.size if len(sf.segments) == 1 else None
        cached = self._head_cache.get(path)
        if cached is not None and (sf.first_ino is None or cached[0] in (None, sf.first_ino)):
            _, probed_size, info = cached
            if info.get("model") or probed_size >= HEAD_SCAN_BYTES or size is None or size <= probed_size:
                return info
        info = probe_head(path, HEAD_SCAN_BYTES)
        if size is None:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        if len(self._head_cache) > 8192:
            self._head_cache.clear()
        self._head_cache[path] = (sf.first_ino, size, info)
        return info

    def children(self, parent_id: str, since_ms: Optional[int] = None) -> List[SessionFile]:
        """Sessions whose session_meta names ``parent_id`` as the thread that spawned them
        (subagents, automatic reviews). Only files written since the parent began are probed,
        and heads are cached, so a repeat call reads nothing new."""
        self.scan()
        out = []
        for sf in self._by_id.values():
            if since_ms is not None and sf.mtime_ms < since_ms:
                continue
            if self.head(sf).get("parentId") == parent_id:
                out.append(sf)
        return out

    def label(self, sf: SessionFile) -> Dict[str, Any]:
        """Head + tail probe; the tail part is cached by (path, size, mtime)."""
        try:
            st = os.stat(sf.path)
            size, mtime_ms = st.st_size, int(st.st_mtime * 1000)
        except OSError:
            size, mtime_ms = sf.size, sf.mtime_ms
        key = (sf.path, size, mtime_ms)
        cached = self._label_cache.get(key)
        if cached is not None:
            return cached
        head = self.head(sf)
        tail = probe_tail(sf.path, size)
        info = {
            "cwd": head.get("cwd"),
            "startedAt": head.get("startedAt"),
            "accountKey": head.get("accountKey"),
            "kind": head.get("kind", "user"),
            "model": tail.get("model") or head.get("model"),
            "effort": tail.get("effort") or head.get("effort"),
            "lastEventAt": tail.get("lastEventAt"),
            "rateLimits": tail.get("rateLimits"),
            "rateLimitsAt": tail.get("rateLimitsAt"),
            "size": size,
            "mtime": mtime_ms,
        }
        if len(self._label_cache) > 512:
            self._label_cache.clear()
        self._label_cache[key] = info
        return info


def root_label(root: str) -> str:
    norm = root.replace("\\", "/")
    if norm.startswith("/mnt/") and len(norm) > 6 and norm[6:7] in ("/", ""):
        return "Windows"
    if re.match(r"^[A-Za-z]:/", norm):
        return "Windows"
    return "WSL" if is_wsl() else ("Windows" if os.name == "nt" else sys.platform)


def project_name(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    parts = [p for p in re.split(r"[\\/]+", cwd) if p]
    return parts[-1][:80] if parts else cwd[:80]
