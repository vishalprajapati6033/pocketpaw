"""UnreadService — per-user unread counts across joined groups.

Paired with the ReadState model. Unread for a group is the number of
messages with _id > last_read_message_id; mention_unread is the cached
counter on the ReadState row.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ee.cloud.models.group import Group
from ee.cloud.models.message import Message
from ee.cloud.models.read_state import ReadState


async def _list_member_groups(user_id: str, workspace_id: str) -> list[Group]:
    return await Group.find(
        {"workspace": workspace_id, "archived": False, "members": user_id}
    ).to_list()


async def _get_read_state(user_id: str, group_id: str) -> ReadState | None:
    return await ReadState.find_one({"user": user_id, "group": group_id})


async def _count_messages_after(group_id: str, last_message_id: str) -> int:
    """Count group messages with _id greater than last_message_id.

    ObjectIds sort monotonically by creation time, so $gt on _id works as
    an ordered cursor without a separate timestamp field.
    """
    from beanie import PydanticObjectId

    try:
        after = PydanticObjectId(last_message_id)
    except Exception:
        return 0

    return await Message.find(
        {
            "context_type": "group",
            "group": group_id,
            "_id": {"$gt": after},
            "deleted": False,
        }
    ).count()


class UnreadService:
    @staticmethod
    async def list_unreads(user_id: str, workspace_id: str) -> list[dict]:
        """For each group the user is a member of, return
        ``{group_id, unread, mention_unread}``."""
        groups = await _list_member_groups(user_id, workspace_id)
        out: list[dict] = []
        for group in groups:
            state = await _get_read_state(user_id, str(group.id))
            if state is None:
                unread = group.message_count
                mention_unread = 0
            else:
                unread = await _count_messages_after(str(group.id), state.last_read_message_id)
                mention_unread = state.mention_unread
            out.append(
                {"group_id": str(group.id), "unread": unread, "mention_unread": mention_unread}
            )
        return out

    @staticmethod
    async def mark_read(user_id: str, group_id: str, last_message_id: str) -> None:
        """Upsert read state for (user, group). Resets mention_unread to 0."""
        state = await ReadState.find_one({"user": user_id, "group": group_id})
        now = datetime.now(timezone.utc)
        if state is None:
            await ReadState(
                user=user_id,
                group=group_id,
                last_read_message_id=last_message_id,
                mention_unread=0,
            ).insert()
            return
        state.last_read_message_id = last_message_id
        state.mention_unread = 0
        state.last_read_at = now
        await state.save()

    @staticmethod
    async def bump_mention(user_id: str, group_id: str) -> None:
        """Increment mention_unread for (user, group). Creates the row if
        missing with an empty last_read_message_id."""
        state = await ReadState.find_one({"user": user_id, "group": group_id})
        if state is None:
            await ReadState(
                user=user_id,
                group=group_id,
                last_read_message_id="",
                mention_unread=1,
            ).insert()
            return
        state.mention_unread += 1
        await state.save()
