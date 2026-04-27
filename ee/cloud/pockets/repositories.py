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
    async def find_by_share_link_token(self, token: str) -> Pocket | None: ...
    async def list_visible_in_workspace(self, workspace_id: str, user_id: str) -> list[Pocket]: ...
    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        owner: str,
        description: str = "",
        type: str = "custom",
        icon: str = "",
        color: str = "",
        visibility: str = "workspace",
        agents: list[str] | None = None,
        widgets: list[dict] | None = None,
        ripple_spec: dict | None = None,
    ) -> Pocket: ...
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
    async def add_widget(self, pocket_id: str, widget_payload: dict) -> Pocket: ...
    async def update_widget_fields(
        self,
        pocket_id: str,
        widget_id: str,
        *,
        name: str | None = None,
        type: str | None = None,
        icon: str | None = None,
        config: dict | None = None,
        props: dict | None = None,
        data: object = None,
        assigned_agent: str | None = None,
    ) -> Pocket: ...
    async def remove_widget(self, pocket_id: str, widget_id: str) -> Pocket: ...
    async def reorder_widgets(self, pocket_id: str, widget_ids: list[str]) -> Pocket: ...
    async def add_collaborator(self, pocket_id: str, target_user_id: str) -> Pocket: ...
    async def remove_collaborator(self, pocket_id: str, target_user_id: str) -> Pocket: ...
    async def add_team_member(self, pocket_id: str, member_id: str) -> Pocket: ...
    async def remove_team_member(self, pocket_id: str, member_id: str) -> Pocket: ...
    async def add_agent(self, pocket_id: str, agent_id: str) -> Pocket: ...
    async def remove_agent(self, pocket_id: str, agent_id: str) -> Pocket: ...


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

    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        owner: str,
        description: str = "",
        type: str = "custom",
        icon: str = "",
        color: str = "",
        visibility: str = "workspace",
        agents: list[str] | None = None,
        widgets: list[dict] | None = None,
        ripple_spec: dict | None = None,
    ) -> Pocket:
        """Insert a new pocket and return its domain projection."""
        widget_docs: list[_WidgetDoc] = []
        for w in widgets or []:
            widget_docs.append(
                _WidgetDoc(
                    name=w.get("name", "Widget"),
                    type=w.get("type", "custom"),
                    icon=w.get("icon", ""),
                    color=w.get("color", ""),
                    span=w.get("span", "col-span-1"),
                    dataSourceType=w.get("dataSourceType", w.get("data_source_type", "static")),
                    config=w.get("config", {}),
                    props=w.get("props", {}),
                    data=w.get("data"),
                    assignedAgent=w.get("assignedAgent", w.get("assigned_agent")),
                )
            )
        doc = _PocketDoc(
            workspace=workspace_id,
            name=name,
            description=description,
            type=type,
            icon=icon,
            color=color,
            owner=owner,
            visibility=visibility,
            agents=list(agents or []),
            widgets=widget_docs,
            rippleSpec=ripple_spec,
        )
        await doc.insert()
        return _pocket_to_domain(doc)

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

    async def add_widget(self, pocket_id: str, widget_payload: dict) -> Pocket:
        """Append a widget to the pocket's embedded widgets array."""
        from ee.cloud._core.errors import NotFound

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        widget = _WidgetDoc(
            name=widget_payload.get("name", "Widget"),
            type=widget_payload.get("type", "custom"),
            icon=widget_payload.get("icon", ""),
            color=widget_payload.get("color", ""),
            span=widget_payload.get("span", "col-span-1"),
            dataSourceType=widget_payload.get(
                "dataSourceType", widget_payload.get("data_source_type", "static")
            ),
            config=widget_payload.get("config", {}),
            props=widget_payload.get("props", {}),
            data=widget_payload.get("data"),
            assignedAgent=widget_payload.get("assignedAgent", widget_payload.get("assigned_agent")),
        )
        doc.widgets.append(widget)
        await doc.save()
        return _pocket_to_domain(doc)

    async def update_widget_fields(
        self,
        pocket_id: str,
        widget_id: str,
        *,
        name: str | None = None,
        type: str | None = None,
        icon: str | None = None,
        config: dict | None = None,
        props: dict | None = None,
        data: object = None,
        assigned_agent: str | None = None,
    ) -> Pocket:
        from ee.cloud._core.errors import NotFound

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        widget = next((w for w in doc.widgets if w.id == widget_id), None)
        if widget is None:
            raise NotFound("widget", widget_id)
        if name is not None:
            widget.name = name
        if type is not None:
            widget.type = type
        if icon is not None:
            widget.icon = icon
        if config is not None:
            widget.config = config
        if props is not None:
            widget.props = props
        if data is not None:
            widget.data = data
        if assigned_agent is not None:
            widget.assignedAgent = assigned_agent
        await doc.save()
        return _pocket_to_domain(doc)

    async def remove_widget(self, pocket_id: str, widget_id: str) -> Pocket:
        from ee.cloud._core.errors import NotFound

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        before = len(doc.widgets)
        doc.widgets = [w for w in doc.widgets if w.id != widget_id]
        if len(doc.widgets) == before:
            raise NotFound("widget", widget_id)
        await doc.save()
        return _pocket_to_domain(doc)

    async def reorder_widgets(self, pocket_id: str, widget_ids: list[str]) -> Pocket:
        from ee.cloud._core.errors import NotFound, ValidationError

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        existing_ids = {w.id for w in doc.widgets}
        if set(widget_ids) != existing_ids:
            raise ValidationError(
                "widget.reorder_mismatch",
                "widget_ids must match the current set exactly",
            )
        widgets_by_id = {w.id: w for w in doc.widgets}
        doc.widgets = [widgets_by_id[wid] for wid in widget_ids]
        await doc.save()
        return _pocket_to_domain(doc)

    async def find_by_share_link_token(self, token: str) -> Pocket | None:
        doc = await _PocketDoc.find_one(_PocketDoc.share_link_token == token)
        return _pocket_to_domain(doc) if doc else None

    async def _mutate_list_field(
        self,
        pocket_id: str,
        field: str,
        value: str,
        action: str,
    ) -> Pocket:
        """Append-or-remove a string value on one of the array fields
        (``shared_with`` / ``team`` / ``agents``). ``action`` is
        ``"add"`` or ``"remove"``; both are idempotent."""
        from ee.cloud._core.errors import NotFound

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is None:
            raise NotFound("pocket", pocket_id)
        current: list[str] = list(getattr(doc, field))
        if action == "add":
            if value not in current:
                current.append(value)
                setattr(doc, field, current)
                await doc.save()
        else:
            if value in current:
                current.remove(value)
                setattr(doc, field, current)
                await doc.save()
        return _pocket_to_domain(doc)

    async def add_collaborator(self, pocket_id: str, target_user_id: str) -> Pocket:
        return await self._mutate_list_field(pocket_id, "shared_with", target_user_id, "add")

    async def remove_collaborator(self, pocket_id: str, target_user_id: str) -> Pocket:
        return await self._mutate_list_field(pocket_id, "shared_with", target_user_id, "remove")

    async def add_team_member(self, pocket_id: str, member_id: str) -> Pocket:
        return await self._mutate_list_field(pocket_id, "team", member_id, "add")

    async def remove_team_member(self, pocket_id: str, member_id: str) -> Pocket:
        return await self._mutate_list_field(pocket_id, "team", member_id, "remove")

    async def add_agent(self, pocket_id: str, agent_id: str) -> Pocket:
        return await self._mutate_list_field(pocket_id, "agents", agent_id, "add")

    async def remove_agent(self, pocket_id: str, agent_id: str) -> Pocket:
        return await self._mutate_list_field(pocket_id, "agents", agent_id, "remove")


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
