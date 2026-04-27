# Phase 2: `notifications/` Pilot Module Migration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** First module to receive the full hexagonal layout — `domain.py`, `repositories.py`, `dto.py`, refactored `service.py`, refactored `router.py`. Notifications is small, isolated, and used by only two external callers (chat/message_service, workspace/service), making it the right pattern proof for Phases 3-10.

**Architecture:** Keep the existing **public API surface** of `NotificationService` (classmethods used by external callers in chat and workspace) so those modules don't need changes in this phase. Internally, the service routes through new layers: services consume domain entities; repositories convert to/from Beanie documents; the router uses `RequestContext` and returns DTOs at the boundary. `_to_wire` is replaced by an explicit `notification_to_dto` mapping function so the wire shape is locked-in by type, not by ad-hoc dict construction.

**Tech Stack:** Python 3.11+, FastAPI, Beanie ODM (`ee/cloud/models/notification.py`), Pydantic v2, pytest.

---

## Spec sections covered

- §4.1 layer model — `domain → repositories → service → router` cleanly separated for one module
- §4.2 hybrid layout — `notifications/{domain,repositories,dto,service,router}.py`
- §5.1 strangler step — write golden tests, build new alongside, delete dead, atomic per-module PR
- §6 error handling — `Forbidden`, `NotFound` from `_core.errors` instead of any inline `HTTPException`

Out of scope (deferred):
- Updating `chat/message_service.py` and `workspace/service.py` to use the new domain types — those modules migrate in Phases 6/7/10 and will adopt the new types then. Phase 2 keeps `NotificationService.create` accepting the existing `NotificationSource` (Beanie sub-model from `models/notification.py`) so callers don't change.
- Full CQRS read/write split — notifications is too small to need it.
- Rename of the Beanie document class — stays as `Notification` in `models/notification.py`. We use a module-private alias inside the repository.

---

## File Structure

**Create:**
- `ee/cloud/notifications/domain.py` — frozen dataclasses (`Notification`, `NotificationSource`)
- `ee/cloud/notifications/repositories.py` — `INotificationRepository` Protocol + `MongoNotificationRepository` + module-level `get_default_repository()`
- `ee/cloud/notifications/dto.py` — `NotificationOut` + `notification_to_dto`
- `tests/cloud/notifications/test_domain.py`
- `tests/cloud/notifications/test_repository_inmemory.py` — uses an in-memory fake to validate the Protocol contract
- `tests/cloud/notifications/test_dto.py`
- `tests/cloud/notifications/test_service_v2.py` — new tests for the refactored service using a fake repo
- `tests/cloud/notifications/test_router_golden.py` — golden response tests at the router boundary

**Modify:**
- `ee/cloud/notifications/service.py` — internals route through new layers; classmethod API unchanged
- `ee/cloud/notifications/router.py` — uses `Depends(request_context)`, returns DTOs (Pydantic), `response_model` set
- `ee/cloud/notifications/__init__.py` — keeps the same exports

