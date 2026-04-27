# `ee/cloud/` Clean-Architecture Restructure — Design

**Date:** 2026-04-27
**Status:** Draft, pending user review
**Branch:** `refactor/cloud-restructure` (off `ee`)
**Author:** brainstorming session, decisions log at the end

## 1. Problem

`ee/cloud/` was built incrementally with hotfixes. Recurring symptoms:

- **Layers leak.** Routers reach into Beanie ODM directly; services raise `HTTPException`; auth checks scattered across modules with one-off "exemptions."
- **Big-file gravity.** `chat/` has five files >600 lines; cross-cutting concerns (`shared/agent_bridge.py` at 500 lines) absorb features they shouldn't own.
- **Band-aids.** Memory log calls out four: Socket.IO mount issues, auth-middleware exemptions, duplicate-message bugs, missing awaits. Each one was patched at the symptom, not the cause.
- **Performance is unmeasured.** No timing data on the hot path; "feels slow" is a guess.

This restructure makes the architecture enforce its own invariants so these classes of bug become structurally impossible (or at least loudly visible) to introduce.

## 2. Goals

1. **Correctness first.** Auth, awaits, message persistence, and workspace-scope checks become impossible to bypass by the layout itself, not by reviewer vigilance.
2. **Structural cleanup.** Each module has the same 5-file shape (`domain`, `repositories`, `dto`, `service`, `router`). No file does two layers' jobs.
3. **Performance pass.** Measure → optimize the top 3 hot endpoints. Done after structural work, on stable code.

Priority: correctness > structure > performance. Performance work happens last on a known-correct, known-clean base.

## 3. Non-goals

- **No frontend changes.** `paw-enterprise/` consumes the same wire-level JSON as today. API contract is invariant; proven by golden-response tests at the router boundary.
- **No new feature work** in this slice. We ship the same behavior, restructured.
- **No swap of persistence engine.** Mongo/Beanie stays. The repository abstraction exists so services don't depend on Beanie, not because we plan to leave Mongo.
- **No CQRS for read-heavy modules other than `chat`.** Premature elsewhere.
- **No mutation testing, no architecture-fitness tests, no perf-regression CI gates.** (Test bar A; revisit if drift becomes a problem.)
- **No restructure of `chat/` until non-chat modules are done** and `feat/chat-unify` follow-ups have settled.

## 4. Architecture

### 4.1 Layer model (per module)

```
                         ┌──────────────┐
HTTP request ──────────▶ │   router.py  │  FastAPI endpoints
                         │              │  Depends(...) for context, service
                         └──────┬───────┘
                                │ DTO in / DTO out
                                ▼
                         ┌──────────────┐
                         │   dto.py     │  Pydantic request/response models
                         └──────┬───────┘
                                │
                                ▼
                         ┌──────────────┐
                         │  service.py  │  Orchestration, business rules
                         │              │  Takes RequestContext + Domain in,
                         │              │  returns Domain. Raises CloudError.
                         └──┬─────────┬─┘
                            │         │
                            ▼         ▼
                  ┌──────────────┐ ┌──────────────┐
                  │  domain.py   │ │ repositories.│  Pure value objects.
                  │              │ │ py           │  Repository = Protocol +
                  │              │ │              │  Mongo impl in same file.
                  └──────────────┘ └──────┬───────┘
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ Beanie / │   Only repositories touch
                                     │ Mongo    │   the ODM directly.
                                     └──────────┘
```

**Hard rules:**
- `router.py` may import only from `dto`, `service`, `_core` (and FastAPI/std).
- `service.py` may import only from `domain`, `repositories`, `_core` (and std). Never `HTTPException`. Never Beanie.
- `domain.py` is pure: dataclasses / `attrs` / pydantic-as-value-object. No I/O imports. No Beanie. No FastAPI.
- `repositories.py` exports a `Protocol` (the abstract repo) and a Mongo implementation. Services depend on the Protocol.
- `dto.py` may import `domain` for type-only mappings; never the other way around.

### 4.2 Directory layout (Hybrid C from brainstorming)

