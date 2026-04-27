# Phase 4: `workspace/` Module Migration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the hexagonal layout to `workspace/` — the biggest module so far at 546 lines of service code spanning three sub-domains (workspace CRUD, members, invites). This is a substantial refactor: 11 instance methods, 3 classmethod realtime helpers preserved verbatim, soft-delete cascade, dual event paths (`emit()` realtime + `event_bus.emit()` legacy), cache-invalidation via `get_resolver()`, and a cross-module `NotificationService.create_default` call.

**Architecture:**
- Three domain types live together in `workspace/domain.py` (`Workspace`, `WorkspaceMember`, `Invite`) because they're tightly coupled in operations like invite acceptance.
- Two repositories: `IWorkspaceRepository` (Workspace doc + member-list queries on User docs, since members are stored on User) and `IInviteRepository` (Invite doc CRUD).
- DTO layer matches existing camelCase + underscore wire shape (`_id`, `createdAt`, `memberCount`, `joinedAt`, `invitedBy`, `expiresAt`).
- `WorkspaceService` becomes an instance class. Legacy classmethod facade is added with `*_default` suffix for the 11 mutating methods. **The 3 realtime classmethods stay verbatim** (`list_member_ids`, `list_admin_ids`, `list_peer_ids`) since they're pure queries used directly by `realtime/audience.py` and a couple of routers.
- Router uses `Depends(request_context)` for ctx and `Depends(get_workspace_service)` for the service. Auth-action guards (`require_action`, `require_membership`) stay where they are — moved to `_core/deps` in Phase 1.

**Tech Stack:** Python 3.11+, FastAPI, Beanie, Pydantic v2, pytest.

---

## Spec sections covered

- §4.1 layer model, §4.2 hybrid layout, §4.3 RequestContext adoption.
- §6 errors: service raises typed `CloudError` subclasses (`NotFound`, `ConflictError`, `Forbidden`, `SeatLimitError`, `ValidationError`).

Out of scope (deferred):
- Splitting workspace into 3 packages (workspace/, members/, invites/) — premature; size is manageable in one module.
- Replacing `event_bus.emit` (legacy in-process bus) with the new `EventBus` port — happens in Phase 5 (realtime).
- A real `IUserMembershipRepository` extracted from `IWorkspaceRepository.list_members*` — defer until auth's repo is more developed.

---

## File Structure

**Create:**
- `ee/cloud/workspace/domain.py` — `Workspace`, `WorkspaceMember`, `Invite`
- `ee/cloud/workspace/dto.py` — `WorkspaceOut`, `MemberOut`, `InviteOut`, `ValidateInviteOut` + mappers
- `ee/cloud/workspace/repositories.py` — `IWorkspaceRepository`, `IInviteRepository` + Beanie impls
- `tests/cloud/workspace/test_domain.py`
- `tests/cloud/workspace/test_dto.py`
- `tests/cloud/workspace/test_repository_inmemory.py`
- `tests/cloud/workspace/test_service_v2.py`

