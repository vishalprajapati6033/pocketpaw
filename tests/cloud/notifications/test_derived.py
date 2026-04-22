"""Derivation tests: mention / reaction / invite → NotificationService.create.

Each emit site is exercised through its owning service; the spy on
``NotificationService.create`` proves the derivation fires with the right
arguments (and does NOT fire for self-targeted events).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _fake_message(
    *,
    message_id: str = "m1",
    group_id: str = "g1",
    sender: str = "u1",
    content: str = "hi",
    reactions: list | None = None,
) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=message_id,
        group=group_id,
        sender=sender,
        sender_type="user",
        agent=None,
        content=content,
        mentions=[],
        reply_to=None,
        attachments=[],
        reactions=reactions if reactions is not None else [],
        edited=False,
        edited_at=None,
        deleted=False,
        context_type="group",
        createdAt=now,
        insert=AsyncMock(),
        save=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# Mention derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_with_mention_creates_notification_for_target():
    from ee.cloud.chat.schemas import SendMessageRequest

    group = _fake_group()
    fake_msg = _fake_message(sender="u1", content="hello @alice")

    def fake_message_ctor(*_args, **kwargs):
        fake_msg.sender = kwargs.get("sender", fake_msg.sender)
        fake_msg.content = kwargs.get("content", fake_msg.content)
        fake_msg.mentions = kwargs.get("mentions", [])
        return fake_msg

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.Message", new=fake_message_ctor),
        patch("ee.cloud.chat.message_service.NotificationService.create", new=spy),
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

    group = _fake_group()
    fake_msg = _fake_message(sender="u1", content="me me me")

    def fake_message_ctor(*_args, **kwargs):
        fake_msg.sender = kwargs.get("sender", fake_msg.sender)
        fake_msg.content = kwargs.get("content", fake_msg.content)
        return fake_msg

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.Message", new=fake_message_ctor),
        patch("ee.cloud.chat.message_service.NotificationService.create", new=spy),
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
    group = _fake_group(owner="u1", workspace="w1")
    # msg sender is u1, reactor is u2 → should notify u1.
    msg = _fake_message(sender="u1", content="hey", reactions=[])

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_message_or_404",
            new=AsyncMock(return_value=msg),
        ),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.NotificationService.create", new=spy),
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
    group = _fake_group(owner="u1", workspace="w1")
    # msg sender and reactor are both u1 → no notification.
    msg = _fake_message(sender="u1", content="hey", reactions=[])

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_message_or_404",
            new=AsyncMock(return_value=msg),
        ),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.NotificationService.create", new=spy),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.toggle_reaction("m1", "u1", "\U0001f44d")

    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_toggle_reaction_removing_does_not_notify():
    from ee.cloud.models.message import Reaction

    group = _fake_group(owner="u1", workspace="w1")
    # u2 already reacted — toggling removes and should NOT notify.
    msg = _fake_message(
        sender="u1",
        content="hey",
        reactions=[Reaction(emoji="\U0001f44d", users=["u2"])],
    )

    spy = AsyncMock()

    with (
        patch("ee.cloud.chat.message_service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service._get_group_message_or_404",
            new=AsyncMock(return_value=msg),
        ),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.NotificationService.create", new=spy),
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


@pytest.mark.asyncio
async def test_create_invite_with_existing_user_creates_notification():
    from ee.cloud.workspace.schemas import CreateInviteRequest
    from ee.cloud.workspace.service import WorkspaceService

    user = _make_user("u1", "u1@example.com")
    ws = _make_ws()
    invited = _make_user("u2", "invitee@example.com")

    def fake_invite_ctor(*_args, **kwargs):
        inv = SimpleNamespace(
            id="inv_new",
            workspace=kwargs.get("workspace"),
            email=kwargs.get("email"),
            role=kwargs.get("role"),
            invited_by=kwargs.get("invited_by"),
            token=kwargs.get("token"),
            group=kwargs.get("group"),
            accepted=False,
            revoked=False,
            expired=False,
            expires_at=None,
        )
        inv.insert = AsyncMock()
        return inv

    invite_stub = MagicMock(side_effect=fake_invite_ctor)
    invite_stub.find_one = AsyncMock(return_value=None)

    user_stub = MagicMock()
    user_stub.find_one = AsyncMock(return_value=invited)
    user_stub.email = MagicMock()

    spy = AsyncMock()

    with (
        patch("ee.cloud.workspace.service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service._count_members",
            new=AsyncMock(return_value=1),
        ),
        patch("ee.cloud.workspace.service.Invite", new=invite_stub),
        patch("ee.cloud.workspace.service.User", new=user_stub),
        patch("ee.cloud.workspace.service.PydanticObjectId", new=lambda x: x),
        patch("ee.cloud.workspace.service.NotificationService.create", new=spy),
    ):
        await WorkspaceService.create_invite(
            "w1",
            user,
            CreateInviteRequest(email="invitee@example.com", role="member"),
        )

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["recipient"] == "u2"
    assert kwargs["kind"] == "invite"
    assert kwargs["workspace_id"] == "w1"
    assert "Acme" in kwargs["title"]
    assert kwargs["source"].type == "invite"
    assert kwargs["source"].id == "inv_new"


@pytest.mark.asyncio
async def test_create_invite_with_unknown_email_does_not_notify():
    from ee.cloud.workspace.schemas import CreateInviteRequest
    from ee.cloud.workspace.service import WorkspaceService

    user = _make_user("u1", "u1@example.com")
    ws = _make_ws()

    def fake_invite_ctor(*_args, **kwargs):
        inv = SimpleNamespace(
            id="inv_new",
            workspace=kwargs.get("workspace"),
            email=kwargs.get("email"),
            role=kwargs.get("role"),
            invited_by=kwargs.get("invited_by"),
            token=kwargs.get("token"),
            group=kwargs.get("group"),
            accepted=False,
            revoked=False,
            expired=False,
            expires_at=None,
        )
        inv.insert = AsyncMock()
        return inv

    invite_stub = MagicMock(side_effect=fake_invite_ctor)
    invite_stub.find_one = AsyncMock(return_value=None)

    user_stub = MagicMock()
    user_stub.find_one = AsyncMock(return_value=None)
    user_stub.email = MagicMock()

    spy = AsyncMock()

    with (
        patch("ee.cloud.workspace.service.emit", new=AsyncMock()),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service._count_members",
            new=AsyncMock(return_value=1),
        ),
        patch("ee.cloud.workspace.service.Invite", new=invite_stub),
        patch("ee.cloud.workspace.service.User", new=user_stub),
        patch("ee.cloud.workspace.service.PydanticObjectId", new=lambda x: x),
        patch("ee.cloud.workspace.service.NotificationService.create", new=spy),
    ):
        await WorkspaceService.create_invite(
            "w1",
            user,
            CreateInviteRequest(email="stranger@example.com", role="member"),
        )

    spy.assert_not_awaited()
