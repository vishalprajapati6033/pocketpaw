"""LiveKit service — room management, token generation, call lifecycle.

Uses the ``livekit-api`` Python SDK to talk to LiveKit Cloud.
Requires ``LIVEKIT_URL``, ``LIVEKIT_API_KEY``, ``LIVEKIT_API_SECRET``
environment variables.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from livekit.api import AccessToken, LiveKitAPI, VideoGrants
from livekit.protocol.room import (
    CreateRoomRequest,
    DeleteRoomRequest,
    ListParticipantsRequest,
    ListRoomsRequest,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Active meeting agents registry
# ---------------------------------------------------------------------------

_active_agents: dict[str, "CallMeetingAgent"] = {}

def _get_agent(group_id: str) -> "CallMeetingAgent | None":
    """Get the active meeting agent for a group, if any."""
    return _active_agents.get(group_id)

# ---------------------------------------------------------------------------
# Configuration — read from environment
# ---------------------------------------------------------------------------

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

# System agent used for bot posting
CALL_BOT_USER_ID = "__livekit_call_bot__"


def _ensure_configured() -> None:
    """Raise if LiveKit credentials are missing."""
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise RuntimeError(
            "LiveKit is not configured. Set LIVEKIT_URL, LIVEKIT_API_KEY, "
            "and LIVEKIT_API_SECRET environment variables."
        )


def room_name_for_group(group_id: str) -> str:
    """Build a deterministic LiveKit room name for a group."""
    return f"group-call-{group_id}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_room(group_id: str) -> dict[str, Any]:
    """Create a LiveKit room for a group call.

    Returns room metadata including the room name and the admin token.
    The room is named ``group-call-{group_id}`` for deterministic lookup.
    Automatically starts the meeting notes agent for this room.
    """
    _ensure_configured()

    room_name = room_name_for_group(group_id)

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            req = CreateRoomRequest(
                name=room_name,
                empty_timeout=5 * 60,
                max_participants=50,
            )
            room = await lk.room.create_room(req)
            logger.info("Created LiveKit room %s for group %s", room_name, group_id)
        except Exception as exc:
            msg = str(exc).lower()
            # If room already exists, just return the existing one
            if "already exists" in msg:
                logger.info("LiveKit room %s already exists for group %s", room_name, group_id)
            else:
                logger.error("Failed to create LiveKit room: %s", exc)
                raise

    # Generate a subscriber-only token for the call bot (no agent flag
    # so it auto-subscribes to remote tracks for transcription)
    bot_token = await _generate_token(
        room_name, "call-bot",
        can_publish=False,
        can_subscribe=True,
        is_admin=False,
    )

    # Start the meeting notes agent as a background task
    if group_id not in _active_agents:
        from ee.cloud.livekit.agent import CallMeetingAgent
        agent = CallMeetingAgent(
            group_id=group_id,
            room_name=room_name,
            bot_token=bot_token,
            livekit_url=LIVEKIT_URL,
        )
        _active_agents[group_id] = agent
        asyncio.create_task(agent.start())
        logger.info("Started meeting agent for group %s (room %s)", group_id, room_name)

    return {
        "room_name": room_name,
        "group_id": group_id,
        "url": LIVEKIT_URL,
        "bot_token": bot_token,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def generate_participant_token(
    room_name: str,
    identity: str,
    *,
    can_publish: bool = True,
    can_subscribe: bool = True,
    ttl_seconds: int = 3600,
) -> str:
    """Generate a LiveKit access token for a participant.

    Args:
        room_name: The LiveKit room name.
        identity: Participant identity (usually user ID).
        can_publish: Whether the participant can publish audio/video.
        can_subscribe: Whether the participant can subscribe to others.
        ttl_seconds: Token time-to-live.

    Returns:
        A JWT token string for connecting to LiveKit.
    """
    _ensure_configured()
    return await _generate_token(
        room_name,
        identity,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        ttl_seconds=ttl_seconds,
    )


async def end_room(group_id: str) -> dict[str, Any]:
    """End an active call by deleting the LiveKit room.

    When the room is deleted, all participants are disconnected and the
    call bot's cleanup logic (posting meeting notes) is triggered.
    """
    _ensure_configured()

    room_name = room_name_for_group(group_id)

    # Stop the meeting agent first (generates and posts notes)
    agent = _active_agents.pop(group_id, None)
    if agent is not None:
        try:
            await agent.stop()
            logger.info("Stopped meeting agent for group %s", group_id)
        except Exception as exc:
            logger.warning("Error stopping meeting agent for group %s: %s", group_id, exc)

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            req = DeleteRoomRequest(room=room_name)
            await lk.room.delete_room(req)
            logger.info("Deleted LiveKit room %s for group %s", room_name, group_id)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                logger.warning("LiveKit room %s not found (already deleted)", room_name)
            else:
                logger.error("Failed to delete LiveKit room: %s", exc)
                raise

    return {
        "room_name": room_name,
        "group_id": group_id,
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_room_info(group_id: str) -> dict[str, Any] | None:
    """Get the current state of a LiveKit room.

    Returns None if the room doesn't exist (no active call).
    """
    _ensure_configured()

    room_name = room_name_for_group(group_id)

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            list_req = ListRoomsRequest(names=[room_name])
            list_resp = await lk.room.list_rooms(list_req)
            if not list_resp.rooms:
                return None

            parts_req = ListParticipantsRequest(room=room_name)
            parts_resp = await lk.room.list_participants(parts_req)
            participants = parts_resp.participants

            return {
                "room_name": room_name,
                "group_id": group_id,
                "participant_count": len(participants),
                "participants": [
                    {
                        "identity": p.identity,
                        "name": p.name or p.identity,
                        "joined_at": _format_joined_at(p.joined_at),
                        "kind": str(p.kind),
                    }
                    for p in participants
                ],
                "active": True,
            }
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                return None
            logger.error("Failed to get room info: %s", exc)
            raise


# ---------------------------------------------------------------------------
# Meeting notes — called by the agent after call ends
# ---------------------------------------------------------------------------


async def post_meeting_notes_to_group(
    group_id: str,
    transcript: str,
    summary: str,
    action_items: list[str],
    participants: list[str],
    duration_seconds: int,
) -> None:
    """Post meeting notes to a group after a call ends.

    Creates a system message directly (bypassing membership check since the
    call-bot is not a group member) and emits a real-time event so all
    group members see it.
    """
    lines = [
        "📋 **Meeting Notes**",
        "",
        f"**Duration:** {_format_duration(duration_seconds)}",
        f"**Participants:** {', '.join(participants) if participants else 'N/A'}",
        "",
        "**Summary:**",
        summary,
    ]

    if action_items:
        lines.append("")
        lines.append("**Action Items:**")
        for i, item in enumerate(action_items, 1):
            lines.append(f"{i}. {item}")

    if transcript:
        lines.append("")
        lines.append("**Transcript:**")
        # Truncate transcript to stay within message limits
        max_transcript_len = 4000
        transcript_preview = transcript[:max_transcript_len]
        if len(transcript) > max_transcript_len:
            transcript_preview += "\n\n*(transcript truncated)*"
        lines.append(f"```\n{transcript_preview}\n```")

    content = "\n".join(lines)

    try:
        # Use direct message creation to bypass membership check
        from ee.cloud.chat.message_service import _create_group_message_doc
        from ee.cloud.shared.events import event_bus
        from datetime import datetime, timezone

        domain_msg = await _create_group_message_doc(
            group_id=group_id,
            sender=CALL_BOT_USER_ID,
            sender_type="user",
            content=content,
        )
        await event_bus.emit(
            "message.sent",
            {
                "group_id": group_id,
                "message_id": domain_msg.id,
                "sender_id": CALL_BOT_USER_ID,
                "sender_type": "user",
                "content": content,
                "mentions": [],
            },
        )
        logger.info("Posted meeting notes to group %s (message %s)", group_id, domain_msg.id)
    except Exception as exc:
        logger.error("Failed to post meeting notes to group %s: %s", group_id, exc)
        raise


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _generate_token(
    room_name: str,
    identity: str,
    *,
    can_publish: bool = True,
    can_subscribe: bool = True,
    is_admin: bool = False,
    ttl_seconds: int = 3600,
) -> str:
    """Internal: generate a LiveKit access token."""
    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_sources=["microphone", "camera", "screen_share"] if can_publish else None,
        can_update_own_metadata=True,
        agent=is_admin or None,
    )

    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_ttl(timedelta(seconds=ttl_seconds))
        .with_grants(grants)
        .with_name(identity)
    )

    jwt = token.to_jwt()
    return jwt


def _format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration string."""
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def _format_joined_at(joined_at: Any) -> str | None:
    """Format a LiveKit participant's joined_at timestamp to ISO string.

    The livekit-api may return joined_at as a protobuf Timestamp (with
    ``ToDatetime()``) or as a plain Unix timestamp (int/float).  Handle both.
    """
    if joined_at is None:
        return None
    if hasattr(joined_at, "ToDatetime"):
        return joined_at.ToDatetime().isoformat()
    if isinstance(joined_at, (int, float)):
        return datetime.fromtimestamp(joined_at, tz=timezone.utc).isoformat()
    return str(joined_at)
