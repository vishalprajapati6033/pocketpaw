"""Cross-cutting FastAPI dependencies for cloud routers.

These deps belong to no single domain: they extract identity and
workspace from the authed user, and enforce workspace-level role-based
access control. Domain-specific guards (group, agent, pocket) live in
their owning modules; until those modules migrate, they remain in
``ee.cloud.shared.deps``.

The action-based guard machinery (``require_action``, ``require_membership``)
delegates to ``pocketpaw.ee.guards`` (the platform-wide RBAC package) for
the actual policy lookup. We translate platform ``GuardForbidden``
exceptions to cloud-native ``Forbidden`` so the standard error envelope
applies.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException

from ee.cloud._core.errors import Forbidden
from ee.cloud.auth import current_active_user
from ee.cloud.models.user import User
from pocketpaw.ee.guards.audit import log_denial
from pocketpaw.ee.guards.deps import check_workspace_action
from pocketpaw.ee.guards.rbac import Forbidden as GuardForbidden

# ---------------------------------------------------------------------------
# Identity / workspace extraction
# ---------------------------------------------------------------------------


async def current_user(user: User = Depends(current_active_user)) -> User:
    """Get the authenticated user from the JWT/session."""
    return user


async def current_user_id(user: User = Depends(current_active_user)) -> str:
    """Extract user ID from the authenticated user."""
    return str(user.id)


async def current_workspace_id(user: User = Depends(current_active_user)) -> str:
    """Extract active workspace ID from the authenticated user.

    Raises HTTP 400 (not a CloudError — this surfaces as a setup error
    that the client UI handles, not a denial) when the user has no
    active workspace.
    """
    if not user.active_workspace:
        raise HTTPException(400, "No active workspace. Create or join a workspace first.")
    return user.active_workspace


async def optional_workspace_id(
    user: User = Depends(current_active_user),
) -> str | None:
    """Extract workspace ID if set, or None."""
    return user.active_workspace


# ---------------------------------------------------------------------------
# Action-based guards (workspace scope)
# ---------------------------------------------------------------------------


async def _workspace_id_from_path(workspace_id: str) -> str:
    """Pull `workspace_id` from the path. FastAPI binds by parameter name."""
    return workspace_id


_WorkspaceIdDep = Callable[..., Coroutine[Any, Any, str]]


def require_action(
    action: str,
    workspace_dep: _WorkspaceIdDep = _workspace_id_from_path,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """FastAPI dependency enforcing an ACTIONS entry against the caller's
    workspace role.

    Default ``workspace_dep`` reads ``workspace_id`` from the path. Pass
    ``current_workspace_id`` to read from the user's active workspace
    instead.

    On deny, raises the cloud-native ``Forbidden`` (CloudError) so the
    global exception handler emits the standard error envelope. Every
    denial is audited via ``log_denial`` already inside
    ``check_workspace_action``, but we also surface the guard's ``code``
    through the cloud envelope.
    """

    async def _guard(
        user: User = Depends(current_active_user),
        workspace_id: str = Depends(workspace_dep),
    ) -> User:
        try:
            check_workspace_action(user, workspace_id, action)
        except GuardForbidden as exc:
            raise Forbidden(exc.code, exc.detail or "Access denied") from exc
        return user

    _guard.__name__ = f"require_action_{action.replace('.', '_')}"
    return _guard


def require_action_any_workspace(
    action: str,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Variant of ``require_action`` that resolves workspace from the
    user's ``active_workspace``. Use when the route has no
    ``{workspace_id}`` path param."""
    return require_action(action, workspace_dep=current_workspace_id)


async def require_membership(
    user: User = Depends(current_active_user),
    workspace_id: str = Depends(_workspace_id_from_path),
) -> User:
    """Light guard — just asserts the user is a member of the path
    workspace. Used on read routes where any member can view (no role
    check)."""
    for m in user.workspaces:
        if m.workspace == workspace_id:
            return user
    log_denial(
        actor=str(user.id),
        action="workspace.view",
        code="workspace.not_member",
        workspace_id=workspace_id,
    )
    raise Forbidden("workspace.not_member", "Not a member of this workspace")


__all__ = [
    "current_user",
    "current_user_id",
    "current_workspace_id",
    "optional_workspace_id",
    "require_action",
    "require_action_any_workspace",
    "require_membership",
]
