"""Agent-facing pocket fetch — returns the full pocket document as a
compact JSON string for injection into agent tool responses.

Lives under ee/cloud/pockets/ because it's pocket-domain logic. The thin
MCP binding that exposes this to the Claude Agent SDK is in
``src/pocketpaw/agents/sdk_mcp_pocket.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fields dropped from the agent-visible pocket payload:
# - secrets that the agent should never see
# - bulk relationship fields the agent doesn't need to reason about the
#   pocket's contents or layout
_AGENT_INVISIBLE_FIELDS = (
    "share_link_token",
    "shared_with",
    "team",
    "agents",
)


def _json_safe(doc: Any) -> Any:
    """Normalize a Mongo/Beanie document so ``json.dumps`` can serialize it."""
    return json.loads(json.dumps(doc, default=str))


async def fetch_pocket_for_agent(pocket_id: str) -> dict[str, Any]:
    """Return the full pocket document for an agent, or an error dict.

    Shape on success:
        {"ok": True, "pocket": {...}}
    Shape on failure:
        {"ok": False, "error": "..."}
    """
    if not pocket_id or not isinstance(pocket_id, str):
        return {"ok": False, "error": "pocket_id is required (string)"}

    try:
        from beanie import PydanticObjectId

        from ee.cloud.models.pocket import Pocket

        pocket = await Pocket.get(PydanticObjectId(pocket_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_pocket_for_agent: lookup failed for %s: %s", pocket_id, exc)
        return {"ok": False, "error": f"could not load pocket {pocket_id}: {exc}"}

    if pocket is None:
        return {"ok": False, "error": f"pocket {pocket_id} not found"}

    doc = pocket.model_dump(mode="json", by_alias=True, exclude_none=True)
    for k in _AGENT_INVISIBLE_FIELDS:
        doc.pop(k, None)
    return {"ok": True, "pocket": _json_safe(doc)}


# ---------------------------------------------------------------------------
# Mutation helpers — backing the MCP write tools the cloud SSE chat agent
# uses to edit the pocket it lives inside. We don't run an HTTP request /
# auth dependency stack from inside the agent's tool channel; the agent
# runs in-process for an authorized user, mirroring the trust model
# ``fetch_pocket_for_agent`` already relies on.
# ---------------------------------------------------------------------------


async def _load_pocket(pocket_id: str) -> tuple[Any | None, str | None]:
    if not pocket_id or not isinstance(pocket_id, str):
        return None, "pocket_id is required (string)"
    try:
        from beanie import PydanticObjectId

        from ee.cloud.models.pocket import Pocket

        pocket = await Pocket.get(PydanticObjectId(pocket_id))
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load pocket {pocket_id}: {exc}"
    if pocket is None:
        return None, f"pocket {pocket_id} not found"
    return pocket, None


def _ok(pocket: Any) -> dict[str, Any]:
    doc = pocket.model_dump(mode="json", by_alias=True, exclude_none=True)
    for k in _AGENT_INVISIBLE_FIELDS:
        doc.pop(k, None)
    safe = _json_safe(doc)
    # Push to the active SSE stream's mutation sink (if any) so the chat
    # surfaces a ``pocket_mutation`` event in real time. Imported lazily
    # so the cloud-chat dependency tree stays optional for callers that
    # never go through the SSE path (CLI tools, unit tests).
    try:
        from ee.cloud.chat.agent_service import push_pocket_mutation

        push_pocket_mutation(
            {
                "action": "replace",
                "pocket_id": str(getattr(pocket, "id", "")) or safe.get("_id", ""),
                "pocket": safe,
            }
        )
    except Exception:
        logger.debug("push_pocket_mutation failed (non-fatal)", exc_info=True)
    return {"ok": True, "pocket": safe}


async def update_pocket_for_agent(
    pocket_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    ripple_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Patch top-level pocket fields. ``ripple_spec`` is normalized.

    Only fields the caller explicitly provides are touched — passing
    ``None`` (the default) leaves the existing value alone.
    """
    pocket, err = await _load_pocket(pocket_id)
    if err:
        return {"ok": False, "error": err}

    try:
        from ee.cloud.ripple_normalizer import normalize_ripple_spec
    except Exception:  # noqa: BLE001
        normalize_ripple_spec = None  # type: ignore[assignment]

    if name is not None:
        pocket.name = name
    if description is not None:
        pocket.description = description
    if icon is not None:
        pocket.icon = icon
    if color is not None:
        pocket.color = color
    if ripple_spec is not None:
        pocket.rippleSpec = (
            normalize_ripple_spec(ripple_spec) if normalize_ripple_spec else ripple_spec
        )

    try:
        await pocket.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_pocket_for_agent: save failed for %s: %s", pocket_id, exc)
        return {"ok": False, "error": f"save failed: {exc}"}
    return _ok(pocket)


async def add_widget_for_agent(pocket_id: str, widget: dict[str, Any]) -> dict[str, Any]:
    """Append a widget to the pocket's embedded widget list."""
    if not isinstance(widget, dict):
        return {"ok": False, "error": "widget must be a JSON object"}

    pocket, err = await _load_pocket(pocket_id)
    if err:
        return {"ok": False, "error": err}

    try:
        from ee.cloud.models.pocket import Widget

        new_widget = Widget(
            name=widget.get("name", "Widget"),
            type=widget.get("type", "custom"),
            icon=widget.get("icon", ""),
            color=widget.get("color", ""),
            span=widget.get("span", "col-span-1"),
            dataSourceType=widget.get("dataSourceType", "static"),
            config=widget.get("config", {}) or {},
            props=widget.get("props", {}) or {},
            data=widget.get("data"),
            assignedAgent=widget.get("assignedAgent"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"invalid widget spec: {exc}"}

    pocket.widgets.append(new_widget)
    try:
        await pocket.save()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"save failed: {exc}"}
    return _ok(pocket)


