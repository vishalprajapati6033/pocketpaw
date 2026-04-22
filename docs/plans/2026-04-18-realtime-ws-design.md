# Global Realtime over `/ws/cloud` — Design

**Date:** 2026-04-18
**Scope:** EE cloud (`ee/cloud/` backend + paw-enterprise client). Eliminate the "refresh after every step" UX by emitting WebSocket events for every server-side mutation across groups, workspaces, messages, presence, files, sessions, agent activity, and notifications.
**Status:** Approved — ready for implementation plan

## Goal

Every mutation the user or the server makes to chat-related state should appear in every relevant client in realtime, without a page refresh. Reuse the existing `/ws/cloud` endpoint and `ConnectionManager`; add a thin event-bus abstraction so future multi-instance deployments swap to Redis pub/sub without touching call sites.

## Scope decisions

- **Tier**: EE only. OSS dashboard already has its own per-chat WebSocket and is out of scope.
- **Transport**: one native WebSocket per user tab at `/ws/cloud?token=<JWT>`. Retire `socket.io-client` usage (`core/shared/socket.ts`, `core/notifications/socket.ts`).
- **Catch-up model**: reconcile-on-reconnect. WS delivers live events only; on `open` the client re-fetches authoritative state via existing REST. No event log, no resume cursors.
- **Topology**: `EventBus` protocol. `InProcessBus` is the default (single FastAPI process). `RedisBus` added behind `POCKETPAW_REALTIME_BUS=redis` for multi-instance. Call sites don't change.
- **Subscription model (hybrid)**:
  - User-scoped events auto-deliver to all of a user's connected sockets based on server-known membership (workspaces, groups).
  - Room-local high-frequency events (`typing.*`, `read.ack`-derived `message.read`) require explicit `room.join` per focused group.
- **Transactional boundary**: emit after DB commit, never inside. WS fanout failures never abort a mutation.

## Architecture

```
FastAPI process (ee/cloud)
  Service layer (group, message, workspace, invite, session, upload, agent)
      │
      │  await emit(event)
      ▼
  ┌──────────────┐      ┌──────────────────┐
  │  EventBus    │────▶ │ InProcessBus     │  (default)
  │  (Protocol)  │      └──────────────────┘
  │              │      ┌──────────────────┐
  │              │────▶ │ RedisBus         │ ── pub/sub ──▶ other instances
  └──────┬───────┘      └──────────────────┘
         │
         ▼
  AudienceResolver (event → list[user_id])
         │
         ▼
  ConnectionManager.send_to_user   (existing)
         │
         ▼
  /ws/cloud?token=JWT

paw-enterprise client
  ├── RealtimeClient singleton (/ws/cloud)
  ├── dispatcher → handlers/{group,workspace,message,...}.ts
  ├── store reconcilers (upsert/patch/remove by id)
  └── room.join for typing + read.ack only
```

### Principles

- **Services emit, routers don't.** A mutation that runs via REST today and via an agent tool tomorrow fires the same event from the same line.
- **Audience resolution in one place.** One `AudienceResolver.audience(event)` function, one branch per event type. Call sites stay one line.
- **Client is dumb.** Receive, dispatch, mutate store by id. No ordering logic, no dedupe — stores are idempotent.
- **Reconnect = refetch + buffered flush.** On `open`, buffer incoming events for 200 ms, fire REST refetches for the mounted screens, replace store contents, flush buffer. Late duplicates are no-ops.

## Files touched

```
ee/cloud/realtime/
├── __init__.py
├── bus.py              EventBus protocol + InProcessBus + get_bus() factory
├── redis_bus.py        RedisBus (loaded only when POCKETPAW_REALTIME_BUS=redis)
├── events.py           Typed Event dataclasses for all surfaces
├── audience.py         AudienceResolver (event → user_ids) with 2s TTL cache
└── emit.py             Thin facade: async def emit(event) -> None

ee/cloud/chat/ws.py     ConnectionManager untouched; InProcessBus wraps it
ee/cloud/chat/group_service.py       emit() after mutations
ee/cloud/chat/message_service.py     emit() after mutations
ee/cloud/workspace/service.py        emit() after mutations
ee/cloud/sessions/service.py         emit() after mutations
ee/cloud/uploads/service.py (EE)     emit() on put/delete with chat_id
ee/cloud/shared/agent_bridge.py      refactor: replace ws_manager.broadcast_to_group with emit()

paw-enterprise/src/lib/core/realtime/
├── client.ts           RealtimeClient singleton (replaces core/chat/socket.ts)
├── dispatcher.ts       type → handler map
├── reconcile.ts        post-reconnect refetch orchestrator
└── handlers/
    ├── group.ts
    ├── workspace.ts
    ├── message.ts
    ├── presence.ts
    ├── session.ts
    ├── file.ts
    ├── agent.ts
    └── notification.ts
```

