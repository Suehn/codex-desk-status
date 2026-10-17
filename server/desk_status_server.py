#!/usr/bin/env python3
"""stdio entry point for the 刻漏 (desk-status) MCP server.

Launched by the plugin's mcp.json as ``python3 -I server/desk_status_server.py``.
``-I`` (isolated mode) keeps PYTHONPATH / user site-packages / the current
directory out of ``sys.path``, so we add this file's own directory explicitly.
"""

import os
import sys

if sys.version_info < (3, 8):
    sys.stderr.write("[desk-status] error: Python 3.8+ is required\n")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from desk_status.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
