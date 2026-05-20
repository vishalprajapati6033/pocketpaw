"""Entry-point provider classes for the OSS-EE extension surfaces.

Core (`pocketpaw`) defines the Protocols in `pocketpaw.extensions` and
discovers implementations via `importlib.metadata.entry_points`. This module
collects every `pocketpaw_ee` provider in one place; the entry-points that
point at these classes are declared in `pyproject.toml` (and will migrate to
`ee/pyproject.toml` in Phase 4).

Each provider does its heavy `pocketpaw_ee` imports lazily inside methods so
that merely loading this module — which the registry does on first access —
stays cheap and free of import cycles.
"""

from __future__ import annotations

from typing import Any


class CloudEventBusProvider:
    """`pocketpaw.event_bus` — the process-wide async pub/sub bus."""

    def get_event_bus(self) -> Any:
        from pocketpaw_ee.cloud.shared.events import event_bus

        return event_bus


class CloudEmbeddingProvider:
    """`pocketpaw.embeddings` — KB text/image embedder factory."""

    def build_embedder(self, settings: Any) -> Any:
        from pocketpaw_ee.cloud.embeddings import build_embedder

        return build_embedder(settings)


class MongoMemoryBackendProvider:
    """`pocketpaw.memory_backends` — MongoDB-backed memory store."""

    name = "mongodb"

    def build(self, settings: Any) -> Any:
        from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

        return MongoMemoryStore()


class CloudCapabilityProvider:
    """`pocketpaw.capabilities` — features the cloud product force-enables."""

    def capabilities(self) -> dict[str, bool]:
        from pocketpaw_ee.cloud import features

        return {"chat_titles_enabled": features.chat_titles_enabled()}


class CloudAuthProvider:
    """`pocketpaw.auth` — FastAPI auth dependencies for cloud-mounted routes."""

    def current_optional_user(self) -> Any:
        from pocketpaw_ee.cloud.auth.core import current_optional_user

        return current_optional_user
