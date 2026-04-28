"""Shared fixtures for cloud tests.

Installs a RecordingBus for every test so ``emit()`` calls inside services
don't raise AssertionError (the real bus is only set up in ``init_realtime``
during app startup, which tests don't invoke). Tests that want to assert
on emitted events request the ``recording_bus`` fixture explicitly to read
``bus.events``.

Also exposes:
- ``mongo_db`` — Beanie initialized against a fresh mongomock-motor DB
  for the test. Used by service-level tests that exercise real Beanie
  query paths instead of relying on a Protocol fake.
- ``cloud_app_client`` — a FastAPI app with the enterprise chat routers
  mounted and auth/license dependencies overridden, used by HTTP-layer
  tests so they don't need a real JWT.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.realtime.events import Event


class RecordingBus:
    """Test EventBus that records published events instead of fanning out.

    Drop-in replacement for the production bus. Tests assert on
    ``bus.events`` to verify emit-time behavior.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a RecordingBus for every test.

    Tests that don't care about events ignore the fixture; tests that
    do request it explicitly to inspect ``bus.events``.
    """
    from ee.cloud._core.realtime import bus as bus_mod

    rec = RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_repo_singletons():
    # Snapshot and restore each repositories module's lazy singleton.
    # Tests that swap in a fake via `set_*_repository(...)` (or poke the
    # global directly) would otherwise leak the fake into later tests.
    # This will become a no-op once all modules drop their repositories.py;
    # the fixture is removed in the final cleanup commit of Milestone 1.
    from ee.cloud.agents import repositories as agents_repos
    from ee.cloud.auth import repositories as auth_repos
    from ee.cloud.chat import repositories as chat_repos
    from ee.cloud.pockets import repositories as pockets_repos
    from ee.cloud.sessions import repositories as sessions_repos
    from ee.cloud.workspace import repositories as workspace_repos

    snapshots: list[tuple[object, str, object]] = [
        (agents_repos, "_default", agents_repos._default),  # type: ignore[attr-defined]
        (auth_repos, "_default", auth_repos._default),  # type: ignore[attr-defined]
        (chat_repos, "_default_message", chat_repos._default_message),  # type: ignore[attr-defined]
        (chat_repos, "_default_group", chat_repos._default_group),  # type: ignore[attr-defined]
        (pockets_repos, "_default", pockets_repos._default),  # type: ignore[attr-defined]
        (sessions_repos, "_default", sessions_repos._default),  # type: ignore[attr-defined]
        (
            workspace_repos,
            "_default_workspace",
            workspace_repos._default_workspace,  # type: ignore[attr-defined]
        ),
        (
            workspace_repos,
            "_default_invite",
            workspace_repos._default_invite,  # type: ignore[attr-defined]
        ),
    ]
    yield
    for module, attr, prev in snapshots:
        setattr(module, attr, prev)


@pytest_asyncio.fixture
async def mongo_db() -> Any:
    """Initialize Beanie against an isolated in-memory mongomock-motor DB.

    Each test gets a uniquely-named database. Beanie >=1.26 calls
    ``database.list_collection_names(authorizedCollections=True, nameOnly=True)``;
    mongomock-motor doesn't accept those kwargs, so we wrap the method
    to drop unknown kwargs.
    """
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    from ee.cloud.memory.documents import MemoryFactDoc
    from ee.cloud.models import ALL_DOCUMENTS

    db_name = f"test_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args, **_kwargs):
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield db


def _fixed_user() -> str:
    return "u1"


def _fixed_workspace() -> str:
    return "w1"


def _no_op_license() -> None:
    return None


@pytest_asyncio.fixture
async def cloud_app_client() -> AsyncClient:
    from ee.cloud.chat.agent_router import router as agent_router
    from ee.cloud.license import require_license
    from ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[current_user_id] = _fixed_user
    app.dependency_overrides[current_workspace_id] = _fixed_workspace
    app.dependency_overrides[require_license] = _no_op_license

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client