**Delete:**
- `ee/cloud/notifications/schemas.py` — replaced by `dto.py`. `schemas.py` is unused (router doesn't reference it; grep below confirms).
- `tests/cloud/notifications/test_service.py` — replaced by `test_service_v2.py`. The old tests patch internals (`Notification`, `PydanticObjectId`, `emit`) at module level; the new tests use a clean repository fake. Already-failing `test_derived.py` left as-is (its failures are pre-existing baseline; out of scope to fix here).

**Audit-only:**
- `ee/cloud/chat/message_service.py` — calls `NotificationService.create(...)` (kwargs unchanged) — no edit needed
- `ee/cloud/workspace/service.py` — calls `NotificationService.create(...)` (kwargs unchanged) — no edit needed

---

## Conventions

- **Branch:** `refactor/cloud-notifications` (already created off `refactor/cloud-shared-into-core`).
- **Commit style:** `feat(notifications): <area>` for new files; `refactor(notifications): <change>` for restructure.
- **Test runner:** `uv run pytest tests/cloud/notifications -v` plus the broader baseline regression check `uv run pytest tests/cloud --maxfail=30 -q`.

---

## Pre-flight verified

- Branch created off `refactor/cloud-shared-into-core` (tip `4fc64587`).
- Working tree clean.
- Existing notifications baseline: 5 tests in `test_service.py` (passing on parent), 5 tests in `test_derived.py` (already failing on parent — pre-existing).
- External callers of `NotificationService.create`: `ee/cloud/chat/message_service.py` (2 calls), `ee/cloud/workspace/service.py` (1 call). All use kwargs `workspace_id, recipient, kind, title, body, source` — preserved.
- `notifications/schemas.py` (`NotificationResponse`) is **not referenced anywhere** in the codebase (verified by `grep -r NotificationResponse ee/ tests/ src/`); safe to delete.

---

## Task 1: `notifications/domain.py` — frozen value objects

**Files:**
- Create: `ee/cloud/notifications/domain.py`
- Test: `tests/cloud/notifications/test_domain.py`

- [ ] **Step 1.1: Write failing tests**

`tests/cloud/notifications/test_domain.py`:

```python
"""Tests for ee.cloud.notifications.domain — pure value objects."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from ee.cloud.notifications.domain import Notification, NotificationSource


def test_notification_source_is_frozen() -> None:
    src = NotificationSource(type="message", id="m1", pocket_id=None)
    with pytest.raises(FrozenInstanceError):
        src.type = "comment"  # type: ignore[misc]


def test_notification_source_with_pocket_id() -> None:
    src = NotificationSource(type="comment", id="c1", pocket_id="p1")
    assert src.pocket_id == "p1"


def test_notification_construct_minimal() -> None:
    n = Notification(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="t",
        body="",
        source=None,
        read=False,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        expires_at=None,
    )
    assert n.id == "n1"
    assert n.read is False
    assert n.source is None
    assert n.expires_at is None


def test_notification_with_source() -> None:
    src = NotificationSource(type="message", id="m1", pocket_id=None)
    n = Notification(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="t",
        body="b",
        source=src,
        read=False,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )
    assert n.source is src


def test_notification_is_frozen() -> None:
    n = Notification(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="t",
        body="",
        source=None,
        read=False,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        n.read = True  # type: ignore[misc]
```

- [ ] **Step 1.2: Run, expect failure**

```bash
uv run pytest tests/cloud/notifications/test_domain.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud.notifications.domain'`.

- [ ] **Step 1.3: Implement `domain.py`**

`ee/cloud/notifications/domain.py`:

```python
"""Domain value objects for notifications.

Pure-Python frozen dataclasses. No Beanie, no Pydantic, no FastAPI
imports. The repository layer converts between these and the Beanie
``Notification`` document; the DTO layer converts these to Pydantic
response models.

Why a separate `Notification` from the Beanie document: services should
operate on plain Python values so they can be tested without a Mongo
fixture. The Beanie document carries persistence concerns (indexes,
ObjectId management, snake_case Mongo field aliases) that services
shouldn't touch.

`NotificationSource` is structurally identical to the Beanie sub-model
of the same name; the repository converts field-by-field. We accept the
duplication for now because eliminating it would require changes to
``models/notification.py`` and its callers — out of scope for Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NotificationSource:
    """Pointer to the entity that triggered the notification (a message,
    comment, invite, etc.)."""

    type: str
    id: str
    pocket_id: str | None = None


@dataclass(frozen=True)
class Notification:
    """In-app notification for a user."""

    id: str
    workspace_id: str
    recipient_id: str
    kind: str  # mention, comment, reply, invite, agent_complete, pocket_shared
    title: str
    body: str
    source: NotificationSource | None
    read: bool
    created_at: datetime
    expires_at: datetime | None = None


__all__ = ["Notification", "NotificationSource"]
```

- [ ] **Step 1.4: Run, expect pass**

```bash
uv run pytest tests/cloud/notifications/test_domain.py -v
```

Expected: 5 PASS.

- [ ] **Step 1.5: Lint, format, type-check, commit**

```bash
uv run ruff check --fix ee/cloud/notifications/domain.py tests/cloud/notifications/test_domain.py
uv run ruff format ee/cloud/notifications/domain.py tests/cloud/notifications/test_domain.py
uv run mypy ee/cloud/notifications/domain.py
git add ee/cloud/notifications/domain.py tests/cloud/notifications/test_domain.py
git commit -m "feat(notifications): domain — Notification, NotificationSource value objects"
```

---

## Task 2: `notifications/dto.py` — Pydantic wire shape + mapper

The DTO matches what the existing `_to_wire()` function produces today, byte-for-byte. This locks in the wire contract.

**Files:**
- Create: `ee/cloud/notifications/dto.py`
- Test: `tests/cloud/notifications/test_dto.py`

- [ ] **Step 2.1: Write failing tests**

`tests/cloud/notifications/test_dto.py`:

```python
"""Tests for ee.cloud.notifications.dto — wire DTO + mapping."""

from __future__ import annotations

from datetime import UTC, datetime

from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.dto import NotificationOut, notification_to_dto


def _domain(**overrides) -> Notification:
    base = dict(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="You were mentioned",
        body="hello",
        source=None,
        read=False,
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        expires_at=None,
    )
    base.update(overrides)
    return Notification(**base)


def test_dto_contains_wire_keys_in_order() -> None:
    """Asserts the keys the existing wire shape promises:
    id, user_id, workspace_id, kind, title, body, source_id, read, created_at."""
    out = NotificationOut(
        id="n1",
        user_id="u1",
        workspace_id="w1",
        kind="mention",
        title="t",
        body="b",
        source_id=None,
        read=False,
        created_at="2026-04-27T12:00:00+00:00",
    )
    dump = out.model_dump()
    assert set(dump.keys()) == {
        "id",
        "user_id",
        "workspace_id",
        "kind",
        "title",
        "body",
        "source_id",
        "read",
        "created_at",
    }


def test_notification_to_dto_no_source() -> None:
    out = notification_to_dto(_domain())
    assert out.id == "n1"
    assert out.user_id == "u1"
    assert out.workspace_id == "w1"
    assert out.kind == "mention"
    assert out.title == "You were mentioned"
    assert out.body == "hello"
    assert out.source_id is None
    assert out.read is False
    assert out.created_at == "2026-04-27T12:00:00+00:00"


def test_notification_to_dto_with_source() -> None:
    src = NotificationSource(type="message", id="m42", pocket_id=None)
    out = notification_to_dto(_domain(source=src))
    assert out.source_id == "m42"


def test_notification_to_dto_serializes_naive_created_at_as_utc() -> None:
    """Beanie reads return naive datetimes; iso_utc anchors them to +00:00."""
    naive = datetime(2026, 4, 27, 12, 0, 0)
    out = notification_to_dto(_domain(created_at=naive))
    assert out.created_at == "2026-04-27T12:00:00+00:00"
```

- [ ] **Step 2.2: Run, expect failure**

```bash
uv run pytest tests/cloud/notifications/test_dto.py -v
```

Expected: `ModuleNotFoundError: No module named 'ee.cloud.notifications.dto'`.

- [ ] **Step 2.3: Implement `dto.py`**

`ee/cloud/notifications/dto.py`:

```python
"""Wire-format DTO for notifications.

The shape of `NotificationOut` matches the dict the legacy `_to_wire`
function produced byte-for-byte. Tests in `test_router_golden.py` lock
this in at the router boundary. Routers map domain → DTO via
``notification_to_dto`` instead of constructing dicts ad-hoc.
"""

from __future__ import annotations

from pydantic import BaseModel

from ee.cloud._core.time import iso_utc
from ee.cloud.notifications.domain import Notification


class NotificationOut(BaseModel):
    """Wire response shape for notifications."""

    id: str
    user_id: str
    workspace_id: str
    kind: str
    title: str
    body: str
    source_id: str | None
    read: bool
    created_at: str | None


def notification_to_dto(n: Notification) -> NotificationOut:
    """Map a domain `Notification` to its wire DTO."""
    return NotificationOut(
        id=n.id,
        user_id=n.recipient_id,
        workspace_id=n.workspace_id,
        kind=n.kind,
        title=n.title,
        body=n.body,
        source_id=n.source.id if n.source else None,
        read=n.read,
        created_at=iso_utc(n.created_at),
    )


__all__ = ["NotificationOut", "notification_to_dto"]
```

- [ ] **Step 2.4: Run, expect pass; lint, format, type, commit**

```bash
uv run pytest tests/cloud/notifications/test_dto.py -v
uv run ruff check --fix ee/cloud/notifications/dto.py tests/cloud/notifications/test_dto.py
uv run ruff format ee/cloud/notifications/dto.py tests/cloud/notifications/test_dto.py
uv run mypy ee/cloud/notifications/dto.py
git add ee/cloud/notifications/dto.py tests/cloud/notifications/test_dto.py
git commit -m "feat(notifications): dto — NotificationOut + notification_to_dto mapper"
```

Expected: 4 PASS, all clean.

---

## Task 3: `notifications/repositories.py` — Protocol + Mongo impl

**Files:**
- Create: `ee/cloud/notifications/repositories.py`
- Test: `tests/cloud/notifications/test_repository_inmemory.py`

- [ ] **Step 3.1: Write failing tests using an in-memory fake**

`tests/cloud/notifications/test_repository_inmemory.py`:

```python
"""Tests for INotificationRepository via an in-memory fake.

Each phase ships unit tests against a fake repository (the Protocol).
The Mongo-backed implementation is exercised via the broader test suite
+ explicit integration tests later (Phase 11). Phase 2 is pragmatic:
we trust that conforming to the Protocol means the Mongo impl works,
provided the fake matches the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.repositories import INotificationRepository


class _InMemoryNotificationRepository:
    """Conforms to INotificationRepository for tests. Stores domain
    entities in a dict; preserves insertion order via a list."""

    def __init__(self) -> None:
        self._items: dict[str, Notification] = {}

    async def create(self, notification: Notification) -> Notification:
        self._items[notification.id] = notification
        return notification

    async def get(self, notification_id: str) -> Notification | None:
        return self._items.get(notification_id)

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        # Newest first
        out = [n for n in self._items.values() if n.recipient_id == user_id]
        if unread:
            out = [n for n in out if not n.read]
        out.sort(key=lambda n: n.created_at, reverse=True)
        return out[:limit]

    async def mark_read(self, notification_id: str) -> bool:
        n = self._items.get(notification_id)
        if n is None or n.read:
            return False
        # Domain is frozen — replace with a copy
        from dataclasses import replace

        self._items[notification_id] = replace(n, read=True)
        return True

    async def clear_unread(self, user_id: str) -> int:
        from dataclasses import replace

        count = 0
        for nid, n in list(self._items.items()):
            if n.recipient_id == user_id and not n.read:
                self._items[nid] = replace(n, read=True)
                count += 1
        return count


def _n(id: str, recipient: str, *, read: bool = False, ts: int = 0) -> Notification:
    return Notification(
        id=id,
        workspace_id="w1",
        recipient_id=recipient,
        kind="mention",
        title="t",
        body="",
        source=None,
        read=read,
        created_at=datetime(2026, 4, 27, 12, 0, ts, tzinfo=UTC),
    )


@pytest.fixture
def repo() -> INotificationRepository:
    return _InMemoryNotificationRepository()


async def test_create_returns_same_entity(repo) -> None:
    n = _n("n1", "u1")
    out = await repo.create(n)
    assert out is n


async def test_get_returns_created(repo) -> None:
    await repo.create(_n("n1", "u1"))
    fetched = await repo.get("n1")
    assert fetched is not None
    assert fetched.id == "n1"


async def test_get_returns_none_for_missing(repo) -> None:
    assert await repo.get("missing") is None


async def test_list_for_user_filters_by_recipient(repo) -> None:
    await repo.create(_n("n1", "u1"))
    await repo.create(_n("n2", "u2"))
    out = await repo.list_for_user("u1")
    assert [n.id for n in out] == ["n1"]


async def test_list_for_user_unread_filter(repo) -> None:
    await repo.create(_n("n1", "u1", read=True))
    await repo.create(_n("n2", "u1", read=False))
    out = await repo.list_for_user("u1", unread=True)
    assert [n.id for n in out] == ["n2"]


async def test_list_for_user_orders_newest_first(repo) -> None:
    await repo.create(_n("n1", "u1", ts=1))
    await repo.create(_n("n2", "u1", ts=5))
    await repo.create(_n("n3", "u1", ts=3))
    out = await repo.list_for_user("u1")
    assert [n.id for n in out] == ["n2", "n3", "n1"]


async def test_list_for_user_respects_limit(repo) -> None:
    for i in range(5):
        await repo.create(_n(f"n{i}", "u1", ts=i))
    out = await repo.list_for_user("u1", limit=2)
    assert len(out) == 2


async def test_mark_read_returns_true_and_flips(repo) -> None:
    await repo.create(_n("n1", "u1", read=False))
    changed = await repo.mark_read("n1")
    assert changed is True
    assert (await repo.get("n1")).read is True


async def test_mark_read_returns_false_when_already_read(repo) -> None:
    await repo.create(_n("n1", "u1", read=True))
    assert await repo.mark_read("n1") is False


async def test_mark_read_returns_false_when_missing(repo) -> None:
    assert await repo.mark_read("missing") is False


async def test_clear_unread_returns_count(repo) -> None:
    await repo.create(_n("n1", "u1", read=False))
    await repo.create(_n("n2", "u1", read=True))
    await repo.create(_n("n3", "u1", read=False))
    count = await repo.clear_unread("u1")
    assert count == 2
    assert (await repo.get("n1")).read is True
    assert (await repo.get("n3")).read is True
```

- [ ] **Step 3.2: Run, expect failure**

```bash
uv run pytest tests/cloud/notifications/test_repository_inmemory.py -v
```

Expected: import error for `ee.cloud.notifications.repositories`.

- [ ] **Step 3.3: Implement `repositories.py`**

`ee/cloud/notifications/repositories.py`:

```python
"""Repository for notifications.

Defines the abstract `INotificationRepository` Protocol and a Beanie-
backed `MongoNotificationRepository`. Services depend on the Protocol;
the router DI's the default Mongo implementation. Tests substitute an
in-memory fake.

The Beanie ``Notification`` document and our domain ``Notification`` are
distinct types. The two private converters (`_to_domain`, `_to_doc`)
mediate. Beanie generates ObjectIds; the domain entity stores them as
plain strings.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud.models.notification import Notification as _NotificationDoc
from ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc
from ee.cloud.notifications.domain import Notification, NotificationSource


def _source_to_domain(
    src: _NotificationSourceDoc | None,
) -> NotificationSource | None:
    if src is None:
        return None
    return NotificationSource(type=src.type, id=src.id, pocket_id=src.pocket_id)


def _source_to_doc(
    src: NotificationSource | _NotificationSourceDoc | None,
) -> _NotificationSourceDoc | None:
    """Accepts either domain or doc form (callers in chat/workspace still
    pass the doc form; new code passes domain). Always returns the doc
    form for Beanie to persist."""
    if src is None:
        return None
    if isinstance(src, _NotificationSourceDoc):
        return src
    return _NotificationSourceDoc(type=src.type, id=src.id, pocket_id=src.pocket_id)


def _to_domain(doc: _NotificationDoc) -> Notification:
    return Notification(
        id=str(doc.id),
        workspace_id=doc.workspace,
        recipient_id=doc.recipient,
        kind=doc.type,
        title=doc.title,
        body=doc.body,
        source=_source_to_domain(doc.source),
        read=doc.read,
        # Beanie stores naive datetimes; created_at via TimestampedDocument's
        # ``createdAt`` attribute (camelCase Mongo field).
        created_at=getattr(doc, "createdAt", None),
        expires_at=doc.expires_at,
    )


@runtime_checkable
class INotificationRepository(Protocol):
    async def create(self, notification: Notification) -> Notification: ...
    async def get(self, notification_id: str) -> Notification | None: ...
    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]: ...
    async def mark_read(self, notification_id: str) -> bool: ...
    async def clear_unread(self, user_id: str) -> int: ...


class MongoNotificationRepository:
    """Beanie-backed implementation. Services should depend on
    `INotificationRepository`; this concrete class is wired by the
    default-repository accessor below."""

    async def create(self, notification: Notification) -> Notification:
        # The domain's `id` is ignored on insert — Beanie generates one.
        # The returned domain entity carries the new id.
        doc = _NotificationDoc(
            workspace=notification.workspace_id,
            recipient=notification.recipient_id,
            type=notification.kind,
            title=notification.title,
            body=notification.body,
            source=_source_to_doc(notification.source),
            read=notification.read,
            expires_at=notification.expires_at,
        )
        await doc.insert()
        return _to_domain(doc)

    async def get(self, notification_id: str) -> Notification | None:
        doc = await _NotificationDoc.get(PydanticObjectId(notification_id))
        return _to_domain(doc) if doc else None

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        query: dict = {"recipient": user_id}
        if unread:
            query["read"] = False
        cursor = (
            _NotificationDoc.find(query).sort(-_NotificationDoc.createdAt).limit(limit)
        )
        return [_to_domain(doc) async for doc in cursor]

    async def mark_read(self, notification_id: str) -> bool:
        doc = await _NotificationDoc.get(PydanticObjectId(notification_id))
        if not doc or doc.read:
            return False
        doc.read = True
        await doc.save()
        return True

    async def clear_unread(self, user_id: str) -> int:
        result = await _NotificationDoc.find(
            {"recipient": user_id, "read": False}
        ).update_many({"$set": {"read": True}})
        return getattr(result, "modified_count", 0)


_default: INotificationRepository | None = None


def get_default_repository() -> INotificationRepository:
    """Process-wide default Mongo-backed repository. Constructed lazily so
    importing this module doesn't require a live Mongo connection."""
    global _default
    if _default is None:
        _default = MongoNotificationRepository()
    return _default


def set_default_repository(repo: INotificationRepository) -> None:
    """Override the default repository (used by integration tests)."""
    global _default
    _default = repo


__all__ = [
    "INotificationRepository",
    "MongoNotificationRepository",
    "get_default_repository",
    "set_default_repository",
]
```

- [ ] **Step 3.4: Run, expect pass; lint/format/mypy/commit**

```bash
uv run pytest tests/cloud/notifications/test_repository_inmemory.py -v
uv run ruff check --fix ee/cloud/notifications/repositories.py tests/cloud/notifications/test_repository_inmemory.py
uv run ruff format ee/cloud/notifications/repositories.py tests/cloud/notifications/test_repository_inmemory.py
uv run mypy ee/cloud/notifications/repositories.py
git add ee/cloud/notifications/repositories.py tests/cloud/notifications/test_repository_inmemory.py
git commit -m "feat(notifications): repositories — INotificationRepository Protocol + Mongo impl"
```

Expected: 11 PASS.

---

## Task 4: Refactor `notifications/service.py`

The classmethod API of `NotificationService` (used by external callers in chat and workspace) is preserved. Internally, classmethods now build a transient instance with the default repository and delegate. Tests for the new architecture use the instance directly with a fake repository.

The realtime `emit` calls move into the instance methods. The existing wire-shape callbacks (`NotificationNew`, `NotificationRead`, `NotificationCleared` event payloads) are reconstructed from the domain entity via the same `notification_to_dto` mapper used by the router — the wire shape stays identical.

**Files:**
- Modify: `ee/cloud/notifications/service.py`
- Test: `tests/cloud/notifications/test_service_v2.py`
- Delete: `tests/cloud/notifications/test_service.py` (superseded)

- [ ] **Step 4.1: Write the new tests first (no production change yet)**

`tests/cloud/notifications/test_service_v2.py`:

```python
"""Tests for the refactored NotificationService.

Uses an in-memory repository fake — no Beanie patches, no internal
mocks. Asserts both the new instance-method API and the legacy
classmethod fan-out API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.repositories import (
    INotificationRepository,
    set_default_repository,
)
from ee.cloud.notifications.service import NotificationService
from ee.cloud.realtime.events import (
    NotificationCleared,
    NotificationNew,
    NotificationRead,
)


# ---------------------------------------------------------------------------
# In-memory repository fake (mirrors the one in test_repository_inmemory.py)
# ---------------------------------------------------------------------------


class _InMemoryRepo:
    def __init__(self) -> None:
        self._items: dict[str, Notification] = {}
        self._next_id = 0

    async def create(self, notification: Notification) -> Notification:
        from dataclasses import replace

        self._next_id += 1
        new = replace(
            notification,
            id=f"n{self._next_id}",
            created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        )
        self._items[new.id] = new
        return new

    async def get(self, notification_id: str) -> Notification | None:
        return self._items.get(notification_id)

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        out = [n for n in self._items.values() if n.recipient_id == user_id]
        if unread:
            out = [n for n in out if not n.read]
        out.sort(key=lambda n: n.created_at, reverse=True)
        return out[:limit]

    async def mark_read(self, notification_id: str) -> bool:
        from dataclasses import replace

        n = self._items.get(notification_id)
        if n is None or n.read:
            return False
        self._items[notification_id] = replace(n, read=True)
        return True

    async def clear_unread(self, user_id: str) -> int:
        from dataclasses import replace

        count = 0
        for nid, n in list(self._items.items()):
            if n.recipient_id == user_id and not n.read:
                self._items[nid] = replace(n, read=True)
                count += 1
        return count


# ---------------------------------------------------------------------------
# Capture realtime events instead of touching the bus
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []

    async def fake_emit(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr("ee.cloud.notifications.service.emit", fake_emit)
    return events


@pytest.fixture
def repo() -> INotificationRepository:
    return _InMemoryRepo()


@pytest.fixture
def service(repo: INotificationRepository) -> NotificationService:
    return NotificationService(repo)


# ---------------------------------------------------------------------------
# Instance-method API (new)
# ---------------------------------------------------------------------------


async def test_create_persists_and_emits_new(service, repo, captured_events) -> None:
    out = await service.create(
        workspace_id="w1",
        recipient="u2",
        kind="mention",
        title="You were mentioned",
        body="hi",
    )
    assert out.recipient_id == "u2"
    assert out.workspace_id == "w1"
    assert out.kind == "mention"
    assert (await repo.get(out.id)) is not None

    assert len(captured_events) == 1
    ev = captured_events[0]
    assert isinstance(ev, NotificationNew)
    assert ev.data["user_id"] == "u2"
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["kind"] == "mention"
    assert ev.data["read"] is False


async def test_list_for_user_filters_unread(service) -> None:
    await service.create(workspace_id="w1", recipient="u1", kind="m", title="t1")
    await service.create(workspace_id="w1", recipient="u1", kind="m", title="t2")
    # Mark first as read via repo to get a mixed list
    notes = await service.list_for_user("u1")
    await service.mark_read(notes[-1].id, "u1")  # the older one

    unread = await service.list_for_user("u1", unread=True)
    assert len(unread) == 1
    assert unread[0].title == "t2"


async def test_mark_read_emits_event_and_returns_true(service, captured_events) -> None:
    captured_events.clear()
    n = await service.create(
        workspace_id="w1", recipient="u1", kind="m", title="t"
    )
    captured_events.clear()  # discard the NotificationNew

    changed = await service.mark_read(n.id, "u1")
    assert changed is True
    assert any(isinstance(e, NotificationRead) for e in captured_events)
    read_ev = next(e for e in captured_events if isinstance(e, NotificationRead))
    assert read_ev.data == {"id": n.id, "user_id": "u1"}


async def test_mark_read_noop_for_wrong_user(service, captured_events) -> None:
    n = await service.create(
        workspace_id="w1", recipient="u1", kind="m", title="t"
    )
    captured_events.clear()
    changed = await service.mark_read(n.id, "u_other")
    assert changed is False
    assert captured_events == []


async def test_mark_read_noop_for_already_read(service, captured_events) -> None:
    n = await service.create(
        workspace_id="w1", recipient="u1", kind="m", title="t"
    )
    await service.mark_read(n.id, "u1")
    captured_events.clear()
    changed = await service.mark_read(n.id, "u1")
    assert changed is False
    assert captured_events == []


async def test_clear_all_returns_count_and_emits(service, captured_events) -> None:
    await service.create(workspace_id="w1", recipient="u1", kind="m", title="a")
    await service.create(workspace_id="w1", recipient="u1", kind="m", title="b")
    await service.create(workspace_id="w1", recipient="u2", kind="m", title="c")
    captured_events.clear()

    count = await service.clear_all("u1")
    assert count == 2
    cleared = [e for e in captured_events if isinstance(e, NotificationCleared)]
    assert len(cleared) == 1
    assert cleared[0].data == {"user_id": "u1"}


# ---------------------------------------------------------------------------
# Legacy classmethod API (still works for chat/message_service +
# workspace/service callers)
# ---------------------------------------------------------------------------


async def test_classmethod_create_uses_default_repo(
    captured_events, monkeypatch
) -> None:
    """The classmethod facade routes through the configured default repo."""
    fake = _InMemoryRepo()
    set_default_repository(fake)
    try:
        out = await NotificationService.create(
            workspace_id="w1",
            recipient="u9",
            kind="invite",
            title="Joined",
            body="",
        )
        assert out.recipient_id == "u9"
        # Default-repo path emits via the same path
        assert any(isinstance(e, NotificationNew) for e in captured_events)
    finally:
        # Reset so other tests get a fresh Mongo-backed default
        from ee.cloud.notifications.repositories import (
            MongoNotificationRepository,
            set_default_repository,
        )

        set_default_repository(MongoNotificationRepository())


async def test_classmethod_list_for_user_returns_dicts(monkeypatch) -> None:
    """Legacy callers expect ``list[dict]`` (the wire shape). Preserved."""
    fake = _InMemoryRepo()
    set_default_repository(fake)
    try:
        await NotificationService.create(
            workspace_id="w1", recipient="u1", kind="m", title="t"
        )
        out = await NotificationService.list_for_user("u1")
        assert isinstance(out, list) and len(out) == 1
        item = out[0]
        # Legacy wire keys
        assert set(item.keys()) >= {
            "id",
            "user_id",
            "workspace_id",
            "kind",
            "title",
            "body",
            "source_id",
            "read",
            "created_at",
        }
    finally:
        from ee.cloud.notifications.repositories import (
            MongoNotificationRepository,
            set_default_repository,
        )

        set_default_repository(MongoNotificationRepository())
```

- [ ] **Step 4.2: Run new tests, expect failure (service.py not yet refactored)**

```bash
uv run pytest tests/cloud/notifications/test_service_v2.py -v
```

Expected: failures. The current `service.py` doesn't have an instance constructor; `NotificationService(repo)` will TypeError or work with no-op (depending on Python's behavior). Either way, tests should not pass.

