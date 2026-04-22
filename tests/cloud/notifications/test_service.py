"""Unit tests for NotificationService.

Patches the Notification document + ``emit`` so we exercise the service's
CRUD + fan-out contract without touching Mongo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_notification(
    *,
    notif_id: str = "n1",
    workspace: str = "w1",
    recipient: str = "u1",
    kind: str = "mention",
    title: str = "t",
    body: str = "b",
    read: bool = False,
) -> SimpleNamespace:
    ns = SimpleNamespace(
        id=notif_id,
        workspace=workspace,
        recipient=recipient,
        type=kind,
        title=title,
        body=body,
        source=None,
        read=read,
        createdAt=datetime.now(UTC),
    )
    ns.insert = AsyncMock()
    ns.save = AsyncMock()
    return ns


@pytest.mark.asyncio
async def test_create_persists_and_emits_notification_new():
    from ee.cloud.notifications.service import NotificationService
    from ee.cloud.realtime.events import NotificationNew

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    fake = _fake_notification()

    def fake_ctor(*args, **kwargs):
        fake.workspace = kwargs.get("workspace", fake.workspace)
        fake.recipient = kwargs.get("recipient", fake.recipient)
        fake.type = kwargs.get("type", fake.type)
        fake.title = kwargs.get("title", fake.title)
        fake.body = kwargs.get("body", fake.body)
        fake.source = kwargs.get("source")
        return fake

    with (
        patch("ee.cloud.notifications.service.emit", new=fake_emit),
        patch("ee.cloud.notifications.service.Notification", new=fake_ctor),
    ):
        result = await NotificationService.create(
            workspace_id="w1",
            recipient="u2",
            kind="mention",
            title="You were mentioned",
            body="hello",
        )

    fake.insert.assert_awaited_once()
    assert result is fake
    assert len(recorded) == 1
    assert isinstance(recorded[0], NotificationNew)
    data = recorded[0].data
    assert data["user_id"] == "u2"
    assert data["workspace_id"] == "w1"
    assert data["kind"] == "mention"
    assert data["title"] == "You were mentioned"
    assert data["body"] == "hello"
    assert data["read"] is False


@pytest.mark.asyncio
async def test_list_for_user_filters_unread():
    from ee.cloud.notifications.service import NotificationService

    unread_notif = _fake_notification(notif_id="n1", read=False)

    seen_query: dict = {}

    class FakeCursor:
        def sort(self, *_a, **_kw):
            return self

        def limit(self, *_a, **_kw):
            return self

        def __aiter__(self):
            async def _gen():
                yield unread_notif

            return _gen()

    def fake_find(query):
        seen_query.update(query)
        return FakeCursor()

    notification_stub = MagicMock()
    notification_stub.find = fake_find
    notification_stub.createdAt = MagicMock()

    with patch("ee.cloud.notifications.service.Notification", new=notification_stub):
        results = await NotificationService.list_for_user("u1", unread=True, limit=25)

    assert seen_query == {"recipient": "u1", "read": False}
    assert len(results) == 1
    assert results[0]["id"] == "n1"
    assert results[0]["read"] is False


@pytest.mark.asyncio
async def test_mark_read_emits_notification_read():
    from ee.cloud.notifications.service import NotificationService
    from ee.cloud.realtime.events import NotificationRead

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    notif = _fake_notification(notif_id="n1", recipient="u1", read=False)

    notification_stub = MagicMock()
    notification_stub.get = AsyncMock(return_value=notif)

    with (
        patch("ee.cloud.notifications.service.emit", new=fake_emit),
        patch("ee.cloud.notifications.service.Notification", new=notification_stub),
        patch("ee.cloud.notifications.service.PydanticObjectId", new=lambda x: x),
    ):
        await NotificationService.mark_read("n1", "u1")

    assert notif.read is True
    notif.save.assert_awaited_once()
    assert len(recorded) == 1
    assert isinstance(recorded[0], NotificationRead)
    assert recorded[0].data == {"id": "n1", "user_id": "u1"}


@pytest.mark.asyncio
async def test_mark_read_noop_for_already_read():
    from ee.cloud.notifications.service import NotificationService

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    notif = _fake_notification(notif_id="n1", recipient="u1", read=True)

    notification_stub = MagicMock()
    notification_stub.get = AsyncMock(return_value=notif)

    with (
        patch("ee.cloud.notifications.service.emit", new=fake_emit),
        patch("ee.cloud.notifications.service.Notification", new=notification_stub),
        patch("ee.cloud.notifications.service.PydanticObjectId", new=lambda x: x),
    ):
        await NotificationService.mark_read("n1", "u1")

    notif.save.assert_not_awaited()
    assert recorded == []


@pytest.mark.asyncio
async def test_clear_all_emits_notification_cleared():
    from ee.cloud.notifications.service import NotificationService
    from ee.cloud.realtime.events import NotificationCleared

    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    update_result = SimpleNamespace(modified_count=3)

    class FakeCursor:
        update_many = AsyncMock(return_value=update_result)

    notification_stub = MagicMock()
    notification_stub.find = MagicMock(return_value=FakeCursor())

    with (
        patch("ee.cloud.notifications.service.emit", new=fake_emit),
        patch("ee.cloud.notifications.service.Notification", new=notification_stub),
    ):
        count = await NotificationService.clear_all("u1")

    assert count == 3
    notification_stub.find.assert_called_once_with({"recipient": "u1", "read": False})
    assert len(recorded) == 1
    assert isinstance(recorded[0], NotificationCleared)
    assert recorded[0].data == {"user_id": "u1"}
