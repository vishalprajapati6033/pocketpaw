"""Tests that GroupService emits realtime events via the bus.

Each public GroupService mutation must fire the appropriate Event class
through ``emit()`` and, when membership/admin shifts, invalidate the
AudienceResolver cache. We install fake group + message repositories
(Phase 10 routed mutations through ``IGroupRepository`` /
``IMessageRepository``) and stub ``_populate_lookups_for_domain_groups``
because it touches Beanie User / Agent docs that aren't initialised in
unit tests.
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


async def _empty_lookups(_groups):
    """Stub for ``_populate_lookups_for_domain_groups`` — bypasses
    Beanie queries on User/Agent that the unit-test environment can't
    serve."""
    return {}, {}


# ---------------------------------------------------------------------------
# create_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_group_emits_group_created(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import CreateGroupRequest

    _msg_repo, grp_repo = chat_repos
    recorded, fake_emit = _capture_emits()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
        ),
    ):
        await GroupService.create_group(
            "w1", "u1", CreateGroupRequest(name="Test", member_ids=["u2"])
        )

    # The repo recorded a create call with both members.
    assert len(grp_repo.created) == 1
    assert set(grp_repo.created[0]["members"]) == {"u1", "u2"}

    created = [e for e in recorded if isinstance(e, GroupCreated)]
    assert len(created) == 1
    assert set(created[0].data.get("member_ids", [])) == {"u1", "u2"}


# ---------------------------------------------------------------------------
# update_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_group_emits_group_updated(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import UpdateGroupRequest
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(make_domain_group(id="g1", owner="u1"))

    recorded, fake_emit = _capture_emits()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
        ),
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
async def test_archive_group_emits_group_updated_archived(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(make_domain_group(id="g1", owner="u1"))

    recorded, fake_emit = _capture_emits()

    with patch("ee.cloud.chat.group_service.emit", new=fake_emit):
        await GroupService.archive_group("g1", "u1")

    events = [e for e in recorded if isinstance(e, GroupUpdated)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "archived": True}


# ---------------------------------------------------------------------------
# join_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_group_emits_member_added_and_invalidates_cache(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(make_domain_group(id="g1", owner="u1", type="public", members=["u1"]))

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
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
async def test_join_group_no_emit_when_already_member(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(id="g1", owner="u1", type="public", members=["u1", "u2"])
    )

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        await GroupService.join_group("g1", "u2")

    assert not [e for e in recorded if isinstance(e, GroupMemberAdded)]
    resolver_mock.invalidate_group.assert_not_called()


# ---------------------------------------------------------------------------
# leave_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leave_group_emits_member_removed_and_invalidates_cache(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(make_domain_group(id="g1", owner="u1", members=["u1", "u2"]))

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
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
async def test_add_members_emits_one_event_per_newly_added_user(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(
            id="g1", owner="u1", members=["u1"], member_roles={"u1": "admin"}
        )
    )

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
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
async def test_add_members_skips_duplicates_and_does_not_invalidate_when_none_added(
    chat_repos,
):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(
            id="g1", owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"}
        )
    )

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
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
async def test_remove_member_emits_member_removed_and_invalidates_cache(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(
            id="g1", owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"}
        )
    )

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
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
async def test_set_member_role_emits_member_role_no_invalidation(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(
            id="g1", owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"}
        )
    )

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
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
async def test_add_agent_emits_agent_added(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import AddGroupAgentRequest
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(make_domain_group(id="g1", owner="u1", agents=[]))

    recorded, fake_emit = _capture_emits()

    with patch("ee.cloud.chat.group_service.emit", new=fake_emit):
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
async def test_update_agent_emits_agent_updated(chat_repos):
    from ee.cloud.chat.domain import GroupAgent
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import UpdateGroupAgentRequest
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(
            id="g1",
            owner="u1",
            agents=[GroupAgent(agent_id="a1", role="assistant", respond_mode="auto")],
        )
    )

    recorded, fake_emit = _capture_emits()

    with patch("ee.cloud.chat.group_service.emit", new=fake_emit):
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
async def test_remove_agent_emits_agent_removed(chat_repos):
    from ee.cloud.chat.domain import GroupAgent
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(
            id="g1",
            owner="u1",
            agents=[GroupAgent(agent_id="a1", role="assistant", respond_mode="auto")],
        )
    )

    recorded, fake_emit = _capture_emits()

    with patch("ee.cloud.chat.group_service.emit", new=fake_emit):
        await GroupService.remove_agent("g1", "u1", "a1")

    events = [e for e in recorded if isinstance(e, GroupAgentRemoved)]
    assert len(events) == 1
    assert events[0].data == {"group_id": "g1", "agent_id": "a1"}


# ---------------------------------------------------------------------------
# get_or_create_dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_dm_emits_when_created(chat_repos):
    from ee.cloud.chat.group_service import GroupService

    _msg_repo, _grp_repo = chat_repos  # empty repo → will create

    recorded, fake_emit = _capture_emits()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
        ),
    ):
        await GroupService.get_or_create_dm("w1", "u1", "u2")

    events = [e for e in recorded if isinstance(e, GroupCreated)]
    assert len(events) == 1
    assert set(events[0].data.get("member_ids", [])) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_get_or_create_dm_no_emit_when_existing(chat_repos):
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(id="dm1", workspace_id="w1", type="dm", members=["u1", "u2"])
    )

    recorded, fake_emit = _capture_emits()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
        ),
    ):
        await GroupService.get_or_create_dm("w1", "u1", "u2")

    assert not [e for e in recorded if isinstance(e, GroupCreated)]


# ---------------------------------------------------------------------------
# get_or_create_agent_dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_agent_dm_emits_when_created(chat_repos):
    from ee.cloud.chat.group_service import GroupService

    _msg_repo, _grp_repo = chat_repos  # empty → will create

    recorded, fake_emit = _capture_emits()

    fake_agent_doc = SimpleNamespace(
        id="a1",
        workspace="w1",
        owner="u1",
        visibility="workspace",
    )
    fake_agent_cls = MagicMock()
    fake_agent_cls.get = AsyncMock(return_value=fake_agent_doc)

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service.PydanticObjectId", new=lambda x: x),
        patch("ee.cloud.models.agent.Agent", new=fake_agent_cls),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
        ),
    ):
        await GroupService.get_or_create_agent_dm("w1", "u1", "a1")

    events = [e for e in recorded if isinstance(e, GroupCreated)]
    assert len(events) == 1
    assert events[0].data.get("member_ids") == ["u1"]


@pytest.mark.asyncio
async def test_get_or_create_agent_dm_no_emit_when_existing(chat_repos):
    from ee.cloud.chat.domain import GroupAgent
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(
        make_domain_group(
            id="dm_a1",
            workspace_id="w1",
            type="dm",
            members=["u1"],
            agents=[GroupAgent(agent_id="a1", role="assistant", respond_mode="auto")],
        )
    )

    recorded, fake_emit = _capture_emits()

    fake_agent_doc = SimpleNamespace(
        id="a1",
        workspace="w1",
        owner="u1",
        visibility="workspace",
    )
    fake_agent_cls = MagicMock()
    fake_agent_cls.get = AsyncMock(return_value=fake_agent_doc)

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch("ee.cloud.chat.group_service.PydanticObjectId", new=lambda x: x),
        patch("ee.cloud.models.agent.Agent", new=fake_agent_cls),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
        ),
    ):
        await GroupService.get_or_create_agent_dm("w1", "u1", "a1")

    assert not [e for e in recorded if isinstance(e, GroupCreated)]


@pytest.mark.asyncio
async def test_join_group_allows_channel_type(chat_repos):
    """Channels should be self-joinable just like public groups."""
    from ee.cloud.chat.group_service import GroupService
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(make_domain_group(id="g1", owner="u1", type="channel", members=["u1"]))

    recorded, fake_emit = _capture_emits()
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.chat.group_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.group_service._populate_lookups_for_domain_groups",
            new=_empty_lookups,
        ),
        patch("ee.cloud.chat.group_service.get_resolver", lambda: resolver_mock),
    ):
        await GroupService.join_group("g1", "u2")

    # Repo should have recorded the add_member call.
    assert grp_repo.member_added == [{"group_id": "g1", "user_id": "u2", "role": None}]
    events = [e for e in recorded if isinstance(e, GroupMemberAdded)]
    assert len(events) == 1
    resolver_mock.invalidate_group.assert_called_once_with("g1")


@pytest.mark.asyncio
async def test_join_group_still_rejects_private(chat_repos):
    """Private groups must remain invite-only."""
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.shared.errors import Forbidden
    from tests.cloud.chat.conftest import make_domain_group

    _msg_repo, grp_repo = chat_repos
    grp_repo.add(make_domain_group(id="g1", owner="u1", type="private", members=["u1"]))

    with pytest.raises(Forbidden):
        await GroupService.join_group("g1", "u2")
