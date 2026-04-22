"""Pydantic response schemas for the notifications domain."""

from __future__ import annotations

from pydantic import BaseModel


class NotificationResponse(BaseModel):
    id: str
    workspace_id: str
    kind: str
    title: str
    body: str
    source_id: str | None
    read: bool
    created_at: str | None
