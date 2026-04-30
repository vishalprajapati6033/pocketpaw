# test_listener.py — tests for the FileReady KB-indexing subscriber.
# Created: 2026-04-30 — Stage 1.B "Files as Knowledge". Verifies the
#   listener resolves the storage path, runs extraction, ingests into
#   workspace KB, and contains failures so they don't propagate back to
#   the publisher.
"""Tests for ``ee.cloud.uploads.listeners.index_uploaded_file``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ee.cloud._core.realtime.events import FileReady
from ee.cloud.extraction.adapter import ExtractionResult


class _FakeChain:
    """Stand-in for ee.cloud.extraction.ExtractionChain.run."""

    def __init__(self, result: ExtractionResult):
        self._result = result
        self.calls: list[tuple[Path, str]] = []

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        self.calls.append((path, mime))
        return self._result


def _patch_listener(
    monkeypatch,
    *,
    chain: _FakeChain | None = None,
    storage_path: Path | None = None,
    ingest: AsyncMock | None = None,
):
    """Wire the listener's collaborators with test doubles."""
    from ee.cloud.uploads import listeners

    if chain is not None:
        monkeypatch.setattr(
            "ee.cloud.extraction.build_chain",
            lambda settings: chain,
        )

    if storage_path is not None:
        monkeypatch.setattr(
            listeners,
            "_resolve_storage_path",
            lambda key: storage_path,
        )

    if ingest is not None:
        from ee.cloud.agents import knowledge as kn

        monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)


@pytest.mark.asyncio
async def test_index_uploaded_file_calls_chain_then_kb(monkeypatch, tmp_path):
    """Happy path: extraction runs, KB ingest gets the right scope/source."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    fake_path = tmp_path / "doc.pdf"
    fake_path.write_bytes(b"unused; chain.run is mocked")

    chain = _FakeChain(
        ExtractionResult(text="hello world", backend="local")
    )
    ingest = AsyncMock(return_value={"id": "art-1"})
    _patch_listener(monkeypatch, chain=chain, storage_path=fake_path, ingest=ingest)

    ev = FileReady(
        data={
            "workspace_id": "w1",
            "file_id": "f1",
            "filename": "doc.pdf",
            "mime": "application/pdf",
            "storage_key": "ws/w1/f1.pdf",
        }
    )

    await index_uploaded_file(ev)

    assert chain.calls == [(fake_path, "application/pdf")]
    ingest.assert_awaited_once_with(
        scope="workspace:w1",
        text="hello world",
        source="doc.pdf",
    )


@pytest.mark.asyncio
async def test_index_skips_when_workspace_id_missing(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="ignored", backend="local"))
    ingest = AsyncMock()
    _patch_listener(
        monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest
    )

    await index_uploaded_file(
        FileReady(data={"file_id": "f1", "storage_key": "k"})
    )

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_skips_when_file_id_missing(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="ignored", backend="local"))
    ingest = AsyncMock()
    _patch_listener(
        monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest
    )

    await index_uploaded_file(
        FileReady(data={"workspace_id": "w1", "storage_key": "k"})
    )

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_skips_when_storage_path_unresolved(monkeypatch):
    """Remote adapters return None for local_path — listener must bail."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="ignored", backend="local"))
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, storage_path=None, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "storage_key": "s3://bucket/f1",
            }
        )
    )

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_skips_when_extraction_returns_empty(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="   ", backend="local"))
    ingest = AsyncMock()
    _patch_listener(
        monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "storage_key": "k",
                "filename": "blank.pdf",
                "mime": "application/pdf",
            }
        )
    )

    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_failure_does_not_propagate(monkeypatch, tmp_path):
    """Chain raises → listener swallows the error and skips ingest."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    class _ExplodingChain:
        async def run(self, *_args, **_kwargs):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(
        "ee.cloud.extraction.build_chain", lambda settings: _ExplodingChain()
    )
    ingest = AsyncMock()
    _patch_listener(monkeypatch, storage_path=tmp_path / "x", ingest=ingest)

    # Must not raise — the bus catches handler errors but we keep the
    # listener defensive so the failure surfaces as a log line, not a
    # missed audit trail.
    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "storage_key": "k",
                "filename": "x.pdf",
                "mime": "application/pdf",
            }
        )
    )

    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_kb_failure_does_not_propagate(monkeypatch, tmp_path):
    """KB ingest raises → listener swallows so the publisher never sees it."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="content", backend="local"))
    ingest = AsyncMock(side_effect=RuntimeError("kb missing"))
    _patch_listener(
        monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "storage_key": "k",
                "filename": "x.pdf",
                "mime": "application/pdf",
            }
        )
    )

    # The mock was actually called, but its side-effect was contained.
    ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_upload_listeners_subscribes_to_file_ready():
    """Bootstrap path: register_upload_listeners hooks the bus."""
    from ee.cloud._core.realtime import bus as bus_mod
    from ee.cloud._core.realtime.audience import AudienceResolver
    from ee.cloud._core.realtime.bus import InProcessBus
    from ee.cloud.uploads.listeners import (
        index_uploaded_file,
        register_upload_listeners,
    )

    real_bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = real_bus  # type: ignore[attr-defined]
    try:
        register_upload_listeners()
        handlers = real_bus._handlers.get(FileReady.EVENT_TYPE, [])
        assert index_uploaded_file in handlers
    finally:
        bus_mod._bus = prev  # type: ignore[attr-defined]
