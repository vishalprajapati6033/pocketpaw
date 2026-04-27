# Refactored: Split from service.py — contains GroupService class and group-related
# helper functions. N+1 query in _group_response() fixed with batch loading for
# both members (User) and agents (AgentModel).

"""Chat domain — group business logic (CRUD, membership, agents, DMs)."""

from __future__ import annotations

import logging
import re
from typing import Literal

from beanie import PydanticObjectId

from ee.cloud.chat.schemas import (
    AddGroupAgentRequest,
    CreateGroupRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
)
from ee.cloud.models.group import Group, MemberRole
from ee.cloud.realtime.bus import get_resolver
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    GroupAgentAdded,
    GroupAgentRemoved,
    GroupAgentUpdated,
    GroupCreated,
    GroupJoined,
    GroupMemberAdded,
    GroupMemberRemoved,
    GroupMemberRole,
    GroupUpdated,
)
from ee.cloud.shared.errors import Forbidden, NotFound, ValidationError
from ee.cloud.shared.time import iso_utc
from pocketpaw.ee.guards.actions import GroupRole
from pocketpaw.ee.guards.audit import log_denial

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_slug(name: str) -> str:
    """Lowercase, replace spaces/underscores with hyphens, strip non-alnum."""
    slug = name.lower().strip()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


async def _group_response(group: Group) -> dict:
    """Convert a Group document to a frontend-compatible dict.

    Populates member IDs -> {_id, name, email} and agent IDs ->
    {_id, agent, name, role, respond_mode}.
    Uses batch queries to avoid N+1 per-member / per-agent lookups.
    """
    from ee.cloud.models.agent import Agent as AgentModel
    from ee.cloud.models.user import User

    # Batch load members
    member_ids = [PydanticObjectId(uid) for uid in group.members]
    users = await User.find({"_id": {"$in": member_ids}}).to_list() if member_ids else []
    user_map = {str(u.id): u for u in users}

    populated_members = []
    for uid in group.members:
        user = user_map.get(uid)
        if user:
            populated_members.append(
                {
                    "_id": str(user.id),
                    "name": user.full_name or user.email,
                    "email": user.email,
                    "avatar": user.avatar,
                }
            )
        else:
            populated_members.append({"_id": uid, "name": uid, "email": ""})

    # Batch load agents
    agent_ids = [PydanticObjectId(ga.agent) for ga in group.agents]
    agents = await AgentModel.find({"_id": {"$in": agent_ids}}).to_list() if agent_ids else []
    agent_map = {str(a.id): a for a in agents}

    populated_agents = []
    for ga in group.agents:
        agent_doc = agent_map.get(ga.agent)
        populated_agents.append(
            {
                "_id": str(agent_doc.id) if agent_doc else ga.agent,
                "agent": ga.agent,
                "name": agent_doc.name if agent_doc else "Agent",
                "uname": agent_doc.slug if agent_doc else "",
                "avatar": agent_doc.avatar if agent_doc else "",
                "role": ga.role,
                "respond_mode": ga.respond_mode,
            }
        )

    return {
        "_id": str(group.id),
        "workspace": group.workspace,
        "name": group.name,
        "slug": group.slug,
        "description": group.description,
        "type": group.type,
        "icon": group.icon,
        "color": group.color,
        "owner": group.owner,
        "members": populated_members,
        "memberRoles": dict(group.member_roles),
        "agents": populated_agents,
        "pinnedMessages": group.pinned_messages,
        "archived": group.archived,
        "lastMessageAt": iso_utc(group.last_message_at),
        "messageCount": group.message_count,
        "createdAt": iso_utc(group.createdAt),
    }


def _require_group_member(group: Group, user_id: str) -> None:
    """Raise Forbidden if user is not a member of the group."""
    if user_id not in group.members:
        log_denial(
            actor=user_id,
            action="group.view",
            code="group.not_member",
            resource_id=str(group.id),
        )
        raise Forbidden("group.not_member", "You are not a member of this group")


def _require_group_admin(group: Group, user_id: str) -> None:
    """Raise Forbidden if user is not a group admin or owner.

    Admin tier is derived from ``group.member_roles[user_id] == "admin"``.
    The group owner is always an implicit admin.
    """
    if group.owner == user_id:
        return
    if group.member_roles.get(user_id) == "admin":
        return
    log_denial(
        actor=user_id,
        action="group.admin",
        code="group.not_admin",
        resource_id=str(group.id),
    )
    raise Forbidden("group.not_admin", "Only group admins can perform this action")