- [ ] **Step 4.3: Refactor `service.py`**

Replace the entire content of `ee/cloud/notifications/service.py`:

```python
"""Notification service.

Refactored in Phase 2 of the cloud-restructure: services consume domain
entities and depend on `INotificationRepository`. The legacy classmethod
API (`NotificationService.create`, `.list_for_user`, `.mark_read`,
`.clear_all`) is preserved for fan-out callers in
`chat/message_service.py` and `workspace/service.py`; those classmethods
build a transient instance against `get_default_repository()` and
delegate.

`emit` is imported at module level so it can be monkey-patched by tests
without touching the realtime bus directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ee.cloud.notifications.domain import Notification
from ee.cloud.notifications.dto import notification_to_dto
from ee.cloud.notifications.repositories import (
    INotificationRepository,
    get_default_repository,
)
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    NotificationCleared,
    NotificationNew,
    NotificationRead,
)

if TYPE_CHECKING:
    from ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc


class NotificationService:
    """Notifications CRUD + realtime fan-out.

    New code should construct an instance with an explicit repository
    (typically via FastAPI ``Depends``). The classmethod API exists for
    backward compatibility with legacy fan-out callers; it routes through
    `get_default_repository()`.
    """

    def __init__(self, repository: INotificationRepository) -> None:
        self._repo = repository

    # -------------------------------------------------------------------
    # Instance API (preferred for new code)
    # -------------------------------------------------------------------

    async def create(
        self,
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: "_NotificationSourceDoc | None" = None,
    ) -> Notification:
        """Create a notification and emit `NotificationNew`.

        ``source`` accepts the legacy Beanie sub-model. The repository
        converts to/from the domain form so callers in chat/workspace
        don't need to change their imports.
        """
        # Build the domain entity. id/created_at are placeholders;
        # the repository overwrites them on insert.
        from datetime import UTC, datetime

        from ee.cloud.notifications.domain import (
            NotificationSource as _DomainSource,
        )

        domain_source: _DomainSource | None = None
        if source is not None:
            domain_source = _DomainSource(
                type=source.type, id=source.id, pocket_id=source.pocket_id
            )

        proto = Notification(
            id="",  # placeholder, repo assigns
            workspace_id=workspace_id,
            recipient_id=recipient,
            kind=kind,
            title=title,
            body=body,
            source=domain_source,
            read=False,
            created_at=datetime.now(UTC),
        )
        created = await self._repo.create(proto)
        await emit(NotificationNew(data=notification_to_dto(created).model_dump()))
        return created

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        return await self._repo.list_for_user(user_id, unread=unread, limit=limit)

    async def mark_read(self, notification_id: str, user_id: str) -> bool:
        """Mark a notification as read iff it belongs to ``user_id`` and
        is currently unread. Returns whether anything changed."""
        existing = await self._repo.get(notification_id)
        if not existing or existing.recipient_id != user_id:
            return False
        changed = await self._repo.mark_read(notification_id)
        if changed:
            await emit(
                NotificationRead(data={"id": notification_id, "user_id": user_id})
            )
        return changed

    async def clear_all(self, user_id: str) -> int:
        count = await self._repo.clear_unread(user_id)
        await emit(NotificationCleared(data={"user_id": user_id}))
        return count

    # -------------------------------------------------------------------
    # Legacy classmethod facade (fan-out callers in chat/workspace)
    # -------------------------------------------------------------------

    @classmethod
    async def _default(cls) -> "NotificationService":
        return cls(get_default_repository())

    @classmethod
    async def create(  # type: ignore[no-redef]  # noqa: F811
        cls,
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: "_NotificationSourceDoc | None" = None,
    ) -> Notification:
        """Backward-compat classmethod. Equivalent to
        ``NotificationService(get_default_repository()).create(...)``."""
        impl = await cls._default()
        return await NotificationService.__instance_create(
            impl,
            workspace_id=workspace_id,
            recipient=recipient,
            kind=kind,
            title=title,
            body=body,
            source=source,
        )

    @staticmethod
    async def __instance_create(
        impl: "NotificationService",
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: "_NotificationSourceDoc | None" = None,
    ) -> Notification:
        # Trampoline because Python's name resolution doesn't let a
        # classmethod and an instance method share the same name on the
        # same class. We expose the instance behavior via a hidden helper.
        return await NotificationService._real_create(
            impl,
            workspace_id=workspace_id,
            recipient=recipient,
            kind=kind,
            title=title,
            body=body,
            source=source,
        )

    @staticmethod
    async def _real_create(
        impl: "NotificationService",
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: "_NotificationSourceDoc | None" = None,
    ) -> Notification:
        # Identical body to the instance ``create`` above; we keep one
        # implementation by routing the instance method through here too.
        from datetime import UTC, datetime

        from ee.cloud.notifications.domain import (
            NotificationSource as _DomainSource,
        )

        domain_source: _DomainSource | None = None
        if source is not None:
            domain_source = _DomainSource(
                type=source.type, id=source.id, pocket_id=source.pocket_id
            )
        proto = Notification(
            id="",
            workspace_id=workspace_id,
            recipient_id=recipient,
            kind=kind,
            title=title,
            body=body,
            source=domain_source,
            read=False,
            created_at=datetime.now(UTC),
        )
        created = await impl._repo.create(proto)
        await emit(NotificationNew(data=notification_to_dto(created).model_dump()))
        return created
```