Deleted:
- `paw-enterprise/src/lib/core/shared/socket.ts`
- `paw-enterprise/src/lib/core/notifications/socket.ts`
- `paw-enterprise/src/lib/core/chat/socket.ts` (folded into `realtime/client.ts`)
- `socket.io-client` dependency

## Event catalog

All events: `{type, data, ts}`. Audience resolved server-side by `AudienceResolver`.

### Workspace
| type | audience | payload |
|---|---|---|
| `workspace.updated` | members | `{workspace_id, name, icon, ...}` |
| `workspace.deleted` | members | `{workspace_id}` |
| `workspace.member_added` | members + new user | `{workspace_id, user_id, role}` |
| `workspace.member_removed` | remaining + removed user | `{workspace_id, user_id}` |
| `workspace.member_role` | members | `{workspace_id, user_id, role}` |
| `workspace.invite.created` | admins + invitee (if registered) | `{workspace_id, invite_id, email}` |
| `workspace.invite.accepted` | admins + invitee | `{workspace_id, invite_id, user_id}` |
| `workspace.invite.revoked` | admins + invitee | `{workspace_id, invite_id}` |

### Groups
| type | audience | payload |
|---|---|---|
| `group.created` | new members | full `GroupResponse` |
| `group.updated` | members | `{group_id, name, icon, color, type, archived}` |
| `group.deleted` | last-known members | `{group_id}` |
| `group.member_added` | members + added user | `{group_id, user_id, role}` |
| `group.member_removed` | remaining + removed user | `{group_id, user_id}` |
| `group.member_role` | members | `{group_id, user_id, role}` |
| `group.agent_added` / `_removed` / `_updated` | members | `{group_id, agent_id, ...}` |
| `group.pinned` / `group.unpinned` | members | `{group_id, message_id}` |

### Messages
| type | audience | payload |
|---|---|---|
| `message.new` | members (exc. sender) | `MessageResponse` |
| `message.sent` | sender only | `MessageResponse` |
| `message.edited` | members | `MessageResponse` |
| `message.deleted` | members | `{group_id, message_id}` |
| `message.reaction.added` / `_removed` | members | `{group_id, message_id, emoji, user_id}` |
| `message.read` | members (room-joined) | `{group_id, user_id, up_to_message_id}` |
| `group.unread_delta` | specific user | `{group_id, unread}` |

### Presence
| type | audience | payload |
|---|---|---|
| `presence.online` | workspace peers | `{user_id, workspaces: [...]}` |
| `presence.offline` | workspace peers | `{user_id}` (after 30s grace — infra exists) |
| `typing.start` / `typing.stop` | room-joined group members | `{group_id, user_id}` |

### Files (EE upload service, chat-scoped only)
| type | audience | payload |
|---|---|---|
| `file.ready` | chat members | `{group_id, file_id, filename, mime, size, url}` |
| `file.deleted` | chat members | `{group_id, file_id}` |

### Sessions / DMs
| type | audience | payload |
|---|---|---|
| `session.created` | both participants | `{session_id, agent_id, peer_id}` |
| `session.updated` | participants | `{session_id, last_message_at, last_message_preview}` |
| `session.deleted` | participants | `{session_id}` |

### Agent activity (`agent_bridge` — already emits, now through `emit()`)
| type | audience | payload |
|---|---|---|
| `agent.thinking` | members | `{group_id, agent_id}` |
| `agent.tool_start` | members | `{group_id, agent_id, tool, args_preview}` |
| `agent.tool_result` | members | `{group_id, agent_id, tool, ok, summary}` |
| `agent.error` | members | `{group_id, agent_id, error}` |
| `agent.stream_chunk` | members | `{group_id, message_id, chunk}` |
| `agent.stream_end` | members | `{group_id, message_id}` |

### Notifications (unified feed)
| type | audience | payload |
|---|---|---|
| `notification.new` | specific user | `{id, kind, source_id, preview, created_at}` |
| `notification.read` | user's other tabs | `{id}` |
| `notification.cleared` | user's other tabs | `{}` |

