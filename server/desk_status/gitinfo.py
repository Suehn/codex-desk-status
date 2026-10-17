"""Read-only git status probe.

Safety:
* argument list only (``shell=False``), the cwd is passed after ``-C`` as a
  separate argv element and must be an existing absolute directory;
* ``--no-optional-locks`` + ``GIT_OPTIONAL_LOCKS=0`` so ``git status`` never
  rewrites the index; ``core.fsmonitor=false`` so a repository-local config
  cannot make status execute a program; no prompts, no pager;
* hard timeout, executed on a background thread with a short-lived cache so a
  slow repository (e.g. on a 9P /mnt/c mount) never blocks polling;
* ``safe.directory`` is never overridden: a "dubious ownership" refusal is
  reported as such.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from .discovery import is_wsl

GIT_TIMEOUT_S = 2.5
CACHE_TTL_S = 8.0
FIRST_WAIT_S = 1.2
MAX_OUTPUT_LINES = 200000

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_UNC_RE = re.compile(r"^[\\/]{2}wsl(?:\$|\.localhost)[\\/]([^\\/]+)[\\/]?(.*)$", re.I)


def map_cwd(cwd: Optional[str], enabled: Optional[bool] = None) -> Optional[str]:
    """Map Windows paths to WSL paths when running inside WSL (optional).

    ``C:\\Users\\me\\proj`` -> ``/mnt/c/Users/me/proj``
    ``\\\\wsl.localhost\\Ubuntu\\home\\me`` -> ``/home/me`` (same distro only)
    """
    if not cwd or not isinstance(cwd, str):
        return None
    if enabled is None:
        enabled = os.environ.get("DESK_STATUS_WSL_PATH_MAP", "1") != "0"
    if os.name == "nt" or not enabled:
        return cwd
    m = _WIN_DRIVE_RE.match(cwd)
    if m and is_wsl():
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        mount = f"/mnt/{drive}"
        if os.path.isdir(mount):
            return f"{mount}/{rest}".rstrip("/") or mount
        return cwd
    m = _WSL_UNC_RE.match(cwd)
    if m and is_wsl():
        distro = os.environ.get("WSL_DISTRO_NAME", "")
        if distro and m.group(1).lower() == distro.lower():
            return "/" + m.group(2).replace("\\", "/")
    return cwd


def parse_porcelain_v2(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "branch": None,
        "detached": False,
        "oid": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 0,
    }
    for i, line in enumerate(text.splitlines()):
        if i > MAX_OUTPUT_LINES:
            break
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
            if head == "(detached)":
                out["detached"] = True
            else:
                out["branch"] = head[:200]
        elif line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):].strip()
            out["oid"] = None if oid == "(initial)" else oid[:12]
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line[len("# branch.upstream "):].strip()[:200]
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                out["ahead"] = int(m.group(1))
                out["behind"] = int(m.group(2))
        elif line.startswith(("1 ", "2 ")):
            xy = line[2:4]
            if len(xy) == 2:
                if xy[0] != ".":
                    out["staged"] += 1
                if xy[1] != ".":
                    out["unstaged"] += 1
        elif line.startswith("u "):
            out["conflicted"] += 1
        elif line.startswith("? "):
            out["untracked"] += 1
    out["dirty"] = bool(out["staged"] or out["unstaged"] or out["untracked"] or out["conflicted"])
    return out


def classify_git_error(stderr: str) -> str:
    s = stderr.lower()
    if "dubious ownership" in s or "safe.directory" in s:
        return "dubious_ownership"
    if "not a git repository" in s:
        return "not_repo"
    return "error"


def run_git_status(path: str, git: Optional[str] = None, timeout: float = GIT_TIMEOUT_S) -> Dict[str, Any]:
    started = time.time()
    base = {"checkedAt": int(started * 1000), "path": path}
    git = git or shutil.which("git")
    if not git:
        return dict(base, state="unavailable", message="git 不可用")
    if not path or not os.path.isabs(path) or not os.path.isdir(path):
        return dict(base, state="no_cwd", message="工作目录不存在或不可访问")
    # Drop inherited GIT_* (GIT_DIR, GIT_INDEX_FILE, ...) so the probe always
    # inspects exactly the session's working directory.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    argv = [
        git,
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotepath=false",
        "-C",
        path,
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=normal",
    ]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return dict(base, state="timeout", message=f"git status 超过 {timeout:g} 秒")
    except OSError as exc:
        return dict(base, state="error", message=f"无法执行 git：{exc.__class__.__name__}")
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        state = classify_git_error(stderr)
        first = next((ln.strip() for ln in stderr.splitlines() if ln.strip()), "")
        return dict(base, state=state, message=first[:240])
    info = parse_porcelain_v2(proc.stdout.decode("utf-8", "replace"))
    info.update(base)
    info["state"] = "ok"
    info["elapsedMs"] = int((time.time() - started) * 1000)
    return info


class GitCache:
    """Per-path cache with background refresh; never blocks a poll for long."""

    def __init__(self, enabled: bool = True, runner=run_git_status):
        self.enabled = enabled
        self.runner = runner
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}

    def get(self, cwd: Optional[str]) -> Dict[str, Any]:
        if not self.enabled:
            return {"state": "disabled", "message": "已通过配置关闭 git 状态"}
        if not cwd:
            return {"state": "no_cwd", "message": "会话未记录工作目录"}
        path = map_cwd(cwd)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(path)
            if entry is None:
                entry = {"result": None, "at": 0.0, "event": None}
                self._entries[path] = entry
                if len(self._entries) > 64:
                    oldest = min(self._entries, key=lambda k: self._entries[k]["at"])
                    if oldest != path:
                        del self._entries[oldest]
            fresh = entry["result"] is not None and now - entry["at"] < CACHE_TTL_S
            if not fresh and entry["event"] is None:
                ev = threading.Event()
                entry["event"] = ev
                threading.Thread(target=self._work, args=(path, entry, ev), daemon=True).start()
            ev = entry["event"]
            result = entry["result"]
        if result is None and ev is not None:
            ev.wait(FIRST_WAIT_S)
            with self._lock:
                result = entry["result"]
        if result is None:
            return {"state": "pending", "message": "正在读取 git 状态", "path": path}
        out = dict(result)
        if path != cwd:
            out["mappedFrom"] = "windows"
        out["refreshing"] = entry["event"] is not None
        return out

    def _work(self, path: str, entry: Dict[str, Any], ev: threading.Event) -> None:
        try:
            result = self.runner(path)
        except Exception as exc:  # pragma: no cover - defensive
            result = {"state": "error", "message": exc.__class__.__name__, "path": path}
        with self._lock:
            entry["result"] = result
            entry["at"] = time.monotonic()
            entry["event"] = None
        ev.set()
