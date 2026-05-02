# backend/tests/test_ripple_manifest.py
"""Tests for ee.ripple.manifest — fetcher, cache, formatter, fallback."""

from __future__ import annotations

import httpx
import pytest


pytestmark = pytest.mark.asyncio


VALID_MANIFEST = {
    "schema": "ripple.manifest/v1",
    "version": "0.2.0",
    "generatedAt": "2026-05-02T00:00:00.000Z",
    "widgets": [
        {
            "type": "metric",
            "category": "display",
            "description": "KPI tile.",
            "props": {
                "label": {"type": "string", "required": False, "description": "Label."},
                "value": {"type": "string | number", "required": True, "description": "Value."},
            },
            "example": {"type": "metric", "props": {"label": "MRR", "value": "$48k"}},
        }
    ],
}


@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    from ee.ripple import manifest as m

    m._cache.clear()
    yield
    m._cache.clear()


async def test_fetch_success(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is not None
    assert result["schema"] == "ripple.manifest/v1"
    assert len(result["widgets"]) == 1


async def test_cache_hit_avoids_second_fetch(monkeypatch):
    from ee.ripple import manifest as m

    calls = {"n": 0}

    async def fake_get(self, url, timeout):
        calls["n"] += 1
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert calls["n"] == 1


async def test_cache_expiry_triggers_refetch(monkeypatch):
    from ee.ripple import manifest as m

    calls = {"n": 0}

    async def fake_get(self, url, timeout):
        calls["n"] += 1
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await m.get_manifest("https://example/manifest.json", ttl_seconds=0)
    # ttl=0 means every call is expired — second call refetches
    await m.get_manifest("https://example/manifest.json", ttl_seconds=0)
    assert calls["n"] == 2


async def test_fetch_timeout_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_fetch_4xx_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        return httpx.Response(404, text="not found", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_malformed_json_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        return httpx.Response(200, text="not json", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_schema_mismatch_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    bad = {"schema": "ripple.manifest/v2", "version": "1", "widgets": []}

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=bad, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_missing_widgets_field_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    bad = {"schema": "ripple.manifest/v1", "version": "1"}

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=bad, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


def test_format_for_prompt_renders_widgets():
    from ee.ripple import manifest as m

    block = m.format_for_prompt(VALID_MANIFEST)
    assert "<ripple-widget-reference>" in block
    assert "</ripple-widget-reference>" in block
    assert "metric" in block
    assert "KPI tile." in block


def test_format_for_prompt_empty_widgets_returns_empty_string():
    from ee.ripple import manifest as m

    empty = {"schema": "ripple.manifest/v1", "version": "1", "widgets": []}
    assert m.format_for_prompt(empty) == ""