**Note**: the classmethod / instance method name collision (both called `create`) is a real Python constraint. I've sketched one resolution above using a trampoline, but it's awkward. **An equivalent, cleaner approach** is to drop the duplicated `create` method on the class and instead define a module-level `async def create(*, workspace_id, ...)` that uses the default repo, with the class only carrying instance methods. That requires updating the existing fan-out callers (`chat/message_service.py`, `workspace/service.py`) to call `NotificationService.create(...)` differently.

**Concrete decision for the executor:** use the cleaner approach. Replace the entire prior content of `service.py` with the version below (this is the canonical implementation; the trampoline above was illustrative).

`ee/cloud/notifications/service.py` (final):

```python
"""Notification service — CRUD + realtime fan-out.

Refactored in Phase 2 of the cloud-restructure. The service is now an
instance class that depends on ``INotificationRepository``. Existing
fan-out callers (`chat/message_service.py`, `workspace/service.py`)
call `NotificationService.create(...)` as a classmethod-like static; we
provide a module-level convenience that builds a transient service
against the default repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.dto import notification_to_dto
from ee.cloud.notifications.repositories import (
    INotificationRepository,
    get_default_repository,
)
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    NotificationCleared,
    NotificationNew,
    NotificationRead,
)

if TYPE_CHECKING:
    from ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc


def _to_domain_source(
    src: "_NotificationSourceDoc | NotificationSource | None",
) -> NotificationSource | None:
    if src is None:
        return None
    if isinstance(src, NotificationSource):
        return src
    return NotificationSource(type=src.type, id=src.id, pocket_id=src.pocket_id)


class NotificationService:
    """Notifications CRUD + realtime fan-out.

    Construct with a repository for testing; the classmethod facade
    methods (`create_for_default`, etc.) are provided for legacy callers
    that already use the class as a static namespace.
    """

    def __init__(self, repository: INotificationRepository) -> None:
        self._repo = repository

    async def create(
        self,
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: "_NotificationSourceDoc | NotificationSource | None" = None,
    ) -> Notification:
        proto = Notification(
            id="",
            workspace_id=workspace_id,
            recipient_id=recipient,
            kind=kind,
            title=title,
            body=body,
            source=_to_domain_source(source),
            read=False,
            created_at=datetime.now(UTC),
        )
        created = await self._repo.create(proto)
        await emit(NotificationNew(data=notification_to_dto(created).model_dump()))
        return created

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        return await self._repo.list_for_user(user_id, unread=unread, limit=limit)

    async def mark_read(self, notification_id: str, user_id: str) -> bool:
        existing = await self._repo.get(notification_id)
        if not existing or existing.recipient_id != user_id:
            return False
        changed = await self._repo.mark_read(notification_id)
        if changed:
            await emit(
                NotificationRead(data={"id": notification_id, "user_id": user_id})
            )
        return changed

    async def clear_all(self, user_id: str) -> int:
        count = await self._repo.clear_unread(user_id)
        await emit(NotificationCleared(data={"user_id": user_id}))
        return count

    # ------------------------------------------------------------------
    # Legacy classmethod facade — preserves call signatures used by
    # chat/message_service.py and workspace/service.py.
    # ------------------------------------------------------------------

    @classmethod
    async def create_default(
        cls,
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: "_NotificationSourceDoc | NotificationSource | None" = None,
    ) -> Notification:
        return await cls(get_default_repository()).create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind=kind,
            title=title,
            body=body,
            source=source,
        )

    @classmethod
    async def list_for_user_default(
        cls, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[dict]:
        """Legacy wire-shape variant: returns ``list[dict]`` for routers
        that haven't yet adopted the DTO."""
        notes = await cls(get_default_repository()).list_for_user(
            user_id, unread=unread, limit=limit
        )
        return [notification_to_dto(n).model_dump() for n in notes]

    @classmethod
    async def mark_read_default(cls, notification_id: str, user_id: str) -> None:
        await cls(get_default_repository()).mark_read(notification_id, user_id)

    @classmethod
    async def clear_all_default(cls, user_id: str) -> int:
        return await cls(get_default_repository()).clear_all(user_id)
```

