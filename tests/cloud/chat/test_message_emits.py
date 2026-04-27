"""Tests that MessageService emits realtime events via the bus.

Each public MessageService mutation must fire the appropriate Event class
through ``emit()``. We patch the DB/permission layer at its seams so we
test the emit behavior in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_group(
    group_id: str = "g1",
    owner: str = "u1",
    members: list[str] | None = None,
) -> SimpleNamespace:
    """Minimal Group stand-in for permission checks."""
    effective_members = members if members is not None else [owner]
    return SimpleNamespace(
        id=group_id,
        workspace="w1",
        owner=owner,
        members=effective_members,
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
) -> SimpleNamespace:
    """Minimal Message stand-in with the attributes _message_response reads."""
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
        thread_count=0,
        attachments=[],
        reactions=[],
        edited=False,
        edited_at=None,
        deleted=False,
        context_type="group",
        createdAt=now,
        insert=AsyncMock(),
        save=AsyncMock(),
    )


def _capture_emits() -> tuple[list, object]:
    """Return (recorded, fake_emit) where fake_emit is an async callable that
    appends each emitted event to the shared ``recorded`` list."""
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    return recorded, fake_emit


@pytest.mark.asyncio
async def test_send_message_emits_new_and_sent():
    """MessageService.send_message must fire both message.new and message.sent."""
    from ee.cloud.chat.dto import SendMessageRequest
    from ee.cloud.realtime.events import MessageNew, MessageSent

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    group = _fake_group()

    fake_msg = _fake_message(sender="u1", content="hi")

    def _fake_message_ctor(*args, **kwargs):
        # Mirror the fields send_message sets on the Message so _message_response
        # produces a sensible shape.
        fake_msg.content = kwargs.get("content", fake_msg.content)
        fake_msg.sender = kwargs.get("sender", fake_msg.sender)
        fake_msg.group = kwargs.get("group", fake_msg.group)
        return fake_msg

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
        patch("ee.cloud.chat.message_service.Message", new=_fake_message_ctor),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.send_message("g1", "u1", SendMessageRequest(content="hi"))

    wire_types = {type(e).__name__ for e in recorded}
    assert "MessageNew" in wire_types
    assert "MessageSent" in wire_types

    # message.new payload must carry sender so AudienceResolver can exclude.
    new_ev = next(e for e in recorded if isinstance(e, MessageNew))
    assert new_ev.data.get("sender") == "u1"

    # message.sent payload must carry sender_id so AudienceResolver can address it.
    sent_ev = next(e for e in recorded if isinstance(e, MessageSent))
    assert sent_ev.data.get("sender_id") == "u1"


@pytest.mark.asyncio
async def test_edit_message_emits_edited():
    from ee.cloud.chat.dto import EditMessageRequest
    from ee.cloud.realtime.events import MessageEdited

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    msg = _fake_message()
    group = _fake_group()

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_message_or_404",
            new=AsyncMock(return_value=msg),
        ),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.edit_message("m1", "u1", EditMessageRequest(content="new"))

    assert any(isinstance(e, MessageEdited) for e in recorded)
    ev = next(e for e in recorded if isinstance(e, MessageEdited))
    assert ev.data["message_id"] == "m1"
    assert ev.data["group_id"] == "g1"
    assert ev.data["content"] == "new"
    assert "edited_at" in ev.data


@pytest.mark.asyncio
async def test_delete_message_emits_deleted():
    from ee.cloud.realtime.events import MessageDeleted

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    msg = _fake_message()

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_message_or_404",
            new=AsyncMock(return_value=msg),
        ),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.delete_message("m1", "u1")

    assert any(isinstance(e, MessageDeleted) for e in recorded)
    ev = next(e for e in recorded if isinstance(e, MessageDeleted))
    assert ev.data["message_id"] == "m1"
    assert ev.data["group_id"] == "g1"


@pytest.mark.asyncio
async def test_toggle_reaction_emits_message_reaction():
    from ee.cloud.realtime.events import MessageReaction

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    msg = _fake_message()
    group = _fake_group()

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_message_or_404",
            new=AsyncMock(return_value=msg),
        ),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
    ):
        from ee.cloud.chat.message_service import MessageService

        await MessageService.toggle_reaction("m1", "u1", "\U0001f44d")

    assert any(isinstance(e, MessageReaction) for e in recorded)
    ev = next(e for e in recorded if isinstance(e, MessageReaction))
    assert ev.data["message_id"] == "m1"
    assert ev.data["group_id"] == "g1"
    assert ev.data["emoji"] == "\U0001f44d"
    assert ev.data["user_id"] == "u1"


def test_router_no_longer_broadcasts_message_events():
    """Regression guard: the four _ws_message_* handlers must not call manager.broadcast/send."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[3] / "ee/cloud/chat/router.py").read_text(
        encoding="utf-8"
    )

    start = src.index("async def _ws_message_send")
    end = src.index("async def _ws_typing")
    segment = src[start:end]
    assert "manager.broadcast_to_group" not in segment, (
        "message send/edit/delete/react handler still broadcasts directly — route via emit()"
    )
    assert "manager.send_to_user" not in segment, (
        "message handler still calls send_to_user directly"
    )


