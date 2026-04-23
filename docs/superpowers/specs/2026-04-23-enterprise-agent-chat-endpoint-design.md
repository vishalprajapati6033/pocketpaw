# Enterprise Agent Chat Endpoint — Design

**Status:** Draft
**Date:** 2026-04-23
**Owner:** prakash@snctm.com
**Module:** `backend/ee/cloud/chat/`

## Context

The OSS chat endpoint at `backend/src/pocketpaw/api/v1/chat.py` (`POST /chat`, `POST /chat/stream`, `POST /chat/stop`) is a stateless bridge from a user message to the AgentLoop. It has no notion of workspace, group, DM, pocket, presence, or ripple (dynamic UI) output, and it intentionally must stay that way for the single-user local product.

The enterprise cloud surface (`backend/ee/cloud/chat/router.py`) already provides workspace-scoped REST + WebSocket for groups, DMs, messages, reactions, threads, and pins, but it does **not** yet have a dedicated agent-generation endpoint. Agent replies inside the cloud flow today go through `ee/cloud/shared/agent_bridge.py` without scope-aware context, pocket-scoped tools, or structured ripple output.

## Goal

Add a fully separate enterprise agent chat endpoint that:

1. Lives entirely under the enterprise auth + license stack (`require_license`, `current_user_id`, `current_workspace_id`, scope-specific membership guards).
2. Is scope-aware for DM, group, and pocket contexts.
3. Streams rich output (chunks, tool events, thinking, ripple UI blocks) over SSE to the caller.
4. Broadcasts the finished assistant message (and agent typing state) to other scope members over the existing `/ws/cloud` WebSocket.
5. Mounts pocket-scoped tools for pocket runs, without leaking them to other scopes.
6. Shares the underlying AgentLoop engine with OSS — we wrap, we do not fork.

Non-goals:

- Changing `api/v1/chat.py` in any way.
- Live chunk-by-chunk broadcast to non-caller members (deferred; finished-message broadcast only for now).
- New WebSocket handler flows from the client (existing `/ws/cloud` is purely additive).

## Architecture

```
paw-enterprise (desktop)
      │
      │  POST /cloud/chat/{scope}/{scope_id}/agent   (SSE, Bearer JWT)
      ▼
┌──────────────────────────────────────────────────────────┐
│ ee/cloud/chat/agent_router.py   (new)                     │
│   • auth: current_user_id, current_workspace_id,          │
│           require_license, scope-specific guard           │
│   • resolves ScopeContext (dm | group | pocket)           │
│   • persists user message via MessageService              │
│   • broadcasts "message.new" on /ws/cloud                 │
│   • spawns agent run via CloudAgentBridge                 │
│   • streams SSE back to caller (chunks, tool_*, ripple,   │
│     stream_end)                                           │
│   • on stream_end: persists assistant message, broadcasts │
│     "message.new" and "agent.typing" events               │
└──────────┬───────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│ ee/cloud/chat/agent_service.py   (new)                    │
│   • ScopeContext builder (DM / group / pocket)            │
│   • Toolset assembler (base + pocket-scoped)              │
│   • Participant / presence context block for system prompt│
│   • Ripple-block pass-through (no stripping)              │
└──────────┬───────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│ ee/cloud/shared/agent_bridge.py   (existing, extended)    │
│   • wraps AgentLoop for a single cloud-scoped run         │
│   • accepts ScopeContext + runtime toolset                │
│   • emits AgentEvents; router adapts to SSE + WS          │
└──────────────────────────────────────────────────────────┘
```

Key decisions:

- **One parametric route, three guards.** `POST /cloud/chat/{scope}/{scope_id}/agent` with `scope ∈ {dm, group, pocket}` dispatches to a scope-specific resolver. Less duplication than three separate routes; guards are chosen by scope in the resolver.
- **Separate endpoint, shared engine.** Agent backends, memory, tracing, and the message bus are all reused through `CloudAgentBridge`; only the cloud-specific context and toolset assembly is new.
- **Scope is explicit in the URL**, not inferred from a group type, so pocket-specific tool loading and presence semantics are unambiguous.
- **`/ws/cloud` stays the broadcast channel.** New outbound event types are added; no new inbound WS handler flows.