def _role_for(group: Group, user_id: str) -> Literal["owner", "admin", "edit", "view", "none"]:
    """Return the role of a user in a group.

    - "owner" if user_id == group.owner
    - member_roles[user_id] if present ("admin" | "edit" | "view")
    - "edit" if user is a member without an explicit role entry (back-compat default)
    - "none" if user is not a member
    """
    if group.owner == user_id:
        return "owner"
    if user_id not in group.members:
        return "none"
    explicit = group.member_roles.get(user_id)
    if explicit in ("admin", "edit", "view"):
        return explicit  # type: ignore[return-value]
    return "edit"


def resolve_group_role(group: Group, user_id: str) -> GroupRole:
    """Structured role resolution for the canonical guards matrix.

    Raises Forbidden ``group.not_member`` if the user has no membership.
    """
    raw = _role_for(group, user_id)
    if raw == "none":
        raise Forbidden("group.not_member", "You are not a member of this group")
    return GroupRole.from_str("edit" if raw == "edit" else raw)


def _require_can_post(group: Group, user_id: str) -> None:
    """Raise Forbidden if the user's role in the group cannot post / mutate."""
    role = _role_for(group, user_id)
    if role == "view":
        log_denial(
            actor=user_id,
            action="group.post",
            code="group.view_only",
            resource_id=str(group.id),
        )
        raise Forbidden("group.view_only", "You have read-only access in this group")
    if role == "none":
        log_denial(
            actor=user_id,
            action="group.post",
            code="group.not_member",
            resource_id=str(group.id),
        )
        raise Forbidden("group.not_member", "You are not a member of this group")


async def _get_group_or_404(group_id: str) -> Group:
    """Load a group by ID or raise NotFound."""
    group = await Group.get(PydanticObjectId(group_id))
    if not group:
        raise NotFound("group", group_id)
    return group


# ---------------------------------------------------------------------------
# Domain-Group helpers (used by methods routing through IGroupRepository)
# ---------------------------------------------------------------------------


def _require_domain_group_member(group, user_id: str) -> None:
    """Raise Forbidden if user is not a member of the (domain) group."""
    if user_id not in group.members:
        log_denial(
            actor=user_id,
            action="group.view",
            code="group.not_member",
            resource_id=group.id,
        )
        raise Forbidden("group.not_member", "You are not a member of this group")


def _require_domain_group_admin(group, user_id: str) -> None:
    """Raise Forbidden if user is not a group admin or owner.

    Operates on the domain ``Group`` value object. ``member_roles`` is
    a tuple of (user_id, role) pairs on the domain entity.
    """
    if group.owner == user_id:
        return
    if dict(group.member_roles).get(user_id) == "admin":
        return
    log_denial(
        actor=user_id,
        action="group.admin",
        code="group.not_admin",
        resource_id=group.id,
    )
    raise Forbidden("group.not_admin", "Only group admins can perform this action")


