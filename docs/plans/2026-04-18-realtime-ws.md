# Global Realtime `/ws/cloud` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Emit WebSocket events for every EE mutation so the paw-enterprise client never needs a refresh to see group, workspace, message, presence, file, session, agent, or notification changes.

**Architecture:** Services call a single `emit(event)` facade after each successful mutation. A pluggable `EventBus` (in-process default, Redis optional) resolves audience via a central `AudienceResolver` and fans out through the existing `ConnectionManager` on `/ws/cloud`. Client runs one `RealtimeClient` singleton, dispatches events to store reconcilers, and refetches on reconnect instead of replaying a log.

**Tech Stack:** FastAPI, Beanie (MongoDB), Pydantic, SvelteKit 2 + Svelte 5 runes, Vitest, pytest + pytest-asyncio, fakeredis, redis.asyncio.

**Design source:** `docs/plans/2026-04-18-realtime-ws-design.md`

---

## Phase 1 — Event bus infra

### Task 1: Scaffold `ee/cloud/realtime/` package

**Files:**
- Create: `ee/cloud/realtime/__init__.py`
- Create: `ee/cloud/realtime/events.py`
- Create: `ee/cloud/realtime/bus.py`
- Create: `ee/cloud/realtime/audience.py`
- Create: `ee/cloud/realtime/emit.py`
- Test: `tests/cloud/realtime/__init__.py`
- Test: `tests/cloud/realtime/test_events.py`

**Step 1: Write the failing test**

```python
# tests/cloud/realtime/test_events.py
from ee.cloud.realtime.events import Event, GroupCreated

def test_event_has_type_data_ts():
    ev = Event(type="x.y", data={"a": 1})
    assert ev.type == "x.y"
    assert ev.data == {"a": 1}
    assert ev.ts is not None  # defaults to now()

def test_typed_event_subclass_sets_type():
    ev = GroupCreated(data={"group_id": "g1", "member_ids": ["u1"]})
    assert ev.type == "group.created"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cloud/realtime/test_events.py -v`
Expected: FAIL — import error.

**Step 3: Implement `events.py`**

```python
# ee/cloud/realtime/events.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar

@dataclass
class Event:
    type: str
    data: dict
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        cls_type = getattr(type(self), "EVENT_TYPE", None)
        if cls_type:
            self.type = cls_type

@dataclass
class GroupCreated(Event):
    EVENT_TYPE: ClassVar[str] = "group.created"
    type: str = "group.created"

# (one class per type in the design's event catalog — use the catalog as the source of truth)
```

Implement one class per event listed in the design's event catalog (workspace.*, group.*, message.*, presence.*, file.*, session.*, agent.*, notification.*). Keep it mechanical — name, `EVENT_TYPE`, nothing else.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cloud/realtime/test_events.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/cloud/realtime/__init__.py ee/cloud/realtime/events.py tests/cloud/realtime/
git commit -m "feat(realtime): add typed Event dataclasses for all surfaces"
```

---

### Task 2: `AudienceResolver` with per-event branches

**Files:**
- Modify: `ee/cloud/realtime/audience.py`
- Test: `tests/cloud/realtime/test_audience.py`

**Step 1: Write failing tests**

```python
# tests/cloud/realtime/test_audience.py
import pytest
from ee.cloud.realtime.audience import AudienceResolver
from ee.cloud.realtime.events import (
    GroupCreated, GroupMemberRemoved, MessageNew, MessageSent,
    WorkspaceInviteCreated, SessionCreated, NotificationNew,
)

@pytest.mark.asyncio
async def test_group_created_returns_member_ids():
    r = AudienceResolver(group_members=lambda gid: ["u1","u2","u3"])
    ev = GroupCreated(data={"group_id":"g1","member_ids":["u1","u2","u3"]})
    assert set(await r.audience(ev)) == {"u1","u2","u3"}

@pytest.mark.asyncio
async def test_member_removed_includes_removed_user():
    r = AudienceResolver(group_members=lambda gid: ["u1","u2"])
    ev = GroupMemberRemoved(data={"group_id":"g1","user_id":"u3"})
    assert set(await r.audience(ev)) == {"u1","u2","u3"}

@pytest.mark.asyncio
async def test_message_sent_only_to_sender():
    r = AudienceResolver()
    ev = MessageSent(data={"group_id":"g1","sender_id":"u1"})
    assert await r.audience(ev) == ["u1"]
```

Run: `uv run pytest tests/cloud/realtime/test_audience.py -v`
Expected: FAIL.

**Step 2: Implement `AudienceResolver`**

```python
# ee/cloud/realtime/audience.py
from __future__ import annotations
import time
from typing import Callable, Awaitable
from ee.cloud.realtime.events import Event