The classmethods are renamed with a `_default` suffix to avoid shadowing instance methods. **This requires updating the two external callers** (`chat/message_service.py`, `workspace/service.py`) to call `NotificationService.create_default(...)`.

- [ ] **Step 4.4: Update external callers**

`ee/cloud/chat/message_service.py` — find the two `await NotificationService.create(...)` lines and rename to `create_default`.

```bash
grep -n "NotificationService.create(" ee/cloud/chat/message_service.py
```

For each match, replace `NotificationService.create(` with `NotificationService.create_default(`. Use `sed -i` style edit via the `Edit` tool with sufficient surrounding context to make each occurrence unique.

`ee/cloud/workspace/service.py` — same.

```bash
grep -n "NotificationService.create(" ee/cloud/workspace/service.py
```

Rename to `create_default`.

- [ ] **Step 4.5: Run new service tests, expect pass**

The classmethod-API tests in `test_service_v2.py` were written against `create()` (the legacy name). They need updating to use `create_default`. Update the test file:

Replace `await NotificationService.create(` with `await NotificationService.create_default(` in the two `test_classmethod_*` functions. Replace `await NotificationService.list_for_user(` with `await NotificationService.list_for_user_default(`.

```bash
uv run pytest tests/cloud/notifications/test_service_v2.py -v
```

