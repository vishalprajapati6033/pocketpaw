"""Tests for free-floating 'session' context_type support in Session model + service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ee.cloud.sessions.schemas import CreateSessionRequest


@pytest.mark.asyncio
async def test_create_session_no_pocket_no_group_uses_session_type():
    """When neither pocket_id nor group_id is provided, context_type must be 'session'."""
    from ee.cloud.sessions.service import SessionService

    captured = {}

    class _StubSession:
        def __init__(self, **kw):
            captured.update(kw)
            self.id = "fake-id"
            self.sessionId = kw.get("sessionId", "ws_x")
            self.workspace = kw.get("workspace", "")
            self.owner = kw.get("owner", "")
            self.title = kw.get("title", "")
            self.pocket = kw.get("pocket")
            self.group = kw.get("group")
            self.agent = kw.get("agent")
            self.messageCount = 0
            self.lastActivity = None
            self.createdAt = None
            self.deleted_at = None

        async def insert(self):
            return None

    with (
        patch("ee.cloud.sessions.service.Session", _StubSession),
        patch("ee.cloud.sessions.service.event_bus.emit", AsyncMock()),
        patch("ee.cloud.sessions.service.emit", AsyncMock()),
    ):
        body = CreateSessionRequest(title="Free chat", agent_id="a1")
        await SessionService.create("w1", "u1", body)

    assert captured["context_type"] == "session"
    assert captured.get("pocket") is None
    assert captured.get("group") is None
    assert captured["agent"] == "a1"
    assert captured["workspace"] == "w1"
    assert captured["owner"] == "u1"


@pytest.mark.asyncio
async def test_create_session_with_pocket_id_keeps_pocket_type():
    """Backwards-compat: pocket_id still produces context_type='pocket'."""
    from ee.cloud.sessions.service import SessionService

    captured = {}

    class _StubSession:
        def __init__(self, **kw):
            captured.update(kw)
            self.id = "fake-id"
            self.sessionId = kw.get("sessionId", "ws_x")
            for k, v in kw.items():
                setattr(self, k, v)
            self.messageCount = 0
            self.lastActivity = None
            self.createdAt = None
            self.deleted_at = None

        async def insert(self):
            return None

    with (
        patch("ee.cloud.sessions.service.Session", _StubSession),
        patch("ee.cloud.sessions.service.event_bus.emit", AsyncMock()),
        patch("ee.cloud.sessions.service.emit", AsyncMock()),
    ):
        body = CreateSessionRequest(title="t", pocket_id="p1")
        await SessionService.create("w1", "u1", body)

    assert captured["context_type"] == "pocket"
    assert captured["pocket"] == "p1"


def test_session_model_accepts_session_context_type():
    """Session model validator allows context_type='session' with no pocket/group."""
    from ee.cloud.models.session import Session

    # model_construct bypasses Beanie's __init__ (which requires MongoDB).
    # We call _enforce_context() directly to exercise the validator logic.
    s = Session.model_construct(
        sessionId="ws_test",
        context_type="session",
        workspace="w1",
        owner="u1",
    )
    result = s._enforce_context()
    assert result.context_type == "session"
    assert result.pocket is None
    assert result.group is None


def test_session_model_session_type_rejects_pocket_field():
    """Session model validator rejects context_type='session' when pocket is set."""
    from ee.cloud.models.session import Session

    s = Session.model_construct(
        sessionId="ws_test",
        context_type="session",
        workspace="w1",
        owner="u1",
        pocket="p1",
    )
    with pytest.raises(ValueError):
        s._enforce_context()


def test_session_model_session_type_rejects_group_field():
    """Session model validator rejects context_type='session' when group is set."""
    from ee.cloud.models.session import Session

    s = Session.model_construct(
        sessionId="ws_test",
        context_type="session",
        workspace="w1",
        owner="u1",
        group="g1",
    )
    with pytest.raises(ValueError):
        s._enforce_context()
