"""Tests for IAuthRepository via in-memory fake."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ee.cloud.auth.domain import AuthUser, WorkspaceMembershipRef
from ee.cloud.auth.repositories import IAuthRepository


class _InMemoryAuthRepo:
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

        u = self._users[user_id]
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

        u = self._users[user_id]
        u = replace(u, active_workspace=workspace_id)
        self._users[user_id] = u
        return u


def _user(uid: str = "u1") -> AuthUser:
    return AuthUser(
        id=uid,
        email="a@b.c",
        full_name="A",
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
        is_verified=False,
        is_superuser=False,
    )


@pytest.fixture
def repo() -> IAuthRepository:
    return _InMemoryAuthRepo()


async def test_get_by_id_returns_seeded_user(repo) -> None:
    repo.seed(_user())  # type: ignore[attr-defined]
    out = await repo.get_by_id("u1")
    assert out is not None
    assert out.id == "u1"


async def test_get_by_id_none_for_missing(repo) -> None:
    assert await repo.get_by_id("missing") is None


async def test_update_profile_partial(repo) -> None:
    repo.seed(_user())  # type: ignore[attr-defined]
    out = await repo.update_profile("u1", full_name="Updated")
    assert out.full_name == "Updated"
    assert out.avatar == ""  # Untouched


async def test_update_profile_status(repo) -> None:
    repo.seed(_user())  # type: ignore[attr-defined]
    out = await repo.update_profile("u1", status="online")
    assert out.status == "online"


async def test_set_active_workspace(repo) -> None:
    repo.seed(_user())  # type: ignore[attr-defined]
    out = await repo.set_active_workspace("u1", "w42")
    assert out.active_workspace == "w42"
