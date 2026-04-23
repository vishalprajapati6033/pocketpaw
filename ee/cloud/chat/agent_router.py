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
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    resolve_scope_context,
)
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.shared.errors import CloudError

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
# Collaborators (stubs — Task 7 replaces with real bridge)
# ---------------------------------------------------------------------------


async def _persist_user_message(ctx: ScopeContext, body: CloudAgentChatRequest) -> str:
    """Persist the user message. Task 7 wires this to MessageService."""
    raise NotImplementedError("wired in Task 7")


async def _run_agent_stream(
    ctx: ScopeContext,
    user_message_id: str,
    body: CloudAgentChatRequest,
    cancel_event: asyncio.Event,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield (event_name, data) tuples from the agent run. Task 7 replaces this stub."""
    if False:
        yield  # pragma: no cover — typing hint only
    raise NotImplementedError("wired in Task 7")


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]
