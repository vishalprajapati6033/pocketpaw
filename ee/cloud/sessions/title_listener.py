"""Persist backend-generated chat titles into MongoDB.

The pocketpaw message bus emits a ``session_titled`` SystemEvent once per
session (after the first user message) with a Haiku-generated title. The OSS
dashboard consumes it live via SSE, but cloud mode needs to persist the title
to the Session document so it survives a refresh.

This module wires a subscriber at startup; it is a best-effort side-effect
that must never break the chat flow.
"""

from __future__ import annotations

import logging

from pocketpaw.bus import get_message_bus
from pocketpaw.bus.events import SystemEvent

logger = logging.getLogger(__name__)


async def _on_system_event(event: SystemEvent) -> None:
    if event.event_type != "session_titled":
        return
    data = event.data or {}
    session_id = data.get("session_id")
    title = (data.get("title") or "").strip()
    if not session_id or not title:
        logger.info("session_titled event missing session_id/title: %s", data)
        return

    # Lazy imports so a bare ``import`` of this module doesn't require
    # Beanie/Motor to be initialized.
    try:
        from ee.cloud.models.session import Session
        from ee.cloud.realtime.emit import emit
        from ee.cloud.realtime.events import SessionUpdated
    except ImportError:
        logger.warning("ee.cloud models unavailable; skipping title persist")
        return

    try:
        session = await Session.find_one(Session.sessionId == session_id)
    except Exception:
        logger.warning("session lookup failed for %s", session_id, exc_info=True)
        return
    if session is None:
        logger.info("no cloud session for %s; skipping title persist", session_id)
        return
    # Respect user-edited titles. Only overwrite the default placeholder.
    if session.title and session.title not in ("New Chat", "Chat"):
        logger.info(
            "session %s already titled %r; skipping overwrite", session_id, session.title,
        )
        return

    session.title = title
    try:
        await session.save()
    except Exception:
        logger.warning("session title save failed for %s", session_id, exc_info=True)
        return
    logger.info("persisted title for session %s: %r", session_id, title)

    try:
        await emit(
            SessionUpdated(
                data={
                    "session_id": str(session.id),
                    "user_id": session.owner,
                    "title": title,
                }
            )
        )
    except Exception:
        logger.warning("session title emit failed for %s", session_id, exc_info=True)


_subscribed = False


def register() -> None:
    """Idempotently subscribe the title-persistence handler to the bus."""
    global _subscribed
    if _subscribed:
        return
    get_message_bus().subscribe_system(_on_system_event)
    _subscribed = True
    logger.info("Cloud chat-title listener registered")
