"""Notifications REST router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ee.cloud.auth import current_active_user
from ee.cloud.models.user import User
from ee.cloud.notifications.service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("")
async def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_active_user),
) -> list[dict]:
    return await NotificationService.list_for_user(str(user.id), unread=unread, limit=limit)


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    user: User = Depends(current_active_user),
) -> dict:
    await NotificationService.mark_read(notification_id, str(user.id))
    return {"ok": True}


@router.post("/clear")
async def clear_all(user: User = Depends(current_active_user)) -> dict:
    count = await NotificationService.clear_all(str(user.id))
    return {"cleared": count}
