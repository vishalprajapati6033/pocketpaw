"""Cloud agent chat service — scope resolution, toolset assembly, context.

Keeps the router thin: the router handles HTTP + SSE plumbing; this module
handles *what the agent sees*:

* ``resolve_scope_context`` turns (scope, scope_id, user_id) into a
  ``ScopeContext`` including the target agent id, members, and
  pocket-scoped tool specs where applicable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ee.cloud.shared.errors import CloudError, NotFound


class ScopeKind(StrEnum):
    DM = "dm"
    GROUP = "group"
    POCKET = "pocket"


class InvalidScope(ValueError):
    """Raised when the URL's ``scope`` path param is not one of the known kinds."""


@dataclass
class ScopeContext:
    kind: ScopeKind
    scope_id: str
    workspace_id: str
    user_id: str
    members: list[str]
    target_agent_id: str
    agent_ids_in_scope: list[str] = field(default_factory=list)
    pocket_tool_specs: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Beanie accessors (thin wrappers so tests can patch them)
# ---------------------------------------------------------------------------


async def _get_group(group_id: str) -> Any:
    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    try:
        return await Group.get(PydanticObjectId(group_id))
    except Exception:
        return None


async def _get_pocket(pocket_id: str) -> Any:
    from beanie import PydanticObjectId

    from ee.cloud.models.pocket import Pocket

    try:
        return await Pocket.get(PydanticObjectId(pocket_id))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


async def resolve_scope_context(
    *, scope: str, scope_id: str, user_id: str, agent_id_hint: str | None
) -> ScopeContext:
    """Resolve a ``ScopeContext`` for a cloud agent chat request.

    Raises:
        InvalidScope: ``scope`` is not one of dm/group/pocket.
        NotFound: the group or pocket doesn't exist.
        CloudError: caller is not a member, no agent is in scope, or the
            caller must disambiguate ``agent_id`` for a multi-agent group.
    """
    try:
        kind = ScopeKind(scope)
    except ValueError as e:
        raise InvalidScope(scope) from e

    if kind is ScopeKind.POCKET:
        return await _resolve_pocket(scope_id, user_id, agent_id_hint)
    return await _resolve_group_like(kind, scope_id, user_id, agent_id_hint)


async def _resolve_group_like(
    kind: ScopeKind, scope_id: str, user_id: str, agent_id_hint: str | None
) -> ScopeContext:
    group = await _get_group(scope_id)
    if group is None:
        raise NotFound("group", scope_id)
    if getattr(group, "archived", False):
        raise CloudError(409, "group.archived", "Group is archived")
    members = list(getattr(group, "members", []) or [])
    if user_id not in members:
        raise CloudError(403, "group.not_member", "Caller is not a group member")

    # DM kind must actually be a dm on the document, and vice versa — prevents
    # a caller from driving a normal group through the /dm/ route to bypass
    # multi-agent disambiguation.
    if kind is ScopeKind.DM and getattr(group, "type", "") != "dm":
        raise CloudError(400, "scope.mismatch", "Group is not a DM")
    if kind is ScopeKind.GROUP and getattr(group, "type", "") == "dm":
        raise CloudError(400, "scope.mismatch", "DM must use /dm/ scope")

    agents = list(getattr(group, "agents", []) or [])
    agent_ids = [getattr(a, "agent", None) for a in agents if getattr(a, "agent", None)]
    if not agent_ids:
        raise CloudError(400, "group.no_agent", "No agent in scope")

    target = _pick_target_agent(agent_ids, agent_id_hint)

    return ScopeContext(
        kind=kind,
        scope_id=scope_id,
        workspace_id=str(getattr(group, "workspace", "")),
        user_id=user_id,
        members=members,
        target_agent_id=target,
        agent_ids_in_scope=agent_ids,
    )