**Modify:**
- `ee/cloud/workspace/service.py` — instance class + classmethod facade (`create_default` etc.); 3 realtime classmethods preserved verbatim
- `ee/cloud/workspace/router.py` — `RequestContext` + DTOs + DI service
- `ee/cloud/workspace/__init__.py` — drop the router re-export (lesson from auth's circular import)
- `ee/cloud/chat/router.py` — `WorkspaceService.list_peer_ids` stays (no rename)
- `ee/cloud/uploads/router.py` — `WorkspaceService.list_admin_ids` stays (no rename)
- `ee/cloud/__init__.py` — same (3 realtime helpers stay)
- `tests/cloud/workspace/test_workspace_emits.py` — rename existing classmethod calls to `*_default` variant (11 call sites)
- `tests/cloud/notifications/test_derived.py` — already-failing baseline; rename if practical, otherwise leave (still fails for unrelated reasons)

**Delete:**
- `ee/cloud/workspace/schemas.py`
- `tests/cloud/test_workspace_schemas.py` (assumed exists; verify before deleting)

---

## Pre-flight

- Branch is `refactor/cloud-restructure` (already merged Phase 0-3; tip `4a3b24ad`).
- All future Phase 4 commits land directly on this branch.

---

## Naming conventions for the classmethod facade

| Existing (kept on classmethod) | New instance method | Classmethod facade name |
|---|---|---|
| `list_member_ids(workspace_id)` | (same — kept as classmethod, no instance variant) | `list_member_ids` (unchanged) |
| `list_admin_ids(workspace_id)` | (same) | `list_admin_ids` (unchanged) |
| `list_peer_ids(user_id)` | (same) | `list_peer_ids` (unchanged) |
| `create(user, body)` | `create(ctx, body)` | `create_default(user, body)` |
| `get(workspace_id, user)` | `get(ctx, workspace_id)` | `get_default(workspace_id, user)` |
| `update(workspace_id, user, body)` | `update(ctx, workspace_id, body)` | `update_default(workspace_id, user, body)` |
| `delete(workspace_id, user)` | `delete(ctx, workspace_id)` | `delete_default(workspace_id, user)` |
| `list_for_user(user)` | `list_for_user(ctx)` | `list_for_user_default(user)` |
| `list_members(workspace_id, user)` | `list_members(ctx, workspace_id)` | `list_members_default(workspace_id, user)` |
| `update_member_role(workspace_id, target, role, user)` | same | `update_member_role_default(workspace_id, target, role, user)` |
| `remove_member(workspace_id, target, user)` | same | `remove_member_default(workspace_id, target, user)` |
| `list_invites(workspace_id)` | same | `list_invites_default(workspace_id)` |
| `create_invite(workspace_id, user, body)` | `create_invite(ctx, workspace_id, body)` | `create_invite_default(workspace_id, user, body)` |
| `validate_invite(token)` | same | `validate_invite_default(token)` |
| `accept_invite(token, user)` | same | `accept_invite_default(token, user)` |
| `revoke_invite(workspace_id, invite_id, user)` | same | `revoke_invite_default(workspace_id, invite_id, user)` |

---

## Tasks

### Task 1: domain.py
- Create `Workspace`, `WorkspaceMember`, `Invite` frozen dataclasses
- Tests: dataclass invariants
- Commit: `feat(workspace): domain — Workspace, WorkspaceMember, Invite value objects`

### Task 2: dto.py
- `WorkspaceOut`, `MemberOut`, `InviteOut`, `ValidateInviteOut` (matches existing wire shape exactly)
- `workspace_to_dto`, `member_to_dto`, `invite_to_dto`, `invite_to_validate_dto`
- Tests: shape + field mapping
- Commit: `feat(workspace): dto — wire shapes + mappers`

### Task 3: repositories.py
- `IWorkspaceRepository` Protocol — workspace CRUD + member-list queries on User docs
- `IInviteRepository` Protocol — invite CRUD
- `MongoWorkspaceRepository`, `MongoInviteRepository`
- Module-level `get_workspace_repository()`, `get_invite_repository()`
- Tests: in-memory fakes for each
- Commit: `feat(workspace): repositories — IWorkspaceRepository + IInviteRepository`

### Task 4: service.py refactor
- New instance class with the 11 methods (using ctx + repos + emit/event_bus/resolver)
- Classmethod facades with `_default` suffix (delegate to default-repo instance)
- 3 realtime classmethods preserved verbatim
- Tests: in-memory repos, captured events, asserts behavior + side effects
- Commit: `refactor(workspace): service uses domain/repos/dto layers`

### Task 5: router.py refactor
- All endpoints use `Depends(request_context)`, `response_model=...OutTypes`
- Service via `Depends(get_workspace_service)`
- Auth guards (`require_action`, `require_membership`) preserved on each route
- Commit: `refactor(workspace): router uses RequestContext + DTOs`

### Task 6: Update external test sites
- Rename `WorkspaceService.<method>(` → `WorkspaceService.<method>_default(` in `tests/cloud/workspace/test_workspace_emits.py`
- Run full test suite; baseline must be unchanged
- Commit: `test(workspace): update mock targets for *_default classmethod renames`

### Task 7: Drop `workspace/__init__.py` router re-export + delete `schemas.py`
- Same circular-import-prevention pattern as auth
- Commit: `refactor(workspace): drop router re-export; delete schemas.py`

---

## Key code patterns

### Soft-delete cascade in `IWorkspaceRepository.delete`

The current implementation:
1. Sets `deleted_at` on the workspace
2. Strips workspace from every member's `User.workspaces` list
3. Clears `active_workspace` on users who had the deleted workspace selected, swapping for another membership if available

Repository implementation must preserve all 3 steps. Tests use an in-memory user store + workspace store.

### Cache invalidation

The current code calls `get_resolver().invalidate_workspace(workspace_id)` after mutations. The service keeps doing this directly (cache invalidation is a side-effect of the mutation, not a domain rule). When tests use a fake repo, they patch `get_resolver` or assert it was called.

### Dual event paths

Some methods emit BOTH:
- `await emit(WorkspaceMemberAdded(...))` — typed-event path via realtime bus
- `await event_bus.emit("invite.accepted", {...})` — legacy in-process bus

Both must be preserved. Tests capture both via monkeypatch.

### Classmethod facade pattern (recap)

```python
class WorkspaceService:
    def __init__(self, ws_repo: IWorkspaceRepository, invite_repo: IInviteRepository) -> None:
        self._ws = ws_repo
        self._invites = invite_repo

    async def create(self, ctx: RequestContext, body: CreateWorkspaceRequest) -> Workspace:
        ...

    @classmethod
    async def create_default(cls, user: User, body: CreateWorkspaceRequest) -> dict:
        from ee.cloud._core.context import RequestContext, ScopeKind
        ctx = RequestContext(
            user_id=str(user.id),
            workspace_id=user.active_workspace,
            request_id="legacy",
            scope=ScopeKind.NONE,
            started_at=datetime.now(UTC),
        )
        impl = cls(get_workspace_repository(), get_invite_repository())
        ws = await impl.create(ctx, body)
        return workspace_to_dto(ws).model_dump()

    # 3 realtime helpers stay as classmethods, no facade rename
    @classmethod
    async def list_member_ids(cls, workspace_id: str) -> list[str]:
        # exact body from the old service.py
        ...
```

The realtime helpers are PURE QUERIES with no business logic, so no instance variant is needed; they stay as classmethods that call User.find directly. This keeps the 5 existing call sites in `realtime/audience.py`, `chat/router.py`, `uploads/router.py`, and `ee/cloud/__init__.py` unchanged.

---

## Self-review

**Spec coverage:** §4.1, §4.2, §4.3, §6 ✓.

**Out-of-scope items deferred (named):** package-split into 3, real EventBus port, dedicated User-membership repo.

**Type consistency:** `Workspace` (domain) vs `_WorkspaceDoc` (Beanie alias) — repository converts. `Invite` (domain) vs `_InviteDoc` — same pattern.

**Wire-shape preservation:** `_id` (not `id`), `createdAt`/`expiresAt`/`joinedAt` (camelCase), `memberCount` (camelCase), `invitedBy` (camelCase). DTO field names match wire keys; tests assert byte-level equality at the router boundary.

---

## Handoff

When tasks pass, all changes are on `refactor/cloud-restructure`. Next plan (Phase 5): `refactor/cloud-realtime` — promote `realtime/bus.py` `EventBus` Protocol into `_core/ports.py`, update existing consumers.
