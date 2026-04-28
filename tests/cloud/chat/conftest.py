"""Fixtures for cloud chat tests.

Uses ``mongomock-motor`` so the suite runs in CI without a real MongoDB
service. Each test gets an isolated in-memory database via a uniquely-named
mock client.

Also provides ``FakeMessageRepo`` / ``FakeGroupRepo`` and the
``chat_repos`` fixture for emit-tests that previously patched the
Beanie ctor seam — Phase 10's mutation migrations made those patches
dead, so emit assertions now run against in-memory fake repositories.
The autouse ``_reset_repo_singletons`` fixture in
``tests/cloud/conftest.py`` restores the real singletons after each
test.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from ee.cloud.chat.domain import Group, GroupAgent, Message
from ee.cloud.chat.repositories import (
    set_group_repository,
    set_message_repository,
)

# ---------------------------------------------------------------------------
# Domain object builders
# ---------------------------------------------------------------------------


def make_domain_message(**overrides: Any) -> Message:
    """Build a chat domain ``Message`` with sensible defaults."""
    base: dict[str, Any] = {
        "id": "m1",
        "context_type": "group",
        "workspace_id": "w1",
        "group": "g1",
        "sender": "u1",
        "sender_type": "user",
        "agent": None,
        "content": "hi",
        "mentions": (),
        "reply_to": None,
        "thread_count": 0,
        "attachments": (),
        "reactions": (),
        "edited": False,
        "edited_at": None,
        "deleted": False,
        "session_key": None,
        "role": None,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Message(**base)


def make_domain_group(**overrides: Any) -> Group:
    """Build a chat domain ``Group`` with sensible defaults.

    Accepts list/dict inputs for tuple-typed fields (members, agents,
    pinned_messages, member_roles) and coerces them.
    """
    base: dict[str, Any] = {
        "id": "g1",
        "workspace_id": "w1",
        "name": "G",
        "slug": "g",
        "description": "",
        "icon": "",
        "color": "",
        "type": "private",
        "members": ("u1",),
        "member_roles": (),
        "agents": (),
        "pinned_messages": (),
        "owner": "u1",
        "archived": False,
        "last_message_at": None,
        "message_count": 0,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    if isinstance(base["members"], list):
        base["members"] = tuple(base["members"])
    if isinstance(base["agents"], list):
        base["agents"] = tuple(base["agents"])
    if isinstance(base["pinned_messages"], list):
        base["pinned_messages"] = tuple(base["pinned_messages"])
    if isinstance(base["member_roles"], dict):
        base["member_roles"] = tuple(base["member_roles"].items())
    return Group(**base)


# ---------------------------------------------------------------------------
# Fake repositories
# ---------------------------------------------------------------------------


class FakeMessageRepo:
    """In-memory ``IMessageRepository`` that records mutations.

    Tests prime ``self._messages`` (via ``add(...)`` ), set ``next_id`` /
    ``toggle_added`` to control mutation outputs, then assert on the
    call logs (``created`` / ``edited`` / ``deleted`` / ``reactions``)
    and on the events captured from the patched ``emit``.
    """

    def __init__(self) -> None:
        self._messages: dict[str, Message] = {}
        self.created: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[str] = []
        self.reactions: list[dict] = []
        self.next_id: str = "m_new"
        self.toggle_added: bool = True

    def add(self, msg: Message) -> Message:
        self._messages[msg.id] = msg
        return msg

    async def get(self, message_id: str) -> Message | None:
        return self._messages.get(message_id)

    async def get_many(self, message_ids: list[str]) -> list[Message]:
        return [self._messages[i] for i in message_ids if i in self._messages]

    async def list_for_group(self, group_id: str, **_: Any) -> list[Message]:
        return [m for m in self._messages.values() if m.group == group_id]

    async def list_for_group_paged(self, group_id: str, **_: Any) -> list[Message]:
        return [m for m in self._messages.values() if m.group == group_id]

    async def list_for_session(self, session_key: str, **_: Any) -> list[Message]:
        return [m for m in self._messages.values() if m.session_key == session_key]

    async def list_replies(self, parent_message_id: str) -> list[Message]:
        return [m for m in self._messages.values() if m.reply_to == parent_message_id]

    async def search_in_group(self, group_id: str, query: str, **_: Any) -> list[Message]:
        return [
            m
            for m in self._messages.values()
            if m.group == group_id and query.lower() in (m.content or "").lower()
        ]

    async def search_in_groups(
        self, group_ids: list[str], query: str, **_: Any
    ) -> list[Message]:
        gset = set(group_ids)
        return [
            m
            for m in self._messages.values()
            if m.group in gset and query.lower() in (m.content or "").lower()
        ]

    async def create_group_message(
        self,
        *,
        group_id: str,
        sender: str | None,
        sender_type: str,
        content: str,
        agent: str | None = None,
        mentions: list[dict] | None = None,
        attachments: list[dict] | None = None,
        reply_to: str | None = None,
    ) -> Message:
        self.created.append(
            {
                "group_id": group_id,
                "sender": sender,
                "sender_type": sender_type,
                "content": content,
                "agent": agent,
                "mentions": mentions,
                "attachments": attachments,
                "reply_to": reply_to,
            }
        )
        msg = make_domain_message(
            id=self.next_id,
            group=group_id,
            sender=sender,
            sender_type=sender_type,
            agent=agent,
            content=content,
            reply_to=reply_to,
        )
        self._messages[msg.id] = msg
        return msg

    async def edit_content(
        self, message_id: str, content: str, *, edited_at: datetime
    ) -> Message:
        self.edited.append(
            {"message_id": message_id, "content": content, "edited_at": edited_at}
        )
        existing = self._messages.get(message_id) or make_domain_message(id=message_id)
        updated = replace(existing, content=content, edited=True, edited_at=edited_at)
        self._messages[message_id] = updated
        return updated

    async def soft_delete(self, message_id: str) -> Message:
        self.deleted.append(message_id)
        existing = self._messages.get(message_id) or make_domain_message(id=message_id)
        updated = replace(existing, deleted=True)
        self._messages[message_id] = updated
        return updated

    async def toggle_reaction(
        self, message_id: str, user_id: str, emoji: str
    ) -> tuple[Message, bool]:
        self.reactions.append(
            {"message_id": message_id, "user_id": user_id, "emoji": emoji}
        )
        existing = self._messages.get(message_id) or make_domain_message(id=message_id)
        return existing, self.toggle_added


class FakeGroupRepo:
    """In-memory ``IGroupRepository`` that records mutations."""

    def __init__(self) -> None:
        self._groups: dict[str, Group] = {}
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.member_added: list[dict] = []
        self.members_added: list[dict] = []
        self.member_removed: list[dict] = []
        self.member_role_set: list[dict] = []
        self.agent_added: list[dict] = []
        self.agent_updated: list[dict] = []
        self.agent_removed: list[dict] = []
        self.pin_calls: list[dict] = []
        self.unpin_calls: list[dict] = []
        self.bumps: list[dict] = []
        self.next_id: str = "g_new"

    def add(self, group: Group) -> Group:
        self._groups[group.id] = group
        return group

    async def get(self, group_id: str) -> Group | None:
        return self._groups.get(group_id)

    async def get_by_slug(self, workspace_id: str, slug: str) -> Group | None:
        for g in self._groups.values():
            if g.workspace_id == workspace_id and g.slug == slug:
                return g
        return None

    async def list_for_workspace(self, workspace_id: str, **_: Any) -> list[Group]:
        return [g for g in self._groups.values() if g.workspace_id == workspace_id]

    async def list_for_user(self, workspace_id: str, user_id: str) -> list[Group]:
        return [
            g
            for g in self._groups.values()
            if g.workspace_id == workspace_id and user_id in g.members
        ]

    async def list_visible_in_workspace(
        self, workspace_id: str, user_id: str
    ) -> list[Group]:
        return [
            g
            for g in self._groups.values()
            if g.workspace_id == workspace_id
            and not g.archived
            and (g.type in ("public", "channel") or user_id in g.members)
        ]

    async def find_dm_between_users(
        self, workspace_id: str, members: list[str]
    ) -> Group | None:
        target = sorted(members)
        for g in self._groups.values():
            if (
                g.workspace_id == workspace_id
                and g.type == "dm"
                and sorted(g.members) == target
            ):
                return g
        return None

    async def find_user_agent_dm(
        self, workspace_id: str, user_id: str, agent_id: str
    ) -> Group | None:
        for g in self._groups.values():
            if (
                g.workspace_id == workspace_id
                and g.type == "dm"
                and list(g.members) == [user_id]
                and any(a.agent_id == agent_id for a in g.agents)
            ):
                return g
        return None

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
        self.created.append(
            {
                "workspace_id": workspace_id,
                "name": name,
                "slug": slug,
                "owner": owner,
                "type": type,
                "members": list(members),
                "agents": list(agents) if agents else [],
            }
        )
        domain_agents = tuple(
            GroupAgent(agent_id=a, role=r, respond_mode=rm) for (a, r, rm) in (agents or [])
        )
        new = make_domain_group(
            id=self.next_id,
            workspace_id=workspace_id,
            name=name,
            slug=slug,
            description=description,
            icon=icon,
            color=color,
            owner=owner,
            type=type,
            members=tuple(members),
            agents=domain_agents,
        )
        self._groups[new.id] = new
        return new

    async def update_fields(self, group_id: str, **fields: Any) -> Group:
        self.updated.append({"group_id": group_id, **fields})
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        change = {k: v for k, v in fields.items() if v is not None}
        updated = replace(existing, **change) if change else existing
        self._groups[group_id] = updated
        return updated

    async def add_member(
        self, group_id: str, user_id: str, *, role: str | None = None
    ) -> Group:
        self.member_added.append(
            {"group_id": group_id, "user_id": user_id, "role": role}
        )
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        members = (
            existing.members
            if user_id in existing.members
            else (*existing.members, user_id)
        )
        roles = dict(existing.member_roles)
        if role is not None:
            roles[user_id] = role
        updated = replace(existing, members=members, member_roles=tuple(roles.items()))
        self._groups[group_id] = updated
        return updated

    async def add_members(
        self, group_id: str, member_ids: list[str], *, role: str = "edit"
    ) -> tuple[Group, list[str]]:
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        new_ids = [m for m in member_ids if m not in existing.members]
        self.members_added.append(
            {"group_id": group_id, "member_ids": list(member_ids), "role": role}
        )
        members = (*existing.members, *new_ids)
        roles = dict(existing.member_roles)
        for mid in member_ids:
            if role in ("admin", "view"):
                roles[mid] = role
            elif role == "edit" and mid in roles:
                del roles[mid]
        updated = replace(existing, members=members, member_roles=tuple(roles.items()))
        self._groups[group_id] = updated
        return updated, new_ids

    async def remove_member(self, group_id: str, user_id: str) -> Group:
        self.member_removed.append({"group_id": group_id, "user_id": user_id})
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        members = tuple(m for m in existing.members if m != user_id)
        roles = {k: v for k, v in existing.member_roles if k != user_id}
        updated = replace(existing, members=members, member_roles=tuple(roles.items()))
        self._groups[group_id] = updated
        return updated

    async def set_member_role(self, group_id: str, user_id: str, role: str) -> Group:
        self.member_role_set.append(
            {"group_id": group_id, "user_id": user_id, "role": role}
        )
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        roles = dict(existing.member_roles)
        if role == "edit":
            roles.pop(user_id, None)
        else:
            roles[user_id] = role
        updated = replace(existing, member_roles=tuple(roles.items()))
        self._groups[group_id] = updated
        return updated

    async def add_group_agent(
        self, group_id: str, agent_id: str, *, role: str, respond_mode: str
    ) -> Group:
        self.agent_added.append(
            {
                "group_id": group_id,
                "agent_id": agent_id,
                "role": role,
                "respond_mode": respond_mode,
            }
        )
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        agents = (
            *existing.agents,
            GroupAgent(agent_id=agent_id, role=role, respond_mode=respond_mode),
        )
        updated = replace(existing, agents=agents)
        self._groups[group_id] = updated
        return updated

    async def update_group_agent_respond_mode(
        self, group_id: str, agent_id: str, respond_mode: str
    ) -> Group | None:
        self.agent_updated.append(
            {"group_id": group_id, "agent_id": agent_id, "respond_mode": respond_mode}
        )
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        if not any(a.agent_id == agent_id for a in existing.agents):
            return None
        new_agents = tuple(
            replace(a, respond_mode=respond_mode) if a.agent_id == agent_id else a
            for a in existing.agents
        )
        updated = replace(existing, agents=new_agents)
        self._groups[group_id] = updated
        return updated

    async def remove_group_agent(self, group_id: str, agent_id: str) -> Group | None:
        self.agent_removed.append({"group_id": group_id, "agent_id": agent_id})
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        if not any(a.agent_id == agent_id for a in existing.agents):
            return None
        new_agents = tuple(a for a in existing.agents if a.agent_id != agent_id)
        updated = replace(existing, agents=new_agents)
        self._groups[group_id] = updated
        return updated

    async def pin_message(self, group_id: str, message_id: str) -> Group:
        self.pin_calls.append({"group_id": group_id, "message_id": message_id})
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        if message_id in existing.pinned_messages:
            return existing
        updated = replace(
            existing, pinned_messages=(*existing.pinned_messages, message_id)
        )
        self._groups[group_id] = updated
        return updated

    async def unpin_message(self, group_id: str, message_id: str) -> Group | None:
        self.unpin_calls.append({"group_id": group_id, "message_id": message_id})
        existing = self._groups.get(group_id) or make_domain_group(id=group_id)
        if message_id not in existing.pinned_messages:
            return None
        updated = replace(
            existing,
            pinned_messages=tuple(
                p for p in existing.pinned_messages if p != message_id
            ),
        )
        self._groups[group_id] = updated
        return updated

    async def bump_message_stats(
        self, group_id: str, *, last_message_at: datetime
    ) -> None:
        self.bumps.append({"group_id": group_id, "last_message_at": last_message_at})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chat_repos() -> tuple[FakeMessageRepo, FakeGroupRepo]:
    """Install fresh fake message + group repositories for the test.

    Restored automatically by ``_reset_repo_singletons`` (autouse in
    ``tests/cloud/conftest.py``).
    """
    msg_repo = FakeMessageRepo()
    grp_repo = FakeGroupRepo()
    set_message_repository(msg_repo)
    set_group_repository(grp_repo)
    return msg_repo, grp_repo


@pytest.fixture()
async def beanie_memory_db():
    """Initialize Beanie against an in-memory mongomock-motor database.

    Beanie >=1.26 calls ``database.list_collection_names(authorizedCollections=True,
    nameOnly=True)``; mongomock-motor's stub doesn't accept those kwargs.
    We wrap the method to drop unknown kwargs so the suite runs in CI
    without a real MongoDB service.
    """
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    from ee.cloud.memory.documents import MemoryFactDoc
    from ee.cloud.models import ALL_DOCUMENTS

    db_name = f"test_chat_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args, **_kwargs):
        # mongomock-motor doesn't honour authorizedCollections / nameOnly;
        # the no-arg call returns the same list we need for Beanie init.
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield db
