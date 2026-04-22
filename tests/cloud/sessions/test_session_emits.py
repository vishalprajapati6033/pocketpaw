"""Tests that SessionService emits realtime events via the bus.

Each mutating SessionService method must fire the appropriate Event class
through ``emit()`` after the DB commit. We patch DB/event primitives at
their seams so we exercise emit behavior in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ee.cloud.realtime.events import SessionCreated, SessionDeleted, SessionUpdated
from ee.cloud.sessions.schemas import CreateSessionRequest, UpdateSessionRequest


def _capture_emits():
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    return recorded, fake_emit


def _make_session(
    *,
    session_oid: str = "s_oid",
    session_id: str = "websocket_abc",
    owner: str = "u1",
    agent: str | None = "agent1",
    pocket: str | None = None,
    group: str | None = None,
    workspace: str = "w1",
    last_activity: datetime | None = None,
    message_count: int = 0,
    deleted_at: datetime | None = None,
):
    s = SimpleNamespace()
    s.id = session_oid
    s.sessionId = session_id
    s.owner = owner
    s.agent = agent
    s.pocket = pocket
    s.group = group
    s.workspace = workspace
    s.title = "New Chat"
    s.lastActivity = last_activity or datetime.now(UTC)
    s.messageCount = message_count
    s.deleted_at = deleted_at
    s.createdAt = datetime.now(UTC)
    s.save = AsyncMock()
    s.insert = AsyncMock()
    return s


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_emits_session_created():
    from ee.cloud.sessions.service import SessionService

    recorded, fake_emit = _capture_emits()

    constructed: list = []

    def fake_session_ctor(*args, **kwargs):
        s = _make_session(
            session_oid="new_oid",
            session_id=kwargs.get("sessionId", "sid"),
            owner=kwargs.get("owner", "u1"),
            agent=kwargs.get("agent"),
            pocket=kwargs.get("pocket"),
            group=kwargs.get("group"),
            workspace=kwargs.get("workspace"),
        )
        constructed.append(s)
        return s

    session_stub = MagicMock(side_effect=fake_session_ctor)
    session_stub.find_one = AsyncMock(return_value=None)
    session_stub.sessionId = MagicMock()

    event_bus_mock = MagicMock()
    event_bus_mock.emit = AsyncMock()

    with (
        patch("ee.cloud.sessions.service.emit", new=fake_emit),
        patch("ee.cloud.sessions.service.Session", new=session_stub),
        patch("ee.cloud.sessions.service.event_bus", new=event_bus_mock),
    ):
        await SessionService.create(
            "w1",
            "u1",
            CreateSessionRequest(title="Chat", agent_id="agent1"),
        )

    created = [e for e in recorded if isinstance(e, SessionCreated)]
    assert len(created) == 1
    data = created[0].data
    assert data["session_id"] == "new_oid"
    assert data["user_id"] == "u1"
    assert data["agent_id"] == "agent1"
    assert data["workspace_id"] == "w1"
    # Plain agent session — no pocket_id key expected
    assert "pocket_id" not in data


# ---------------------------------------------------------------------------
# create_for_pocket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_for_pocket_emits_session_created():
    from ee.cloud.sessions.service import SessionService

    recorded, fake_emit = _capture_emits()

    def fake_session_ctor(*args, **kwargs):
        return _make_session(
            session_oid="pocket_oid",
            session_id=kwargs.get("sessionId", "sid"),
            owner=kwargs.get("owner", "u1"),
            agent=kwargs.get("agent"),
            pocket=kwargs.get("pocket"),
            workspace=kwargs.get("workspace"),
        )

    session_stub = MagicMock(side_effect=fake_session_ctor)
    session_stub.find_one = AsyncMock(return_value=None)
    session_stub.sessionId = MagicMock()

    event_bus_mock = MagicMock()
    event_bus_mock.emit = AsyncMock()

    with (
        patch("ee.cloud.sessions.service.emit", new=fake_emit),
        patch("ee.cloud.sessions.service.Session", new=session_stub),
        patch("ee.cloud.sessions.service.event_bus", new=event_bus_mock),
    ):
        await SessionService.create_for_pocket(
            "w1",
            "u1",
            "pocket_42",
            CreateSessionRequest(title="Chat", agent_id="agent1"),
        )

    created = [e for e in recorded if isinstance(e, SessionCreated)]
    assert len(created) == 1
    data = created[0].data
    assert data["session_id"] == "pocket_oid"
    assert data["user_id"] == "u1"
    assert data["agent_id"] == "agent1"
    assert data["workspace_id"] == "w1"
    assert data["pocket_id"] == "pocket_42"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_emits_session_updated_with_patched_fields():
    from ee.cloud.sessions.service import SessionService

    recorded, fake_emit = _capture_emits()
    session = _make_session(session_oid="s1", owner="u1")

    with (
        patch("ee.cloud.sessions.service.emit", new=fake_emit),
        patch(
            "ee.cloud.sessions.service.SessionService._get_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        await SessionService.update(
            "s1",
            "u1",
            UpdateSessionRequest(title="Renamed"),
        )

    events = [e for e in recorded if isinstance(e, SessionUpdated)]
    assert len(events) == 1
    data = events[0].data
    assert data["session_id"] == "s1"
    assert data["user_id"] == "u1"
    assert data["title"] == "Renamed"
    # pocket_id was not in the request, must not leak
    assert "pocket_id" not in data


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_emits_session_deleted():
    from ee.cloud.sessions.service import SessionService

    recorded, fake_emit = _capture_emits()
    session = _make_session(session_oid="s1", owner="u1")

    with (
        patch("ee.cloud.sessions.service.emit", new=fake_emit),
        patch(
            "ee.cloud.sessions.service.SessionService._get_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        await SessionService.delete("s1", "u1")

    events = [e for e in recorded if isinstance(e, SessionDeleted)]
    assert len(events) == 1
    assert events[0].data == {"session_id": "s1", "user_id": "u1"}


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_emits_session_updated():
    from ee.cloud.sessions.service import SessionService

    recorded, fake_emit = _capture_emits()
    session = _make_session(session_oid="s1", owner="u1")

    session_stub = MagicMock()
    session_stub.find_one = AsyncMock(return_value=session)
    session_stub.sessionId = MagicMock()

    with (
        patch("ee.cloud.sessions.service.emit", new=fake_emit),
        patch("ee.cloud.sessions.service.Session", new=session_stub),
    ):
        await SessionService.touch("websocket_abc")

    events = [e for e in recorded if isinstance(e, SessionUpdated)]
    assert len(events) == 1
    data = events[0].data
    assert data["session_id"] == "s1"
    assert data["user_id"] == "u1"
    assert "last_message_at" in data
    assert isinstance(data["last_message_at"], str)


@pytest.mark.asyncio
async def test_touch_no_emit_when_session_missing():
    from ee.cloud.sessions.service import SessionService

    recorded, fake_emit = _capture_emits()

    session_stub = MagicMock()
    session_stub.find_one = AsyncMock(return_value=None)
    session_stub.sessionId = MagicMock()

    with (
        patch("ee.cloud.sessions.service.emit", new=fake_emit),
        patch("ee.cloud.sessions.service.Session", new=session_stub),
    ):
        await SessionService.touch("unknown_id")

    assert recorded == []