class AudienceResolver:
    def __init__(
        self,
        group_members: Callable[[str], Awaitable[list[str]]] | None = None,
        workspace_members: Callable[[str], Awaitable[list[str]]] | None = None,
        workspace_admins: Callable[[str], Awaitable[list[str]]] | None = None,
        workspace_peers: Callable[[str], Awaitable[list[str]]] | None = None,
    ):
        self._group_members = group_members
        self._workspace_members = workspace_members
        self._workspace_admins = workspace_admins
        self._workspace_peers = workspace_peers
        self._cache: dict[tuple[str,str], tuple[float, list[str]]] = {}

    async def _cached(self, kind: str, key: str, fn) -> list[str]:
        now = time.monotonic()
        entry = self._cache.get((kind, key))
        if entry and now - entry[0] < 2.0:
            return entry[1]
        value = await fn(key)
        self._cache[(kind, key)] = (now, value)
        return value

    def invalidate_group(self, group_id: str) -> None:
        self._cache.pop(("group", group_id), None)

    def invalidate_workspace(self, workspace_id: str) -> None:
        self._cache.pop(("workspace", workspace_id), None)
        self._cache.pop(("workspace_admins", workspace_id), None)

    async def audience(self, event: Event) -> list[str]:
        d = event.data
        t = event.type
        # ... branches per design
```

Implement all branches from the design's AudienceResolver sketch. Add branches until every event type in `events.py` is covered (test coverage check in Task 3).

**Step 3: Run tests to verify pass**

Run: `uv run pytest tests/cloud/realtime/test_audience.py -v`
Expected: PASS (all three).

**Step 4: Commit**

```bash
git add ee/cloud/realtime/audience.py tests/cloud/realtime/test_audience.py
git commit -m "feat(realtime): add AudienceResolver with 2s TTL cache"
```

---

### Task 3: Audience coverage sanity test

**Files:**
- Test: `tests/cloud/realtime/test_audience_coverage.py`

**Step 1: Write the failing test**

```python
# tests/cloud/realtime/test_audience_coverage.py
import inspect
import pytest
from ee.cloud.realtime import events as ev_mod
from ee.cloud.realtime.audience import AudienceResolver
from ee.cloud.realtime.events import Event

@pytest.mark.asyncio
async def test_every_event_type_is_resolved():
    """Every Event subclass must have a branch in AudienceResolver.audience."""
    r = AudienceResolver(
        group_members=lambda g: [],
        workspace_members=lambda w: [],
        workspace_admins=lambda w: [],
        workspace_peers=lambda u: [],
    )
    subclasses = [
        c for _, c in inspect.getmembers(ev_mod, inspect.isclass)
        if issubclass(c, Event) and c is not Event
    ]
    assert subclasses, "no typed events defined"
    for cls in subclasses:
        # Minimal payload with common keys; resolver must not KeyError.
        ev = cls(data={
            "group_id":"g","user_id":"u","sender_id":"u","peer_id":"p",
            "workspace_id":"w","invite_id":"i","member_ids":["u"],
            "message_id":"m","emoji":"x","file_id":"f",
        })
        result = await r.audience(ev)
        assert isinstance(result, list), f"{cls.__name__}: expected list, got {type(result)}"
```

**Step 2: Run test; fix any uncovered branches**

Run: `uv run pytest tests/cloud/realtime/test_audience_coverage.py -v`
If any event type errors or returns non-list, add its branch to `audience.py`.

**Step 3: Commit**

```bash
git add tests/cloud/realtime/test_audience_coverage.py ee/cloud/realtime/audience.py
git commit -m "test(realtime): enforce AudienceResolver covers every Event type"
```

---

### Task 4: `EventBus` protocol + `InProcessBus`

**Files:**
- Modify: `ee/cloud/realtime/bus.py`
- Test: `tests/cloud/realtime/test_bus.py`

**Step 1: Write failing tests**

```python
# tests/cloud/realtime/test_bus.py
import pytest
from unittest.mock import AsyncMock
from ee.cloud.realtime.bus import InProcessBus
from ee.cloud.realtime.audience import AudienceResolver
from ee.cloud.realtime.events import GroupCreated

@pytest.mark.asyncio
async def test_inprocess_bus_fans_out_to_resolved_audience():
    resolver = AudienceResolver(group_members=AsyncMock(return_value=["u1","u2"]))
    conn = AsyncMock()
    bus = InProcessBus(resolver=resolver, conn_manager=conn)
    ev = GroupCreated(data={"group_id":"g1","member_ids":["u1","u2"]})

    await bus.publish(ev)

    assert conn.send_to_user.await_count == 2
    sent_users = {call.args[0] for call in conn.send_to_user.await_args_list}
    assert sent_users == {"u1","u2"}

