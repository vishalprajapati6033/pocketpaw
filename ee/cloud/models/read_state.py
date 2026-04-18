"""ReadState — per-(user, group) last-read marker for unread computation.

One row per (user, group) pair; updated on read.ack WS events. Paired
with the group's message_count to derive unread counts without count queries.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field

from ee.cloud.models.base import TimestampedDocument


class ReadState(TimestampedDocument):
    user: str
    group: str
    last_read_message_id: str
    last_read_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mention_unread: int = 0

    class Settings:
        name = "read_states"
        indexes = [
            [("user", 1), ("group", 1)],
        ]
