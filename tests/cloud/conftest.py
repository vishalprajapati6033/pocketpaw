"""Shared fixtures for cloud tests.

Installs a no-op realtime bus for every test so ``emit()`` calls inside
services don't raise AssertionError (the real bus is only set up in
``init_realtime`` during app startup, which tests don't invoke).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _install_noop_bus():
    from ee.cloud.realtime import bus as bus_mod

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = AsyncMock()  # type: ignore[attr-defined]
    yield
    bus_mod._bus = prev  # type: ignore[attr-defined]