@pytest.mark.asyncio
async def test_inprocess_bus_isolates_handler_exceptions():
    resolver = AudienceResolver(group_members=AsyncMock(return_value=["u1","u2","u3"]))
    conn = AsyncMock()
    # Fail on middle recipient
    conn.send_to_user.side_effect = [None, RuntimeError("dead socket"), None]
    bus = InProcessBus(resolver=resolver, conn_manager=conn)
    await bus.publish(GroupCreated(data={"group_id":"g1","member_ids":["u1","u2","u3"]}))
    # u3 still got it
    assert conn.send_to_user.await_count == 3
```

Run: `uv run pytest tests/cloud/realtime/test_bus.py -v` → FAIL.

**Step 2: Implement `bus.py`**

```python
# ee/cloud/realtime/bus.py
from __future__ import annotations
import logging
from typing import Protocol
from ee.cloud.chat.schemas import WsOutbound
from ee.cloud.realtime.audience import AudienceResolver
from ee.cloud.realtime.events import Event

logger = logging.getLogger(__name__)

class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

class InProcessBus:
    def __init__(self, resolver: AudienceResolver, conn_manager) -> None:
        self._resolver = resolver
        self._conn = conn_manager

    async def publish(self, event: Event) -> None:
        try:
            audience = await self._resolver.audience(event)
        except Exception:
            logger.exception("audience resolution failed for %s", event.type)
            return
        payload = WsOutbound(type=event.type, data=event.data)
        for uid in audience:
            try:
                await self._conn.send_to_user(uid, payload)
            except Exception:
                logger.warning("ws send failed for user=%s event=%s", uid, event.type)

_bus: EventBus | None = None

def set_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus

def get_bus() -> EventBus:
    assert _bus is not None, "EventBus not initialized"
    return _bus
```

**Step 3: Verify tests pass**

Run: `uv run pytest tests/cloud/realtime/test_bus.py -v` → PASS.

**Step 4: Commit**

```bash
git add ee/cloud/realtime/bus.py tests/cloud/realtime/test_bus.py
git commit -m "feat(realtime): add EventBus protocol and InProcessBus"
```

---

### Task 5: `emit()` facade

**Files:**
- Modify: `ee/cloud/realtime/emit.py`
- Test: `tests/cloud/realtime/test_emit.py`

**Step 1: Write failing test**

```python
# tests/cloud/realtime/test_emit.py
import pytest
from unittest.mock import AsyncMock
from ee.cloud.realtime.bus import set_bus
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import GroupCreated

@pytest.mark.asyncio
async def test_emit_delegates_to_bus():
    bus = AsyncMock()
    set_bus(bus)
    ev = GroupCreated(data={"group_id":"g","member_ids":[]})
    await emit(ev)
    bus.publish.assert_awaited_once_with(ev)
```

Run: `uv run pytest tests/cloud/realtime/test_emit.py -v` → FAIL.

**Step 2: Implement `emit.py`**

```python
# ee/cloud/realtime/emit.py
from ee.cloud.realtime.bus import get_bus
from ee.cloud.realtime.events import Event

async def emit(event: Event) -> None:
    try:
        await get_bus().publish(event)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("emit failed for %s", event.type)
```

**Step 3: Verify**

Run: `uv run pytest tests/cloud/realtime/test_emit.py -v` → PASS.

**Step 4: Commit**

```bash
git add ee/cloud/realtime/emit.py tests/cloud/realtime/test_emit.py
git commit -m "feat(realtime): add emit() facade"
```

---

### Task 6: Wire bus into cloud startup

**Files:**
- Modify: `ee/cloud/__init__.py` (wherever cloud app startup lives — grep for `ConnectionManager` or `manager = ConnectionManager()`)
- Modify: `src/pocketpaw/dashboard_lifecycle.py` if cloud bus init belongs in startup hooks
- Test: `tests/cloud/realtime/test_wiring.py`

**Step 1: Write failing test**

```python
# tests/cloud/realtime/test_wiring.py
from ee.cloud.realtime.bus import get_bus, _bus
from ee.cloud import init_realtime  # will define in step 2

def test_init_realtime_sets_inprocess_bus_by_default(monkeypatch):
    monkeypatch.delenv("POCKETPAW_REALTIME_BUS", raising=False)
    init_realtime()
    assert type(get_bus()).__name__ == "InProcessBus"
```

Run: `uv run pytest tests/cloud/realtime/test_wiring.py -v` → FAIL.

**Step 2: Implement `init_realtime()` in `ee/cloud/__init__.py`**

```python
# ee/cloud/__init__.py (add)
import os
from ee.cloud.chat.ws import manager as _conn_manager
from ee.cloud.realtime.audience import AudienceResolver
from ee.cloud.realtime.bus import InProcessBus, set_bus

