"""Pockets domain — business logic service.

Changes: Added create_from_ripple_spec() static method to PocketService for
auto-creating pockets from agent-generated ripple specs (moved from agent_bridge.py).
"""

from __future__ import annotations

import logging
import secrets

from beanie import PydanticObjectId

from ee.cloud.models.pocket import Pocket
from ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
)
from ee.cloud.ripple_normalizer import normalize_ripple_spec
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

logger = logging.getLogger(__name__)


def _pocket_response(pocket: Pocket) -> dict:
    """Build a frontend-compatible dict from a Pocket document."""
    return {
        "_id": str(pocket.id),
        "workspace": pocket.workspace,
        "name": pocket.name,
        "description": pocket.description,
        "type": pocket.type,
        "icon": pocket.icon,
        "color": pocket.color,
        "owner": pocket.owner,
        "visibility": pocket.visibility,
        "team": pocket.team,
        "agents": pocket.agents,
        "widgets": [w.model_dump(by_alias=True) for w in pocket.widgets],
        "rippleSpec": pocket.rippleSpec,
        "shareLinkToken": pocket.share_link_token,
        "shareLinkAccess": pocket.share_link_access,
        "sharedWith": pocket.shared_with,
        "createdAt": iso_utc(pocket.createdAt),
        "updatedAt": iso_utc(pocket.updatedAt),
    }


def _check_owner(pocket: Pocket, user_id: str) -> None:
    """Raise Forbidden if user is not the pocket owner."""
    if pocket.owner != user_id:
        from pocketpaw.ee.guards.audit import log_denial

        log_denial(
            actor=user_id,
            action="pocket.share",
            code="pocket.not_owner",
            resource_id=str(pocket.id),
        )
        raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")


def _check_edit_access(pocket: Pocket, user_id: str) -> None:
    """Raise Forbidden if user has no edit access (owner or shared_with)."""
    if pocket.owner == user_id:
        return
    if user_id in pocket.shared_with:
        return
    if pocket.visibility == "workspace":
        return
    from pocketpaw.ee.guards.audit import log_denial

    log_denial(
        actor=user_id,
        action="pocket.edit",
        code="pocket.access_denied",
        resource_id=str(pocket.id),
    )
    raise Forbidden("pocket.access_denied", "You do not have edit access to this pocket")


def _check_domain_owner(domain_pocket, user_id: str) -> None:
    """Same owner check, but for a domain ``Pocket`` value object."""
    if domain_pocket.owner != user_id:
        from pocketpaw.ee.guards.audit import log_denial

        log_denial(
            actor=user_id,
            action="pocket.share",
            code="pocket.not_owner",
            resource_id=domain_pocket.id,
        )
        raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")


def _check_domain_edit_access(domain_pocket, user_id: str) -> None:
    """Same edit-access check, but for a domain ``Pocket`` value object.

    Used by the methods migrated to the repository — they receive the
    domain entity (frozen dataclass) rather than the Beanie doc.
    """
    if domain_pocket.owner == user_id:
        return
    if user_id in domain_pocket.shared_with:
        return
    if domain_pocket.visibility == "workspace":
        return
    from pocketpaw.ee.guards.audit import log_denial

    log_denial(
        actor=user_id,
        action="pocket.edit",
        code="pocket.access_denied",
        resource_id=domain_pocket.id,
    )
    raise Forbidden("pocket.access_denied", "You do not have edit access to this pocket")


async def _get_pocket_or_404(pocket_id: str) -> Pocket:
    """Fetch pocket by ID or raise NotFound."""
    pocket = await Pocket.get(PydanticObjectId(pocket_id))
    if not pocket:
        raise NotFound("pocket", pocket_id)
    return pocket