@pytest.mark.asyncio
async def test_send_message_fans_out_everyone_mention_to_all_members():
    """@everyone creates one notification per non-sender member and bumps their
    mention counter."""
    from types import SimpleNamespace

    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.dto import SendMessageRequest

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2", "u3"])

    created_notifs: list[dict] = []
    bumped: list[tuple[str, str]] = []

    async def fake_notif(**kwargs):
        created_notifs.append(kwargs)
        return SimpleNamespace(id="n")

    async def fake_bump(user_id, group_id):
        bumped.append((user_id, group_id))

    fake_msg_ns = SimpleNamespace(
        id="m1",
        createdAt=None,
        insert=AsyncMock(),
    )

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "ee.cloud.chat.message_service._require_can_post",
            new=MagicMock(),
        ),
        patch(
            "ee.cloud.chat.message_service.Message",
            new=MagicMock(return_value=fake_msg_ns),
        ),
        patch(
            "ee.cloud.chat.message_service._message_response",
            new=MagicMock(return_value={"_id": "m1"}),
        ),
        patch(
            "ee.cloud.chat.message_service.event_bus.emit",
            new=AsyncMock(),
        ),
        patch(
            "ee.cloud.chat.message_service.NotificationService.create_default",
            new=fake_notif,
        ),
        patch(
            "ee.cloud.chat.message_service.UnreadService.bump_mention",
            new=fake_bump,
        ),
    ):
        body = SendMessageRequest(
            content="hello team",
            mentions=[{"type": "everyone", "id": "", "display_name": "@everyone"}],
        )
        await MessageService.send_message("g1", "sender", body)

    recipients = {n["recipient"] for n in created_notifs}
    assert recipients == {"u2", "u3"}
    assert set(bumped) == {("u2", "g1"), ("u3", "g1")}


@pytest.mark.asyncio
async def test_send_message_user_and_broadcast_mention_dedupes():
    """If a message has both @user(u2) and @everyone, u2 only gets one
    notification, not two."""
    from types import SimpleNamespace

    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.dto import SendMessageRequest

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2", "u3"])

    created_notifs: list[dict] = []

    async def fake_notif(**kwargs):
        created_notifs.append(kwargs)
        return SimpleNamespace(id="n")

    fake_msg_ns = SimpleNamespace(id="m1", createdAt=None, insert=AsyncMock())

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch("ee.cloud.chat.message_service._get_group_or_404", new=AsyncMock(return_value=group)),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
        patch("ee.cloud.chat.message_service.Message", new=MagicMock(return_value=fake_msg_ns)),
        patch("ee.cloud.chat.message_service._message_response", new=MagicMock(return_value={"_id": "m1"})),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
        patch("ee.cloud.chat.message_service.NotificationService.create_default", new=fake_notif),
        patch("ee.cloud.chat.message_service.UnreadService.bump_mention", new=AsyncMock()),
    ):
        body = SendMessageRequest(
            content="hi u2",
            mentions=[
                {"type": "user", "id": "u2", "display_name": "@u2"},
                {"type": "everyone", "id": "", "display_name": "@everyone"},
            ],
        )
        await MessageService.send_message("g1", "sender", body)

    # Each recipient should appear exactly once.
    recipients = [n["recipient"] for n in created_notifs]
    assert sorted(recipients) == ["u2", "u3"]  # no duplicate u2


