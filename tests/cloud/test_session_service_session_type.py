"""Tests for free-floating 'session' context_type support in Session model + service.

Service-level context_type inference is exercised through the v2
``ISessionRepository`` fake (the bare-Beanie ctor seam was removed
in Phase 9; the legacy classmethod facades route through the
configured default repo).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ee.cloud.sessions.domain import Session
from ee.cloud.sessions.dto import CreateSessionRequest
from ee.cloud.sessions.repositories import set_default_repository


class _FakeSessionRepo:
    """Minimal ``ISessionRepository`` capturing the kwargs passed to
    ``create``. Only the methods exercised by the tests below are
    implemented."""

    def __init__(self) -> None:
        self.created: dict = {}
        self._counter = 0

    async def get(self, session_id: str) -> Session | None:  # pragma: no cover
        return None

    async def get_by_session_id(self, session_id: str) -> Session | None:
        return None

    async def list_for_owner(self, **_: object) -> list[Session]:  # pragma: no cover
        return []

    async def list_by_agent(self, **_: object) -> list[Session]:  # pragma: no cover
        return []

    async def list_for_pocket(self, **_: object) -> list[Session]:  # pragma: no cover
        return []

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
        self.created = {
            "sessionId": sessionId,
            "context_type": context_type,
            "workspace_id": workspace_id,
            "owner": owner,
            "title": title,
            "pocket": pocket,
            "group": group,
            "agent": agent,
        }
        self._counter += 1
        return Session(
            id=f"s{self._counter}",
            sessionId=sessionId,
            context_type=context_type,
            workspace=workspace_id,
            owner=owner,
            title=title,
            pocket=pocket,
            group=group,
            agent=agent,
            message_count=0,
            last_activity=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

    async def update(self, *_args, **_kwargs) -> Session:  # pragma: no cover
        return replace(Session(**self.created), id="x")  # not used

    async def soft_delete(self, *_args) -> None:  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_create_session_no_pocket_no_group_uses_session_type():
    """When neither pocket_id nor group_id is provided, context_type must be 'session'."""
    from unittest.mock import patch

    from ee.cloud.sessions.service import SessionService

    repo = _FakeSessionRepo()
    set_default_repository(repo)

    with (
        patch("ee.cloud.sessions.service.event_bus") as bus_mock,
        patch("ee.cloud.sessions.service.emit"),
    ):
        bus_mock.emit = lambda *a, **kw: _async_noop()
        body = CreateSessionRequest(title="Free chat", agent_id="a1")
        await SessionService.create_default("w1", "u1", body)

    assert repo.created["context_type"] == "session"
    assert repo.created["pocket"] is None
    assert repo.created["group"] is None
    assert repo.created["agent"] == "a1"
    assert repo.created["workspace_id"] == "w1"
    assert repo.created["owner"] == "u1"


@pytest.mark.asyncio
async def test_create_session_with_pocket_id_keeps_pocket_type():
    """Backwards-compat: pocket_id still produces context_type='pocket'."""
    from unittest.mock import patch

    from ee.cloud.sessions.service import SessionService

    repo = _FakeSessionRepo()
    set_default_repository(repo)

    with (
        patch("ee.cloud.sessions.service.event_bus") as bus_mock,
        patch("ee.cloud.sessions.service.emit"),
    ):
        bus_mock.emit = lambda *a, **kw: _async_noop()
        body = CreateSessionRequest(title="t", pocket_id="p1")
        await SessionService.create_default("w1", "u1", body)

    assert repo.created["context_type"] == "pocket"
    assert repo.created["pocket"] == "p1"


async def _async_noop() -> None:
    return None


def test_session_model_accepts_session_context_type():
    """Session model validator allows context_type='session' with no pocket/group."""
    from ee.cloud.models.session import Session as SessionDoc

    # model_construct bypasses Beanie's __init__ (which requires MongoDB).
    # We call _enforce_context() directly to exercise the validator logic.
    s = SessionDoc.model_construct(
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
    from ee.cloud.models.session import Session as SessionDoc

    s = SessionDoc.model_construct(
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
    from ee.cloud.models.session import Session as SessionDoc

    s = SessionDoc.model_construct(
        sessionId="ws_test",
        context_type="session",
        workspace="w1",
        owner="u1",
        group="g1",
    )
    with pytest.raises(ValueError):
        s._enforce_context()
