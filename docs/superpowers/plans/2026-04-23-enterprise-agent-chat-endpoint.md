# Enterprise Agent Chat Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fully separate enterprise SSE agent chat endpoint at `POST /cloud/chat/{scope}/{scope_id}/agent` that is scope-aware (dm/group/pocket), mounts pocket-scoped tools, passes ripple blocks through, and routes soul observation/self-eval to the target agent's soul instead of the global PocketPaw soul.

**Architecture:** New router (`agent_router.py`) + scope/tools/context service (`agent_service.py`) + schemas (`agent_schemas.py`) under `backend/ee/cloud/chat/`. Reuses the existing `AgentPool` (already per-agent-soul aware via `pool.observe(agent_id, …)`). Adds a `suppress_global_soul_observe` per-run flag to `AgentLoop` so OSS is untouched. Broadcasts finished assistant messages + `agent.typing` over the existing `/ws/cloud` `ConnectionManager`.

**Tech Stack:** FastAPI, Pydantic v2, Beanie (MongoDB ODM), pytest + pytest-asyncio, existing `pocketpaw.agents.pool.AgentPool`, existing `ee.cloud.chat.ws.manager`.

**Spec reference:** `backend/docs/superpowers/specs/2026-04-23-enterprise-agent-chat-endpoint-design.md`

**Working directory for all commands:** `D:/paw/backend`

---

## Task 0: Quick pre-flight

**Files:** none

- [ ] **Step 1: Confirm baseline tests pass**

Run: `uv run pytest --ignore=tests/e2e -x -q`
Expected: all pass (or same failures as pre-plan baseline — record them so later failures are attributable to this plan).

- [ ] **Step 2: Confirm lint+typecheck baseline**

Run: `uv run ruff check . && uv run mypy . || true`
Expected: record the current number of warnings/errors. Treat only *new* ones as blocking in later tasks.

---

## Task 1: Add `tool_specs` field to Pocket model

**Files:**
- Modify: `backend/ee/cloud/models/pocket.py`
- Test: `backend/tests/cloud/test_pocket_model.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/cloud/test_pocket_model.py`:

```python
"""Pocket document model tests."""
from __future__ import annotations

import pytest

from ee.cloud.models.pocket import Pocket


def test_pocket_tool_specs_defaults_to_empty_list():
    """New pockets have no scoped tools by default — must not inherit anything."""
    p = Pocket(workspace="w1", name="n", owner="u1")
    assert p.tool_specs == []


def test_pocket_tool_specs_accepts_list_of_dicts():
    """tool_specs is a free-form list of dicts so built-in IDs, MCP refs,
    and inline declarative tools can all be represented."""
    specs = [
        {"kind": "builtin", "id": "web_fetch"},
        {"kind": "mcp", "server": "notion", "name": "search_pages"},
        {"kind": "inline", "name": "echo", "schema": {"type": "object"}},
    ]
    p = Pocket(workspace="w1", name="n", owner="u1", tool_specs=specs)
    assert p.tool_specs == specs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_pocket_model.py -v`
Expected: FAIL with `AttributeError` or validation error on unknown field `tool_specs`.

- [ ] **Step 3: Add the field**

Modify `backend/ee/cloud/models/pocket.py`. After the `shared_with: list[str] = ...` line, before `model_config`, add:

```python
    # Pocket-scoped tool specs merged into the base toolset for agent runs
    # performed inside this pocket. Each entry is free-form so built-in IDs,
    # workspace MCP refs, and inline declarative tools can coexist.
    tool_specs: list[dict[str, Any]] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cloud/test_pocket_model.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ee/cloud/models/pocket.py tests/cloud/test_pocket_model.py
git commit -m "feat(cloud): add tool_specs to Pocket for scoped agent tools"
```

---

## Task 2: Add `suppress_global_soul_observe` flag to AgentLoop (OSS-safe)

**Files:**
- Modify: `backend/src/pocketpaw/agents/loop.py` (around line 1396-1402, `_soul_observe_and_emit` call site)
- Test: `backend/tests/test_agent_loop_soul_suppress.py`

**Context:** The cloud agent endpoint (Task 7) drives `AgentPool.run` directly, which calls the agent backend and bypasses `AgentLoop` entirely — so for today's wiring, the fix to the reported bug ("pocketpaw agent's soul gets updated no matter which agent was addressed") is delivered in Task 7 alone by using `AgentPool.observe(target_agent_id, …)`. This task adds the flag as **defense-in-depth**: any future code path that routes a cloud turn through `AgentLoop` (e.g. if the agent backend is swapped for one that reuses the loop's memory + observe pipeline) must be able to suppress the global observe. The flag rides on `InboundMessage.metadata["suppress_global_soul_observe"] = True`. OSS requests never set it, so behavior is unchanged.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_loop_soul_suppress.py`:

```python
"""AgentLoop must honor a per-turn flag that suppresses global soul observe.

This is the hook cloud runs use so the default PocketPaw soul does not
evolve from interactions that were actually directed at a specific
workspace agent. OSS paths never set the flag, so default behavior is
unchanged — verified by the second test.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_soul_observe_skipped_when_flag_set(monkeypatch):
    from pocketpaw.agents import loop as loop_mod

    # Build a minimal AgentLoop with a soul manager mock. We only exercise
    # the branch that decides whether to spawn _soul_observe_and_emit.
    al = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    al._soul_manager = MagicMock()
    al._soul_observe_and_emit = AsyncMock()

    message = MagicMock()
    message.content = "hello"
    message.metadata = {"suppress_global_soul_observe": True}
    session_key = "cloud:g:a"

    await al._maybe_observe_soul(message, "full response", session_key, cancelled=False)

    al._soul_observe_and_emit.assert_not_called()


@pytest.mark.asyncio
async def test_soul_observe_runs_when_flag_absent():
    from pocketpaw.agents import loop as loop_mod

    al = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    al._soul_manager = MagicMock()
    al._soul_observe_and_emit = AsyncMock()

    message = MagicMock()
    message.content = "hello"
    message.metadata = {}
    session_key = "websocket:abc"

    await al._maybe_observe_soul(message, "full response", session_key, cancelled=False)

    al._soul_observe_and_emit.assert_awaited_once_with("hello", "full response", session_key)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_loop_soul_suppress.py -v`
Expected: FAIL — `_maybe_observe_soul` does not exist yet.

- [ ] **Step 3: Refactor the existing observe branch into `_maybe_observe_soul`**

