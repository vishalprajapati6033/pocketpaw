"""EventBus protocol and in-process implementation.

Services call ``emit(event)`` (see ``emit.py``) which delegates to the active
bus. The default ``InProcessBus`` resolves audiences via ``AudienceResolver``
and fans out through the existing ``ConnectionManager.send_to_user``.

A future ``RedisBus`` (Task 33) will use the same protocol so call sites are
unaffected.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ee.cloud.realtime.audience import AudienceResolver
from ee.cloud.realtime.events import Event

logger = logging.getLogger(__name__)


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...


class InProcessBus:
    """Fan out events to sockets on the same process."""

    def __init__(self, *, resolver: AudienceResolver, conn_manager) -> None:
        self._resolver = resolver
        self._conn = conn_manager

    async def publish(self, event: Event) -> None:
        # WsOutbound is imported lazily: ee.cloud.chat.schemas is the lowest
        # reachable node that also sits on the message-send import chain, so
        # pytest collection orderings that load services before realtime can
        # see a partially-initialised bus if we import at module top. Tested:
        # reverts to ImportError under pytest collection of test_bus.py.
        from ee.cloud.chat.schemas import WsOutbound

        try:
            audience = await self._resolver.audience(event)
        except Exception:
            logger.exception("audience resolution failed for event %s", event.type)
            return
        if not audience:
            return
        payload = WsOutbound(type=event.type, data=event.data)
        for uid in audience:
            try:
                await self._conn.send_to_user(uid, payload)
            except Exception:
                logger.warning("ws send failed; user=%s event=%s", uid, event.type, exc_info=True)


# --- module-level singleton ---------------------------------------------------

_bus: EventBus | None = None


def set_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus


def get_bus() -> EventBus:
    assert _bus is not None, "EventBus not initialized — call init_realtime()"
    return _bus


_resolver: AudienceResolver | None = None


def set_resolver(resolver: AudienceResolver) -> None:
    global _resolver
    _resolver = resolver


def get_resolver() -> AudienceResolver:
    assert _resolver is not None, "AudienceResolver not initialized — call init_realtime()"
    return _resolver
