"""Notification service — CRUD + realtime fan-out.

Refactored in Phase 2 of the cloud-restructure. The service is now an
instance class that depends on ``INotificationRepository``. Existing
fan-out callers (`chat/message_service.py`, `workspace/service.py`)
call the legacy classmethod facade ``create_default`` etc., which
routes through the configured default repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.dto import notification_to_dto
from ee.cloud.notifications.repositories import (
    INotificationRepository,
    get_default_repository,
)
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    NotificationCleared,
    NotificationNew,
    NotificationRead,
)

if TYPE_CHECKING:
    from ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc


def _to_domain_source(
    src: _NotificationSourceDoc | NotificationSource | None,
) -> NotificationSource | None:
    if src is None:
        return None
    if isinstance(src, NotificationSource):
        return src
    return NotificationSource(type=src.type, id=src.id, pocket_id=src.pocket_id)


class NotificationService:
    """Notifications CRUD + realtime fan-out.

    Construct with a repository for testing or DI; the classmethod facade
    methods (`create_default`, etc.) are provided for legacy callers
    that already use the class as a static namespace.
    """

    def __init__(self, repository: INotificationRepository) -> None:
        self._repo = repository

    async def create(
        self,
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: _NotificationSourceDoc | NotificationSource | None = None,
    ) -> Notification:
        proto = Notification(
            id="",
            workspace_id=workspace_id,
            recipient_id=recipient,
            kind=kind,
            title=title,
            body=body,
            source=_to_domain_source(source),
            read=False,
            created_at=datetime.now(UTC),
        )
        created = await self._repo.create(proto)
        await emit(NotificationNew(data=notification_to_dto(created).model_dump()))
        return created

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        return await self._repo.list_for_user(user_id, unread=unread, limit=limit)

    async def mark_read(self, notification_id: str, user_id: str) -> bool:
        existing = await self._repo.get(notification_id)
        if not existing or existing.recipient_id != user_id:
            return False
        changed = await self._repo.mark_read(notification_id)
        if changed:
            await emit(
                NotificationRead(data={"id": notification_id, "user_id": user_id})
            )
        return changed

    async def clear_all(self, user_id: str) -> int:
        count = await self._repo.clear_unread(user_id)
        await emit(NotificationCleared(data={"user_id": user_id}))
        return count

    # ------------------------------------------------------------------
    # Legacy classmethod facade — preserves call signatures used by
    # chat/message_service.py and workspace/service.py.
    # ------------------------------------------------------------------

    @classmethod
    async def create_default(
        cls,
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: _NotificationSourceDoc | NotificationSource | None = None,
    ) -> Notification:
        return await cls(get_default_repository()).create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind=kind,
            title=title,
            body=body,
            source=source,
        )

    @classmethod
    async def list_for_user_default(
        cls, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[dict]:
        """Legacy wire-shape variant: returns ``list[dict]`` for callers
        that haven't yet adopted the DTO."""
        notes = await cls(get_default_repository()).list_for_user(
            user_id, unread=unread, limit=limit
        )
        return [notification_to_dto(n).model_dump() for n in notes]

    @classmethod
    async def mark_read_default(cls, notification_id: str, user_id: str) -> None:
        await cls(get_default_repository()).mark_read(notification_id, user_id)

    @classmethod
    async def clear_all_default(cls, user_id: str) -> int:
        return await cls(get_default_repository()).clear_all(user_id)
