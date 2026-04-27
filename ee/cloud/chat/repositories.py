"""Repositories for the chat module.

Two protocols:
- ``IMessageRepository`` — message CRUD across all three context types
  (group, pocket, session). The repository converts between the Beanie
  ``Message`` document and the domain ``Message`` value object.
- ``IGroupRepository`` — group CRUD + membership operations.

Phase 10 ships these protocols + Mongo implementations as available
infrastructure. The existing ``GroupService`` and ``MessageService``
classmethods keep working unchanged; new code that wants the cleaner
abstraction can DI an instance with these repos. A future incremental
migration will move method-by-method from the classmethods onto
instance services that depend on these protocols.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud.chat.domain import (
    Attachment,
    Group,
    GroupAgent,
    Mention,
    Message,
    Reaction,
)
from ee.cloud.models.group import Group as _GroupDoc
from ee.cloud.models.group import GroupAgent as _GroupAgentDoc
from ee.cloud.models.message import Attachment as _AttachmentDoc
from ee.cloud.models.message import Mention as _MentionDoc
from ee.cloud.models.message import Message as _MessageDoc
from ee.cloud.models.message import Reaction as _ReactionDoc

# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _mention_to_domain(m: _MentionDoc) -> Mention:
    return Mention(type=m.type, id=m.id, display_name=m.display_name)


def _attachment_to_domain(a: _AttachmentDoc) -> Attachment:
    return Attachment(
        type=a.type, url=a.url, name=a.name, meta=tuple((k, v) for k, v in a.meta.items())
    )


def _reaction_to_domain(r: _ReactionDoc) -> Reaction:
    return Reaction(emoji=r.emoji, users=tuple(r.users))


def _message_to_domain(doc: _MessageDoc) -> Message:
    return Message(
        id=str(doc.id),
        context_type=doc.context_type or "group",
        workspace_id=doc.workspace_id,
        group=doc.group,
        sender=doc.sender,
        sender_type=doc.sender_type,
        agent=doc.agent,
        content=doc.content,
        mentions=tuple(_mention_to_domain(m) for m in doc.mentions),
        reply_to=doc.reply_to,
        thread_count=doc.thread_count,
        attachments=tuple(_attachment_to_domain(a) for a in doc.attachments),
        reactions=tuple(_reaction_to_domain(r) for r in doc.reactions),
        edited=doc.edited,
        edited_at=doc.edited_at,
        deleted=doc.deleted,
        session_key=doc.session_key,
        role=doc.role,
        created_at=getattr(doc, "createdAt", None),
    )


def _group_agent_to_domain(ga: _GroupAgentDoc) -> GroupAgent:
    return GroupAgent(agent_id=ga.agent, role=ga.role, respond_mode=ga.respond_mode)


def _group_to_domain(doc: _GroupDoc) -> Group:
    return Group(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        slug=doc.slug,
        description=doc.description,
        icon=doc.icon,
        color=doc.color,
        type=doc.type,
        members=tuple(doc.members),
        member_roles=tuple(doc.member_roles.items()),
        agents=tuple(_group_agent_to_domain(a) for a in doc.agents),
        pinned_messages=tuple(doc.pinned_messages),
        owner=doc.owner,
        archived=doc.archived,
        last_message_at=doc.last_message_at,
        message_count=doc.message_count,
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        updated_at=getattr(doc, "updatedAt", None),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Message repository
# ---------------------------------------------------------------------------


@runtime_checkable
class IMessageRepository(Protocol):
    async def get(self, message_id: str) -> Message | None: ...
    async def list_for_group(
        self,
        group_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> list[Message]: ...
    async def list_for_session(self, session_key: str, *, limit: int = 50) -> list[Message]: ...
    async def search_in_group(
        self, group_id: str, query: str, *, limit: int = 100
    ) -> list[Message]: ...


class MongoMessageRepository:
    """Beanie-backed implementation of `IMessageRepository`."""

    async def get(self, message_id: str) -> Message | None:
        try:
            doc = await _MessageDoc.get(PydanticObjectId(message_id))
        except Exception:
            return None
        return _message_to_domain(doc) if doc else None

    async def list_for_group(
        self,
        group_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> list[Message]:
        query: dict = {"context_type": "group", "group": group_id}
        if not include_deleted:
            query["deleted"] = False
        if before:
            try:
                query["_id"] = {"$lt": PydanticObjectId(before)}
            except Exception:
                pass
        cursor = (
            _MessageDoc.find(query)
            .sort(-_MessageDoc.id)  # type: ignore[operator,arg-type]
            .limit(limit)
        )
        return [_message_to_domain(d) async for d in cursor]

    async def list_for_session(self, session_key: str, *, limit: int = 50) -> list[Message]:
        cursor = (
            _MessageDoc.find({"session_key": session_key})
            .sort(_MessageDoc.id)  # type: ignore[arg-type]
            .limit(limit)
        )
        return [_message_to_domain(d) async for d in cursor]

    async def search_in_group(
        self, group_id: str, query: str, *, limit: int = 100
    ) -> list[Message]:
        import re

        pattern = re.escape(query)
        docs = (
            await _MessageDoc.find(
                {
                    "context_type": "group",
                    "group": group_id,
                    "deleted": False,
                    "content": {"$regex": pattern, "$options": "i"},
                }
            )
            .limit(limit)
            .to_list()
        )
        return [_message_to_domain(d) for d in docs]


# ---------------------------------------------------------------------------
# Group repository
# ---------------------------------------------------------------------------


@runtime_checkable
class IGroupRepository(Protocol):
    async def get(self, group_id: str) -> Group | None: ...
    async def get_by_slug(self, workspace_id: str, slug: str) -> Group | None: ...
    async def list_for_workspace(
        self, workspace_id: str, *, include_archived: bool = False
    ) -> list[Group]: ...
    async def list_for_user(self, workspace_id: str, user_id: str) -> list[Group]: ...


class MongoGroupRepository:
    """Beanie-backed implementation of `IGroupRepository`."""

    async def get(self, group_id: str) -> Group | None:
        try:
            doc = await _GroupDoc.get(PydanticObjectId(group_id))
        except Exception:
            return None
        return _group_to_domain(doc) if doc else None

    async def get_by_slug(self, workspace_id: str, slug: str) -> Group | None:
        doc = await _GroupDoc.find_one(
            _GroupDoc.workspace == workspace_id,
            _GroupDoc.slug == slug,
        )
        return _group_to_domain(doc) if doc else None

    async def list_for_workspace(
        self, workspace_id: str, *, include_archived: bool = False
    ) -> list[Group]:
        query: dict = {"workspace": workspace_id}
        if not include_archived:
            query["archived"] = False
        docs = await _GroupDoc.find(query).to_list()
        return [_group_to_domain(d) for d in docs]

    async def list_for_user(self, workspace_id: str, user_id: str) -> list[Group]:
        docs = await _GroupDoc.find(
            {"workspace": workspace_id, "members": user_id, "archived": False}
        ).to_list()
        return [_group_to_domain(d) for d in docs]


# ---------------------------------------------------------------------------
# Default-repo accessors
# ---------------------------------------------------------------------------


_default_message: IMessageRepository | None = None
_default_group: IGroupRepository | None = None


def get_message_repository() -> IMessageRepository:
    global _default_message
    if _default_message is None:
        _default_message = MongoMessageRepository()
    return _default_message


def get_group_repository() -> IGroupRepository:
    global _default_group
    if _default_group is None:
        _default_group = MongoGroupRepository()
    return _default_group


def set_message_repository(repo: IMessageRepository) -> None:
    global _default_message
    _default_message = repo


def set_group_repository(repo: IGroupRepository) -> None:
    global _default_group
    _default_group = repo


__all__ = [
    "IGroupRepository",
    "IMessageRepository",
    "MongoGroupRepository",
    "MongoMessageRepository",
    "get_group_repository",
    "get_message_repository",
    "set_group_repository",
    "set_message_repository",
]
