"""Tests that MessageService emits realtime events via the bus.

Each public MessageService mutation must fire the appropriate Event class
through ``emit()``. We install fake message + group repositories (Phase 10
moved mutations to ``IMessageRepository`` / ``IGroupRepository``) and
patch the residual legacy seams (``_get_group_or_404`` /
``_require_can_post``) so emit behavior is exercised in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_group(
    group_id: str = "g1",
    owner: str = "u1",
    members: list[str] | None = None,
) -> SimpleNamespace:
    """Beanie-shape Group stand-in for the legacy ``_get_group_or_404`` seam.

    The membership / can-post check still runs against the Beanie
    ``Group`` doc because that helper is shared with non-migrated paths.
    """
    effective_members = members if members is not None else [owner]
    return SimpleNamespace(
        id=group_id,
        workspace="w1",
        name="G",
        owner=owner,
        members=effective_members,
        member_roles={owner: "admin"},
        archived=False,
        type="group",
        last_message_at=None,
        message_count=0,
        save=AsyncMock(),
    )


def _capture_emits() -> tuple[list, object]:
    """Return ``(recorded, fake_emit)`` — fake_emit appends each event."""
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    return recorded, fake_emit


@pytest.mark.asyncio
async def test_send_message_emits_new_and_sent(chat_repos):
    """MessageService.send_message must fire both message.new and message.sent."""
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import SendMessageRequest
    from ee.cloud.realtime.events import MessageNew, MessageSent

    msg_repo, _grp_repo = chat_repos
    recorded, fake_emit = _capture_emits()
    group = _fake_group()

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
    ):
        await MessageService.send_message("g1", "u1", SendMessageRequest(content="hi"))

    # Repository was called with the right shape.
    assert len(msg_repo.created) == 1
    call = msg_repo.created[0]
    assert call["sender"] == "u1"
    assert call["content"] == "hi"
    assert call["sender_type"] == "user"

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
async def test_edit_message_emits_edited(chat_repos):
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import EditMessageRequest
    from ee.cloud.realtime.events import MessageEdited
    from tests.cloud.chat.conftest import make_domain_message

    msg_repo, _grp_repo = chat_repos
    msg_repo.add(make_domain_message(id="m1", group="g1", sender="u1"))

    recorded, fake_emit = _capture_emits()
    group = _fake_group()

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
    ):
        await MessageService.edit_message("m1", "u1", EditMessageRequest(content="new"))

    assert len(msg_repo.edited) == 1
    assert msg_repo.edited[0]["content"] == "new"

    assert any(isinstance(e, MessageEdited) for e in recorded)
    ev = next(e for e in recorded if isinstance(e, MessageEdited))
    assert ev.data["message_id"] == "m1"
    assert ev.data["group_id"] == "g1"
    assert ev.data["content"] == "new"
    assert "edited_at" in ev.data


@pytest.mark.asyncio
async def test_delete_message_emits_deleted(chat_repos):
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.realtime.events import MessageDeleted
    from tests.cloud.chat.conftest import make_domain_message

    msg_repo, _grp_repo = chat_repos
    msg_repo.add(make_domain_message(id="m1", group="g1", sender="u1"))

    recorded, fake_emit = _capture_emits()

    with patch("ee.cloud.chat.message_service.emit", new=fake_emit):
        await MessageService.delete_message("m1", "u1")

    assert msg_repo.deleted == ["m1"]

    assert any(isinstance(e, MessageDeleted) for e in recorded)
    ev = next(e for e in recorded if isinstance(e, MessageDeleted))
    assert ev.data["message_id"] == "m1"
    assert ev.data["group_id"] == "g1"


@pytest.mark.asyncio
async def test_toggle_reaction_emits_message_reaction(chat_repos):
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.realtime.events import MessageReaction
    from tests.cloud.chat.conftest import make_domain_message

    msg_repo, _grp_repo = chat_repos
    msg_repo.add(make_domain_message(id="m1", group="g1", sender="u1"))

    recorded, fake_emit = _capture_emits()
    group = _fake_group()

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post"),
    ):
        await MessageService.toggle_reaction("m1", "u1", "\U0001f44d")

    assert msg_repo.reactions == [
        {"message_id": "m1", "user_id": "u1", "emoji": "\U0001f44d"}
    ]

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
async def test_send_message_fans_out_everyone_mention_to_all_members(chat_repos):
    """@everyone creates one notification per non-sender member and bumps their
    mention counter."""
    from types import SimpleNamespace

    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import SendMessageRequest

    _msg_repo, _grp_repo = chat_repos
    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2", "u3"])

    created_notifs: list[dict] = []
    bumped: list[tuple[str, str]] = []

    async def fake_notif(**kwargs):
        created_notifs.append(kwargs)
        return SimpleNamespace(id="n")

    async def fake_bump(user_id, group_id):
        bumped.append((user_id, group_id))

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
            "ee.cloud.chat.message_service.event_bus.emit",
            new=AsyncMock(),
        ),
        patch(
            "ee.cloud.chat.message_service.notifications_service.create",
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
async def test_send_message_user_and_broadcast_mention_dedupes(chat_repos):
    """If a message has both @user(u2) and @everyone, u2 only gets one
    notification, not two."""
    from types import SimpleNamespace

    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import SendMessageRequest

    _msg_repo, _grp_repo = chat_repos
    _recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2", "u3"])

    created_notifs: list[dict] = []

    async def fake_notif(**kwargs):
        created_notifs.append(kwargs)
        return SimpleNamespace(id="n")

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
        patch(
            "ee.cloud.chat.message_service.notifications_service.create",
            new=fake_notif,
        ),
        patch(
            "ee.cloud.chat.message_service.UnreadService.bump_mention",
            new=AsyncMock(),
        ),
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
async def test_send_message_emits_unread_update_for_non_senders(chat_repos):
    """Every non-sender member should receive an unread.update event."""
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import SendMessageRequest
    from ee.cloud.realtime.events import UnreadUpdate

    _msg_repo, _grp_repo = chat_repos
    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2", "u3"])

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
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
async def test_send_reply_does_not_bump_thread_count(chat_repos):
    """Inline-quoted replies replaced threads: the parent message is
    fetched for preview only — never edited/deleted/reacted as a side
    effect of the reply send."""
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import SendMessageRequest
    from tests.cloud.chat.conftest import make_domain_message

    msg_repo, _grp_repo = chat_repos
    parent = make_domain_message(id="parent1", group="g1", sender="u_other", thread_count=0)
    msg_repo.add(parent)

    _recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender", "u2"])

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
    ):
        await MessageService.send_message(
            "g1",
            "sender",
            SendMessageRequest(content="a reply", reply_to="parent1"),
        )

    # Parent must not be touched as a side-effect of the reply.
    assert msg_repo.edited == []
    assert msg_repo.deleted == []
    assert msg_repo.reactions == []


@pytest.mark.asyncio
async def test_send_reply_emits_message_new_not_thread_reply(chat_repos):
    """Inline replies fan out via MessageNew; no ThreadReply event fires
    because we no longer render a separate thread panel."""
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import SendMessageRequest
    from ee.cloud.realtime.events import MessageNew, ThreadReply
    from tests.cloud.chat.conftest import make_domain_message

    msg_repo, _grp_repo = chat_repos
    msg_repo.add(make_domain_message(id="parent1", group="g1", sender="u_other"))

    recorded, fake_emit = _capture_emits()
    group = _fake_group(owner="sender", members=["sender"])

    with (
        patch("ee.cloud.chat.message_service.emit", new=fake_emit),
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=group),
        ),
        patch("ee.cloud.chat.message_service._require_can_post", new=MagicMock()),
        patch("ee.cloud.chat.message_service.event_bus.emit", new=AsyncMock()),
    ):
        await MessageService.send_message(
            "g1",
            "sender",
            SendMessageRequest(content="a reply", reply_to="parent1"),
        )

    threads = [e for e in recorded if isinstance(e, ThreadReply)]
    news = [e for e in recorded if isinstance(e, MessageNew)]
    assert len(threads) == 0
    assert len(news) == 1
