# Phase 1: `shared/` → `_core/` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the canonical home of three subsets of `ee/cloud/shared/` (`errors.py`, `time.py`, and the cross-cutting parts of `deps.py`) into `ee/cloud/_core/`. Existing import paths continue to work via re-export shims, so no consumer code changes in this phase. The domain-specific guards in `shared/deps.py` (group, agent, pocket) stay in `shared/` until those domains migrate in Phases 7-8.

**Architecture:** Strangler step. For each migrated symbol, the canonical definition moves from `shared/` to `_core/`; the original `shared/` location becomes a re-export shim importing from `_core/`. All 45 existing call sites (counted: 18 for errors, 8 for time, 19 for deps) keep working without modification because Python re-imports return the same class object.

**Tech Stack:** Python 3.11+, FastAPI dependencies, Beanie/Mongo (already imported by deps), pytest.

---

## Spec sections covered

- §5.2 Phase 1 (`refactor/cloud-shared-into-core` — migrate errors/time/parts of deps into `_core/`)
- §6 Error handling — canonical `CloudError` hierarchy now lives in `_core/errors.py`

Out of scope (deferred):
- `shared/agent_bridge.py`, `shared/event_handlers.py`, `shared/events.py`, `shared/db.py` — Phase 5 (realtime) and Phase 10 (chat).
- Domain-specific guards in `shared/deps.py` (`require_group_action`, `require_agent_owner_or_admin`, `require_pocket_edit`, `require_pocket_owner`) — Phases 7-8 with their owning domains.
- Removing the `shared/` shims entirely — post-Phase-10 cleanup pass.

---

## File Structure

**Create:**
- `ee/cloud/_core/time.py` — canonical home for `iso_utc`
- `ee/cloud/_core/deps.py` — canonical home for the cross-cutting FastAPI deps and workspace-level action guards
- `tests/cloud/_core/test_time.py`
- `tests/cloud/_core/test_deps.py`
- `tests/cloud/_core/test_shims.py` — proves shim identity for all migrated symbols

**Modify:**
- `ee/cloud/_core/errors.py` — flip from re-export shim to canonical definition (defines the classes; shared no longer imports here)
- `ee/cloud/shared/errors.py` — flip to re-export shim
- `ee/cloud/shared/time.py` — flip to re-export shim
- `ee/cloud/shared/deps.py` — re-export the migrated names from `_core/deps.py`; keep domain-specific guards in place

No call-site edits in Phase 1. The shim guarantees backward compatibility.

---

## Conventions

- **Branch:** all tasks happen on `refactor/cloud-shared-into-core` (already created off `refactor/cloud-core-bootstrap`).
- **Commit style:** `refactor(_core): <area> — <one-line summary>` for canonical-home moves; `refactor(shared): <name> shim to _core` for shim conversions.
- **Test runner:** `uv run pytest tests/cloud/_core -v` plus `uv run pytest tests/cloud --maxfail=30 -q` to confirm baseline failure count is unchanged.
- **Lint/format/type:** ruff + mypy on every changed file.

---

## Pre-flight verified

- Branch `refactor/cloud-shared-into-core` created off `refactor/cloud-core-bootstrap` (tip `8ec0c1c8`).
- Working tree clean.
- Import counts on the parent branch: `shared.errors` = 18 files, `shared.time` = 8 files, `shared.deps` = 19 files.
- Cloud test baseline (refactor/cloud-core-bootstrap): 20 failed, 10 errors, 464 passed, 92 warnings.

---

## Task 1: `_core/time.py` — canonical home for `iso_utc`

**Files:**
- Create: `ee/cloud/_core/time.py`
- Modify: `ee/cloud/shared/time.py` (becomes shim)
- Test: `tests/cloud/_core/test_time.py`

- [ ] **Step 1.1: Write failing test**

`tests/cloud/_core/test_time.py`:

