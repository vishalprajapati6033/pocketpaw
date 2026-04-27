"""Wire DTOs for the chat module — re-exports from ``schemas.py`` plus
domain → wire mappers that convert ``chat.domain`` value objects to the
legacy wire-format dicts.

Phase 10 keeps ``schemas.py`` as the canonical home for chat-domain
Pydantic models (because chat-unify added many references to it across
agent_router/agent_service/router) and exposes them under ``dto`` for
naming consistency with the rest of the cloud modules. New code should
import from this module:

    from ee.cloud.chat.dto import SendMessageRequest, MessageResponse

A future cleanup pass can flip the canonical home if/when the
chat-unify references are migrated.
"""

from __future__ import annotations

from typing import Any

from ee.cloud._core.time import iso_utc
from ee.cloud.chat.domain import Message
from ee.cloud.chat.schemas import (  # noqa: F401
    AddGroupAgentRequest,
    AddGroupMembersRequest,
    CreateGroupRequest,
    CursorPage,
    EditMessageRequest,
    GroupResponse,
    MessageResponse,
    ReactRequest,
    SendMessageRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
    UpdateMemberRoleRequest,
    WsInbound,
    WsOutbound,
)


def message_to_wire_dict(m: Message, *, parent: Message | None = None) -> dict[str, Any]:
    """Convert a domain ``Message`` to the legacy wire-format dict.

    Byte-equivalent to the existing ``_message_response`` in
    ``message_service.py`` so callers migrating to the repository
    abstraction don't shift the API contract.

    The optional ``parent`` is the message being replied to; when
    supplied, ``replyPreview`` is populated so the FE can render the
    inline quote without a second fetch.
    """
    return {
        "_id": m.id,
        "group": m.group,
        "sender": m.sender,
        "senderType": m.sender_type,
        "agent": m.agent,
        "content": m.content,
        "mentions": [
            {"type": x.type, "id": x.id, "display_name": x.display_name} for x in m.mentions
        ],
        "replyTo": m.reply_to,
        "replyPreview": _reply_preview(parent) if m.reply_to else None,
        "threadCount": m.thread_count,
        "attachments": [
            {"type": a.type, "url": a.url, "name": a.name, "meta": dict(a.meta)}
            for a in m.attachments
        ],
        "reactions": [{"emoji": r.emoji, "users": list(r.users)} for r in m.reactions],
        "edited": m.edited,
        "editedAt": iso_utc(m.edited_at),
        "deleted": m.deleted,
        "createdAt": iso_utc(m.created_at),
    }


_REPLY_PREVIEW_CHARS = 140


def _reply_preview(parent: Message | None) -> dict[str, Any] | None:
    """Build a small preview payload for an inline reply quote.

    Mirrors the helper in ``message_service.py``; kept here so the new
    repository-driven path doesn't need to import from the legacy
    service module.
    """
    if parent is None or parent.deleted:
        return None
    snippet = (parent.content or "")[:_REPLY_PREVIEW_CHARS]
    return {
        "id": parent.id,
        "content": snippet,
        "sender": parent.sender,
        "senderType": parent.sender_type,
        "agent": parent.agent,
    }


__all__ = [
    "AddGroupAgentRequest",
    "AddGroupMembersRequest",
    "CreateGroupRequest",
    "CursorPage",
    "EditMessageRequest",
    "GroupResponse",
    "MessageResponse",
    "ReactRequest",
    "SendMessageRequest",
    "UpdateGroupAgentRequest",
    "UpdateGroupRequest",
    "UpdateMemberRoleRequest",
    "WsInbound",
    "WsOutbound",
    "message_to_wire_dict",
]
