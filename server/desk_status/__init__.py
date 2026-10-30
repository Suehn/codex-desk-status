"""会话状态 (internal name: Clepsydra / desk-status) — local Codex session status MCP App (stdio server).

Standard library only. Reads local Codex rollout logs read-only and serves a
self-contained MCP Apps UI resource.
"""

__version__ = "0.2.6"

SERVER_NAME = "desk-status"
# The host persists this resource URI with saved panels. Keep the published identity
# across compatible plugin updates; the server and APP_INFO carry the current version.
UI_RESOURCE_URI = "ui://desk-status/panel-0.2.5"
UI_MIME_TYPE = "text/html;profile=mcp-app"
SNAPSHOT_SCHEMA = "desk-status/2"
SNAPSHOT_META_KEY = "desk-status/snapshot"
