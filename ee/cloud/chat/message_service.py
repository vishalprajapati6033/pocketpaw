# Refactored: Split from service.py — contains MessageService class and message-related
# helper functions. Added create_agent_message() static method for use by agent_bridge
# instead of creating Message documents directly.
# 2026-04-19: ``message.sent`` event now carries ``attachments`` so agent_bridge
# can surface filename/mime/size into the channel agent prompt (fixes silent
# attachment drop on the channel path — DM path already had this).
# 2026-04-19 (Cluster E sub-PR 2): added `search_workspace_messages` which
# scopes to groups the caller is a member of and to public/channel rooms in
# the workspace, escapes the query with re.escape, and caps at 100 hits.

"""Chat domain — message business logic (CRUD, reactions, threads, pins, search)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import cast

from beanie import PydanticObjectId

from ee.cloud.chat.group_service import (
    _get_group_or_404,
    _require_can_post,
    _require_group_member,
)
from ee.cloud.chat.schemas import (
    EditMessageRequest,
    SendMessageRequest,
)
from ee.cloud.chat.unread_service import UnreadService
from ee.cloud.models.message import Attachment, Message
from ee.cloud.models.notification import NotificationSource
from ee.cloud.notifications import service as notifications_service
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    MessageDeleted,
    MessageEdited,
    MessageNew,
    MessageReaction,
    MessageSent,
    UnreadUpdate,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REPLY_PREVIEW_CHARS = 140


def _reply_preview(parent: Message | None) -> dict | None:
    """Build a small preview payload for an inline reply quote.

    Rendered as the small quote bubble above a reply message. We keep the
    text short so history payloads don't balloon for channels with lots
    of replies. ``None`` when the parent is missing or soft-deleted — the
    FE falls back to a "message deleted" placeholder.
    """
    if parent is None or parent.deleted:
        return None
    snippet = (parent.content or "")[:_REPLY_PREVIEW_CHARS]
    return {
        "id": str(parent.id),
        "content": snippet,
        "sender": parent.sender,
        "senderType": parent.sender_type,
        "agent": parent.agent,
    }


def _message_response(msg: Message, *, parent: Message | None = None) -> dict:
    """Convert a Message document to a frontend-compatible dict.

    When ``parent`` is supplied (because the caller already fetched it,
    or prefetched a batch for a list view), a ``replyPreview`` field is
    included so the FE can render the inline quote without a second
    fetch. For replies where the parent isn't resolvable (deleted, or
    caller chose not to fetch), ``replyPreview`` is ``None``.
    """
    return {
        "_id": str(msg.id),
        "group": msg.group,
        "sender": msg.sender,
        "senderType": msg.sender_type,
        "agent": msg.agent,
        "content": msg.content,
        "mentions": [m.model_dump() for m in msg.mentions],
        "replyTo": msg.reply_to,
        "replyPreview": _reply_preview(parent) if msg.reply_to else None,
        "threadCount": msg.thread_count,
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


async def _get_group_message_domain_or_404(message_id: str):
    """Same as ``_get_group_message_or_404`` but returns the domain
    ``Message`` value object via the repository. Use this for new code
    paths that just need to read fields (not mutate)."""
    from ee.cloud.chat.repositories import get_message_repository

    msg = await get_message_repository().get(message_id)
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

        Phase 10: routes through ``IMessageRepository.create_group_message``
        + ``IGroupRepository.bump_message_stats``. The membership check
        still loads the Beanie group via ``_get_group_or_404`` because
        the helper is shared with non-migrated paths.
        """
        from ee.cloud.chat.dto import message_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository, get_message_repository

        group = await _get_group_or_404(group_id)
        _require_can_post(group, user_id)

        if group.archived:
            raise Forbidden("group.archived", "Cannot send messages to an archived group")

        msg_repo = get_message_repository()
        domain_msg = await msg_repo.create_group_message(
            group_id=group_id,
            sender=user_id,
            sender_type="user",
            content=body.content,
            mentions=body.mentions,
            attachments=body.attachments,
            reply_to=body.reply_to,
        )

        # Atomic last_message_at / message_count bump via the group repo.
        bumped_at = domain_msg.created_at or datetime.now(UTC)
        await get_group_repository().bump_message_stats(group_id, last_message_at=bumped_at)

        # Resolve the reply parent once so we can both embed a preview in
        # the response and keep replies rendering inline in the main feed.
        # The previous implementation also bumped ``parent.thread_count``
        # and emitted a ``ThreadReply`` event — we've moved to inline
        # quoted replies (Telegram-style), so neither is needed: the
        # parent's counter isn't displayed anywhere and the reply fans out
        # via ``MessageNew`` like any other message.
        parent_domain = await msg_repo.get(body.reply_to) if body.reply_to else None
        if parent_domain is not None and parent_domain.context_type != "group":
            parent_domain = None

        response = message_to_wire_dict(domain_msg, parent=parent_domain)

        # Thread reply-target metadata into the event so the agent bridge can
        # decide whether an ``auto``-mode agent should respond. A reply aimed
        # at a human is a directed side-conversation and shouldn't summon
        # auto agents; a reply aimed at an agent is a follow-up and should
        # go through the normal respond-mode logic.
        reply_meta: dict = {}
        if body.reply_to:
            reply_meta["reply_to"] = body.reply_to
            if parent_domain is not None:
                reply_meta["reply_to_sender_type"] = parent_domain.sender_type
                reply_meta["reply_to_agent_id"] = parent_domain.agent

        await event_bus.emit(
            "message.sent",
            {
                "group_id": group_id,
                "message_id": domain_msg.id,
                "sender_id": user_id,
                "sender_type": "user",
                "content": body.content,
                "mentions": body.mentions,
                # Attachments ride on the event so ``agent_bridge`` can inject
                # filename / mime / size into the agent prompt. Mirrors the DM
                # path's file-awareness contract; raw dicts match
                # ``body.attachments``'s shape and downstream handlers no-op on
                # unknown keys.
                "attachments": body.attachments,
                "workspace_id": group.workspace,
                **reply_meta,
            },
        )

        # Realtime fan-out: message.new to the group (sender excluded via
        # AudienceResolver reading data["sender"]), message.sent ack to the
        # sender only (keyed by data["sender_id"]). Inline replies share
        # the same ``MessageNew`` channel as top-level messages — the quote
        # bubble is rendered client-side from ``replyPreview``.
        await emit(MessageNew(data={**response, "group_id": group_id}))
        await emit(MessageSent(data={**response, "group_id": group_id, "sender_id": user_id}))

        # Unread badge sync — every non-sender member receives a delta so their
        # client can increment the sidebar counter without a full /unreads refetch.
        # Concurrent emit() so a 1000-member channel isn't 999 sequential awaits
        # on the sender's request path.
        unread_tasks = [
            emit(UnreadUpdate(data={"group_id": group_id, "user_id": member, "delta": 1}))
            for member in group.members
            if member != user_id
        ]
        if unread_tasks:
            await asyncio.gather(*unread_tasks)

        # Derive mention notifications. Dedupe recipients across multiple
        # mentions in the same message. Broadcast types (@here/@channel/@everyone)
        # target every non-sender member. User mentions add the specific user.
        group_name = getattr(group, "name", "") or ""
        broadcast_types = {"here", "channel", "everyone"}
        recipients: set[str] = set()

        for mention in body.mentions or []:
            if not isinstance(mention, dict):
                continue
            mtype = mention.get("type")
            if mtype == "user":
                target = mention.get("id")
                if target and target != user_id:
                    recipients.add(target)
            elif mtype in broadcast_types:
                for member in group.members:
                    if member != user_id:
                        recipients.add(member)
            # "agent" and "channel_ref" skip — not a user notification trigger.

        async def _fan_out_mention(target: str) -> None:
            await notifications_service.create(
                workspace_id=str(group.workspace),
                recipient=target,
                kind="mention",
                title=(
                    f"You were mentioned in #{group_name}" if group_name else "You were mentioned"
                ),
                body=body.content[:200],
                source=NotificationSource(
                    type="message",
                    id=domain_msg.id,
                    pocket_id=None,
                ),
            )
            await UnreadService.bump_mention(target, group_id)

        if recipients:
            await asyncio.gather(*(_fan_out_mention(t) for t in recipients))

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
        Message documents directly. Returns the persisted Beanie Message
        document for legacy callers; new code should consume the domain
        ``Message`` value object instead.

        Phase 10: routes through ``IMessageRepository.create_group_message``
        + ``IGroupRepository.bump_message_stats``. The Beanie doc is
        re-fetched to satisfy the legacy return type.
        """
        from ee.cloud.chat.repositories import get_group_repository, get_message_repository

        attachment_dicts = (
            [a.model_dump() if hasattr(a, "model_dump") else dict(a) for a in attachments or []]
            if attachments
            else None
        )
        domain_msg = await get_message_repository().create_group_message(
            group_id=group_id,
            sender=None,
            sender_type="agent",
            agent=agent_id,
            content=content,
            attachments=attachment_dicts,
        )
        bumped_at = domain_msg.created_at or datetime.now(UTC)
        await get_group_repository().bump_message_stats(group_id, last_message_at=bumped_at)
        return await Message.get(PydanticObjectId(domain_msg.id))

    @staticmethod
    async def edit_message(message_id: str, user_id: str, body: EditMessageRequest) -> dict:
        """Edit a message. Author only, and the author must still be able to post.

        Phase 10: routes through ``IMessageRepository.edit_content``.
        Author + can-post check still goes through the legacy Beanie
        helpers because they are shared with non-migrated paths.
        """
        from ee.cloud.chat.dto import message_to_wire_dict
        from ee.cloud.chat.repositories import get_message_repository

        msg = await _get_group_message_domain_or_404(message_id)

        if msg.sender != user_id:
            raise Forbidden("message.not_author", "Only the message author can edit it")

        # Defense-in-depth: if the author's role has been downgraded to view,
        # block edits even though they authored the message.
        group = await _get_group_or_404(cast(str, msg.group))
        _require_can_post(group, user_id)

        edited_at = datetime.now(UTC)
        domain_msg = await get_message_repository().edit_content(
            message_id, body.content, edited_at=edited_at
        )

        await emit(
            MessageEdited(
                data={
                    "message_id": domain_msg.id,
                    "group_id": cast(str, domain_msg.group),
                    "content": domain_msg.content,
                    "edited_at": str(domain_msg.edited_at),
                }
            )
        )
        return message_to_wire_dict(domain_msg)

    @staticmethod
    async def delete_message(message_id: str, user_id: str) -> None:
        """Soft-delete a message. Author or group owner can delete.

        Phase 10: routes through ``IMessageRepository.soft_delete``.
        """
        from ee.cloud.chat.repositories import get_message_repository

        msg = await _get_group_message_domain_or_404(message_id)

        if msg.sender != user_id:
            group = await _get_group_or_404(cast(str, msg.group))
            if group.owner != user_id:
                raise Forbidden(
                    "message.not_authorized",
                    "Only the author or group owner can delete this message",
                )

        await get_message_repository().soft_delete(message_id)

        await emit(
            MessageDeleted(
                data={
                    "message_id": msg.id,
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

        Phase 10: routes through ``IMessageRepository.toggle_reaction``.
        """
        from ee.cloud.chat.dto import message_to_wire_dict
        from ee.cloud.chat.repositories import get_message_repository

        msg = await _get_group_message_domain_or_404(message_id)

        # View-only members cannot react
        group = await _get_group_or_404(cast(str, msg.group))
        _require_can_post(group, user_id)

        domain_msg, added = await get_message_repository().toggle_reaction(
            message_id, user_id, emoji
        )

        await emit(
            MessageReaction(
                data={
                    "message_id": domain_msg.id,
                    "group_id": cast(str, domain_msg.group),
                    "emoji": emoji,
                    "user_id": user_id,
                }
            )
        )

        # Derive reaction notification: only on ADD, and only if the reactor
        # is not the original sender of the message.
        if added and domain_msg.sender and domain_msg.sender != user_id:
            await notifications_service.create(
                workspace_id=str(group.workspace),
                recipient=domain_msg.sender,
                kind="reaction",
                title=f"{emoji} on your message",
                body=(domain_msg.content or "")[:200],
                source=NotificationSource(type="message", id=domain_msg.id),
            )

        return message_to_wire_dict(domain_msg)

    @staticmethod
    async def get_messages(
        group_id: str,
        user_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Cursor-based paginated messages, newest first.

        Phase 10: routes through ``IMessageRepository.list_for_group_paged``
        + ``IMessageRepository.get_many`` for the parent-prefetch. Cursor
        encoding stays on the service (it's a wire concern).

        Cursor format: ``"{iso_timestamp}|{object_id}"``.
        Fetches ``limit + 1`` to determine ``has_more``.
        Excludes soft-deleted messages.
        """
        from ee.cloud.chat.dto import message_to_wire_dict
        from ee.cloud.chat.repositories import get_message_repository

        group = await _get_group_or_404(group_id)
        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        # Decode cursor (wire concern, stays on service)
        before_time: datetime | None = None
        before_id: str | None = None
        if cursor:
            parts = cursor.split("|", 1)
            if len(parts) == 2:
                try:
                    before_time = datetime.fromisoformat(parts[0])
                    before_id = parts[1]
                except ValueError:
                    before_time = None
                    before_id = None

        repo = get_message_repository()
        messages = await repo.list_for_group_paged(
            group_id,
            before_time=before_time,
            before_id=before_id,
            limit=limit + 1,
        )
        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        # Batch-fetch reply parents in a single round-trip
        parent_ids = [m.reply_to for m in messages if m.reply_to]
        parent_domains = await repo.get_many(parent_ids) if parent_ids else []
        parents_by_id = {p.id: p for p in parent_domains}

        items = [
            message_to_wire_dict(m, parent=parents_by_id.get(m.reply_to) if m.reply_to else None)
            for m in messages
        ]

        next_cursor: str | None = None
        if has_more and messages:
            last = messages[-1]
            if last.created_at is not None:
                next_cursor = f"{last.created_at.isoformat()}|{last.id}"

        return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}

    @staticmethod
    async def get_thread(message_id: str, user_id: str) -> list[dict]:
        """Get all replies to a message, sorted ascending by creation time.

        Phase 10: routes through ``IMessageRepository.list_replies``.
        Auth check still hits the Beanie ``Group`` doc directly because
        the group-membership predicate isn't on the domain Group yet.
        """
        from ee.cloud.chat.dto import message_to_wire_dict
        from ee.cloud.chat.repositories import get_message_repository

        msg = await _get_group_message_domain_or_404(message_id)
        group = await _get_group_or_404(cast(str, msg.group))
        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        replies = await get_message_repository().list_replies(msg.id)
        return [message_to_wire_dict(r) for r in replies]

    @staticmethod
    async def pin_message(group_id: str, user_id: str, message_id: str) -> None:
        """Pin a message in a group. Owner only.

        Phase 10: routes through ``IGroupRepository.pin_message`` after
        verifying the message belongs to the group via the message
        repository.
        """
        from ee.cloud.chat.group_service import _require_domain_group_admin
        from ee.cloud.chat.repositories import get_group_repository, get_message_repository

        group_repo = get_group_repository()
        group = await group_repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)

        msg = await get_message_repository().get(message_id)
        if msg is None or msg.group != group_id:
            raise NotFound("message", message_id)

        await group_repo.pin_message(group_id, message_id)

    @staticmethod
    async def unpin_message(group_id: str, user_id: str, message_id: str) -> None:
        """Unpin a message from a group. Owner only.

        Phase 10: routes through ``IGroupRepository.unpin_message``.
        """
        from ee.cloud.chat.group_service import _require_domain_group_admin
        from ee.cloud.chat.repositories import get_group_repository

        group_repo = get_group_repository()
        group = await group_repo.get(group_id)
        if group is None:
            raise NotFound("group", group_id)
        _require_domain_group_admin(group, user_id)

        result = await group_repo.unpin_message(group_id, message_id)
        if result is None:
            raise NotFound("pinned_message", message_id)

    @staticmethod
    async def search_messages(group_id: str, user_id: str, query: str) -> list[dict]:
        """Search messages by content using regex. Limited to 50 results.

        Phase 10: routes through ``IMessageRepository.search_in_group``
        and the domain → wire mapper, demonstrating the new repository
        abstraction is callable end-to-end. Auth/membership check still
        runs against the Beanie ``Group`` doc directly because the group
        repository's ``get`` returns a domain entity that doesn't expose
        the ``_require_group_member`` predicate yet.
        """
        from ee.cloud.chat.dto import message_to_wire_dict
        from ee.cloud.chat.repositories import get_message_repository

        group = await _get_group_or_404(group_id)
        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        # Repository handles regex escaping + the Mongo query construction;
        # service stays focused on auth and shape.
        domain_messages = await get_message_repository().search_in_group(group_id, query, limit=50)
        return [message_to_wire_dict(m) for m in domain_messages]

    @staticmethod
    async def search_workspace_messages(
        workspace_id: str,
        user_id: str,
        query: str,
        limit: int = 50,
    ) -> list[dict]:
        """Full-text-ish search across the workspace, honoring per-group scope.

        The caller sees hits only from:
          * public / channel groups in ``workspace_id`` (any workspace member
            can read these), plus
          * private / dm groups where the caller is an explicit member.

        Agent DMs are private groups with one member, so this also catches
        the user's own agent conversations.

        Phase 10: routes through ``IGroupRepository.list_visible_in_workspace``
        + ``IMessageRepository.search_in_groups``. The query is escaped
        inside the repo, not here. Capped at 100 results.
        """
        from ee.cloud.chat.dto import message_to_wire_dict
        from ee.cloud.chat.repositories import get_group_repository, get_message_repository

        capped = max(1, min(limit, 100))
        if not query or not query.strip():
            return []

        groups = await get_group_repository().list_visible_in_workspace(workspace_id, user_id)
        group_ids = [g.id for g in groups]
        if not group_ids:
            return []

        messages = await get_message_repository().search_in_groups(
            group_ids, query.strip(), limit=capped
        )
        return [message_to_wire_dict(m) for m in messages]
