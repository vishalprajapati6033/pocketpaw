"""Enterprise agent chat — SSE endpoint.

``POST /cloud/chat/{scope}/{scope_id}/agent`` streams a typed SSE sequence
to the caller while persisting the user message and (at stream end) the
assistant message. Agent run mechanics live in Task 7 — this module owns
the HTTP + SSE plumbing and scope/auth guards.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    ScopeKind,
    resolve_scope_context,
)

if TYPE_CHECKING:
    from ee.cloud.models.message import Message as _MessageDoc
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.shared.errors import CloudError
from pocketpaw.agents.pool import get_agent_pool  # re-exported for test patching

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


# In-process cancel registry keyed by (scope, scope_id, user_id). A new request
# for the same tuple cancels the prior run — mirrors OSS /chat/stream semantics.
_active_runs: dict[tuple[str, str, str], asyncio.Event] = {}


Scope = Literal["dm", "group", "pocket"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/cloud/chat/{scope}/{scope_id}/agent")
async def post_agent_chat(
    scope: Scope,
    scope_id: str,
    body: CloudAgentChatRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    try:
        ctx = await resolve_scope_context(
            scope=scope, scope_id=scope_id, user_id=user_id, agent_id_hint=body.agent_id
        )
    except InvalidScope:
        raise HTTPException(status_code=400, detail={"code": "scope.invalid"})
    except CloudError as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", 400),
            detail={"code": e.code, "message": str(e)},
        )

    # Signal any prior in-flight run for the same (scope, scope_id, user_id)
    # to stop. We don't wait on it — each generator cleans its own slot in
    # ``_active_runs`` only when the slot still points to its own event, so
    # the new request's entry is safe from the old generator's ``finally``.
    key = (scope, scope_id, user_id)
    prev = _active_runs.get(key)
    if prev is not None:
        prev.set()

    cancel_event = asyncio.Event()
    _active_runs[key] = cancel_event

    try:
        user_message_id = await _persist_user_message(ctx, body)
    except CloudError as e:
        # Clean up our slot on failure so we don't leak a cancel event.
        if _active_runs.get(key) is cancel_event:
            _active_runs.pop(key, None)
        raise HTTPException(
            status_code=getattr(e, "status_code", 400),
            detail={"code": e.code, "message": str(e)},
        )

    async def gen() -> AsyncIterator[bytes]:
        try:
            yield _sse(
                "message.persisted",
                {"user_message_id": user_message_id, "client_message_id": body.client_message_id},
            )
            async for name, data in _run_agent_stream(ctx, user_message_id, body, cancel_event):
                yield _sse(name, data)
                if name in ("stream_end", "error"):
                    break
        finally:
            # Only clear the slot if it still belongs to this run — a
            # superseding request will have replaced ``_active_runs[key]``
            # with its own event, and we must not evict that.
            if _active_runs.get(key) is cancel_event:
                _active_runs.pop(key, None)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cloud/chat/{scope}/{scope_id}/agent/stop")
async def post_agent_chat_stop(
    scope: Scope,
    scope_id: str,
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    key = (scope, scope_id, user_id)
    ev = _active_runs.get(key)
    if ev is None:
        raise HTTPException(status_code=404, detail={"code": "no_active_run"})
    ev.set()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


RIPPLE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _session_key_for(ctx: ScopeContext) -> str:
    """Stable session key for the agent run. Used both as the agent backend's
    session key and as the pocket ``Message.session_key``."""
    return f"cloud:{ctx.kind.value}:{ctx.scope_id}:{ctx.target_agent_id}"


async def _persist_user_message(ctx: ScopeContext, body: CloudAgentChatRequest) -> str:
    """Persist the caller's message as a ``Message`` document and return its id.

    We write directly rather than going through ``MessageService.send_message``
    to avoid triggering the legacy ``agent_bridge`` auto-response path — the
    SSE endpoint is the sole driver of the reply for this request.
    """
    from ee.cloud.models.message import Message

    if ctx.kind is ScopeKind.POCKET:
        msg = Message(
            context_type="pocket",
            session_key=_session_key_for(ctx),
            role="user",
            sender=ctx.user_id,
            sender_type="user",
            content=body.content,
            attachments=body.attachments,
            workspace_id=ctx.workspace_id,
        )
    else:
        msg = Message(
            context_type="group",
            group=ctx.scope_id,
            sender=ctx.user_id,
            sender_type="user",
            content=body.content,
            attachments=body.attachments,
            mentions=body.mentions,
            reply_to=body.reply_to,
            workspace_id=ctx.workspace_id,
        )
    await msg.insert()
    return str(msg.id)


async def _persist_assistant_message(
    ctx: ScopeContext, content: str, attachments: list[dict[str, Any]]
) -> _MessageDoc:
    from ee.cloud.models.message import Attachment, Message

    att_models = [Attachment(**a) if isinstance(a, dict) else a for a in attachments]
    if ctx.kind is ScopeKind.POCKET:
        msg = Message(
            context_type="pocket",
            session_key=_session_key_for(ctx),
            role="assistant",
            sender=None,
            sender_type="agent",
            agent=ctx.target_agent_id,
            content=content,
            attachments=att_models,
            workspace_id=ctx.workspace_id,
        )
    else:
        msg = Message(
            context_type="group",
            group=ctx.scope_id,
            sender=None,
            sender_type="agent",
            agent=ctx.target_agent_id,
            content=content,
            attachments=att_models,
            workspace_id=ctx.workspace_id,
        )
    await msg.insert()
    return msg


async def _broadcast_message_new(
    ctx: ScopeContext,
    message_id: str,
    content: str,
    attachments: list[dict[str, Any]],
    created_at: datetime,
) -> None:
    """Broadcast the finished assistant message to every other scope member."""
    from ee.cloud.chat.schemas import WsOutbound
    from ee.cloud.chat.ws import manager

    others = [m for m in ctx.members if m != ctx.user_id]
    if not others:
        return
    await manager.broadcast_to_group(
        ctx.scope_id,
        others,
        WsOutbound(
            type="message.new",
            data={
                "id": message_id,
                "group": ctx.scope_id,
                "sender_type": "agent",
                "agent": ctx.target_agent_id,
                "content": content,
                "attachments": attachments,
                "created_at": created_at.isoformat(),
            },
        ),
    )


async def _broadcast_agent_typing(ctx: ScopeContext, active: bool) -> None:
    from ee.cloud.chat.schemas import WsOutbound
    from ee.cloud.chat.ws import manager

    others = [m for m in ctx.members if m != ctx.user_id]
    if not others:
        return
    await manager.broadcast_to_group(
        ctx.scope_id,
        others,
        WsOutbound(
            type="agent.typing",
            data={
                "scope": ctx.kind.value,
                "scope_id": ctx.scope_id,
                "agent_id": ctx.target_agent_id,
                "active": active,
            },
        ),
    )


async def _run_agent_stream(
    ctx: ScopeContext,
    user_message_id: str,
    body: CloudAgentChatRequest,
    cancel_event: asyncio.Event,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Drive AgentPool.run and translate events into SSE tuples."""
    run_id = _new_run_id()
    session_key = _session_key_for(ctx)

    pool = get_agent_pool()
    try:
        instance = await pool.get(ctx.target_agent_id)
    except Exception as e:
        logger.exception("Failed to load agent instance %s", ctx.target_agent_id)
        yield ("error", {"code": "agent.load_failed", "message": str(e)})
        return

    # Inject the scope/participants block via knowledge_context — AgentPool.run
    # prepends this to the system prompt, which is the least invasive way to
    # give the agent scope awareness without changing pool.run's signature.
    from ee.cloud.chat.agent_service import build_context_block

    scope_block = build_context_block(ctx)

    await _broadcast_agent_typing(ctx, active=True)

    yield (
        "stream_start",
        {
            "run_id": run_id,
            "agent_id": ctx.target_agent_id,
            "agent_name": getattr(instance, "agent_name", ""),
            "scope": ctx.kind.value,
            "scope_id": ctx.scope_id,
        },
    )

    full_text = ""
    cancelled = False
    try:
        async for event in pool.run(
            ctx.target_agent_id,
            body.content,
            session_key,
            history=None,
            knowledge_context=scope_block,
        ):
            if cancel_event.is_set():
                cancelled = True
                break
            etype = getattr(event, "type", None)
            econtent = getattr(event, "content", "")
            if etype == "message":
                full_text += econtent if isinstance(econtent, str) else ""
                yield ("chunk", {"content": econtent, "type": "text"})
            elif etype == "thinking":
                yield ("thinking", {"content": econtent if isinstance(econtent, str) else ""})
            elif etype == "tool_use":
                name = ""
                if isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                elif isinstance(econtent, str):
                    name = econtent
                yield (
                    "tool_start",
                    {"tool": name, "input": econtent if isinstance(econtent, dict) else {}},
                )
            elif etype == "tool_result":
                name = ""
                output: Any = econtent
                if isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                    output = econtent.get("result", econtent)
                yield ("tool_result", {"tool": name, "output": output})
            elif etype == "done":
                break
    except Exception as e:
        logger.exception("Cloud agent run failed for agent=%s", ctx.target_agent_id)
        yield ("error", {"code": "agent.run_failed", "message": str(e)})
        await _broadcast_agent_typing(ctx, active=False)
        return

    # Extract ripple block from the accumulated text (same regex as agent_bridge).
    attachments: list[dict[str, Any]] = []
    match = RIPPLE_JSON_RE.search(full_text)
    if match:
        try:
            candidate = json.loads(match.group(1))
        except Exception:
            candidate = None
            logger.debug("Ripple parse failed", exc_info=True)
        if isinstance(candidate, dict) and (
            "lifecycle" in candidate or "widgets" in candidate
        ):
            spec: dict[str, Any] = candidate
            try:
                from ee.cloud.ripple_normalizer import normalize_ripple_spec

                normalized = normalize_ripple_spec(candidate)
                if normalized:
                    spec = normalized
            except Exception:
                logger.debug("Ripple normalize failed", exc_info=True)
            attachments.append({"type": "ripple", "meta": spec})
            full_text = (full_text[: match.start()] + full_text[match.end() :]).strip()
            yield ("ripple", {"spec": spec})

    if cancelled or not full_text.strip():
        yield ("stream_end", {"assistant_message_id": None, "usage": {}, "cancelled": cancelled})
        await _broadcast_agent_typing(ctx, active=False)
        return

    assistant_msg = await _persist_assistant_message(ctx, full_text, attachments)
    assistant_id = str(assistant_msg.id)
    await _broadcast_message_new(
        ctx, assistant_id, full_text, attachments, created_at=assistant_msg.createdAt
    )
    await _broadcast_agent_typing(ctx, active=False)

    # Per-agent soul observation — routed to the target agent's SoulManager
    # via AgentPool. Never touches the global default PocketPaw soul.
    try:
        await pool.observe(ctx.target_agent_id, body.content, full_text)
    except Exception:
        logger.warning(
            "pool.observe failed for agent %s — per-agent soul not updated",
            ctx.target_agent_id,
            exc_info=True,
        )

    yield (
        "stream_end",
        {"assistant_message_id": assistant_id, "usage": {}, "cancelled": False},
    )


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]
