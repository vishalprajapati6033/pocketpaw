"""Derivation tests: mention / reaction / invite → notifications_service.create.

Each emit site is exercised through its owning service; the spy on
``notifications_service.create`` proves the derivation fires with
the right arguments (and does NOT fire for self-targeted events).

Phase 10 routed chat mutations through ``IMessageRepository`` /
``IGroupRepository``; these tests install in-memory fakes from
``tests.cloud.chat.conftest`` instead of patching the Beanie ctor seam.
The autouse ``_reset_repo_singletons`` fixture restores the real
singletons after every test.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ee.cloud.chat.repositories import (
    set_group_repository,
    set_message_repository,
)
from tests.cloud.chat.conftest import (
    FakeGroupRepo,
    FakeMessageRepo,
    make_domain_message,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_group(
    *,
    group_id: str = "g1",
    owner: str = "u1",
    workspace: str = "w1",
    name: str = "general",
) -> SimpleNamespace:
    """Beanie-shape stand-in for the legacy ``_get_group_or_404`` seam.

    The membership / can-post check still loads the Beanie ``Group`` doc
    via the legacy helper because mutation paths share it with
    non-migrated reads.
    """
    return SimpleNamespace(
        id=group_id,
        workspace=workspace,
        owner=owner,
        name=name,
        members=[owner],
        member_roles={owner: "admin"},
        archived=False,
        type="group",
        last_message_at=None,
        message_count=0,
        save=AsyncMock(),
    )


def _install_chat_fakes() -> tuple[FakeMessageRepo, FakeGroupRepo]:
    msg_repo = FakeMessageRepo()
    grp_repo = FakeGroupRepo()
    set_message_repository(msg_repo)
    set_group_repository(grp_repo)
    return msg_repo, grp_repo


# ---------------------------------------------------------------------------
# Mention derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_with_mention_creates_notification_for_target():
    from ee.cloud.chat.schemas import SendMessageRequest

    msg_repo, _grp_repo = _install_chat_fakes()
    msg_repo.next_id = "m1"
    group = _fake_group()

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.notifications_service.create", new=spy),
        patch("ee.cloud.chat.message_service.UnreadService.bump_mention", new=AsyncMock()),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.send_message(
            "g1",
            "u1",
            SendMessageRequest(
                content="hello @alice",
                mentions=[{"type": "user", "id": "u2", "display_name": "alice"}],
            ),
        )

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["recipient"] == "u2"
    assert kwargs["kind"] == "mention"
    assert kwargs["workspace_id"] == "w1"
    assert kwargs["body"] == "hello @alice"
    assert "#general" in kwargs["title"]
    assert kwargs["source"].type == "message"
    assert kwargs["source"].id == "m1"


@pytest.mark.asyncio
async def test_send_message_self_mention_does_not_notify():
    from ee.cloud.chat.schemas import SendMessageRequest

    _msg_repo, _grp_repo = _install_chat_fakes()
    group = _fake_group()

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.notifications_service.create", new=spy),
        patch("ee.cloud.chat.message_service.UnreadService.bump_mention", new=AsyncMock()),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.send_message(
            "g1",
            "u1",
            SendMessageRequest(
                content="me me me",
                mentions=[{"type": "user", "id": "u1", "display_name": "self"}],
            ),
        )

    spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reaction derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_reaction_adding_notifies_original_sender():
    msg_repo, _grp_repo = _install_chat_fakes()
    # msg sender is u1, reactor is u2 → should notify u1.
    msg_repo.add(make_domain_message(id="m1", group="g1", sender="u1", content="hey"))
    group = _fake_group(owner="u1", workspace="w1")

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.notifications_service.create", new=spy),
        patch("ee.cloud.chat.message_service.UnreadService.bump_mention", new=AsyncMock()),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.toggle_reaction("m1", "u2", "\U0001f44d")

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["recipient"] == "u1"
    assert kwargs["kind"] == "reaction"
    assert kwargs["workspace_id"] == "w1"
    assert "\U0001f44d" in kwargs["title"]
    assert kwargs["body"] == "hey"
    assert kwargs["source"].type == "message"
    assert kwargs["source"].id == "m1"


@pytest.mark.asyncio
async def test_toggle_reaction_self_reaction_does_not_notify():
    msg_repo, _grp_repo = _install_chat_fakes()
    # msg sender and reactor are both u1 → no notification.
    msg_repo.add(make_domain_message(id="m1", group="g1", sender="u1", content="hey"))
    group = _fake_group(owner="u1", workspace="w1")

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.notifications_service.create", new=spy),
        patch("ee.cloud.chat.message_service.UnreadService.bump_mention", new=AsyncMock()),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.toggle_reaction("m1", "u1", "\U0001f44d")

    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_toggle_reaction_removing_does_not_notify():
    msg_repo, _grp_repo = _install_chat_fakes()
    # u2 already reacted — toggling removes and should NOT notify (the
    # ``toggle_added=False`` knob mimics the repo's "user removed
    # reaction" return path).
    msg_repo.add(make_domain_message(id="m1", group="g1", sender="u1", content="hey"))
    msg_repo.toggle_added = False
    group = _fake_group(owner="u1", workspace="w1")

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.notifications_service.create", new=spy),
        patch("ee.cloud.chat.message_service.UnreadService.bump_mention", new=AsyncMock()),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.toggle_reaction("m1", "u2", "\U0001f44d")

    spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Invite derivation
# ---------------------------------------------------------------------------


def _make_ws(ws_id: str = "w1", name: str = "Acme", seats: int = 10) -> SimpleNamespace:
    ws = SimpleNamespace(
        id=ws_id,
        name=name,
        slug="acme",
        owner="u1",
        plan="free",
        seats=seats,
        createdAt=None,
        deleted_at=None,
        settings=None,
    )
    ws.save = AsyncMock()
    return ws


def _make_user(user_id: str = "u1", email: str = "u1@example.com") -> SimpleNamespace:
    u = SimpleNamespace()
    u.id = user_id
    u.email = email
    u.workspaces = []
    u.save = AsyncMock()
    return u