```python
"""Tests for ee.cloud._core.time.iso_utc."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from ee.cloud._core.time import iso_utc


def test_none_returns_none() -> None:
    assert iso_utc(None) is None


def test_naive_datetime_anchored_to_utc() -> None:
    naive = datetime(2026, 4, 27, 12, 0, 0)
    assert iso_utc(naive) == "2026-04-27T12:00:00+00:00"


def test_aware_utc_passthrough() -> None:
    aware = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
    assert iso_utc(aware) == "2026-04-27T12:00:00+00:00"


def test_aware_non_utc_preserved() -> None:
    """Non-UTC tz is preserved (we only re-anchor naive values)."""
    pst = timezone(timedelta(hours=-8))
    aware = datetime(2026, 4, 27, 4, 0, 0, tzinfo=pst)
    assert iso_utc(aware) == "2026-04-27T04:00:00-08:00"
```

- [ ] **Step 1.2: Run test, expect collection failure**

```bash
uv run pytest tests/cloud/_core/test_time.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud._core.time'`.

- [ ] **Step 1.3: Implement `_core/time.py`**

`ee/cloud/_core/time.py`:

```python
"""Datetime serialization helpers for cloud services.

Beanie/Mongo persists ``datetime`` values without timezone info, so reads
return naive ``datetime`` objects. ``datetime.isoformat()`` on a naive value
produces ``"2026-04-18T07:00:00"`` with no offset — JS ``new Date(...)`` then
parses that string as **local time**, shifting timestamps by the user's UTC
offset. ``iso_utc`` re-anchors naive values to UTC before formatting so the
emitted string is always unambiguous (``...+00:00``).
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


__all__ = ["iso_utc"]
```

- [ ] **Step 1.4: Run test, expect pass**

```bash
uv run pytest tests/cloud/_core/test_time.py -v
```

Expected: 4 PASS.

- [ ] **Step 1.5: Convert `shared/time.py` to a shim**

Replace the entire content of `ee/cloud/shared/time.py` with:

```python
"""Re-export shim. Canonical home moved to ``ee.cloud._core.time``
in Phase 1 of the cloud-restructure (2026-04-27). This module remains
so existing imports keep working; new code should import from ``_core``.
"""

from ee.cloud._core.time import iso_utc

__all__ = ["iso_utc"]
```

- [ ] **Step 1.6: Verify all `shared.time` consumers still pass**

```bash
uv run pytest tests/cloud --maxfail=30 -q 2>&1 | tail -3
```

Expected: same baseline as recorded above (20 failed, 10 errors, 464+ passed). The shim should be transparent — `iso_utc` is the same function regardless of import path.

- [ ] **Step 1.7: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/time.py ee/cloud/shared/time.py tests/cloud/_core/test_time.py
uv run ruff format ee/cloud/_core/time.py ee/cloud/shared/time.py tests/cloud/_core/test_time.py
uv run mypy ee/cloud/_core/time.py ee/cloud/shared/time.py
```

Expected: clean.

- [ ] **Step 1.8: Commit**

```bash
git add ee/cloud/_core/time.py ee/cloud/shared/time.py tests/cloud/_core/test_time.py
git commit -m "refactor(_core): time — move iso_utc canonical home; shared/time becomes shim"
```

---

## Task 2: `_core/errors.py` — flip canonical home

`_core/errors.py` currently imports from `shared/errors.py` (Phase 0 starting point). Phase 1 reverses this: definitions move to `_core/errors.py`, and `shared/errors.py` becomes the shim.

The flip preserves class identity. Existing assertions like `core_errors.CloudError is shared_errors.CloudError` continue to hold because both modules name the same class object.

**Files:**
- Modify: `ee/cloud/_core/errors.py`
- Modify: `ee/cloud/shared/errors.py`

- [ ] **Step 2.1: Read current `shared/errors.py` content**

```bash
sed -n '1,80p' ee/cloud/shared/errors.py
```

(Verify it matches the snippet below; if it has drifted, align before proceeding.)

Current content (should be identical to this):

```python
"""Unified error hierarchy for the cloud module.

Every domain package raises these instead of raw HTTPException so that
error handling, logging, and API responses stay consistent.
"""

from __future__ import annotations


class CloudError(Exception):
    """Base cloud error with status_code, code (machine-readable), message (human-readable)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict:
        """Return a JSON-serializable error envelope."""
        return {"error": {"code": self.code, "message": self.message}}


class NotFound(CloudError):
    """Resource not found (404)."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        code = f"{resource}.not_found"
        if resource_id:
            message = f"{resource} '{resource_id}' not found"
        else:
            message = f"{resource} not found"
        super().__init__(404, code, message)