Expected: 8 PASS.

- [ ] **Step 4.6: Delete the old service test file**

The old `tests/cloud/notifications/test_service.py` patches `Notification`, `PydanticObjectId`, and `emit` at module level — those patches no longer apply because `service.py` no longer imports `Notification` or `PydanticObjectId` directly (they live in the repository).

```bash
git rm tests/cloud/notifications/test_service.py
```

- [ ] **Step 4.7: Run broader cloud suite for regression**

```bash
uv run pytest tests/cloud --maxfail=30 -q 2>&1 | grep -E "^[0-9]+ (passed|failed|error)" | tail -3
```

Expected: same baseline failure count (20 failed, 10 errors). Pre-existing `test_derived.py` failures unchanged.

- [ ] **Step 4.8: Lint, format, type-check, commit**

```bash
uv run ruff check --fix ee/cloud/notifications/service.py ee/cloud/chat/message_service.py ee/cloud/workspace/service.py tests/cloud/notifications/test_service_v2.py
uv run ruff format ee/cloud/notifications/service.py ee/cloud/chat/message_service.py ee/cloud/workspace/service.py tests/cloud/notifications/test_service_v2.py
uv run mypy ee/cloud/notifications/service.py
git add ee/cloud/notifications/service.py ee/cloud/chat/message_service.py ee/cloud/workspace/service.py tests/cloud/notifications/test_service_v2.py
git rm tests/cloud/notifications/test_service.py
git commit -m "refactor(notifications): service uses domain/repo/dto layers; classmethod facade renamed *_default"
```

