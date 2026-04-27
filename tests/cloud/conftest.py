"""Shared fixtures for cloud tests.

Installs a no-op realtime bus for every test so ``emit()`` calls inside
services don't raise AssertionError (the real bus is only set up in
``init_realtime`` during app startup, which tests don't invoke).

Also exposes ``cloud_app_client`` — a FastAPI app with the enterprise
chat routers mounted and auth/license dependencies overridden, used by
HTTP-layer tests so they don't need a real JWT.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _install_noop_bus():
    # Phase 5 (2026-04-27) moved the bus singleton into _core.realtime.bus.
    # The old `ee.cloud.realtime.bus` is now a shim, but the singleton
    # still lives at the canonical path.
    from ee.cloud._core.realtime import bus as bus_mod

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = AsyncMock()  # type: ignore[attr-defined]
    yield
    bus_mod._bus = prev  # type: ignore[attr-defined]


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
