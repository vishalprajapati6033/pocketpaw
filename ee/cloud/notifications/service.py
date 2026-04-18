"""Notification service — creates, lists, marks read, clears."""

from __future__ import annotations

from beanie import PydanticObjectId

from ee.cloud.models.notification import Notification, NotificationSource
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import NotificationCleared, NotificationNew, NotificationRead
from ee.cloud.shared.time import iso_utc


def _to_wire(n: Notification) -> dict:
    return {
        "id": str(n.id),
        "user_id": n.recipient,
        "workspace_id": n.workspace,
        "kind": n.type,
        "title": n.title,
        "body": n.body,
        "source_id": n.source.id if n.source else None,
        "read": n.read,
        "created_at": iso_utc(getattr(n, "createdAt", None)),
    }


class NotificationService:
    """Stateless CRUD + fan-out for in-app notifications."""

    @staticmethod
    async def create(
        *,
        workspace_id: str,
        recipient: str,
        kind: str,
        title: str,
        body: str = "",
        source: NotificationSource | None = None,
    ) -> Notification:
        notif = Notification(
            workspace=workspace_id,
            recipient=recipient,
            type=kind,
            title=title,
            body=body,
            source=source,
        )
        await notif.insert()
        await emit(NotificationNew(data=_to_wire(notif)))
        return notif

    @staticmethod
    async def list_for_user(user_id: str, *, unread: bool = False, limit: int = 50) -> list[dict]:
        query: dict = {"recipient": user_id}
        if unread:
            query["read"] = False
        cursor = Notification.find(query).sort(-Notification.createdAt).limit(limit)
        return [_to_wire(n) async for n in cursor]

    @staticmethod
    async def mark_read(notif_id: str, user_id: str) -> None:
        notif = await Notification.get(PydanticObjectId(notif_id))
        if not notif or notif.recipient != user_id:
            return
        if notif.read:
            return
        notif.read = True
        await notif.save()
        await emit(NotificationRead(data={"id": notif_id, "user_id": user_id}))

    @staticmethod
    async def clear_all(user_id: str) -> int:
        result = await Notification.find({"recipient": user_id, "read": False}).update_many(
            {"$set": {"read": True}}
        )
        await emit(NotificationCleared(data={"user_id": user_id}))
        return getattr(result, "modified_count", 0)