def init_realtime() -> None:
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.workspace.service import WorkspaceService

    resolver = AudienceResolver(
        group_members=GroupService.list_member_ids,       # async (group_id) -> list[str]
        workspace_members=WorkspaceService.list_member_ids,
        workspace_admins=WorkspaceService.list_admin_ids,
        workspace_peers=WorkspaceService.list_peers,
    )
    mode = os.environ.get("POCKETPAW_REALTIME_BUS", "inprocess")
    if mode == "redis":
        from ee.cloud.realtime.redis_bus import RedisBus
        set_bus(RedisBus(resolver=resolver, conn_manager=_conn_manager))
    else:
        set_bus(InProcessBus(resolver=resolver, conn_manager=_conn_manager))
```

Add required helper methods on services — use `list_member_ids` style async classmethods that return `list[str]`. Implement using Beanie `find` projection.

Call `init_realtime()` from the cloud startup path (look for existing `@app.on_event("startup")` or equivalent).

**Step 3: Verify**

Run: `uv run pytest tests/cloud/realtime/test_wiring.py -v` → PASS.

**Step 4: Commit**

```bash
git add ee/cloud/__init__.py ee/cloud/realtime/
git commit -m "feat(realtime): wire EventBus into cloud startup"
```

---

## Phase 2 — Canary: refactor `agent_bridge` to use `emit()`

### Task 7: Replace direct `ws_manager.broadcast_to_group` calls with `emit()`

**Files:**
- Modify: `ee/cloud/shared/agent_bridge.py:221-360` (the five existing broadcast sites)
- Test: `tests/cloud/shared/test_agent_bridge_emits.py`

**Step 1: Write failing test**

```python
# tests/cloud/shared/test_agent_bridge_emits.py
import pytest
from unittest.mock import AsyncMock, patch
from ee.cloud.shared.agent_bridge import broadcast_agent_thinking  # or whatever the fn names are

@pytest.mark.asyncio
async def test_agent_thinking_fires_emit():
    with patch("ee.cloud.shared.agent_bridge.emit", new=AsyncMock()) as m_emit:
        await broadcast_agent_thinking(group_id="g1", agent_id="a1")
        m_emit.assert_awaited_once()
        ev = m_emit.await_args.args[0]
        assert ev.type == "agent.thinking"
        assert ev.data["group_id"] == "g1"
        assert ev.data["agent_id"] == "a1"
```

Write one test per current broadcast site in `agent_bridge.py`.

Run: `uv run pytest tests/cloud/shared/test_agent_bridge_emits.py -v` → FAIL.

**Step 2: Refactor `agent_bridge.py`**

Replace every `await ws_manager.broadcast_to_group(group_id, member_ids, WsOutbound(type=X, data=Y))` with `await emit(EventClass(data=Y))`. Delete the manual member-id lookups — the resolver owns that.

**Step 3: Run existing agent-bridge tests + new tests**

Run:
- `uv run pytest tests/cloud/shared/ -v`
- `uv run pytest tests/cloud/ -v --ignore=tests/e2e`

Expected: all pass. Existing agent integration tests continue to work because events still reach the same clients — just via the bus.

**Step 4: Commit**

```bash
git add ee/cloud/shared/agent_bridge.py tests/cloud/shared/test_agent_bridge_emits.py
git commit -m "refactor(realtime): route agent_bridge through emit() canary"
```

---

## Phase 3 — Route existing message + typing emits through the bus

### Task 8: Refactor `MessageService` to emit through bus

**Files:**
- Modify: `ee/cloud/chat/message_service.py` (and wherever `message.new` / `message.edited` / `message.deleted` / `message.reaction.*` are currently broadcast — `ee/cloud/chat/router.py:463-610` per design exploration)
- Test: `tests/cloud/chat/test_message_emits.py`

**Step 1: Write failing test**

```python
# tests/cloud/chat/test_message_emits.py
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_send_message_emits_message_new_and_sent(group_fixture, user_fixture):
    from ee.cloud.chat.message_service import MessageService
    from ee.cloud.chat.schemas import SendMessageRequest
    with patch("ee.cloud.chat.message_service.emit", new=AsyncMock()) as m_emit:
        body = SendMessageRequest(content="hi")
        await MessageService.send_message(group_fixture.id, user_fixture.id, body)

    types = [c.args[0].type for c in m_emit.await_args_list]
    assert "message.new" in types
    assert "message.sent" in types
