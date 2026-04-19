"""In-process SDK MCP binding that exposes pocket context to the Claude Agent SDK.

Why MCP: the Claude Agent SDK passes the system prompt to ``claude.exe`` as a
CLI argument. Windows ``CreateProcess`` caps command lines at ~32KB (WinError
206), so embedding a full pocket document — including a large
``rippleSpec.ui`` tree — in the prompt is unsafe.

Instead we register an in-process MCP server with a ``get_pocket`` tool. The
agent fetches the full pocket on demand; the response flows through the SDK's
tool-result channel (stdio JSON, unbounded) and never touches the CLI command
line.

This module is a thin adapter — the actual fetch lives in
``ee/cloud/pockets/agent_context.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_pocket"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
GET_POCKET_TOOL_ID = f"mcp__{SERVER_NAME}__get_pocket"


async def _get_pocket_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import fetch_pocket_for_agent

    result = await fetch_pocket_for_agent(args.get("pocket_id", ""))
    if not result.get("ok"):
        return {
            "content": [{"type": "text", "text": f"Error: {result.get('error')}"}],
            "is_error": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result["pocket"], separators=(",", ":")),
            }
        ]
    }


def build_pocket_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or return None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocket_context MCP disabled")
        return None

    @tool(
        "get_pocket",
        (
            "Fetch the full PocketPaw pocket document (rippleSpec, widgets, "
            "visibility, metadata) by id. Call this before answering any "
            "question about what a pocket contains, its widgets, or its layout."
        ),
        {"pocket_id": str},
    )
    async def get_pocket(args):  # type: ignore[no-untyped-def]
        return await _get_pocket_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[get_pocket],
    )
    return SERVER_NAME, server
