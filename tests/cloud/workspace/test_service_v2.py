"""Tests for the refactored WorkspaceService.

Uses in-memory repository fakes — no Beanie patches. Asserts:
- Domain operations (CRUD + members + invites)
- Event emission via realtime.emit + legacy event_bus
- Cache invalidation via get_resolver().invalidate_workspace
- Cross-module side effects (Notification creation on invite-to-existing-user)

Replaces the old ``test_workspace_emits.py`` which patched Beanie
internals that no longer exist post-refactor.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import (
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
)
from ee.cloud.realtime.events import (
    WorkspaceDeleted,
    WorkspaceInviteAccepted,
    WorkspaceInviteCreated,
    WorkspaceInviteRevoked,
    WorkspaceMemberAdded,
    WorkspaceMemberRemoved,
    WorkspaceMemberRole,
    WorkspaceUpdated,
)
from ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember
from ee.cloud.workspace.dto import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)
from ee.cloud.workspace.service import WorkspaceService

# ---------------------------------------------------------------------------
# In-memory repos
# ---------------------------------------------------------------------------


class _WSRepo:
    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.memberships: dict[tuple[str, str], str] = {}  # (ws_id, user_id) -> role
        self.users: dict[str, dict] = {}  # user_id -> {email, name, avatar, joined_at}
        self._counter = 0

    def seed_user(self, user_id: str, email: str = "u@x.c", name: str = "U") -> None:
        self.users[user_id] = {
            "email": email,
            "name": name,
            "avatar": "",
            "joined_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

    def seed_workspace(self, ws: Workspace) -> None:
        self.workspaces[ws.id] = ws

    def seed_membership(self, ws_id: str, user_id: str, role: str) -> None:
        self.memberships[(ws_id, user_id)] = role
        if user_id not in self.users:
            self.seed_user(user_id)

    async def get(self, workspace_id: str) -> Workspace | None:
        ws = self.workspaces.get(workspace_id)
        if ws is None or ws.deleted_at is not None:
            return None
        count = await self.count_members(workspace_id)
        return replace(ws, member_count=count)

    async def get_by_slug(self, slug: str) -> Workspace | None:
        for ws in self.workspaces.values():
            if ws.slug == slug and ws.deleted_at is None:
                return ws
        return None

    async def create(self, *, name: str, slug: str, owner_user_id: str) -> Workspace:
        self._counter += 1
        wid = f"w{self._counter}"
        ws = Workspace(
            id=wid,
            name=name,
            slug=slug,
            owner=owner_user_id,
            plan="team",
            seats=5,
            created_at=datetime.now(UTC),
        )
        self.workspaces[wid] = ws
        return ws

    async def update(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        settings: dict | None = None,
    ) -> Workspace:
        ws = self.workspaces.get(workspace_id)
        if ws is None or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)
        ws = replace(ws, name=name if name is not None else ws.name)
        self.workspaces[workspace_id] = ws
        count = await self.count_members(workspace_id)
        return replace(ws, member_count=count)

    async def soft_delete_with_cascade(self, workspace_id: str) -> None:
        ws = self.workspaces.get(workspace_id)
        if ws is None or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)
        self.workspaces[workspace_id] = replace(ws, deleted_at=datetime.now(UTC))
        # Cascade: drop memberships
        for key in list(self.memberships):
            if key[0] == workspace_id:
                del self.memberships[key]

    async def list_for_user(self, user_id: str) -> list[Workspace]:
        out = []
        for (wid, uid), _ in self.memberships.items():
            if uid != user_id:
                continue
            ws = self.workspaces.get(wid)
            if ws is None or ws.deleted_at is not None:
                continue
            count = await self.count_members(wid)
            out.append(replace(ws, member_count=count))
        return out

    async def count_members(self, workspace_id: str) -> int:
        return sum(1 for (wid, _) in self.memberships if wid == workspace_id)

    async def add_member(
        self,
        workspace_id: str,
        user_id: str,
        *,
        role: str,
        set_active: bool = False,
    ) -> None:
        if (workspace_id, user_id) in self.memberships:
            return
        self.memberships[(workspace_id, user_id)] = role
        if user_id not in self.users:
            self.seed_user(user_id)

    async def remove_member(self, workspace_id: str, user_id: str) -> bool:
        key = (workspace_id, user_id)
        if key not in self.memberships:
            return False
        del self.memberships[key]
        return True

    async def update_member_role(self, workspace_id: str, user_id: str, role: str) -> bool:
        key = (workspace_id, user_id)
        if key not in self.memberships:
            return False
        self.memberships[key] = role
        return True

    async def list_members(self, workspace_id: str) -> list[WorkspaceMember]:
        out = []
        for (wid, uid), role in self.memberships.items():
            if wid != workspace_id:
                continue
            u = self.users.get(uid, {})
            out.append(
                WorkspaceMember(
                    user_id=uid,
                    email=u.get("email", ""),
                    name=u.get("name", ""),
                    avatar=u.get("avatar", ""),
                    role=role,
                    joined_at=u.get("joined_at", datetime(2026, 1, 1, tzinfo=UTC)),
                )
            )
        return out

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        return self.memberships.get((workspace_id, user_id))

    async def list_member_ids(self, workspace_id: str) -> list[str]:
        return [uid for (wid, uid) in self.memberships if wid == workspace_id]

    async def list_admin_ids(self, workspace_id: str) -> list[str]:
        return [
            uid
            for (wid, uid), role in self.memberships.items()
            if wid == workspace_id and role in ("owner", "admin")
        ]

    async def list_peer_ids(self, user_id: str) -> list[str]:
        my_workspaces = {wid for (wid, uid) in self.memberships if uid == user_id}
        out = set()
        for wid, uid in self.memberships:
            if wid in my_workspaces and uid != user_id:
                out.add(uid)
        return list(out)

    async def find_user_id_by_email(self, email: str) -> str | None:
        for uid, info in self.users.items():
            if info.get("email") == email:
                return uid
        return None


class _InviteRepo:
    def __init__(self) -> None:
        self.invites: dict[str, Invite] = {}
        self._counter = 0

    def seed(self, inv: Invite) -> None:
        self.invites[inv.id] = inv

    async def get(self, invite_id: str) -> Invite | None:
        return self.invites.get(invite_id)

    async def get_by_token(self, token: str) -> Invite | None:
        for inv in self.invites.values():
            if inv.token == token:
                return inv
        return None

    async def find_pending(
        self, *, workspace_id: str, email: str, group_id: str | None
    ) -> Invite | None:
        for inv in self.invites.values():
            if (
                inv.workspace_id == workspace_id
                and inv.email == email
                and inv.group_id == group_id
                and not inv.accepted
                and not inv.revoked
            ):
                return inv
        return None

    async def list_pending_for_workspace(self, workspace_id: str) -> list[Invite]:
        return [
            inv
            for inv in self.invites.values()
            if inv.workspace_id == workspace_id
            and not inv.accepted
            and not inv.revoked
            and not inv.expired
        ]

    async def create(
        self,
        *,
        workspace_id: str,
        email: str,
        role: str,
        invited_by: str,
        token: str,
        group_id: str | None,
    ) -> Invite:
        self._counter += 1
        iid = f"i{self._counter}"
        inv = Invite(
            id=iid,
            workspace_id=workspace_id,
            email=email,
            role=role,
            invited_by=invited_by,
            token=token,
            group_id=group_id,
            accepted=False,
            revoked=False,
            expired=False,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        self.invites[iid] = inv
        return inv

    async def mark_accepted(self, invite_id: str) -> None:
        inv = self.invites[invite_id]
        self.invites[invite_id] = replace(inv, accepted=True)

    async def mark_revoked(self, invite_id: str) -> None:
        inv = self.invites[invite_id]
        self.invites[invite_id] = replace(inv, revoked=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(user_id: str = "u1", workspace_id: str | None = None) -> RequestContext:
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

    monkeypatch.setattr("ee.cloud.workspace.service.emit", fake_emit)
    return events


@pytest.fixture
def captured_legacy_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    class _FakeBus:
        async def emit(self, name: str, payload: dict) -> None:
            events.append((name, payload))

    monkeypatch.setattr("ee.cloud.workspace.service.event_bus", _FakeBus())
    return events


@pytest.fixture(autouse=True)
def resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Autouse: every test gets a stub resolver so get_resolver() doesn't
    explode (the real one needs init_realtime which we don't run in unit
    tests)."""
    mock = MagicMock()
    monkeypatch.setattr("ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


@pytest.fixture
def repos() -> tuple[_WSRepo, _InviteRepo]:
    return _WSRepo(), _InviteRepo()


@pytest.fixture
def service(repos) -> WorkspaceService:
    return WorkspaceService(*repos)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_create_emits_member_added_and_invalidates_cache(
    service, repos, captured_events, resolver_mock
) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1", email="a@b.c", name="A")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="Acme", slug="acme"))
    assert ws.name == "Acme"
    assert ws.member_count == 1
    assert any(isinstance(e, WorkspaceMemberAdded) for e in captured_events)
    resolver_mock.invalidate_workspace.assert_called_once_with(ws.id)


async def test_create_rejects_duplicate_slug(service, repos) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    with pytest.raises(ConflictError):
        await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A2", slug="a"))


async def test_update_emits_workspace_updated(service, repos, captured_events) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    captured_events.clear()
    await service.update(_ctx("u1"), ws.id, UpdateWorkspaceRequest(name="B"))
    assert any(isinstance(e, WorkspaceUpdated) for e in captured_events)


async def test_delete_cascades_and_emits(service, repos, captured_events, resolver_mock) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws_repo.seed_user("u2")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    ws_repo.seed_membership(ws.id, "u2", "member")
    captured_events.clear()
    await service.delete(_ctx("u1"), ws.id)
    assert any(isinstance(e, WorkspaceDeleted) for e in captured_events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)
    # Cascade: u2's membership is gone
    assert await ws_repo.get_member_role(ws.id, "u2") is None


async def test_update_member_role_blocks_demoting_owner(service, repos) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    with pytest.raises(Forbidden) as exc:
        await service.update_member_role(ws.id, "u1", "member", "u1")
    assert exc.value.code == "workspace.cannot_demote_owner"


async def test_update_member_role_emits_role_event(
    service, repos, captured_events, resolver_mock
) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    ws_repo.seed_membership(ws.id, "u2", "member")
    captured_events.clear()
    resolver_mock.invalidate_workspace.reset_mock()
    await service.update_member_role(ws.id, "u2", "admin", "u1")
    assert any(isinstance(e, WorkspaceMemberRole) for e in captured_events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)


async def test_remove_member_blocks_owner(service, repos) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    with pytest.raises(Forbidden) as exc:
        await service.remove_member(ws.id, "u1", "u1")
    assert exc.value.code == "workspace.cannot_remove_owner"


async def test_remove_member_emits_both_paths(
    service, repos, captured_events, captured_legacy_events, resolver_mock
) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    ws_repo.seed_membership(ws.id, "u2", "member")
    captured_events.clear()
    captured_legacy_events.clear()
    resolver_mock.invalidate_workspace.reset_mock()

    await service.remove_member(ws.id, "u2", "u1")
    assert any(isinstance(e, WorkspaceMemberRemoved) for e in captured_events)
    assert any(name == "member.removed" for (name, _) in captured_legacy_events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)


async def test_create_invite_seat_limit(service, repos) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    # Saturate seats
    for i in range(ws.seats):
        ws_repo.seed_membership(ws.id, f"u{i + 10}", "member")
    with pytest.raises(SeatLimitError):
        await service.create_invite(_ctx("u1"), ws.id, CreateInviteRequest(email="x@y.z"))


async def test_create_invite_rejects_duplicate_pending(service, repos) -> None:
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    await service.create_invite(_ctx("u1"), ws.id, CreateInviteRequest(email="x@y.z"))
    with pytest.raises(ConflictError) as exc:
        await service.create_invite(_ctx("u1"), ws.id, CreateInviteRequest(email="x@y.z"))
    assert exc.value.code == "invite.already_pending"


async def test_create_invite_emits_invite_created(
    service, repos, captured_events, monkeypatch
) -> None:
    monkeypatch.setattr(
        "ee.cloud.workspace.service.NotificationService.create_default",
        _async_noop,
    )
    ws_repo, _ = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    captured_events.clear()
    await service.create_invite(_ctx("u1"), ws.id, CreateInviteRequest(email="x@y.z"))
    assert any(isinstance(e, WorkspaceInviteCreated) for e in captured_events)


async def test_create_invite_to_existing_user_includes_user_id_and_notifies(
    service, repos, captured_events, monkeypatch
) -> None:
    notifications: list = []

    async def fake_notify(**kwargs):
        notifications.append(kwargs)

    monkeypatch.setattr(
        "ee.cloud.workspace.service.NotificationService.create_default",
        fake_notify,
    )
    ws_repo, _ = repos
    ws_repo.seed_user("u1", email="owner@x.c")
    ws_repo.seed_user("u2", email="invitee@x.c")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    captured_events.clear()
    await service.create_invite(_ctx("u1"), ws.id, CreateInviteRequest(email="invitee@x.c"))

    created = next(e for e in captured_events if isinstance(e, WorkspaceInviteCreated))
    assert created.data.get("user_id") == "u2"
    assert len(notifications) == 1
    assert notifications[0]["recipient"] == "u2"


async def test_accept_invite_adds_member_and_emits(
    service, repos, captured_events, monkeypatch, resolver_mock
) -> None:
    monkeypatch.setattr(
        "ee.cloud.workspace.service.NotificationService.create_default",
        _async_noop,
    )
    ws_repo, invite_repo = repos
    ws_repo.seed_user("u1")
    ws_repo.seed_user("u2", email="u2@b.c")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    invite = await service.create_invite(_ctx("u1"), ws.id, CreateInviteRequest(email="u2@b.c"))
    captured_events.clear()
    resolver_mock.invalidate_workspace.reset_mock()
    await service.accept_invite(_ctx("u2"), invite.token)

    assert await ws_repo.get_member_role(ws.id, "u2") == invite.role
    assert any(isinstance(e, WorkspaceInviteAccepted) for e in captured_events)
    assert any(isinstance(e, WorkspaceMemberAdded) for e in captured_events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)


async def test_accept_invite_rejects_revoked(service, repos) -> None:
    ws_repo, invite_repo = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    invite_repo.seed(
        Invite(
            id="i1",
            workspace_id=ws.id,
            email="x@y.z",
            role="member",
            invited_by="u1",
            token="tok",
            group_id=None,
            accepted=False,
            revoked=True,
            expired=False,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    with pytest.raises(Forbidden) as exc:
        await service.accept_invite(_ctx("u2"), "tok")
    assert exc.value.code == "invite.revoked"


async def test_revoke_invite_emits_invite_revoked(service, repos, captured_events) -> None:
    ws_repo, invite_repo = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="A", slug="a"))
    invite_repo.seed(
        Invite(
            id="i1",
            workspace_id=ws.id,
            email="x@y.z",
            role="member",
            invited_by="u1",
            token="tok",
            group_id=None,
            accepted=False,
            revoked=False,
            expired=False,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    captured_events.clear()
    await service.revoke_invite(ws.id, "i1")
    assert any(isinstance(e, WorkspaceInviteRevoked) for e in captured_events)


async def test_validate_invite_returns_workspace_name(service, repos) -> None:
    ws_repo, invite_repo = repos
    ws_repo.seed_user("u1")
    ws = await service.create(_ctx("u1"), CreateWorkspaceRequest(name="Acme", slug="acme"))
    invite_repo.seed(
        Invite(
            id="i1",
            workspace_id=ws.id,
            email="x@y.z",
            role="member",
            invited_by="u1",
            token="tok",
            group_id=None,
            accepted=False,
            revoked=False,
            expired=False,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    invite, ws_name = await service.validate_invite("tok")
    assert invite.id == "i1"
    assert ws_name == "Acme"


async def test_validate_invite_unknown_token_raises_not_found(service) -> None:
    with pytest.raises(NotFound):
        await service.validate_invite("nope")


async def _async_noop(*_args, **_kwargs):
    return None
