"""Workspace domain — FastAPI router.

Refactored in Phase 4 of the cloud-restructure. Authorization is
declared at the route level via ``require_action(...)``. Service
methods take ``RequestContext`` and return domain entities; the router
maps to DTOs at the boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud._core.deps import (
    current_user,
    require_action,
    require_membership,
)
from ee.cloud.license import require_license
from ee.cloud.models.user import User
from ee.cloud.workspace.dto import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    InviteOut,
    MemberOut,
    UpdateMemberRoleRequest,
    UpdateWorkspaceRequest,
    ValidateInviteOut,
    WorkspaceOut,
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
from ee.cloud.workspace.service import WorkspaceService

router = APIRouter(
    prefix="/workspaces", tags=["Workspace"], dependencies=[Depends(require_license)]
)


def get_workspace_service(
    ws_repo: IWorkspaceRepository = Depends(get_workspace_repository),
    invite_repo: IInviteRepository = Depends(get_invite_repository),
) -> WorkspaceService:
    return WorkspaceService(ws_repo, invite_repo)


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=WorkspaceOut)
async def create_workspace(
    body: CreateWorkspaceRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(current_user),  # legacy presence — drives the auth chain
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceOut:
    ws = await service.create(ctx, body)
    return workspace_to_dto(ws)


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(current_user),
    service: WorkspaceService = Depends(get_workspace_service),
) -> list[WorkspaceOut]:
    items = await service.list_for_user(ctx)
    return [workspace_to_dto(ws) for ws in items]


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceOut:
    ws = await service.get(ctx, workspace_id)
    return workspace_to_dto(ws)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.update")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceOut:
    ws = await service.update(ctx, workspace_id, body)
    return workspace_to_dto(ws)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.delete")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> Response:
    await service.delete(ctx, workspace_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
    service: WorkspaceService = Depends(get_workspace_service),
) -> list[MemberOut]:
    items = await service.list_members(ctx, workspace_id)
    return [member_to_dto(m) for m in items]


@router.patch("/{workspace_id}/members/{user_id}")
async def update_member_role(
    workspace_id: str,
    user_id: str,
    body: UpdateMemberRoleRequest,
    user: User = Depends(require_action("workspace.member.role_change")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> dict:
    await service.update_member_role(workspace_id, user_id, body.role, str(user.id))
    return {"ok": True}


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(
    workspace_id: str,
    user_id: str,
    user: User = Depends(require_action("workspace.member.remove")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> Response:
    await service.remove_member(workspace_id, user_id, str(user.id))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/invites", response_model=list[InviteOut])
async def list_invites(
    workspace_id: str,
    user: User = Depends(require_action("invite.create")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> list[InviteOut]:
    items = await service.list_invites(workspace_id)
    return [invite_to_dto(i) for i in items]


@router.post("/{workspace_id}/invites", response_model=InviteOut)
async def create_invite(
    workspace_id: str,
    body: CreateInviteRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("invite.create")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> InviteOut:
    invite = await service.create_invite(ctx, workspace_id, body)
    return invite_to_dto(invite)


@router.get("/invites/{token}", response_model=ValidateInviteOut)
async def validate_invite(
    token: str,
    service: WorkspaceService = Depends(get_workspace_service),
) -> ValidateInviteOut:
    invite, ws_name = await service.validate_invite(token)
    return invite_to_validate_dto(invite, ws_name)


@router.post("/invites/{token}/accept")
async def accept_invite(
    token: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(current_user),
    service: WorkspaceService = Depends(get_workspace_service),
) -> dict:
    await service.accept_invite(ctx, token)
    return {"ok": True}


@router.delete("/{workspace_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    workspace_id: str,
    invite_id: str,
    user: User = Depends(require_action("invite.revoke")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> Response:
    await service.revoke_invite(workspace_id, invite_id)
    return Response(status_code=204)
