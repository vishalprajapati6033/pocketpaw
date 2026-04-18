"""Tests that GroupService emits realtime events via the bus.

Each public GroupService mutation must fire the appropriate Event class
through ``emit()`` and, when membership shifts, invalidate the
AudienceResolver group cache. We patch the DB/permission layer at its
seams so we exercise emit behavior in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _capture_emits():
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    return recorded, fake_emit


def _fake_group(
    *,
    group_id: str = "g1",
    owner: str = "u1",
    members: list[str] | None = None,
    gtype: str = "private",
    member_roles: dict | None = None,
    agents: list | None = None,
    archived: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=group_id,
        workspace="w1",
        name="G",
        slug="g",
        description="",
        type=gtype,
        icon="",
        color="",
        owner=owner,
        members=list(members) if members is not None else [owner],
        member_roles=dict(member_roles) if member_roles is not None else {},
        agents=list(agents) if agents is not None else [],
        pinned_messages=[],
        archived=archived,
        last_message_at=None,
        message_count=0,
        createdAt=None,
        save=AsyncMock(),
    )


async def _fake_group_response(grp) -> dict:
    return {
        "_id": str(grp.id),
        "workspace": grp.workspace,
        "name": grp.name,
        "type": grp.type,
        "members": [{"_id": m} for m in grp.members],
    }


# ---------------------------------------------------------------------------
# create_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_group_emits_group_created():
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import CreateGroupRequest

    recorded, fake_emit = _capture_emits()

    constructed: list = []

    def fake_group_ctor(**kwargs):
        obj = _fake_group(
            group_id="g_new",
            owner=kwargs.get("owner", "u1"),
            members=kwargs.get("members", []),
            gtype=kwargs.get("type", "private"),
        )
        obj.insert = AsyncMock(return_value=obj)
        constructed.append(obj)
        return obj

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
        patch("ee.cloud.chat.group_service.Group", new=fake_group_ctor),
    ):
        await GroupService.create_group(
            "w1", "u1", CreateGroupRequest(name="Test", member_ids=["u2"])
        )

    created = [e for e in recorded if isinstance(e, GroupCreated)]
    assert len(created) == 1
    ev = created[0]
    assert set(ev.data.get("member_ids", [])) == {"u1", "u2"}


# ---------------------------------------------------------------------------
# update_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_group_emits_group_updated():
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import UpdateGroupRequest

    recorded, fake_emit = _capture_emits()
    group = _fake_group()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
    ):
        await GroupService.update_group("g1", "u1", UpdateGroupRequest(name="NewName"))

    events = [e for e in recorded if isinstance(e, GroupUpdated)]
    assert len(events) == 1
    assert events[0].data["group_id"] == "g1"
    assert events[0].data["name"] == "NewName"


# ---------------------------------------------------------------------------
# archive_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_group_emits_group_updated_archived():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
    ):
        await GroupService.archive_group("g1", "u1")

    events = [e for e in recorded if isinstance(e, GroupUpdated)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "archived": True}


# ---------------------------------------------------------------------------
# join_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_group_emits_member_added_and_invalidates_cache():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group(gtype="public", members=["u1"])
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        await GroupService.join_group("g1", "u2")

    events = [e for e in recorded if isinstance(e, GroupMemberAdded)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "user_id": "u2", "role": "edit"}
    # group.joined — audience scoped to the joining user so their sidebar
    # hydrates the room without a manual refresh.
    joined = [e for e in recorded if isinstance(e, GroupJoined)]
    assert len(joined) == 1
    assert joined[0].data["member_ids"] == ["u2"]
    assert joined[0].data["_id"] == "g1"
    resolver_mock.invalidate_group.assert_called_once_with("g1")


@pytest.mark.asyncio
async def test_join_group_no_emit_when_already_member():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group(gtype="public", members=["u1", "u2"])
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        await GroupService.join_group("g1", "u2")

    assert not [e for e in recorded if isinstance(e, GroupMemberAdded)]
    resolver_mock.invalidate_group.assert_not_called()


# ---------------------------------------------------------------------------
# leave_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leave_group_emits_member_removed_and_invalidates_cache():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="u1", members=["u1", "u2"])
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        await GroupService.leave_group("g1", "u2")

    events = [e for e in recorded if isinstance(e, GroupMemberRemoved)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "user_id": "u2"}
    resolver_mock.invalidate_group.assert_called_once_with("g1")


# ---------------------------------------------------------------------------
# add_members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_members_emits_one_event_per_newly_added_user():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="u1", members=["u1"], member_roles={"u1": "admin"})
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        added = await GroupService.add_members("g1", "u1", ["u2", "u3", "u4"])

    assert added == ["u2", "u3", "u4"]
    events = [e for e in recorded if isinstance(e, GroupMemberAdded)]
    assert len(events) == 3
    assert {e.data["user_id"] for e in events} == {"u2", "u3", "u4"}
    for ev in events:
        assert ev.data["group_id"] == "g1"
        assert ev.data["role"] == "edit"
    # One group.joined scoped to the newly-added user ids so their sidebars
    # hydrate the room without refreshing.
    joined = [e for e in recorded if isinstance(e, GroupJoined)]
    assert len(joined) == 1
    assert joined[0].data["member_ids"] == ["u2", "u3", "u4"]
    assert joined[0].data["_id"] == "g1"
    resolver_mock.invalidate_group.assert_called_once_with("g1")


@pytest.mark.asyncio
async def test_add_members_skips_duplicates_and_does_not_invalidate_when_none_added():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"})
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        added = await GroupService.add_members("g1", "u1", ["u2"])

    assert added == []
    assert not [e for e in recorded if isinstance(e, GroupMemberAdded)]
    resolver_mock.invalidate_group.assert_not_called()


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_member_emits_member_removed_and_invalidates_cache():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"})
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        await GroupService.remove_member("g1", "u1", "u2")

    events = [e for e in recorded if isinstance(e, GroupMemberRemoved)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "user_id": "u2"}
    resolver_mock.invalidate_group.assert_called_once_with("g1")


# ---------------------------------------------------------------------------
# set_member_role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_member_role_emits_member_role_no_invalidation():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"})
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        await GroupService.set_member_role("g1", "u1", "u2", "admin")

    events = [e for e in recorded if isinstance(e, GroupMemberRole)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "user_id": "u2", "role": "admin"}
    resolver_mock.invalidate_group.assert_not_called()


# ---------------------------------------------------------------------------
# add_agent / update_agent / remove_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_agent_emits_agent_added():
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import AddGroupAgentRequest

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="u1", agents=[])

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
    ):
        await GroupService.add_agent(
            "g1",
            "u1",
            AddGroupAgentRequest(agent_id="a1", respond_mode="auto"),
        )

    events = [e for e in recorded if isinstance(e, GroupAgentAdded)]
    assert len(events) == 1
    assert events[0].data == {
        "group_id": "g1",
        "agent_id": "a1",
        "respond_mode": "auto",
    }


@pytest.mark.asyncio
async def test_update_agent_emits_agent_updated():
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import UpdateGroupAgentRequest

    recorded, fake_emit = _capture_emits()
    existing_agent = SimpleNamespace(agent="a1", role="assistant", respond_mode="auto")
    group = _fake_group(owner="u1", agents=[existing_agent])

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
    ):
        await GroupService.update_agent(
            "g1", "u1", "a1", UpdateGroupAgentRequest(respond_mode="mention")
        )

    events = [e for e in recorded if isinstance(e, GroupAgentUpdated)]
    assert len(events) == 1
    assert events[0].data == {
        "group_id": "g1",
        "agent_id": "a1",
        "respond_mode": "mention",
    }


@pytest.mark.asyncio
async def test_remove_agent_emits_agent_removed():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    existing_agent = SimpleNamespace(agent="a1", role="assistant", respond_mode="auto")
    group = _fake_group(owner="u1", agents=[existing_agent])

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
    ):
        await GroupService.remove_agent("g1", "u1", "a1")

    events = [e for e in recorded if isinstance(e, GroupAgentRemoved)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "agent_id": "a1"}


# ---------------------------------------------------------------------------
# get_or_create_dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_dm_emits_when_created():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()

    def fake_group_ctor(**kwargs):
        obj = _fake_group(
            group_id="dm_new",
            owner=kwargs.get("owner", "u1"),
            members=kwargs.get("members", []),
            gtype="dm",
        )
        obj.insert = AsyncMock(return_value=obj)
        return obj

    fake_group_ctor.find_one = AsyncMock(return_value=None)

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service.Group", new=fake_group_ctor),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
    ):
        await GroupService.get_or_create_dm("w1", "u1", "u2")

    events = [e for e in recorded if isinstance(e, GroupCreated)]
    assert len(events) == 1
    assert set(events[0].data.get("member_ids", [])) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_get_or_create_dm_no_emit_when_existing():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()
    existing = _fake_group(gtype="dm", members=["u1", "u2"])

    fake_group_ctor = MagicMock()
    fake_group_ctor.find_one = AsyncMock(return_value=existing)

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service.Group", new=fake_group_ctor),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
    ):
        await GroupService.get_or_create_dm("w1", "u1", "u2")

    assert not [e for e in recorded if isinstance(e, GroupCreated)]


# ---------------------------------------------------------------------------
# get_or_create_agent_dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_agent_dm_emits_when_created():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()

    fake_agent_doc = SimpleNamespace(
        id="a1",
        workspace="w1",
        owner="u1",
        visibility="workspace",
    )

    def fake_group_ctor(**kwargs):
        obj = _fake_group(
            group_id="dm_new",
            owner=kwargs.get("owner", "u1"),
            members=kwargs.get("members", []),
            gtype="dm",
        )
        obj.insert = AsyncMock(return_value=obj)
        return obj

    fake_group_ctor.find_one = AsyncMock(return_value=None)

    fake_agent_cls = MagicMock()
    fake_agent_cls.get = AsyncMock(return_value=fake_agent_doc)

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service.PydanticObjectId", new=lambda x: x),
        patch("ee.cloud.models.agent.Agent", new=fake_agent_cls),
        patch("ee.cloud.chat.group_service.Group", new=fake_group_ctor),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
    ):
        await GroupService.get_or_create_agent_dm("w1", "u1", "a1")

    events = [e for e in recorded if isinstance(e, GroupCreated)]
    assert len(events) == 1
    assert events[0].data.get("member_ids") == ["u1"]


@pytest.mark.asyncio
async def test_get_or_create_agent_dm_no_emit_when_existing():
    from ee.cloud.chat.group_service import GroupService

    recorded, fake_emit = _capture_emits()

    fake_agent_doc = SimpleNamespace(
        id="a1",
        workspace="w1",
        owner="u1",
        visibility="workspace",
    )
    existing = _fake_group(gtype="dm", members=["u1"])

    fake_group_ctor = MagicMock()
    fake_group_ctor.find_one = AsyncMock(return_value=existing)

    fake_agent_cls = MagicMock()
    fake_agent_cls.get = AsyncMock(return_value=fake_agent_doc)

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service.PydanticObjectId", new=lambda x: x),
        patch("ee.cloud.models.agent.Agent", new=fake_agent_cls),
        patch("ee.cloud.chat.group_service.Group", new=fake_group_ctor),
        patch("ee.cloud.chat.group_service._group_response", new=_fake_group_response),
    ):
        await GroupService.get_or_create_agent_dm("w1", "u1", "a1")

    assert not [e for e in recorded if isinstance(e, GroupCreated)]