```
ee/cloud/
├── _core/                       Cross-cutting framework. Underscored prefix
│   ├── __init__.py              signals "infrastructure, don't import from
│   ├── context.py               routers."
│   ├── errors.py                  RequestContext (workspace_id, user_id,
│   ├── repository.py              request_id, scope), error→HTTP handler,
│   ├── ports.py                   base Repository[Domain], port Protocols
│   ├── http.py                    (Clock, IdGenerator, EventBus), and one
│   ├── timing.py                  request-timing middleware that records
│   └── tests/                     p50/p95 to a per-process buffer for the
│                                  later perf phase.
│
├── auth/
│   ├── domain.py                AuthUser, AuthSession (value objects)
│   ├── repositories.py          IUserRepository (Protocol), MongoUserRepository
│   ├── dto.py                   LoginRequest, MeResponse, etc.
│   ├── service.py               authenticate(), refresh(), logout()
│   ├── router.py                FastAPI endpoints
│   └── tests/                   Per-module test colocation
│
├── workspace/                   Same shape …
├── pockets/
├── sessions/
├── agents/
├── kb/
├── files/
├── uploads/
├── notifications/
├── memory/
├── chat/                        (Restructured last. May split repositories.py
│   │                             into repositories/{message,group,session}.py
│   │                             when it outgrows a single file.)
│
├── models/                      Beanie ODM documents. Only `repositories.py`
│                                files import from here. Will eventually be
│                                renamed `_models/` to signal "infra layer."
│
├── ripple_normalizer.py         Stays where it is for this slice (utility,
├── license.py                   not domain-shaped). Move only if a clean home
└── features.py                  emerges naturally during chat phase.
```

**Rule for outgrowing single-file layers:** if a module's `repositories.py` exceeds ~250 lines, split into a `repositories/` sub-folder with one repo per file. Same for `service.py`. Don't split preemptively.

### 4.3 `_core/` contents

#### `_core/context.py`

```python
@dataclass(frozen=True)
class RequestContext:
    user_id: str            # never empty in authed routes
    workspace_id: str       # never empty in workspace-scoped routes
    request_id: str         # for log correlation
    scope: ScopeKind        # workspace | session | pocket | group | dm
    started_at: datetime    # for timing the request

async def request_context(
    user: User = Depends(current_active_user),
) -> RequestContext: ...
```

Every authed router endpoint takes `ctx: RequestContext = Depends(request_context)`. This eliminates ad-hoc `current_user_id`, `current_workspace_id`, etc., and gives every service call a typed envelope.

#### `_core/errors.py`

Promote and consolidate the existing `shared/errors.py` `CloudError` hierarchy. Add:
- `Conflict(409)`, `RateLimited(429)`, `Unprocessable(422)`, `Internal(500)` (only the ones missing).
- `CloudError.with_cause(exc)` — capture an underlying exception for log context without leaking it to clients.

Single FastAPI exception handler in `_core/http.py` maps `CloudError → JSONResponse`. Routers never `raise HTTPException`. Linter rule (informal — code review) enforces this; if drift appears, we add an arch-fitness test later.

#### `_core/repository.py`

```python
class Repository(Protocol[Domain]):
    async def get(self, id: str) -> Domain | None: ...
    async def list(self, **filters) -> list[Domain]: ...
    async def create(self, entity: Domain) -> Domain: ...
    async def update(self, entity: Domain) -> Domain: ...
    async def delete(self, id: str) -> None: ...
```

Modules extend with their own methods. Each module's `repositories.py` provides:
- `IFooRepository(Protocol)` — module-specific surface
- `MongoFooRepository(IFooRepository)` — Beanie-backed impl
- A `_provide_foo_repository()` FastAPI dependency that returns the Mongo impl by default. Tests inject fakes.

#### `_core/ports.py`

Cross-cutting infra contracts that domain code may need:
- `Clock` — for testable time
- `IdGenerator` — `ObjectId`-or-equivalent factory
- `EventBus` — port over `realtime/bus.py`; promotes the existing in-process pubsub from a free function to a swappable interface

#### `_core/http.py`

- `error_handler` — FastAPI exception handler for `CloudError`
- `add_error_handler(app)` — registers the handler
- (Future) request-id middleware that populates `RequestContext.request_id`

#### `_core/timing.py`

Lightweight middleware that records `(endpoint, duration_ms)` samples to an in-memory ring buffer (default 10k samples per endpoint). A small CLI dumps p50/p95/p99 on demand. Exists from Phase 0 so by the time we reach the perf phase we have weeks of data, not zero. Deliberately not a metrics system — no Prometheus, no histograms — because we want to keep this slice small.

### 4.4 Service / repository contract

