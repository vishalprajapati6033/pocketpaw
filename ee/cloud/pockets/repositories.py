"""Repository for the pockets module.

Phase 8 ships the basic repository contract. The Beanie implementation
covers the simple read paths first (get / list_for_workspace_visible).
Mutations stay on Beanie via the existing PocketService classmethods
until incrementally migrated.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud.models.pocket import Pocket as _PocketDoc
from ee.cloud.models.pocket import Widget as _WidgetDoc
from ee.cloud.pockets.domain import Pocket, Widget, WidgetPosition


def _widget_to_domain(w: _WidgetDoc) -> Widget:
    return Widget(
        id=w.id,
        name=w.name,
        type=w.type,
        icon=w.icon,
        color=w.color,
        span=w.span,
        data_source_type=w.dataSourceType,
        config=tuple(w.config.items()),
        props=tuple(w.props.items()),
        data=w.data,
        assigned_agent=w.assignedAgent,
        position=WidgetPosition(row=w.position.row, col=w.position.col),
    )


def _pocket_to_domain(doc: _PocketDoc) -> Pocket:
    return Pocket(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        description=doc.description,
        type=doc.type,
        icon=doc.icon,
        color=doc.color,
        owner=doc.owner,
        visibility=doc.visibility,
        team=tuple(str(t) for t in doc.team),
        agents=tuple(str(a) for a in doc.agents),
        widgets=tuple(_widget_to_domain(w) for w in doc.widgets),
        ripple_spec=doc.rippleSpec,
        share_link_token=doc.share_link_token,
        share_link_access=doc.share_link_access,
        shared_with=tuple(doc.shared_with),
        tool_specs=tuple(doc.tool_specs),
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


@runtime_checkable
class IPocketRepository(Protocol):
    async def get(self, pocket_id: str) -> Pocket | None: ...
    async def list_visible_in_workspace(self, workspace_id: str, user_id: str) -> list[Pocket]: ...
    async def update_fields(
        self,
        pocket_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        type: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        visibility: str | None = None,
        ripple_spec: dict | None = None,
        share_link_token: str | None = None,
        share_link_access: str | None = None,
    ) -> Pocket: ...
    async def delete(self, pocket_id: str) -> None: ...
    async def clear_share_link(self, pocket_id: str) -> None: ...


class MongoPocketRepository:
    """Beanie-backed implementation."""

    async def get(self, pocket_id: str) -> Pocket | None:
        try:
            doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        except Exception:
            return None
        return _pocket_to_domain(doc) if doc else None

    async def list_visible_in_workspace(self, workspace_id: str, user_id: str) -> list[Pocket]:
        """Pockets visible to the user in a workspace: owner, shared_with,
        or workspace-visible."""
        docs = await _PocketDoc.find(
            {
                "workspace": workspace_id,
                "$or": [
                    {"owner": user_id},
                    {"shared_with": user_id},
                    {"visibility": "workspace"},
                ],
            }
        ).to_list()
        return [_pocket_to_domain(d) for d in docs]

    async def update_fields(
        self,
        pocket_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        type: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        visibility: str | None = None,
        ripple_spec: dict | None = None,
        share_link_token: str | None = None,
        share_link_access: str | None = None,
    ) -> Pocket:
        from ee.cloud._core.errors import NotFound

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        if name is not None:
            doc.name = name
        if description is not None:
            doc.description = description
        if type is not None:
            doc.type = type
        if icon is not None:
            doc.icon = icon
        if color is not None:
            doc.color = color
        if visibility is not None:
            doc.visibility = visibility
        if ripple_spec is not None:
            doc.rippleSpec = ripple_spec
        if share_link_token is not None:
            doc.share_link_token = share_link_token
        if share_link_access is not None:
            doc.share_link_access = share_link_access
        await doc.save()
        return _pocket_to_domain(doc)

    async def delete(self, pocket_id: str) -> None:
        from ee.cloud._core.errors import NotFound

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        await doc.delete()

    async def clear_share_link(self, pocket_id: str) -> None:
        """Set share_link_token to None and reset access to 'view'.
        Separate from update_fields because the latter treats ``None`` as
        'unchanged' rather than 'set to null'."""
        from ee.cloud._core.errors import NotFound

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        doc.share_link_token = None
        doc.share_link_access = "view"
        await doc.save()


_default: IPocketRepository | None = None


def get_default_repository() -> IPocketRepository:
    global _default
    if _default is None:
        _default = MongoPocketRepository()
    return _default


def set_default_repository(repo: IPocketRepository) -> None:
    global _default
    _default = repo


__all__ = [
    "IPocketRepository",
    "MongoPocketRepository",
    "get_default_repository",
    "set_default_repository",
]