async def _populate_lookups_for_domain_groups(
    groups: list,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Batch-load user + agent details for the given domain groups.

    Returns ``(users_by_id, agents_by_id)`` ready for
    ``group_to_wire_dict``. Two Mongo queries regardless of group count.
    """
    from ee.cloud.models.agent import Agent as AgentModel
    from ee.cloud.models.user import User

    all_user_ids: set[str] = set()
    all_agent_ids: set[str] = set()
    for g in groups:
        all_user_ids.update(g.members)
        for ga in g.agents:
            all_agent_ids.add(ga.agent_id)

    users_by_id: dict[str, dict[str, str]] = {}
    if all_user_ids:
        user_oids = []
        for uid in all_user_ids:
            try:
                user_oids.append(PydanticObjectId(uid))
            except Exception:
                pass
        user_docs = await User.find({"_id": {"$in": user_oids}}).to_list() if user_oids else []
        for u in user_docs:
            users_by_id[str(u.id)] = {
                "_id": str(u.id),
                "name": u.full_name or u.email,
                "email": u.email,
                "avatar": u.avatar,
            }

    agents_by_id: dict[str, dict[str, str]] = {}
    if all_agent_ids:
        agent_oids = []
        for aid in all_agent_ids:
            try:
                agent_oids.append(PydanticObjectId(aid))
            except Exception:
                pass
        agent_docs = (
            await AgentModel.find({"_id": {"$in": agent_oids}}).to_list() if agent_oids else []
        )
        for a in agent_docs:
            agents_by_id[str(a.id)] = {
                "_id": str(a.id),
                "name": a.name,
                "uname": a.slug,
                "avatar": a.avatar,
            }

    return users_by_id, agents_by_id


# ---------------------------------------------------------------------------
# GroupService
# ---------------------------------------------------------------------------


class GroupService:
    """Stateless service for group/channel business logic."""

    @staticmethod
    async def create_group(workspace_id: str, user_id: str, body: CreateGroupRequest) -> dict:
        """Create a group and add the creator as a member.

        For DMs: validates exactly 2 member_ids, auto-names as "DM".

        Phase 10: routes through ``IGroupRepository.create``.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository

        if body.type == "dm":
            if len(body.member_ids) != 1:
                raise ValidationError(
                    "group.dm_requires_one_target",
                    "DM groups require exactly one target member_id (the other party)",
                )
            members = sorted({user_id, body.member_ids[0]})
            name = "DM"
        else:
            members = list({user_id, *body.member_ids})
            name = body.name

        group = await get_group_repository().create(
            workspace_id=workspace_id,
            name=name,
            slug=_generate_slug(name),
            owner=user_id,
            type=body.type,
            members=members,
            description=body.description,
            icon=body.icon,
            color=body.color,
        )
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
        resp = group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)
        await emit(GroupCreated(data={**resp, "member_ids": list(group.members)}))
        return resp

    @staticmethod
    async def list_groups(workspace_id: str, user_id: str) -> list[dict]:
        """List groups visible to the user.

        Returns public groups in the workspace plus private/dm groups
        where the user is a member.

        Phase 10: routes through ``IGroupRepository.list_visible_in_workspace``.
        Member/agent population was N+1 (one user/agent batch per group);
        new path batches across ALL groups in a single user-find and a
        single agent-find — O(1) Mongo round-trips for the populated
        list regardless of how many groups the user has.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository

        groups = await get_group_repository().list_visible_in_workspace(workspace_id, user_id)
        if not groups:
            return []

        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups(groups)
        return [
            group_to_wire_dict(g, users_by_id=users_by_id, agents_by_id=agents_by_id)
            for g in groups
        ]

    @staticmethod
    async def get_group(group_id: str, user_id: str) -> dict:
        """Get a single group. Private/DM groups require membership.

        Phase 10: routes through ``IGroupRepository.get`` and the
        domain → wire mapper.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository

        group = await get_group_repository().get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        if group.type in ("private", "dm"):
            _require_domain_group_member(group, user_id)
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
        return group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)

    @staticmethod
    async def update_group(group_id: str, user_id: str, body: UpdateGroupRequest) -> dict:
        """Update group fields. Owner only. Cannot update DMs.

        Phase 10: routes through ``IGroupRepository.update_fields``.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        if group.type == "dm":
            raise Forbidden("group.cannot_update_dm", "DM groups cannot be updated")
        _require_domain_group_admin(group, user_id)

        new_slug = _generate_slug(body.name) if body.name is not None else None
        new_type = body.type if (body.type is not None and body.type != group.type) else None
        updated = await repo.update_fields(
            group_id,
            name=body.name,
            slug=new_slug,
            description=body.description,
            type=new_type,
            icon=body.icon,
            color=body.color,
        )
        patched = body.model_dump(exclude_unset=True)
        await emit(GroupUpdated(data={"group_id": group_id, **patched}))
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([updated])
        return group_to_wire_dict(updated, users_by_id=users_by_id, agents_by_id=agents_by_id)

    @staticmethod
    async def archive_group(group_id: str, user_id: str) -> None:
        """Archive a group. Owner only.

        Phase 10: routes through ``IGroupRepository.update_fields``.
        """
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)
        await repo.update_fields(group_id, archived=True)
        await emit(GroupUpdated(data={"group_id": group_id, "archived": True}))

    @staticmethod
    async def join_group(group_id: str, user_id: str) -> None:
        """Join a public group. Adds user to members list.

        Phase 10: routes through ``IGroupRepository.add_member``.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        if group.type not in ("public", "channel"):
            raise Forbidden(
                "group.not_joinable",
                "Only public groups and channels can be joined directly",
            )
        if group.archived:
            raise Forbidden("group.archived", "Cannot join an archived group")

        if user_id in group.members:
            return

        updated = await repo.add_member(group_id, user_id)
        await emit(
            GroupMemberAdded(data={"group_id": group_id, "user_id": user_id, "role": "edit"})
        )
        # The joining user has no local record of this group yet — a
        # ``group.joined`` (audience = just them) hydrates the room in their
        # sidebar so they don't have to refresh to see it.
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([updated])
        resp = group_to_wire_dict(updated, users_by_id=users_by_id, agents_by_id=agents_by_id)
        await emit(GroupJoined(data={**resp, "member_ids": [user_id]}))
        get_resolver().invalidate_group(group_id)

    @staticmethod
    async def leave_group(group_id: str, user_id: str) -> None:
        """Leave a group. Owner cannot leave (must transfer ownership first).

        Phase 10: routes through ``IGroupRepository.remove_member``.
        """
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_member(group, user_id)
        if group.owner == user_id:
            raise Forbidden(
                "group.owner_cannot_leave",
                "The group owner cannot leave. Transfer ownership first.",
            )
        await repo.remove_member(group_id, user_id)
        await emit(GroupMemberRemoved(data={"group_id": group_id, "user_id": user_id}))
        get_resolver().invalidate_group(group_id)

    @staticmethod
    async def add_members(
        group_id: str,
        user_id: str,
        member_ids: list[str],
        role: MemberRole = "edit",
    ) -> list[str]:
        """Add members to a group with an initial role. Owner only.

        Returns the list of user IDs that were newly added (skipping duplicates).
        Role "edit" is the default (no role entry is written to keep the dict
        small); "view" / "admin" writes an explicit entry per added member.

        Phase 10: routes through ``IGroupRepository.add_members``.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)
        if group.archived:
            raise Forbidden("group.archived", "Cannot modify an archived group")

        updated, newly_added = await repo.add_members(group_id, member_ids, role=role)

        for added_user_id in newly_added:
            await emit(
                GroupMemberAdded(
                    data={"group_id": group_id, "user_id": added_user_id, "role": role}
                )
            )
        if newly_added:
            # Newly-added members have no local record of this group yet — a
            # ``group.joined`` (audience = just the new ids) hydrates the room
            # in their sidebars so they don't have to refresh to see it.
            users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([updated])
            resp = group_to_wire_dict(updated, users_by_id=users_by_id, agents_by_id=agents_by_id)
            await emit(GroupJoined(data={**resp, "member_ids": newly_added}))
            get_resolver().invalidate_group(group_id)

        return newly_added

    @staticmethod
    async def remove_member(group_id: str, user_id: str, target_user_id: str) -> None:
        """Remove a member from a group. Owner only. Cannot remove the owner.

        Phase 10: routes through ``IGroupRepository.remove_member``.
        """
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)
        if target_user_id == group.owner:
            raise Forbidden("group.cannot_remove_owner", "Cannot remove the group owner")
        if target_user_id not in group.members:
            raise NotFound("member", target_user_id)
        await repo.remove_member(group_id, target_user_id)
        await emit(GroupMemberRemoved(data={"group_id": group_id, "user_id": target_user_id}))
        get_resolver().invalidate_group(group_id)

    @staticmethod
    async def set_member_role(
        group_id: str, user_id: str, target_user_id: str, role: MemberRole
    ) -> MemberRole:
        """Set a member's role to "edit" / "view" / "admin". Owner only.

        Cannot change the owner's role. Raises NotFound if target is not a member.
        Returns the new role on success.

        Phase 10: routes through ``IGroupRepository.set_member_role``.
        """
        from ee.cloud.chat.repositories import get_group_repository

        if role not in ("admin", "edit", "view"):
            raise ValidationError(
                "group.invalid_role",
                f"Role must be one of 'admin', 'edit', 'view'; got {role!r}",
            )

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)
        if target_user_id == group.owner:
            raise Forbidden("group.cannot_change_owner_role", "Cannot change the owner's role")
        if target_user_id not in group.members:
            raise NotFound("member", target_user_id)

        await repo.set_member_role(group_id, target_user_id, role)
        await emit(
            GroupMemberRole(data={"group_id": group_id, "user_id": target_user_id, "role": role})
        )
        return role

    @staticmethod
    async def add_agent(group_id: str, user_id: str, body: AddGroupAgentRequest) -> None:
        """Add an agent to a group. Owner only.

        Phase 10: routes through ``IGroupRepository.add_group_agent``.
        """
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)

        for existing in group.agents:
            if existing.agent_id == body.agent_id:
                raise ValidationError(
                    "group.agent_already_added",
                    f"Agent '{body.agent_id}' is already in this group",
                )

        await repo.add_group_agent(
            group_id, body.agent_id, role=body.role, respond_mode=body.respond_mode
        )
        await emit(
            GroupAgentAdded(
                data={
                    "group_id": group_id,
                    "agent_id": body.agent_id,
                    "respond_mode": body.respond_mode,
                }
            )
        )

    @staticmethod
    async def update_agent(
        group_id: str, user_id: str, agent_id: str, body: UpdateGroupAgentRequest
    ) -> None:
        """Update an agent's respond_mode in a group. Owner only.

        Phase 10: routes through ``IGroupRepository.update_group_agent_respond_mode``.
        """
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)

        result = await repo.update_group_agent_respond_mode(group_id, agent_id, body.respond_mode)
        if result is None:
            raise NotFound("agent", agent_id)
        await emit(
            GroupAgentUpdated(
                data={
                    "group_id": group_id,
                    "agent_id": agent_id,
                    "respond_mode": body.respond_mode,
                }
            )
        )

    @staticmethod
    async def remove_agent(group_id: str, user_id: str, agent_id: str) -> None:
        """Remove an agent from a group. Owner only.

        Phase 10: routes through ``IGroupRepository.remove_group_agent``.
        """
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        group = await repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)

        result = await repo.remove_group_agent(group_id, agent_id)
        if result is None:
            raise NotFound("agent", agent_id)
        await emit(GroupAgentRemoved(data={"group_id": group_id, "agent_id": agent_id}))

    @staticmethod
    async def get_or_create_dm(workspace_id: str, user_id: str, target_user_id: str) -> dict:
        """Find an existing DM between two users, or create one.

        DM groups have type="dm", sorted members, and name="DM".

        Phase 10: routes through ``IGroupRepository.find_dm_between_users``
        + ``create``.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository

        repo = get_group_repository()
        members = sorted([user_id, target_user_id])

        existing = await repo.find_dm_between_users(workspace_id, members)
        if existing is not None:
            users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([existing])
            return group_to_wire_dict(existing, users_by_id=users_by_id, agents_by_id=agents_by_id)

        group = await repo.create(
            workspace_id=workspace_id,
            name="DM",
            slug=_generate_slug("dm"),
            owner=user_id,
            type="dm",
            members=members,
        )
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
        resp = group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)
        await emit(GroupCreated(data={**resp, "member_ids": list(group.members)}))
        return resp

    @staticmethod
    async def get_or_create_agent_dm(workspace_id: str, user_id: str, agent_id: str) -> dict:
        """Find or create a 1:1 DM between the user and an agent.

        Stored as a type="dm" group with ``members=[user_id]`` and a single
        ``GroupAgent`` (respond_mode="auto" so the agent replies by default).
        Verifies the user can see the agent (owner | workspace-visible | public).

        Phase 10: routes through ``IGroupRepository.find_user_agent_dm`` +
        ``create``. Agent visibility check still queries the AgentModel
        directly until the agents module gets an analogous repository.
        """
        from ee.cloud.chat.dto import group_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository
        from ee.cloud.models.agent import Agent as AgentModel

        try:
            agent_oid = PydanticObjectId(agent_id)
        except Exception as exc:  # noqa: BLE001 - surface as NotFound
            raise NotFound("agent", agent_id) from exc

        agent_doc = await AgentModel.get(agent_oid)
        if not agent_doc:
            raise NotFound("agent", agent_id)

        visible = (
            (agent_doc.workspace == workspace_id and agent_doc.owner == user_id)
            or (agent_doc.workspace == workspace_id and agent_doc.visibility == "workspace")
            or agent_doc.visibility == "public"
        )
        if not visible:
            raise NotFound("agent", agent_id)

        repo = get_group_repository()
        existing = await repo.find_user_agent_dm(workspace_id, user_id, agent_id)
        if existing is not None:
            users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([existing])
            return group_to_wire_dict(existing, users_by_id=users_by_id, agents_by_id=agents_by_id)

        group = await repo.create(
            workspace_id=workspace_id,
            name="DM",
            slug=_generate_slug("dm"),
            owner=user_id,
            type="dm",
            members=[user_id],
            agents=[(agent_id, "assistant", "auto")],
        )
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
        resp = group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)
        await emit(GroupCreated(data={**resp, "member_ids": list(group.members)}))
        return resp

    # ------------------------------------------------------------------
    # Realtime helpers (audience lookups)
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetch_group(group_id: str):
        """Wrapped for testability."""
        try:
            oid = PydanticObjectId(group_id)
        except Exception:
            return None
        return await Group.get(oid)

    @staticmethod
    async def list_member_ids(group_id: str) -> list[str]:
        """Return the user_ids that are members of the group. Empty if missing."""
        group = await GroupService._fetch_group(group_id)
        return list(group.members) if group else []