Derived server-side by listening on `message.new` (mentions), `message.reaction.added` (reactions to your message), and `workspace.invite.created`. Persisted in a new `notifications` Beanie collection.

## Client wiring

### RealtimeClient

```ts
class RealtimeClient {
  private ws: WebSocket | null = null;
  private state: 'idle' | 'connecting' | 'open' | 'reconnecting' = 'idle';
  private backoff = 1000;          // 1s → 2s → 4s → ... cap 30s with ±20% jitter
  private buffer: Event[] = [];
  private currentRoom: string | null = null;

  connect(token: string): void
  disconnect(): void
  joinRoom(groupId: string): void
  leaveRoom(groupId: string): void
  send(type: string, data: object): void     // typing.start/stop, read.ack
}
```

Singleton exported from `core/realtime/client.ts`, mounted once in the root layout.

### Reconnect + reconcile

On every `open` (first connect AND reconnect):

1. Start 200 ms buffer window — events queued, not dispatched.
2. Fire REST refetches in parallel for mounted screens:
   - Always: `GET /workspaces`, `GET /workspaces/{current}/members`, `GET /groups`, `GET /notifications?unread=true`
   - Current group view: `GET /groups/{id}`, `GET /groups/{id}/messages?cursor=last_seen_id`
   - Current DM view: `GET /sessions/{id}`, `GET /sessions/{id}/messages?cursor=last_seen_id`
3. Replace store contents (set-by-id is idempotent).
4. Flush `buffer` through the dispatcher. Late duplicates are no-ops.
5. `joinRoom(currentGroupId)` if a room view is mounted.

### Dispatcher

```ts
const handlers: Record<string, (data: any) => void> = {
  'group.created':            groupHandlers.onCreated,
  'group.updated':            groupHandlers.onUpdated,
  'group.member_added':       groupHandlers.onMemberAdded,
  'workspace.invite.created': workspaceHandlers.onInviteCreated,
  'message.new':              messageHandlers.onNew,
  // one entry per event type
};

export function dispatch(event: {type: string; data: any}) {
  handlers[event.type]?.(event.data);
}
```

### Store pattern

Every store exposes `upsert(item)`, `patch(id, fields)`, `remove(id)`. Handlers call these by id; order-independence is a property, not a contract.

### Room join

- Enter group/DM view → `{type: "room.join", group_id}`.
- Leave view → `{type: "room.leave", group_id}`.
- Server tracks `socket → current_room`. `typing.*` and `message.read` only fan out to members currently joined to the room. All other events still use `user_id` fanout regardless.

## Server-side emit wiring

### Facade

```python
# ee/cloud/realtime/emit.py
from ee.cloud.realtime.bus import get_bus
from ee.cloud.realtime.events import Event

async def emit(event: Event) -> None:
    await get_bus().publish(event)
```

### Call sites

| Service | Method | Event(s) |
|---|---|---|
| `GroupService` | `create_group` | `group.created` |
| | `update_group` | `group.updated` |
| | `delete_group` | `group.deleted` |
| | `add_members` | `group.member_added` (batched one per user_id) |
| | `remove_member` | `group.member_removed` |
| | `update_member_role` | `group.member_role` |
| | `add_agent` / `remove_agent` / `update_agent` | `group.agent_added/removed/updated` |
| | `pin_message` / `unpin_message` | `group.pinned/unpinned` |
| `MessageService` | `send_message` | `message.new` + `message.sent` + derived `notification.new` (mentions) + per-recipient `group.unread_delta` |
| | `edit_message` | `message.edited` |
| | `delete_message` | `message.deleted` |
| | `react` / `unreact` | `message.reaction.*` + derived `notification.new` if target != actor |
| | `mark_read` (new) | `message.read` + `group.unread_delta` to reader |
| `WorkspaceService` | `update` / `delete` | `workspace.updated/deleted` |
| | `add_member` / `remove_member` / `set_role` | `workspace.member_*` |
| | `create_invite` / `accept_invite` / `revoke_invite` | `workspace.invite.*` |
| `SessionService` | `get_or_create` (only when created) | `session.created` |
| | hook on DM message | `session.updated` |
| | `delete_session` | `session.deleted` |
| `EEUploadService` | `upload` with `chat_id` | `file.ready` |
| | `delete` | `file.deleted` |
| `agent_bridge` | existing hooks | keep events; route through `emit()` |

### AudienceResolver (sketch)

One branch per event type. Caches `_group_member_ids` and `_workspace_member_ids` with 2s TTL keyed by id. Invalidate on membership mutation.

