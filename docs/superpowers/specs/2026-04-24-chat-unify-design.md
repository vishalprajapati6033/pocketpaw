# Chat Unification — Unified `/cloud/chat/{scope}/{scope_id}/agent` for All Scopes

**Date:** 2026-04-24
**Branch:** `feat/chat-unify` (both `backend/` and `paw-enterprise/`)
**Status:** Design approved; plan next.
**Predecessors:** `2026-04-23-enterprise-agent-chat-endpoint-design.md` (backend SSE endpoint), `2026-04-23-pocket-agent-sse-frontend-design.md` (pocket-only frontend cutover).

## Problem

`POST /cloud/chat/{scope}/{scope_id}/agent` ships with `scope ∈ {dm, group, pocket}` but paw-enterprise only routes pocket sessions to it. Generic/runtime sessions still hit OSS `/api/v1/chat/stream` (via `streamChat()` in `core/chat/service.ts:74`); DMs and groups still send over WebSocket. This fragments transport, event schema, cancellation, and persistence. The unified endpoint already gives us per-agent soul routing, `client_message_id` reconciliation, pocket toolset assembly, and typed SSE events — extending it to all scopes collapses three flows into one.

OSS `/chat/stream` stays alive — it serves the OSS dashboard. Only paw-enterprise migrates off.

## Decision summary

- **Add a fourth `session` scope** (not: self-DMs, not: implicit inbox pocket). Keeps the generic "named thread with an agent" product model intact.
- **All three current frontend paths collapse** onto a single `agentChat(scope, scopeId, content, opts)` client in `core/chat/service.ts`. Existing thin wrappers (`streamChat`, `pocketChat`, new `dmChat` / `groupChat`) call through it.
- **WebSocket stays connected** but becomes receive-only for chat (presence, typing, `message.new` for non-caller participants, read receipts). All outbound sends go through SSE.
- **Single branch, no feature flag, no parallel run.** Sequenced commits inside the branch: backend → session cutover → dm/group cutover.
- **`/chat/stream` and `/sessions/runtime/*` are not retired.** paw-enterprise stops calling them; OSS backend keeps them for the OSS dashboard.

## Backend changes (`backend/ee/cloud/chat/`)

1. `agent_schemas.py` — extend `ScopeType` enum with `"session"`.
2. `agent_router.py` — `_resolve_scope_context` gains a session branch: load `Session` by `_id`, verify `workspace_id == current_workspace` and `owner_id == current_user`, return `ScopeContext(scope="session", scope_id=session._id, workspace_id, owner_id, pocket=None, room=None)`.
3. Toolset assembly (`agent_service.py`) — session scope uses default agent toolset. No `tool_specs` merge, no room-level overrides. Future work: per-session tool configs.
4. Persistence — cloud `Message` rows written with `context_type="session"`, `context_id=session._id`. Update Mongo validator to accept `"session"` in `context_type`.
5. Soul routing — same as pocket: `AgentPool.run(target_agent_id, ...)` with per-agent soul, `suppress_global_soul_observe=True`.
6. New endpoints:
   - `POST /cloud/chat/sessions` — body: `{title?, agent_id?}`; creates Session doc (`workspace=current_workspace`, `owner=current_user`, `pocket=None`). Returns Session.
   - `GET /cloud/chat/sessions` — list caller's sessions in workspace. Paginated.
   - `GET /cloud/chat/session/{session_id}/messages` — history by `(context_type="session", context_id)`. Paginated, newest-first.
   - `DELETE /cloud/chat/sessions/{session_id}` — soft delete.
   - `PATCH /cloud/chat/sessions/{session_id}` — rename.
7. Cancel — `POST /cloud/chat/session/{session_id}/agent/stop` reuses the existing `_active_runs` map keyed on `(scope, scope_id, user_id)`.

## Frontend changes (`paw-enterprise/src/lib/core/chat/`)

### Unified client

```ts
type Scope = 'dm' | 'group' | 'pocket' | 'session';

agentChat(scope: Scope, scopeId: string, content: string, opts: {
  clientMessageId: string;
  agentId?: string;
  media?: string[];
  fileContext?: FileContext;
  model?: string;
  abortController: AbortController;
}): Promise<void>
```

- Single `fetch` + SSE reader + `handleSSEEvent` dispatch. No new module — logic stays in `service.ts`.
- `agentStop(scope, scopeId)` — fire-and-forget cancel; `AbortController` handles local.
- `handleSSEEvent` already handles `chunk`, `thinking`, `tool_start`, `tool_result`, `ripple`, `pocket_created`, `pocket_mutation`, `ask_user_question`, `message.persisted`, `stream_start`, `stream_end`, `error`. Session scope adds nothing new.
- Every send generates a `client_message_id` (crypto.randomUUID) before dispatch; optimistic user message is tagged so `message.persisted` can reconcile.