@pytest.mark.asyncio
async def test_send_message_emits_unread_update_for_non_senders():
    """Every non-sender member should receive an unread.update event."""
    from types import SimpleNamespace

    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.dto import SendMessageRequest
    from ee.cloud.realtime.events import UnreadUpdate

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2", "u3"])

    fake_msg_ns = SimpleNamespace(id="m1", createdAt=None, insert=AsyncMock())

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch("ee.cloud.chat.message_service._get_group_or_404", new=AsyncMock(return_value=group)),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
        patch("ee.cloud.chat.message_service.Message", new=MagicMock(return_value=fake_msg_ns)),
        patch("ee.cloud.chat.message_service._message_response", new=MagicMock(return_value={"_id": "m1"})),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
    ):
        await MessageService.send_message("g1", "sender", SendMessageRequest(content="hi"))

    updates = [e for e in recorded if isinstance(e, UnreadUpdate)]
    recipients = {e.data["user_id"] for e in updates}
    assert recipients == {"u2", "u3"}
    for e in updates:
        assert e.data["group_id"] == "g1"
        assert e.data["delta"] == 1


@pytest.mark.asyncio
async def test_send_reply_does_not_bump_thread_count():
    """Inline-quoted replies replaced threads: parent.thread_count stays
    untouched so we don't do a pointless parent write on every reply."""
    from types import SimpleNamespace

    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.dto import SendMessageRequest

    _recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2"])

    parent = SimpleNamespace(
        id="parent1",
        thread_count=0,
        sender_type="user",
        agent=None,
        save=AsyncMock(),
    )
    fake_msg_ns = SimpleNamespace(id="reply1", createdAt=None, insert=AsyncMock())

    fake_message_class = MagicMock(return_value=fake_msg_ns)
    fake_message_class.find_one = AsyncMock(return_value=parent)

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch("ee.cloud.chat.message_service._get_group_or_404", new=AsyncMock(return_value=group)),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
        patch("ee.cloud.chat.message_service.Message", new=fake_message_class),
        patch("ee.cloud.chat.message_service._message_response", new=MagicMock(return_value={"_id": "reply1"})),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
    ):
        await MessageService.send_message(
            "g1",
            "sender",
            SendMessageRequest(content="a reply", reply_to="507f1f77bcf86cd799439011"),
        )

    assert parent.thread_count == 0
    parent.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_reply_emits_message_new_not_thread_reply():
    """Inline replies fan out via MessageNew; no ThreadReply event fires
    because we no longer render a separate thread panel."""
    from types import SimpleNamespace

    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.dto import SendMessageRequest
    from ee.cloud.realtime.events import MessageNew, ThreadReply

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender"])
    parent = SimpleNamespace(
        id="parent1",
        thread_count=0,
        sender_type="user",
        agent=None,
        save=AsyncMock(),
    )
    fake_msg_ns = SimpleNamespace(id="r1", createdAt=None, insert=AsyncMock(), thread_count=0)

    fake_message_class = MagicMock(return_value=fake_msg_ns)
    fake_message_class.find_one = AsyncMock(return_value=parent)

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch("ee.cloud.chat.message_service._get_group_or_404", new=AsyncMock(return_value=group)),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
        patch("ee.cloud.chat.message_service.Message", new=fake_message_class),
        patch("ee.cloud.chat.message_service._message_response", new=MagicMock(return_value={"_id": "r1"})),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
    ):
        await MessageService.send_message(
            "g1",
            "sender",
            SendMessageRequest(content="a reply", reply_to="507f1f77bcf86cd799439011"),
        )

    threads = [e for e in recorded if isinstance(e, ThreadReply)]
    news = [e for e in recorded if isinstance(e, MessageNew)]
    assert len(threads) == 0
    assert len(news) == 1
