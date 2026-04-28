"""Sessions service — CRUD + history + activity tracking.

Sole owner of writes to the ``Session`` Beanie document. Module-level
``async def`` API, no class wrapper, no Protocol-based repository.

Public API:
- ``create(ctx, workspace_id, body)`` — create or upsert a session
- ``list_for_owner(ctx, workspace_id)``
- ``list_by_agent(ctx, workspace_id, agent_id)``
- ``list_for_pocket(ctx, pocket_id)``
- ``get(ctx, session_id)``
- ``update(ctx, session_id, body)``
- ``delete(ctx, session_id)`` — soft delete
- ``link_pocket(workspace_id, session_id_str, pocket_id)`` — used by
  ``pockets/service.py`` when a pocket is created with a session_id
- ``get_history(session_id, user_id, limit)`` — kept on Beanie because it
  spans three context types and reads the unified Message collection
- ``touch(session_id)`` — kept on Beanie; called by chat persistence
  bridges on the hot path
- ``legacy_ctx(user_id, workspace_id)`` — helper for routers that haven't
  yet migrated to ``Depends(request_context)``
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import (
    SessionCreated,
    SessionDeleted,
    SessionUpdated,
)
from ee.cloud.models.session import Session as _SessionDoc
from ee.cloud.sessions.dto import (
    CreateSessionRequest,
    UpdateSessionRequest,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

if TYPE_CHECKING:
    from ee.cloud.sessions.domain import Session as DomainSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _to_domain(doc: _SessionDoc) -> DomainSession:
    from ee.cloud.sessions.domain import Session

    return Session(
        id=str(doc.id),
        sessionId=doc.sessionId,
        context_type=doc.context_type or "session",
        workspace=doc.workspace,
        owner=doc.owner,
        title=doc.title,
        pocket=doc.pocket,
        group=doc.group,
        agent=doc.agent,
        message_count=doc.messageCount,
        last_activity=doc.lastActivity,
        created_at=getattr(doc, "createdAt", datetime.now(UTC)),  # type: ignore[arg-type]
        deleted_at=doc.deleted_at,
    )


def legacy_ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    """Build a RequestContext for routers that haven't migrated to
    ``Depends(request_context)`` yet. Synthesizes request_id and scope."""
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="legacy",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Public API — CRUD
# ---------------------------------------------------------------------------


async def create(
    ctx: RequestContext,
    workspace_id: str,
    body: CreateSessionRequest,
) -> DomainSession:
    """Create a session, or update-and-return the existing one when
    ``body.session_id`` matches an existing row."""
    sid = body.session_id or f"websocket_{uuid.uuid4().hex[:12]}"

    if body.session_id:
        existing_doc = await _SessionDoc.find_one(
            _SessionDoc.sessionId == body.session_id
        )
        if existing_doc is not None:
            patched: dict = {}
            if body.pocket_id and body.pocket_id != existing_doc.pocket:
                existing_doc.pocket = body.pocket_id
                # Linking a pocket to a session-typed session promotes its
                # context_type so the model validator (which forbids
                # session-typed rows from carrying a pocket) is satisfied.
                if existing_doc.context_type == "session":
                    existing_doc.context_type = "pocket"
                patched["pocket_id"] = body.pocket_id
            if (
                body.title
                and body.title != "New Chat"
                and body.title != existing_doc.title
            ):
                existing_doc.title = body.title
                patched["title"] = body.title
            if patched:
                await existing_doc.save()
                await emit(
                    SessionUpdated(
                        data={
                            "session_id": str(existing_doc.id),
                            "user_id": existing_doc.owner,
                            **patched,
                        }
                    )
                )
            return _to_domain(existing_doc)

    if body.group_id:
        ctype = "group"
    elif body.pocket_id:
        ctype = "pocket"
    else:
        ctype = "session"

    doc = _SessionDoc(
        sessionId=sid,
        context_type=ctype,
        workspace=workspace_id,
        owner=ctx.user_id,
        title=body.title,
        pocket=body.pocket_id,
        group=body.group_id,
        agent=body.agent_id,
    )
    await doc.insert()
    session = _to_domain(doc)

    await event_bus.emit(
        "session.created",
        {
            "session_id": session.id,
            "session_uuid": session.sessionId,
            "workspace_id": workspace_id,
            "owner_id": ctx.user_id,
            "pocket_id": body.pocket_id,
        },
    )

    created_data: dict = {
        "session_id": session.id,
        "user_id": ctx.user_id,
        "agent_id": body.agent_id,
        "workspace_id": workspace_id,
    }
    if body.pocket_id:
        created_data["pocket_id"] = body.pocket_id
    await emit(SessionCreated(data=created_data))

    return session


async def list_for_owner(
    ctx: RequestContext, workspace_id: str
) -> list[DomainSession]:
    docs = (
        await _SessionDoc.find(
            _SessionDoc.workspace == workspace_id,
            _SessionDoc.owner == ctx.user_id,
            _SessionDoc.deleted_at == None,  # noqa: E711
        )
        .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
        .to_list()
    )
    return [_to_domain(d) for d in docs]


async def list_by_agent(
    ctx: RequestContext, workspace_id: str, agent_id: str
) -> list[DomainSession]:
    docs = (
        await _SessionDoc.find(
            _SessionDoc.workspace == workspace_id,
            _SessionDoc.owner == ctx.user_id,
            _SessionDoc.agent == agent_id,
            _SessionDoc.deleted_at == None,  # noqa: E711
        )
        .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
        .to_list()
    )
    return [_to_domain(d) for d in docs]


async def list_for_pocket(
    ctx: RequestContext, pocket_id: str
) -> list[DomainSession]:
    docs = (
        await _SessionDoc.find(
            _SessionDoc.pocket == pocket_id,
            _SessionDoc.owner == ctx.user_id,
            _SessionDoc.deleted_at == None,  # noqa: E711
        )
        .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
        .to_list()
    )
    return [_to_domain(d) for d in docs]


async def _fetch_owned(session_id: str, user_id: str) -> _SessionDoc:
    """Internal: fetch by ObjectId or sessionId; check owner; raise
    NotFound / Forbidden as needed. Used by both ``get`` and the
    history/touch helpers that need the raw doc."""
    doc: _SessionDoc | None = None
    try:
        doc = await _SessionDoc.get(PydanticObjectId(session_id))
    except Exception:
        doc = None
    if doc is None:
        doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
    if doc is None or doc.deleted_at:
        raise NotFound("session", session_id)
    if doc.owner != user_id:
        raise Forbidden("session.not_owner", "Not the session owner")
    return doc


async def get(ctx: RequestContext, session_id: str) -> DomainSession:
    doc = await _fetch_owned(session_id, ctx.user_id)
    return _to_domain(doc)


async def update(
    ctx: RequestContext,
    session_id: str,
    body: UpdateSessionRequest,
) -> DomainSession:
    doc = await _fetch_owned(session_id, ctx.user_id)
    patched = body.model_dump(exclude_unset=True)
    if not patched:
        return _to_domain(doc)
    if body.title is not None:
        doc.title = body.title
    if body.pocket_id is not None:
        doc.pocket = body.pocket_id
        if doc.context_type == "session":
            doc.context_type = "pocket"
    await doc.save()
    await emit(
        SessionUpdated(
            data={
                "session_id": str(doc.id),
                "user_id": ctx.user_id,
                **patched,
            }
        )
    )
    return _to_domain(doc)


async def delete(ctx: RequestContext, session_id: str) -> None:
    doc = await _fetch_owned(session_id, ctx.user_id)
    doc.deleted_at = datetime.now(UTC)
    await doc.save()
    await emit(
        SessionDeleted(
            data={
                "session_id": str(doc.id),
                "user_id": ctx.user_id,
            }
        )
    )


async def link_pocket(
    workspace_id: str, session_id_str: str, pocket_id: str
) -> None:
    """Link a session (looked up by its sessionId string) to a pocket.

    Used by ``pockets/service.create_pocket`` when called with
    ``body.session_id``. No-op if the session doesn't exist or is in a
    different workspace.
    """
    doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id_str)
    if doc is None or doc.workspace != workspace_id:
        return
    doc.pocket = pocket_id
    # Promote the session's context_type so the model validator (which
    # forbids session-typed rows from carrying a pocket) is satisfied.
    if doc.context_type == "session":
        doc.context_type = "pocket"
    await doc.save()


# ---------------------------------------------------------------------------
# History + activity — kept on Beanie (cross-cutting, hot path)
# ---------------------------------------------------------------------------


async def get_history(session_id: str, user_id: str, limit: int = 100) -> dict:
    """Return session chat history from the unified Mongo messages store.

    Spans three context types (session/group/pocket) with shape-specific
    queries. Stays on Beanie because a full extraction would need a
    separate HistoryReader port.
    """
    from ee.cloud.models.message import Message

    session = await _fetch_owned(session_id, user_id)

    if session.context_type == "session":
        messages = (
            await Message.find(
                {
                    "context_type": "session",
                    "session_key": f"cloud:session:{session.id}:{session.agent}",
                }
            )
            .sort("createdAt")
            .limit(limit)
            .to_list()
        )
        return {
            "messages": [
                {
                    "_id": str(m.id),
                    "role": m.role or "user",
                    "content": m.content,
                    "sender": m.sender,
                    "senderType": m.sender_type,
                    "createdAt": iso_utc(m.createdAt),
                    "attachments": [a.model_dump() for a in (m.attachments or [])],
                }
                for m in messages
            ]
        }

    if session.context_type == "group" and session.group:
        messages = (
            await Message.find(
                {
                    "context_type": "group",
                    "group": session.group,
                    "deleted": False,
                }
            )
            .sort("createdAt")
            .limit(limit)
            .to_list()
        )
        return {
            "messages": [
                {
                    "_id": str(m.id),
                    "role": "assistant" if m.sender_type == "agent" else "user",
                    "content": m.content,
                    "sender": m.sender,
                    "senderType": m.sender_type,
                    "createdAt": iso_utc(m.createdAt),
                    "attachments": [a.model_dump() for a in (m.attachments or [])],
                }
                for m in messages
            ]
        }

    # Pocket context — three writer paths land here with different
    # session_key shapes; preserved verbatim from the legacy implementation.
    pocket_candidate_keys = [session.sessionId]
    if session.pocket and session.agent:
        pocket_candidate_keys.append(
            f"cloud:pocket:{session.pocket}:{session.agent}"
        )
    or_clauses: list[dict[str, Any]] = [
        {"context_type": "pocket", "session_key": {"$in": pocket_candidate_keys}}
    ]
    if session.agent:
        or_clauses.append(
            {
                "context_type": "session",
                "session_key": f"cloud:session:{session.id}:{session.agent}",
            }
        )

    messages = (
        await Message.find({"$or": or_clauses})
        .sort("createdAt")
        .limit(limit)
        .to_list()
    )
    return {
        "messages": [
            {
                "_id": str(m.id),
                "role": m.role or "user",
                "content": m.content,
                "sender": m.sender,
                "senderType": m.sender_type,
                "createdAt": iso_utc(m.createdAt),
                "attachments": [a.model_dump() for a in (m.attachments or [])],
            }
            for m in messages
        ]
    }


async def touch(session_id: str) -> None:
    """Update lastActivity and increment messageCount.

    Called by chat persistence bridges on the hot path; kept on Beanie.
    """
    doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
    if not doc and session_id.startswith("websocket_"):
        doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id[10:])
    if not doc:
        return
    doc.lastActivity = datetime.now(UTC)
    doc.messageCount += 1
    await doc.save()

    await emit(
        SessionUpdated(
            data={
                "session_id": str(doc.id),
                "user_id": doc.owner,
                "last_message_at": iso_utc(doc.lastActivity),
            }
        )
    )


__all__ = [
    "legacy_ctx",
    "create",
    "list_for_owner",
    "list_by_agent",
    "list_for_pocket",
    "get",
    "update",
    "delete",
    "link_pocket",
    "get_history",
    "touch",
]