```

Add tests for edit/delete/react/unreact.

Run → FAIL.

**Step 2: Refactor**

1. Move the existing `manager.broadcast_to_group` / `manager.send_to_user` calls out of `chat/router.py` and into `MessageService` methods.
2. Replace with `emit(MessageNew(...))`, `emit(MessageSent(...))`, etc.
3. Keep the router thin — just: validate → call service → return.

**Step 3: Run tests**

Run: `uv run pytest tests/cloud/chat/ -v`
Expected: all pass. Existing end-to-end message tests should be unchanged.

**Step 4: Commit**

```bash
git add ee/cloud/chat/message_service.py ee/cloud/chat/router.py tests/cloud/chat/test_message_emits.py
git commit -m "refactor(realtime): route message events through bus"
```

---

### Task 9: Move typing / read.ack handlers through bus

**Files:**
- Modify: `ee/cloud/chat/router.py` (`_ws_typing`, `_ws_read_ack`)
- Test: `tests/cloud/chat/test_typing_emits.py`

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_typing_start_emits_to_room_joined_members_only():
    # Details: two users joined room, one not joined → only joined user receives
    ...

@pytest.mark.asyncio
async def test_read_ack_emits_message_read():
    ...
```

**Step 2: Refactor**

Introduce room-scoped routing in `ConnectionManager`:
- Add `join_room(ws, group_id)` / `leave_room(ws)` / `send_to_room_members(group_id, members, event)` where members intersect only sockets whose `current_room == group_id`.
- `emit()` for `typing.*` / `message.read` calls a new `ConnectionManager.send_room(...)` instead of fanout by user_id.
- Alternative: `AudienceResolver` returns `(user_ids, room_id)`; bus consults manager. Simpler: route room-scoped events via a distinct bus method `publish_room(event, group_id)`.

**Step 3: Tests pass.**

Run: `uv run pytest tests/cloud/chat/test_typing_emits.py -v` → PASS.

**Step 4: Commit**

```bash
git commit -m "feat(realtime): room-scoped fanout for typing + read.ack"
```

---

## Phase 4 — Group emits

### Task 10: Emit from `GroupService.create_group`

**Files:**
- Modify: `ee/cloud/chat/group_service.py`
- Test: `tests/cloud/chat/test_group_emits.py`

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_create_group_emits_group_created(user_fixture):
    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.schemas import CreateGroupRequest
    with patch("ee.cloud.chat.group_service.emit", new=AsyncMock()) as m_emit:
        body = CreateGroupRequest(name="test", member_ids=["u2","u3"])
        g = await GroupService.create_group("workspace1", user_fixture.id, body)
    m_emit.assert_awaited()
    ev = m_emit.await_args_list[0].args[0]
    assert ev.type == "group.created"
    assert set(ev.data["member_ids"]) >= {user_fixture.id, "u2", "u3"}
```

Run → FAIL.

**Step 2: Implement**

Add `await emit(GroupCreated(data={...}))` after the `await group.insert()` in `GroupService.create_group`.

**Step 3: Pass.** Commit.

```bash
git commit -m "feat(realtime): emit group.created"
```

---

### Task 11: Emit group.updated / group.deleted

Mirror of Task 10 for `update_group` and `delete_group`. One commit per event to keep diffs small.

**Commits:**
- `feat(realtime): emit group.updated`
- `feat(realtime): emit group.deleted`

---

### Task 12: Emit group.member_added / _removed / _role

**Files:**
- Modify: `ee/cloud/chat/group_service.py`
- Test: extend `tests/cloud/chat/test_group_emits.py`

For `add_members`, emit one `GroupMemberAdded` per new user_id so each audience resolution is correct (the new user receives it; existing members also do). After commit, call `resolver.invalidate_group(group_id)` so the cached member list refreshes.

Same pattern for `remove_member` (emit before resolver cache refresh so the removed user is still in the audience — or pass their id explicitly via the event payload, which the resolver branch already handles).

**Commits (one each):**
- `feat(realtime): emit group.member_added`
- `feat(realtime): emit group.member_removed`
- `feat(realtime): emit group.member_role`

---

### Task 13: Emit group.agent_* and group.pinned / unpinned

Mechanical repetition of Task 10 for each method. One commit each.

---

## Phase 5 — Workspace emits

### Task 14: Emit workspace.updated / deleted / member_* / invite.*

**Files:**
- Modify: `ee/cloud/workspace/service.py`
- Test: `tests/cloud/workspace/test_workspace_emits.py`

One test + one emit per mutation. One commit each. Follow Task 10's pattern.

After membership mutations: `resolver.invalidate_workspace(workspace_id)`.

---

## Phase 6 — Session / DM emits

### Task 15: Emit session.created / updated / deleted

**Files:**
- Modify: `ee/cloud/sessions/service.py`
- Test: `tests/cloud/sessions/test_session_emits.py`

`session.created` only fires when `get_or_create` actually creates — not on retrieval. `session.updated` fires as a side effect of DM message send (call from `MessageService` when group is a DM type).

**Commits:**
- `feat(realtime): emit session.created on DM creation`
- `feat(realtime): emit session.updated on DM message`
- `feat(realtime): emit session.deleted`

---

## Phase 7 — Upload emits

### Task 16: Emit file.ready / file.deleted from EE upload service

**Files:**
- Modify: `ee/cloud/uploads/service.py`
- Test: `tests/cloud/uploads/test_upload_emits.py`

Emit `file.ready` from `EEUploadService.upload` when `chat_id` is set (no emit for avatar/KB uploads — those are out of scope).

**Commits:**
- `feat(realtime): emit file.ready`
- `feat(realtime): emit file.deleted`

---

## Phase 8 — Notifications (derived)

### Task 17: `notifications` Beanie collection

**Files:**
- Create: `ee/cloud/notifications/__init__.py`
- Create: `ee/cloud/notifications/models.py`
- Create: `ee/cloud/notifications/service.py`
- Create: `ee/cloud/notifications/router.py`
- Test: `tests/cloud/notifications/test_models.py`

```python
# ee/cloud/notifications/models.py
class Notification(TimestampedDocument):
    user_id: Indexed(str)
    kind: Literal["mention","reaction","invite","dm"]
    source_id: str          # message_id or invite_id
    preview: str
    read_at: datetime | None = None

    class Settings:
        name = "notifications"
        indexes = [[("user_id", 1), ("read_at", 1), ("createdAt", -1)]]