## Endpoint surface

### `POST /cloud/chat/{scope}/{scope_id}/agent`

SSE response. Auth: Bearer JWT. License: required. Membership: required for the resolved scope.

Request body (`CloudAgentChatRequest`):

```python
class CloudAgentChatRequest(BaseModel):
    content: str
    attachments: list[Attachment] = []
    reply_to: str | None = None
    mentions: list[str] = []            # user/agent ids explicitly addressed
    agent_id: str | None = None         # required for group scope when >1 agent member
    client_message_id: str | None = None  # idempotency key for the user message
```

SSE event sequence (in order, with optional events interleaved):

| Event | Data | When |
|-------|------|------|
| `message.persisted` | `{user_message_id, client_message_id}` | Immediately after the user message is written. |
| `stream_start` | `{run_id, agent_id, scope, scope_id}` | Agent run begins. `run_id` is a server-generated UUID used as the cancellation and trace key. |
| `thinking` | `{content}` | Backend emits thinking events. |
| `tool_start` | `{tool, input}` | Tool invocation starts. |
| `tool_result` | `{tool, output}` | Tool invocation completes. |
| `chunk` | `{content, type: "text"}` | Streamed text chunk. |
| `ripple` | `{spec}` | A complete ripple UI JSON block, emitted as a single event. Never split across chunks. |
| `pocket_created` | `{spec, session_id, pocket_cloud_id}` | Pocket scope only. |
| `pocket_mutation` | `{mutation}` | Pocket scope only. |
| `ask_user_question` | `{question, options}` | Agent requests clarification. |
| `stream_end` | `{assistant_message_id, usage, cancelled: bool}` | Run complete. |
| `error` | `{code, message}` | Run failed; stream closes after this event. |

### `POST /cloud/chat/{scope}/{scope_id}/agent/stop`

Cancels the in-flight run for the caller in the given scope. Mirrors OSS `/chat/stop`. Returns `{status: "ok"}` or 404 if no run is active.

### Existing `/ws/cloud` — additive events

Broadcast to all scope members **except the caller**:

| Event | Data | When |
|-------|------|------|
| `agent.typing` | `{scope, scope_id, agent_id, active: bool}` | Active on `stream_start`; inactive on `stream_end`/`error`. |
| `message.new` | `Message` document (existing shape) | Emitted once at `stream_end` with the fully-assembled assistant message, including any ripple blocks as structured content. |
| `message.failed` | `{scope, scope_id, agent_id, client_message_id, code}` | Emitted if the agent run errors before producing a persistable assistant message. |

Rationale for "caller gets chunks, others get finished message": avoids N clients rendering half-streamed ripple JSON, keeps broadcast volume sane, and matches Slack/Discord UX where remote viewers see finished bot messages. Live chunk broadcasting can be added later as opt-in.

## ScopeContext, presence & tools

`ee/cloud/chat/agent_service.py` resolves a `ScopeContext` per request:

| Scope | Resolution | Participants loaded | Presence | Tools mounted |
|-------|-----------|---------------------|----------|---------------|
| `dm` | `Group` where `type=dm` and caller is a member | The two users (or user+agent). | Online/offline of peer from WS manager. | Base toolset. |
| `group` | `Group` where caller is a member + license check | All group members + group agents. | Roster + typing state. | Base toolset. Group-level integrations reserved for later. |
| `pocket` | `Pocket` where caller has access | Pocket collaborators. | Pocket-scoped presence. | Base + pocket tools from `Pocket.tool_specs`. |

"Base toolset" means whatever `AgentLoop` currently exposes for its configured backend — cloud scopes do not subtract from it.

### Pocket tools

Each `Pocket` document declares a `tool_specs: list[dict]` field. Each entry identifies either:

