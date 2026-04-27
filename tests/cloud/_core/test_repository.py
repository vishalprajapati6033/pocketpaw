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
    fetched = await repo.get("a")
    assert fetched is not None
    assert fetched.label == "ALPHA"
    await repo.delete("a")
    assert await repo.get("a") is None


async def test_repo_get_returns_none_for_missing() -> None:
    repo: Repository[_Widget] = _InMemoryWidgetRepository()
    assert await repo.get("missing") is None


async def test_repo_list_empty() -> None:
    repo: Repository[_Widget] = _InMemoryWidgetRepository()
    assert await repo.list() == []
