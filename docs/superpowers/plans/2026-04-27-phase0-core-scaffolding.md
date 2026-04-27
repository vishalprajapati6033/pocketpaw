# Phase 0: `_core/` Scaffolding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `ee/cloud/_core/` — the cross-cutting framework that the rest of the restructure depends on. No existing module is migrated in this phase; subsequent per-module plans (Phase 1+) will reference these primitives.

**Architecture:** Per the spec at `docs/superpowers/specs/2026-04-27-ee-cloud-restructure-design.md`, `_core/` holds: `RequestContext` + scope enum + FastAPI dep, an extension of the existing `shared/errors.py` `CloudError` hierarchy, a generic `Repository[Domain]` `Protocol`, `Clock`/`IdGenerator` ports with default and fixed (test) implementations, an extracted `cloud_error_handler` + `add_error_handler(app)`, and a lightweight `TimingMiddleware` with an in-memory ring buffer for percentile reports. Phase 0 is purely additive, plus a single small edit to `ee/cloud/__init__.py` to replace the inlined error-handler registration with the extracted version and to mount the timing middleware.

**Tech Stack:** Python 3.11+, FastAPI, Starlette, pydantic, pytest (`asyncio_mode = "auto"`), Beanie/MongoDB (only touched indirectly via existing `User` model in the context dep), `bson` (for `ObjectId` in `IdGenerator`).

---

## Spec sections covered by this plan

- §4.3 `_core/` contents (every sub-bullet)
- §4.1 layer model — only the parts that affect routers receiving `RequestContext` (referenced; full layer model is exercised in Phase 2+)
- §6 Error handling — `cloud_error_handler` extraction, no behavior change

Out of scope (covered by other phase plans):
- §4.2 per-module restructure (Phases 2-10)
- §5 strangler migration steps for any module (Phases 2-10)
- §4.5 chat CQRS (Phase 10)
- Performance optimization itself (Phase 11) — Phase 0 only installs the **measurement** infrastructure.

---

## File Structure

**Create:**
- `ee/cloud/_core/__init__.py`
- `ee/cloud/_core/context.py`
- `ee/cloud/_core/errors.py`
- `ee/cloud/_core/repository.py`
- `ee/cloud/_core/ports.py`
- `ee/cloud/_core/http.py`
- `ee/cloud/_core/timing.py`
- `tests/cloud/_core/__init__.py`
- `tests/cloud/_core/test_context.py`
- `tests/cloud/_core/test_errors.py`
- `tests/cloud/_core/test_repository.py`
- `tests/cloud/_core/test_ports.py`
- `tests/cloud/_core/test_http.py`
- `tests/cloud/_core/test_timing.py`

**Modify:**
- `ee/cloud/__init__.py` — replace inline error-handler registration; add `TimingMiddleware`

**Total lines added (target):** ≈700 (≈400 production, ≈300 test). No production code is deleted in Phase 0; the inline `@app.exception_handler(CloudError)` in `ee/cloud/__init__.py:58-60` is replaced by a single `add_error_handler(app)` call.

---

## Conventions used by every task in this plan

- **Branch:** all tasks happen on `refactor/cloud-core-bootstrap` (branched off `refactor/cloud-restructure`).
- **Commit style:** `feat(_core): <area> — <one-line summary>` for new files; `refactor(ee-cloud): <change>` for the wire-in.
- **Test runner:** `uv run pytest tests/cloud/_core -v` (cloud tests are excluded by default `addopts` in `pyproject.toml:410`; explicit path overrides).
- **Lint/format:** after each task's tests pass, run `uv run ruff check ee/cloud/_core tests/cloud/_core` and `uv run ruff format ee/cloud/_core tests/cloud/_core`.
- **Type check:** `uv run mypy ee/cloud/_core` (does not yet exist as a CI gate, but we run it manually).
- **Async tests:** no `@pytest.mark.asyncio` decorator needed; `asyncio_mode = "auto"` is set in `pyproject.toml`.

---

## Pre-flight — branch + directory setup

- [ ] **Step 1: Create the working branch**

```bash
cd /d/paw/backend
git checkout refactor/cloud-restructure
git checkout -b refactor/cloud-core-bootstrap
```

Expected: `Switched to a new branch 'refactor/cloud-core-bootstrap'`

- [ ] **Step 2: Verify clean working tree and base SHA**

```bash
git status --short
git log --oneline -1
```

Expected: empty `git status` output; `git log` shows `28c5264a docs(ee-cloud): clean-architecture restructure design` as the tip.

---

## Task 1: Create `_core/` package marker

**Files:**
- Create: `ee/cloud/_core/__init__.py`
- Create: `tests/cloud/_core/__init__.py`

- [ ] **Step 1.1: Create the production package marker**

Create `ee/cloud/_core/__init__.py` with:

```python
"""Cross-cutting framework for the cloud module.

Underscore prefix signals that this is internal infrastructure: routers
and services inside `ee/cloud/<module>/` may import from here, but code
outside `ee/cloud/` should not.

See `docs/superpowers/specs/2026-04-27-ee-cloud-restructure-design.md`
for the architectural rationale.
"""
```

- [ ] **Step 1.2: Create the test package marker**

Create `tests/cloud/_core/__init__.py` (empty file — required for pytest to collect tests when `__init__.py` exists in `tests/cloud/`).

```python
```

- [ ] **Step 1.3: Verify imports**

```bash
uv run python -c "import ee.cloud._core"
```

Expected: no output (clean exit). If you see an `ImportError`, the file path or content is wrong.

- [ ] **Step 1.4: Commit**

```bash
git add ee/cloud/_core/__init__.py tests/cloud/_core/__init__.py
git commit -m "feat(_core): bootstrap package marker"
```

---

## Task 2: `_core/errors.py` — extend `CloudError` hierarchy

The existing `ee/cloud/shared/errors.py` defines `CloudError`, `NotFound`, `Forbidden`, `ConflictError`, `ValidationError`, `SeatLimitError`. We re-export those *plus* add `RateLimited (429)`, `Internal (500)`, and a `with_cause` helper.

We do **not** modify `shared/errors.py` in Phase 0. The canonical home moves to `_core/errors.py` in Phase 1.

**Files:**
- Create: `ee/cloud/_core/errors.py`
- Test: `tests/cloud/_core/test_errors.py`

- [ ] **Step 2.1: Write the failing tests**

