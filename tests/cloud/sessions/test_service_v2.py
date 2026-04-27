"""Tests for the refactored SessionService.

Uses an in-memory ``ISessionRepository`` fake. Replaces the
``test_session_emits.py`` that patched Beanie internals which no longer
exist post-Phase-9 refactor.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud.realtime.events import SessionCreated, SessionDeleted, SessionUpdated
from ee.cloud.sessions.domain import Session
from ee.cloud.sessions.dto import CreateSessionRequest, UpdateSessionRequest
from ee.cloud.sessions.repositories import (
    ISessionRepository,
    MongoSessionRepository,
    set_default_repository,
)
from ee.cloud.sessions.service import SessionService


class _FakeRepo:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._counter = 0

    def seed(self, s: Session) -> None:
        self.sessions[s.id] = s

    async def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def get_by_session_id(self, session_id: str) -> Session | None:
        for s in self.sessions.values():
            if s.sessionId == session_id:
                return s
        return None

    async def list_for_owner(
        self, *, workspace_id: str, user_id: str
    ) -> list[Session]:
        return [
            s
            for s in self.sessions.values()
            if s.workspace == workspace_id and s.owner == user_id and not s.deleted_at
        ]

    async def list_by_agent(
        self, *, workspace_id: str, user_id: str, agent_id: str
    ) -> list[Session]:
        return [
            s
            for s in self.sessions.values()
            if s.workspace == workspace_id
            and s.owner == user_id
            and s.agent == agent_id
            and not s.deleted_at
        ]

    async def list_for_pocket(
        self, *, pocket_id: str, user_id: str
    ) -> list[Session]:
        return [
            s
            for s in self.sessions.values()
            if s.pocket == pocket_id and s.owner == user_id and not s.deleted_at
        ]

    async def create(
        self,
        *,
        sessionId: str,
        context_type: str,
        workspace_id: str,
        owner: str,
        title: str,
        pocket: str | None,
        group: str | None,
        agent: str | None,
    ) -> Session:
        self._counter += 1
        sid = f"s{self._counter}"
        now = datetime.now(UTC)
        s = Session(
            id=sid,
            sessionId=sessionId,
            context_type=context_type,
            workspace=workspace_id,
            owner=owner,
            title=title,
            pocket=pocket,
            group=group,
            agent=agent,
            message_count=0,
            last_activity=now,
            created_at=now,
        )
        self.sessions[sid] = s
        return s

    async def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pocket: str | None = None,
    ) -> Session:
        s = self.sessions[session_id]
        kwargs: dict = {}
        if title is not None:
            kwargs["title"] = title
        if pocket is not None:
            kwargs["pocket"] = pocket
        s = replace(s, **kwargs)
        self.sessions[session_id] = s
        return s

    async def soft_delete(self, session_id: str) -> None:
        s = self.sessions[session_id]
        self.sessions[session_id] = replace(s, deleted_at=datetime.now(UTC))


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []

    async def fake_emit(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr("ee.cloud.sessions.service.emit", fake_emit)
    return events


@pytest.fixture
def captured_legacy_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    class _FakeBus:
        async def emit(self, name: str, payload: dict) -> None:
            events.append((name, payload))

    monkeypatch.setattr("ee.cloud.sessions.service.event_bus", _FakeBus())
    return events


@pytest.fixture
def repo() -> ISessionRepository:
    return _FakeRepo()


@pytest.fixture
def service(repo: ISessionRepository) -> SessionService:
    return SessionService(repo)


@pytest.fixture
def reset_default_repo():
    yield
    set_default_repository(MongoSessionRepository())


# ---------------------------------------------------------------------------
# Instance API
# ---------------------------------------------------------------------------


async def test_create_emits_session_created(
    service, captured_events, captured_legacy_events
) -> None:
    s = await service.create(
        _ctx(), "w1", CreateSessionRequest(title="My chat")
    )
    assert s.context_type == "session"
    assert s.title == "My chat"
    assert any(isinstance(e, SessionCreated) for e in captured_events)
    assert any(name == "session.created" for (name, _) in captured_legacy_events)


async def test_create_with_pocket_id_uses_pocket_context(service) -> None:
    s = await service.create(
        _ctx(), "w1", CreateSessionRequest(title="t", pocket_id="p1")
    )
    assert s.context_type == "pocket"
    assert s.pocket == "p1"


async def test_create_with_existing_session_id_returns_existing(service, repo) -> None:
    # Seed existing
    seeded = await service.create(
        _ctx(), "w1", CreateSessionRequest(title="orig", session_id="known-sid")
    )
    # Second call with same session_id reuses
    s2 = await service.create(
        _ctx(), "w1", CreateSessionRequest(title="orig", session_id="known-sid")
    )
    assert s2.id == seeded.id


async def test_create_with_existing_session_id_updates_pocket(service, captured_events) -> None:
    await service.create(
        _ctx(), "w1", CreateSessionRequest(title="t", session_id="known")
    )
    captured_events.clear()
    s2 = await service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="t", session_id="known", pocket_id="p1"),
    )
    assert s2.pocket == "p1"
    assert any(isinstance(e, SessionUpdated) for e in captured_events)


async def test_list_sessions_returns_owners_only(service) -> None:
    await service.create(_ctx("u1"), "w1", CreateSessionRequest(title="a"))
    await service.create(_ctx("u2"), "w1", CreateSessionRequest(title="b"))
    items = await service.list_sessions(_ctx("u1"), "w1")
    assert len(items) == 1
    assert items[0].title == "a"


async def test_update_emits_session_updated(service, captured_events) -> None:
    s = await service.create(_ctx(), "w1", CreateSessionRequest(title="orig"))
    captured_events.clear()
    updated = await service.update(_ctx(), s.id, UpdateSessionRequest(title="new"))
    assert updated.title == "new"
    assert any(isinstance(e, SessionUpdated) for e in captured_events)


async def test_delete_emits_session_deleted(service, captured_events) -> None:
    s = await service.create(_ctx(), "w1", CreateSessionRequest(title="t"))
    captured_events.clear()
    await service.delete(_ctx(), s.id)
    assert any(isinstance(e, SessionDeleted) for e in captured_events)


async def test_get_rejects_other_owner(service) -> None:
    from ee.cloud.shared.errors import Forbidden

    s = await service.create(_ctx("u1"), "w1", CreateSessionRequest(title="t"))
    with pytest.raises(Forbidden):
        await service.get(_ctx("u2"), s.id)


async def test_list_for_pocket_filters(service) -> None:
    await service.create(_ctx(), "w1", CreateSessionRequest(title="a", pocket_id="p1"))
    await service.create(_ctx(), "w1", CreateSessionRequest(title="b", pocket_id="p2"))
    p1_sessions = await service.list_for_pocket(_ctx(), "p1")
    assert len(p1_sessions) == 1
    assert p1_sessions[0].pocket == "p1"


# ---------------------------------------------------------------------------
# Classmethod facade
# ---------------------------------------------------------------------------


async def test_create_default_returns_wire_dict(
    repo, reset_default_repo, captured_events
) -> None:
    set_default_repository(repo)
    out = await SessionService.create_default(
        "w1", "u1", CreateSessionRequest(title="t")
    )
    assert isinstance(out, dict)
    assert set(out.keys()) >= {
        "_id",
        "sessionId",
        "workspace",
        "owner",
        "title",
        "messageCount",
        "lastActivity",
        "createdAt",
    }


async def test_list_for_pocket_default_returns_wire_dicts(
    repo, reset_default_repo
) -> None:
    set_default_repository(repo)
    await SessionService.create_default(
        "w1", "u1", CreateSessionRequest(title="t", pocket_id="p1")
    )
    out = await SessionService.list_for_pocket_default("p1", "u1")
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["pocket"] == "p1"


async def test_create_for_pocket_default_links_pocket(
    repo, reset_default_repo
) -> None:
    set_default_repository(repo)
    out = await SessionService.create_for_pocket_default(
        "w1", "u1", "p1", CreateSessionRequest(title="t")
    )
    assert out["pocket"] == "p1"