```

Add REST endpoints:
- `GET /notifications?unread=true` — paginated
- `POST /notifications/{id}/read`
- `POST /notifications/clear`

**Commit:** `feat(notifications): add Notification model + REST`

---

### Task 18: Derive notifications from message events

**Files:**
- Modify: `ee/cloud/chat/message_service.py`
- Test: `tests/cloud/notifications/test_derived.py`

On `send_message`: for each mention in `body.mentions`, create a `Notification(kind="mention")` and emit `notification.new` with its payload. Audience = `[user_id]`.

On `react`: if target message sender != reactor, create `Notification(kind="reaction")`, emit.

Keep derivation inside the service — don't subscribe to bus events; it's simpler, and the bus is fanout-only.

**Commits:**
- `feat(notifications): derive mention notifications from message.new`
- `feat(notifications): derive reaction notifications from message.react`
- `feat(notifications): derive invite notifications from workspace.invite.created`

---

## Phase 9 — Client: RealtimeClient + reconcile

### Task 19: Scaffold `core/realtime/` module

**Files:**
- Create: `paw-enterprise/src/lib/core/realtime/client.ts`
- Create: `paw-enterprise/src/lib/core/realtime/dispatcher.ts`
- Create: `paw-enterprise/src/lib/core/realtime/reconcile.ts`
- Create: `paw-enterprise/src/lib/core/realtime/types.ts`
- Test: `paw-enterprise/src/lib/core/realtime/__tests__/client.test.ts`

**Step 1: Write failing test**

```ts
// __tests__/client.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { RealtimeClient } from '../client';

describe('RealtimeClient', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as any).WebSocket = vi.fn(() => ({
      send: vi.fn(), close: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }));
  });

  it('opens a socket to /ws/cloud with token', () => {
    const c = new RealtimeClient('ws://x');
    c.connect('jwt123');
    expect((globalThis as any).WebSocket).toHaveBeenCalledWith(
      'ws://x/ws/cloud?token=jwt123'
    );
  });

  it('backs off exponentially on close', async () => {
    const c = new RealtimeClient('ws://x');
    c.connect('t');
    // simulate close event → expect next connect after 1s
    const ws = (globalThis as any).WebSocket.mock.results[0].value;
    ws.onclose?.({});
    vi.advanceTimersByTime(1000);
    expect((globalThis as any).WebSocket).toHaveBeenCalledTimes(2);
  });
});
```

Run: `bun run test src/lib/core/realtime/__tests__/client.test.ts` → FAIL.

**Step 2: Implement `RealtimeClient`**

```ts
// client.ts
export class RealtimeClient {
  private ws: WebSocket | null = null;
  private state: 'idle' | 'connecting' | 'open' | 'reconnecting' = 'idle';
  private backoff = 1000;
  private buffer: any[] = [];
  private currentRoom: string | null = null;
  private token: string | null = null;

  constructor(private baseUrl: string, private onOpen?: () => void) {}

  connect(token: string): void {
    this.token = token;
    this.state = 'connecting';
    this.ws = new WebSocket(`${this.baseUrl}/ws/cloud?token=${encodeURIComponent(token)}`);
    this.ws.onopen = () => this.handleOpen();
    this.ws.onclose = () => this.handleClose();
    this.ws.onmessage = (e) => this.handleMessage(e);
    this.ws.onerror = () => {};
  }

  private handleOpen() {
    this.state = 'open';
    this.backoff = 1000;
    this.onOpen?.();
  }

