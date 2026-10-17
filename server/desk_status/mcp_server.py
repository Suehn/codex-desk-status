"""Minimal, dependency-free MCP server over stdio (newline-delimited JSON-RPC).

stdout carries protocol messages only; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import (
    SERVER_NAME,
    SNAPSHOT_META_KEY,
    SNAPSHOT_SCHEMA,
    UI_MIME_TYPE,
    UI_RESOURCE_URI,
    __version__,
)
from .discovery import SessionIndex, UUID_RE, resolve_roots
from .gitinfo import GitCache
from .status import StatusService

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_DIST = os.path.join(PLUGIN_ROOT, "ui", "dist", "panel.html")
ICON_PATH = os.path.join(PLUGIN_ROOT, "assets", "tool-icon.svg")

# JSON-RPC / MCP error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002

_LOG_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "off": 100}


class Log:
    level = _LOG_LEVELS["info"]

    @classmethod
    def emit(cls, level: str, msg: str) -> None:
        if _LOG_LEVELS[level] >= cls.level:
            try:
                sys.stderr.write(f"[desk-status] {level}: {msg}\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# --------------------------------------------------------------------------
# Schemas & tiny validator (covers the subset of JSON Schema we use)
# --------------------------------------------------------------------------

UUID_PATTERN = UUID_RE.pattern

OPEN_INPUT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
READ_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sessionId": {
            "type": "string",
            "pattern": UUID_PATTERN,
            "description": "用户在面板中手动选择的会话 ID（小写 UUID）。优先级最高。",
        },
        "hostSessionId": {
            "type": "string",
            "pattern": UUID_PATTERN,
            "description": "面板从宿主上下文得到的候选会话 ID；仅当与本地会话完全一致时才会绑定。",
        },
        "threadId": {
            "type": "string",
            "pattern": UUID_PATTERN,
            "description": "要查看的子代理或自动审查线程 ID；只接受当前会话派生的线程，结果放在 thread 字段。",
        },
        "includeSessions": {"type": "boolean", "description": "是否附带最近 20 个本地会话列表。"},
        "allKinds": {"type": "boolean", "description": "会话列表是否包含子代理与自动审查会话（默认仅用户会话）。"},
        "seq": {"type": "integer", "minimum": 0, "maximum": 9007199254740991, "description": "客户端请求序号，原样回显。"},
    },
    "additionalProperties": False,
}

OPEN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "panel": {"type": "string"},
        "version": {"type": "string"},
        "binding": {"type": "string", "enum": ["host", "manual", "none", "manual_missing"]},
    },
    "required": ["panel", "version", "binding"],
    "additionalProperties": False,
}

_NULLABLE_OBJ = {"type": ["object", "null"]}
READ_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "schema": {"type": "string", "const": SNAPSHOT_SCHEMA},
        "server": {
            "type": "object",
            "properties": {"version": {"type": "string"}, "time": {"type": "integer"}},
            "required": ["version", "time"],
        },
        "request": {"type": "object"},
        "binding": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["host", "manual", "none", "manual_missing"]},
                "sessionId": {"type": ["string", "null"]},
                "suggestedSessionId": {"type": ["string", "null"]},
                "host": {"type": "object"},
            },
            "required": ["state", "sessionId", "host"],
        },
        "session": _NULLABLE_OBJ,
        "rateLimits": _NULLABLE_OBJ,
        "pricing": {"type": "object"},
        "sessions": {"type": ["array", "null"], "items": {"type": "object"}},
        "sessionFilter": {"type": "object"},
        "roots": {"type": "array", "items": {"type": "object"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "schema", "server", "request", "binding", "session", "rateLimits", "sessions", "sessionFilter", "roots", "warnings",
    ],
}

_TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: Dict[str, Any], path: str = "arguments") -> List[str]:
    errors: List[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPE_CHECKS[x](value) for x in types):
            return [f"{path}: 需要 {'/'.join(types)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 取值不符")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 不在允许的取值内")
    if isinstance(value, str):
        if "pattern" in schema and not re.match(schema["pattern"], value):
            errors.append(f"{path}: 格式无效")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: 过长")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: 小于最小值")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: 大于最大值")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: 缺失")
        for k, v in value.items():
            if k in props:
                errors.extend(validate(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: 不支持的参数 {str(k)[:40]!r}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value[:1000]):
            errors.extend(validate(v, schema["items"], f"{path}[{i}]"))
    return errors


# --------------------------------------------------------------------------
# Descriptors
# --------------------------------------------------------------------------

def _icon_data_uri() -> Optional[str]:
    try:
        with open(ICON_PATH, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return "data:image/svg+xml;base64," + base64.b64encode(data).decode("ascii")


def tool_icons() -> List[Dict[str, Any]]:
    uri = _icon_data_uri()
    if not uri:
        return []
    return [{"src": uri, "mimeType": "image/svg+xml", "sizes": ["any"]}]


READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
    "idempotentHint": True,
}


def tool_descriptors() -> List[Dict[str, Any]]:
    icons = tool_icons()
    open_tool = {
        "name": "open_status",
        "title": "会话状态",
        "description": (
            "打开「会话状态」面板：只读显示本机 Codex 会话的上下文占用、额度快照、缓存命中、"
            "客户端观测输出速率与 git 状态。由用户从侧栏或对话旁的面板打开，模型无需调用。"
        ),
        "inputSchema": OPEN_INPUT_SCHEMA,
        "outputSchema": OPEN_OUTPUT_SCHEMA,
        "annotations": dict(READ_ONLY_ANNOTATIONS, title="会话状态"),
        "_meta": {
            "ui": {"resourceUri": UI_RESOURCE_URI, "visibility": ["app"]},
            "openai/outputTemplate": UI_RESOURCE_URI,
            "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
            "openai/toolInvocation/invoking": "正在打开会话状态…",
            "openai/toolInvocation/invoked": "会话状态已打开",
        },
    }
    read_tool = {
        "name": "read_status",
        "title": "读取会话状态",
        "description": (
            "仅供「会话状态」面板轮询使用（app-only）：返回所选本地 Codex 会话的只读状态快照。"
            "不会修改任何文件，不访问网络。"
        ),
        "inputSchema": READ_INPUT_SCHEMA,
        "outputSchema": READ_OUTPUT_SCHEMA,
        "annotations": dict(READ_ONLY_ANNOTATIONS, title="读取会话状态"),
        "_meta": {
            "ui": {"visibility": ["app"]},
            "openai/widgetAccessible": True,
            "openai/visibility": "private",
        },
    }
    if icons:
        open_tool["icons"] = icons
        read_tool["icons"] = icons
    return [open_tool, read_tool]


RESOURCE_META = {
    "ui": {
        "csp": {"connectDomains": [], "resourceDomains": []},
        "prefersBorder": False,
    },
    "openai/widgetDescription": "会话状态：本机 Codex 会话的只读状态面板（上下文、额度、缓存、输出速率、git）。",
    "openai/widgetPrefersBorder": False,
    "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
    "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"]},
}


class UiResource:
    def __init__(self, path: str = UI_DIST):
        self.path = path
        self._cache: Optional[Tuple[float, str]] = None

    def html(self) -> str:
        st = os.stat(self.path)
        if self._cache and self._cache[0] == st.st_mtime:
            return self._cache[1]
        with open(self.path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self._cache = (st.st_mtime, text)
        return text


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class McpServer:
    def __init__(self, service: StatusService, ui: Optional[UiResource] = None):
        self.service = service
        self.ui = ui or UiResource()
        self.initialized = False
        self.client_info: Dict[str, Any] = {}
        self.client_supports_ui: Optional[bool] = None
        self._write_lock = threading.Lock()
        self._out = sys.stdout.buffer

    # ---- transport ---------------------------------------------------------
    def send(self, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        with self._write_lock:
            self._out.write(data + b"\n")
            self._out.flush()

    def serve(self, stream=None) -> None:
        stream = stream or sys.stdin.buffer
        Log.emit("info", f"v{__version__} ready; roots={len(self.service.index.roots)}")
        while True:
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_MESSAGE_BYTES and not line.endswith(b"\n"):
                # Drain the oversized message.
                while True:
                    more = stream.readline(MAX_MESSAGE_BYTES)
                    if not more or more.endswith(b"\n"):
                        break
                self.send(_error(None, INVALID_REQUEST, "Message too large"))
                continue
            if not line.strip():
                continue
            for reply in self.handle_raw(line):
                try:
                    self.send(reply)
                except ValueError:  # non-JSON-serialisable value slipped through
                    Log.emit("error", "result not serialisable")
                    self.send(_error(reply.get("id"), INTERNAL_ERROR, "Internal error"))
        Log.emit("info", "stdin closed; exiting")

    # ---- dispatch ----------------------------------------------------------
    def handle_raw(self, line: bytes) -> List[Dict[str, Any]]:
        try:
            msg = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return [_error(None, PARSE_ERROR, "Parse error")]
        if isinstance(msg, list):
            return [_error(None, INVALID_REQUEST, "Batch requests are not supported")]
        reply = self.handle(msg)
        return [reply] if reply is not None else []

    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(_safe_id(msg), INVALID_REQUEST, "Invalid Request")
        method = msg.get("method")
        has_id = "id" in msg
        msg_id = msg.get("id")
        if method is None:
            # A response to something we never sent; ignore.
            return None
        if not isinstance(method, str):
            return _error(_safe_id(msg), INVALID_REQUEST, "Invalid Request")
        if has_id and not (isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool)):
            return _error(None, INVALID_REQUEST, "Invalid id")
        params = msg.get("params", {})
        if params is None:
            params = {}
        if not has_id:
            self._notification(method, params)
            return None
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        try:
            result = self._request(method, params)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except RpcError as exc:
            return _error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive
            Log.emit("error", f"{method} failed: {exc.__class__.__name__}: {exc}")
            return _error(msg_id, INTERNAL_ERROR, "Internal error")

    def _notification(self, method: str, params: Any) -> None:
        if method == "notifications/initialized":
            self.initialized = True
        elif method == "notifications/cancelled":
            pass
        else:
            Log.emit("debug", f"ignored notification {method[:64]}")

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_descriptors()}
        if method == "tools/call":
            return self._tools_call(params)
        if method == "resources/list":
            return {
                "resources": [
                    {
                        "uri": UI_RESOURCE_URI,
                        "name": "desk-status-panel",
                        "title": "会话状态面板",
                        "description": "会话状态 MCP App 界面（自包含 HTML/CSS/JS，无外部资源）。",
                        "mimeType": UI_MIME_TYPE,
                        "_meta": RESOURCE_META,
                    }
                ]
            }
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            return self._resources_read(params)
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method[:64]}")

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        info = params.get("clientInfo")
        self.client_info = info if isinstance(info, dict) else {}
        caps = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
        ext = caps.get("extensions") if isinstance(caps.get("extensions"), dict) else {}
        self.client_supports_ui = "io.modelcontextprotocol/ui" in ext
        Log.emit(
            "info",
            f"initialize from {str(self.client_info.get('name'))[:40]!r} protocol={str(requested)[:20]} "
            f"-> {version}; mcp-apps ext advertised={self.client_supports_ui}",
        )
        server_info: Dict[str, Any] = {"name": SERVER_NAME, "title": "会话状态", "version": __version__}
        icons = tool_icons()
        if icons:
            server_info["icons"] = icons
        return {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": server_info,
            "instructions": (
                "会话状态（desk-status）是一个只读的本地 Codex 会话状态面板。工具仅供面板 UI 使用，"
                "模型不需要调用它们。"
            ),
        }

    def _resources_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise RpcError(INVALID_PARAMS, "uri must be a string")
        if uri != UI_RESOURCE_URI:
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri[:200]})
        try:
            html = self.ui.html()
        except OSError:
            Log.emit("error", "ui/dist/panel.html missing; run scripts/build_ui.py")
            raise RpcError(INTERNAL_ERROR, "UI bundle missing")
        return {
            "contents": [
                {"uri": UI_RESOURCE_URI, "mimeType": UI_MIME_TYPE, "text": html, "_meta": RESOURCE_META}
            ]
        }

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "Tool name must be a string")
        args = params.get("arguments", {})
        if args is None:
            args = {}
        meta = params.get("_meta")
        if name == "open_status":
            schema = OPEN_INPUT_SCHEMA
        elif name == "read_status":
            schema = READ_INPUT_SCHEMA
        else:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name[:64]}")
        errors = validate(args, schema)
        if errors:
            # Input validation failures are tool execution errors (MCP 2025-11-25).
            return {
                "content": [{"type": "text", "text": "参数无效：" + "；".join(errors[:5])}],
                "isError": True,
            }
        started = time.perf_counter()
        if name == "open_status":
            snap = self.service.snapshot(host_meta=meta, include_sessions=True, tool="open_status")
            result = {
                "content": [{"type": "text", "text": "会话状态面板已打开：只读显示本机 Codex 会话状态。"}],
                "structuredContent": {
                    "panel": SERVER_NAME,
                    "version": __version__,
                    "binding": snap["binding"]["state"],
                },
                "_meta": {SNAPSHOT_META_KEY: snap},
            }
        else:
            snap = self.service.snapshot(
                manual_id=args.get("sessionId"),
                host_meta=meta,
                host_hint=args.get("hostSessionId"),
                thread_id=args.get("threadId"),
                include_sessions=bool(args.get("includeSessions")),
                all_kinds=bool(args.get("allKinds")),
                seq=args.get("seq"),
            )
            result = {
                "content": [{"type": "text", "text": "desk-status snapshot"}],
                "structuredContent": snap,
            }
        elapsed = (time.perf_counter() - started) * 1000
        keys = ",".join(self.service.last_host_keys) or "-"
        Log.emit(
            "debug" if name == "read_status" else "info",
            f"{name} binding={snap['binding']['state']} host={snap['binding']['host']['status']} "
            f"metaKeys=[{keys[:300]}] {elapsed:.0f}ms",
        )
        return result


def _safe_id(msg: Any) -> Any:
    if isinstance(msg, dict):
        i = msg.get("id")
        if isinstance(i, (str, int)) and not isinstance(i, bool):
            return i
    return None


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_service(roots: List[str], git_enabled: bool = True) -> StatusService:
    return StatusService(SessionIndex(roots), GitCache(enabled=git_enabled))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="desk-status", description="会话状态（desk-status）：本地 Codex 会话状态 MCP 服务器（stdio）")
    ap.add_argument("--sessions-root", action="append", default=[], metavar="DIR",
                    help="会话日志根目录（可重复）；设置后替换默认目录。也可用环境变量 DESK_STATUS_SESSION_ROOTS")
    ap.add_argument("--no-git", action="store_true", help="不读取 git 状态")
    ap.add_argument("--log-level", choices=sorted(_LOG_LEVELS), default=os.environ.get("DESK_STATUS_LOG_LEVEL", "info"))
    ap.add_argument("--print-roots", action="store_true", help="打印解析后的会话根目录并退出（诊断用）")
    ap.add_argument("--snapshot", action="store_true", help="打印一次快照 JSON 并退出（诊断用，不走 MCP）")
    ap.add_argument("--session", help="与 --snapshot 搭配：指定会话 ID")
    ns = ap.parse_args(argv)
    Log.level = _LOG_LEVELS[ns.log_level]
    roots, source = resolve_roots(ns.sessions_root)
    git_enabled = not ns.no_git and os.environ.get("DESK_STATUS_GIT", "1") != "0"
    if ns.print_roots:
        print(json.dumps({"source": source, "roots": roots}, ensure_ascii=False, indent=2))
        return 0
    service = build_service(roots, git_enabled)
    if ns.snapshot:
        snap = service.snapshot(manual_id=ns.session, include_sessions=True)
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    Log.emit("info", f"session roots ({source}): {len(roots)}")
    McpServer(service).serve()
    return 0
