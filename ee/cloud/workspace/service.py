"""Workspace domain — business logic service.

Refactored in Phase 4 of the cloud-restructure. Instance class taking
`IWorkspaceRepository` + `IInviteRepository`. Methods accept
`RequestContext` and return domain entities; the router maps to DTOs.

The 11 mutating methods have classmethod-`*_default` facades so legacy
callers (chat/router, uploads/router, ee/cloud/__init__, tests) keep
working through the global default repos.

The 3 realtime helpers (``list_member_ids``, ``list_admin_ids``,
``list_peer_ids``) stay as classmethods unchanged because they're pure
queries used as function references by ``realtime/audience.py`` and a
couple of routers — preserving the call signature avoids touching those
sites.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import (
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
)
from ee.cloud.models.notification import NotificationSource
from ee.cloud.notifications import service as notifications_service
from ee.cloud.realtime.bus import get_resolver
from ee.cloud.realtime.emit import emit
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
from ee.cloud.shared.events import event_bus
from ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember
from ee.cloud.workspace.dto import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
    invite_to_dto,
    invite_to_validate_dto,
    member_to_dto,
    workspace_to_dto,
)
from ee.cloud.workspace.repositories import (
    IInviteRepository,
    IWorkspaceRepository,
    get_invite_repository,
    get_workspace_repository,
)

if TYPE_CHECKING:
    from beanie import PydanticObjectId  # noqa: F401

    from ee.cloud.models.user import User


# ---------------------------------------------------------------------------
# Helpers — used by both instance methods and the legacy classmethod facade
# ---------------------------------------------------------------------------


def _legacy_ctx(user: User) -> RequestContext:
    """Build a RequestContext from a Beanie User doc — bridge for the
    legacy ``*_default`` classmethods used by routers/tests that haven't
    migrated to ``request_context``."""
    return RequestContext(
        user_id=str(user.id),
        workspace_id=user.active_workspace,
        request_id="legacy",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WorkspaceService:
    """Workspace + members + invites."""

    def __init__(
        self,
        ws_repo: IWorkspaceRepository,
        invite_repo: IInviteRepository,
    ) -> None:
        self._ws = ws_repo
        self._invites = invite_repo

    # ------------------------------------------------------------------
    # Workspace CRUD (instance API)
    # ------------------------------------------------------------------

    async def create(self, ctx: RequestContext, body: CreateWorkspaceRequest) -> Workspace:
        existing = await self._ws.get_by_slug(body.slug)
        if existing is not None:
            raise ConflictError("workspace.slug_taken", f"Slug '{body.slug}' is already in use")
        ws = await self._ws.create(name=body.name, slug=body.slug, owner_user_id=ctx.user_id)
        await self._ws.add_member(ws.id, ctx.user_id, role="owner", set_active=True)

        await emit(
            WorkspaceMemberAdded(
                data={"workspace_id": ws.id, "user_id": ctx.user_id, "role": "owner"}
            )
        )
        get_resolver().invalidate_workspace(ws.id)

        # Reload to get the post-add member_count
        from dataclasses import replace

        return replace(ws, member_count=1)

    async def get(self, ctx: RequestContext, workspace_id: str) -> Workspace:
        # Membership check
        role = await self._ws.get_member_role(workspace_id, ctx.user_id)
        if role is None:
            raise NotFound("workspace", workspace_id)
        ws = await self._ws.get(workspace_id)
        if ws is None:
            raise NotFound("workspace", workspace_id)
        return ws

    async def update(
        self,
        ctx: RequestContext,
        workspace_id: str,
        body: UpdateWorkspaceRequest,
    ) -> Workspace:
        ws = await self._ws.update(workspace_id, name=body.name, settings=body.settings)
        patched = body.model_dump(exclude_unset=True)
        await emit(WorkspaceUpdated(data={"workspace_id": workspace_id, **patched}))
        return ws

    async def delete(self, ctx: RequestContext, workspace_id: str) -> None:
        await self._ws.soft_delete_with_cascade(workspace_id)
        await emit(WorkspaceDeleted(data={"workspace_id": workspace_id}))
        get_resolver().invalidate_workspace(workspace_id)

    async def list_for_user(self, ctx: RequestContext) -> list[Workspace]:
        return await self._ws.list_for_user(ctx.user_id)

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    async def list_members(self, ctx: RequestContext, workspace_id: str) -> list[WorkspaceMember]:
        # Membership check
        role = await self._ws.get_member_role(workspace_id, ctx.user_id)
        if role is None:
            raise NotFound("workspace", workspace_id)
        return await self._ws.list_members(workspace_id)

    async def update_member_role(
        self,
        workspace_id: str,
        target_user_id: str,
        role: str,
        actor_user_id: str,
    ) -> None:
        ws = await self._ws.get(workspace_id)
        if ws is None:
            raise NotFound("workspace", workspace_id)
        if ws.owner == target_user_id and role != "owner":
            raise Forbidden(
                "workspace.cannot_demote_owner",
                "Cannot demote the workspace owner",
            )
        ok = await self._ws.update_member_role(workspace_id, target_user_id, role)
        if not ok:
            raise NotFound("member", target_user_id)
        await emit(
            WorkspaceMemberRole(
                data={
                    "workspace_id": workspace_id,
                    "user_id": target_user_id,
                    "role": role,
                }
            )
        )
        get_resolver().invalidate_workspace(workspace_id)

    async def remove_member(
        self,
        workspace_id: str,
        target_user_id: str,
        actor_user_id: str,
    ) -> None:
        ws = await self._ws.get(workspace_id)
        if ws is None:
            raise NotFound("workspace", workspace_id)
        if ws.owner == target_user_id:
            raise Forbidden("workspace.cannot_remove_owner", "Cannot remove the workspace owner")
        ok = await self._ws.remove_member(workspace_id, target_user_id)
        if not ok:
            raise NotFound("member", target_user_id)

        await event_bus.emit(
            "member.removed",
            {
                "workspace_id": workspace_id,
                "user_id": target_user_id,
                "removed_by": actor_user_id,
            },
        )
        await emit(
            WorkspaceMemberRemoved(
                data={
                    "workspace_id": workspace_id,
                    "user_id": target_user_id,
                }
            )
        )
        get_resolver().invalidate_workspace(workspace_id)

    # ------------------------------------------------------------------
    # Invites
    # ------------------------------------------------------------------

    async def list_invites(self, workspace_id: str) -> list[Invite]:
        return await self._invites.list_pending_for_workspace(workspace_id)

    async def create_invite(
        self,
        ctx: RequestContext,
        workspace_id: str,
        body: CreateInviteRequest,
    ) -> Invite:
        ws = await self._ws.get(workspace_id)
        if ws is None:
            raise NotFound("workspace", workspace_id)

        if ws.member_count >= ws.seats:
            raise SeatLimitError(ws.seats)

        existing = await self._invites.find_pending(
            workspace_id=workspace_id, email=body.email, group_id=body.group_id
        )
        if existing is not None and not existing.expired:
            msg = f"A pending invite already exists for {body.email}" + (
                " in this group" if body.group_id else ""
            )
            raise ConflictError("invite.already_pending", msg)

        invite = await self._invites.create(
            workspace_id=workspace_id,
            email=body.email,
            role=body.role,
            invited_by=ctx.user_id,
            token=secrets.token_urlsafe(32),
            group_id=body.group_id,
        )

        # Resolve invitee-as-existing-user before emitting so the audience
        # resolver can route the event (via user_id branch) as well as to
        # workspace admins.
        invited_user_id = await self._ws.find_user_id_by_email(body.email)

        event_data: dict = {
            "workspace_id": workspace_id,
            "invite_id": invite.id,
            "email": body.email,
        }
        if invited_user_id:
            event_data["user_id"] = invited_user_id

        await emit(WorkspaceInviteCreated(data=event_data))

        if invited_user_id:
            await notifications_service.create(
                workspace_id=workspace_id,
                recipient=invited_user_id,
                kind="invite",
                title=f"You were invited to join {ws.name}",
                body="",
                source=NotificationSource(type="invite", id=invite.id),
            )

        return invite

    async def validate_invite(self, token: str) -> tuple[Invite, str]:
        """Return ``(invite, workspace_name)``. Raises NotFound if the
        invite token is unknown."""
        invite = await self._invites.get_by_token(token)
        if invite is None:
            raise NotFound("invite")
        ws = await self._ws.get(invite.workspace_id)
        ws_name = ws.name if ws is not None else ""
        return invite, ws_name

    async def accept_invite(self, ctx: RequestContext, token: str) -> None:
        invite = await self._invites.get_by_token(token)
        if invite is None:
            raise NotFound("invite")
        if invite.accepted:
            raise ConflictError(
                "invite.already_accepted",
                "This invite has already been accepted",
            )
        if invite.revoked:
            raise Forbidden("invite.revoked", "This invite has been revoked")
        if invite.expired:
            raise Forbidden("invite.expired", "This invite has expired")

        ws = await self._ws.get(invite.workspace_id)
        if ws is None:
            raise NotFound("workspace", invite.workspace_id)

        already_member = (
            await self._ws.get_member_role(invite.workspace_id, ctx.user_id) is not None
        )
        if not already_member:
            if ws.member_count >= ws.seats:
                raise SeatLimitError(ws.seats)
            await self._ws.add_member(
                invite.workspace_id,
                ctx.user_id,
                role=invite.role,
                set_active=True,
            )

        await self._invites.mark_accepted(invite.id)

        await event_bus.emit(
            "invite.accepted",
            {
                "workspace_id": invite.workspace_id,
                "user_id": ctx.user_id,
                "invite_id": invite.id,
                "group_id": invite.group_id,
            },
        )

        wid = invite.workspace_id
        await emit(
            WorkspaceInviteAccepted(
                data={
                    "workspace_id": wid,
                    "invite_id": invite.id,
                    "user_id": ctx.user_id,
                }
            )
        )
        await emit(
            WorkspaceMemberAdded(
                data={
                    "workspace_id": wid,
                    "user_id": ctx.user_id,
                    "role": invite.role,
                }
            )
        )
        get_resolver().invalidate_workspace(wid)

    async def revoke_invite(self, workspace_id: str, invite_id: str) -> None:
        invite = await self._invites.get(invite_id)
        if invite is None or invite.workspace_id != workspace_id:
            raise NotFound("invite", invite_id)
        await self._invites.mark_revoked(invite_id)
        await emit(
            WorkspaceInviteRevoked(data={"workspace_id": workspace_id, "invite_id": invite_id})
        )

    # ------------------------------------------------------------------
    # Legacy classmethod facade — preserves call signatures used by
    # routers/tests that haven't adopted RequestContext yet. Each
    # classmethod builds a transient instance from the default repos
    # and delegates. Returns the legacy wire-format dict (via DTO mapper)
    # for the methods whose existing callers expect dicts.
    # ------------------------------------------------------------------

    @classmethod
    def _default(cls) -> WorkspaceService:
        return cls(get_workspace_repository(), get_invite_repository())

    @classmethod
    async def create_default(cls, user: User, body: CreateWorkspaceRequest) -> dict:
        ws = await cls._default().create(_legacy_ctx(user), body)
        return workspace_to_dto(ws).model_dump(by_alias=True)

    @classmethod
    async def get_default(cls, workspace_id: str, user: User) -> dict:
        ws = await cls._default().get(_legacy_ctx(user), workspace_id)
        return workspace_to_dto(ws).model_dump(by_alias=True)

    @classmethod
    async def update_default(
        cls, workspace_id: str, user: User, body: UpdateWorkspaceRequest
    ) -> dict:
        ws = await cls._default().update(_legacy_ctx(user), workspace_id, body)
        return workspace_to_dto(ws).model_dump(by_alias=True)

    @classmethod
    async def delete_default(cls, workspace_id: str, user: User) -> None:
        await cls._default().delete(_legacy_ctx(user), workspace_id)

    @classmethod
    async def list_for_user_default(cls, user: User) -> list[dict]:
        items = await cls._default().list_for_user(_legacy_ctx(user))
        return [workspace_to_dto(ws).model_dump(by_alias=True) for ws in items]

    @classmethod
    async def list_members_default(cls, workspace_id: str, user: User) -> list[dict]:
        items = await cls._default().list_members(_legacy_ctx(user), workspace_id)
        return [member_to_dto(m).model_dump(by_alias=True) for m in items]

    @classmethod
    async def update_member_role_default(
        cls,
        workspace_id: str,
        target_user_id: str,
        role: str,
        user: User,
    ) -> None:
        await cls._default().update_member_role(workspace_id, target_user_id, role, str(user.id))

    @classmethod
    async def remove_member_default(
        cls, workspace_id: str, target_user_id: str, user: User
    ) -> None:
        await cls._default().remove_member(workspace_id, target_user_id, str(user.id))

    @classmethod
    async def list_invites_default(cls, workspace_id: str) -> list[dict]:
        items = await cls._default().list_invites(workspace_id)
        return [invite_to_dto(i).model_dump(by_alias=True) for i in items]

    @classmethod
    async def create_invite_default(
        cls, workspace_id: str, user: User, body: CreateInviteRequest
    ) -> dict:
        invite = await cls._default().create_invite(_legacy_ctx(user), workspace_id, body)
        return invite_to_dto(invite).model_dump(by_alias=True)

    @classmethod
    async def validate_invite_default(cls, token: str) -> dict:
        invite, ws_name = await cls._default().validate_invite(token)
        return invite_to_validate_dto(invite, ws_name).model_dump(by_alias=True)

    @classmethod
    async def accept_invite_default(cls, token: str, user: User) -> None:
        await cls._default().accept_invite(_legacy_ctx(user), token)

    @classmethod
    async def revoke_invite_default(cls, workspace_id: str, invite_id: str, user: User) -> None:
        await cls._default().revoke_invite(workspace_id, invite_id)

    # ------------------------------------------------------------------
    # Realtime helpers (audience lookups) — used as function references
    # by realtime/audience.py and a couple of routers. Preserve verbatim.
    # ------------------------------------------------------------------

    @classmethod
    async def list_member_ids(cls, workspace_id: str) -> list[str]:
        return await get_workspace_repository().list_member_ids(workspace_id)

    @classmethod
    async def list_admin_ids(cls, workspace_id: str) -> list[str]:
        return await get_workspace_repository().list_admin_ids(workspace_id)

    @classmethod
    async def list_peer_ids(cls, user_id: str) -> list[str]:
        return await get_workspace_repository().list_peer_ids(user_id)