  private handleClose() {
    if (this.state === 'idle') return;
    this.state = 'reconnecting';
    const jitter = Math.random() * 0.4 - 0.2;
    const delay = Math.min(30000, this.backoff * (1 + jitter));
    setTimeout(() => { if (this.token) this.connect(this.token); }, delay);
    this.backoff = Math.min(30000, this.backoff * 2);
  }

  private handleMessage(e: MessageEvent) { /* fill in Task 20 */ }

  joinRoom(groupId: string) {
    this.currentRoom = groupId;
    this.ws?.send(JSON.stringify({ type: 'room.join', group_id: groupId }));
  }
  leaveRoom(groupId: string) {
    if (this.currentRoom === groupId) this.currentRoom = null;
    this.ws?.send(JSON.stringify({ type: 'room.leave', group_id: groupId }));
  }
  send(type: string, data: object) {
    this.ws?.send(JSON.stringify({ type, ...data }));
  }
  disconnect() { this.state = 'idle'; this.ws?.close(); }
}
```

**Step 3: Pass.** Commit.

```bash
git add paw-enterprise/src/lib/core/realtime/
git commit -m "feat(realtime): scaffold RealtimeClient with backoff"
```

---

### Task 20: Dispatcher + buffer/flush

**Files:**
- Modify: `paw-enterprise/src/lib/core/realtime/dispatcher.ts`
- Modify: `paw-enterprise/src/lib/core/realtime/client.ts` (handleMessage)
- Test: `paw-enterprise/src/lib/core/realtime/__tests__/dispatcher.test.ts`

**Step 1: Test**

```ts
it('unknown event types are no-op', () => {
  const fn = vi.fn();
  const dispatcher = new Dispatcher({ 'known.event': fn });
  dispatcher.dispatch({ type: 'unknown.event', data: {} });
  expect(fn).not.toHaveBeenCalled();
});

it('events during buffer window are flushed after close', () => {
  const fn = vi.fn();
  const dispatcher = new Dispatcher({ 'x.y': fn });
  dispatcher.openBuffer();
  dispatcher.dispatch({ type: 'x.y', data: 1 });
  expect(fn).not.toHaveBeenCalled();
  dispatcher.closeBuffer();
  expect(fn).toHaveBeenCalledWith(1);
});
```

**Step 2: Implement**, wire `handleMessage` to route through dispatcher (buffer if open, dispatch otherwise).

**Step 3: Pass.** Commit.

---

### Task 21: Reconcile orchestrator

**Files:**
- Modify: `paw-enterprise/src/lib/core/realtime/reconcile.ts`
- Test: `paw-enterprise/src/lib/core/realtime/__tests__/reconcile.test.ts`

**Step 1: Test**

```ts
it('buffer → refetch → flush in order; duplicate events are idempotent', async () => {
  const store = { upsert: vi.fn(), map: new Map() };
  const fetcher = vi.fn().mockResolvedValue([{id:'g1',name:'g'}]);
  const reconciler = new Reconciler({ groups: { fetch: fetcher, store } });

  reconciler.openBuffer();
  reconciler.handleEvent({ type: 'group.updated', data: { group_id: 'g1', name: 'new' } });
  await reconciler.reconcile();
  reconciler.flushBuffer();

  // First the refetch set 'g', then flush applied 'new' → final is 'new'
  expect(store.upsert).toHaveBeenLastCalledWith(expect.objectContaining({ name: 'new' }));
});
```

**Step 2: Implement** — `reconcile()` runs REST refetches in parallel, calls `store.replace(items)` per surface, then `flushBuffer()`.

**Step 3: Commit.**

```bash
git commit -m "feat(realtime): reconcile on reconnect with buffered flush"
```

---

## Phase 10 — Client handlers

### Task 22: Store reconciliation helpers

**Files:**
- Modify: every store in `paw-enterprise/src/lib/stores/` that holds chat state (`chat.svelte.ts` and friends)
- Add to each: `upsert(item)`, `patch(id, fields)`, `remove(id)`, `replace(items)`.
- Test: smoke tests per store

**Commit:** `refactor(stores): add by-id reconciliation helpers`

---

### Task 23-30: Per-surface handlers

One task per handler file. Pattern:

```ts
// handlers/group.ts
import { stores } from '$lib/stores';

