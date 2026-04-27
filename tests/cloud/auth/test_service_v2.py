"""Tests for the refactored AuthService."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud.auth.domain import AuthUser, WorkspaceMembershipRef
from ee.cloud.auth.service import AuthService


class _Repo:
    def __init__(self) -> None:
        self._users: dict[str, AuthUser] = {}

    def seed(self, u: AuthUser) -> None:
        self._users[u.id] = u

    async def get_by_id(self, user_id: str) -> AuthUser | None:
        return self._users.get(user_id)

    async def update_profile(
        self,
        user_id: str,
        *,
        full_name: str | None = None,
        avatar: str | None = None,
        status: str | None = None,
    ) -> AuthUser:
        from dataclasses import replace

        u = self._users.get(user_id)
        if u is None:
            raise NotFound("user", user_id)
        u = replace(
            u,
            full_name=full_name if full_name is not None else u.full_name,
            avatar=avatar if avatar is not None else u.avatar,
            status=status if status is not None else u.status,
        )
        self._users[user_id] = u
        return u

    async def set_active_workspace(self, user_id: str, workspace_id: str) -> AuthUser:
        from dataclasses import replace

        u = self._users.get(user_id)
        if u is None:
            raise NotFound("user", user_id)
        u = replace(u, active_workspace=workspace_id)
        self._users[user_id] = u
        return u


def _ctx(user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=None,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime(2026, 4, 27, tzinfo=UTC),
    )


def _user(uid: str = "u1", **overrides) -> AuthUser:
    base = dict(
        id=uid,
        email="a@b.c",
        full_name="Alice",
        avatar="",
        status="offline",
        active_workspace=None,
        workspaces=(
            WorkspaceMembershipRef(
                workspace="w1",
                role="member",
                joined_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        is_verified=True,
        is_superuser=False,
    )
    base.update(overrides)
    return AuthUser(**base)


@pytest.fixture
def repo() -> _Repo:
    r = _Repo()
    r.seed(_user())
    return r


@pytest.fixture
def service(repo: _Repo) -> AuthService:
    return AuthService(repo)


async def test_get_profile_returns_user(service) -> None:
    out = await service.get_profile(_ctx())
    assert out.id == "u1"
    assert out.email == "a@b.c"


async def test_get_profile_raises_not_found(service) -> None:
    with pytest.raises(NotFound):
        await service.get_profile(_ctx("missing"))


async def test_update_profile_changes_full_name(service) -> None:
    out = await service.update_profile(_ctx(), full_name="Bob")
    assert out.full_name == "Bob"


async def test_update_profile_partial_only_full_name(service, repo) -> None:
    await service.update_profile(_ctx(), full_name="Bob")
    u = await repo.get_by_id("u1")
    assert u is not None
    assert u.full_name == "Bob"
    assert u.avatar == ""  # untouched


async def test_set_active_workspace_persists(service) -> None:
    out = await service.set_active_workspace(_ctx(), "w42")
    assert out.active_workspace == "w42"


async def test_set_active_workspace_empty_raises_validation(service) -> None:
    with pytest.raises(ValidationError):
        await service.set_active_workspace(_ctx(), "")


async def test_set_avatar_path_persists(service) -> None:
    out = await service.set_avatar_path(_ctx(), "/api/v1/auth/avatar/u1.png")
    assert out.avatar == "/api/v1/auth/avatar/u1.png"