Create `tests/cloud/_core/test_errors.py`:

```python
"""Tests for ee.cloud._core.errors — Phase 0 additions to the hierarchy."""

from __future__ import annotations

import pytest

from ee.cloud._core import errors as core_errors
from ee.cloud.shared.errors import (
    CloudError as SharedCloudError,
    ConflictError as SharedConflictError,
    Forbidden as SharedForbidden,
    NotFound as SharedNotFound,
    SeatLimitError as SharedSeatLimit,
    ValidationError as SharedValidation,
)


class TestReExports:
    """Re-exporting from shared/errors keeps a single class identity per error."""

    def test_cloud_error_is_same_class(self) -> None:
        assert core_errors.CloudError is SharedCloudError

    def test_not_found_is_same_class(self) -> None:
        assert core_errors.NotFound is SharedNotFound

    def test_forbidden_is_same_class(self) -> None:
        assert core_errors.Forbidden is SharedForbidden

    def test_conflict_is_same_class(self) -> None:
        assert core_errors.ConflictError is SharedConflictError

    def test_validation_is_same_class(self) -> None:
        assert core_errors.ValidationError is SharedValidation

    def test_seat_limit_is_same_class(self) -> None:
        assert core_errors.SeatLimitError is SharedSeatLimit


class TestRateLimited:
    def test_default_message(self) -> None:
        err = core_errors.RateLimited("api.rate_limited")
        assert err.status_code == 429
        assert err.code == "api.rate_limited"
        assert err.message == "Rate limit exceeded"

    def test_custom_message(self) -> None:
        err = core_errors.RateLimited("login.rate_limited", "Too many login attempts")
        assert err.message == "Too many login attempts"

    def test_to_dict(self) -> None:
        err = core_errors.RateLimited("api.rate_limited")
        assert err.to_dict() == {
            "error": {"code": "api.rate_limited", "message": "Rate limit exceeded"}
        }


class TestInternal:
    def test_default(self) -> None:
        err = core_errors.Internal()
        assert err.status_code == 500
        assert err.code == "internal"
        assert err.message == "Internal server error"

    def test_custom_code_and_message(self) -> None:
        err = core_errors.Internal("repo.unavailable", "Datastore offline")
        assert err.code == "repo.unavailable"
        assert err.message == "Datastore offline"


class TestWithCause:
    def test_attaches_cause_and_returns_same_error(self) -> None:
        original = ValueError("bad json")
        err = core_errors.NotFound("workspace", "abc")
        returned = core_errors.with_cause(err, original)
        assert returned is err  # same instance, allows fluent raising
        assert err.__cause__ is original

    def test_raise_with_cause_preserves_cause(self) -> None:
        try:
            try:
                raise ValueError("inner")
            except ValueError as inner:
                raise core_errors.with_cause(core_errors.Internal(), inner)
        except core_errors.CloudError as outer:
            assert isinstance(outer.__cause__, ValueError)
            assert str(outer.__cause__) == "inner"
        else:
            pytest.fail("expected CloudError")
```

- [ ] **Step 2.2: Run tests, verify they fail**

```bash
uv run pytest tests/cloud/_core/test_errors.py -v
```

Expected: collection succeeds, all tests `FAIL` with `ImportError` or `AttributeError` (module not found / `RateLimited` etc. not defined).

- [ ] **Step 2.3: Implement `_core/errors.py`**

Create `ee/cloud/_core/errors.py`:

```python
"""Canonical error hierarchy for ee/cloud.

In Phase 0 this module re-exports the existing classes from
`ee/cloud/shared/errors.py` and adds two missing ones (`RateLimited`,
`Internal`) plus a `with_cause` helper. Phase 1 moves the canonical
definitions here and turns `shared/errors.py` into a re-export shim.

Routers must never raise `HTTPException`; services raise these
`CloudError` subclasses and `_core.http.cloud_error_handler` maps them
to JSON responses.
"""

from __future__ import annotations

from ee.cloud.shared.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
    ValidationError,
)


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

- [ ] **Step 2.4: Run tests, verify they pass**

```bash
uv run pytest tests/cloud/_core/test_errors.py -v
```

Expected: all 13 tests `PASS`.

- [ ] **Step 2.5: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/errors.py tests/cloud/_core/test_errors.py
uv run ruff format ee/cloud/_core/errors.py tests/cloud/_core/test_errors.py
uv run mypy ee/cloud/_core/errors.py
```

Expected: ruff clean; format clean (or auto-applies and rerun is clean); mypy reports `Success: no issues found`.

- [ ] **Step 2.6: Commit**

```bash
git add ee/cloud/_core/errors.py tests/cloud/_core/test_errors.py
git commit -m "feat(_core): errors module — re-export hierarchy + add RateLimited, Internal, with_cause"
```

---

## Task 3: `_core/ports.py` — Clock + IdGenerator ports

**Files:**
- Create: `ee/cloud/_core/ports.py`
- Test: `tests/cloud/_core/test_ports.py`

- [ ] **Step 3.1: Write the failing tests**

Create `tests/cloud/_core/test_ports.py`:

