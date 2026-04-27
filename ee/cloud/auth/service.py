"""Auth service — profile, active-workspace, avatar.

Refactored in Phase 3 of the cloud-restructure. Instance class taking
``IAuthRepository``. Methods accept ``RequestContext`` and return domain
``AuthUser``.

The router converts the result to ``ProfileOut`` (DTO) at the boundary.
"""

from __future__ import annotations

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud.auth.domain import AuthUser
from ee.cloud.auth.repositories import IAuthRepository


class AuthService:
    def __init__(self, repository: IAuthRepository) -> None:
        self._repo = repository

    async def get_profile(self, ctx: RequestContext) -> AuthUser:
        user = await self._repo.get_by_id(ctx.user_id)
        if user is None:
            raise NotFound("user", ctx.user_id)
        return user

    async def update_profile(
        self,
        ctx: RequestContext,
        *,
        full_name: str | None = None,
        avatar: str | None = None,
        status: str | None = None,
    ) -> AuthUser:
        return await self._repo.update_profile(
            ctx.user_id,
            full_name=full_name,
            avatar=avatar,
            status=status,
        )

    async def set_active_workspace(self, ctx: RequestContext, workspace_id: str) -> AuthUser:
        if not workspace_id:
            raise ValidationError("workspace_id.required", "workspace_id required")
        return await self._repo.set_active_workspace(ctx.user_id, workspace_id)

    async def set_avatar_path(self, ctx: RequestContext, avatar_path: str) -> AuthUser:
        """Persist the avatar URL after the router has written the file
        to disk. Kept separate from ``update_profile`` because the
        avatar upload endpoint owns the file I/O and only needs the
        repo to record the path."""
        return await self._repo.update_profile(ctx.user_id, avatar=avatar_path)
