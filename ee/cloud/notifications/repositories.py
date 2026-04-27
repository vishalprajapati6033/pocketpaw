"""Repository for notifications.

Defines the abstract `INotificationRepository` Protocol and a Beanie-
backed `MongoNotificationRepository`. Services depend on the Protocol;
the router DI's the default Mongo implementation. Tests substitute an
in-memory fake.

The Beanie ``Notification`` document and our domain ``Notification`` are
distinct types. The two private converters (`_to_domain`, `_source_to_doc`)
mediate. Beanie generates ObjectIds; the domain entity stores them as
plain strings.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from beanie import PydanticObjectId

from ee.cloud.models.notification import Notification as _NotificationDoc
from ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc
from ee.cloud.notifications.domain import Notification, NotificationSource


def _source_to_domain(
    src: _NotificationSourceDoc | None,
) -> NotificationSource | None:
    if src is None:
        return None
    return NotificationSource(type=src.type, id=src.id, pocket_id=src.pocket_id)


def _source_to_doc(
    src: NotificationSource | _NotificationSourceDoc | None,
) -> _NotificationSourceDoc | None:
    """Accept either domain or doc form, return the doc form for Beanie
    to persist. Callers in chat/workspace pass the doc form today; new
    code passes the domain form."""
    if src is None:
        return None
    if isinstance(src, _NotificationSourceDoc):
        return src
    return _NotificationSourceDoc(type=src.type, id=src.id, pocket_id=src.pocket_id)


def _to_domain(doc: _NotificationDoc) -> Notification:
    return Notification(
        id=str(doc.id),
        workspace_id=doc.workspace,
        recipient_id=doc.recipient,
        kind=doc.type,
        title=doc.title,
        body=doc.body,
        source=_source_to_domain(doc.source),
        read=doc.read,
        # Beanie reads return naive datetimes; TimestampedDocument exposes
        # ``createdAt`` (camelCase Mongo field). Always populated in
        # practice; type:ignore covers the getattr fallback.
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        expires_at=doc.expires_at,
    )


@runtime_checkable
class INotificationRepository(Protocol):
    async def create(self, notification: Notification) -> Notification: ...
    async def get(self, notification_id: str) -> Notification | None: ...
    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]: ...
    async def mark_read(self, notification_id: str) -> bool: ...
    async def clear_unread(self, user_id: str) -> int: ...


class MongoNotificationRepository:
    """Beanie-backed implementation. Services depend on
    `INotificationRepository`; this concrete class is wired by the
    default-repository accessor below."""

    async def create(self, notification: Notification) -> Notification:
        doc = _NotificationDoc(
            workspace=notification.workspace_id,
            recipient=notification.recipient_id,
            type=notification.kind,
            title=notification.title,
            body=notification.body,
            source=_source_to_doc(notification.source),
            read=notification.read,
            expires_at=notification.expires_at,
        )
        await doc.insert()
        return _to_domain(doc)

    async def get(self, notification_id: str) -> Notification | None:
        doc = await _NotificationDoc.get(PydanticObjectId(notification_id))
        return _to_domain(doc) if doc else None

    async def list_for_user(
        self, user_id: str, *, unread: bool = False, limit: int = 50
    ) -> list[Notification]:
        query: dict = {"recipient": user_id}
        if unread:
            query["read"] = False
        # Beanie's query DSL: `-field` = descending sort. Mypy doesn't
        # model this; suppress.
        cursor = (
            _NotificationDoc.find(query)
            .sort(-_NotificationDoc.createdAt)  # type: ignore[operator]
            .limit(limit)
        )
        return [_to_domain(doc) async for doc in cursor]

    async def mark_read(self, notification_id: str) -> bool:
        doc = await _NotificationDoc.get(PydanticObjectId(notification_id))
        if not doc or doc.read:
            return False
        doc.read = True
        await doc.save()
        return True

    async def clear_unread(self, user_id: str) -> int:
        result = await _NotificationDoc.find({"recipient": user_id, "read": False}).update_many(
            {"$set": {"read": True}}
        )
        return getattr(result, "modified_count", 0)


_default: INotificationRepository | None = None


def get_default_repository() -> INotificationRepository:
    """Process-wide default Mongo-backed repository. Constructed lazily so
    importing this module doesn't require a live Mongo connection."""
    global _default
    if _default is None:
        _default = MongoNotificationRepository()
    return _default


def set_default_repository(repo: INotificationRepository) -> None:
    """Override the default repository (used by integration tests)."""
    global _default
    _default = repo


__all__ = [
    "INotificationRepository",
    "MongoNotificationRepository",
    "get_default_repository",
    "set_default_repository",
]
