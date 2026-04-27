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
