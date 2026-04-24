# Chat Unification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `session` scope to `POST /cloud/chat/{scope}/{scope_id}/agent` and migrate every paw-enterprise chat surface (generic sessions, DMs, groups, pockets) onto the unified endpoint. OSS `/chat/stream` stays alive for the OSS dashboard.

**Architecture:** Backend extends `ScopeKind` with `SESSION` and adds a session resolver, CRUD/history endpoints, and Mongo-validator coverage. Frontend collapses three transports (`streamChat` → OSS SSE, `pocketChat` → cloud SSE, `chatSocket.sendMessage` → WS) onto one `agentChat(scope, scopeId, ...)` client. WS stays connected but receive-only for chat.

**Tech Stack:** Python 3.11, FastAPI, Beanie (Mongo ODM), pytest, asyncio; SvelteKit 2, Svelte 5 runes, Bun, vitest.

**Design doc:** `backend/docs/superpowers/specs/2026-04-24-chat-unify-design.md`

**Working branch:** `feat/chat-unify` (both `backend/` and `paw-enterprise/`).

---

## Task 0: Pre-flight

**Goal:** verify branch state; capture baseline test counts so regressions are visible.

**Step 1:** Verify branch state in both repos.

```bash
cd D:/paw/backend && git branch --show-current && git status --short
cd D:/paw/paw-enterprise && git branch --show-current && git status --short
```

If either branch is not `feat/chat-unify`, stop and ask the user before proceeding. If there are uncommitted changes unrelated to this plan, stop and ask.

**Step 2:** Baseline backend tests.

```bash
cd D:/paw/backend && uv run pytest tests/cloud/chat/ --ignore=tests/e2e -q 2>&1 | tail -20
```

Record pass/fail counts in `docs/superpowers/plans/2026-04-24-chat-unify.md` under "Baseline" below.

**Step 3:** Baseline frontend tests.

```bash
cd D:/paw/paw-enterprise && bun run test -- --run src/lib/core/chat/ 2>&1 | tail -20
```