- A built-in cloud tool by id.
- A workspace-registered MCP tool by id.
- An inline declarative tool.

`agent_service.assemble_toolset(scope_ctx)` merges the base toolset with pocket tools into the `AgentLoop` invocation for that single run. No global registry mutation — tools are scoped to the run.

### Presence and participant context

A new `CloudContextProvider` assembles a compact block for the system prompt via `AgentContextBuilder`:

```
<scope>{dm|group|pocket} {scope_id}</scope>
<participants>{compact roster}</participants>
<recent_activity>{typing state, recent joiners}</recent_activity>
```

This gives the agent the minimum situational awareness to address participants by name and tailor tone (DM vs group) without bloating the prompt.

## Error handling

- **Auth / license / membership:** rejected with 401/403/402 *before* opening the SSE stream. An auth error must never be streamed.
- **In-stream failures:** any exception inside the bridge emits an `error` SSE event with a `CloudError` code (see `ee/cloud/shared/errors.py`), then closes the stream. The user message is already persisted; the assistant message is **not** persisted on failure — avoids half-baked replies in history. A `message.failed` WS event is broadcast so other members see the attempt didn't land.
- **Cancellation:** `/stop` sets the run's cancel event, the bridge unsubscribes from the bus, and a `stream_end` with `{cancelled: true}` is emitted. No assistant message persisted. A final `agent.typing` inactive event is broadcast.
- **Concurrent runs per scope:** a new request for the same `(scope, scope_id, user_id)` cancels the prior in-flight run, mirroring OSS behavior. Tracked in an in-process `dict[(scope, scope_id, user_id), CancelEvent]`.

## Testing

- **Unit:**
  - `ScopeContext` resolution for dm/group/pocket (including non-member, archived group, inaccessible pocket).
  - Toolset assembly for pocket scope (base + pocket tools merged, duplicates deduped).
  - `CloudError` → SSE `error` event mapping.
  - Concurrent-run cancellation for the same `(scope, scope_id, user_id)`.
- **Integration** (FastAPI TestClient + beanie test DB):
  - Full SSE round-trip per scope using a fake `AgentBackend` that yields a scripted event sequence including a ripple block. Assert order: `message.persisted` → `stream_start` → chunks → `ripple` → `stream_end`.
  - Verify `message.new` broadcast on a second connected WS member at `stream_end`.
  - Verify `agent.typing` active/inactive bracket the run.
- **Negative tests:** license disabled → 402 before stream; non-member → 403 before stream; invalid JWT → 401 before stream.
- **No change** to `backend/tests/test_api_chat.py`.

## File plan

New:

- `backend/ee/cloud/chat/agent_router.py` — SSE endpoint + `/stop`.
- `backend/ee/cloud/chat/agent_service.py` — `ScopeContext`, toolset assembly, `CloudContextProvider`.
- `backend/ee/cloud/chat/agent_schemas.py` — `CloudAgentChatRequest`, SSE event payload models.
- `backend/tests/ee/cloud/chat/test_agent_router.py`
- `backend/tests/ee/cloud/chat/test_agent_service.py`

Modified:

- `backend/ee/cloud/shared/agent_bridge.py` — accept `ScopeContext` + runtime toolset; emit ripple events untouched.
- `backend/ee/cloud/chat/router.py` — include the new `agent_router`.
- `backend/ee/cloud/models/pocket.py` — add `tool_specs: list[dict]` field if not already present.

Unchanged:

- `backend/src/pocketpaw/api/v1/chat.py` — OSS path remains pristine.
- `backend/ee/cloud/chat/ws.py` — only additive new event types.

## Open items deferred

These are explicitly out of scope for this iteration and will be revisited:

- Live chunk broadcast to non-caller members.
- Multi-agent turn-taking inside a group (which agent replies when).
- Persisting tool traces as structured sub-documents on the assistant `Message`.
- Rate limiting per `(workspace, user)`.
