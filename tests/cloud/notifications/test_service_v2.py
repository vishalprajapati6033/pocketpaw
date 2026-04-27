"""Tests for the refactored NotificationService.

Uses an in-memory repository fake — no Beanie patches, no internal
mocks. Asserts both the new instance-method API and the legacy
classmethod fan-out API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ee.cloud.notifications.domain import Notification
from ee.cloud.notifications.repositories import (
    INotificationRepository,
    MongoNotificationRepository,
    set_default_repository,
)
from ee.cloud.notifications.service import NotificationService
from ee.cloud.realtime.events import (
    NotificationCleared,
    NotificationNew,
    NotificationRead,
)


class _InMemoryRepo:
    def __init__(self) -> None:
        self._items: dict[str, Notification] = {}
        self._next_id = 0

    async def create(self, notification: Notification) -> Notification:
        from dataclasses import replace

        self._next_id += 1
        new = replace(
            notification,
            id=f"n{self._next_id}",
            created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        )
        self._items[new.id] = new
        return new

    async def get(self, notification_id: str) -> Notification | None:
        return self._items.get(notification_id)

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        out = [n for n in self._items.values() if n.recipient_id == user_id]
        if unread:
            out = [n for n in out if not n.read]
        out.sort(key=lambda n: n.created_at, reverse=True)
        return out[:limit]

    async def mark_read(self, notification_id: str) -> bool:
        from dataclasses import replace

        n = self._items.get(notification_id)
        if n is None or n.read:
            return False
        self._items[notification_id] = replace(n, read=True)
        return True

    async def clear_unread(self, user_id: str) -> int:
        from dataclasses import replace

        count = 0
        for nid, n in list(self._items.items()):
            if n.recipient_id == user_id and not n.read:
                self._items[nid] = replace(n, read=True)
                count += 1
        return count


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []

    async def fake_emit(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr("ee.cloud.notifications.service.emit", fake_emit)
    return events


@pytest.fixture
def repo() -> INotificationRepository:
    return _InMemoryRepo()


@pytest.fixture
def service(repo: INotificationRepository) -> NotificationService:
    return NotificationService(repo)


# ---------------------------------------------------------------------------
# Instance-method API
# ---------------------------------------------------------------------------


async def test_create_persists_and_emits_new(service, repo, captured_events) -> None:
    out = await service.create(
        workspace_id="w1",
        recipient="u2",
        kind="mention",
        title="You were mentioned",
        body="hi",
    )
    assert out.recipient_id == "u2"
    assert out.workspace_id == "w1"
    assert out.kind == "mention"
    assert (await repo.get(out.id)) is not None

    assert len(captured_events) == 1
    ev = captured_events[0]
    assert isinstance(ev, NotificationNew)
    assert ev.data["user_id"] == "u2"
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["kind"] == "mention"
    assert ev.data["read"] is False


async def test_list_for_user_filters_unread(service) -> None:
    n1 = await service.create(workspace_id="w1", recipient="u1", kind="m", title="t1")
    await service.create(workspace_id="w1", recipient="u1", kind="m", title="t2")
    # Mark n1 specifically read; remaining unread should be the other one
    await service.mark_read(n1.id, "u1")

    unread = await service.list_for_user("u1", unread=True)
    assert len(unread) == 1
    assert unread[0].title == "t2"


async def test_mark_read_emits_event_and_returns_true(service, captured_events) -> None:
    n = await service.create(workspace_id="w1", recipient="u1", kind="m", title="t")
    captured_events.clear()  # discard the NotificationNew

    changed = await service.mark_read(n.id, "u1")
    assert changed is True
    assert any(isinstance(e, NotificationRead) for e in captured_events)
    read_ev = next(e for e in captured_events if isinstance(e, NotificationRead))
    assert read_ev.data == {"id": n.id, "user_id": "u1"}


async def test_mark_read_noop_for_wrong_user(service, captured_events) -> None:
    n = await service.create(workspace_id="w1", recipient="u1", kind="m", title="t")
    captured_events.clear()
    changed = await service.mark_read(n.id, "u_other")
    assert changed is False
    assert captured_events == []


async def test_mark_read_noop_for_already_read(service, captured_events) -> None:
    n = await service.create(workspace_id="w1", recipient="u1", kind="m", title="t")
    await service.mark_read(n.id, "u1")
    captured_events.clear()
    changed = await service.mark_read(n.id, "u1")
    assert changed is False
    assert captured_events == []


async def test_clear_all_returns_count_and_emits(service, captured_events) -> None:
    await service.create(workspace_id="w1", recipient="u1", kind="m", title="a")
    await service.create(workspace_id="w1", recipient="u1", kind="m", title="b")
    await service.create(workspace_id="w1", recipient="u2", kind="m", title="c")
    captured_events.clear()

    count = await service.clear_all("u1")
    assert count == 2
    cleared = [e for e in captured_events if isinstance(e, NotificationCleared)]
    assert len(cleared) == 1
    assert cleared[0].data == {"user_id": "u1"}


# ---------------------------------------------------------------------------
# Legacy classmethod facade — preserved for chat/message_service +
# workspace/service callers.
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_default_repo():
    """Restore the default Mongo repo after a test that swaps it."""
    yield
    set_default_repository(MongoNotificationRepository())


async def test_classmethod_create_default_uses_default_repo(
    captured_events, reset_default_repo
) -> None:
    fake = _InMemoryRepo()
    set_default_repository(fake)

    out = await NotificationService.create_default(
        workspace_id="w1",
        recipient="u9",
        kind="invite",
        title="Joined",
        body="",
    )
    assert out.recipient_id == "u9"
    assert any(isinstance(e, NotificationNew) for e in captured_events)


async def test_classmethod_list_for_user_default_returns_dicts(
    captured_events, reset_default_repo
) -> None:
    fake = _InMemoryRepo()
    set_default_repository(fake)

    await NotificationService.create_default(workspace_id="w1", recipient="u1", kind="m", title="t")
    out = await NotificationService.list_for_user_default("u1")
    assert isinstance(out, list) and len(out) == 1
    item = out[0]
    assert set(item.keys()) >= {
        "id",
        "user_id",
        "workspace_id",
        "kind",
        "title",
        "body",
        "source_id",
        "read",
        "created_at",
    }
