"""ReadState model — per-(user, group) last-read marker."""

from __future__ import annotations

from datetime import datetime

import pytest


@pytest.mark.asyncio
async def test_read_state_defaults(beanie_memory_db):
    from ee.cloud.models.read_state import ReadState
    rs = ReadState(user="u1", group="g1", last_read_message_id="m1")
    assert rs.mention_unread == 0
    assert isinstance(rs.last_read_at, datetime)
