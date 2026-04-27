"""Repository for the auth domain.

Defines `IAuthRepository` and a Beanie-backed implementation. Services
depend on the Protocol; the router DI's the Beanie impl. Tests use
in-memory fakes.

Note: the underlying Beanie ``User`` document is managed by
fastapi-users. We do not own its lifecycle (registration, password
hashing, JWT) — only its profile fields and workspace membership.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud.auth.domain import AuthUser, WorkspaceMembershipRef
from ee.cloud.models.user import User as _UserDoc


def _membership_to_domain(m) -> WorkspaceMembershipRef:
    return WorkspaceMembershipRef(workspace=m.workspace, role=m.role, joined_at=m.joined_at)


def _to_domain(doc: _UserDoc) -> AuthUser:
    return AuthUser(
        id=str(doc.id),
        email=doc.email,
        full_name=doc.full_name,
        avatar=doc.avatar,
        status=doc.status,
        active_workspace=doc.active_workspace,
        workspaces=tuple(_membership_to_domain(m) for m in doc.workspaces),
        is_verified=doc.is_verified,
        is_superuser=doc.is_superuser,
    )


@runtime_checkable
class IAuthRepository(Protocol):
    async def get_by_id(self, user_id: str) -> AuthUser | None: ...
    async def update_profile(
        self,
        user_id: str,
        *,
        full_name: str | None = None,
        avatar: str | None = None,
        status: str | None = None,
    ) -> AuthUser: ...
    async def set_active_workspace(self, user_id: str, workspace_id: str) -> AuthUser: ...


class BeanieAuthRepository:
    """Beanie-backed implementation of `IAuthRepository`."""

    async def get_by_id(self, user_id: str) -> AuthUser | None:
        doc = await _UserDoc.get(PydanticObjectId(user_id))
        return _to_domain(doc) if doc else None

    async def update_profile(
        self,
        user_id: str,
        *,
        full_name: str | None = None,
        avatar: str | None = None,
        status: str | None = None,
    ) -> AuthUser:
        from ee.cloud._core.errors import NotFound

        doc = await _UserDoc.get(PydanticObjectId(user_id))
        if doc is None:
            raise NotFound("user", user_id)
        if full_name is not None:
            doc.full_name = full_name
        if avatar is not None:
            doc.avatar = avatar
        if status is not None:
            doc.status = status
        await doc.save()
        return _to_domain(doc)

    async def set_active_workspace(self, user_id: str, workspace_id: str) -> AuthUser:
        from ee.cloud._core.errors import NotFound

        doc = await _UserDoc.get(PydanticObjectId(user_id))
        if doc is None:
            raise NotFound("user", user_id)
        doc.active_workspace = workspace_id
        await doc.save()
        return _to_domain(doc)


_default: IAuthRepository | None = None


def get_default_repository() -> IAuthRepository:
    global _default
    if _default is None:
        _default = BeanieAuthRepository()
    return _default


def set_default_repository(repo: IAuthRepository) -> None:
    global _default
    _default = repo


__all__ = [
    "BeanieAuthRepository",
    "IAuthRepository",
    "get_default_repository",
    "set_default_repository",
]
