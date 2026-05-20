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


class CloudRouteProvider:
    """`pocketpaw.routes` — mounts the multi-tenant cloud API."""

    def mount(self, app: Any) -> None:
        from pocketpaw_ee.cloud import mount_cloud

        mount_cloud(app)


class CloudLifecycleHook:
    """`pocketpaw.lifecycle` — cloud DB init + admin/workspace seeding +
    chat-title listener registration, run on dashboard startup."""

    async def on_startup(self, app: Any) -> None:
        import logging
        import os

        logger = logging.getLogger(__name__)

        from pocketpaw_ee.cloud.db import init_cloud_db

        mongo_uri = os.environ.get(
            "CLOUD_MONGODB_URI", "mongodb://localhost:27017/paw-enterprise"
        )
        await init_cloud_db(mongo_uri)

        from pocketpaw_ee.cloud.auth.core import (
            ensure_default_agent_all_workspaces,
            seed_admin,
            seed_workspace,
        )

        admin = await seed_admin()
        await seed_workspace(admin)
        # Back-fill the pocketpaw agent for workspaces that predate agent seeding.
        await ensure_default_agent_all_workspaces()

        # Persist Haiku-generated chat titles into MongoDB.
        try:
            from pocketpaw_ee.cloud.sessions.title_listener import (
                register as register_title_listener,
            )

            register_title_listener()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cloud chat-title listener registration failed: %s", exc)

    async def on_shutdown(self, app: Any) -> None:
        # Cloud teardown is handled inside mount_cloud's own shutdown hook.
        return None


class CloudStorageBackend:
    """`pocketpaw.storage_backends` — the EE Mongo-backed upload store."""

    name = "cloud"

    def adapter(self) -> Any:
        from pocketpaw_ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER

    def meta(self) -> Any:
        from pocketpaw_ee.cloud.uploads.router import _META

        return _META
