# Refactored: Split from service.py — contains MessageService class and message-related
# helper functions. Added create_agent_message() static method for use by agent_bridge
# instead of creating Message documents directly.

"""Chat domain — message business logic (CRUD, reactions, threads, pins, search)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import cast

from beanie import PydanticObjectId

from ee.cloud.chat.group_service import (
    _get_group_or_404,
    _require_can_post,
    _require_group_admin,
    _require_group_member,
)
from ee.cloud.chat.schemas import (
    EditMessageRequest,
    SendMessageRequest,
)
from ee.cloud.models.message import Attachment, Mention, Message, Reaction
from ee.cloud.models.notification import NotificationSource
from ee.cloud.notifications.service import NotificationService
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    MessageDeleted,
    MessageEdited,
    MessageNew,
    MessageReaction,
    MessageSent,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message_response(msg: Message) -> dict:
    """Convert a Message document to a frontend-compatible dict."""
    return {
        "_id": str(msg.id),
        "group": msg.group,
        "sender": msg.sender,
        "senderType": msg.sender_type,
        "agent": msg.agent,
        "content": msg.content,
        "mentions": [m.model_dump() for m in msg.mentions],
        "replyTo": msg.reply_to,
        "attachments": [a.model_dump() for a in msg.attachments],
        "reactions": [r.model_dump() for r in msg.reactions],
        "edited": msg.edited,
        "editedAt": iso_utc(msg.edited_at),
        "deleted": msg.deleted,
        "createdAt": iso_utc(msg.createdAt),
    }


async def _get_group_message_or_404(message_id: str) -> Message:
    """Load a non-deleted group-context message or raise NotFound.

    Pocket-context messages are not addressable via the group chat routes —
    if the id resolves to a pocket row it's treated as not found.
    """
    msg = await Message.get(PydanticObjectId(message_id))
    if not msg or msg.deleted or msg.context_type != "group" or not msg.group:
        raise NotFound("message", message_id)
    return msg


# ---------------------------------------------------------------------------
# MessageService
# ---------------------------------------------------------------------------


class MessageService:
    """Stateless service for message business logic."""

    @staticmethod
    async def send_message(group_id: str, user_id: str, body: SendMessageRequest) -> dict:
        """Send a message to a group.

        Verifies membership, checks group is not archived, creates the
        Message document, emits a ``message.sent`` event, and updates
        the group's last_message_at / message_count.
        """
        group = await _get_group_or_404(group_id)
        _require_can_post(group, user_id)

        if group.archived:
            raise Forbidden("group.archived", "Cannot send messages to an archived group")

        mentions = [Mention(**m) for m in body.mentions]
        attachments = [Attachment(**a) for a in body.attachments]

        msg = Message(
            context_type="group",
            group=group_id,
            sender=user_id,
            sender_type="user",
            content=body.content,
            mentions=mentions,
            reply_to=body.reply_to,
            attachments=attachments,
        )
        await msg.insert()

        # Update group stats
        group.last_message_at = msg.createdAt
        group.message_count += 1
        await group.save()

        response = _message_response(msg)

        await event_bus.emit(
            "message.sent",
            {
                "group_id": group_id,
                "message_id": str(msg.id),
                "sender_id": user_id,
                "sender_type": "user",
                "content": body.content,
                "mentions": body.mentions,
                "workspace_id": group.workspace,
            },
        )

        # Realtime fan-out: message.new to the group (sender excluded via
        # AudienceResolver reading data["sender"]), message.sent ack to the
        # sender only (keyed by data["sender_id"]).
        await emit(MessageNew(data={**response, "group_id": group_id}))
        await emit(MessageSent(data={**response, "group_id": group_id, "sender_id": user_id}))

        # Derive mention notifications. Each @user mention in the payload
        # creates a Notification row and emits notification.new (except for
        # self-mentions).
        group_name = getattr(group, "name", "") or ""
        for mention in body.mentions or []:
            if not isinstance(mention, dict) or mention.get("type") != "user":
                continue
            target = mention.get("id")
            if not target or target == user_id:
                continue
            await NotificationService.create(
                workspace_id=str(group.workspace),
                recipient=target,
                kind="mention",
                title=f"You were mentioned in #{group_name}"
                if group_name
                else "You were mentioned",
                body=body.content[:200],
                source=NotificationSource(
                    type="message",
                    id=str(msg.id),
                    pocket_id=None,
                ),
            )

        return response

    @staticmethod
    async def create_agent_message(
        group_id: str,
        agent_id: str,
        content: str,
        attachments: list[Attachment] | None = None,
    ) -> Message:
        """Create a message from an agent in a group.

        Used by agent_bridge to persist agent responses instead of creating
        Message documents directly. Returns the persisted Message document.
        """
        msg = Message(
            context_type="group",
            group=group_id,
            sender=None,
            sender_type="agent",
            agent=agent_id,
            content=content,
            attachments=attachments or [],
        )
        await msg.insert()

        # Update group stats
        group = await _get_group_or_404(group_id)
        group.last_message_at = msg.createdAt
        group.message_count += 1
        await group.save()

        return msg

    @staticmethod
    async def edit_message(message_id: str, user_id: str, body: EditMessageRequest) -> dict:
        """Edit a message. Author only, and the author must still be able to post."""
        msg = await _get_group_message_or_404(message_id)

        if msg.sender != user_id:
            raise Forbidden("message.not_author", "Only the message author can edit it")

        # Defense-in-depth: if the author's role has been downgraded to view,
        # block edits even though they authored the message.
        group = await _get_group_or_404(cast(str, msg.group))
        _require_can_post(group, user_id)

        msg.content = body.content
        msg.edited = True
        msg.edited_at = datetime.now(UTC)
        await msg.save()

        await emit(
            MessageEdited(
                data={
                    "message_id": str(msg.id),
                    "group_id": cast(str, msg.group),
                    "content": msg.content,
                    "edited_at": str(msg.edited_at),
                }
            )
        )

        return _message_response(msg)

    @staticmethod
    async def delete_message(message_id: str, user_id: str) -> None:
        """Soft-delete a message. Author or group owner can delete."""
        msg = await _get_group_message_or_404(message_id)

        if msg.sender != user_id:
            # Check if user is the group owner
            group = await _get_group_or_404(cast(str, msg.group))
            if group.owner != user_id:
                raise Forbidden(
                    "message.not_authorized",
                    "Only the author or group owner can delete this message",
                )

        msg.deleted = True
        await msg.save()

        await emit(
            MessageDeleted(
                data={
                    "message_id": str(msg.id),
                    "group_id": cast(str, msg.group),
                }
            )
        )

    @staticmethod
    async def toggle_reaction(message_id: str, user_id: str, emoji: str) -> dict:
        """Toggle a reaction on a message.

        If the user already reacted with the given emoji, remove their
        reaction. Otherwise, add it. If the emoji reaction has no users
        left, remove the entire reaction entry.
        """
        msg = await _get_group_message_or_404(message_id)

        # View-only members cannot react
        group = await _get_group_or_404(cast(str, msg.group))
        _require_can_post(group, user_id)

        # Find existing reaction for this emoji
        existing: Reaction | None = None
        for r in msg.reactions:
            if r.emoji == emoji:
                existing = r
                break

        added = True
        if existing is not None:
            if user_id in existing.users:
                # Remove user from this reaction
                existing.users.remove(user_id)
                # Remove the reaction entry entirely if no users left
                if not existing.users:
                    msg.reactions.remove(existing)
                added = False
            else:
                existing.users.append(user_id)
        else:
            msg.reactions.append(Reaction(emoji=emoji, users=[user_id]))

        await msg.save()

        await emit(
            MessageReaction(
                data={
                    "message_id": str(msg.id),
                    "group_id": cast(str, msg.group),
                    "emoji": emoji,
                    "user_id": user_id,
                }
            )
        )

        # Derive reaction notification: only on ADD, and only if the reactor
        # is not the original sender of the message.
        if added and msg.sender and msg.sender != user_id:
            await NotificationService.create(
                workspace_id=str(group.workspace),
                recipient=msg.sender,
                kind="reaction",
                title=f"{emoji} on your message",
                body=(msg.content or "")[:200],
                source=NotificationSource(type="message", id=str(msg.id)),
            )

        return _message_response(msg)

    @staticmethod
    async def get_messages(
        group_id: str,
        user_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Cursor-based paginated messages, newest first.

        Cursor format: ``"{iso_timestamp}|{object_id}"``.
        Fetches ``limit + 1`` to determine ``has_more``.
        Excludes soft-deleted messages.
        """
        group = await _get_group_or_404(group_id)

        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        query: dict = {"context_type": "group", "group": group_id, "deleted": False}

        if cursor:
            parts = cursor.split("|", 1)
            if len(parts) == 2:
                cursor_time = datetime.fromisoformat(parts[0])
                cursor_id = PydanticObjectId(parts[1])
                query["$or"] = [
                    {"createdAt": {"$lt": cursor_time}},
                    {"createdAt": cursor_time, "_id": {"$lt": cursor_id}},
                ]

        messages = (
            await Message.find(query)
            .sort([("createdAt", -1), ("_id", -1)])
            .limit(limit + 1)
            .to_list()
        )

        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        items = [_message_response(m) for m in messages]

        next_cursor: str | None = None
        if has_more and messages:
            last = messages[-1]
            next_cursor = f"{last.createdAt.isoformat()}|{last.id}"

        return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}

    @staticmethod
    async def get_thread(message_id: str, user_id: str) -> list[dict]:
        """Get all replies to a message, sorted ascending by creation time."""
        msg = await _get_group_message_or_404(message_id)

        # Verify user can access the group
        group = await _get_group_or_404(cast(str, msg.group))
        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        replies = (
            await Message.find({"context_type": "group", "reply_to": str(msg.id), "deleted": False})
            .sort([("createdAt", 1)])
            .to_list()
        )
        return [_message_response(r) for r in replies]

    @staticmethod
    async def pin_message(group_id: str, user_id: str, message_id: str) -> None:
        """Pin a message in a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        # Verify message belongs to this group
        msg = await _get_group_message_or_404(message_id)
        if msg.group != group_id:
            raise NotFound("message", message_id)

        if message_id not in group.pinned_messages:
            group.pinned_messages.append(message_id)
            await group.save()

    @staticmethod
    async def unpin_message(group_id: str, user_id: str, message_id: str) -> None:
        """Unpin a message from a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        if message_id not in group.pinned_messages:
            raise NotFound("pinned_message", message_id)

        group.pinned_messages.remove(message_id)
        await group.save()

    @staticmethod
    async def search_messages(group_id: str, user_id: str, query: str) -> list[dict]:
        """Search messages by content using regex. Limited to 50 results."""
        group = await _get_group_or_404(group_id)

        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        # Escape regex special characters for safe search
        escaped = re.escape(query)
        messages = (
            await Message.find(
                {
                    "context_type": "group",
                    "group": group_id,
                    "deleted": False,
                    "content": {"$regex": escaped, "$options": "i"},
                }
            )
            .sort([("createdAt", -1)])
            .limit(50)
            .to_list()
        )
        return [_message_response(m) for m in messages]