**Step 4:** Commit baseline note (if design doc isn't already committed, include it).

```bash
cd D:/paw/backend
git add docs/superpowers/specs/2026-04-24-chat-unify-design.md docs/superpowers/plans/2026-04-24-chat-unify.md
git commit -m "docs: chat-unify design + plan"
```

---

# Cluster 1 — Backend session scope + unified frontend client

No user-visible change. Session scope fully shipped on backend; frontend lands `agentChat()` and refactors `pocketChat()` to use it.

---

## Task 1: Add `SESSION` to `ScopeKind`

**Files:**
- Modify: `backend/ee/cloud/chat/agent_service.py:20-23`
- Test: `backend/tests/cloud/chat/test_agent_service_scope.py` (may exist — check first)

**Step 1: Write the failing test**

Append to `tests/cloud/chat/test_agent_service_scope.py` (create if absent):

```python
from ee.cloud.chat.agent_service import ScopeKind

def test_session_kind_value():
    assert ScopeKind.SESSION.value == "session"

def test_scopekind_accepts_session_string():
    assert ScopeKind("session") is ScopeKind.SESSION
```

**Step 2: Run tests — verify fail.**

```bash
uv run pytest tests/cloud/chat/test_agent_service_scope.py -v
```

Expected: `AttributeError: SESSION` or `ValueError`.

**Step 3: Extend the enum.**

```python
class ScopeKind(StrEnum):
    DM = "dm"
    GROUP = "group"
    POCKET = "pocket"
    SESSION = "session"
```

**Step 4: Verify pass.**

```bash
uv run pytest tests/cloud/chat/test_agent_service_scope.py -v
```

**Step 5: Commit.**

```bash
git add ee/cloud/chat/agent_service.py tests/cloud/chat/test_agent_service_scope.py
git commit -m "feat(cloud-chat): add SESSION variant to ScopeKind"
```

---

## Task 2: `_resolve_session` resolver (ownership + workspace check)

**Files:**
- Modify: `backend/ee/cloud/chat/agent_service.py`
- Test: `backend/tests/cloud/chat/test_agent_service_scope.py`

**Step 1: Write failing tests.**

```python
import pytest
from unittest.mock import AsyncMock, patch
from ee.cloud.chat.agent_service import resolve_scope_context, ScopeKind
from ee.cloud.shared.errors import CloudError, NotFound

@pytest.mark.asyncio
async def test_session_scope_happy_path():
    from ee.cloud.models.session import Session
    fake = Session.model_construct(
        id="s1", sessionId="websocket_abc", workspace="w1", owner="u1",
        agent="a1", pocket=None, deleted_at=None,
    )
    with patch("ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        ctx = await resolve_scope_context(
            scope="session", scope_id="s1", user_id="u1", agent_id_hint=None,
        )
    assert ctx.kind is ScopeKind.SESSION
    assert ctx.scope_id == "s1"
    assert ctx.workspace_id == "w1"
    assert ctx.target_agent_id == "a1"
    assert ctx.members == ["u1"]

@pytest.mark.asyncio
async def test_session_scope_not_found():
    with patch("ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=None)):
        with pytest.raises(NotFound):
            await resolve_scope_context(
                scope="session", scope_id="missing", user_id="u1", agent_id_hint=None,
            )

@pytest.mark.asyncio
async def test_session_scope_wrong_owner_forbidden():
    from ee.cloud.models.session import Session
    fake = Session.model_construct(
        id="s1", sessionId="ws", workspace="w1", owner="other", agent="a1",
        pocket=None, deleted_at=None,
    )
    with patch("ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        with pytest.raises(CloudError) as exc:
            await resolve_scope_context(
                scope="session", scope_id="s1", user_id="u1", agent_id_hint=None,
            )
    assert exc.value.code == "session.forbidden"

@pytest.mark.asyncio
async def test_session_scope_agent_id_hint_overrides():
    from ee.cloud.models.session import Session
    fake = Session.model_construct(
        id="s1", sessionId="ws", workspace="w1", owner="u1", agent="a1",
        pocket=None, deleted_at=None,
    )
    with patch("ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        ctx = await resolve_scope_context(
            scope="session", scope_id="s1", user_id="u1", agent_id_hint="a2",
        )
    assert ctx.target_agent_id == "a2"
```

**Step 2: Verify failures.**

```bash
uv run pytest tests/cloud/chat/test_agent_service_scope.py -v
```

**Step 3: Implement.** Add to `agent_service.py`:

```python
async def _get_session(session_id: str) -> Any:
    from beanie import PydanticObjectId
    from ee.cloud.models.session import Session
    try:
        return await Session.get(PydanticObjectId(session_id))
    except Exception:
        return None


async def _resolve_session(
    scope_id: str, user_id: str, agent_id_hint: str | None
) -> ScopeContext:
    session = await _get_session(scope_id)
    if session is None:
        raise NotFound("session", scope_id)
    if getattr(session, "deleted_at", None) is not None:
        raise NotFound("session", scope_id)
    if getattr(session, "owner", None) != user_id:
        raise CloudError(403, "session.forbidden", "Caller does not own this session")
    target = agent_id_hint or getattr(session, "agent", None)
    if not target:
        raise CloudError(400, "session.no_agent", "Session has no agent")
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id=scope_id,
        workspace_id=str(getattr(session, "workspace", "")),
        user_id=user_id,
        members=[user_id],
        target_agent_id=target,
        agent_ids_in_scope=[target],
    )
```

Then wire it into `resolve_scope_context`:

```python
if kind is ScopeKind.POCKET:
    return await _resolve_pocket(scope_id, user_id, agent_id_hint)
if kind is ScopeKind.SESSION:
    return await _resolve_session(scope_id, user_id, agent_id_hint)
return await _resolve_group_like(kind, scope_id, user_id, agent_id_hint)
```

**Step 4: Verify pass.** Run the same pytest command.

**Step 5: Commit.**

```bash
git add ee/cloud/chat/agent_service.py tests/cloud/chat/test_agent_service_scope.py
git commit -m "feat(cloud-chat): resolve_scope_context supports session scope"
```

---

## Task 3: Persist session-scope messages with `context_type="session"`

**Files:**
- Modify: `backend/ee/cloud/chat/agent_router.py` (`_persist_user_message`, `_persist_assistant_message`)
- Test: `backend/tests/cloud/chat/test_agent_router_persist.py` (create)

**Step 1: Write failing tests** that insert a session-scope `ScopeContext`, call `_persist_user_message`, and assert the Mongo doc has `context_type="session"`, `context_id=<session._id>`. Use `Message.model_construct` if direct write is fiddly in unit tests; otherwise run against the real integration Mongo if available.

```python
import pytest
from unittest.mock import AsyncMock, patch
from ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from ee.cloud.chat.agent_router import _persist_user_message
from ee.cloud.chat.agent_schemas import CloudAgentChatRequest

@pytest.mark.asyncio
async def test_persist_user_message_session_scope(monkeypatch):
    from ee.cloud.models import message as message_mod
    captured = {}
    class _Stub:
        def __init__(self, **kw):
            captured.update(kw)
            self.id = "mid"
        async def insert(self):
            return None
    monkeypatch.setattr(message_mod, "Message", _Stub)
    ctx = ScopeContext(
        kind=ScopeKind.SESSION, scope_id="s1", workspace_id="w1", user_id="u1",
        members=["u1"], target_agent_id="a1",
    )
    body = CloudAgentChatRequest(content="hello")
    mid = await _persist_user_message(ctx, body)
    assert mid == "mid"
    assert captured["context_type"] == "session"
    # Depending on Message schema: `session` field or `context_id`. Verify
    # whichever is in the schema — the test must match reality.
```

**Step 2: Verify failure.**

```bash
uv run pytest tests/cloud/chat/test_agent_router_persist.py -v
```

**Step 3: Implement.** In `_persist_user_message` and `_persist_assistant_message`, add a session branch **before** the group fallback:

```python
elif ctx.kind is ScopeKind.SESSION:
    msg = Message(
        context_type="session",
        session=ctx.scope_id,  # or context_id=ctx.scope_id — match the model
        role="user",  # assistant branch uses "assistant"
        sender=ctx.user_id,
        sender_type="user",
        content=body.content,
        attachments=body.attachments,
        workspace_id=ctx.workspace_id,
    )
```

Check `ee/cloud/models/message.py` first for the right field name (`session` vs `context_id`). If the Message model needs a new field, add it in this task.

**Step 4: Run tests.** Verify pass.

**Step 5: Commit.**

```bash
git add ee/cloud/chat/agent_router.py ee/cloud/models/message.py tests/cloud/chat/test_agent_router_persist.py
git commit -m "feat(cloud-chat): persist session-scope messages"
```

---

## Task 4: Mongo validator accepts `context_type="session"`

**Files:**
- Modify: wherever the Mongo validator is declared (check `ee/cloud/models/__init__.py` or `ee/cloud/db.py` — grep `context_type`).
- Test: integration test in `backend/tests/cloud/chat/test_message_validator.py` (create).

**Step 1: Locate validator.**

```bash
grep -rn '"context_type"' ee/cloud/models/ ee/cloud/db*.py
```

**Step 2: Write integration test** against a real Mongo (use the existing test fixture if one exists; look in `tests/conftest.py` for `mongo_client`/`mongo_db` fixtures).

```python
@pytest.mark.asyncio
async def test_insert_session_context_type_accepted(mongo_db):
    from ee.cloud.models.message import Message
    msg = Message(
        context_type="session", session="sid1", role="user",
        sender="u1", sender_type="user", content="hi", workspace_id="w1",
    )
    await msg.insert()
    assert msg.id is not None
```

**Step 3: Verify failure** — the validator rejects `"session"`.

**Step 4: Update validator enum to include `"session"`.**

**Step 5: Verify pass.**

**Step 6: Commit.**

```bash
git add <validator-file> tests/cloud/chat/test_message_validator.py
git commit -m "feat(cloud-chat): Mongo validator accepts context_type=session"
```

---

## Task 5: Session-scope smoke test through the SSE endpoint

**Files:**
- Test: `backend/tests/cloud/chat/test_agent_router_session_scope.py` (create)

Mirror `test_agent_router_smoke.py` / the pocket smoke test from Task 10 of the prior plan. Test covers the full SSE sequence for a session-scope run: `stream_start`, `message.persisted`, `chunk*`, `stream_end`.

**Step 1: Write the test** with FastAPI TestClient (async), mock `AgentPool.run` to yield fixed events, assert the frame sequence.

**Step 2: Verify pass** (should work once Tasks 2-4 landed — the router is scope-agnostic after resolver/persistence branches).

```bash
uv run pytest tests/cloud/chat/test_agent_router_session_scope.py -v
```

**Step 3: Commit.**

```bash
git add tests/cloud/chat/test_agent_router_session_scope.py
git commit -m "test(cloud-chat): session-scope SSE smoke"
```

---

## Task 6: `_ensure_scope_session` for SESSION kind (stability)

**Files:**
- Modify: `backend/ee/cloud/chat/agent_router.py:209-276` (`_ensure_scope_session`)

`_ensure_scope_session` currently handles POCKET and DM. For SESSION the Session doc already *is* the scope — the function should return `ctx.scope_id`'s session's `sessionId` without creating anything. Keeps `stream_start.session_id` consistent with other scopes.

**Step 1: Add a unit test** patching `Session.get` and asserting the function returns the doc's `sessionId`.

**Step 2: Implement** a short branch:

```python
if ctx.kind is ScopeKind.SESSION:
    s = await Session.get(PydanticObjectId(ctx.scope_id))
    return s.sessionId if s else None
```

**Step 3: Verify pass. Commit.**

```bash
git add ee/cloud/chat/agent_router.py tests/cloud/chat/test_agent_router_session_scope.py
git commit -m "feat(cloud-chat): _ensure_scope_session handles session scope"
```

---

## Task 7: Cancel tuple for session scope

**Files:**
- Test: `backend/tests/cloud/test_agent_router_cancel.py` (extend)

The cancel endpoint keys on `(scope, scope_id, user_id)` — already scope-agnostic (see `agent_router.py:150-159`). Just add a test asserting session-scope cancel works.

**Step 1: Write test.** Start a streaming session run, `POST /cloud/chat/session/{id}/agent/stop`, assert the stream closes with `cancelled=true`.

**Step 2: Run.** Expected: PASS (no implementation change needed).

**Step 3: Commit.**

```bash
git add tests/cloud/test_agent_router_cancel.py
git commit -m "test(cloud-chat): cancel covers session scope"
```

---

## Task 8: `POST /cloud/chat/sessions` — create session

**Files:**
- Create/modify: `backend/ee/cloud/chat/session_router.py` (new) and register in app factory.
- Schemas: `backend/ee/cloud/chat/session_schemas.py` (new).
- Test: `backend/tests/cloud/chat/test_session_router.py` (create).

**Step 1: Write failing test** — POST with body `{title: "My chat", agent_id: "a1"}` returns 200 with `{_id, sessionId, title, agent}`; Session row exists with `workspace=current_workspace`, `owner=current_user`, `pocket=None`.

**Step 2: Implement schemas.**

```python
class CreateSessionRequest(BaseModel):
    title: str | None = None
    agent_id: str | None = None

class SessionResponse(BaseModel):
    id: str = Field(alias="_id")
    sessionId: str
    title: str
    agent: str | None
    workspace: str
    createdAt: datetime
```

**Step 3: Implement router** — mirror the shape of `pockets/router.py` create. Use `current_user`, `current_workspace_id` deps; require license. Prefix `/chat/sessions`.

**Step 4: Verify pass. Commit.**

```bash
git add ee/cloud/chat/session_router.py ee/cloud/chat/session_schemas.py tests/cloud/chat/test_session_router.py <app-factory>
git commit -m "feat(cloud-chat): POST /chat/sessions"
```

---

## Task 9: `GET /cloud/chat/sessions` — list sessions

**Files:** same as Task 8 plus a `list` handler.

**Step 1: Test** — two sessions owned by caller, one by another user: list returns two, newest-first, excludes deleted.

**Step 2: Implement** — filter by `owner=current_user`, `workspace=current_workspace`, `deleted_at=None`; sort `-lastActivity`; paginate `limit`/`before`.

**Step 3: Verify. Commit.**

```bash
git commit -m "feat(cloud-chat): GET /chat/sessions"
```

---

## Task 10: `GET /cloud/chat/session/{id}/messages` — history

**Files:** same router.

**Step 1: Test** — seed three messages with `context_type="session"`, assert they're returned newest-first, verify cross-owner access is 403.

**Step 2: Implement** — query `Message.find(context_type="session", session=id)`, sort `-createdAt`, paginate.

**Step 3: Verify. Commit.**

```bash
git commit -m "feat(cloud-chat): GET /chat/session/{id}/messages"
```

---

## Task 11: `DELETE /cloud/chat/sessions/{id}` — soft delete

**Step 1: Test** — delete sets `deleted_at`, subsequent list excludes it.

**Step 2: Implement** — set `deleted_at=utcnow()`; 204.

**Step 3: Verify. Commit.**

```bash
git commit -m "feat(cloud-chat): DELETE /chat/sessions/{id}"
```

---

## Task 12: `PATCH /cloud/chat/sessions/{id}` — rename

**Step 1: Test** — patch with `{title: "new"}` updates the doc.

**Step 2: Implement.**

**Step 3: Verify. Commit.**

```bash
git commit -m "feat(cloud-chat): PATCH /chat/sessions/{id}"
```

---

## Task 13: Backend lint + format sweep

```bash
uv run ruff check ee/cloud/chat/ tests/cloud/chat/ --fix
uv run ruff format ee/cloud/chat/ tests/cloud/chat/
uv run mypy ee/cloud/chat/
git diff --stat
```

Commit any formatting fixes with `style: ruff sweep for chat-unify backend`.

---

## Task 14: Unified `agentChat` frontend client

**Files:**
- Modify: `paw-enterprise/src/lib/core/chat/service.ts`
- Test: `paw-enterprise/src/lib/core/chat/__tests__/agent-chat-unified.test.ts` (create)

**Step 1: Write failing tests** — `agentChat('session', 's1', 'hi', opts)` fetches `BASE_URL/cloud/chat/session/s1/agent` with `POST`, `credentials: "include"`, right headers, right body. Same for `dm`, `group`, `pocket`.

**Step 2: Extract** the existing pocket-chat streaming logic (`service.ts:509-...` `pocketChat`) into a private `agentChat(scope, scopeId, content, opts)` that takes a scope in its URL and a `client_message_id` in its body. Move the SSE reader loop and `handleSSEEvent` dispatch into it.

**Step 3: Verify unit tests pass.**

```bash
bun run test -- --run src/lib/core/chat/__tests__/agent-chat-unified.test.ts
```

**Step 4: Commit.**

```bash
cd D:/paw/paw-enterprise
git add src/lib/core/chat/service.ts src/lib/core/chat/__tests__/agent-chat-unified.test.ts
git commit -m "feat(chat): unified agentChat client for all scopes"
```

---

## Task 15: Refactor `pocketChat` to call `agentChat`

**Files:** `paw-enterprise/src/lib/core/chat/service.ts`

`pocketChat(content, pocket, media, abort)` becomes:

```ts
export async function pocketChat(content, pocket, media, abort) {
  // Any pocket-specific state setup (activeCloudRun, etc.) stays here.
  const clientMessageId = ...;
  return agentChat('pocket', pocket._cloudId, content, {
    clientMessageId, media: ..., abortController: abort,
  });
}
```

**Step 1: Run existing pocket vitest suite** — should pass after the refactor.

```bash
bun run test -- --run src/lib/core/chat/__tests__/pocket-agent-sse.test.ts
```

**Step 2: Commit.**

```bash
git commit -am "refactor(chat): pocketChat uses agentChat under the hood"
```

---

# Cluster 2 — Session cutover

First user-visible change: the default chat hits the new endpoint.

---

## Task 16: Session CRUD frontend — repoint to cloud endpoints

**Files:** `paw-enterprise/src/lib/core/runtime/api.ts:42-86`

Replace bodies of `listNativeSessions`, `createNativeSession`, `deleteNativeSession`, `renameNativeSession`, `getNativeSessionHistory` to hit `/cloud/chat/sessions*` instead of `/sessions/*`. Keep the function names and signatures for the callers.

**Step 1:** Update each function. Verify types still match Session schema from Task 8.

**Step 2:** Run session-related components by hand (sidebar list, create button) — or update the vitest suites that mock these functions.

**Step 3: Commit.**

```bash
git commit -am "refactor(chat): session CRUD targets /cloud/chat/sessions"
```

---

## Task 17: Audit `activeRuntimeSessionId` consumers

**Step 1: Grep for every consumer.**

```bash
grep -rn "activeRuntimeSessionId\|\.sessionId" src/lib/ --include='*.ts' --include='*.svelte'
```

**Step 2:** Each consumer either:
- Uses the id as the scope_id for `/cloud/chat/session/{id}/agent` — switch to `activeSession._id` (Mongo id).
- Uses the id as a local key (no server call) — stays as-is.
- Uses the id in localStorage / URL params — migrate key or accept a one-time reset.

Make a list of touched files in a short note inside the plan doc before changing code.

**Step 3: Commit the audit note.**

```bash
git commit -am "docs(chat): active-session-id audit"
```

---

## Task 18: `streamChat` flips to `agentChat('session', ...)`

**Files:** `paw-enterprise/src/lib/core/chat/service.ts:74-158`

**Step 1: Update existing vitest suite (or add one).** Mock fetch; assert that `streamChat('hi', undefined, abort)` when `sessionStore.activeSession = {_id: "s1", ...}` fires `/cloud/chat/session/s1/agent`.

**Step 2: Implement.** Replace the `runtimeApi.chatStream(sessionId, content, ...)` call with:

```ts
const sessionCloudId = sessionStore.activeSession?._id;
if (!sessionCloudId) throw new Error('No active session');
const clientMessageId = crypto.randomUUID();
// Tag optimistic message (same pattern as pocketChat)
await agentChat('session', sessionCloudId, content, {
  clientMessageId,
  media: media?.map(...) ?? undefined,
  fileContext: (await getFileContext()) ?? undefined,
  abortController,
});
```

Drop the old `_APISessionBridge`-flavored error handling that was OSS-specific.

**Step 3: Verify.**

```bash
bun run test -- --run src/lib/core/chat/
```

**Step 4: Commit.**

```bash
git commit -am "feat(chat): streamChat routes to /cloud/chat/session/{id}/agent"
```

---

## Task 19: `stopGeneration` — session branch

**Files:** `paw-enterprise/src/lib/core/chat/service.ts:302-320`

Extend `stopGeneration(pocketCloudId?)` to also accept a session id:

```ts
export async function stopGeneration(opts?: { pocketCloudId?: string; sessionId?: string }) {
  const abortLocal = () => { activeCloudRun?.abortController.abort(); };
  if (opts?.pocketCloudId) { fetch(`.../pocket/${opts.pocketCloudId}/agent/stop`, ...); abortLocal(); return; }
  if (opts?.sessionId) { fetch(`.../session/${opts.sessionId}/agent/stop`, ...); abortLocal(); return; }
  // ...existing fallback
}
```

Callers in `chatStore`, `ChatPanel`, `ChatInput` updated to pass `sessionId` when relevant.

**Step 1: Unit test** each branch.

**Step 2: Commit.**

```bash
git commit -am "feat(chat): stopGeneration handles session scope"
```

---

## Task 20: Refactor `components/os/ChatPanel.svelte:441`

**Files:** `paw-enterprise/src/lib/components/os/ChatPanel.svelte`

Replace the direct `chatStream(sid, text, ...)` call with `chatStore.sendMessage(text, media)` (or `agentChat('session', sid, text, ...)` if the component truly needs a bespoke send — unlikely).

**Step 1: Read the full `sendMessage` function** in that file (lines 361-460 or so) to understand why it reaches past the store.

**Step 2: If there's no good reason,** replace with `chatStore.sendMessage(text, media)`.

**Step 3: If there is a reason,** call `agentChat` directly.

**Step 4: Manual smoke** — open the OS chat panel, send a message, confirm it streams.

**Step 5: Commit.**

```bash
git commit -am "refactor(chat): os/ChatPanel uses unified send path"
```

---

## Task 21: Refactor `core/files-chat/service.ts:264`

**Files:** `paw-enterprise/src/lib/core/files-chat/service.ts`

Replace the direct `chatStream(sessionId, text, ...)` with `agentChat('session', sessionId, text, { ... })`.

**Step 1: Check** whether files-chat has its own session id or shares with the main sidebar. If shared: same `activeSession._id`. If separate: needs its own session creation call.

**Step 2: Implement** — keep file-attachment payload shape; pass via `opts.fileContext` or `opts.media` as appropriate.

**Step 3: Manual smoke** — Files tab send.

**Step 4: Commit.**

```bash
git commit -am "refactor(chat): files-chat uses unified send path"
```

---

## Task 22: Session-scope vitest suite

**Files:** `paw-enterprise/src/lib/core/chat/__tests__/agent-chat-session.test.ts` (create)

Mirror `pocket-agent-sse.test.ts`. Cases: happy path, cancel mid-stream, in-stream `error` event, pre-stream 4xx, `client_message_id` reconciliation.

```bash
bun run test -- --run src/lib/core/chat/__tests__/agent-chat-session.test.ts
```

**Commit.**

```bash
git commit -am "test(chat): agent-chat-session coverage"
```

---

## Task 23: Frontend manual smoke pass — session scope

**Check each of these in dev (bun run dev):**

- [ ] Create a new session from the sidebar; send a message; see streaming chunks.
- [ ] Cancel mid-stream via the stop button.
- [ ] Reload the page — session is in the sidebar; history loads.
- [ ] Rename and delete a session.
- [ ] ChatPill, QuickAsk, SidePanel, AgentChatSidebar — send one message through each, confirm streams.
- [ ] Files tab chat.
- [ ] OS ChatPanel.

If anything fails, stop and fix before moving to Cluster 3.

**No commit here** — pure verification.

---

# Cluster 3 — DM / group cutover

---

## Task 24: DM + group wrappers

**Files:** `paw-enterprise/src/lib/core/chat/service.ts`

```ts
export async function dmChat(content: string, roomId: string, opts: {...}) {
  return agentChat('dm', roomId, content, { clientMessageId: ..., ...opts });
}
export async function groupChat(content: string, groupId: string, opts: {...}) {
  return agentChat('group', groupId, content, { clientMessageId: ..., agentId: opts.agentId, ...opts });
}
```

**Step 1:** unit tests — dispatch URL/body.

**Step 2:** commit.

```bash
git commit -am "feat(chat): dmChat + groupChat wrappers"
```

---

## Task 25: Swap `chatSocket.sendMessage` in `core/chat/store.svelte.ts:680`

**Files:** `paw-enterprise/src/lib/core/chat/store.svelte.ts`

Today: optimistic push + `chatSocket.sendMessage(roomId, text, {...})` with fallback to `chatApi.sendMessage`. After: optimistic push + `dmChat|groupChat` call (decide scope from the room type).

**Step 1: Read the room type enum** — how do we tell DM from group? (Likely `room.type === 'dm'`.)

**Step 2: Replace.** Route DMs to `dmChat`, everything else to `groupChat`.

**Step 3: Keep WS connected** — it still receives `message.new` for other participants, `agent.typing`, etc. But we stop calling `chatSocket.sendMessage`.

**Step 4: Manual smoke** — open a group chat, send, see own chunks + eventual persisted message; open as second user in another browser, see `message.new` at stream end.

**Step 5: Commit.**

```bash
git commit -am "feat(chat): DM/group send routes to unified endpoint"
```

---

## Task 26: Delete WS `sendMessage`

**Files:** `paw-enterprise/src/lib/core/chat/socket.ts:120-189`

Once grep confirms no remaining callers:

```bash
grep -rn "chatSocket\.sendMessage\|socket\.sendMessage" src/lib/
```

**Step 1:** Delete the `sendMessage` method from `socket.ts`.

**Step 2:** Delete the `chatApi.sendMessage` REST fallback if the unused-import linter flags it (otherwise keep — it might still serve non-chat pockets of the app).

**Step 3: Commit.**

```bash
git commit -am "chore(chat): remove WS send path"
```

---

## Task 27: DM + group vitest suites

**Files:**
- `paw-enterprise/src/lib/core/chat/__tests__/agent-chat-dm.test.ts` (create)
- `paw-enterprise/src/lib/core/chat/__tests__/agent-chat-group.test.ts` (create)

Mirror session-scope suite.

```bash
bun run test -- --run src/lib/core/chat/
```

**Commit.**

```bash
git commit -am "test(chat): dm + group suites"
```

---

## Task 28: Final smoke pass

Run everything (same checklist as Task 23) plus:

- [ ] DM with a human peer — send, see their reply.
- [ ] DM with an agent — send, see streaming chunks.
- [ ] Group chat with two members — caller sees chunks; second tab sees `message.new`.
- [ ] Pocket chat — Ripple inline rendering intact.

If all green, push the branch and open the PR.

---

## Task 29: Cleanup + PR

**Step 1:** Final `ruff check` + `bun run check`.

```bash
cd D:/paw/backend && uv run ruff check ee/cloud/chat/ tests/cloud/chat/ && uv run mypy ee/cloud/chat/
cd D:/paw/paw-enterprise && bun run check
```

**Step 2:** Update `PROGRESS.md` with the outcome.

**Step 3:** Push both repos' `feat/chat-unify` branches, open PRs, link the design doc.

---

## Baseline

Filled in by Task 0.

- Backend `tests/cloud/chat/` baseline: _____ passed, _____ failed.
- Frontend `src/lib/core/chat/` vitest baseline: _____ passed, _____ failed.

## Gotchas

- Always `git branch --show-current` at the start and end of each task.
- `Beanie Document.model_construct(...)` in unit tests, not `Document(...)`.
- `CloudError(status_code, code, message)` — positional.
- Ruff UP042: `StrEnum`, never `class X(str, Enum)`.
- Subagent reports can lie; verify with `git show` / re-run tests.
- Svelte 5: `$state`, `$derived`, no legacy stores. Don't break reactivity with direct mutations.
- Tailwind 4: no string interpolation in `class=""` — use `cn()`.
- The "no buttons in chat-inline Ripple specs" invariant still holds (chat-inline display only; pockets own interactive UI).
