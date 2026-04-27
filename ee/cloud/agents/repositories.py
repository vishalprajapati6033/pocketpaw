"""Repository for the agents domain.

Defines `IAgentRepository` and a Beanie-backed implementation. Services
depend on the Protocol; tests inject in-memory fakes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud._core.errors import NotFound
from ee.cloud.agents.domain import Agent, AgentConfigSpec
from ee.cloud.models.agent import Agent as _AgentDoc
from ee.cloud.models.agent import AgentConfig as _AgentConfigDoc


def _config_to_domain(c: _AgentConfigDoc) -> AgentConfigSpec:
    return AgentConfigSpec(
        backend=c.backend,
        model=c.model,
        system_prompt=c.system_prompt,
        tools=tuple(c.tools),
        trust_level=c.trust_level,
        temperature=c.temperature,
        max_tokens=c.max_tokens,
        scopes=tuple(c.scopes),
        soul_enabled=c.soul_enabled,
        soul_persona=c.soul_persona,
        soul_archetype=c.soul_archetype,
        soul_values=tuple(c.soul_values),
        soul_ocean=tuple(c.soul_ocean.items()),
    )


def _config_to_doc(c: AgentConfigSpec) -> _AgentConfigDoc:
    return _AgentConfigDoc(
        backend=c.backend,
        model=c.model,
        system_prompt=c.system_prompt,
        tools=list(c.tools),
        trust_level=c.trust_level,
        temperature=c.temperature,
        max_tokens=c.max_tokens,
        scopes=list(c.scopes),
        soul_enabled=c.soul_enabled,
        soul_persona=c.soul_persona,
        soul_archetype=c.soul_archetype,
        soul_values=list(c.soul_values),
        soul_ocean=dict(c.soul_ocean),
    )


def _to_domain(doc: _AgentDoc) -> Agent:
    return Agent(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        slug=doc.slug,
        avatar=doc.avatar,
        visibility=doc.visibility,
        owner=doc.owner,
        config=_config_to_domain(doc.config),
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        updated_at=getattr(doc, "updatedAt", None),  # type: ignore[arg-type]
    )


@runtime_checkable
class IAgentRepository(Protocol):
    async def get(self, agent_id: str) -> Agent | None: ...
    async def get_by_slug(self, workspace_id: str, slug: str) -> Agent | None: ...
    async def create(self, agent: Agent) -> Agent: ...
    async def update_config(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        avatar: str | None = None,
        visibility: str | None = None,
        config: AgentConfigSpec | None = None,
    ) -> Agent: ...
    async def list_by_workspace(
        self, workspace_id: str, *, query: str | None = None
    ) -> list[Agent]: ...
    async def delete(self, agent_id: str) -> None: ...
    async def discover(
        self,
        *,
        workspace_id: str,
        user_id: str,
        visibility: str | None,
        query: str,
        page: int,
        page_size: int,
    ) -> list[Agent]: ...


class MongoAgentRepository:
    """Beanie implementation of `IAgentRepository`."""

    async def get(self, agent_id: str) -> Agent | None:
        try:
            doc = await _AgentDoc.get(PydanticObjectId(agent_id))
        except Exception:
            return None
        return _to_domain(doc) if doc else None

    async def get_by_slug(self, workspace_id: str, slug: str) -> Agent | None:
        doc = await _AgentDoc.find_one(
            _AgentDoc.workspace == workspace_id,
            _AgentDoc.slug == slug,
        )
        return _to_domain(doc) if doc else None

    async def create(self, agent: Agent) -> Agent:
        doc = _AgentDoc(
            workspace=agent.workspace_id,
            name=agent.name,
            slug=agent.slug,
            avatar=agent.avatar,
            visibility=agent.visibility,
            owner=agent.owner,
            config=_config_to_doc(agent.config),
        )
        await doc.insert()
        return _to_domain(doc)

    async def update_config(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        avatar: str | None = None,
        visibility: str | None = None,
        config: AgentConfigSpec | None = None,
    ) -> Agent:
        doc = await _AgentDoc.get(PydanticObjectId(agent_id))
        if doc is None:
            raise NotFound("agent", agent_id)
        if name is not None:
            doc.name = name
        if avatar is not None:
            doc.avatar = avatar
        if visibility is not None:
            doc.visibility = visibility
        if config is not None:
            doc.config = _config_to_doc(config)
        await doc.save()
        return _to_domain(doc)

    async def list_by_workspace(
        self, workspace_id: str, *, query: str | None = None
    ) -> list[Agent]:
        filters: dict[str, Any] = {"workspace": workspace_id}
        if query:
            filters["name"] = {"$regex": query, "$options": "i"}
        docs = await _AgentDoc.find(filters).to_list()
        return [_to_domain(d) for d in docs]

    async def delete(self, agent_id: str) -> None:
        doc = await _AgentDoc.get(PydanticObjectId(agent_id))
        if doc is None:
            raise NotFound("agent", agent_id)
        await doc.delete()

    async def discover(
        self,
        *,
        workspace_id: str,
        user_id: str,
        visibility: str | None,
        query: str,
        page: int,
        page_size: int,
    ) -> list[Agent]:
        filters: dict[str, Any] = {}
        if visibility == "private":
            filters["workspace"] = workspace_id
            filters["owner"] = user_id
        elif visibility == "workspace":
            filters["workspace"] = workspace_id
        elif visibility == "public":
            filters["visibility"] = "public"
        else:
            filters["$or"] = [
                {"workspace": workspace_id, "owner": user_id},
                {"workspace": workspace_id, "visibility": "workspace"},
                {"visibility": "public"},
            ]
        if query:
            filters["name"] = {"$regex": query, "$options": "i"}

        skip = (page - 1) * page_size
        docs = await _AgentDoc.find(filters).skip(skip).limit(page_size).to_list()
        return [_to_domain(d) for d in docs]


_default: IAgentRepository | None = None


def get_default_repository() -> IAgentRepository:
    global _default
    if _default is None:
        _default = MongoAgentRepository()
    return _default


def set_default_repository(repo: IAgentRepository) -> None:
    global _default
    _default = repo


__all__ = [
    "IAgentRepository",
    "MongoAgentRepository",
    "get_default_repository",
    "set_default_repository",
]