- Services receive `RequestContext` as their first argument and pass it to repository calls when scoping is needed (`repo.list_for_workspace(ctx.workspace_id)`).
- Services return **domain** objects, never Beanie documents.
- Routers map domain → DTO at the boundary. The mapping is explicit (not `model_dump()` of the Beanie doc), so adding a domain field doesn't accidentally leak it through the API.
- Repositories own all Beanie/Mongo-specific concerns (indexes, transactions, ObjectId conversion). Service code reads as plain Python.

### 4.5 Hot-path CQRS (chat only)

`chat/` will split into:
- `chat/services/read.py` — history fetch, search, unread counts
- `chat/services/write.py` — message create, edit, delete, react

Reads and writes have very different access patterns; splitting lets us cache or denormalize the read side without touching write logic. **No CQRS in any other module.** It's overhead we don't pay unless we need to.

## 5. Migration strategy (strangler, atomic per module)

### 5.1 Per-module steps

For each module M:

1. **Branch off `refactor/cloud-restructure`.** Branch name `refactor/cloud-<m>`.
2. **Write golden-response tests** capturing the current HTTP behavior of M's router. These are the parity gate and must exist *before* any structural change to M. If M already has comparable test coverage, supplement only the gaps.
3. **Build the new layers** alongside the existing code:
   - Create `M/domain.py`, `M/repositories.py`, `M/dto.py`.
   - Refactor `M/service.py` to depend on the new layers.
   - Refactor `M/router.py` to take `RequestContext`, return DTOs.
4. **Run tests.** Existing unit tests + new unit tests + golden-response tests + (if applicable) integration tests against testcontainers Mongo.
5. **Audit external imports** of the old path. Grep the entire repo (not just `ee/cloud/`) for callers of any function/class about to be deleted: other backend packages, tests, scripts, hooks, type stubs. Update or document each.
6. **Delete dead old code in the same PR.** If the new path replaces an old function entirely, the old function goes. No coexistence beyond the PR. (Step 5 makes this safe.)
7. **Squash-merge to `refactor/cloud-restructure`.** Keeps the per-module commit clean for review.

API contract is invariant within each PR. Frontend is never broken.

### 5.2 Module ordering (Z order: chat last)