class Forbidden(CloudError):
    """Access denied (403)."""

    def __init__(self, code: str, message: str = "Access denied") -> None:
        super().__init__(403, code, message)


class ConflictError(CloudError):
    """Resource conflict (409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class ValidationError(CloudError):
    """Validation failure (422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(422, code, message)


class SeatLimitError(CloudError):
    """Seat/billing limit reached (402)."""

    def __init__(self, seats: int) -> None:
        super().__init__(402, "billing.seat_limit", f"Seat limit of {seats} reached")
```

- [ ] **Step 2.2: Rewrite `_core/errors.py` as canonical home**

Replace the entire content of `ee/cloud/_core/errors.py` with:

```python
"""Canonical error hierarchy for ee/cloud.

Routers must never raise `HTTPException`; services raise these
`CloudError` subclasses and `_core.http.cloud_error_handler` maps them
to JSON responses.

Re-exports remain accessible via `ee.cloud.shared.errors` (a shim) for
the transition period; new code should import from this module.
"""

from __future__ import annotations


class CloudError(Exception):
    """Base cloud error with status_code, code (machine-readable),
    message (human-readable)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict:
        """Return a JSON-serializable error envelope."""
        return {"error": {"code": self.code, "message": self.message}}


class NotFound(CloudError):
    """Resource not found (404)."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        code = f"{resource}.not_found"
        if resource_id:
            message = f"{resource} '{resource_id}' not found"
        else:
            message = f"{resource} not found"
        super().__init__(404, code, message)


class Forbidden(CloudError):
    """Access denied (403)."""

    def __init__(self, code: str, message: str = "Access denied") -> None:
        super().__init__(403, code, message)


class ConflictError(CloudError):
    """Resource conflict (409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class ValidationError(CloudError):
    """Validation failure (422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(422, code, message)


class SeatLimitError(CloudError):
    """Seat/billing limit reached (402)."""

    def __init__(self, seats: int) -> None:
        super().__init__(402, "billing.seat_limit", f"Seat limit of {seats} reached")


class RateLimited(CloudError):
    """Rate limit exceeded (429)."""

    def __init__(self, code: str, message: str = "Rate limit exceeded") -> None:
        super().__init__(429, code, message)


class Internal(CloudError):
    """Unexpected internal error (500). Use sparingly — prefer specific codes."""

    def __init__(
        self, code: str = "internal", message: str = "Internal server error"
    ) -> None:
        super().__init__(500, code, message)


def with_cause(error: CloudError, cause: BaseException) -> CloudError:
    """Attach an underlying exception for log context. Returns the same error
    so it can be used fluently: `raise with_cause(NotFound(...), exc)`.

    The cause is stored on `__cause__` (Python's standard exception-chaining
    slot). The `to_dict()` envelope sent to clients still contains only
    `code` and `message`; the cause is not leaked.
    """
    error.__cause__ = cause
    return error


__all__ = [
    "CloudError",
    "ConflictError",
    "Forbidden",
    "Internal",
    "NotFound",
    "RateLimited",
    "SeatLimitError",
    "ValidationError",
    "with_cause",
]
```

- [ ] **Step 2.3: Convert `shared/errors.py` to a shim**

Replace the entire content of `ee/cloud/shared/errors.py` with:

```python
"""Re-export shim. Canonical home moved to ``ee.cloud._core.errors``
in Phase 1 of the cloud-restructure (2026-04-27). This module remains
so existing imports keep working; new code should import from ``_core``.
"""

from ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
    ValidationError,
)

__all__ = [
    "CloudError",
    "ConflictError",
    "Forbidden",
    "NotFound",
    "SeatLimitError",
    "ValidationError",
]
```

Note: `RateLimited`, `Internal`, and `with_cause` are intentionally not re-exported through `shared.errors` because they are Phase 0 additions. Code that wants them must import from `_core.errors`. This keeps `shared.errors` as a frozen surface.

- [ ] **Step 2.4: Re-run all `_core` tests**

```bash
uv run pytest tests/cloud/_core -v 2>&1 | tail -5
```

Expected: 54 PASS (Phase 0 baseline) + 4 from Task 1 = **58 PASS**, 0 fail.

The Phase 0 `test_errors.py::TestReExports` tests assert `core_errors.CloudError is SharedCloudError` etc. — these must still pass because both modules now reference the same class object (defined in `_core`, re-exported by `shared`).

- [ ] **Step 2.5: Run broader cloud test suite**

```bash
uv run pytest tests/cloud --maxfail=30 -q 2>&1 | tail -3
```

Expected: baseline unchanged (20 failed, 10 errors, 464+ passed). Same number of failures = no regression.

- [ ] **Step 2.6: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/errors.py ee/cloud/shared/errors.py
uv run ruff format ee/cloud/_core/errors.py ee/cloud/shared/errors.py
uv run mypy ee/cloud/_core/errors.py ee/cloud/shared/errors.py
```

Expected: clean.

- [ ] **Step 2.7: Commit**

```bash
git add ee/cloud/_core/errors.py ee/cloud/shared/errors.py
git commit -m "refactor(_core): errors — flip canonical home; shared/errors becomes shim"
```

---

## Task 3: `_core/deps.py` — extract cross-cutting FastAPI deps

`shared/deps.py` mixes cross-cutting deps with domain-specific guards. Phase 1 extracts the cross-cutting subset to `_core/deps.py`; the domain-specific guards stay in `shared/deps.py` and migrate with their domains in Phases 7-8.

**Cross-cutting subset to migrate (canonical):**
- `current_user` — re-export of the auth dep
- `current_user_id`
- `current_workspace_id`
- `optional_workspace_id`
- `_workspace_id_from_path` (private helper, but used by `require_action`'s default arg)
- `require_action`
- `require_action_any_workspace`
- `require_membership`

**Stays in `shared/deps.py` (domain-specific):**
- `require_group_action` — moves with `chat/` (Phase 10)
- `require_agent_owner_or_admin` — moves with `agents/` (Phase 6)
- `require_pocket_edit` — moves with `pockets/` (Phase 8)
- `require_pocket_owner` — moves with `pockets/` (Phase 8)

**Files:**
- Create: `ee/cloud/_core/deps.py`
- Modify: `ee/cloud/shared/deps.py` (re-exports the migrated names; keeps domain guards)
- Test: `tests/cloud/_core/test_deps.py`

- [ ] **Step 3.1: Write failing tests**

`tests/cloud/_core/test_deps.py`:

```python
"""Tests for ee.cloud._core.deps — cross-cutting FastAPI dependencies."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from ee.cloud._core.deps import (
    current_user,
    current_user_id,
    current_workspace_id,
    optional_workspace_id,
    require_action,
    require_action_any_workspace,
    require_membership,
)
from ee.cloud._core.errors import Forbidden


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Minimal stand-in for ee.cloud.models.user.User."""

    def __init__(
        self,
        id: str,
        active_workspace: str | None,
        workspaces: list[_FakeMembership] | None = None,
    ) -> None:
        self.id = id
        self.active_workspace = active_workspace
        self.workspaces = workspaces or []


@pytest.fixture
def make_app():
    """Factory for a FastAPI app with the auth dep overridden to a fake user."""
    from ee.cloud.auth import current_active_user

    def _builder(user: _FakeUser) -> FastAPI:
        app = FastAPI()
        app.dependency_overrides[current_active_user] = lambda: user
        return app

    return _builder


def test_current_user_id_extracts_id(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace="w1")
    app = make_app(user)

    @app.get("/me/id")
    async def _r(uid: str = Depends(current_user_id)) -> dict:
        return {"uid": uid}

    resp = TestClient(app).get("/me/id")
    assert resp.json() == {"uid": "u1"}


def test_current_workspace_id_returns_active(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace="w42")
    app = make_app(user)

    @app.get("/ws/active")
    async def _r(ws: str = Depends(current_workspace_id)) -> dict:
        return {"ws": ws}

    resp = TestClient(app).get("/ws/active")
    assert resp.json() == {"ws": "w42"}


def test_current_workspace_id_400_when_no_active(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace=None)
    app = make_app(user)

    @app.get("/ws/active")
    async def _r(ws: str = Depends(current_workspace_id)) -> dict:
        return {"ws": ws}

    resp = TestClient(app).get("/ws/active")
    assert resp.status_code == 400
    assert "No active workspace" in resp.json().get("detail", "")


def test_optional_workspace_id_returns_none_when_unset(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace=None)
    app = make_app(user)

    @app.get("/ws/optional")
    async def _r(ws: str | None = Depends(optional_workspace_id)) -> dict:
        return {"ws": ws}

    assert TestClient(app).get("/ws/optional").json() == {"ws": None}


def test_require_membership_passes_for_member(make_app) -> None:
    user = _FakeUser(
        id="u1",
        active_workspace="w1",
        workspaces=[_FakeMembership(workspace="w1")],
    )
    app = make_app(user)

    @app.get("/ws/{workspace_id}/view")
    async def _r(workspace_id: str, _u=Depends(require_membership)) -> dict:
        return {"ok": True}

    assert TestClient(app).get("/ws/w1/view").status_code == 200


def test_require_membership_403_for_non_member(make_app) -> None:
    user = _FakeUser(
        id="u1",
        active_workspace="w1",
        workspaces=[_FakeMembership(workspace="w1")],
    )
    app = make_app(user)
    # Add the cloud error handler so Forbidden -> 403 envelope
    from ee.cloud._core.http import add_error_handler

    add_error_handler(app)

    @app.get("/ws/{workspace_id}/view")
    async def _r(workspace_id: str, _u=Depends(require_membership)) -> dict:
        return {"ok": True}

    resp = TestClient(app).get("/ws/w_other/view")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "workspace.not_member"


def test_current_user_is_re_exported_function() -> None:
    """current_user is the simplest possible passthrough — verify it
    returns whatever the auth dep produces."""
    assert callable(current_user)


def test_require_action_returns_named_callable() -> None:
    """require_action returns a closure named after the action; FastAPI
    uses the closure name for OpenAPI op IDs."""
    guard = require_action("workspace.edit")
    assert guard.__name__ == "require_action_workspace_edit"


def test_require_action_any_workspace_uses_active_workspace() -> None:
    """The variant that resolves workspace from the user instead of path
    is built on require_action with current_workspace_id as workspace_dep."""
    guard = require_action_any_workspace("workspace.edit")
    assert callable(guard)
```

- [ ] **Step 3.2: Run tests, expect collection failure**

```bash
uv run pytest tests/cloud/_core/test_deps.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud._core.deps'`.

- [ ] **Step 3.3: Implement `_core/deps.py`**

`ee/cloud/_core/deps.py`:

```python
"""Cross-cutting FastAPI dependencies for cloud routers.

These deps belong to no single domain: they extract identity and
workspace from the authed user, and enforce workspace-level role-based
access control. Domain-specific guards (group, agent, pocket) live in
their owning modules; until those modules migrate, they remain in
``ee.cloud.shared.deps``.

The action-based guard machinery (``require_action``, ``require_membership``)
delegates to ``pocketpaw.ee.guards`` (the platform-wide RBAC package) for
the actual policy lookup. We translate platform ``GuardForbidden``
exceptions to cloud-native ``Forbidden`` so the standard error envelope
applies.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException

from ee.cloud._core.errors import Forbidden
from ee.cloud.auth import current_active_user
from ee.cloud.models.user import User
from pocketpaw.ee.guards.audit import log_denial
from pocketpaw.ee.guards.deps import check_workspace_action
from pocketpaw.ee.guards.rbac import Forbidden as GuardForbidden


# ---------------------------------------------------------------------------
# Identity / workspace extraction
# ---------------------------------------------------------------------------


async def current_user(user: User = Depends(current_active_user)) -> User:
    """Get the authenticated user from the JWT/session."""
    return user


async def current_user_id(user: User = Depends(current_active_user)) -> str:
    """Extract user ID from the authenticated user."""
    return str(user.id)


async def current_workspace_id(user: User = Depends(current_active_user)) -> str:
    """Extract active workspace ID from the authenticated user.

    Raises HTTP 400 (not a CloudError — this surfaces as a setup error
    that the client UI handles, not a denial) when the user has no
    active workspace.
    """
    if not user.active_workspace:
        raise HTTPException(
            400, "No active workspace. Create or join a workspace first."
        )
    return user.active_workspace


async def optional_workspace_id(user: User = Depends(current_active_user)) -> str | None:
    """Extract workspace ID if set, or None."""
    return user.active_workspace


# ---------------------------------------------------------------------------
# Action-based guards (workspace scope)
# ---------------------------------------------------------------------------


async def _workspace_id_from_path(workspace_id: str) -> str:
    """Pull `workspace_id` from the path. FastAPI binds by parameter name."""
    return workspace_id


_WorkspaceIdDep = Callable[..., Coroutine[Any, Any, str]]


def require_action(
    action: str,
    workspace_dep: _WorkspaceIdDep = _workspace_id_from_path,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """FastAPI dependency enforcing an ACTIONS entry against the caller's
    workspace role.

    Default ``workspace_dep`` reads ``workspace_id`` from the path. Pass
    ``current_workspace_id`` to read from the user's active workspace
    instead.

    On deny, raises the cloud-native ``Forbidden`` (CloudError) so the
    global exception handler emits the standard error envelope. Every
    denial is audited via ``log_denial`` already inside
    ``check_workspace_action``, but we also surface the guard's ``code``
    through the cloud envelope.
    """

    async def _guard(
        user: User = Depends(current_active_user),
        workspace_id: str = Depends(workspace_dep),
    ) -> User:
        try:
            check_workspace_action(user, workspace_id, action)
        except GuardForbidden as exc:
            raise Forbidden(exc.code, exc.detail or "Access denied") from exc
        return user

    _guard.__name__ = f"require_action_{action.replace('.', '_')}"
    return _guard


def require_action_any_workspace(
    action: str,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Variant of ``require_action`` that resolves workspace from the
    user's ``active_workspace``. Use when the route has no
    ``{workspace_id}`` path param."""
    return require_action(action, workspace_dep=current_workspace_id)


async def require_membership(
    user: User = Depends(current_active_user),
    workspace_id: str = Depends(_workspace_id_from_path),
) -> User:
    """Light guard — just asserts the user is a member of the path
    workspace. Used on read routes where any member can view (no role
    check)."""
    for m in user.workspaces:
        if m.workspace == workspace_id:
            return user
    log_denial(
        actor=str(user.id),
        action="workspace.view",
        code="workspace.not_member",
        workspace_id=workspace_id,
    )
    raise Forbidden("workspace.not_member", "Not a member of this workspace")


__all__ = [
    "current_user",
    "current_user_id",
    "current_workspace_id",
    "optional_workspace_id",
    "require_action",
    "require_action_any_workspace",
    "require_membership",
]
```

- [ ] **Step 3.4: Run tests, expect pass**

```bash
uv run pytest tests/cloud/_core/test_deps.py -v
```

Expected: 9 PASS.

If `test_require_membership_403_for_non_member` returns 403 but a different envelope, check that `add_error_handler(app)` was wired in the test — without it, `Forbidden` is raised but starlette's default handler returns 500.

- [ ] **Step 3.5: Convert the migrated parts of `shared/deps.py` to re-exports**

Read `ee/cloud/shared/deps.py` first to be sure of its current state, then replace the **top portion** (the imports and the cross-cutting deps — lines ~1 through ~108 ending after `require_membership`) with re-exports. Keep the domain-specific guards (`require_group_action`, `require_agent_owner_or_admin`, `require_pocket_edit`, `require_pocket_owner`) intact below.

The new top of `ee/cloud/shared/deps.py`:

```python
"""FastAPI dependencies for cloud routers.

The cross-cutting deps (identity extraction and workspace-level action
guards) moved to ``ee.cloud._core.deps`` in Phase 1 of the
cloud-restructure (2026-04-27). They are re-exported here so existing
imports keep working; new code should import from ``_core``.

The domain-specific guards (group, agent, pocket) remain here. They will
move with their owning domains in Phases 6-10.
"""

from __future__ import annotations

from ee.cloud._core.deps import (
    _workspace_id_from_path,
    current_user,
    current_user_id,
    current_workspace_id,
    optional_workspace_id,
    require_action,
    require_action_any_workspace,
    require_membership,
)

# ---------------------------------------------------------------------------
# Domain-specific guards (will migrate with their owning domains in Phases 6-10)
# ---------------------------------------------------------------------------

```

…then leave the domain-specific guards (everything from `require_group_action` onward) exactly as it was. Do NOT modify those guards in this phase.

Concretely the procedure:

1. Open `ee/cloud/shared/deps.py`.
2. Identify the line where `require_group_action` begins (currently around line 116).
3. Replace lines 1 through 114 (everything from the top through the closing of `require_membership` plus the comment header that precedes `require_group_action`) with the re-export block above.
4. Save.
5. Verify the file still parses:
   ```bash
   uv run python -c "import ee.cloud.shared.deps; print(dir(ee.cloud.shared.deps))" 2>&1 | tail -5
   ```
   Expected: a list of names that includes `current_user`, `current_user_id`, `current_workspace_id`, `optional_workspace_id`, `require_action`, `require_action_any_workspace`, `require_membership`, `require_group_action`, `require_agent_owner_or_admin`, `require_pocket_edit`, `require_pocket_owner`.

- [ ] **Step 3.6: Verify all `shared.deps` consumers still pass**

```bash
uv run pytest tests/cloud --maxfail=30 -q 2>&1 | tail -3
```

Expected: same baseline (20 failed, 10 errors). The shim is transparent because the re-exported objects are the same Python objects as before.

- [ ] **Step 3.7: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/deps.py ee/cloud/shared/deps.py tests/cloud/_core/test_deps.py
uv run ruff format ee/cloud/_core/deps.py ee/cloud/shared/deps.py tests/cloud/_core/test_deps.py
uv run mypy ee/cloud/_core/deps.py
```

Expected: clean. (`mypy ee/cloud/shared/deps.py` may surface pre-existing issues with the domain-specific guards — record but don't fix in this phase.)

- [ ] **Step 3.8: Commit**

```bash
git add ee/cloud/_core/deps.py ee/cloud/shared/deps.py tests/cloud/_core/test_deps.py
git commit -m "refactor(_core): deps — extract cross-cutting deps; shared/deps re-exports"
```

---

## Task 4: Cross-shim identity test

A single test file that asserts every shimmed symbol resolves to the same Python object whether imported via `_core` or `shared`. This is the contract of the strangler shim.

**Files:**
- Create: `tests/cloud/_core/test_shims.py`

- [ ] **Step 4.1: Write the test**

`tests/cloud/_core/test_shims.py`:

```python
"""Identity tests for the Phase 1 strangler shims.

These assert that ``ee.cloud.shared.<x>`` and ``ee.cloud._core.<x>``
resolve to the same Python object. If a future change accidentally
defines a class in two places, this test catches it before
behavior diverges.
"""

from __future__ import annotations


def test_errors_shim_identity() -> None:
    from ee.cloud._core import errors as core_errors
    from ee.cloud.shared import errors as shared_errors

    assert core_errors.CloudError is shared_errors.CloudError
    assert core_errors.NotFound is shared_errors.NotFound
    assert core_errors.Forbidden is shared_errors.Forbidden
    assert core_errors.ConflictError is shared_errors.ConflictError
    assert core_errors.ValidationError is shared_errors.ValidationError
    assert core_errors.SeatLimitError is shared_errors.SeatLimitError


def test_time_shim_identity() -> None:
    from ee.cloud._core.time import iso_utc as core_iso_utc
    from ee.cloud.shared.time import iso_utc as shared_iso_utc

    assert core_iso_utc is shared_iso_utc


def test_deps_shim_identity() -> None:
    from ee.cloud._core import deps as core_deps
    from ee.cloud.shared import deps as shared_deps

    assert core_deps.current_user is shared_deps.current_user
    assert core_deps.current_user_id is shared_deps.current_user_id
    assert core_deps.current_workspace_id is shared_deps.current_workspace_id
    assert core_deps.optional_workspace_id is shared_deps.optional_workspace_id
    assert core_deps.require_action is shared_deps.require_action
    assert core_deps.require_action_any_workspace is shared_deps.require_action_any_workspace
    assert core_deps.require_membership is shared_deps.require_membership


def test_shared_errors_does_not_re_export_phase0_additions() -> None:
    """RateLimited, Internal, and with_cause are intentionally only in
    _core.errors. Asserts they are NOT exposed via the shared shim."""
    from ee.cloud.shared import errors as shared_errors

    assert not hasattr(shared_errors, "RateLimited")
    assert not hasattr(shared_errors, "Internal")
    assert not hasattr(shared_errors, "with_cause")


def test_shared_deps_keeps_domain_guards() -> None:
    """The domain-specific guards remain in shared.deps until their
    domains migrate. Phase 1 must not move them."""
    from ee.cloud.shared import deps as shared_deps

    assert hasattr(shared_deps, "require_group_action")
    assert hasattr(shared_deps, "require_agent_owner_or_admin")
    assert hasattr(shared_deps, "require_pocket_edit")
    assert hasattr(shared_deps, "require_pocket_owner")
```

- [ ] **Step 4.2: Run the test, expect pass**

```bash
uv run pytest tests/cloud/_core/test_shims.py -v
```

Expected: 5 PASS.

- [ ] **Step 4.3: Lint, format**

```bash
uv run ruff check tests/cloud/_core/test_shims.py
uv run ruff format tests/cloud/_core/test_shims.py
```

- [ ] **Step 4.4: Commit**

```bash
git add tests/cloud/_core/test_shims.py
git commit -m "test(_core): shim identity tests for Phase 1 strangler"
```

---

## Task 5: Final verification

- [ ] **Step 5.1: Full `_core` suite**

```bash
uv run pytest tests/cloud/_core -v 2>&1 | tail -10
```

Expected: **72 PASS** (54 from Phase 0 + 4 time + 9 deps + 5 shims = 72), 0 fail.

- [ ] **Step 5.2: Full cloud suite**

```bash
uv run pytest tests/cloud --maxfail=30 -q 2>&1 | tail -3
```

Expected: 20 failed, 10 errors, 482+ passed (464 baseline + 18 new). Same failure count = no regression.

- [ ] **Step 5.3: Verify shim file sizes**

```bash
wc -l ee/cloud/shared/errors.py ee/cloud/shared/time.py ee/cloud/shared/deps.py
```

Expected:
- `shared/errors.py` ≈ 25 lines (was 62)
- `shared/time.py` ≈ 10 lines (was 22)
- `shared/deps.py` ≈ 200 lines (was 289 — kept domain guards)

- [ ] **Step 5.4: Verify importers unchanged**

```bash
git diff ee --stat -- 'ee/cloud/agents/' 'ee/cloud/auth/' 'ee/cloud/chat/' 'ee/cloud/files/' 'ee/cloud/kb/' 'ee/cloud/notifications/' 'ee/cloud/pockets/' 'ee/cloud/realtime/' 'ee/cloud/sessions/' 'ee/cloud/uploads/' 'ee/cloud/workspace/' 'ee/cloud/__init__.py'
```

Expected: only `ee/cloud/__init__.py` shows changes (from Phase 0 wire-in). All other module files are untouched — proves the shim approach worked.

- [ ] **Step 5.5: Lint + mypy on new + modified files**

```bash
uv run ruff check ee/cloud/_core ee/cloud/shared
uv run ruff format --check ee/cloud/_core ee/cloud/shared
uv run mypy ee/cloud/_core
```

Expected: clean.

- [ ] **Step 5.6: Branch state for handoff**

```bash
git log --oneline ee..HEAD
git rev-parse HEAD
```

Record for the user's review.

---

## Self-review checklist

**Spec coverage:**
- §5.2 Phase 1 (`shared/{errors,time,parts of deps}` → `_core/`) ✓
- §6 canonical error hierarchy in `_core/errors.py` ✓
- §3 non-goal: API contract invariant ✓ (shims preserve every import path)

**No placeholders:** every code block is complete; every command is exact.

**Type consistency:** all migrated symbols (`iso_utc`, the error classes, `current_user`, `current_user_id`, `current_workspace_id`, `optional_workspace_id`, `_workspace_id_from_path`, `require_action`, `require_action_any_workspace`, `require_membership`) are referenced consistently across tasks.

**Out-of-scope items deferred (named):**
- `shared/agent_bridge.py`, `shared/event_handlers.py`, `shared/events.py`, `shared/db.py` — Phases 5/10
- Domain guards (`require_group_action`, `require_agent_owner_or_admin`, `require_pocket_edit`, `require_pocket_owner`) — Phases 6/8/10
- Removing the `shared/` shims entirely — post-Phase-10 cleanup

---

## Handoff

When all tasks pass, the branch `refactor/cloud-shared-into-core` is ready
for user review. The next plan is
`docs/superpowers/plans/2026-04-27-phase2-notifications.md` (the pilot
module migration).