export const groupHandlers = {
  onCreated: (data: any) => stores.groups.upsert(data),
  onUpdated: (data: any) => stores.groups.patch(data.group_id, data),
  onDeleted: (data: any) => {
    stores.groups.remove(data.group_id);
    if (stores.chat.currentGroupId === data.group_id) stores.chat.currentGroupId = null;
  },
  onMemberAdded: (data: any) => stores.groups.patchMember(data.group_id, data.user_id, { role: data.role }),
  onMemberRemoved: (data: any) => stores.groups.removeMember(data.group_id, data.user_id),
  // ...
};
```

Register each in `dispatcher.ts`'s handler map.

**Commits (one per surface):**
- `feat(realtime): group handlers`
- `feat(realtime): workspace handlers`
- `feat(realtime): message handlers`
- `feat(realtime): presence handlers`
- `feat(realtime): session handlers`
- `feat(realtime): file handlers`
- `feat(realtime): agent handlers`
- `feat(realtime): notification handlers`

---

### Task 31: Mount RealtimeClient in root layout + feature flag

**Files:**
- Modify: `paw-enterprise/src/routes/+layout.svelte`
- Modify: `paw-enterprise/.env.example` (add `VITE_REALTIME_V2=false`)

Behind `import.meta.env.VITE_REALTIME_V2 === 'true'`, wire `realtime.connect(token)` on mount + `disconnect()` on unmount. Else keep existing `core/chat/socket.ts` path.

**Commit:** `feat(realtime): mount RealtimeClient behind VITE_REALTIME_V2 flag`

---

## Phase 11 — Cutover

### Task 32: Flip flag + delete socket.io

**Files:**
- Modify: `paw-enterprise/src/routes/+layout.svelte` (remove flag check)
- Delete: `paw-enterprise/src/lib/core/shared/socket.ts`
- Delete: `paw-enterprise/src/lib/core/notifications/socket.ts`
- Delete: `paw-enterprise/src/lib/core/chat/socket.ts`
- Modify: `paw-enterprise/package.json` (remove `socket.io-client`)

Run: `bun install && bun run check && bun run test`

**Manual verification:**

- [ ] Two tabs, create group with other user → instant sidebar update on both.
- [ ] Invite non-member → appears without refresh, acceptance clears admin's pending list.
- [ ] Remove user from group → their group view closes.
- [ ] 20-second disconnect + 3 mutations + reconnect → UI reconciles.

**Commit:** `feat(realtime): cutover to /ws/cloud; delete socket.io`

---

## Phase 12 — Redis bus (follow-up PR)

### Task 33: `RedisBus` implementation

**Files:**
- Create: `ee/cloud/realtime/redis_bus.py`
- Test: `tests/cloud/realtime/test_redis_bus.py` (fakeredis)

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_redis_bus_cross_instance_fanout(fakeredis_factory):
    r1 = fakeredis_factory()
    r2 = fakeredis_factory()  # same backing store
    conn_a = AsyncMock()
    conn_b = AsyncMock()
    bus_a = RedisBus(redis=r1, resolver=resolver_a, conn_manager=conn_a)
    bus_b = RedisBus(redis=r2, resolver=resolver_b, conn_manager=conn_b)
    await bus_a.start()
    await bus_b.start()

    await bus_a.publish(GroupCreated(data={"group_id":"g","member_ids":["u1"]}))
    await asyncio.sleep(0.05)

    # Both instances fanned out to their local sockets
    assert conn_a.send_to_user.await_count == 1
    assert conn_b.send_to_user.await_count == 1
```

**Step 2: Implement**

```python
# ee/cloud/realtime/redis_bus.py
class RedisBus:
    CHANNEL = "pocketpaw:realtime"

    def __init__(self, redis, resolver, conn_manager):
        self._redis = redis
        self._resolver = resolver
        self._conn = conn_manager
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._subscribe_loop())

    async def publish(self, event):
        payload = json.dumps({"type": event.type, "data": event.data})
        await self._redis.publish(self.CHANNEL, payload)

    async def _subscribe_loop(self):
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self.CHANNEL)
        async for message in pubsub.listen():
            if message["type"] != "message": continue
            raw = json.loads(message["data"])
            event = Event(type=raw["type"], data=raw["data"])
            audience = await self._resolver.audience(event)
            payload = WsOutbound(type=event.type, data=event.data)
            for uid in audience:
                try: await self._conn.send_to_user(uid, payload)
                except Exception: pass
```

**Step 3: Pass.** Commit.

```bash
git commit -m "feat(realtime): Redis-backed EventBus for multi-instance"
```

---

### Task 34: Document `POCKETPAW_REALTIME_BUS`

**Files:**
- Modify: `docs/deployment/` (add a realtime-scaling page if absent)
- Modify: `README.md` (env-var table if present)

**Commit:** `docs(realtime): document Redis bus and multi-instance scaling`

---

## Verification checklist

Before declaring done:

- [ ] `uv run pytest --ignore=tests/e2e` — all green
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] `cd paw-enterprise && bun run check && bun run test`
- [ ] All manual checks in Task 32 pass.
- [ ] `docs/wiki/` rebuilt (post-commit hook).

---

**Total task count:** ~34 tasks across 12 phases. Phases 1-8 land independently. Phase 9-11 land as a second PR once backend emits are live. Phase 12 is a follow-up.
