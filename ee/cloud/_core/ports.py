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

from datetime import UTC, datetime
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
        return datetime.now(UTC)


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