```python
"""Tests for ee.cloud._core.ports — Clock and IdGenerator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ee.cloud._core.ports import (
    Clock,
    FixedClock,
    FixedIdGenerator,
    IdGenerator,
    ObjectIdGenerator,
    SystemClock,
    get_clock,
    get_id_generator,
    set_clock,
    set_id_generator,
)


class TestSystemClock:
    def test_returns_aware_utc_datetime(self) -> None:
        before = datetime.now(timezone.utc)
        now = SystemClock().now()
        after = datetime.now(timezone.utc)
        assert now.tzinfo is not None
        assert before <= now <= after + timedelta(seconds=1)


class TestFixedClock:
    def test_returns_provided_instant(self) -> None:
        instant = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        clock = FixedClock(instant)
        assert clock.now() == instant
        # Stable across calls
        assert clock.now() == instant


class TestObjectIdGenerator:
    def test_returns_valid_object_id(self) -> None:
        from bson import ObjectId

        gen = ObjectIdGenerator()
        new = gen.new_id()
        assert isinstance(new, str)
        assert ObjectId.is_valid(new)

    def test_returns_unique_ids(self) -> None:
        gen = ObjectIdGenerator()
        ids = {gen.new_id() for _ in range(20)}
        assert len(ids) == 20


class TestFixedIdGenerator:
    def test_returns_provided_ids_in_order(self) -> None:
        gen = FixedIdGenerator(["aaa", "bbb", "ccc"])
        assert gen.new_id() == "aaa"
        assert gen.new_id() == "bbb"
        assert gen.new_id() == "ccc"

    def test_raises_when_exhausted(self) -> None:
        gen = FixedIdGenerator(["only"])
        gen.new_id()
        with pytest.raises(IndexError):
            gen.new_id()


class TestProtocolConformance:
    def test_system_clock_is_clock(self) -> None:
        clock: Clock = SystemClock()
        assert callable(clock.now)

    def test_fixed_clock_is_clock(self) -> None:
        clock: Clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert callable(clock.now)

    def test_object_id_generator_is_id_generator(self) -> None:
        gen: IdGenerator = ObjectIdGenerator()
        assert callable(gen.new_id)


class TestGlobalAccessors:
    def test_default_clock_is_system_clock(self) -> None:
        # First test to read the global — capture initial state
        clock = get_clock()
        assert isinstance(clock, SystemClock)

    def test_default_id_generator_is_object_id_generator(self) -> None:
        gen = get_id_generator()
        assert isinstance(gen, ObjectIdGenerator)

    def test_set_clock_overrides_then_restores(self) -> None:
        original = get_clock()
        try:
            fixed = FixedClock(datetime(2030, 6, 1, tzinfo=timezone.utc))
            set_clock(fixed)
            assert get_clock() is fixed
            assert get_clock().now() == fixed.now()
        finally:
            set_clock(original)
        assert get_clock() is original

    def test_set_id_generator_overrides_then_restores(self) -> None:
        original = get_id_generator()
        try:
            fixed = FixedIdGenerator(["X"])
            set_id_generator(fixed)
            assert get_id_generator() is fixed
        finally:
            set_id_generator(original)
        assert get_id_generator() is original
```

- [ ] **Step 3.2: Run tests, verify they fail**

```bash
uv run pytest tests/cloud/_core/test_ports.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'ee.cloud._core.ports'`.

- [ ] **Step 3.3: Implement `_core/ports.py`**

Create `ee/cloud/_core/ports.py`:

```python
"""Cross-cutting infrastructure ports for ee/cloud.

These contracts let domain code (services, repositories) get pure-Python
dependencies (time, IDs) injected without coupling to system clocks or
ObjectId-generation libraries directly. Tests inject `FixedClock` and
`FixedIdGenerator` for determinism.

The `EventBus` port lives in `ee/cloud/realtime/bus.py` for now and
moves to `_core/ports.py` in Phase 5 (`refactor/cloud-realtime`). Phase 0
keeps EventBus where it is to avoid touching realtime callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Time port. Implementations must return timezone-aware datetimes."""

    def now(self) -> datetime: ...


class SystemClock:
    """UTC system clock. Default implementation."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Deterministic clock for tests."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


# ---------------------------------------------------------------------------
# IdGenerator
# ---------------------------------------------------------------------------


@runtime_checkable
class IdGenerator(Protocol):
    """ID factory port. Returns string IDs (typically ObjectId hex)."""

    def new_id(self) -> str: ...


class ObjectIdGenerator:
    """Default implementation backed by `bson.ObjectId`."""

    def new_id(self) -> str:
        from bson import ObjectId

        return str(ObjectId())


class FixedIdGenerator:
    """Deterministic generator for tests. Raises IndexError when exhausted."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = list(ids)

    def new_id(self) -> str:
        if not self._ids:
            raise IndexError("FixedIdGenerator exhausted")
        return self._ids.pop(0)


# ---------------------------------------------------------------------------
# Module-level singletons + accessors
# ---------------------------------------------------------------------------


_clock: Clock = SystemClock()
_id_gen: IdGenerator = ObjectIdGenerator()


def get_clock() -> Clock:
    """Return the current process clock. Use this in service/domain code."""
    return _clock


def set_clock(clock: Clock) -> None:
    """Replace the process clock. Tests use this to inject `FixedClock`."""
    global _clock
    _clock = clock


def get_id_generator() -> IdGenerator:
    """Return the current process ID generator."""
    return _id_gen


def set_id_generator(gen: IdGenerator) -> None:
    """Replace the process ID generator. Tests use this to inject fixed IDs."""
    global _id_gen
    _id_gen = gen


__all__ = [
    "Clock",
    "FixedClock",
    "FixedIdGenerator",
    "IdGenerator",
    "ObjectIdGenerator",
    "SystemClock",
    "get_clock",
    "get_id_generator",
    "set_clock",
    "set_id_generator",
]
```

- [ ] **Step 3.4: Run tests, verify they pass**

```bash
uv run pytest tests/cloud/_core/test_ports.py -v
```

Expected: all 13 tests `PASS`.

- [ ] **Step 3.5: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/ports.py tests/cloud/_core/test_ports.py
uv run ruff format ee/cloud/_core/ports.py tests/cloud/_core/test_ports.py
uv run mypy ee/cloud/_core/ports.py
```

Expected: clean.

- [ ] **Step 3.6: Commit**

```bash
git add ee/cloud/_core/ports.py tests/cloud/_core/test_ports.py
git commit -m "feat(_core): ports — Clock, IdGenerator with default + fixed impls"
```

---

## Task 4: `_core/repository.py` — generic Repository Protocol

**Files:**
- Create: `ee/cloud/_core/repository.py`
- Test: `tests/cloud/_core/test_repository.py`

- [ ] **Step 4.1: Write the failing tests**

Create `tests/cloud/_core/test_repository.py`:

```python
"""Tests for ee.cloud._core.repository — generic Repository Protocol."""

from __future__ import annotations

from dataclasses import dataclass

from ee.cloud._core.repository import Repository


@dataclass
class _Widget:
    id: str
    label: str