async def _resolve_pocket(scope_id: str, user_id: str, agent_id_hint: str | None) -> ScopeContext:
    pocket = await _get_pocket(scope_id)
    if pocket is None:
        raise NotFound("pocket", scope_id)

    team = list(getattr(pocket, "team", []) or [])
    shared = list(getattr(pocket, "shared_with", []) or [])
    owner = getattr(pocket, "owner", None)
    visibility = getattr(pocket, "visibility", "workspace")
    is_member = user_id == owner or user_id in team or user_id in shared
    if visibility == "private" and not is_member:
        raise CloudError(403, "pocket.forbidden", "No access to pocket")
    # For workspace/public we still require the caller be a workspace member;
    # the route-level dependency ``current_workspace_id`` already enforced that.

    agents = list(getattr(pocket, "agents", []) or [])
    agent_ids = [a if isinstance(a, str) else getattr(a, "id", None) for a in agents]
    agent_ids = [a for a in agent_ids if a]
    if not agent_ids:
        raise CloudError(400, "pocket.no_agent", "Pocket has no agent")

    # Pockets default to the first listed agent when no hint is given (unlike
    # groups, which require explicit disambiguation for multi-agent scopes).
    if agent_id_hint is not None:
        if agent_id_hint not in agent_ids:
            raise CloudError(400, "agent.not_in_scope", "agent_id not in scope")
        target = agent_id_hint
    else:
        target = agent_ids[0]

    # Build the participant list: owner first, then team, then shared-with,
    # deduped. Pocket.owner is a required field on the model, so the falsy
    # branch is defensive only. Note: Pocket has no ``archived`` field today,
    # so there's no archived check here (intentional, not a parity gap with
    # the group path).
    seen: set[str] = set()
    members: list[str] = []
    for m in [owner, *team, *shared]:
        if m is None or m in seen:
            continue
        seen.add(m)
        members.append(m)

    return ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id=scope_id,
        workspace_id=str(getattr(pocket, "workspace", "")),
        user_id=user_id,
        members=members,
        target_agent_id=target,
        agent_ids_in_scope=agent_ids,
        pocket_tool_specs=list(getattr(pocket, "tool_specs", []) or []),
    )


def _pick_target_agent(agent_ids: list[str], hint: str | None) -> str:
    if hint is not None:
        if hint not in agent_ids:
            raise CloudError(400, "agent.not_in_scope", "agent_id not in scope")
        return hint
    if len(agent_ids) == 1:
        return agent_ids[0]
    raise CloudError(
        400,
        "agent.ambiguous",
        "Multiple agents in scope — pass agent_id",
    )


# ---------------------------------------------------------------------------
# Toolset assembly
# ---------------------------------------------------------------------------


def _tool_identity(spec: dict[str, Any]) -> tuple:
    """Stable tuple for deduping tool specs of different kinds."""
    kind = spec.get("kind", "")
    if kind == "builtin":
        return ("builtin", spec.get("id", ""))
    if kind == "mcp":
        return ("mcp", spec.get("server", ""), spec.get("name", ""))
    if kind == "inline":
        return ("inline", spec.get("name", ""))
    return (kind, repr(sorted(spec.items())))


def assemble_toolset(ctx: ScopeContext, *, base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge base + pocket-scoped tools. Dedupes by identity, base wins."""
    if ctx.kind is not ScopeKind.POCKET or not ctx.pocket_tool_specs:
        return list(base)
    seen: set[tuple] = {_tool_identity(t) for t in base}
    merged = list(base)
    for spec in ctx.pocket_tool_specs:
        ident = _tool_identity(spec)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(spec)
    return merged


# ---------------------------------------------------------------------------
# Context block for system prompt
# ---------------------------------------------------------------------------


def build_context_block(ctx: ScopeContext) -> str:
    """Compact string the agent prompt embeds so the model knows who is here."""
    member_list = ", ".join(ctx.members) if ctx.members else "(none)"
    return (
        f"<scope>{ctx.kind.value} {ctx.scope_id}</scope>\n"
        f"<participants>{member_list}</participants>"
    )
