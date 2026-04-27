"""Tests for INotificationRepository via an in-memory fake.

Each phase ships unit tests against a fake repository (the Protocol).
The Mongo-backed implementation is exercised via the broader test suite
+ explicit integration tests later (Phase 11). Phase 2 is pragmatic:
we trust that conforming to the Protocol means the Mongo impl works,
provided the fake matches the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ee.cloud.notifications.domain import Notification
from ee.cloud.notifications.repositories import INotificationRepository


class _InMemoryNotificationRepository:
    """Conforms to INotificationRepository for tests. Stores domain
    entities in a dict; preserves insertion order via a list."""

    def __init__(self) -> None:
        self._items: dict[str, Notification] = {}

    async def create(self, notification: Notification) -> Notification:
        self._items[notification.id] = notification
        return notification

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


def _n(id: str, recipient: str, *, read: bool = False, ts: int = 0) -> Notification:
    return Notification(
        id=id,
        workspace_id="w1",
        recipient_id=recipient,
        kind="mention",
        title="t",
        body="",
        source=None,
        read=read,
        created_at=datetime(2026, 4, 27, 12, 0, ts, tzinfo=UTC),
    )


@pytest.fixture
def repo() -> INotificationRepository:
    return _InMemoryNotificationRepository()


async def test_create_returns_same_entity(repo) -> None:
    n = _n("n1", "u1")
    out = await repo.create(n)
    assert out is n


async def test_get_returns_created(repo) -> None:
    await repo.create(_n("n1", "u1"))
    fetched = await repo.get("n1")
    assert fetched is not None
    assert fetched.id == "n1"


async def test_get_returns_none_for_missing(repo) -> None:
    assert await repo.get("missing") is None


async def test_list_for_user_filters_by_recipient(repo) -> None:
    await repo.create(_n("n1", "u1"))
    await repo.create(_n("n2", "u2"))
    out = await repo.list_for_user("u1")
    assert [n.id for n in out] == ["n1"]


async def test_list_for_user_unread_filter(repo) -> None:
    await repo.create(_n("n1", "u1", read=True))
    await repo.create(_n("n2", "u1", read=False))
    out = await repo.list_for_user("u1", unread=True)
    assert [n.id for n in out] == ["n2"]


async def test_list_for_user_orders_newest_first(repo) -> None:
    await repo.create(_n("n1", "u1", ts=1))
    await repo.create(_n("n2", "u1", ts=5))
    await repo.create(_n("n3", "u1", ts=3))
    out = await repo.list_for_user("u1")
    assert [n.id for n in out] == ["n2", "n3", "n1"]


async def test_list_for_user_respects_limit(repo) -> None:
    for i in range(5):
        await repo.create(_n(f"n{i}", "u1", ts=i))
    out = await repo.list_for_user("u1", limit=2)
    assert len(out) == 2


async def test_mark_read_returns_true_and_flips(repo) -> None:
    await repo.create(_n("n1", "u1", read=False))
    changed = await repo.mark_read("n1")
    assert changed is True
    fetched = await repo.get("n1")
    assert fetched is not None
    assert fetched.read is True


async def test_mark_read_returns_false_when_already_read(repo) -> None:
    await repo.create(_n("n1", "u1", read=True))
    assert await repo.mark_read("n1") is False


async def test_mark_read_returns_false_when_missing(repo) -> None:
    assert await repo.mark_read("missing") is False


async def test_clear_unread_returns_count(repo) -> None:
    await repo.create(_n("n1", "u1", read=False))
    await repo.create(_n("n2", "u1", read=True))
    await repo.create(_n("n3", "u1", read=False))
    count = await repo.clear_unread("u1")
    assert count == 2
    n1 = await repo.get("n1")
    n3 = await repo.get("n3")
    assert n1 is not None and n1.read is True
    assert n3 is not None and n3.read is True