class _InMemoryWidgetRepository:
    """A test double that conforms to Repository[_Widget]."""

    def __init__(self) -> None:
        self._items: dict[str, _Widget] = {}

    async def get(self, id: str) -> _Widget | None:
        return self._items.get(id)

    async def list(self) -> list[_Widget]:
        return list(self._items.values())

    async def create(self, entity: _Widget) -> _Widget:
        self._items[entity.id] = entity
        return entity

    async def update(self, entity: _Widget) -> _Widget:
        self._items[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._items.pop(id, None)


async def test_in_memory_repo_satisfies_protocol() -> None:
    """Structural conformance: Repository is a Protocol so duck-typing works."""
    repo: Repository[_Widget] = _InMemoryWidgetRepository()
    w = _Widget(id="a", label="alpha")
    assert await repo.create(w) is w
    assert await repo.get("a") == w
    assert await repo.list() == [w]
    updated = _Widget(id="a", label="ALPHA")
    assert await repo.update(updated) is updated
    assert (await repo.get("a")).label == "ALPHA"
    await repo.delete("a")
    assert await repo.get("a") is None


async def test_repo_get_returns_none_for_missing() -> None:
    repo: Repository[_Widget] = _InMemoryWidgetRepository()
    assert await repo.get("missing") is None


async def test_repo_list_empty() -> None:
    repo: Repository[_Widget] = _InMemoryWidgetRepository()
    assert await repo.list() == []
```

- [ ] **Step 4.2: Run tests, verify they fail**

```bash
uv run pytest tests/cloud/_core/test_repository.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud._core.repository'`.

- [ ] **Step 4.3: Implement `_core/repository.py`**

Create `ee/cloud/_core/repository.py`:

```python
"""Generic repository contract for ee/cloud.

Modules define their own repository Protocols (e.g. `IUserRepository` in
`ee/cloud/auth/repositories.py`) that extend or compose this base. The
core idea: services depend on Protocol types, never on Beanie/Mongo
classes directly. Tests substitute in-memory fakes that conform to the
same Protocol.

Why a Protocol (not an ABC): Python's structural typing means Beanie-
backed and in-memory implementations need not share a base class — they
just need the same method signatures. This avoids inheritance ceremony.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

Domain = TypeVar("Domain")


@runtime_checkable
class Repository(Protocol[Domain]):
    """Generic CRUD contract. Module-specific repositories extend with
    domain-shaped methods (e.g. `find_by_workspace`, `mark_read`).
    """

    async def get(self, id: str) -> Domain | None: ...
    async def list(self) -> list[Domain]: ...
    async def create(self, entity: Domain) -> Domain: ...
    async def update(self, entity: Domain) -> Domain: ...
    async def delete(self, id: str) -> None: ...


__all__ = ["Repository"]
```

- [ ] **Step 4.4: Run tests, verify they pass**

```bash
uv run pytest tests/cloud/_core/test_repository.py -v
```

Expected: all 3 tests `PASS`.

- [ ] **Step 4.5: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/repository.py tests/cloud/_core/test_repository.py
uv run ruff format ee/cloud/_core/repository.py tests/cloud/_core/test_repository.py
uv run mypy ee/cloud/_core/repository.py
```

Expected: clean.

- [ ] **Step 4.6: Commit**

```bash
git add ee/cloud/_core/repository.py tests/cloud/_core/test_repository.py
git commit -m "feat(_core): generic Repository[Domain] Protocol"
```

---

## Task 5: `_core/context.py` — RequestContext + FastAPI dep

**Files:**
- Create: `ee/cloud/_core/context.py`
- Test: `tests/cloud/_core/test_context.py`

- [ ] **Step 5.1: Write the failing tests**

Create `tests/cloud/_core/test_context.py`:

```python
"""Tests for ee.cloud._core.context — RequestContext and FastAPI dependency."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.context import RequestContext, ScopeKind, request_context


class TestScopeKind:
    def test_known_values(self) -> None:
        assert ScopeKind.WORKSPACE.value == "workspace"
        assert ScopeKind.SESSION.value == "session"
        assert ScopeKind.POCKET.value == "pocket"
        assert ScopeKind.GROUP.value == "group"
        assert ScopeKind.DM.value == "dm"
        assert ScopeKind.NONE.value == "none"

    def test_is_str_enum(self) -> None:
        # Behaves as a str so it can be used in path/query templating
        assert ScopeKind.SESSION == "session"


class TestRequestContext:
    def test_construct(self) -> None:
        started = datetime.now(timezone.utc)
        ctx = RequestContext(
            user_id="u1",
            workspace_id="w1",
            request_id="req-abc",
            scope=ScopeKind.WORKSPACE,
            started_at=started,
        )
        assert ctx.user_id == "u1"
        assert ctx.workspace_id == "w1"
        assert ctx.request_id == "req-abc"
        assert ctx.scope is ScopeKind.WORKSPACE
        assert ctx.started_at == started

    def test_is_frozen(self) -> None:
        ctx = RequestContext(
            user_id="u1",
            workspace_id=None,
            request_id="r",
            scope=ScopeKind.NONE,
            started_at=datetime.now(timezone.utc),
        )
        with pytest.raises(FrozenInstanceError):
            ctx.user_id = "u2"  # type: ignore[misc]

    def test_workspace_id_optional(self) -> None:
        ctx = RequestContext(
            user_id="u1",
            workspace_id=None,
            request_id="r",
            scope=ScopeKind.NONE,
            started_at=datetime.now(timezone.utc),
        )
        assert ctx.workspace_id is None


class _FakeUser:
    """Minimal stand-in for ee.cloud.models.user.User."""

    def __init__(self, id: str, active_workspace: str | None) -> None:
        self.id = id
        self.active_workspace = active_workspace


@pytest.fixture
def app_with_context_route() -> FastAPI:
    """Build a tiny FastAPI app that exposes RequestContext via the dep.

    Uses FastAPI's `dependency_overrides` (not monkeypatch) so the
    swap reaches the dependency reference captured by the inner
    `Depends(current_active_user)` inside `request_context`. Patching
    the module attribute would not.
    """
    from ee.cloud.auth import current_active_user

    fake_user = _FakeUser(id="user-1", active_workspace="ws-42")

    async def _fake_current_active_user() -> _FakeUser:
        return fake_user

    app = FastAPI()
    app.dependency_overrides[current_active_user] = _fake_current_active_user

    @app.get("/_test/ctx")
    async def show_ctx(ctx: RequestContext = Depends(request_context)) -> dict:
        return {
            "user_id": ctx.user_id,
            "workspace_id": ctx.workspace_id,
            "request_id": ctx.request_id,
            "scope": ctx.scope.value,
        }

    return app


def test_request_context_dep_populates_user_and_workspace(app_with_context_route: FastAPI) -> None:
    client = TestClient(app_with_context_route)
    resp = client.get("/_test/ctx", headers={"x-request-id": "abc-123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "user-1"
    assert body["workspace_id"] == "ws-42"
    assert body["request_id"] == "abc-123"
    assert body["scope"] == "none"


def test_request_context_dep_generates_request_id_when_header_missing(
    app_with_context_route: FastAPI,
) -> None:
    client = TestClient(app_with_context_route)
    resp = client.get("/_test/ctx")
    assert resp.status_code == 200
    body = resp.json()
    # No header → some 32-hex-char request id was generated
    assert isinstance(body["request_id"], str)
    assert len(body["request_id"]) == 32
```

- [ ] **Step 5.2: Run tests, verify they fail**

```bash
uv run pytest tests/cloud/_core/test_context.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud._core.context'`.

- [ ] **Step 5.3: Implement `_core/context.py`**

Create `ee/cloud/_core/context.py`:

```python
"""RequestContext: typed per-request envelope.

Routers obtain a `RequestContext` via `Depends(request_context)`.
Services accept it as their first positional argument and pass parts of
it to repositories (typically `workspace_id`). This replaces the ad-hoc
`current_user_id` / `current_workspace_id` dependencies in
`shared/deps.py` over the course of the strangler migration; both styles
coexist during the transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Request

from ee.cloud.auth import current_active_user
from ee.cloud.models.user import User


class ScopeKind(str, Enum):
    """The kind of scope a request operates within.

    Inheriting from `str` lets the value be used directly in URL
    templating and JSON serialization without explicit conversion.
    """

    WORKSPACE = "workspace"
    SESSION = "session"
    POCKET = "pocket"
    GROUP = "group"
    DM = "dm"
    NONE = "none"


@dataclass(frozen=True)
class RequestContext:
    """Typed per-request envelope passed router → service → repository.

    Fields:
        user_id: ID of the authenticated user. Never empty in authed routes.
        workspace_id: Active workspace ID. None when the user has no
            active workspace OR the route is workspace-agnostic. Routes that
            REQUIRE a workspace must check explicitly and raise
            `WorkspaceNotFound` (or similar) if missing.
        request_id: For log correlation. Sourced from the `x-request-id`
            header if present, otherwise a fresh hex UUID.
        scope: The scope kind this endpoint operates within. Default
            `NONE`; per-route overrides set this when relevant.
        started_at: Timezone-aware UTC timestamp recorded at dependency
            resolution time. Used by the timing middleware and downstream
            log correlation.
    """

    user_id: str
    workspace_id: str | None
    request_id: str
    scope: ScopeKind
    started_at: datetime


async def request_context(
    request: Request,
    user: Annotated[User, Depends(current_active_user)],
) -> RequestContext:
    """Build a `RequestContext` from the authenticated user.

    Per-route scope overrides are out of scope for Phase 0; consumers
    needing a non-`NONE` scope should construct their own context derived
    from this one until a per-scope dep ships in a later phase.
    """
    request_id = request.headers.get("x-request-id") or uuid4().hex
    return RequestContext(
        user_id=str(user.id),
        workspace_id=user.active_workspace,
        request_id=request_id,
        scope=ScopeKind.NONE,
        started_at=datetime.now(timezone.utc),
    )


__all__ = ["RequestContext", "ScopeKind", "request_context"]
```

- [ ] **Step 5.4: Run tests, verify they pass**

```bash
uv run pytest tests/cloud/_core/test_context.py -v
```

Expected: all 7 tests `PASS`.

- [ ] **Step 5.5: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/context.py tests/cloud/_core/test_context.py
uv run ruff format ee/cloud/_core/context.py tests/cloud/_core/test_context.py
uv run mypy ee/cloud/_core/context.py
```

Expected: clean.

- [ ] **Step 5.6: Commit**

```bash
git add ee/cloud/_core/context.py tests/cloud/_core/test_context.py
git commit -m "feat(_core): RequestContext, ScopeKind, request_context FastAPI dep"
```

---

## Task 6: `_core/http.py` — extract cloud_error_handler + add_error_handler

**Files:**
- Create: `ee/cloud/_core/http.py`
- Test: `tests/cloud/_core/test_http.py`

- [ ] **Step 6.1: Write the failing tests**

Create `tests/cloud/_core/test_http.py`:

```python
"""Tests for ee.cloud._core.http — extracted CloudError exception handler."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.errors import (
    CloudError,
    Forbidden,
    Internal,
    NotFound,
    RateLimited,
)
from ee.cloud._core.http import add_error_handler, cloud_error_handler


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)

    @app.get("/notfound")
    def _nf() -> dict:
        raise NotFound("workspace", "abc")

    @app.get("/forbidden")
    def _fb() -> dict:
        raise Forbidden("auth.denied", "You shall not pass")

    @app.get("/rate")
    def _rl() -> dict:
        raise RateLimited("api.rate_limited")

    @app.get("/internal")
    def _ie() -> dict:
        raise Internal()

    @app.get("/raw_cloud")
    def _raw() -> dict:
        raise CloudError(418, "teapot", "I am a teapot")

    @app.get("/ok")
    def _ok() -> dict:
        return {"ok": True}

    return app


def test_not_found_returns_404_envelope() -> None:
    client = TestClient(_build_app())
    resp = client.get("/notfound")
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {"code": "workspace.not_found", "message": "workspace 'abc' not found"}
    }


def test_forbidden_returns_403_envelope() -> None:
    client = TestClient(_build_app())
    resp = client.get("/forbidden")
    assert resp.status_code == 403
    assert resp.json() == {"error": {"code": "auth.denied", "message": "You shall not pass"}}


def test_rate_limited_returns_429() -> None:
    client = TestClient(_build_app())
    resp = client.get("/rate")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "api.rate_limited"


def test_internal_returns_500_with_generic_envelope() -> None:
    client = TestClient(_build_app())
    resp = client.get("/internal")
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {"code": "internal", "message": "Internal server error"}
    }


def test_arbitrary_cloud_error_uses_provided_status() -> None:
    client = TestClient(_build_app())
    resp = client.get("/raw_cloud")
    assert resp.status_code == 418
    assert resp.json() == {"error": {"code": "teapot", "message": "I am a teapot"}}


def test_non_cloud_routes_unaffected() -> None:
    client = TestClient(_build_app())
    assert client.get("/ok").json() == {"ok": True}


def test_handler_function_is_idempotent_when_added_twice() -> None:
    """add_error_handler may be called more than once during app reload."""
    app = FastAPI()
    add_error_handler(app)
    add_error_handler(app)

    @app.get("/x")
    def _x() -> dict:
        raise NotFound("thing")

    resp = TestClient(app).get("/x")
    assert resp.status_code == 404


async def test_cloud_error_handler_returns_envelope_directly() -> None:
    """Direct unit test of the handler function — no app, no client."""
    from starlette.requests import Request

    err = NotFound("workspace", "abc")
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    response = await cloud_error_handler(Request(scope), err)
    assert response.status_code == 404
    body = bytes(response.body).decode("utf-8")
    assert '"workspace.not_found"' in body
```

- [ ] **Step 6.2: Run tests, verify they fail**

```bash
uv run pytest tests/cloud/_core/test_http.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud._core.http'`.

- [ ] **Step 6.3: Implement `_core/http.py`**

Create `ee/cloud/_core/http.py`:

```python
"""HTTP-layer glue for ee/cloud — exception handler, registration helpers.

In Phase 0 the only function exposed is the `CloudError` → JSON-envelope
mapping that already lives inline at `ee/cloud/__init__.py:58`. We
extract it so it can be imported, unit-tested without booting the full
cloud app, and consistently registered.

Subsequent phases extend this module with: a request-id middleware that
propagates `RequestContext.request_id` from the response back to the
client (so frontends can echo it in bug reports), and any HTTP-shape
helpers that emerge during chat refactor.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ee.cloud._core.errors import CloudError


async def cloud_error_handler(request: Request, exc: CloudError) -> JSONResponse:
    """Map a `CloudError` to its JSON envelope.

    Behaviorally identical to the inline handler currently registered in
    `ee/cloud/__init__.py:mount_cloud`. Phase 0 extracts this so the wire
    behavior is unit-testable without booting the whole cloud app.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


def add_error_handler(app: FastAPI) -> None:
    """Register `cloud_error_handler` on `app`. Safe to call more than once;
    the latter call wins (FastAPI overwrites by exception class)."""
    app.add_exception_handler(CloudError, cloud_error_handler)


__all__ = ["add_error_handler", "cloud_error_handler"]
```

- [ ] **Step 6.4: Run tests, verify they pass**

```bash
uv run pytest tests/cloud/_core/test_http.py -v
```

Expected: all 8 tests `PASS`.

- [ ] **Step 6.5: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/http.py tests/cloud/_core/test_http.py
uv run ruff format ee/cloud/_core/http.py tests/cloud/_core/test_http.py
uv run mypy ee/cloud/_core/http.py
```

Expected: clean.

- [ ] **Step 6.6: Commit**

```bash
git add ee/cloud/_core/http.py tests/cloud/_core/test_http.py
git commit -m "feat(_core): extract cloud_error_handler + add_error_handler(app)"
```

---

## Task 7: `_core/timing.py` — request-timing middleware + percentile report

**Files:**
- Create: `ee/cloud/_core/timing.py`
- Test: `tests/cloud/_core/test_timing.py`

- [ ] **Step 7.1: Write the failing tests**

Create `tests/cloud/_core/test_timing.py`:

```python
"""Tests for ee.cloud._core.timing — request-timing middleware + percentiles."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.timing import (
    TimingMiddleware,
    percentiles,
    report,
    reset_buffers,
    snapshot,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_buffers()
    yield
    reset_buffers()


def test_percentiles_empty_returns_zero_for_each() -> None:
    assert percentiles([]) == {0.5: 0.0, 0.95: 0.0, 0.99: 0.0}


def test_percentiles_single_sample() -> None:
    pcts = percentiles([42.0])
    assert pcts == {0.5: 42.0, 0.95: 42.0, 0.99: 42.0}


def test_percentiles_sorted_correctly() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    pcts = percentiles(samples, qs=(0.5, 0.9, 1.0))
    assert pcts[0.5] == pytest.approx(5.0, abs=0.5)
    assert pcts[0.9] == pytest.approx(9.0, abs=0.5)
    assert pcts[1.0] == 10.0


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TimingMiddleware)

    @app.get("/fast")
    def _fast() -> dict:
        return {"ok": True}

    @app.get("/slow")
    def _slow() -> dict:
        time.sleep(0.005)
        return {"ok": True}

    return app


def test_middleware_records_durations_per_endpoint() -> None:
    client = TestClient(_build_app())
    for _ in range(3):
        client.get("/fast")
    client.get("/slow")

    snap = snapshot()
    fast_key = ("GET", "/fast")
    slow_key = ("GET", "/slow")

    assert fast_key in snap
    assert slow_key in snap
    assert len(snap[fast_key]) == 3
    assert len(snap[slow_key]) == 1

    # Slow endpoint should be at least ~5ms; allow some scheduler slack
    assert snap[slow_key][0] >= 4.0


def test_reset_buffers_clears_state() -> None:
    client = TestClient(_build_app())
    client.get("/fast")
    assert snapshot()
    reset_buffers()
    assert snapshot() == {}


def test_ring_buffer_caps_at_capacity() -> None:
    app = FastAPI()
    # Tiny capacity for the test
    app.add_middleware(TimingMiddleware, capacity=5)

    @app.get("/x")
    def _x() -> dict:
        return {"ok": True}

    client = TestClient(app)
    for _ in range(20):
        client.get("/x")

    snap = snapshot()
    assert len(snap[("GET", "/x")]) == 5


def test_report_includes_collected_endpoints() -> None:
    client = TestClient(_build_app())
    client.get("/fast")
    out = report()
    assert "GET" in out
    assert "/fast" in out
    assert "p50" in out and "p95" in out and "p99" in out


def test_report_empty_returns_header_only() -> None:
    out = report()
    # Header line still printed; no data rows
    assert "p50" in out
    # Only one line (the header) when there's no data
    assert len(out.splitlines()) == 1
```

- [ ] **Step 7.2: Run tests, verify they fail**

```bash
uv run pytest tests/cloud/_core/test_timing.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud._core.timing'`.

- [ ] **Step 7.3: Implement `_core/timing.py`**

Create `ee/cloud/_core/timing.py`:

```python
"""Lightweight request-timing middleware for ee/cloud.

Records `(method, path) → duration_ms` samples in an in-memory ring
buffer. A `report()` function dumps p50/p95/p99 on demand. Deliberately
not a metrics system — no Prometheus, no histograms, no exporters —
because the goal is just to have *some* data when the perf phase
begins. Phase 11 may swap in a real metrics stack; until then this
keeps the slice small.

Capacity defaults to 10k samples per endpoint. With the default
capacity the buffer uses ~80 KB per distinct endpoint at steady state
(two `float` per sample is conservative).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Final

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_DEFAULT_CAPACITY: Final = 10_000
_buffers: dict[tuple[str, str], deque[float]] = {}


class TimingMiddleware(BaseHTTPMiddleware):
    """Records request duration per `(method, path)`."""

    def __init__(self, app: FastAPI, capacity: int = _DEFAULT_CAPACITY) -> None:
        super().__init__(app)
        self.capacity = capacity

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0
        # Prefer the matched route template (e.g. /workspaces/{id}) so we
        # don't get one buffer per id; fall back to the raw URL path.
        scope_route = request.scope.get("route")
        path = (
            scope_route.path
            if scope_route is not None and hasattr(scope_route, "path")
            else request.url.path
        )
        key = (request.method, path)
        buf = _buffers.get(key)
        if buf is None:
            buf = deque(maxlen=self.capacity)
            _buffers[key] = buf
        buf.append(duration_ms)
        return response


def reset_buffers() -> None:
    """Clear all collected timings (for tests)."""
    _buffers.clear()


def snapshot() -> dict[tuple[str, str], list[float]]:
    """Return a copy of current buffers as plain lists.

    Copies are O(n) per endpoint; cheap for the default capacity but
    don't call this in a hot path.
    """
    return {key: list(buf) for key, buf in _buffers.items()}


def percentiles(
    samples: list[float],
    qs: tuple[float, ...] = (0.5, 0.95, 0.99),
) -> dict[float, float]:
    """Compute the requested percentiles from `samples`. Linear sort is
    fine for the default capacity (~10k items)."""
    if not samples:
        return {q: 0.0 for q in qs}
    sorted_samples = sorted(samples)
    n = len(sorted_samples)
    out: dict[float, float] = {}
    for q in qs:
        idx = min(n - 1, max(0, int(round(q * (n - 1)))))
        out[q] = sorted_samples[idx]
    return out


def report() -> str:
    """Format current snapshot as a human-readable table."""
    snap = snapshot()
    header = (
        f"{'METHOD':<7} {'PATH':<60} {'COUNT':>6} "
        f"{'p50':>10} {'p95':>10} {'p99':>10}"
    )
    lines = [header]
    for (method, path), samples in sorted(snap.items()):
        pcts = percentiles(samples)
        lines.append(
            f"{method:<7} {path:<60} {len(samples):>6} "
            f"{pcts[0.5]:>8.2f}ms {pcts[0.95]:>8.2f}ms {pcts[0.99]:>8.2f}ms"
        )
    return "\n".join(lines)


__all__ = [
    "TimingMiddleware",
    "percentiles",
    "report",
    "reset_buffers",
    "snapshot",
]
```

- [ ] **Step 7.4: Run tests, verify they pass**

```bash
uv run pytest tests/cloud/_core/test_timing.py -v
```

Expected: all 8 tests `PASS`. The slow-endpoint test allows ≥4ms (vs target 5ms) for scheduler slack; if it flakes on the developer's machine, raise the assertion threshold rather than the actual sleep duration.

- [ ] **Step 7.5: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/_core/timing.py tests/cloud/_core/test_timing.py
uv run ruff format ee/cloud/_core/timing.py tests/cloud/_core/test_timing.py
uv run mypy ee/cloud/_core/timing.py
```

Expected: clean.

- [ ] **Step 7.6: Commit**

```bash
git add ee/cloud/_core/timing.py tests/cloud/_core/test_timing.py
git commit -m "feat(_core): TimingMiddleware + ring-buffer percentile report"
```

---

## Task 8: Wire `_core` into `ee/cloud/__init__.py`

This is the **only** file modified in Phase 0. Two changes:
1. Replace the inlined `@app.exception_handler(CloudError)` (currently `ee/cloud/__init__.py:58-60`) with a single call to `add_error_handler(app)`.
2. Add `app.add_middleware(TimingMiddleware)` near the top of `mount_cloud` so every cloud route is timed from Phase 0 onward.

API behavior is unchanged.

**Files:**
- Modify: `ee/cloud/__init__.py:54-77` (the `mount_cloud` function header)
- Test: `tests/cloud/_core/test_wire_in.py` (new)

- [ ] **Step 8.1: Write the failing wire-in regression test**

Create `tests/cloud/_core/test_wire_in.py`:

```python
"""Smoke test that `mount_cloud` registers the extracted handler and
the timing middleware. This is the only Phase 0 test that touches the
real cloud app, so it doubles as a regression guard for the wire-in.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from ee.cloud import mount_cloud
from ee.cloud._core.errors import CloudError
from ee.cloud._core.http import cloud_error_handler
from ee.cloud._core.timing import TimingMiddleware


def test_mount_cloud_registers_cloud_error_handler() -> None:
    app = FastAPI()
    mount_cloud(app)
    assert app.exception_handlers.get(CloudError) is cloud_error_handler


def test_mount_cloud_installs_timing_middleware() -> None:
    app = FastAPI()
    mount_cloud(app)
    # Starlette wraps the user_middleware list as `Middleware` records;
    # asserting any of them is `TimingMiddleware` covers the install.
    assert any(m.cls is TimingMiddleware for m in app.user_middleware)
```

- [ ] **Step 8.2: Run the test, verify it fails**

```bash
uv run pytest tests/cloud/_core/test_wire_in.py -v
```

Expected first assertion fails because the existing handler is an
inline closure, not the `cloud_error_handler` reference. The middleware
assertion fails because no `TimingMiddleware` is installed yet.

- [ ] **Step 8.3: Read the relevant section of `ee/cloud/__init__.py`**

```bash
sed -n '54,77p' ee/cloud/__init__.py
```

(Verify it matches the snippet quoted in §6 of the spec — line numbers
may have shifted if the file was edited since this plan was written.)

- [ ] **Step 8.4: Edit `ee/cloud/__init__.py`**

Replace the current `mount_cloud` opening (the function declaration through the end of the existing inline handler — currently lines 54-60, ending immediately before `# Import and mount domain routers`):

**Before:**

```python
def mount_cloud(app: FastAPI) -> None:
    """Mount all cloud domain routers and the error handler."""

    # Global error handler
    @app.exception_handler(CloudError)
    async def cloud_error_handler(request: Request, exc: CloudError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    # Import and mount domain routers
```

**After:**

```python
def mount_cloud(app: FastAPI) -> None:
    """Mount all cloud domain routers, the error handler, and the
    request-timing middleware."""
    from ee.cloud._core.http import add_error_handler
    from ee.cloud._core.timing import TimingMiddleware

    # Request-timing middleware first so it wraps every subsequent route
    app.add_middleware(TimingMiddleware)

    # Global error handler — extracted to ee.cloud._core.http
    add_error_handler(app)

    # Import and mount domain routers
```

Also remove the now-unused top-level imports of `Request` and
`JSONResponse` from `ee/cloud/__init__.py` **only if** they aren't used
elsewhere in the file. Check first:

```bash
uv run python -c "
import re, pathlib
src = pathlib.Path('ee/cloud/__init__.py').read_text()
print('Request usages:', len(re.findall(r'\\bRequest\\b', src)))
print('JSONResponse usages:', len(re.findall(r'\\bJSONResponse\\b', src)))
"
```

If `Request` count is 1 (just the import) and `JSONResponse` count is 1 (just the import), remove those imports. Otherwise, leave them alone.

- [ ] **Step 8.5: Run the wire-in test, verify it passes**

```bash
uv run pytest tests/cloud/_core/test_wire_in.py -v
```

Expected: both tests PASS.

- [ ] **Step 8.6: Run the full `_core` test suite**

```bash
uv run pytest tests/cloud/_core -v
```

Expected: 50+ tests PASS, 0 failures.

- [ ] **Step 8.7: Run a smoke pass on the broader cloud tests**

```bash
uv run pytest tests/cloud -v --maxfail=10
```

Expected: no NEW failures attributable to Phase 0. Pre-existing failures (the spec mentions a baseline of 38) remain unchanged. If anything new breaks, it's almost certainly because `Request` / `JSONResponse` were imported elsewhere in `ee/cloud/__init__.py` and Step 8.4's removal was over-eager — re-add the imports.

- [ ] **Step 8.8: Lint, format, type-check**

```bash
uv run ruff check ee/cloud/__init__.py tests/cloud/_core/test_wire_in.py
uv run ruff format ee/cloud/__init__.py tests/cloud/_core/test_wire_in.py
uv run mypy ee/cloud/__init__.py
```

Expected: clean.

- [ ] **Step 8.9: Commit**

```bash
git add ee/cloud/__init__.py tests/cloud/_core/test_wire_in.py
git commit -m "refactor(ee-cloud): use _core.http.add_error_handler + install TimingMiddleware

Replaces the inline @app.exception_handler(CloudError) closure with the
extracted cloud_error_handler. Adds TimingMiddleware so the perf phase
has data to work from. No API behavior change."
```

---

## Task 9: Final verification — entire cloud test suite

- [ ] **Step 9.1: Run the entire cloud test suite**

```bash
uv run pytest tests/cloud -v
```

Expected: same number of failures as the Phase 0 baseline (record this number; spec mentions ~38). Zero NEW failures.

- [ ] **Step 9.2: Run the broader test suite (excluding e2e)**

```bash
uv run pytest --ignore=tests/e2e -v --maxfail=20
```

Expected: same baseline. Phase 0 changes only the cloud app bootstrap; non-cloud tests should be unaffected.

- [ ] **Step 9.3: Run the full lint + type-check on `_core`**

```bash
uv run ruff check ee/cloud/_core
uv run ruff format --check ee/cloud/_core
uv run mypy ee/cloud/_core
```

Expected: all three clean.

- [ ] **Step 9.4: Verify the package shape**

```bash
ls -1 ee/cloud/_core/
```

Expected output (alphabetical):
```
__init__.py
context.py
errors.py
http.py
ports.py
repository.py
timing.py
```

- [ ] **Step 9.5: Push the branch**

> **Hold for user approval before pushing.** The user is AFK during initial
> implementation; pushing should only happen after they review the
> committed work locally. Note the branch name and the tip SHA for them.

```bash
git log --oneline ee..HEAD
git rev-parse HEAD
```

Record the output for the handoff message.

---

## Self-review checklist (run after writing all tasks above)

**Spec coverage:**
- §4.3 `_core/__init__.py` — Task 1 ✓
- §4.3 `_core/context.py` (RequestContext + dep) — Task 5 ✓
- §4.3 `_core/errors.py` (extend hierarchy + with_cause) — Task 2 ✓
- §4.3 `_core/repository.py` (generic Protocol) — Task 4 ✓
- §4.3 `_core/ports.py` (Clock, IdGenerator; EventBus deferred to Phase 5 by spec) — Task 3 ✓
- §4.3 `_core/http.py` (extracted handler + add_error_handler) — Task 6 ✓
- §4.3 `_core/timing.py` (middleware + percentile report) — Task 7 ✓
- §6 wire-in (replace inline handler + install middleware) — Task 8 ✓
- §7 testing (≥80% coverage on new code via unit tests; no router-level golden tests because Phase 0 adds no new endpoints) — covered by Tasks 2-8 ✓

**No placeholders:** confirmed; every code block is complete, every command is exact, no "TODO" or "TBD" remains.

**Type consistency:** `RequestContext`, `ScopeKind`, `Repository[Domain]`, `Clock`, `IdGenerator`, `cloud_error_handler`, `add_error_handler`, `TimingMiddleware`, `reset_buffers`, `snapshot`, `percentiles`, `report` — all referenced consistently across tasks.

**Out-of-scope items deferred to later phases (named, not silently dropped):**
- `EventBus` port — Phase 5 (`refactor/cloud-realtime`)
- `with_cause` becoming a method on `CloudError` — Phase 1 (`shared/errors.py` consolidation)
- request-id propagation in response headers — later (Phase 1+ if needed)
- `models/` rename to `_models/` — post-Phase-10 cleanup

---

## Handoff

When all tasks pass, the branch `refactor/cloud-core-bootstrap` is ready
for user review. The next plan in this series will be
`docs/superpowers/plans/2026-04-27-phase1-shared-into-core.md` once
Phase 0 is reviewed and approved.
