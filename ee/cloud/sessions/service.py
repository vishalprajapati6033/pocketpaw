"""Sessions domain — business logic service.

Refactored in Phase 9 (full hexagonal pass): the simple CRUD operations
route through ``ISessionRepository``. The complex ``get_history``
remains on Beanie because it spans three context types and reads the
unified Message collection — a separate slice would migrate it.

Public API: classmethod ``*_default`` facades preserve the call
signatures used by ``pockets/router.py`` and the existing tests.
``touch`` and ``_get_session`` keep their classmethod form because
they're called by chat-persistence bridges and patched by unit tests.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud.models.session import Session as _SessionDoc
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import SessionCreated, SessionDeleted, SessionUpdated
from ee.cloud.sessions.dto import (
    CreateSessionRequest,
    UpdateSessionRequest,
    session_to_wire_dict,
)
from ee.cloud.sessions.repositories import (
    ISessionRepository,
    get_default_repository,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

if TYPE_CHECKING:
    from ee.cloud.sessions.domain import Session as DomainSession

logger = logging.getLogger(__name__)


# Kept for legacy code paths (touch, _get_session, get_history) that
# still operate on Beanie docs directly.
def _session_response(session: _SessionDoc) -> dict:
    return {
        "_id": str(session.id),
        "sessionId": session.sessionId,
        "workspace": session.workspace,
        "owner": session.owner,
        "title": session.title,
        "pocket": session.pocket,
        "group": session.group,
        "agent": session.agent,
        "messageCount": session.messageCount,
        "lastActivity": iso_utc(session.lastActivity),
        "createdAt": iso_utc(session.createdAt),
        "deletedAt": iso_utc(session.deleted_at),
    }


def _legacy_ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="legacy",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


class SessionService:
    """Sessions CRUD + history.

    Construct with a repository for tests; the classmethod ``*_default``
    facades use the global default repo.
    """

    def __init__(self, repository: ISessionRepository) -> None:
        self._repo = repository

    # ------------------------------------------------------------------
    # Instance API — uses the repository
    # ------------------------------------------------------------------

    async def create(
        self,
        ctx: RequestContext,
        workspace_id: str,
        body: CreateSessionRequest,
    ) -> DomainSession:
        """Create a session, or return the existing one if sessionId
        was supplied and matches an existing row."""
        sid = body.session_id or f"websocket_{uuid.uuid4().hex[:12]}"

        if body.session_id:
            existing = await self._repo.get_by_session_id(body.session_id)
            if existing is not None:
                # Update the existing record (link pocket / set title)
                update_kwargs: dict = {}
                if body.pocket_id and body.pocket_id != existing.pocket:
                    update_kwargs["pocket"] = body.pocket_id
                if body.title and body.title != "New Chat" and body.title != existing.title:
                    update_kwargs["title"] = body.title
                if update_kwargs:
                    existing = await self._repo.update(existing.id, **update_kwargs)
                    patched = {k: v for k, v in update_kwargs.items()}
                    if "pocket" in patched:
                        patched["pocket_id"] = patched.pop("pocket")
                    await emit(
                        SessionUpdated(
                            data={
                                "session_id": existing.id,
                                "user_id": existing.owner,
                                **patched,
                            }
                        )
                    )
                return existing

        if body.group_id:
            ctype = "group"
        elif body.pocket_id:
            ctype = "pocket"
        else:
            ctype = "session"

        session = await self._repo.create(
            sessionId=sid,
            context_type=ctype,
            workspace_id=workspace_id,
            owner=ctx.user_id,
            title=body.title,
            pocket=body.pocket_id,
            group=body.group_id,
            agent=body.agent_id,
        )

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

    async def list_sessions(self, ctx: RequestContext, workspace_id: str) -> list[DomainSession]:
        return await self._repo.list_for_owner(workspace_id=workspace_id, user_id=ctx.user_id)

    async def list_by_agent(
        self, ctx: RequestContext, workspace_id: str, agent_id: str
    ) -> list[DomainSession]:
        return await self._repo.list_by_agent(
            workspace_id=workspace_id, user_id=ctx.user_id, agent_id=agent_id
        )

    async def list_for_pocket(self, ctx: RequestContext, pocket_id: str) -> list[DomainSession]:
        return await self._repo.list_for_pocket(pocket_id=pocket_id, user_id=ctx.user_id)

    async def get(self, ctx: RequestContext, session_id: str) -> DomainSession:
        # Try ObjectId first, then sessionId field
        session = await self._repo.get(session_id)
        if session is None:
            session = await self._repo.get_by_session_id(session_id)
        if session is None or session.deleted_at:
            raise NotFound("session", session_id)
        if session.owner != ctx.user_id:
            raise Forbidden("session.not_owner", "Not the session owner")
        return session

    async def update(
        self,
        ctx: RequestContext,
        session_id: str,
        body: UpdateSessionRequest,
    ) -> DomainSession:
        existing = await self.get(ctx, session_id)
        update_kwargs: dict = {}
        if body.title is not None:
            update_kwargs["title"] = body.title
        if body.pocket_id is not None:
            update_kwargs["pocket"] = body.pocket_id
        if not update_kwargs:
            return existing
        updated = await self._repo.update(existing.id, **update_kwargs)
        patched = body.model_dump(exclude_unset=True)
        await emit(
            SessionUpdated(
                data={
                    "session_id": updated.id,
                    "user_id": ctx.user_id,
                    **patched,
                }
            )
        )
        return updated

    async def delete(self, ctx: RequestContext, session_id: str) -> None:
        existing = await self.get(ctx, session_id)
        await self._repo.soft_delete(existing.id)
        await emit(
            SessionDeleted(
                data={
                    "session_id": existing.id,
                    "user_id": ctx.user_id,
                }
            )
        )

    # ------------------------------------------------------------------
    # Classmethod facade — preserves the legacy call signatures used by
    # pockets/router and tests.
    # ------------------------------------------------------------------

    @classmethod
    def _default(cls) -> SessionService:
        return cls(get_default_repository())

    @classmethod
    async def create_default(
        cls, workspace_id: str, user_id: str, body: CreateSessionRequest
    ) -> dict:
        ctx = _legacy_ctx(user_id, workspace_id)
        s = await cls._default().create(ctx, workspace_id, body)
        return session_to_wire_dict(s)

    @classmethod
    async def list_sessions_default(cls, workspace_id: str, user_id: str) -> list[dict]:
        ctx = _legacy_ctx(user_id, workspace_id)
        items = await cls._default().list_sessions(ctx, workspace_id)
        return [session_to_wire_dict(s) for s in items]

    @classmethod
    async def list_by_agent_default(
        cls, workspace_id: str, user_id: str, agent_id: str
    ) -> list[dict]:
        ctx = _legacy_ctx(user_id, workspace_id)
        items = await cls._default().list_by_agent(ctx, workspace_id, agent_id)
        return [session_to_wire_dict(s) for s in items]

    @classmethod
    async def get_default(cls, session_id: str, user_id: str) -> dict:
        ctx = _legacy_ctx(user_id)
        s = await cls._default().get(ctx, session_id)
        return session_to_wire_dict(s)

    @classmethod
    async def update_default(
        cls, session_id: str, user_id: str, body: UpdateSessionRequest
    ) -> dict:
        ctx = _legacy_ctx(user_id)
        s = await cls._default().update(ctx, session_id, body)
        return session_to_wire_dict(s)

    @classmethod
    async def delete_default(cls, session_id: str, user_id: str) -> None:
        ctx = _legacy_ctx(user_id)
        await cls._default().delete(ctx, session_id)

    @classmethod
    async def list_for_pocket_default(cls, pocket_id: str, user_id: str) -> list[dict]:
        ctx = _legacy_ctx(user_id)
        items = await cls._default().list_for_pocket(ctx, pocket_id)
        return [session_to_wire_dict(s) for s in items]

    @classmethod
    async def create_for_pocket_default(
        cls,
        workspace_id: str,
        user_id: str,
        pocket_id: str,
        body: CreateSessionRequest,
    ) -> dict:
        body_with_pocket = CreateSessionRequest(
            title=body.title,
            pocket_id=pocket_id,
            group_id=body.group_id,
            agent_id=body.agent_id,
            session_id=body.session_id,
        )
        return await cls.create_default(workspace_id, user_id, body_with_pocket)

    # ------------------------------------------------------------------
    # Methods kept on Beanie (complex / cross-cutting)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_history(session_id: str, user_id: str, limit: int = 100) -> dict:
        """Return session chat history from the unified Mongo messages store.

        Phase 9 keeps this on Beanie because it spans three context types
        and reads the Message collection in shape-specific ways. A
        separate slice would extract a HistoryReader port.
        """
        from typing import Any

        from ee.cloud.models.message import Message

        session = await SessionService._get_session(session_id, user_id)

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
        # session_key shapes (see original service.py for the full
        # explanation; preserved verbatim).
        pocket_candidate_keys = [session.sessionId]
        if session.pocket and session.agent:
            pocket_candidate_keys.append(f"cloud:pocket:{session.pocket}:{session.agent}")
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

        messages = await Message.find({"$or": or_clauses}).sort("createdAt").limit(limit).to_list()
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

    @staticmethod
    async def touch(session_id: str) -> None:
        """Update lastActivity and increment messageCount.

        Called by chat persistence bridges; kept on Beanie for now
        because the bridge is hot-path and uses Beanie directly.
        """
        session = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
        if not session and session_id.startswith("websocket_"):
            session = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id[10:])
        if not session:
            return
        session.lastActivity = datetime.now(UTC)
        session.messageCount += 1
        await session.save()

        await emit(
            SessionUpdated(
                data={
                    "session_id": str(session.id),
                    "user_id": session.owner,
                    "last_message_at": iso_utc(session.lastActivity),
                }
            )
        )

    @staticmethod
    async def _get_session(session_id: str, user_id: str) -> _SessionDoc:
        """Fetch by ObjectId first, then by sessionId. Returns the Beanie
        doc so legacy callers (get_history) can use it directly."""
        session = None
        try:
            session = await _SessionDoc.get(PydanticObjectId(session_id))
        except Exception:
            session = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)

        if not session or session.deleted_at:
            raise NotFound("session", session_id)
        if session.owner != user_id:
            raise Forbidden("session.not_owner", "Not the session owner")
        return session