async def update_widget_for_agent(
    pocket_id: str, widget_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a single embedded widget."""
    if not isinstance(fields, dict):
        return {"ok": False, "error": "fields must be a JSON object"}

    pocket, err = await _load_pocket(pocket_id)
    if err:
        return {"ok": False, "error": err}

    widget = next((w for w in pocket.widgets if w.id == widget_id), None)
    if widget is None:
        return {"ok": False, "error": f"widget {widget_id} not found in pocket {pocket_id}"}

    for k in ("name", "type", "icon", "color", "span", "data", "assignedAgent"):
        if k in fields:
            setattr(widget, k, fields[k])
    if "config" in fields and isinstance(fields["config"], dict):
        widget.config = fields["config"]
    if "props" in fields and isinstance(fields["props"], dict):
        widget.props = fields["props"]
    if "dataSourceType" in fields:
        widget.dataSourceType = fields["dataSourceType"]

    try:
        await pocket.save()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"save failed: {exc}"}
    return _ok(pocket)


async def create_pocket_for_agent(
    *,
    name: str,
    description: str = "",
    type_: str = "custom",
    icon: str = "",
    color: str = "",
    ripple_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a brand-new ``Pocket`` document in Mongo, owned by the
    currently-streaming user in their active workspace.

    Workspace and owner identity come from the per-stream
    ``ContextVar``s set by ``agent_router._run_agent_stream`` because
    the in-process MCP tool channel doesn't reach the FastAPI request
    scope. Returns the same ``{ok, pocket}`` shape as the other
    helpers and pushes a ``pocket_created`` SSE event so connected
    frontends mount the new pocket immediately.
    """
    if not name:
        return {"ok": False, "error": "name is required"}

    try:
        from ee.cloud.chat.agent_service import (
            current_session_mongo_id,
            current_user_id,
            current_workspace_id,
            push_sse_event,
        )
        from ee.cloud.models.pocket import Pocket
        from ee.cloud.models.session import Session
        from ee.cloud.ripple_normalizer import normalize_ripple_spec
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"cloud models unavailable: {exc}"}

    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return {
            "ok": False,
            "error": (
                "no active workspace/user — create_pocket can only be called "
                "from inside a cloud SSE chat stream"
            ),
        }

    normalized = normalize_ripple_spec(ripple_spec) if ripple_spec else None

    try:
        pocket = Pocket(
            workspace=workspace_id,
            name=name,
            description=description,
            type=type_,
            icon=icon,
            color=color,
            owner=user_id,
            rippleSpec=normalized,
            visibility="workspace",
        )
        await pocket.insert()
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_pocket_for_agent insert failed: %s", exc, exc_info=True)
        return {"ok": False, "error": f"insert failed: {exc}"}

    safe = _json_safe(
        pocket.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    for k in _AGENT_INVISIBLE_FIELDS:
        safe.pop(k, None)

    # Link the active chat session to the newly-created pocket so the
    # conversation that built it shows up in the pocket's session list.
    # Without this the chat is orphaned at the workspace level: the user
    # navigates into the new pocket, sees an empty sessions list, and a
    # fresh blank session gets auto-created — losing visibility of the
    # creation conversation.
    session_mongo_id = current_session_mongo_id()
    if session_mongo_id:
        try:
            from beanie import PydanticObjectId

            session = await Session.get(PydanticObjectId(session_mongo_id))
            if session is not None and session.owner == user_id:
                session.pocket = str(pocket.id)
                session.context_type = "pocket"
                # Inherit the agent the conversation is running under so
                # ``list_for_pocket`` returns rich session rows. The
                # writer earlier in this stream may have already set this
                # — only fill in when missing.
                await session.save()

                try:
                    from ee.cloud.realtime.emit import emit
                    from ee.cloud.realtime.events import SessionUpdated

                    await emit(
                        SessionUpdated(
                            data={
                                "session_id": str(session.id),
                                "user_id": user_id,
                                "pocket_id": str(pocket.id),
                            }
                        )
                    )
                except Exception:
                    logger.debug(
                        "SessionUpdated emit after pocket-link failed",
                        exc_info=True,
                    )
        except Exception:
            logger.warning(
                "create_pocket: failed to link session %s to pocket %s",
                session_mongo_id,
                pocket.id,
                exc_info=True,
            )

    # Push a ``pocket_created`` SSE event onto the active stream so the
    # frontend mounts the new pocket without waiting for a sidebar
    # refresh. ``session_id`` echoes back so the frontend's
    # ``handlePocketCreated`` can keep the user on their existing chat.
    try:
        push_sse_event(
            "pocket_created",
            {
                "pocket_id": str(pocket.id),
                "pocket": safe,
                "session_id": session_mongo_id,
            },
        )
    except Exception:
        logger.debug("push_sse_event(pocket_created) failed", exc_info=True)

    return {"ok": True, "pocket": safe}


async def remove_widget_for_agent(pocket_id: str, widget_id: str) -> dict[str, Any]:
    """Remove a widget from the pocket's embedded widget list."""
    pocket, err = await _load_pocket(pocket_id)
    if err:
        return {"ok": False, "error": err}

    before = len(pocket.widgets)
    pocket.widgets = [w for w in pocket.widgets if w.id != widget_id]
    if len(pocket.widgets) == before:
        return {"ok": False, "error": f"widget {widget_id} not found in pocket {pocket_id}"}

    try:
        await pocket.save()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"save failed: {exc}"}
    return _ok(pocket)
