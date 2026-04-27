"""Wire DTOs for the chat module — re-exports from ``schemas.py``.

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
]