| Phase | Branch | Contents |
|---|---|---|
| 0 | `refactor/cloud-core-bootstrap` | Build `_core/` (`context`, `errors`, `repository`, `ports`, `http`, `timing`). No module changes yet. |
| 1 | `refactor/cloud-shared-into-core` | Migrate useful pieces of `shared/` (`errors.py`, parts of `deps.py`, `time.py`) into `_core/`. Leave `shared/agent_bridge.py`, `shared/event_handlers.py`, `shared/events.py`, `shared/db.py` for later phases that touch them. |
| 2 | `refactor/cloud-notifications` | **Pilot.** Smallest, most isolated module. Validates the pattern. |
| 3 | `refactor/cloud-auth` | Foundational; everything else depends on a clean auth surface. |
| 4 | `refactor/cloud-workspace` | Depends on auth. |
| 5 | `refactor/cloud-realtime` | `realtime/` becomes `_core/realtime/` (it's framework, not domain). |
| 6 | `refactor/cloud-agents`, then `refactor/cloud-kb` | Workspace-scoped resources. May be parallelized by two contributors; otherwise solo-sequential. |
| 7 | `refactor/cloud-files`, then `refactor/cloud-uploads` | Files first; uploads depends on files' permission model. |
| 8 | `refactor/cloud-pockets`, then `refactor/cloud-memory` | Pockets is complex (`agent_context.py`); memory is small. |
| 9 | `refactor/cloud-sessions` | Last non-chat. Adjacent to chat; final dry-run before chat. |
| 10 | `refactor/cloud-chat` | Includes `shared/agent_bridge.py` migration. CQRS split. Lands after `feat/chat-unify` follow-ups have merged. |
| 11 | `refactor/cloud-perf-pass` | Read timing buffer, identify top 3 slow endpoints, optimize. Single PR. |

### 5.3 Coexistence with `feat/chat-unify`

- Phases 0-9 don't touch `chat/`, so they can land while chat-unify follow-ups are still in flight.
- The two stashes that touch `agent_router.py`, `agent_service.py`, `ripple_normalizer.py`, `shared/agent_bridge.py` should be unstashed and landed (or formally abandoned) before Phase 10. Document this as a Phase 10 prerequisite.
- The frontend `feat/chat-unify-rooms-files` work is paw-enterprise scope and not blocked by this restructure at any phase.

## 6. Error handling

Single source of truth: `_core/errors.py`. Routers never construct `HTTPException`. Services raise `CloudError` subclasses. The `_core/http.error_handler` maps to JSON.

Wire-format envelope (matches existing `CloudError.to_dict`):
```json
{ "error": { "code": "workspace.not_found", "message": "Workspace 'abc' not found" } }
```

Status codes match the `CloudError` subclass. Existing frontend handling continues to work.

## 7. Testing (Test bar A — Pragmatic)

Every per-module migration ships:

1. **Unit tests** for new `domain.py`, `service.py` — ≥80% line coverage on new code (measured by `pytest-cov` `--cov=ee.cloud.<module>` filtered to changed files).
2. **Golden-response tests** at the router boundary. Format: a fixtures file of `(method, path, body, headers) → expected JSON`. Test asserts byte-equal response. These exist *before* the migration begins and serve as the parity gate.
3. **Integration tests** against testcontainers Mongo for repositories that exercise non-trivial queries (aggregations, indexes, transactions). Skip for CRUD-only repos; rely on unit tests with a fake.
4. **Existing tests stay green.** No `xfail` decorations on previously-passing tests.

CI: existing `uv run pytest --ignore=tests/e2e` continues to gate. We do **not** add coverage gates, perf-regression gates, or AST-based architecture tests in this slice.

Out of scope: mutation testing, property tests, fuzz tests. Revisit if drift appears.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Strangler stalls — modules half-migrated for months | Each module is a single PR. No PR merges with both old and new code paths active. |
| Golden tests are flaky (timestamps, ObjectIds) | Use a fixed `Clock` and `IdGenerator` port in tests. `_core/ports.py` exists explicitly to make this possible. |
| `shared/` migration breaks downstream imports | Phase 1 keeps shims at old paths during the transition; remove shims in Phase 11 cleanup. |
| Chat phase is huge | Already deferred to last. Within Phase 10, sub-divide by `services/read.py`, `services/write.py`, repositories, DTO — multiple internal commits, single PR. |
| Performance work surfaces require schema/index changes | Mongo schema changes are out of scope for the structural phases. If Phase 11 needs them, they ship in a separate, clearly-scoped PR with explicit user review. |
| Two outstanding stashes touch `chat/` files | Phase 10 prerequisite: stashes resolved (landed or formally dropped) before chat work begins. |

## 9. Open questions for user review

These are the calls I made unilaterally because the user is AFK. Flag if any are wrong on return:

1. **Branch base.** Restructure is off `ee`, not `dev`/`main`. If `ee` is not the right trunk for this work, redirect.
2. **Pilot = `notifications/`.** Smallest + most isolated. If you'd rather pilot on `auth/` (foundational) for higher early payoff, swap Phase 2 and Phase 3.
3. **`_core/` vs `core/`** (no underscore). Underscore signals "internal infra, don't reach in from a router." If you prefer the un-underscored convention, rename.
4. **Where does `models/` end up?** Kept where it is for this slice. Long-term it probably belongs as `_models/` (infra) or split per-module (`auth/_documents.py`). Defer the rename to after all modules migrate.
5. **`ripple_normalizer.py` and `license.py`** at the module root — left alone. Note: `ripple_normalizer.py` is touched by one of the deferred chat-unify stashes, so it'll come up naturally during Phase 10 prep. If `license.py` should move (e.g., into `_core/license.py`), name a destination.

## 10. Decisions log (from brainstorming)

| # | Decision | Choice | Note |
|---|---|---|---|
| 1 | Slice | A — `backend/ee/cloud/` clean-architecture restructure | Per memory: prior request for "deep restructure, clean arch, production quality" |
| 2 | Dimension priority | D — comprehensive | Default rank: B > A > C (correctness > structure > performance) |
| 3 | Depth | 3 — Deep | Repos, DTO ↔ domain, CQRS for `chat` only |
| 4 | Migration | Strangler | Atomic per-module; no feature flags (API contract invariant) |
| 5 | Order | Z — chat last | Non-chat first, patterns settle before tackling biggest module |
| 6 | Test bar | A — Pragmatic | Unit + golden-response + Mongo integration. No mutation/perf/arch-fitness gates. |
| 7 | Layout | C — Hybrid | Per-module folder, layer-as-file. `_core/` for cross-cutting framework. |
| 8 | Pilot module | `notifications/` | Smallest + most isolated. (Open Q #2 above.) |
| 9 | Branch base | `ee` | (Open Q #1 above.) |
