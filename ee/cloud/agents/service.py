"""Agents domain — business logic service.

Refactored in Phase 6 of the cloud-restructure. Instance class taking
``IAgentRepository``. Methods accept ``RequestContext`` and return
domain entities; the router maps to legacy wire dicts via
``agent_to_dict``.

Eager soul materialization (``get_agent_pool().ensure_soul``) preserved
on create when ``soul_enabled``.

The legacy classmethod facade (``create_default``, ``list_agents_default``,
etc.) preserves the existing wire shape for the sole external caller —
the agents/router.py — until it migrates to the instance API.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import ConflictError, Forbidden, NotFound
from ee.cloud.agents.domain import Agent, AgentConfigSpec
from ee.cloud.agents.dto import (
    CreateAgentRequest,
    DiscoverRequest,
    UpdateAgentRequest,
    agent_to_dict,
)
from ee.cloud.agents.repositories import IAgentRepository, get_default_repository
from ee.cloud.agents.scope_rules import normalise_and_validate_scopes

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _build_create_config(body: CreateAgentRequest) -> AgentConfigSpec:
    """Build a domain AgentConfigSpec from a CreateAgentRequest."""
    base = AgentConfigSpec(
        backend=body.backend,
        model=body.model,
        system_prompt=body.system_prompt,
        soul_enabled=body.soul_enabled,
        soul_persona=body.persona,
        soul_archetype=body.soul_archetype or f"The {body.name}",
    )
    overrides = {}
    if body.temperature is not None:
        overrides["temperature"] = body.temperature
    if body.max_tokens is not None:
        overrides["max_tokens"] = body.max_tokens
    if body.tools is not None:
        overrides["tools"] = tuple(body.tools)
    if body.trust_level is not None:
        overrides["trust_level"] = body.trust_level
    if body.scopes is not None:
        overrides["scopes"] = tuple(body.scopes)
    if body.soul_values is not None:
        overrides["soul_values"] = tuple(body.soul_values)
    if body.soul_ocean is not None:
        overrides["soul_ocean"] = tuple(body.soul_ocean.items())
    return replace(base, **overrides) if overrides else base


def _apply_update(current: AgentConfigSpec, body: UpdateAgentRequest) -> AgentConfigSpec:
    """Apply config-shaped overrides from an UpdateAgentRequest. Returns
    a new AgentConfigSpec — caller decides whether to persist."""
    if body.config is not None:
        # Full replacement
        c = body.config
        return AgentConfigSpec(
            backend=c.get("backend", current.backend),
            model=c.get("model", current.model),
            system_prompt=c.get("system_prompt", current.system_prompt),
            tools=tuple(c.get("tools", list(current.tools))),
            trust_level=c.get("trust_level", current.trust_level),
            temperature=c.get("temperature", current.temperature),
            max_tokens=c.get("max_tokens", current.max_tokens),
            scopes=tuple(c.get("scopes", list(current.scopes))),
            soul_enabled=c.get("soul_enabled", current.soul_enabled),
            soul_persona=c.get("soul_persona", current.soul_persona),
            soul_archetype=c.get("soul_archetype", current.soul_archetype),
            soul_values=tuple(c.get("soul_values", list(current.soul_values))),
            soul_ocean=tuple(
                c.get("soul_ocean", dict(current.soul_ocean)).items()
                if isinstance(c.get("soul_ocean", dict(current.soul_ocean)), dict)
                else current.soul_ocean
            ),
        )

    overrides: dict = {}
    for field, attr in [
        ("backend", body.backend),
        ("model", body.model),
        ("system_prompt", body.system_prompt),
        ("temperature", body.temperature),
        ("max_tokens", body.max_tokens),
        ("trust_level", body.trust_level),
        ("soul_enabled", body.soul_enabled),
        ("soul_archetype", body.soul_archetype),
    ]:
        if attr is not None:
            overrides[field] = attr
    if body.tools is not None:
        overrides["tools"] = tuple(body.tools)
    if body.scopes is not None:
        overrides["scopes"] = tuple(body.scopes)
    if body.soul_values is not None:
        overrides["soul_values"] = tuple(body.soul_values)
    if body.soul_ocean is not None:
        overrides["soul_ocean"] = tuple(body.soul_ocean.items())
    if body.persona is not None:
        overrides["soul_persona"] = body.persona

    return replace(current, **overrides) if overrides else current  # type: ignore[arg-type]


class AgentService:
    """Agent CRUD + scope assignment + discovery."""

    def __init__(self, repository: IAgentRepository) -> None:
        self._repo = repository

    # ------------------------------------------------------------------
    # Instance API
    # ------------------------------------------------------------------

    async def create(
        self, ctx: RequestContext, workspace_id: str, body: CreateAgentRequest
    ) -> Agent:
        existing = await self._repo.get_by_slug(workspace_id, body.slug)
        if existing is not None:
            raise ConflictError(
                "agent.slug_taken",
                f"Slug '{body.slug}' is already in use in this workspace",
            )

        from datetime import UTC, datetime

        now = datetime.now(UTC)
        proto = Agent(
            id="",
            workspace_id=workspace_id,
            name=body.name,
            slug=body.slug,
            avatar=body.avatar,
            visibility=body.visibility,
            owner=ctx.user_id,
            config=_build_create_config(body),
            created_at=now,
            updated_at=now,
        )
        agent = await self._repo.create(proto)

        # Eagerly materialize the soul on disk if enabled. Failures here
        # are non-fatal; AgentPool will retry lazily on first chat.
        if agent.config.soul_enabled:
            await _try_eager_soul(agent)

        return agent

    async def get(self, agent_id: str) -> Agent:
        agent = await self._repo.get(agent_id)
        if agent is None:
            raise NotFound("agent", agent_id)
        return agent

    async def get_by_slug(self, workspace_id: str, slug: str) -> Agent:
        agent = await self._repo.get_by_slug(workspace_id, slug)
        if agent is None:
            raise NotFound("agent", slug)
        return agent

    async def list_agents(self, workspace_id: str, *, query: str | None = None) -> list[Agent]:
        return await self._repo.list_by_workspace(workspace_id, query=query)

    async def update(self, ctx: RequestContext, agent_id: str, body: UpdateAgentRequest) -> Agent:
        existing = await self._repo.get(agent_id)
        if existing is None:
            raise NotFound("agent", agent_id)
        if existing.owner != ctx.user_id:
            raise Forbidden("agent.not_owner", "Only the agent owner can update it")

        new_config = _apply_update(existing.config, body)
        return await self._repo.update_config(
            agent_id,
            name=body.name,
            avatar=body.avatar,
            visibility=body.visibility,
            config=new_config if new_config != existing.config else None,
        )

    async def delete(self, ctx: RequestContext, agent_id: str) -> None:
        existing = await self._repo.get(agent_id)
        if existing is None:
            raise NotFound("agent", agent_id)
        if existing.owner != ctx.user_id:
            raise Forbidden("agent.not_owner", "Only the agent owner can delete it")
        await self._repo.delete(agent_id)

    async def get_scopes(self, agent_id: str) -> list[str]:
        agent = await self._repo.get(agent_id)
        if agent is None:
            raise NotFound("agent", agent_id)
        return list(agent.config.scopes)

    async def set_scopes(self, agent_id: str, scopes: list[str]) -> list[str]:
        cleaned = normalise_and_validate_scopes(scopes)
        existing = await self._repo.get(agent_id)
        if existing is None:
            raise NotFound("agent", agent_id)
        new_config = replace(existing.config, scopes=tuple(cleaned))
        updated = await self._repo.update_config(agent_id, config=new_config)
        return list(updated.config.scopes)

    async def discover(
        self,
        ctx: RequestContext,
        workspace_id: str,
        body: DiscoverRequest,
    ) -> list[Agent]:
        return await self._repo.discover(
            workspace_id=workspace_id,
            user_id=ctx.user_id,
            visibility=body.visibility,
            query=body.query,
            page=body.page,
            page_size=body.page_size,
        )

    # ------------------------------------------------------------------
    # Legacy classmethod facade — preserves the existing call signatures
    # and wire-format dicts used by agents/router.py.
    # ------------------------------------------------------------------

    @classmethod
    def _default(cls) -> AgentService:
        return cls(get_default_repository())

    @classmethod
    async def create_default(
        cls, workspace_id: str, user_id: str, body: CreateAgentRequest
    ) -> dict:
        ctx = _legacy_ctx(user_id, workspace_id)
        agent = await cls._default().create(ctx, workspace_id, body)
        return agent_to_dict(agent)

    @classmethod
    async def list_agents_default(cls, workspace_id: str, query: str | None = None) -> list[dict]:
        items = await cls._default().list_agents(workspace_id, query=query)
        return [agent_to_dict(a) for a in items]

    @classmethod
    async def get_default(cls, agent_id: str) -> dict:
        agent = await cls._default().get(agent_id)
        return agent_to_dict(agent)

    @classmethod
    async def get_by_slug_default(cls, workspace_id: str, slug: str) -> dict:
        agent = await cls._default().get_by_slug(workspace_id, slug)
        return agent_to_dict(agent)

    @classmethod
    async def update_default(cls, agent_id: str, user_id: str, body: UpdateAgentRequest) -> dict:
        ctx = _legacy_ctx(user_id, None)
        agent = await cls._default().update(ctx, agent_id, body)
        return agent_to_dict(agent)

    @classmethod
    async def delete_default(cls, agent_id: str, user_id: str) -> None:
        ctx = _legacy_ctx(user_id, None)
        await cls._default().delete(ctx, agent_id)

    @classmethod
    async def get_scopes_default(cls, agent_id: str) -> list[str]:
        return await cls._default().get_scopes(agent_id)

    @classmethod
    async def set_scopes_default(cls, agent_id: str, scopes: list[str]) -> list[str]:
        return await cls._default().set_scopes(agent_id, scopes)

    @classmethod
    async def discover_default(
        cls, workspace_id: str, user_id: str, body: DiscoverRequest
    ) -> list[dict]:
        ctx = _legacy_ctx(user_id, workspace_id)
        items = await cls._default().discover(ctx, workspace_id, body)
        return [agent_to_dict(a) for a in items]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _legacy_ctx(user_id: str, workspace_id: str | None) -> RequestContext:
    """Build a RequestContext for the legacy classmethod facade."""
    from datetime import UTC, datetime

    from ee.cloud._core.context import ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="legacy",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _try_eager_soul(agent: Agent) -> None:
    """Best-effort eager soul materialization. Logs and continues on
    failure — AgentPool will retry on first chat."""
    try:
        from beanie import PydanticObjectId

        from ee.cloud.models.agent import Agent as _AgentDoc
        from pocketpaw.agents.pool import get_agent_pool

        # AgentPool.ensure_soul expects a Beanie doc; reload it.
        doc = await _AgentDoc.get(PydanticObjectId(agent.id))
        if doc is None:
            return
        await get_agent_pool().ensure_soul(doc)
    except Exception:
        logger.warning("Eager soul creation failed for agent %s", agent.id, exc_info=True)
