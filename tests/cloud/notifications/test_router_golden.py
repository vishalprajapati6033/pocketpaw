"""Golden-response tests for the notifications router.

These tests assert the wire shape returned by each endpoint by
constructing a small FastAPI app that mounts only the notifications
router with the service dep overridden to one backed by an in-memory
repository. The shape asserted here is the same shape the legacy
`_to_wire` function produced.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.http import add_error_handler
from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.router import get_notification_service, router
from ee.cloud.notifications.service import NotificationService


class _Repo:
    def __init__(self) -> None:
        self._items: dict[str, Notification] = {}

    async def create(self, n: Notification) -> Notification:
        self._items[n.id] = n
        return n

    async def get(self, nid: str) -> Notification | None:
        return self._items.get(nid)

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        out = [n for n in self._items.values() if n.recipient_id == user_id]
        if unread:
            out = [n for n in out if not n.read]
        out.sort(key=lambda n: n.created_at, reverse=True)
        return out[:limit]

    async def mark_read(self, nid: str) -> bool:
        from dataclasses import replace

        n = self._items.get(nid)
        if not n or n.read:
            return False
        self._items[nid] = replace(n, read=True)
        return True

    async def clear_unread(self, user_id: str) -> int:
        from dataclasses import replace

        count = 0
        for nid, n in list(self._items.items()):
            if n.recipient_id == user_id and not n.read:
                self._items[nid] = replace(n, read=True)
                count += 1
        return count


def _seed(repo: _Repo, **kw) -> Notification:
    base = dict(
        id="n1",
        workspace_id="w1",
        recipient_id="user-1",
        kind="mention",
        title="hi",
        body="b",
        source=NotificationSource(type="message", id="m1", pocket_id=None),
        read=False,
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
    )
    base.update(kw)
    n = Notification(**base)
    repo._items[n.id] = n
    return n


async def _no_emit(_event):
    pass


@pytest.fixture
def app_and_repo(monkeypatch):
    from ee.cloud.auth import current_active_user

    repo = _Repo()
    service = NotificationService(repo)

    class _U:
        id = "user-1"
        active_workspace = "w1"
        workspaces: list = []

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = lambda: _U()
    app.dependency_overrides[get_notification_service] = lambda: service
    monkeypatch.setattr("ee.cloud.notifications.service.emit", _no_emit)
    return app, repo


def test_list_returns_dto_shape(app_and_repo) -> None:
    app, repo = app_and_repo
    _seed(repo)
    client = TestClient(app)

    resp = client.get("/api/v1/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert set(item.keys()) == {
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
    assert item == {
        "id": "n1",
        "user_id": "user-1",
        "workspace_id": "w1",
        "kind": "mention",
        "title": "hi",
        "body": "b",
        "source_id": "m1",
        "read": False,
        "created_at": "2026-04-27T12:00:00+00:00",
    }


def test_list_unread_query_filters(app_and_repo) -> None:
    app, repo = app_and_repo
    _seed(repo, id="n1", read=True)
    _seed(repo, id="n2", read=False)
    client = TestClient(app)

    body = client.get("/api/v1/notifications?unread=true").json()
    assert [n["id"] for n in body] == ["n2"]


def test_list_limit_query_caps_results(app_and_repo) -> None:
    app, repo = app_and_repo
    for i in range(5):
        _seed(
            repo,
            id=f"n{i}",
            created_at=datetime(2026, 4, 27, 12, 0, i, tzinfo=UTC),
        )
    client = TestClient(app)

    body = client.get("/api/v1/notifications?limit=2").json()
    assert len(body) == 2


def test_mark_read_returns_ok_envelope(app_and_repo) -> None:
    app, repo = app_and_repo
    _seed(repo)

    client = TestClient(app)
    resp = client.post("/api/v1/notifications/n1/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert repo._items["n1"].read is True


def test_clear_returns_count(app_and_repo) -> None:
    app, repo = app_and_repo
    _seed(repo, id="n1", read=False)
    _seed(repo, id="n2", read=False)

    client = TestClient(app)
    resp = client.post("/api/v1/notifications/clear")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 2}