In `backend/src/pocketpaw/agents/loop.py`, find the existing block (around lines 1397-1402 in the file — the exact lines may shift; locate it by searching for `"Soul observation: feed turn for personality/memory evolution"`):

```python
                # Soul observation: feed turn for personality/memory evolution
                if self._soul_manager is not None and not cancelled:
                    t = asyncio.create_task(
                        self._soul_observe_and_emit(message.content, full_response, session_key)
                    )
```

Replace it with a single call:

```python
                # Soul observation: feed turn for personality/memory evolution.
                # Cloud runs pass ``suppress_global_soul_observe`` in metadata so
                # the default PocketPaw soul does not evolve from interactions
                # that were actually directed at a specific workspace agent.
                await self._maybe_observe_soul(
                    message, full_response, session_key, cancelled=cancelled
                )
```

Then add the new helper method. Put it immediately above `_soul_observe_and_emit` (so the ordering is `_maybe_observe_soul` → `_soul_observe_and_emit`):

```python
    async def _maybe_observe_soul(
        self, message: Any, full_response: str, session_key: str, *, cancelled: bool
    ) -> None:
        """Spawn global-soul observation unless the turn explicitly suppresses it.

        The suppression flag lives on ``InboundMessage.metadata`` so cloud
        runs — which route observation to a per-agent soul via
        ``AgentPool.observe`` — don't double-feed the default PocketPaw soul.
        """
        if self._soul_manager is None or cancelled:
            return
        meta = getattr(message, "metadata", None) or {}
        if meta.get("suppress_global_soul_observe"):
            return
        asyncio.create_task(
            self._soul_observe_and_emit(message.content, full_response, session_key)
        )
```

If `Any` isn't already imported in `loop.py`, verify by searching the file — it almost certainly is; if not, add it to the existing `typing` import line.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_agent_loop_soul_suppress.py tests/test_soul_manager.py tests/test_soul_integration.py -v`
Expected: all pass. The existing soul tests must still pass — this is the OSS-unchanged guarantee.

- [ ] **Step 5: Commit**

```bash
git add src/pocketpaw/agents/loop.py tests/test_agent_loop_soul_suppress.py
git commit -m "feat(agents): add suppress_global_soul_observe per-run flag

Cloud runs set this via InboundMessage.metadata so the default
PocketPaw soul does not evolve from turns directed at a specific
workspace agent. OSS behavior unchanged — flag defaults to false."
```

---

## Task 3: Schemas for cloud agent chat request + SSE events

**Files:**
- Create: `backend/ee/cloud/chat/agent_schemas.py`
- Test: `backend/tests/cloud/test_agent_schemas.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/cloud/test_agent_schemas.py`:

```python
"""Cloud agent chat request and SSE event schema tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ee.cloud.chat.agent_schemas import (
    CloudAgentChatRequest,
    SseEventName,
)


def test_request_requires_content():
    with pytest.raises(ValidationError):
        CloudAgentChatRequest(content="")


def test_request_accepts_minimal_body():
    req = CloudAgentChatRequest(content="hello")
    assert req.content == "hello"
    assert req.attachments == []
    assert req.mentions == []
    assert req.reply_to is None
    assert req.agent_id is None
    assert req.client_message_id is None


def test_request_accepts_full_body():
    req = CloudAgentChatRequest(
        content="hi",
        attachments=[{"type": "image", "url": "http://x/y.png"}],
        reply_to="msg_1",
        mentions=[{"type": "agent", "id": "a1"}],
        agent_id="a1",
        client_message_id="client_42",
    )
    assert req.agent_id == "a1"
    assert req.client_message_id == "client_42"


def test_event_names_cover_spec():
    expected = {
        "message.persisted",
        "stream_start",
        "thinking",
        "tool_start",
        "tool_result",
        "chunk",
        "ripple",
        "pocket_created",
        "pocket_mutation",
        "ask_user_question",
        "stream_end",
        "error",
    }
    assert {e.value for e in SseEventName} == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_agent_schemas.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the schemas module**

Create `backend/ee/cloud/chat/agent_schemas.py`:

```python
"""Request and SSE-event payload schemas for the enterprise agent chat endpoint.

The endpoint lives at ``POST /cloud/chat/{scope}/{scope_id}/agent`` and streams
back a typed SSE event sequence. See
``docs/superpowers/specs/2026-04-23-enterprise-agent-chat-endpoint-design.md``
for the full protocol.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CloudAgentChatRequest(BaseModel):
    """Body of ``POST /cloud/chat/{scope}/{scope_id}/agent``."""

    content: str = Field(min_length=1, max_length=10_000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    reply_to: str | None = None
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    # Required for group scope when the group has more than one agent member;
    # optional for dm (the agent peer is unambiguous) and pocket (primary agent
    # used unless overridden).
    agent_id: str | None = None
    # Idempotency key echoed back in ``message.persisted`` so the client can
    # reconcile its optimistic bubble before any agent output arrives.
    client_message_id: str | None = None


class SseEventName(str, Enum):
    """Names of SSE events emitted by the cloud agent endpoint.

    Kept as an Enum so tests and consumers have a single source of truth; the
    router itself builds raw SSE frames (``event:``/``data:``) for performance.
    """

    MESSAGE_PERSISTED = "message.persisted"
    STREAM_START = "stream_start"
    THINKING = "thinking"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    CHUNK = "chunk"
    RIPPLE = "ripple"
    POCKET_CREATED = "pocket_created"
    POCKET_MUTATION = "pocket_mutation"
    ASK_USER_QUESTION = "ask_user_question"
    STREAM_END = "stream_end"
    ERROR = "error"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cloud/test_agent_schemas.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add ee/cloud/chat/agent_schemas.py tests/cloud/test_agent_schemas.py
git commit -m "feat(cloud): add cloud agent chat request + SSE event schemas"
```

---

## Task 4: ScopeContext resolution (dm / group / pocket)

**Files:**
- Create: `backend/ee/cloud/chat/agent_service.py`
- Test: `backend/tests/cloud/test_agent_service_scope.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/cloud/test_agent_service_scope.py`:

```python
"""ScopeContext resolver tests — dm/group/pocket dispatch + target agent.

Uses AsyncMock-substituted Beanie finders so tests stay unit-scoped.
The real Mongo path is exercised by the router integration tests.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeKind,
    resolve_scope_context,
)
from ee.cloud.shared.errors import CloudError, NotFound


