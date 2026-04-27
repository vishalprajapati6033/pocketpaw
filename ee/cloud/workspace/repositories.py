"""Repositories for the workspace domain.

Two protocols:
- ``IWorkspaceRepository`` — Workspace CRUD + membership reads/writes that
  touch the User document (members are stored as embedded
  ``WorkspaceMembership`` rows on User, so workspace-scoped User queries
  live here even though they're not Workspace-document operations).
- ``IInviteRepository`` — Invite CRUD.

The Beanie implementations preserve every behavior of the legacy
service.py: soft-delete cascade, pending-invite expiration check,
member list ordering (insertion).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud.models.invite import Invite as _InviteDoc
from ee.cloud.models.user import User as _UserDoc
from ee.cloud.models.user import WorkspaceMembership as _Membership
from ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from ee.cloud.models.workspace import WorkspaceSettings
from ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember

# ---------------------------------------------------------------------------
# Workspace converters
# ---------------------------------------------------------------------------


def _workspace_to_domain(doc: _WorkspaceDoc, *, member_count: int = 0) -> Workspace:
    return Workspace(
        id=str(doc.id),
        name=doc.name,
        slug=doc.slug,
        owner=doc.owner,
        plan=doc.plan,
        seats=doc.seats,
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        member_count=member_count,
        deleted_at=doc.deleted_at,
    )


def _invite_to_domain(doc: _InviteDoc) -> Invite:
    return Invite(
        id=str(doc.id),
        workspace_id=doc.workspace,
        email=doc.email,
        role=doc.role,
        invited_by=doc.invited_by,
        token=doc.token,
        group_id=doc.group,
        accepted=doc.accepted,
        revoked=doc.revoked,
        expired=doc.expired,
        expires_at=doc.expires_at,
    )


# ---------------------------------------------------------------------------
# Workspace repository
# ---------------------------------------------------------------------------


@runtime_checkable
class IWorkspaceRepository(Protocol):
    # Workspace CRUD
    async def get(self, workspace_id: str) -> Workspace | None: ...
    async def get_by_slug(self, slug: str) -> Workspace | None: ...
    async def create(self, *, name: str, slug: str, owner_user_id: str) -> Workspace: ...
    async def update(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        settings: dict | None = None,
    ) -> Workspace: ...
    async def soft_delete_with_cascade(self, workspace_id: str) -> None: ...
    async def list_for_user(self, user_id: str) -> list[Workspace]: ...
    async def count_members(self, workspace_id: str) -> int: ...

    # Membership operations (touch User docs)
    async def add_member(
        self,
        workspace_id: str,
        user_id: str,
        *,
        role: str,
        set_active: bool = False,
    ) -> None: ...
    async def remove_member(self, workspace_id: str, user_id: str) -> bool: ...
    async def update_member_role(self, workspace_id: str, user_id: str, role: str) -> bool: ...
    async def list_members(self, workspace_id: str) -> list[WorkspaceMember]: ...
    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None: ...
    async def list_member_ids(self, workspace_id: str) -> list[str]: ...
    async def list_admin_ids(self, workspace_id: str) -> list[str]: ...
    async def list_peer_ids(self, user_id: str) -> list[str]: ...
    async def find_user_id_by_email(self, email: str) -> str | None: ...


class MongoWorkspaceRepository:
    """Beanie implementation of `IWorkspaceRepository`."""

    async def get(self, workspace_id: str) -> Workspace | None:
        try:
            doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
        except Exception:
            return None
        if doc is None or doc.deleted_at is not None:
            return None
        count = await self.count_members(workspace_id)
        return _workspace_to_domain(doc, member_count=count)

    async def get_by_slug(self, slug: str) -> Workspace | None:
        doc = await _WorkspaceDoc.find_one(
            _WorkspaceDoc.slug == slug,
            _WorkspaceDoc.deleted_at == None,  # noqa: E711
        )
        if doc is None:
            return None
        count = await self.count_members(str(doc.id))
        return _workspace_to_domain(doc, member_count=count)

    async def create(self, *, name: str, slug: str, owner_user_id: str) -> Workspace:
        doc = _WorkspaceDoc(name=name, slug=slug, owner=owner_user_id)
        await doc.insert()
        return _workspace_to_domain(doc, member_count=0)

    async def update(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        settings: dict | None = None,
    ) -> Workspace:
        from ee.cloud._core.errors import NotFound

        doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
        if doc is None or doc.deleted_at is not None:
            raise NotFound("workspace", workspace_id)
        if name is not None:
            doc.name = name
        if settings is not None:
            doc.settings = WorkspaceSettings(**settings)
        await doc.save()
        count = await self.count_members(workspace_id)
        return _workspace_to_domain(doc, member_count=count)

    async def soft_delete_with_cascade(self, workspace_id: str) -> None:
        from ee.cloud._core.errors import NotFound

        doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
        if doc is None or doc.deleted_at is not None:
            raise NotFound("workspace", workspace_id)

        doc.deleted_at = datetime.now(UTC)
        await doc.save()

        # Cascade: strip workspace from every member's User.workspaces
        members = await _UserDoc.find({"workspaces.workspace": workspace_id}).to_list()
        for member in members:
            before = len(member.workspaces)
            member.workspaces = [m for m in member.workspaces if m.workspace != workspace_id]
            if len(member.workspaces) != before:
                if member.active_workspace == workspace_id:
                    member.active_workspace = (
                        member.workspaces[0].workspace if member.workspaces else None
                    )
                await member.save()

    async def list_for_user(self, user_id: str) -> list[Workspace]:
        try:
            user = await _UserDoc.get(PydanticObjectId(user_id))
        except Exception:
            return []
        if user is None or not user.workspaces:
            return []
        ws_ids = [m.workspace for m in user.workspaces]
        docs = await _WorkspaceDoc.find(
            {
                "_id": {"$in": [PydanticObjectId(wid) for wid in ws_ids]},
                "deleted_at": None,
            }
        ).to_list()
        out: list[Workspace] = []
        for doc in docs:
            count = await self.count_members(str(doc.id))
            out.append(_workspace_to_domain(doc, member_count=count))
        return out

    async def count_members(self, workspace_id: str) -> int:
        return await _UserDoc.find({"workspaces.workspace": workspace_id}).count()

    async def add_member(
        self,
        workspace_id: str,
        user_id: str,
        *,
        role: str,
        set_active: bool = False,
    ) -> None:
        from ee.cloud._core.errors import NotFound

        user = await _UserDoc.get(PydanticObjectId(user_id))
        if user is None:
            raise NotFound("user", user_id)
        # Idempotent: skip if already a member
        if any(m.workspace == workspace_id for m in user.workspaces):
            return
        user.workspaces.append(
            _Membership(
                workspace=workspace_id,
                role=role,
                joined_at=datetime.now(UTC),
            )
        )
        if set_active:
            user.active_workspace = workspace_id
        await user.save()

    async def remove_member(self, workspace_id: str, user_id: str) -> bool:
        user = await _UserDoc.get(PydanticObjectId(user_id))
        if user is None:
            return False
        before = len(user.workspaces)
        user.workspaces = [m for m in user.workspaces if m.workspace != workspace_id]
        if len(user.workspaces) == before:
            return False
        if user.active_workspace == workspace_id:
            user.active_workspace = None
        await user.save()
        return True

    async def update_member_role(self, workspace_id: str, user_id: str, role: str) -> bool:
        user = await _UserDoc.get(PydanticObjectId(user_id))
        if user is None:
            return False
        for m in user.workspaces:
            if m.workspace == workspace_id:
                m.role = role
                await user.save()
                return True
        return False

    async def list_members(self, workspace_id: str) -> list[WorkspaceMember]:
        members = await _UserDoc.find({"workspaces.workspace": workspace_id}).to_list()
        out: list[WorkspaceMember] = []
        for member in members:
            membership = next((m for m in member.workspaces if m.workspace == workspace_id), None)
            if membership is None:
                continue
            out.append(
                WorkspaceMember(
                    user_id=str(member.id),
                    email=member.email,
                    name=member.full_name,
                    avatar=member.avatar,
                    role=membership.role,
                    joined_at=membership.joined_at,
                )
            )
        return out

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        user = await _UserDoc.get(PydanticObjectId(user_id))
        if user is None:
            return None
        for m in user.workspaces:
            if m.workspace == workspace_id:
                return m.role
        return None

    async def list_member_ids(self, workspace_id: str) -> list[str]:
        users = await _UserDoc.find({"workspaces.workspace": workspace_id}).to_list()
        return [str(u.id) for u in users]

    async def list_admin_ids(self, workspace_id: str) -> list[str]:
        users = await _UserDoc.find(
            {
                "workspaces": {
                    "$elemMatch": {
                        "workspace": workspace_id,
                        "role": {"$in": ["owner", "admin"]},
                    }
                }
            }
        ).to_list()
        return [str(u.id) for u in users]

    async def list_peer_ids(self, user_id: str) -> list[str]:
        try:
            me_oid = PydanticObjectId(user_id)
        except Exception:
            return []
        me = await _UserDoc.get(me_oid)
        if me is None or not getattr(me, "workspaces", None):
            return []
        ws_ids = [m.workspace for m in me.workspaces]
        peers = await _UserDoc.find(
            {"workspaces.workspace": {"$in": ws_ids}, "_id": {"$ne": me.id}}
        ).to_list()
        return [str(u.id) for u in peers]

    async def find_user_id_by_email(self, email: str) -> str | None:
        user = await _UserDoc.find_one(_UserDoc.email == email)
        return str(user.id) if user else None


# ---------------------------------------------------------------------------
# Invite repository
# ---------------------------------------------------------------------------


@runtime_checkable
class IInviteRepository(Protocol):
    async def get(self, invite_id: str) -> Invite | None: ...
    async def get_by_token(self, token: str) -> Invite | None: ...
    async def find_pending(
        self, *, workspace_id: str, email: str, group_id: str | None
    ) -> Invite | None: ...
    async def list_pending_for_workspace(self, workspace_id: str) -> list[Invite]: ...
    async def create(
        self,
        *,
        workspace_id: str,
        email: str,
        role: str,
        invited_by: str,
        token: str,
        group_id: str | None,
    ) -> Invite: ...
    async def mark_accepted(self, invite_id: str) -> None: ...
    async def mark_revoked(self, invite_id: str) -> None: ...


class MongoInviteRepository:
    """Beanie implementation of `IInviteRepository`."""

    async def get(self, invite_id: str) -> Invite | None:
        try:
            doc = await _InviteDoc.get(PydanticObjectId(invite_id))
        except Exception:
            return None
        return _invite_to_domain(doc) if doc else None

    async def get_by_token(self, token: str) -> Invite | None:
        doc = await _InviteDoc.find_one(_InviteDoc.token == token)
        return _invite_to_domain(doc) if doc else None

    async def find_pending(
        self, *, workspace_id: str, email: str, group_id: str | None
    ) -> Invite | None:
        query: dict = {
            "workspace": workspace_id,
            "email": email,
            "accepted": False,
            "revoked": False,
            "group": group_id,
        }
        doc = await _InviteDoc.find_one(query)
        return _invite_to_domain(doc) if doc else None

    async def list_pending_for_workspace(self, workspace_id: str) -> list[Invite]:
        docs = await _InviteDoc.find(
            {
                "workspace": workspace_id,
                "accepted": False,
                "revoked": False,
            }
        ).to_list()
        return [_invite_to_domain(d) for d in docs if not d.expired]

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
        doc = _InviteDoc(
            workspace=workspace_id,
            email=email,
            role=role,
            invited_by=invited_by,
            token=token,
            group=group_id,
        )
        await doc.insert()
        return _invite_to_domain(doc)

    async def mark_accepted(self, invite_id: str) -> None:
        from ee.cloud._core.errors import NotFound

        doc = await _InviteDoc.get(PydanticObjectId(invite_id))
        if doc is None:
            raise NotFound("invite", invite_id)
        doc.accepted = True
        await doc.save()

    async def mark_revoked(self, invite_id: str) -> None:
        from ee.cloud._core.errors import NotFound

        doc = await _InviteDoc.get(PydanticObjectId(invite_id))
        if doc is None:
            raise NotFound("invite", invite_id)
        doc.revoked = True
        await doc.save()


# ---------------------------------------------------------------------------
# Default-repo accessors
# ---------------------------------------------------------------------------


_default_workspace: IWorkspaceRepository | None = None
_default_invite: IInviteRepository | None = None


def get_workspace_repository() -> IWorkspaceRepository:
    global _default_workspace
    if _default_workspace is None:
        _default_workspace = MongoWorkspaceRepository()
    return _default_workspace


def get_invite_repository() -> IInviteRepository:
    global _default_invite
    if _default_invite is None:
        _default_invite = MongoInviteRepository()
    return _default_invite


def set_workspace_repository(repo: IWorkspaceRepository) -> None:
    global _default_workspace
    _default_workspace = repo


def set_invite_repository(repo: IInviteRepository) -> None:
    global _default_invite
    _default_invite = repo


__all__ = [
    "IInviteRepository",
    "IWorkspaceRepository",
    "MongoInviteRepository",
    "MongoWorkspaceRepository",
    "get_invite_repository",
    "get_workspace_repository",
    "set_invite_repository",
    "set_workspace_repository",
]
