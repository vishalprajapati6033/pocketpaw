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

from datetime import datetime
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
    async def get_many(self, message_ids: list[str]) -> list[Message]: ...
    async def list_for_group(
        self,
        group_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> list[Message]: ...
    async def list_for_group_paged(
        self,
        group_id: str,
        *,
        before_time: datetime | None = None,
        before_id: str | None = None,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> list[Message]: ...
    async def list_for_session(self, session_key: str, *, limit: int = 50) -> list[Message]: ...
    async def list_replies(self, parent_message_id: str) -> list[Message]: ...
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

    async def get_many(self, message_ids: list[str]) -> list[Message]:
        """Batch fetch messages by ID — used to prefetch reply parents
        without an N+1 round-trip per message."""
        if not message_ids:
            return []
        oids: list[PydanticObjectId] = []
        for mid in message_ids:
            try:
                oids.append(PydanticObjectId(mid))
            except Exception:
                continue
        if not oids:
            return []
        docs = await _MessageDoc.find({"_id": {"$in": oids}}).to_list()
        return [_message_to_domain(d) for d in docs]

    async def list_for_group_paged(
        self,
        group_id: str,
        *,
        before_time: datetime | None = None,
        before_id: str | None = None,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> list[Message]:
        """Cursor-paginated history newest-first.

        Pagination uses ``(createdAt, _id)`` as a composite cursor so
        same-second messages don't get skipped or repeated. The caller
        supplies the *parsed* cursor parts; cursor encoding is the
        service's responsibility (it's a wire-shape concern).
        """
        query: dict = {"context_type": "group", "group": group_id}
        if not include_deleted:
            query["deleted"] = False
        if before_time is not None and before_id is not None:
            try:
                cursor_oid = PydanticObjectId(before_id)
            except Exception:
                cursor_oid = None
            if cursor_oid is not None:
                query["$or"] = [
                    {"createdAt": {"$lt": before_time}},
                    {"createdAt": before_time, "_id": {"$lt": cursor_oid}},
                ]
        cursor = (
            _MessageDoc.find(query)
            .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
            .limit(limit)
        )
        return [_message_to_domain(d) async for d in cursor]

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

    async def list_replies(self, parent_message_id: str) -> list[Message]:
        """All non-deleted group-context replies to a parent, oldest first."""
        cursor = _MessageDoc.find(
            {
                "context_type": "group",
                "reply_to": parent_message_id,
                "deleted": False,
            }
        ).sort([("createdAt", 1)])  # type: ignore[list-item]
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
    async def list_visible_in_workspace(self, workspace_id: str, user_id: str) -> list[Group]: ...
    async def update_fields(
        self,
        group_id: str,
        *,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        type: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        archived: bool | None = None,
    ) -> Group: ...
    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        slug: str,
        owner: str,
        type: str,
        members: list[str],
        description: str = "",
        icon: str = "",
        color: str = "",
        agents: list[tuple[str, str, str]] | None = None,
    ) -> Group: ...
    async def add_member(
        self, group_id: str, user_id: str, *, role: str | None = None
    ) -> Group: ...
    async def add_members(
        self, group_id: str, member_ids: list[str], *, role: str = "edit"
    ) -> tuple[Group, list[str]]: ...
    async def remove_member(self, group_id: str, user_id: str) -> Group: ...
    async def set_member_role(self, group_id: str, user_id: str, role: str) -> Group: ...
    async def add_group_agent(
        self, group_id: str, agent_id: str, *, role: str, respond_mode: str
    ) -> Group: ...
    async def update_group_agent_respond_mode(
        self, group_id: str, agent_id: str, respond_mode: str
    ) -> Group | None: ...
    async def remove_group_agent(self, group_id: str, agent_id: str) -> Group | None: ...


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

    async def list_visible_in_workspace(self, workspace_id: str, user_id: str) -> list[Group]:
        """Public/channel groups in the workspace + private/DM groups
        the user is a member of. Excludes archived."""
        docs = await _GroupDoc.find(
            {
                "workspace": workspace_id,
                "archived": False,
                "$or": [
                    {"type": {"$in": ["public", "channel"]}},
                    {"members": user_id},
                ],
            }
        ).to_list()
        return [_group_to_domain(d) for d in docs]

    async def update_fields(
        self,
        group_id: str,
        *,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        type: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        archived: bool | None = None,
    ) -> Group:
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)
        if name is not None:
            doc.name = name
        if slug is not None:
            doc.slug = slug
        if description is not None:
            doc.description = description
        if type is not None:
            doc.type = type
        if icon is not None:
            doc.icon = icon
        if color is not None:
            doc.color = color
        if archived is not None:
            doc.archived = archived
        await doc.save()
        return _group_to_domain(doc)

    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        slug: str,
        owner: str,
        type: str,
        members: list[str],
        description: str = "",
        icon: str = "",
        color: str = "",
        agents: list[tuple[str, str, str]] | None = None,
    ) -> Group:
        """Insert a new group and return its domain projection.

        ``agents`` is an optional list of ``(agent_id, role, respond_mode)``
        triples to attach at creation time (used by DM-with-agent).
        """
        agent_docs = (
            [
                _GroupAgentDoc(agent=aid, role=arole, respond_mode=amode)
                for (aid, arole, amode) in agents or []
            ]
            if agents
            else []
        )
        doc = _GroupDoc(
            workspace=workspace_id,
            name=name,
            slug=slug,
            description=description,
            type=type,
            icon=icon,
            color=color,
            members=members,
            owner=owner,
            agents=agent_docs,
        )
        await doc.insert()
        return _group_to_domain(doc)

    async def add_member(self, group_id: str, user_id: str, *, role: str | None = None) -> Group:
        """Add user_id to the group's members list (idempotent).
        ``role`` optionally records the user's role in member_roles."""
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)
        changed = False
        if user_id not in doc.members:
            doc.members.append(user_id)
            changed = True
        if role is not None and doc.member_roles.get(user_id) != role:
            doc.member_roles[user_id] = role  # type: ignore[assignment]
            changed = True
        if changed:
            await doc.save()
        return _group_to_domain(doc)

    async def add_members(
        self, group_id: str, member_ids: list[str], *, role: str = "edit"
    ) -> tuple[Group, list[str]]:
        """Batched member-add. Returns ``(group, newly_added_ids)`` so
        the caller can emit per-member events without a second fetch.

        ``role == "edit"`` clears any explicit role entry; "admin" /
        "view" writes one. A single ``save()`` covers all changes.
        """
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)

        newly_added: list[str] = []
        for mid in member_ids:
            if mid not in doc.members:
                doc.members.append(mid)
                newly_added.append(mid)
            if role in ("admin", "view"):
                doc.member_roles[mid] = role  # type: ignore[assignment]
            elif role == "edit" and mid in doc.member_roles:
                doc.member_roles.pop(mid, None)

        if newly_added or role in ("admin", "view"):
            await doc.save()
        return _group_to_domain(doc), newly_added

    async def remove_member(self, group_id: str, user_id: str) -> Group:
        """Remove user_id from the group's members list (idempotent).
        Also clears any member_roles entry for the user."""
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)
        changed = False
        if user_id in doc.members:
            doc.members.remove(user_id)
            changed = True
        if user_id in doc.member_roles:
            del doc.member_roles[user_id]
            changed = True
        if changed:
            await doc.save()
        return _group_to_domain(doc)

    async def set_member_role(self, group_id: str, user_id: str, role: str) -> Group:
        """Set member_roles[user_id] = role. ``role == "edit"`` clears
        the entry (back-compat default). Caller is responsible for
        verifying the user is a member and the role is valid."""
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)
        if role == "edit":
            doc.member_roles.pop(user_id, None)
        else:
            doc.member_roles[user_id] = role  # type: ignore[assignment]
        await doc.save()
        return _group_to_domain(doc)

    async def add_group_agent(
        self, group_id: str, agent_id: str, *, role: str, respond_mode: str
    ) -> Group:
        """Append a new GroupAgent. Caller is responsible for ensuring
        the agent isn't already in the group — this method appends
        unconditionally."""
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)
        doc.agents.append(_GroupAgentDoc(agent=agent_id, role=role, respond_mode=respond_mode))
        await doc.save()
        return _group_to_domain(doc)

    async def update_group_agent_respond_mode(
        self, group_id: str, agent_id: str, respond_mode: str
    ) -> Group | None:
        """Update an agent's respond_mode. Returns ``None`` if the agent
        wasn't found in the group (caller raises NotFound)."""
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)
        for agent in doc.agents:
            if agent.agent == agent_id:
                agent.respond_mode = respond_mode  # type: ignore[assignment]
                await doc.save()
                return _group_to_domain(doc)
        return None

    async def remove_group_agent(self, group_id: str, agent_id: str) -> Group | None:
        """Remove a group agent. Returns ``None`` if the agent wasn't
        found in the group (caller raises NotFound)."""
        from ee.cloud._core.errors import NotFound

        doc = await _GroupDoc.get(PydanticObjectId(group_id))
        if doc is None:
            raise NotFound("group", group_id)
        before = len(doc.agents)
        doc.agents = [a for a in doc.agents if a.agent != agent_id]
        if len(doc.agents) == before:
            return None
        await doc.save()
        return _group_to_domain(doc)


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