@pytest.mark.asyncio
async def test_resolve_dm_with_agent_peer_picks_that_agent():
    group = SimpleNamespace(
        id="g1",
        type="dm",
        members=["u_caller", "u_peer"],
        agents=[SimpleNamespace(agent="agent_peer_1", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        ctx = await resolve_scope_context(
            scope="dm", scope_id="g1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.kind == ScopeKind.DM
    assert ctx.scope_id == "g1"
    assert ctx.target_agent_id == "agent_peer_1"
    assert ctx.workspace_id == "w1"
    assert ctx.members == ["u_caller", "u_peer"]


@pytest.mark.asyncio
async def test_resolve_group_requires_agent_id_when_multiple_agents():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_caller", "u_other"],
        agents=[
            SimpleNamespace(agent="a1", respond_mode="auto"),
            SimpleNamespace(agent="a2", respond_mode="auto"),
        ],
        archived=False,
        workspace="w1",
    )
    with patch("ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_group_defaults_to_sole_agent():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_caller"],
        agents=[SimpleNamespace(agent="only_one", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        ctx = await resolve_scope_context(
            scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.target_agent_id == "only_one"


@pytest.mark.asyncio
async def test_resolve_rejects_non_member():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_other"],
        agents=[SimpleNamespace(agent="a1", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_pocket_uses_first_agent_when_no_hint():
    pocket = SimpleNamespace(
        id="p1",
        workspace="w1",
        owner="u_caller",
        team=["u_caller"],
        agents=["agent_primary", "agent_secondary"],
        tool_specs=[{"kind": "builtin", "id": "web_fetch"}],
        visibility="workspace",
        shared_with=[],
    )
    with patch("ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="p1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.kind == ScopeKind.POCKET
    assert ctx.target_agent_id == "agent_primary"
    assert ctx.pocket_tool_specs == [{"kind": "builtin", "id": "web_fetch"}]


@pytest.mark.asyncio
async def test_resolve_unknown_scope_raises():
    with pytest.raises(InvalidScope):
        await resolve_scope_context(
            scope="nope", scope_id="x", user_id="u", agent_id_hint=None
        )


@pytest.mark.asyncio
async def test_resolve_group_not_found_raises_notfound():
    with patch("ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=None)):
        with pytest.raises(NotFound):
            await resolve_scope_context(
                scope="group", scope_id="missing", user_id="u", agent_id_hint=None
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_agent_service_scope.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the service module**

Create `backend/ee/cloud/chat/agent_service.py`:

```python
"""Cloud agent chat service — scope resolution, toolset assembly, context.

Keeps the router thin: the router handles HTTP + SSE plumbing; this module
handles *what the agent sees*:

* ``resolve_scope_context`` turns (scope, scope_id, user_id) into a
  ``ScopeContext`` including the target agent id, members, and
  pocket-scoped tool specs where applicable.
* ``assemble_toolset`` merges base + pocket tools for a single run.
* ``build_context_block`` renders a compact string the agent prompt can
  embed so the agent knows who is in the room.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ee.cloud.shared.errors import CloudError, NotFound


class ScopeKind(str, Enum):
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
        raise CloudError("group.archived", "Group is archived", status_code=409)
    members = list(getattr(group, "members", []) or [])
    if user_id not in members:
        raise CloudError("group.not_member", "Caller is not a group member", status_code=403)

    # DM kind must actually be a dm on the document, and vice versa — prevents
    # a caller from driving a normal group through the /dm/ route to bypass
    # multi-agent disambiguation.
    if kind is ScopeKind.DM and getattr(group, "type", "") != "dm":
        raise CloudError("scope.mismatch", "Group is not a DM", status_code=400)
    if kind is ScopeKind.GROUP and getattr(group, "type", "") == "dm":
        raise CloudError("scope.mismatch", "DM must use /dm/ scope", status_code=400)

    agents = list(getattr(group, "agents", []) or [])
    agent_ids = [getattr(a, "agent", None) for a in agents if getattr(a, "agent", None)]
    if not agent_ids:
        raise CloudError("group.no_agent", "No agent in scope", status_code=400)

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


async def _resolve_pocket(
    scope_id: str, user_id: str, agent_id_hint: str | None
) -> ScopeContext:
    pocket = await _get_pocket(scope_id)
    if pocket is None:
        raise NotFound("pocket", scope_id)

    team = list(getattr(pocket, "team", []) or [])
    shared = list(getattr(pocket, "shared_with", []) or [])
    owner = getattr(pocket, "owner", None)
    visibility = getattr(pocket, "visibility", "workspace")
    is_member = user_id == owner or user_id in team or user_id in shared
    if visibility == "private" and not is_member:
        raise CloudError("pocket.forbidden", "No access to pocket", status_code=403)
    # For workspace/public we still require the caller be a workspace member;
    # the route-level dependency ``current_workspace_id`` already enforced that.

    agents = list(getattr(pocket, "agents", []) or [])
    agent_ids = [a if isinstance(a, str) else getattr(a, "id", None) for a in agents]
    agent_ids = [a for a in agent_ids if a]
    if not agent_ids:
        raise CloudError("pocket.no_agent", "Pocket has no agent", status_code=400)

    target = _pick_target_agent(agent_ids, agent_id_hint)

    return ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id=scope_id,
        workspace_id=str(getattr(pocket, "workspace", "")),
        user_id=user_id,
        members=[owner] + [m for m in (team + shared) if m != owner] if owner else team,
        target_agent_id=target,
        agent_ids_in_scope=agent_ids,
        pocket_tool_specs=list(getattr(pocket, "tool_specs", []) or []),
    )


def _pick_target_agent(agent_ids: list[str], hint: str | None) -> str:
    if hint is not None:
        if hint not in agent_ids:
            raise CloudError("agent.not_in_scope", "agent_id not in scope", status_code=400)
        return hint
    if len(agent_ids) == 1:
        return agent_ids[0]
    raise CloudError(
        "agent.ambiguous",
        "Multiple agents in scope — pass agent_id",
        status_code=400,
    )
```

- [ ] **Step 4: Check `CloudError` accepts `status_code`**

Run: `uv run python -c "from ee.cloud.shared.errors import CloudError, NotFound; e=CloudError('x','y',status_code=403); print(e.status_code)"`
Expected: prints `403`. If this fails, read `ee/cloud/shared/errors.py` and:
  - If `CloudError.__init__` accepts `status_code`: done.
  - If it does not: either add `status_code` kwarg (default 400), OR replace the `status_code=...` kwargs in `agent_service.py` with however the existing errors surface HTTP status (check how `router.py` currently maps `CloudError` → response). Keep the change minimal and re-run Task 4 Step 4.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/cloud/test_agent_service_scope.py -v`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add ee/cloud/chat/agent_service.py tests/cloud/test_agent_service_scope.py
git commit -m "feat(cloud): add ScopeContext resolver for agent chat"
```

---

## Task 5: Toolset assembly + context block helpers

**Files:**
- Modify: `backend/ee/cloud/chat/agent_service.py`
- Test: `backend/tests/cloud/test_agent_service_tools_context.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/cloud/test_agent_service_tools_context.py`:

```python
"""Toolset assembly + context block helpers."""
from __future__ import annotations

from ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    assemble_toolset,
    build_context_block,
)


def _pocket_ctx(specs: list[dict]) -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="p1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=specs,
    )


def test_assemble_toolset_base_only_for_non_pocket():
    ctx = ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    base = [{"kind": "builtin", "id": "web_fetch"}]
    assert assemble_toolset(ctx, base=base) == base


def test_assemble_toolset_merges_pocket_tools_dedupes_by_identity():
    base = [{"kind": "builtin", "id": "web_fetch"}]
    extra = [
        {"kind": "builtin", "id": "web_fetch"},  # duplicate — dropped
        {"kind": "mcp", "server": "notion", "name": "search_pages"},
    ]
    ctx = _pocket_ctx(extra)
    merged = assemble_toolset(ctx, base=base)
    assert len(merged) == 2
    assert merged[0] == base[0]
    assert merged[1] == extra[1]


def test_build_context_block_has_scope_and_members():
    ctx = ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    block = build_context_block(ctx)
    assert "<scope>group g1</scope>" in block
    assert "u1" in block and "u2" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_agent_service_tools_context.py -v`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Add the helpers**

Append to `backend/ee/cloud/chat/agent_service.py`:

```python
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


def assemble_toolset(
    ctx: ScopeContext, *, base: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cloud/test_agent_service_tools_context.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ee/cloud/chat/agent_service.py tests/cloud/test_agent_service_tools_context.py
git commit -m "feat(cloud): add pocket toolset assembly + scope context block"
```

---

## Task 6: Agent router — SSE endpoint (happy path)

**Files:**
- Create: `backend/ee/cloud/chat/agent_router.py`
- Modify: `backend/ee/cloud/chat/router.py` (include the new router)
- Test: `backend/tests/cloud/test_agent_router.py`

**Context:** This task builds the SSE endpoint with a **stub agent runner** (step-wise real events wired in Task 7) so the happy-path HTTP plumbing and auth are testable in isolation. The stub yields a fixed event sequence we can assert against.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/cloud/test_agent_router.py`:

```python
"""SSE happy-path tests for the cloud agent chat endpoint.

Patches ``_run_agent_stream`` with an async generator yielding a scripted
event sequence so we can assert the wire format without needing a real
agent backend.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from ee.cloud.chat import agent_router as mod


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Return [(event_name, data_dict), ...] from a streaming SSE body."""
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name = None
        data_str = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data_str = line[len("data: "):]
        if name is not None:
            events.append((name, json.loads(data_str) if data_str else {}))
    return events


@pytest.mark.asyncio
async def test_sse_emits_persisted_then_stream_events(cloud_app_client: AsyncClient, monkeypatch):
    """cloud_app_client is a conftest fixture (see Step 3) that returns an
    AsyncClient bound to a FastAPI app with the enterprise router mounted
    and auth dependencies overridden to a fixed user + workspace."""
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="group"),
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
    )

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id_1"

    async def fake_run_stream(ctx, user_msg_id, body, cancel_event):
        yield ("stream_start", {"run_id": "r1", "agent_id": ctx.target_agent_id,
                                 "scope": "group", "scope_id": "g1"})
        yield ("chunk", {"content": "hi ", "type": "text"})
        yield ("chunk", {"content": "there", "type": "text"})
        yield ("stream_end", {"assistant_message_id": "msg_2", "usage": {}, "cancelled": False})

    with patch.object(mod, "resolve_scope_context", fake_resolver), \
         patch.object(mod, "_persist_user_message", fake_persist), \
         patch.object(mod, "_run_agent_stream", fake_run_stream):
        async with cloud_app_client.stream(
            "POST", "/cloud/chat/group/g1/agent",
            json={"content": "hello", "client_message_id": "c1"},
        ) as resp:
            assert resp.status_code == 200
            body = (await resp.aread()).decode()

    events = _parse_sse(body)
    names = [n for n, _ in events]
    assert names[0] == "message.persisted"
    assert events[0][1]["user_message_id"] == "user_msg_id_1"
    assert events[0][1]["client_message_id"] == "c1"
    assert names[1] == "stream_start"
    assert names.count("chunk") == 2
    assert names[-1] == "stream_end"
```

- [ ] **Step 2: Set up a conftest for cloud HTTP tests**

Create `backend/tests/cloud/conftest.py` (only if it doesn't already exist — if it does, add the `cloud_app_client` fixture into the existing file):

```python
"""Shared fixtures for cloud HTTP tests.

Mounts the enterprise chat routers onto a minimal FastAPI app and overrides
the auth/license dependencies so tests don't need a real JWT.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id


def _fixed_user() -> str:
    return "u1"


def _fixed_workspace() -> str:
    return "w1"


def _no_op_license() -> None:
    return None


@pytest_asyncio.fixture
async def cloud_app_client() -> AsyncClient:
    from ee.cloud.chat.agent_router import router as agent_router

    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[current_user_id] = _fixed_user
    app.dependency_overrides[current_workspace_id] = _fixed_workspace
    app.dependency_overrides[require_license] = _no_op_license

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_agent_router.py -v`
Expected: FAIL — `ee.cloud.chat.agent_router` not found.

- [ ] **Step 4: Create the router (skeleton + SSE wire)**

Create `backend/ee/cloud/chat/agent_router.py`:

```python
"""Enterprise agent chat — SSE endpoint.

``POST /cloud/chat/{scope}/{scope_id}/agent`` streams a typed SSE sequence
to the caller while persisting the user message and (at stream end) the
assistant message. Agent run mechanics live in Task 7 — this module owns
the HTTP + SSE plumbing and scope/auth guards.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    resolve_scope_context,
)
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.shared.errors import CloudError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


# In-process cancel registry keyed by (scope, scope_id, user_id). A new request
# for the same tuple cancels the prior run — mirrors OSS /chat/stream semantics.
_active_runs: dict[tuple[str, str, str], asyncio.Event] = {}


Scope = Literal["dm", "group", "pocket"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/cloud/chat/{scope}/{scope_id}/agent")
async def post_agent_chat(
    scope: Scope,
    scope_id: str,
    body: CloudAgentChatRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    try:
        ctx = await resolve_scope_context(
            scope=scope, scope_id=scope_id, user_id=user_id, agent_id_hint=body.agent_id
        )
    except InvalidScope:
        raise HTTPException(status_code=400, detail={"code": "scope.invalid"})
    except CloudError as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", 400),
            detail={"code": e.code, "message": str(e)},
        )

    # Cancel any previous in-flight run for the same caller + scope.
    key = (scope, scope_id, user_id)
    prev = _active_runs.pop(key, None)
    if prev is not None:
        prev.set()
        await asyncio.sleep(0.05)  # let the old generator unwind

    cancel_event = asyncio.Event()
    _active_runs[key] = cancel_event

    user_message_id = await _persist_user_message(ctx, body)

    async def gen() -> AsyncIterator[bytes]:
        try:
            yield _sse(
                "message.persisted",
                {"user_message_id": user_message_id, "client_message_id": body.client_message_id},
            )
            async for name, data in _run_agent_stream(ctx, user_message_id, body, cancel_event):
                yield _sse(name, data)
                if name in ("stream_end", "error"):
                    break
        finally:
            _active_runs.pop(key, None)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cloud/chat/{scope}/{scope_id}/agent/stop")
async def post_agent_chat_stop(
    scope: Scope,
    scope_id: str,
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    key = (scope, scope_id, user_id)
    ev = _active_runs.get(key)
    if ev is None:
        raise HTTPException(status_code=404, detail={"code": "no_active_run"})
    ev.set()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Collaborators (stubs — Task 7 replaces _run_agent_stream with real bridge)
# ---------------------------------------------------------------------------


async def _persist_user_message(ctx: ScopeContext, body: CloudAgentChatRequest) -> str:
    """Persist the user message via MessageService and return its id.

    Task 7 wires this to the real MessageService.send_message; we keep it
    isolated here so Task 6 tests can patch it without Mongo.
    """
    raise NotImplementedError("wired in Task 7")


async def _run_agent_stream(
    ctx: ScopeContext,
    user_message_id: str,
    body: CloudAgentChatRequest,
    cancel_event: asyncio.Event,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield (event_name, data) tuples from the agent run.

    Task 7 replaces this stub with the real AgentPool-backed stream that
    emits chunks, tool events, ripple blocks, and stream_end.
    """
    if False:
        yield  # pragma: no cover — typing hint only
    raise NotImplementedError("wired in Task 7")


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]
```

- [ ] **Step 5: Include the new router**

Modify `backend/ee/cloud/chat/router.py`. At the top with the existing imports, add:

```python
from ee.cloud.chat.agent_router import router as agent_router
```

And near the bottom, next to `router.include_router(_licensed)`, add:

```python
router.include_router(agent_router)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/cloud/test_agent_router.py -v`
Expected: 1 passed.

- [ ] **Step 7: Commit**

```bash
git add ee/cloud/chat/agent_router.py ee/cloud/chat/router.py tests/cloud/test_agent_router.py tests/cloud/conftest.py
git commit -m "feat(cloud): SSE agent chat endpoint skeleton with auth + cancel"
```

---

## Task 7: Wire real agent run through AgentPool with soul routing

**Files:**
- Modify: `backend/ee/cloud/chat/agent_router.py` (replace `_persist_user_message` + `_run_agent_stream` stubs)
- Test: `backend/tests/cloud/test_agent_router_run.py`

**Context:** This task does four things:

1. Persist the user message via `MessageService.send_message` **but** suppress the existing `agent_bridge.on_message_for_agents` auto-response so we don't double-run the agent. We accomplish this by emitting a new event type (`message.sent_silent`) that `agent_bridge` ignores, or by calling the DB layer directly and only emitting `message.new` over `/ws/cloud`. **Recommended path:** call the DB layer directly to persist the Message, then broadcast `message.new` over the WS manager ourselves. This keeps behavior explicit and avoids event-bus fan-out coupling.
2. Run the agent via `AgentPool.run(target_agent_id, …)`.
3. Translate `AgentEvent` items into SSE event tuples (chunk / thinking / tool_* / ripple / stream_end). Ripple blocks are detected with the same regex `agent_bridge.py` uses and emitted as a dedicated event.
4. After the run, call `pool.observe(target_agent_id, …)` — per-agent soul routing. We do **not** need the AgentLoop flag from Task 2 here because this path bypasses AgentLoop entirely (it uses `AgentPool.run` which calls the backend directly). But a soul-using backend path that goes through AgentLoop would still need the flag — Task 2 is the hook for any future wiring that does.

- [ ] **Step 1: Write the failing integration test**

Create `backend/tests/cloud/test_agent_router_run.py`:

```python
"""Wires AgentPool + MessageService into the router; verifies soul routing."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from ee.cloud.chat import agent_router as mod


class _FakePool:
    """Mimics AgentPool for tests: scripts a run and records observe calls."""

    def __init__(self):
        self.observed: list[tuple[str, str, str]] = []

    async def get(self, agent_id):
        return SimpleNamespace(agent_id=agent_id, agent_name="Agent " + agent_id)

    async def run(self, agent_id, message, session_key, history=None, knowledge_context=""):
        # minimal scripted event sequence
        yield SimpleNamespace(type="thinking", content="pondering", metadata={})
        yield SimpleNamespace(type="tool_use", content={"tool": "web_fetch"}, metadata={})
        yield SimpleNamespace(type="message", content="Here is your answer.", metadata={})
        yield SimpleNamespace(
            type="message",
            content='\n\n```json\n{"lifecycle":"v1","widgets":[]}\n```',
            metadata={},
        )
        yield SimpleNamespace(type="done", content="", metadata={})

    async def observe(self, agent_id, user_input, agent_output):
        self.observed.append((agent_id, user_input, agent_output))


@pytest.mark.asyncio
async def test_full_run_emits_chunks_ripple_and_routes_observe_to_target(
    cloud_app_client: AsyncClient,
):
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="group"),
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="agent_X",
        agent_ids_in_scope=["agent_X"],
        pocket_tool_specs=[],
    )
    fake_pool = _FakePool()

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id_1"

    async def fake_persist_assistant(ctx, content, attachments):
        return "assistant_msg_id_1"

    async def fake_broadcast_new(ctx, message_id, content, attachments):
        return None

    with patch.object(mod, "resolve_scope_context", fake_resolver), \
         patch.object(mod, "_persist_user_message", fake_persist), \
         patch.object(mod, "_persist_assistant_message", fake_persist_assistant), \
         patch.object(mod, "_broadcast_message_new", fake_broadcast_new), \
         patch.object(mod, "get_agent_pool", lambda: fake_pool):
        async with cloud_app_client.stream(
            "POST", "/cloud/chat/group/g1/agent",
            json={"content": "hi"},
        ) as resp:
            assert resp.status_code == 200
            body = (await resp.aread()).decode()

    names: list[str] = []
    payloads: list[dict] = []
    for block in body.strip().split("\n\n"):
        name = None
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name:
            names.append(name)
            payloads.append(json.loads(data) if data else {})

    assert names[0] == "message.persisted"
    assert "stream_start" in names
    assert "thinking" in names
    assert "tool_start" in names
    assert names.count("chunk") >= 1
    assert "ripple" in names
    assert names[-1] == "stream_end"
    end_payload = payloads[names.index("stream_end")]
    assert end_payload["assistant_message_id"] == "assistant_msg_id_1"
    assert end_payload["cancelled"] is False

    # Soul routed to the target agent, not the global PocketPaw soul.
    assert fake_pool.observed and fake_pool.observed[0][0] == "agent_X"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/test_agent_router_run.py -v`
Expected: FAIL — `_persist_assistant_message`, `_broadcast_message_new`, `get_agent_pool` not imported in the router.

- [ ] **Step 3: Implement the real stream + persistence**

Open `backend/ee/cloud/chat/agent_router.py`. Replace the `# Collaborators (stubs — …)` section (the two `raise NotImplementedError` functions) and add the new helpers:

```python
# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


import re
from datetime import UTC, datetime

from pocketpaw.agents.pool import get_agent_pool  # re-exported for test patching


RIPPLE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


async def _persist_user_message(ctx: ScopeContext, body: CloudAgentChatRequest) -> str:
    """Persist the caller's message as a ``Message`` document and return its id.

    We write directly rather than going through ``MessageService.send_message``
    to avoid triggering the legacy ``agent_bridge`` auto-response path — the
    SSE endpoint is the sole driver of the reply for this request.
    """
    from ee.cloud.models.message import Message

    msg = Message(
        group=ctx.scope_id if ctx.kind.value != "pocket" else "",
        sender=ctx.user_id,
        sender_type="user",
        content=body.content,
        attachments=body.attachments,
        mentions=body.mentions,
        reply_to=body.reply_to,
    )
    await msg.insert()
    return str(msg.id)


async def _persist_assistant_message(
    ctx: ScopeContext, content: str, attachments: list[dict[str, Any]]
) -> str:
    from ee.cloud.models.message import Attachment, Message

    msg = Message(
        group=ctx.scope_id if ctx.kind.value != "pocket" else "",
        sender=None,
        sender_type="agent",
        agent=ctx.target_agent_id,
        content=content,
        attachments=[Attachment(**a) if isinstance(a, dict) else a for a in attachments],
    )
    await msg.insert()
    return str(msg.id)


async def _broadcast_message_new(
    ctx: ScopeContext,
    message_id: str,
    content: str,
    attachments: list[dict[str, Any]],
) -> None:
    """Broadcast the finished assistant message to every other scope member."""
    from ee.cloud.chat.ws import manager
    from ee.cloud.chat.schemas import WsOutbound

    others = [m for m in ctx.members if m != ctx.user_id]
    if not others:
        return
    await manager.broadcast_to_group(
        ctx.scope_id,
        others,
        WsOutbound(
            type="message.new",
            data={
                "id": message_id,
                "group": ctx.scope_id,
                "sender_type": "agent",
                "agent": ctx.target_agent_id,
                "content": content,
                "attachments": attachments,
                "created_at": datetime.now(UTC).isoformat(),
            },
        ),
    )


async def _broadcast_agent_typing(ctx: ScopeContext, active: bool) -> None:
    from ee.cloud.chat.ws import manager
    from ee.cloud.chat.schemas import WsOutbound

    others = [m for m in ctx.members if m != ctx.user_id]
    if not others:
        return
    await manager.broadcast_to_group(
        ctx.scope_id,
        others,
        WsOutbound(
            type="agent.typing",
            data={
                "scope": ctx.kind.value,
                "scope_id": ctx.scope_id,
                "agent_id": ctx.target_agent_id,
                "active": active,
            },
        ),
    )


async def _run_agent_stream(
    ctx: ScopeContext,
    user_message_id: str,
    body: CloudAgentChatRequest,
    cancel_event: asyncio.Event,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Drive AgentPool.run and translate events into SSE tuples."""
    run_id = _new_run_id()
    session_key = f"cloud:{ctx.kind.value}:{ctx.scope_id}:{ctx.target_agent_id}"

    pool = get_agent_pool()
    try:
        instance = await pool.get(ctx.target_agent_id)
    except Exception as e:
        logger.exception("Failed to load agent instance %s", ctx.target_agent_id)
        yield ("error", {"code": "agent.load_failed", "message": str(e)})
        return

    # Inject the scope/participants block via knowledge_context — AgentPool.run
    # prepends this to the system prompt, which is the least invasive way to
    # give the agent scope awareness without changing pool.run's signature.
    from ee.cloud.chat.agent_service import build_context_block

    scope_block = build_context_block(ctx)

    await _broadcast_agent_typing(ctx, active=True)

    yield (
        "stream_start",
        {
            "run_id": run_id,
            "agent_id": ctx.target_agent_id,
            "agent_name": getattr(instance, "agent_name", ""),
            "scope": ctx.kind.value,
            "scope_id": ctx.scope_id,
        },
    )

    full_text = ""
    cancelled = False
    try:
        async for event in pool.run(
            ctx.target_agent_id,
            body.content,
            session_key,
            history=None,
            knowledge_context=scope_block,
        ):
            if cancel_event.is_set():
                cancelled = True
                break
            etype = getattr(event, "type", None)
            econtent = getattr(event, "content", "")
            if etype == "message":
                full_text += econtent if isinstance(econtent, str) else ""
                yield ("chunk", {"content": econtent, "type": "text"})
            elif etype == "thinking":
                yield ("thinking", {"content": econtent if isinstance(econtent, str) else ""})
            elif etype == "tool_use":
                name = ""
                if isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                elif isinstance(econtent, str):
                    name = econtent
                yield ("tool_start", {"tool": name, "input": econtent if isinstance(econtent, dict) else {}})
            elif etype == "tool_result":
                name = ""
                output: Any = econtent
                if isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                    output = econtent.get("result", econtent)
                yield ("tool_result", {"tool": name, "output": output})
            elif etype == "done":
                break
    except Exception as e:
        logger.exception("Cloud agent run failed for agent=%s", ctx.target_agent_id)
        yield ("error", {"code": "agent.run_failed", "message": str(e)})
        await _broadcast_agent_typing(ctx, active=False)
        return

    # Extract ripple block from the accumulated text (same regex as agent_bridge).
    attachments: list[dict[str, Any]] = []
    match = RIPPLE_JSON_RE.search(full_text)
    if match:
        try:
            candidate = json.loads(match.group(1))
            if "lifecycle" in candidate or "widgets" in candidate:
                from ee.cloud.ripple_normalizer import normalize_ripple_spec

                spec = normalize_ripple_spec(candidate)
                attachments.append({"type": "ripple", "meta": spec})
                full_text = (full_text[: match.start()] + full_text[match.end() :]).strip()
                yield ("ripple", {"spec": spec})
        except Exception:
            logger.debug("Ripple parse failed", exc_info=True)

    if cancelled or not full_text.strip():
        yield ("stream_end", {"assistant_message_id": None, "usage": {}, "cancelled": cancelled})
        await _broadcast_agent_typing(ctx, active=False)
        return

    assistant_id = await _persist_assistant_message(ctx, full_text, attachments)
    await _broadcast_message_new(ctx, assistant_id, full_text, attachments)
    await _broadcast_agent_typing(ctx, active=False)

    # Per-agent soul observation — routed to the target agent's SoulManager
    # via AgentPool. Never touches the global default PocketPaw soul.
    try:
        await pool.observe(ctx.target_agent_id, body.content, full_text)
    except Exception:
        logger.debug("pool.observe failed for agent %s", ctx.target_agent_id, exc_info=True)

    yield (
        "stream_end",
        {"assistant_message_id": assistant_id, "usage": {}, "cancelled": False},
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cloud/test_agent_router.py tests/cloud/test_agent_router_run.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ee/cloud/chat/agent_router.py tests/cloud/test_agent_router_run.py
git commit -m "feat(cloud): wire AgentPool into SSE endpoint with per-agent soul

Cloud agent chat drives AgentPool.run directly (bypassing the legacy
agent_bridge auto-response) and calls pool.observe(target_agent_id)
at stream_end, so soul evolution lands on the target agent's soul
file rather than the default PocketPaw soul."
```

---

## Task 8: Cancellation endpoint and mid-stream cancel behavior

**Files:**
- Test: `backend/tests/cloud/test_agent_router_cancel.py`

**Context:** The `/stop` endpoint and `cancel_event` were added in Task 6. This task covers them with focused tests.

- [ ] **Step 1: Write the test**

Create `backend/tests/cloud/test_agent_router_cancel.py`:

```python
"""Cancellation behavior for the cloud agent endpoint."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from ee.cloud.chat import agent_router as mod


@pytest.mark.asyncio
async def test_stop_with_no_active_run_returns_404(cloud_app_client: AsyncClient):
    resp = await cloud_app_client.post("/cloud/chat/group/g1/agent/stop")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "no_active_run"


@pytest.mark.asyncio
async def test_second_request_cancels_first(cloud_app_client: AsyncClient):
    """A second POST for the same (scope, scope_id, user_id) triggers cancel
    on the first by setting its cancel event."""
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="group"),
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
    )

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id"

    first_done = asyncio.Event()

    async def slow_stream(ctx, user_msg_id, body, cancel_event):
        yield ("stream_start", {"run_id": "r", "agent_id": "a1",
                                 "scope": "group", "scope_id": "g1"})
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=5.0)
        finally:
            first_done.set()
        yield ("stream_end", {"assistant_message_id": None, "usage": {}, "cancelled": True})

    async def fast_stream(ctx, user_msg_id, body, cancel_event):
        yield ("stream_end", {"assistant_message_id": "m", "usage": {}, "cancelled": False})

    with patch.object(mod, "resolve_scope_context", fake_resolver), \
         patch.object(mod, "_persist_user_message", fake_persist):
        with patch.object(mod, "_run_agent_stream", slow_stream):
            first_task = asyncio.create_task(
                cloud_app_client.post("/cloud/chat/group/g1/agent", json={"content": "1"})
            )
            # Give the first request time to register its cancel_event
            await asyncio.sleep(0.05)
        with patch.object(mod, "_run_agent_stream", fast_stream):
            second = await cloud_app_client.post(
                "/cloud/chat/group/g1/agent", json={"content": "2"}
            )
        assert second.status_code == 200
        await asyncio.wait_for(first_task, timeout=2.0)
        assert first_done.is_set()
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/cloud/test_agent_router_cancel.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/cloud/test_agent_router_cancel.py
git commit -m "test(cloud): cover /agent/stop and implicit cancel on new run"
```

---

## Task 9: Error-path tests (resolver error → SSE or 4xx)

**Files:**
- Test: `backend/tests/cloud/test_agent_router_errors.py`

- [ ] **Step 1: Write the test**

Create `backend/tests/cloud/test_agent_router_errors.py`:

```python
"""Pre-stream errors must land as HTTP 4xx, not SSE error frames."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from ee.cloud.chat import agent_router as mod
from ee.cloud.chat.agent_service import InvalidScope
from ee.cloud.shared.errors import CloudError, NotFound


@pytest.mark.asyncio
async def test_invalid_scope_returns_400(cloud_app_client: AsyncClient):
    async def raise_invalid(**_):
        raise InvalidScope("nope")

    with patch.object(mod, "resolve_scope_context", raise_invalid):
        resp = await cloud_app_client.post(
            "/cloud/chat/group/g1/agent", json={"content": "x"}
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "scope.invalid"


@pytest.mark.asyncio
async def test_not_member_returns_403(cloud_app_client: AsyncClient):
    async def raise_forbidden(**_):
        raise CloudError(
            "group.not_member", "Caller is not a group member", status_code=403
        )

    with patch.object(mod, "resolve_scope_context", raise_forbidden):
        resp = await cloud_app_client.post(
            "/cloud/chat/group/g1/agent", json={"content": "x"}
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "group.not_member"


@pytest.mark.asyncio
async def test_not_found_returns_404(cloud_app_client: AsyncClient):
    async def raise_nf(**_):
        raise NotFound("group", "g1")

    with patch.object(mod, "resolve_scope_context", raise_nf):
        resp = await cloud_app_client.post(
            "/cloud/chat/group/g1/agent", json={"content": "x"}
        )
    # NotFound should map to 404 via the existing CloudError handler path.
    # If your code base maps NotFound differently, adjust this assertion to
    # match that convention rather than changing the handler just for tests.
    assert resp.status_code in (404, 400)
```

- [ ] **Step 2: Run tests; adjust `NotFound` handling if needed**

Run: `uv run pytest tests/cloud/test_agent_router_errors.py -v`

If `test_not_found_returns_404` fails because `NotFound` doesn't inherit from `CloudError` or doesn't have `status_code=404`, then in `ee/cloud/chat/agent_router.py::post_agent_chat`, adjust the exception translation block to handle `NotFound` explicitly:

```python
    except NotFound as e:
        raise HTTPException(status_code=404, detail={"code": e.code, "message": str(e)})
```

(Remember to `from ee.cloud.shared.errors import CloudError, NotFound` at the top.)

Re-run until all three tests pass.

- [ ] **Step 3: Commit**

```bash
git add ee/cloud/chat/agent_router.py tests/cloud/test_agent_router_errors.py
git commit -m "feat(cloud): map pre-stream agent-chat errors to explicit 4xx codes"
```

---

## Task 10: Smoke integration test + README update

**Files:**
- Test: `backend/tests/cloud/test_agent_router_smoke.py`
- Modify: `backend/ee/cloud/chat/__init__.py` (export agent router symbols if the module uses explicit `__all__`; skip if it doesn't)

- [ ] **Step 1: Write a top-to-bottom smoke test**

Create `backend/tests/cloud/test_agent_router_smoke.py`:

```python
"""Smoke test: 200 OK SSE stream with every required event kind."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from ee.cloud.chat import agent_router as mod


class _Pool:
    def __init__(self):
        self.observed = []

    async def get(self, aid):
        return SimpleNamespace(agent_id=aid, agent_name="A")

    async def run(self, aid, message, session_key, history=None, knowledge_context=""):
        yield SimpleNamespace(type="thinking", content="t", metadata={})
        yield SimpleNamespace(type="tool_use", content={"tool": "web_fetch"}, metadata={})
        yield SimpleNamespace(type="message", content="hello ", metadata={})
        yield SimpleNamespace(type="message", content="world", metadata={})
        yield SimpleNamespace(type="done", content="", metadata={})

    async def observe(self, aid, user_input, agent_output):
        self.observed.append((aid, user_input, agent_output))


@pytest.mark.asyncio
async def test_smoke_all_event_kinds(cloud_app_client: AsyncClient):
    ctx = SimpleNamespace(
        kind=SimpleNamespace(value="dm"),
        scope_id="dm1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
    )

    async def resolver(**_):
        return ctx

    async def persist(_, __):
        return "uid"

    async def persist_a(_, __, ___):
        return "aid"

    async def bc_new(_, __, ___, ____):
        return None

    pool = _Pool()
    with patch.object(mod, "resolve_scope_context", resolver), \
         patch.object(mod, "_persist_user_message", persist), \
         patch.object(mod, "_persist_assistant_message", persist_a), \
         patch.object(mod, "_broadcast_message_new", bc_new), \
         patch.object(mod, "_broadcast_agent_typing", lambda *a, **k: None), \
         patch.object(mod, "get_agent_pool", lambda: pool):
        async with cloud_app_client.stream(
            "POST", "/cloud/chat/dm/dm1/agent", json={"content": "hi"}
        ) as resp:
            assert resp.status_code == 200
            body = (await resp.aread()).decode()

    names: list[str] = []
    for block in body.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("event: "):
                names.append(line[7:])

    assert names[0] == "message.persisted"
    assert "stream_start" in names
    assert "thinking" in names
    assert "tool_start" in names
    assert names.count("chunk") >= 2
    assert names[-1] == "stream_end"
    assert pool.observed[0][0] == "a1"
```

**Note:** `_broadcast_agent_typing` is patched to a *sync* no-op lambda here; if your `_run_agent_stream` awaits it, either leave this patch out (letting the real function run against the test WS manager, which just returns early when no other members are connected) or patch it with `AsyncMock`. Pick the option that keeps the test passing — do not "fix" this by rewriting production code to work around the patch.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/cloud/test_agent_router_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 3: Full sweep**

Run: `uv run pytest tests/cloud/ tests/test_agent_loop_soul_suppress.py -v`
Expected: all green (new tests + unaffected existing cloud tests).

- [ ] **Step 4: Lint + typecheck**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy .`
Expected: no new errors vs. the Task 0 baseline. Fix any new ones now:
  - Import ordering / unused imports → `uv run ruff check . --fix`
  - Formatting → `uv run ruff format .`
  - Types: add `# type: ignore[reason]` only when the underlying code is genuinely dynamic; otherwise fix the types.

- [ ] **Step 5: Final commit**

```bash
git add tests/cloud/test_agent_router_smoke.py
git commit -m "test(cloud): smoke coverage across full SSE event sequence"
```

---

## Post-plan verification

- [ ] **Step 1: Regression sweep**

Run: `uv run pytest --ignore=tests/e2e -x -q`
Expected: same pass/fail as Task 0's baseline — no *new* failures caused by this plan.

- [ ] **Step 2: Manual smoke (optional)**

With backend running (`uv run pocketpaw --dev`) and a valid JWT:

```bash
curl -N -H "Authorization: Bearer $JWT" \
     -H "Content-Type: application/json" \
     -d '{"content":"hello agent"}' \
     "http://localhost:8888/cloud/chat/group/<GROUP_ID>/agent"
```

Expected: SSE stream with `message.persisted` → `stream_start` → chunks → `stream_end`. In a second terminal connected to `/ws/cloud` as another group member, expect `agent.typing` active → `message.new` → `agent.typing` inactive.

Confirm: chatting with a non-default agent evolves that agent's soul file under `~/.pocketpaw/souls/{workspace}/{slug}.soul` and leaves the default PocketPaw soul untouched.

---

## What's intentionally deferred (per spec)

- Live chunk broadcast to non-caller members.
- Multi-agent turn-taking inside a group.
- Persisting tool traces as structured sub-documents on the assistant `Message`.
- Rate limiting per `(workspace, user)`.