class PocketService:
    """Stateless service encapsulating pocket business logic."""

    # -----------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------

    @staticmethod
    async def create(workspace_id: str, user_id: str, body: CreatePocketRequest) -> dict:
        """Create a pocket with optional agents, widgets, and rippleSpec.

        Phase 8: routes through ``IPocketRepository.create``. Session
        linking still uses the session repository directly.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository
        from ee.cloud.sessions.repositories import (
            get_default_repository as get_session_repo,
        )

        normalized_spec = normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None
        pocket = await get_default_repository().create(
            workspace_id=workspace_id,
            name=body.name,
            owner=user_id,
            description=body.description,
            type=body.type,
            icon=body.icon,
            color=body.color,
            visibility=body.visibility,
            agents=body.agents,
            widgets=body.widgets,
            ripple_spec=normalized_spec,
        )

        if body.session_id:
            session_repo = get_session_repo()
            session = await session_repo.get_by_session_id(body.session_id)
            if session and session.workspace == workspace_id:
                await session_repo.update(session.id, pocket=pocket.id)

        return pocket_to_wire_dict(pocket)

    @staticmethod
    async def list_pockets(workspace_id: str, user_id: str) -> list[dict]:
        """List pockets visible to the user.

        Includes: owned by user, shared with user, or workspace-visible.

        Phase 8: routes through ``IPocketRepository.list_visible_in_workspace``
        + the domain → wire mapper. Wire shape unchanged.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        pockets = await get_default_repository().list_visible_in_workspace(workspace_id, user_id)
        return [pocket_to_wire_dict(p) for p in pockets]

    @staticmethod
    async def get(pocket_id: str, user_id: str) -> dict:
        """Get a single pocket. Checks access.

        Phase 8: routes through ``IPocketRepository.get`` and the
        domain → wire mapper. The remaining 14 PocketService methods
        still call Beanie directly; they will migrate incrementally.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        pocket = await get_default_repository().get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)

        # Access check: owner, shared_with, or workspace-visible
        if (
            pocket.owner != user_id
            and user_id not in pocket.shared_with
            and pocket.visibility == "private"
        ):
            raise Forbidden("pocket.access_denied", "You do not have access to this pocket")

        return pocket_to_wire_dict(pocket)

    @staticmethod
    async def update(pocket_id: str, user_id: str, body: UpdatePocketRequest) -> dict:
        """Update pocket fields. Owner or edit-access users.

        Phase 8: routes through ``IPocketRepository``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)

        # Auth: edit access for non-visibility fields, owner-only for visibility
        if pocket.owner != user_id and user_id not in pocket.shared_with:
            if pocket.visibility != "workspace":
                from pocketpaw.ee.guards.audit import log_denial

                log_denial(
                    actor=user_id,
                    action="pocket.edit",
                    code="pocket.access_denied",
                    resource_id=pocket.id,
                )
                raise Forbidden(
                    "pocket.access_denied", "You do not have edit access to this pocket"
                )
        if body.visibility is not None and pocket.owner != user_id:
            from pocketpaw.ee.guards.audit import log_denial

            log_denial(
                actor=user_id,
                action="pocket.share",
                code="pocket.not_owner",
                resource_id=pocket.id,
            )
            raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")

        normalized_spec = normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None
        updated = await repo.update_fields(
            pocket_id,
            name=body.name,
            description=body.description,
            type=body.type,
            icon=body.icon,
            color=body.color,
            visibility=body.visibility,
            ripple_spec=normalized_spec,
        )
        return pocket_to_wire_dict(updated)

    @staticmethod
    async def delete(pocket_id: str, user_id: str) -> None:
        """Hard-delete a pocket. Owner only.

        Phase 8: routes through ``IPocketRepository``.
        """
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        if pocket.owner != user_id:
            from pocketpaw.ee.guards.audit import log_denial

            log_denial(
                actor=user_id,
                action="pocket.share",
                code="pocket.not_owner",
                resource_id=pocket.id,
            )
            raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")
        await repo.delete(pocket_id)

    # -----------------------------------------------------------------
    # Agent-generated pockets
    # -----------------------------------------------------------------

    @staticmethod
    async def create_from_ripple_spec(
        workspace_id: str,
        owner_id: str,
        ripple_spec: dict,
        description: str = "",
    ) -> str | None:
        """Auto-create a pocket from an agent-generated ripple spec.

        Returns the pocket ID on success, None on failure.

        Phase 8: routes through ``IPocketRepository.create``.
        """
        from ee.cloud.pockets.repositories import get_default_repository

        try:
            normalized = normalize_ripple_spec(ripple_spec)
            if not normalized:
                return None

            name = (
                normalized.get("lifecycle", {}).get("name")
                or normalized.get("name")
                or normalized.get("title")
                or "Agent-generated Pocket"
            )

            pocket = await get_default_repository().create(
                workspace_id=workspace_id,
                name=name,
                owner=owner_id,
                description=description,
                type="ai-generated",
                visibility="workspace",
                ripple_spec=normalized,
            )
            logger.info("Auto-created pocket %s from ripple spec", pocket.id)
            return pocket.id
        except Exception:
            logger.warning("Failed to auto-create pocket from ripple spec", exc_info=True)
            return None

    # -----------------------------------------------------------------
    # Widgets
    # -----------------------------------------------------------------

    @staticmethod
    async def add_widget(pocket_id: str, user_id: str, body: AddWidgetRequest) -> dict:
        """Add a widget to the pocket.

        Phase 8: routes through ``IPocketRepository.add_widget``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        updated = await repo.add_widget(
            pocket_id,
            {
                "name": body.name,
                "type": body.type,
                "icon": body.icon,
                "color": body.color,
                "span": body.span,
                "dataSourceType": body.data_source_type,
                "config": body.config,
                "props": body.props,
                "assignedAgent": body.assigned_agent,
            },
        )
        return pocket_to_wire_dict(updated)

    @staticmethod
    async def update_widget(
        pocket_id: str, widget_id: str, user_id: str, body: UpdateWidgetRequest
    ) -> dict:
        """Update a specific widget inside the pocket.

        Phase 8: routes through ``IPocketRepository.update_widget_fields``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        updated = await repo.update_widget_fields(
            pocket_id,
            widget_id,
            name=body.name,
            type=body.type,
            icon=body.icon,
            config=body.config,
            props=body.props,
            data=body.data,
            assigned_agent=body.assigned_agent,
        )
        return pocket_to_wire_dict(updated)

    @staticmethod
    async def remove_widget(pocket_id: str, widget_id: str, user_id: str) -> dict:
        """Remove a widget from the pocket.

        Phase 8: routes through ``IPocketRepository.remove_widget``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        updated = await repo.remove_widget(pocket_id, widget_id)
        return pocket_to_wire_dict(updated)

    @staticmethod
    async def reorder_widgets(pocket_id: str, user_id: str, widget_ids: list[str]) -> dict:
        """Reorder widgets by the given ordered list of widget IDs.

        Phase 8: routes through ``IPocketRepository.reorder_widgets``.
        Note: legacy behavior tolerated unknown ids by appending leftovers
        at the end. The repository now strictly requires the id set to
        match — callers must include every existing widget id exactly
        once.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        # Preserve the legacy tolerance: missing ids in widget_ids get
        # appended; unknown ids are dropped. Achieve this by computing
        # the full reorder list before calling the strict repo method.
        existing_ids = {w.id for w in pocket.widgets}
        seen: set[str] = set()
        ordered: list[str] = []
        for wid in widget_ids:
            if wid in existing_ids and wid not in seen:
                ordered.append(wid)
                seen.add(wid)
        for w in pocket.widgets:
            if w.id not in seen:
                ordered.append(w.id)
                seen.add(w.id)

        updated = await repo.reorder_widgets(pocket_id, ordered)
        return pocket_to_wire_dict(updated)

    # -----------------------------------------------------------------
    # Sharing — Share links
    # -----------------------------------------------------------------

    @staticmethod
    async def generate_share_link(pocket_id: str, user_id: str, access: str) -> dict:
        """Generate a share link token. Owner only.

        Phase 8: routes through ``IPocketRepository.update_fields``.
        """
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        if pocket.owner != user_id:
            from pocketpaw.ee.guards.audit import log_denial

            log_denial(
                actor=user_id,
                action="pocket.share",
                code="pocket.not_owner",
                resource_id=pocket.id,
            )
            raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")

        token = secrets.token_urlsafe(32)
        await repo.update_fields(pocket_id, share_link_token=token, share_link_access=access)
        return {"token": token, "access": access, "url": f"/shared/{token}"}

    @staticmethod
    async def revoke_share_link(pocket_id: str, user_id: str) -> None:
        """Revoke the share link. Owner only.

        Phase 8: routes through ``IPocketRepository.clear_share_link``.
        """
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        if pocket.owner != user_id:
            from pocketpaw.ee.guards.audit import log_denial

            log_denial(
                actor=user_id,
                action="pocket.share",
                code="pocket.not_owner",
                resource_id=pocket.id,
            )
            raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")
        await repo.clear_share_link(pocket_id)

    @staticmethod
    async def update_share_link(pocket_id: str, user_id: str, access: str) -> dict:
        """Update the share link access level. Owner only.

        Phase 8: routes through ``IPocketRepository.update_fields``.
        """
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        if pocket.owner != user_id:
            from pocketpaw.ee.guards.audit import log_denial

            log_denial(
                actor=user_id,
                action="pocket.share",
                code="pocket.not_owner",
                resource_id=pocket.id,
            )
            raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")
        if not pocket.share_link_token:
            raise NotFound("share_link", pocket_id)

        await repo.update_fields(pocket_id, share_link_access=access)
        return {
            "token": pocket.share_link_token,
            "access": access,
            "url": f"/shared/{pocket.share_link_token}",
        }

    @staticmethod
    async def access_via_share_link(token: str) -> dict:
        """Access a pocket via share link token.

        Phase 8: routes through ``IPocketRepository.find_by_share_link_token``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        pocket = await get_default_repository().find_by_share_link_token(token)
        if pocket is None:
            raise NotFound("pocket", "shared link")
        return pocket_to_wire_dict(pocket)

    # -----------------------------------------------------------------
    # Collaborators
    # -----------------------------------------------------------------

    @staticmethod
    async def add_collaborator(pocket_id: str, user_id: str, body: AddCollaboratorRequest) -> None:
        """Add a collaborator to the pocket. Owner only.

        Phase 8: routes through ``IPocketRepository.add_collaborator``.
        """
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_owner(pocket, user_id)

        await repo.add_collaborator(pocket_id, body.user_id)

        await event_bus.emit(
            "pocket.shared",
            {
                "pocket_id": pocket.id,
                "owner_id": user_id,
                "collaborator_id": body.user_id,
                "access": body.access,
            },
        )

    @staticmethod
    async def remove_collaborator(pocket_id: str, user_id: str, target_user_id: str) -> None:
        """Remove a collaborator from the pocket. Owner only.

        Phase 8: routes through ``IPocketRepository.remove_collaborator``.
        """
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_owner(pocket, user_id)

        await repo.remove_collaborator(pocket_id, target_user_id)

    # -----------------------------------------------------------------
    # Team
    # -----------------------------------------------------------------

    @staticmethod
    async def add_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
        """Add a team member to the pocket. Owner or edit access.

        Phase 8: routes through ``IPocketRepository.add_team_member``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        updated = await repo.add_team_member(pocket_id, member_id)
        return pocket_to_wire_dict(updated)

    @staticmethod
    async def remove_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
        """Remove a team member from the pocket. Owner or edit access.

        Phase 8: routes through ``IPocketRepository.remove_team_member``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        updated = await repo.remove_team_member(pocket_id, member_id)
        return pocket_to_wire_dict(updated)

    # -----------------------------------------------------------------
    # Agents
    # -----------------------------------------------------------------

    @staticmethod
    async def add_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
        """Add an agent to the pocket. Owner or edit access.

        Phase 8: routes through ``IPocketRepository.add_agent``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        updated = await repo.add_agent(pocket_id, agent_id)
        return pocket_to_wire_dict(updated)

    @staticmethod
    async def remove_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
        """Remove an agent from the pocket. Owner or edit access.

        Phase 8: routes through ``IPocketRepository.remove_agent``.
        """
        from ee.cloud.pockets.dto import pocket_to_wire_dict
        from ee.cloud.pockets.repositories import get_default_repository

        repo = get_default_repository()
        pocket = await repo.get(pocket_id)
        if pocket is None:
            raise NotFound("pocket", pocket_id)
        _check_domain_edit_access(pocket, user_id)

        updated = await repo.remove_agent(pocket_id, agent_id)
        return pocket_to_wire_dict(updated)
