"""Tests for init_realtime wiring."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_init_realtime_uses_inprocess_by_default(monkeypatch):
    from ee.cloud import init_realtime
    from ee.cloud.realtime import bus as bus_mod
    from ee.cloud.realtime.bus import InProcessBus

    bus_mod._bus = None  # type: ignore[attr-defined]
    monkeypatch.delenv("POCKETPAW_REALTIME_BUS", raising=False)

    init_realtime()

    assert isinstance(bus_mod._bus, InProcessBus)  # type: ignore[attr-defined]


def test_init_realtime_exposes_resolver(monkeypatch):
    from ee.cloud import init_realtime
    from ee.cloud.realtime import bus as bus_mod
    from ee.cloud.realtime.audience import AudienceResolver
    from ee.cloud.realtime.bus import get_resolver

    bus_mod._bus = None  # type: ignore[attr-defined]
    bus_mod._resolver = None  # type: ignore[attr-defined]
    monkeypatch.delenv("POCKETPAW_REALTIME_BUS", raising=False)

    init_realtime()

    assert isinstance(get_resolver(), AudienceResolver)


def test_init_realtime_falls_back_to_inprocess_for_unsupported_bus(monkeypatch, caplog):
    from ee.cloud import init_realtime
    from ee.cloud.realtime import bus as bus_mod
    from ee.cloud.realtime.bus import InProcessBus

    bus_mod._bus = None  # type: ignore[attr-defined]
    monkeypatch.setenv("POCKETPAW_REALTIME_BUS", "redis")

    with caplog.at_level("WARNING"):
        init_realtime()

    assert isinstance(bus_mod._bus, InProcessBus)  # type: ignore[attr-defined]
    assert any("redis" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_group_list_member_ids_returns_members_field(monkeypatch):
    from ee.cloud.chat.group_service import GroupService

    class FakeGroup:
        members = ["u1", "u2", "u3"]

    async def fake_get(_gid: str):
        return FakeGroup()

    with patch("ee.cloud.chat.group_service.GroupService._fetch_group", fake_get, create=True):
        # The helper calls a small internal fetcher to keep the test mockable
        ids = await GroupService.list_member_ids("g1")
    assert ids == ["u1", "u2", "u3"]


@pytest.mark.asyncio
async def test_group_list_member_ids_returns_empty_for_missing_group():
    from ee.cloud.chat.group_service import GroupService

    async def fake_get(_gid: str):
        return None

    from unittest.mock import patch

    with patch("ee.cloud.chat.group_service.GroupService._fetch_group", fake_get, create=True):
        ids = await GroupService.list_member_ids("gmissing")
    assert ids == []
