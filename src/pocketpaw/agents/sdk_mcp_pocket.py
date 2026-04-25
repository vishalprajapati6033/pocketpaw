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
UPDATE_POCKET_TOOL_ID = f"mcp__{SERVER_NAME}__update_pocket"
ADD_WIDGET_TOOL_ID = f"mcp__{SERVER_NAME}__add_widget"
UPDATE_WIDGET_TOOL_ID = f"mcp__{SERVER_NAME}__update_widget"
REMOVE_WIDGET_TOOL_ID = f"mcp__{SERVER_NAME}__remove_widget"

POCKET_TOOL_IDS = (
    GET_POCKET_TOOL_ID,
    UPDATE_POCKET_TOOL_ID,
    ADD_WIDGET_TOOL_ID,
    UPDATE_WIDGET_TOOL_ID,
    REMOVE_WIDGET_TOOL_ID,
)


def _result_payload(result: dict) -> dict:
    """Translate an ``agent_context`` ``{ok, pocket|error}`` dict into the MCP
    response shape."""
    if not result.get("ok"):
        return {
            "content": [{"type": "text", "text": f"Error: {result.get('error')}"}],
            "is_error": True,
        }
    body = result.get("pocket", result)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":")),
            }
        ]
    }


async def _get_pocket_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import fetch_pocket_for_agent

    return _result_payload(await fetch_pocket_for_agent(args.get("pocket_id", "")))


async def _update_pocket_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import update_pocket_for_agent

    return _result_payload(
        await update_pocket_for_agent(
            args.get("pocket_id", ""),
            name=args.get("name"),
            description=args.get("description"),
            icon=args.get("icon"),
            color=args.get("color"),
            ripple_spec=args.get("ripple_spec"),
        )
    )


async def _add_widget_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import add_widget_for_agent

    return _result_payload(
        await add_widget_for_agent(args.get("pocket_id", ""), args.get("widget", {}))
    )


async def _update_widget_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import update_widget_for_agent

    return _result_payload(
        await update_widget_for_agent(
            args.get("pocket_id", ""),
            args.get("widget_id", ""),
            args.get("fields", {}),
        )
    )


async def _remove_widget_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import remove_widget_for_agent

    return _result_payload(
        await remove_widget_for_agent(args.get("pocket_id", ""), args.get("widget_id", ""))
    )


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

    @tool(
        "update_pocket",
        (
            "Patch top-level fields on a pocket. Pass ``ripple_spec`` to "
            "replace the rendered UI tree (UISpec v1.0 / UniversalSpec v2.0). "
            "Other patchable fields: name, description, icon, color. "
            "Omit a field to leave it unchanged. Returns the updated pocket "
            "document. Always call ``get_pocket`` first so you keep "
            "non-edited parts of the spec intact."
        ),
        {
            "pocket_id": str,
            "name": str,
            "description": str,
            "icon": str,
            "color": str,
            "ripple_spec": dict,
        },
    )
    async def update_pocket(args):  # type: ignore[no-untyped-def]
        return await _update_pocket_handler(args)

    @tool(
        "add_widget",
        (
            "Append a widget to a pocket's embedded widget list. ``widget`` is "
            "an object: {name, type, icon?, color?, span?, dataSourceType?, "
            "config?, props?, data?, assignedAgent?}. For ripple-rendered "
            "pockets, prefer ``update_pocket`` with a new ``ripple_spec`` "
            "instead — the embedded widget list is the legacy widgets-grid "
            "format."
        ),
        {"pocket_id": str, "widget": dict},
    )
    async def add_widget(args):  # type: ignore[no-untyped-def]
        return await _add_widget_handler(args)

    @tool(
        "update_widget",
        (
            "Patch fields on a single embedded widget. ``fields`` is a partial "
            "object — only present keys are written. Patchable: name, type, "
            "icon, color, span, dataSourceType, config, props, data, "
            "assignedAgent."
        ),
        {"pocket_id": str, "widget_id": str, "fields": dict},
    )
    async def update_widget(args):  # type: ignore[no-untyped-def]
        return await _update_widget_handler(args)

    @tool(
        "remove_widget",
        "Remove a widget from a pocket's embedded widget list by widget_id.",
        {"pocket_id": str, "widget_id": str},
    )
    async def remove_widget(args):  # type: ignore[no-untyped-def]
        return await _remove_widget_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[get_pocket, update_pocket, add_widget, update_widget, remove_widget],
    )
    return SERVER_NAME, server