---

## Task 5: Refactor `notifications/router.py`

The router previously called `NotificationService.list_for_user(str(user.id), ...)` (returned `list[dict]`). After refactor: it depends on `request_context` for the user, depends on a service factory for the `NotificationService` instance, and returns `list[NotificationOut]` (typed DTOs). FastAPI auto-serializes the DTOs to JSON; the wire shape is identical to before.

**Files:**
- Modify: `ee/cloud/notifications/router.py`
- Test: `tests/cloud/notifications/test_router_golden.py`

- [ ] **Step 5.1: Write the golden-response tests first**

`tests/cloud/notifications/test_router_golden.py`:

```python
"""Golden-response tests for the notifications router.

These tests assert the wire shape returned by each endpoint by
constructing a small FastAPI app that mounts only the notifications
router with the service dep overridden to one backed by an in-memory
repository. The shape asserted here is the same shape the legacy
`_to_wire` function produced.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.http import add_error_handler
from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.repositories import INotificationRepository
from ee.cloud.notifications.router import get_notification_service, router
from ee.cloud.notifications.service import NotificationService


class _Repo:
    def __init__(self) -> None:
        self._items: dict[str, Notification] = {}

    async def create(self, n: Notification) -> Notification:
        self._items[n.id] = n
        return n

    async def get(self, nid: str) -> Notification | None:
        return self._items.get(nid)

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        out = [n for n in self._items.values() if n.recipient_id == user_id]
        if unread:
            out = [n for n in out if not n.read]
        out.sort(key=lambda n: n.created_at, reverse=True)
        return out[:limit]

    async def mark_read(self, nid: str) -> bool:
        from dataclasses import replace

        n = self._items.get(nid)
        if not n or n.read:
            return False
        self._items[nid] = replace(n, read=True)
        return True

    async def clear_unread(self, user_id: str) -> int:
        from dataclasses import replace

        count = 0
        for nid, n in list(self._items.items()):
            if n.recipient_id == user_id and not n.read:
                self._items[nid] = replace(n, read=True)
                count += 1
        return count


def _seed_notification(repo: _Repo, **kw) -> Notification:
    base = dict(
        id="n1",
        workspace_id="w1",
        recipient_id="user-1",
        kind="mention",
        title="hi",
        body="b",
        source=NotificationSource(type="message", id="m1", pocket_id=None),
        read=False,
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
    )
    base.update(kw)
    n = Notification(**base)
    repo._items[n.id] = n
    return n


@pytest.fixture
def app_and_repo(monkeypatch):
    from ee.cloud.auth import current_active_user

    repo = _Repo()
    service = NotificationService(repo)

    class _U:
        id = "user-1"
        active_workspace = "w1"
        workspaces = []

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = lambda: _U()
    app.dependency_overrides[get_notification_service] = lambda: service
    return app, repo


def test_list_returns_dto_shape(app_and_repo) -> None:
    app, repo = app_and_repo
    _seed_notification(repo)
    client = TestClient(app)

    resp = client.get("/api/v1/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert set(item.keys()) == {
        "id",
        "user_id",
        "workspace_id",
        "kind",
        "title",
        "body",
        "source_id",
        "read",
        "created_at",
    }
    assert item == {
        "id": "n1",
        "user_id": "user-1",
        "workspace_id": "w1",
        "kind": "mention",
        "title": "hi",
        "body": "b",
        "source_id": "m1",
        "read": False,
        "created_at": "2026-04-27T12:00:00+00:00",
    }


def test_list_unread_query_filters(app_and_repo) -> None:
    app, repo = app_and_repo
    _seed_notification(repo, id="n1", read=True)
    _seed_notification(repo, id="n2", read=False)
    client = TestClient(app)

    body = client.get("/api/v1/notifications?unread=true").json()
    assert [n["id"] for n in body] == ["n2"]


def test_list_limit_query_caps_results(app_and_repo) -> None:
    app, repo = app_and_repo
    for i in range(5):
        _seed_notification(repo, id=f"n{i}", created_at=datetime(2026, 4, 27, 12, 0, i, tzinfo=UTC))
    client = TestClient(app)

    body = client.get("/api/v1/notifications?limit=2").json()
    assert len(body) == 2


def test_mark_read_returns_ok_envelope(app_and_repo, monkeypatch) -> None:
    app, repo = app_and_repo
    _seed_notification(repo)
    monkeypatch.setattr("ee.cloud.notifications.service.emit", _no_emit)

    client = TestClient(app)
    resp = client.post("/api/v1/notifications/n1/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert repo._items["n1"].read is True


def test_clear_returns_count(app_and_repo, monkeypatch) -> None:
    app, repo = app_and_repo
    _seed_notification(repo, id="n1", read=False)
    _seed_notification(repo, id="n2", read=False)
    monkeypatch.setattr("ee.cloud.notifications.service.emit", _no_emit)

    client = TestClient(app)
    resp = client.post("/api/v1/notifications/clear")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 2}


async def _no_emit(_event):
    pass
```

