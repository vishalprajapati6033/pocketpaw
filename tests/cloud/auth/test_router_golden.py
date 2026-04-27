"""Golden-response tests for the auth router profile endpoints.

Avatar upload + serve and the fastapi-users sub-routers (login/logout/
register) are NOT golden-tested here — they integrate with fastapi-
users and the filesystem, both of which require heavier setup.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.http import add_error_handler
from ee.cloud.auth.domain import AuthUser, WorkspaceMembershipRef


class _Repo:
    def __init__(self) -> None:
        self._users: dict[str, AuthUser] = {}

    def seed(self, u: AuthUser) -> None:
        self._users[u.id] = u

    async def get_by_id(self, user_id: str):
        return self._users.get(user_id)

    async def update_profile(
        self,
        user_id: str,
        *,
        full_name=None,
        avatar=None,
        status=None,
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


def _make_user() -> AuthUser:
    return AuthUser(
        id="user-1",
        email="a@b.c",
        full_name="Alice",
        avatar="",
        status="online",
        active_workspace="w1",
        workspaces=(
            WorkspaceMembershipRef(
                workspace="w1",
                role="owner",
                joined_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        is_verified=True,
        is_superuser=False,
    )


@pytest.fixture
def app_with_repo():
    """Boots the auth router under FastAPI with the repo and the
    fastapi-users `current_active_user` dep both overridden so the
    profile endpoints don't need a real JWT."""
    from ee.cloud.auth import current_active_user
    from ee.cloud.auth.router import get_auth_repository, router

    repo = _Repo()
    repo.seed(_make_user())

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")

    class _U:
        id = "user-1"
        active_workspace = "w1"
        workspaces: list = []

    app.dependency_overrides[current_active_user] = lambda: _U()
    app.dependency_overrides[get_auth_repository] = lambda: repo
    return app, repo


def test_get_me_returns_dto_shape(app_with_repo) -> None:
    app, _ = app_with_repo
    resp = TestClient(app).get("/api/v1/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "id",
        "email",
        "name",
        "image",
        "emailVerified",
        "activeWorkspace",
        "workspaces",
    }
    assert body["id"] == "user-1"
    assert body["name"] == "Alice"
    assert body["emailVerified"] is True
    assert body["activeWorkspace"] == "w1"
    assert body["workspaces"] == [{"workspace": "w1", "role": "owner"}]


def test_patch_me_updates_full_name(app_with_repo) -> None:
    app, repo = app_with_repo
    resp = TestClient(app).patch("/api/v1/auth/me", json={"full_name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert repo._users["user-1"].full_name == "Renamed"


def test_set_active_workspace(app_with_repo) -> None:
    app, repo = app_with_repo
    resp = TestClient(app).post("/api/v1/auth/set-active-workspace", json={"workspace_id": "w42"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "activeWorkspace": "w42"}
    assert repo._users["user-1"].active_workspace == "w42"


def test_set_active_workspace_empty_returns_422(app_with_repo) -> None:
    app, _ = app_with_repo
    resp = TestClient(app).post("/api/v1/auth/set-active-workspace", json={"workspace_id": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "workspace_id.required"
