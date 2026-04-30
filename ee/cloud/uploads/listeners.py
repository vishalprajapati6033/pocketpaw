# listeners.py — In-process subscribers for upload-related bus events.
# Created: 2026-04-30 — Stage 1.B of "Files as Knowledge". Wires FileReady
#   into the extraction chain and ingests the resulting text into the
#   workspace KB scope. Pocket-scope routing lands in Stage 3.E.
"""Upload bus subscribers.

The upload pipeline emits :class:`FileReady` on every successful upload.
This module subscribes that event and runs the indexing flow:

  1. Resolve the storage path via the EE upload resolver.
  2. Run the configured extraction chain to produce searchable text.
  3. Ingest the text into the kb-go scope ``workspace:{wid}``.

Failures are isolated — a broken extraction or a missing kb binary must not
propagate back to the upload publisher. The bus already wraps each handler
in a try/except, but we keep the listener defensive so the failure mode is
"file uploads, but doesn't auto-index" rather than "upload aborts".

Pocket-scope routing arrives in Stage 3.E: the listener will check
``event.data.get("pocket_id")`` and route into ``pocket:{id}`` when set.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ee.cloud._core.realtime.bus import get_bus
from ee.cloud._core.realtime.events import Event, FileReady

logger = logging.getLogger(__name__)


async def index_uploaded_file(event: Event) -> None:
    """Resolve the file, extract via the chain, ingest into workspace KB.

    The signature accepts the base ``Event`` to satisfy the bus's
    ``Handler`` protocol. We only ever subscribe this to ``file.ready`` so
    the runtime type is always :class:`FileReady` — but typing it loosely
    here keeps mypy happy without an ``# type: ignore`` at the bus
    registration site.
    """
    data = event.data or {}
    workspace_id = data.get("workspace_id") or data.get("workspace")
    file_id = data.get("file_id")
    filename = data.get("filename") or "upload"
    mime = data.get("mime") or "application/octet-stream"
    storage_key = data.get("storage_key")

    if not workspace_id or not file_id:
        logger.debug(
            "FileReady missing workspace_id or file_id; skipping index "
            "(workspace_id=%r, file_id=%r)",
            workspace_id,
            file_id,
        )
        return

    storage_path = _resolve_storage_path(storage_key)
    if storage_path is None:
        # Remote adapters (S3, GCS) don't expose a local path. Stage 1.B is
        # local-disk only; Phase 2 wires download_to_local_temp for remote.
        logger.info(
            "skipping KB index: no local path for file_id=%s storage_key=%r",
            file_id,
            storage_key,
        )
        return

    try:
        from ee.cloud.extraction import build_chain
        from pocketpaw.config import get_settings

        chain = build_chain(get_settings())
        result = await chain.run(storage_path, mime)
    except Exception:
        logger.exception("extraction failed for file_id=%s", file_id)
        return

    text = (result.text or "").strip()
    if not text:
        logger.info(
            "extracted empty text for file_id=%s; skipping KB ingest", file_id
        )
        return

    try:
        from ee.cloud.agents.knowledge import KnowledgeService

        await KnowledgeService.ingest_text_to_scope(
            scope=f"workspace:{workspace_id}",
            text=text,
            source=filename,
        )
    except Exception:
        logger.exception("KB ingest failed for file_id=%s", file_id)


def _resolve_storage_path(storage_key: str | None) -> Path | None:
    """Look up the local on-disk path for the stored blob.

    Returns ``None`` when the adapter doesn't expose a local path (remote
    storage) or when the EE upload singletons can't be reached (test
    contexts that don't mount the upload router).
    """
    if not storage_key:
        return None
    try:
        from ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER.local_path(storage_key)
    except Exception:
        logger.exception(
            "local_path lookup failed for storage_key=%r", storage_key
        )
        return None


def register_upload_listeners() -> None:
    """Wire the upload subscribers into the bus.

    Called once during ``mount_cloud`` after ``init_realtime`` has installed
    the singleton bus. Idempotent only at the framework level — calling
    twice would register the same handler twice. The bootstrap path calls
    it exactly once.
    """
    bus = get_bus()
    bus.subscribe(FileReady.EVENT_TYPE, index_uploaded_file)


__all__ = ["index_uploaded_file", "register_upload_listeners"]