### Wrappers (call through `agentChat`)

- `streamChat(content, media, abort)` → `agentChat('session', activeSession._id, ...)`.
- `pocketChat(content, pocket, media, abort)` → `agentChat('pocket', pocket._cloudId, ...)` (refactor — zero behavior change).
- `dmChat(content, roomId, ...)` — new helper.
- `groupChat(content, groupId, ...)` — new helper.

### Session id swap

Today `sessionStore.activeRuntimeSessionId = activeSession.sessionId` (`"websocket_<hex>"`, OSS runtime key). After: callers use `activeSession._id` (Mongo id) as the scope_id. Any code persisting `activeRuntimeSessionId` (localStorage, route params) is audited and updated.

### Session CRUD flip

`runtime/api.ts` `listNativeSessions` / `createNativeSession` / `getNativeSessionHistory` / `deleteNativeSession` / `renameNativeSession` — keep the functions but repoint to `/cloud/chat/sessions*`. OSS-only endpoints (`/sessions/runtime/*`) untouched; just not called from paw-enterprise.

### Direct callsites to refactor

- `components/os/ChatPanel.svelte:441` — direct `chatStream(sid, text, ...)` → `agentChat('session', ...)`.
- `core/files-chat/service.ts:264` — direct `chatStream(sessionId, text, ...)` → `agentChat('session', ...)`.

### DM / group cutover

`core/chat/store.svelte.ts:680` — `chatSocket.sendMessage(roomId, text, ...)` → `agentChat('dm' | 'group', roomId, ...)`. WS `sendMessage` method deleted once no callsites remain. WS stays connected for receive + presence + typing.

Known deferral stands: caller sees chunk-by-chunk SSE; other group members get `message.new` at `stream_end`. Not in scope.

## Rollout

Single branch `feat/chat-unify`. Three internal commit clusters (each independently reviewable; all ship together at merge):

1. **Backend + unified client.** Session scope shipped end-to-end in backend. Frontend lands `agentChat()` core and refactors `pocketChat()` to use it (no user-visible change; pocket smoke test proves the refactor).
2. **Session scope cutover.** Flip `streamChat()`, session CRUD, `components/os/ChatPanel.svelte`, `files-chat/service.ts`. Session id swap (`sessionId` → `_id`) lands here.
3. **DM + group cutover.** Swap WS sends. Delete WS `sendMessage`.

## Tests

**Backend (`backend/tests/cloud/chat/`):**
- `test_agent_router_session_scope.py` — resolver (ownership, cross-workspace 403, missing 404), happy-path SSE sequence, cancel, pre-stream 4xx, history pagination.
- Extend cancel tests with session-scope case.
- Mongo validator integration test writing `context_type="session"` against the real validator — closes the gap left by the pocket deferral.

**Frontend (vitest):**
- `core/chat/__tests__/agent-chat-unified.test.ts` — `agentChat()` dispatches to right URL per scope, right body shape.
- `agent-chat-session.test.ts`, `agent-chat-dm.test.ts`, `agent-chat-group.test.ts` — mirror existing `pocket-agent-sse.test.ts`: happy / cancel / in-stream error / pre-stream 4xx / `client_message_id` reconciliation.
- Refactor `pocket-agent-sse.test.ts` to import via the unified client.

**Smoke (manual, pre-merge):**
- Generic session chat (create, send, cancel mid-stream, reload history).
- DM chat (send, see participant reply via WS).
- Group chat (caller sees chunks; non-caller tab sees `message.new`).
- Pocket chat regression (Ripple inline rendering intact).
- Files tab chat.

## Out of scope

- Live chunk broadcast to non-caller scope members.
- Multi-agent turn-taking in groups.
- Per-session tool configs.
- Structured tool-trace sub-docs on `Message`.
- Rate limiting per `(workspace, user)`.
- Migration tooling for existing OSS runtime sessions in Mongo (there is no cross-user data to migrate; each user's sidebar just starts empty against the new endpoint).

## Gotchas carried forward

From the pocket branch:
- Always verify `git branch --show-current` at the start and end of every subagent task.
- `Beanie Document.model_construct(...)` in unit tests — not `Document(...)`.
- `CloudError(status_code, code, message)` positional.
- Ruff UP042: `StrEnum`, never `class X(str, Enum)`.
- Subagent reports can lie; re-verify via `git show` / re-run tests.
