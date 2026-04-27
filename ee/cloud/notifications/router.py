"""Notifications REST router.

Refactored in Phase 2 of the cloud-restructure. The router now:
- Uses ``Depends(request_context)`` to obtain the typed RequestContext.
- Uses ``Depends(get_notification_service)`` so the service (and the
  underlying repository) can be swapped in tests via
  ``app.dependency_overrides``.
- Returns Pydantic ``NotificationOut`` DTOs at the boundary; FastAPI
  serializes to JSON. The wire shape matches the legacy `_to_wire`
  output byte-for-byte (verified by golden-response tests).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.notifications.dto import NotificationOut, notification_to_dto
from ee.cloud.notifications.repositories import (
    INotificationRepository,
    get_default_repository,
)
from ee.cloud.notifications.service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def get_notification_service(
    repo: INotificationRepository = Depends(get_default_repository),
) -> NotificationService:
    """FastAPI dep — builds a NotificationService against the default
    Mongo repo. Tests override either this or `get_default_repository`."""
    return NotificationService(repo)


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
    service: NotificationService = Depends(get_notification_service),
) -> list[NotificationOut]:
    notes = await service.list_for_user(ctx.user_id, unread=unread, limit=limit)
    return [notification_to_dto(n) for n in notes]


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    ctx: RequestContext = Depends(request_context),
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    await service.mark_read(notification_id, ctx.user_id)
    return {"ok": True}


@router.post("/clear")
async def clear_all(
    ctx: RequestContext = Depends(request_context),
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    count = await service.clear_all(ctx.user_id)
    return {"cleared": count}