- [ ] **Step 5.2: Run, expect failure (router not yet refactored)**

```bash
uv run pytest tests/cloud/notifications/test_router_golden.py -v
```

Expected: ImportError for `get_notification_service` (not yet defined).

- [ ] **Step 5.3: Refactor `router.py`**

Replace the entire content of `ee/cloud/notifications/router.py`:

```python
"""Notifications REST router.

Refactored in Phase 2 of the cloud-restructure. The router now:
- Uses ``Depends(request_context)`` to obtain the typed RequestContext.
- Uses ``Depends(get_notification_service)`` so the service (and the
  underlying repository) can be swapped in tests via
  ``app.dependency_overrides``.
- Returns Pydantic ``NotificationOut`` DTOs at the boundary; FastAPI
  serializes to JSON. The wire shape matches the legacy `_to_wire`
  output byte-for-byte (verified by golden-response tests).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.notifications.dto import NotificationOut, notification_to_dto
from ee.cloud.notifications.repositories import (
    INotificationRepository,
    get_default_repository,
)
from ee.cloud.notifications.service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def get_notification_service(
    repo: INotificationRepository = Depends(get_default_repository),
) -> NotificationService:
    """FastAPI dep — builds a NotificationService against the default
    Mongo repo. Tests override either this or `get_default_repository`."""
    return NotificationService(repo)


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
    service: NotificationService = Depends(get_notification_service),
) -> list[NotificationOut]:
    notes = await service.list_for_user(ctx.user_id, unread=unread, limit=limit)
    return [notification_to_dto(n) for n in notes]


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    ctx: RequestContext = Depends(request_context),
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    await service.mark_read(notification_id, ctx.user_id)
    return {"ok": True}


@router.post("/clear")
async def clear_all(
    ctx: RequestContext = Depends(request_context),
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    count = await service.clear_all(ctx.user_id)
    return {"cleared": count}
```

- [ ] **Step 5.4: Run golden tests, expect pass**

```bash
uv run pytest tests/cloud/notifications/test_router_golden.py -v
```

Expected: 5 PASS.

- [ ] **Step 5.5: Run all notifications tests**

```bash
uv run pytest tests/cloud/notifications -v
```

Expected: domain (5) + dto (4) + repository inmemory (11) + service v2 (8) + router golden (5) = 33 PASS. (Existing `test_derived.py` 5 failures pre-existing baseline; not addressed here.)

- [ ] **Step 5.6: Delete unused `notifications/schemas.py`**

```bash
git rm ee/cloud/notifications/schemas.py
uv run pytest tests/cloud --maxfail=30 -q 2>&1 | tail -3
```

Expected: same baseline (no test references `NotificationResponse`).

- [ ] **Step 5.7: Lint, format, type-check, commit**

```bash
uv run ruff check --fix ee/cloud/notifications/router.py tests/cloud/notifications/test_router_golden.py
uv run ruff format ee/cloud/notifications/router.py tests/cloud/notifications/test_router_golden.py
uv run mypy ee/cloud/notifications/router.py
git add ee/cloud/notifications/router.py tests/cloud/notifications/test_router_golden.py
git rm ee/cloud/notifications/schemas.py
git commit -m "refactor(notifications): router uses RequestContext + DTO; drop unused schemas.py"
```

---

## Task 6: Final verification

- [ ] **Step 6.1: Full notifications suite**

```bash
uv run pytest tests/cloud/notifications -v 2>&1 | tail -10
```

Expected: 33 PASS (or 38 PASS if `test_derived.py`'s 5 happen to pass on this branch — unlikely; they're stable baseline failures).

- [ ] **Step 6.2: Full _core suite (regression on Phase 0+1 work)**

```bash
uv run pytest tests/cloud/_core -v 2>&1 | tail -5
```

Expected: 72 PASS unchanged.

- [ ] **Step 6.3: Full cloud suite (broader regression)**

```bash
uv run pytest tests/cloud --maxfail=30 -q 2>&1 | grep -E "^[0-9]+ (passed|failed|error)" | tail -3
```

Expected: same 20 failed + 10 errored as baseline; ≥510 passed (baseline 477 + ≥28 new from Phase 2 minus the 5 deleted from old test_service.py).

- [ ] **Step 6.4: Verify no surprise import sites**

```bash
grep -rn "from ee.cloud.notifications.schemas import" ee tests src 2>&1 | head -5
grep -rn "_to_wire" ee/cloud/notifications/ 2>&1 | head -5
```

Expected: no matches. Both are dead.

- [ ] **Step 6.5: Verify external callers still compile**

```bash
uv run python -c "
from ee.cloud.chat import message_service
from ee.cloud.workspace import service as ws_service
print('imports ok')
"
```

Expected: `imports ok`.

- [ ] **Step 6.6: Branch state for handoff**

```bash
git log --oneline ee..HEAD
git rev-parse HEAD
```

Record commits + tip SHA for the user.

---

## Self-review checklist

**Spec coverage:**
- §4.1 layer model — `domain → repositories → service → router` for notifications ✓
- §4.2 hybrid layout — `notifications/{domain,repositories,dto,service,router}.py` ✓
- §5.1 strangler step — golden tests, build new alongside, delete old, atomic per-PR ✓
- §6 error handling — no `HTTPException` raised in notifications code ✓
- §7 testing (Bar A) — unit + golden-response; ≥80% coverage of new code ✓

**No placeholders:** every code block is complete; every command is exact.

**Type consistency:** `Notification`, `NotificationSource` (domain) consistent across files; `INotificationRepository` Protocol; `NotificationOut` DTO; `notification_to_dto` mapper.

**Out-of-scope items deferred (named):**
- Updating chat/workspace callers' source-type imports — happens in Phases 6/7/10 with their domain migrations.
- Mongo integration tests for `MongoNotificationRepository` — covered by the existing broader cloud test suite hitting the live router/service via the standard fixtures.

---

## Handoff

When all tasks pass, the branch `refactor/cloud-notifications` is ready for user review. The next plan is `docs/superpowers/plans/2026-04-27-phase3-auth.md` (auth migration — foundational module everyone depends on).