### EventBus

```python
class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

class InProcessBus:
    def __init__(self, resolver, conn_manager): ...
    async def publish(self, event):
        for uid in await self._resolver.audience(event):
            await self._conn.send_to_user(uid, WsOutbound(type=event.type, data=event.data))

class RedisBus:
    # publish → Redis PUBLISH on channel "realtime:events" with msgpack/json payload
    # background task on start → SUBSCRIBE; on message, fan out to local sockets
    # reconnects with exponential backoff
```

Factory reads `POCKETPAW_REALTIME_BUS` (`inprocess` default, `redis` opt-in).

### Transactional boundary

```python
await group.save()
await emit(GroupCreated(...))  # errors swallowed + logged; never aborts the mutation
```

Emit runs after commit. Failures are logged, not raised — the user's next reconnect refetch re-synchronizes state.

## Error handling

- Fanout exception on a single socket → disconnect it (existing `send_to_user` already handles dead sockets).
- Audience resolution DB error → log, skip that event; client reconciles on next mutation or reconnect.
- Redis down (RedisBus) → log + retry connect; client still has its local socket open, just sees delayed events. REST stays fully functional.
- Client missed events (tab asleep) → reconnect triggers reconcile. Zero data loss for *state*; transient events (typing, tool_start) are lost by design.

## Testing

### Backend

- **Unit — `AudienceResolver`** (`tests/cloud/realtime/test_audience.py`): correct user_ids per event type; removed-member events include removed user; cache invalidation on membership change.
- **Unit — `InProcessBus`** (`tests/cloud/realtime/test_bus.py`): publish calls send_to_user for every audience member; exceptions isolated per handler.
- **Unit — `RedisBus`** (`tests/cloud/realtime/test_redis_bus.py`, fakeredis): cross-instance fanout; ordering per publisher; reconnect after broker drop.
- **Integration — emit sites** (`tests/cloud/chat/test_group_emits.py`, `test_workspace_emits.py`, `test_session_emits.py`, `test_upload_emits.py`): call each service method with a bus spy; assert expected event + audience.
- **Integration — end-to-end** (`tests/cloud/realtime/test_e2e.py`): two TestClient WS connections; verify group.created/member_added/invite flows reach the right users within 500 ms.

### Client

- `dispatcher.test.ts`: unknown types no-op.
- `reconcile.test.ts`: buffer → refetch → flush ordering; duplicate events idempotent.
- `client.test.ts`: exponential backoff with jitter; resubscribe current room on reconnect.

### Manual verification before merge

- [ ] Two tabs: A creates group including B → B sidebar updates without refresh.
- [ ] Invite non-member → invitee receives invite card; accepting removes it from admin's pending list live.
- [ ] Remove user from group → their open group view closes.
- [ ] Force-disconnect socket for 20s, make 3 mutations, reconnect → UI reconciles to server state.
- [ ] With `POCKETPAW_REALTIME_BUS=redis` across two backend instances: mutation on A fans out to user connected on B.
- [ ] Kill Redis: client still sees REST working; on Redis recovery, realtime resumes.

## Rollout

1. Ship infra only: `ee/cloud/realtime/` package with `InProcessBus` + `emit()`. Behavior unchanged.
2. Refactor `agent_bridge.py` to use `emit()` — canary. All existing tests still pass.
3. Add group + workspace + session + file + notification emits one service at a time, each with emit-site tests.
4. Client: add handlers + reconcile behind `VITE_REALTIME_V2=true`.
5. Flip the flag, delete socket.io files and `socket.io-client` dep.
6. Follow-up PR: `RedisBus` + cross-instance tests, gated on `POCKETPAW_REALTIME_BUS=redis`.

## Out of scope

- Event log / resume cursors — reconcile-on-reconnect covers the UX.
- Message delivery receipts beyond `read.ack`.
- E2E encryption of event payloads.
- Cross-workspace notifications.
- Typing-storm rate limiting (add later if measured).
- OSS dashboard realtime — separate surface, untouched.
- paw-runtime DM routing — untouched; `agent_bridge` rides the same bus after refactor.

## Open items for implementation plan

- Concrete `notifications` Beanie collection shape (fields, indexes).
- Exact `room.join` protocol wire format (reuse `WsInbound` with new `type` values vs. separate envelope).
- Whether `agent.stream_chunk` should go on a separate high-volume channel in the future (not needed for (a) topology).
- Feature flag lifecycle — keep `VITE_REALTIME_V2` for one release or flip atomically.
