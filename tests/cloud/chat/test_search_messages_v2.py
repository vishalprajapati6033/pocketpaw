"""Parity test for the Phase 10 ``search_messages`` migration.

Exercises ``MessageService.search_messages`` end-to-end through an
in-memory ``IMessageRepository`` (via ``set_message_repository``) and
asserts the wire dict matches the legacy ``_message_response`` shape.

This is what makes the Phase 10 foundation ``real`` — the new
repository abstraction is now in the actual call path of an existing
classmethod that the chat router exposes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ee.cloud.chat.domain import Attachment, Mention, Message, Reaction
from ee.cloud.chat.repositories import (
    MongoMessageRepository,
    set_message_repository,
)


class _FakeRepo:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, str, int]] = []

    async def get(self, message_id: str) -> Message | None:
        return next((m for m in self.messages if m.id == message_id), None)

    async def list_for_group(
        self,
        group_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> list[Message]:
        return [m for m in self.messages if m.group == group_id][:limit]

    async def list_for_session(self, session_key: str, *, limit: int = 50) -> list[Message]:
        return []

    async def search_in_group(
        self, group_id: str, query: str, *, limit: int = 100
    ) -> list[Message]:
        self.calls.append((group_id, query, limit))
        return [
            m for m in self.messages if m.group == group_id and query.lower() in m.content.lower()
        ][:limit]


def _make_msg(id: str, group: str, content: str, sender: str = "u1") -> Message:
    return Message(
        id=id,
        context_type="group",
        workspace_id="w1",
        group=group,
        sender=sender,
        sender_type="user",
        agent=None,
        content=content,
        mentions=(Mention(type="user", id="u2", display_name="Bob"),),
        attachments=(
            Attachment(type="file", url="https://x/y.pdf", name="y.pdf", meta=(("size", 1024),)),
        ),
        reactions=(Reaction(emoji="👍", users=("u1", "u2")),),
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def fake_repo() -> _FakeRepo:
    return _FakeRepo([])


@pytest.fixture
def reset_default_repo():
    yield
    set_message_repository(MongoMessageRepository())


async def test_search_messages_routes_through_repository(
    fake_repo: _FakeRepo, reset_default_repo
) -> None:
    from ee.cloud.chat.message_service import MessageService

    fake_repo.messages = [
        _make_msg("m1", "g1", "hello world"),
        _make_msg("m2", "g1", "goodbye"),
        _make_msg("m3", "g1", "world peace"),
    ]
    set_message_repository(fake_repo)

    # Patch the auth helpers — they hit Beanie. The point of this test is
    # the repository routing, not the auth chain.
    fake_group = MagicMock(type="public", members=["u1"], member_roles={})
    with (
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=fake_group),
        ),
    ):
        results = await MessageService.search_messages("g1", "u1", "world")

    # Repository was called with the right args
    assert fake_repo.calls == [("g1", "world", 50)]

    # Two matches, in domain order
    assert len(results) == 2
    assert results[0]["_id"] == "m1"
    assert results[1]["_id"] == "m3"


async def test_search_messages_wire_shape_matches_legacy(
    fake_repo: _FakeRepo, reset_default_repo
) -> None:
    """Wire keys + value types match what `_message_response` produces."""
    from ee.cloud.chat.message_service import MessageService

    fake_repo.messages = [_make_msg("m1", "g1", "alpha")]
    set_message_repository(fake_repo)

    fake_group = MagicMock(type="public", members=["u1"], member_roles={})
    with patch(
        "ee.cloud.chat.message_service._get_group_or_404",
        new=AsyncMock(return_value=fake_group),
    ):
        out = await MessageService.search_messages("g1", "u1", "alpha")

    item = out[0]
    # Exact key set the legacy _message_response produced
    assert set(item.keys()) == {
        "_id",
        "group",
        "sender",
        "senderType",
        "agent",
        "content",
        "mentions",
        "replyTo",
        "replyPreview",
        "threadCount",
        "attachments",
        "reactions",
        "edited",
        "editedAt",
        "deleted",
        "createdAt",
    }
    # Spot-check value types
    assert item["_id"] == "m1"
    assert item["senderType"] == "user"
    assert item["createdAt"] == "2026-04-27T12:00:00+00:00"
    assert item["mentions"] == [{"type": "user", "id": "u2", "display_name": "Bob"}]
    assert item["attachments"][0]["meta"] == {"size": 1024}
    assert item["reactions"][0]["users"] == ["u1", "u2"]


async def test_search_messages_private_group_requires_membership(
    fake_repo: _FakeRepo, reset_default_repo
) -> None:
    """Auth check still fires before the repo call."""
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.shared.errors import Forbidden

    set_message_repository(fake_repo)

    # Private group, user is NOT a member
    fake_group = MagicMock(type="private", members=["u_other"], member_roles={})
    fake_group.owner = "u_other"
    with (
        patch(
            "ee.cloud.chat.message_service._get_group_or_404",
            new=AsyncMock(return_value=fake_group),
        ),
        pytest.raises(Forbidden),
    ):
        await MessageService.search_messages("g1", "u1", "anything")

    # Repo never called because auth rejected first
    assert fake_repo.calls == []
