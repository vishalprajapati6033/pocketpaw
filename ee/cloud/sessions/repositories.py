"""Repository for the sessions module.

Defines `ISessionRepository` and a Beanie-backed implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud._core.errors import NotFound
from ee.cloud.models.session import Session as _SessionDoc
from ee.cloud.sessions.domain import Session


def _to_domain(doc: _SessionDoc) -> Session:
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


@runtime_checkable
class ISessionRepository(Protocol):
    async def get(self, session_id: str) -> Session | None: ...
    async def get_by_session_id(self, session_id: str) -> Session | None: ...
    async def list_for_owner(self, *, workspace_id: str, user_id: str) -> list[Session]: ...
    async def list_by_agent(
        self, *, workspace_id: str, user_id: str, agent_id: str
    ) -> list[Session]: ...
    async def list_for_pocket(self, *, pocket_id: str, user_id: str) -> list[Session]: ...
    async def create(
        self,
        *,
        sessionId: str,
        context_type: str,
        workspace_id: str,
        owner: str,
        title: str,
        pocket: str | None,
        group: str | None,
        agent: str | None,
    ) -> Session: ...
    async def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pocket: str | None = None,
    ) -> Session: ...
    async def soft_delete(self, session_id: str) -> None: ...


class MongoSessionRepository:
    """Beanie-backed implementation."""

    async def get(self, session_id: str) -> Session | None:
        try:
            doc = await _SessionDoc.get(PydanticObjectId(session_id))
        except Exception:
            return None
        return _to_domain(doc) if doc else None

    async def get_by_session_id(self, session_id: str) -> Session | None:
        doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
        return _to_domain(doc) if doc else None

    async def list_for_owner(self, *, workspace_id: str, user_id: str) -> list[Session]:
        docs = (
            await _SessionDoc.find(
                _SessionDoc.workspace == workspace_id,
                _SessionDoc.owner == user_id,
                _SessionDoc.deleted_at == None,  # noqa: E711
            )
            .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
            .to_list()
        )
        return [_to_domain(d) for d in docs]

    async def list_by_agent(
        self, *, workspace_id: str, user_id: str, agent_id: str
    ) -> list[Session]:
        docs = (
            await _SessionDoc.find(
                _SessionDoc.workspace == workspace_id,
                _SessionDoc.owner == user_id,
                _SessionDoc.agent == agent_id,
                _SessionDoc.deleted_at == None,  # noqa: E711
            )
            .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
            .to_list()
        )
        return [_to_domain(d) for d in docs]

    async def list_for_pocket(self, *, pocket_id: str, user_id: str) -> list[Session]:
        docs = (
            await _SessionDoc.find(
                _SessionDoc.pocket == pocket_id,
                _SessionDoc.owner == user_id,
                _SessionDoc.deleted_at == None,  # noqa: E711
            )
            .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
            .to_list()
        )
        return [_to_domain(d) for d in docs]

    async def create(
        self,
        *,
        sessionId: str,
        context_type: str,
        workspace_id: str,
        owner: str,
        title: str,
        pocket: str | None,
        group: str | None,
        agent: str | None,
    ) -> Session:
        doc = _SessionDoc(
            sessionId=sessionId,
            context_type=context_type,
            workspace=workspace_id,
            owner=owner,
            title=title,
            pocket=pocket,
            group=group,
            agent=agent,
        )
        await doc.insert()
        return _to_domain(doc)

    async def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pocket: str | None = None,
    ) -> Session:
        doc = await _SessionDoc.get(PydanticObjectId(session_id))
        if doc is None:
            raise NotFound("session", session_id)
        if title is not None:
            doc.title = title
        if pocket is not None:
            doc.pocket = pocket
        await doc.save()
        return _to_domain(doc)

    async def soft_delete(self, session_id: str) -> None:
        doc = await _SessionDoc.get(PydanticObjectId(session_id))
        if doc is None:
            raise NotFound("session", session_id)
        doc.deleted_at = datetime.now(UTC)
        await doc.save()


_default: ISessionRepository | None = None


def get_default_repository() -> ISessionRepository:
    global _default
    if _default is None:
        _default = MongoSessionRepository()
    return _default


def set_default_repository(repo: ISessionRepository) -> None:
    global _default
    _default = repo


__all__ = [
    "ISessionRepository",
    "MongoSessionRepository",
    "get_default_repository",
    "set_default_repository",
]
