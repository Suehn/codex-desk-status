"""会话状态 (internal name: Clepsydra / desk-status) — local Codex session status MCP App (stdio server).

Standard library only. Reads local Codex rollout logs read-only and serves a
self-contained MCP Apps UI resource.
"""

__version__ = "0.2.5"

SERVER_NAME = "desk-status"
# The resource URI is the host's cache key for the bundle: a new one for each release whose
# data or UI meaning changes, so a host never keeps showing an older panel.
UI_RESOURCE_URI = "ui://desk-status/panel-0.2.5"
UI_MIME_TYPE = "text/html;profile=mcp-app"
SNAPSHOT_SCHEMA = "desk-status/2"
SNAPSHOT_META_KEY = "desk-status/snapshot"
