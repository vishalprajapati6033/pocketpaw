"""Sessions domain — business logic service."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud.models.session import Session
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import SessionCreated, SessionDeleted, SessionUpdated
from ee.cloud.sessions.schemas import (
    CreateSessionRequest,
    UpdateSessionRequest,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

logger = logging.getLogger(__name__)


def _session_response(session: Session) -> dict:
    """Build a frontend-compatible dict from a Session document."""
    return {
        "_id": str(session.id),
        "sessionId": session.sessionId,
        "workspace": session.workspace,
        "owner": session.owner,
        "title": session.title,
        "pocket": session.pocket,
        "group": session.group,
        "agent": session.agent,
        "messageCount": session.messageCount,
        "lastActivity": iso_utc(session.lastActivity),
        "createdAt": iso_utc(session.createdAt),
        "deletedAt": iso_utc(session.deleted_at),
    }


class SessionService:
    """Stateless service encapsulating session business logic."""

    @staticmethod
    async def create(workspace_id: str, user_id: str, body: CreateSessionRequest) -> dict:
        """Create a session, or update if sessionId already exists."""
        sid = body.session_id or f"websocket_{uuid.uuid4().hex[:12]}"

        # If linking to an existing runtime session, check if MongoDB record exists
        if body.session_id:
            existing = await Session.find_one(Session.sessionId == body.session_id)
            if existing:
                # Update the existing record (e.g. add pocket link)
                patched: dict = {}
                if body.pocket_id:
                    existing.pocket = body.pocket_id
                    patched["pocket_id"] = body.pocket_id
                if body.title and body.title != "New Chat":
                    existing.title = body.title
                    patched["title"] = body.title
                await existing.save()

                if patched:
                    await emit(
                        SessionUpdated(
                            data={
                                "session_id": str(existing.id),
                                "user_id": existing.owner,
                                **patched,
                            }
                        )
                    )
                return _session_response(existing)

        session = Session(
            sessionId=sid,
            context_type="group" if body.group_id else "pocket",
            workspace=workspace_id,
            owner=user_id,
            title=body.title,
            pocket=body.pocket_id,
            group=body.group_id,
            agent=body.agent_id,
        )
        await session.insert()

        await event_bus.emit(
            "session.created",
            {
                "session_id": str(session.id),
                "session_uuid": session.sessionId,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "pocket_id": body.pocket_id,
            },
        )

        created_data: dict = {
            "session_id": str(session.id),
            "user_id": user_id,
            "agent_id": body.agent_id,
            "workspace_id": workspace_id,
        }
        if body.pocket_id:
            created_data["pocket_id"] = body.pocket_id
        await emit(SessionCreated(data=created_data))

        return _session_response(session)

    @staticmethod
    async def list_sessions(workspace_id: str, user_id: str) -> list[dict]:
        """List all sessions for user, sorted by lastActivity desc."""
        sessions = (
            await Session.find(
                Session.workspace == workspace_id,
                Session.owner == user_id,
                Session.deleted_at == None,  # noqa: E711
            )
            .sort(-Session.lastActivity)
            .to_list()
        )
        return [_session_response(s) for s in sessions]

    @staticmethod
    async def list_by_agent(
        workspace_id: str,
        user_id: str,
        agent_id: str,
    ) -> list[dict]:
        """List a user's DM sessions for a specific agent, newest first.

        Used by the frontend to resolve the DM room for an agent — pick the
        most-recent session to resume, or show the full list for history.
        """
        sessions = (
            await Session.find(
                Session.workspace == workspace_id,
                Session.owner == user_id,
                Session.agent == agent_id,
                Session.deleted_at == None,  # noqa: E711
            )
            .sort(-Session.lastActivity)
            .to_list()
        )
        return [_session_response(s) for s in sessions]

    @staticmethod
    async def get(session_id: str, user_id: str) -> dict:
        session = await SessionService._get_session(session_id, user_id)
        return _session_response(session)

    @staticmethod
    async def update(session_id: str, user_id: str, body: UpdateSessionRequest) -> dict:
        session = await SessionService._get_session(session_id, user_id)
        if body.title is not None:
            session.title = body.title
        if body.pocket_id is not None:
            session.pocket = body.pocket_id
        await session.save()

        patched = body.model_dump(exclude_unset=True)
        await emit(
            SessionUpdated(
                data={
                    "session_id": str(session.id),
                    "user_id": user_id,
                    **patched,
                }
            )
        )
        return _session_response(session)

    @staticmethod
    async def delete(session_id: str, user_id: str) -> None:
        session = await SessionService._get_session(session_id, user_id)
        session.deleted_at = datetime.now(UTC)
        await session.save()

        await emit(
            SessionDeleted(
                data={
                    "session_id": str(session.id),
                    "user_id": user_id,
                }
            )
        )

    # -----------------------------------------------------------------
    # Pocket-scoped
    # -----------------------------------------------------------------

    @staticmethod
    async def list_for_pocket(pocket_id: str, user_id: str) -> list[dict]:
        logger.info(f"Listing sessions for pocket {pocket_id} and user {user_id}")
        sessions = (
            await Session.find(
                Session.pocket == pocket_id,
                Session.owner == user_id,
                Session.deleted_at == None,  # noqa: E711
            )
            .sort(-Session.lastActivity)
            .to_list()
        )
        return [_session_response(s) for s in sessions]

    @staticmethod
    async def create_for_pocket(
        workspace_id: str,
        user_id: str,
        pocket_id: str,
        body: CreateSessionRequest,
    ) -> dict:
        body_with_pocket = CreateSessionRequest(
            title=body.title,
            pocket_id=pocket_id,
            group_id=body.group_id,
            agent_id=body.agent_id,
            session_id=body.session_id,
        )
        return await SessionService.create(workspace_id, user_id, body_with_pocket)

    # -----------------------------------------------------------------
    # History
    # -----------------------------------------------------------------

    @staticmethod
    async def get_history(session_id: str, user_id: str, limit: int = 100) -> dict:
        """Return session chat history from the unified Mongo messages store.

        Resolves the Session doc first, then reads messages by context:
        group sessions fetch from `messages` filtered by group; pocket
        sessions fetch by session_key. No file-memory fallback — ee ships
        with MongoDB-backed memory.
        """
        from ee.cloud.models.message import Message

        session = await SessionService._get_session(session_id, user_id)

        if session.context_type == "group" and session.group:
            messages = (
                await Message.find(
                    {
                        "context_type": "group",
                        "group": session.group,
                        "deleted": False,
                    }
                )
                .sort("createdAt")
                .limit(limit)
                .to_list()
            )
            return {
                "messages": [
                    {
                        "_id": str(m.id),
                        "role": "assistant" if m.sender_type == "agent" else "user",
                        "content": m.content,
                        "sender": m.sender,
                        "senderType": m.sender_type,
                        "createdAt": iso_utc(m.createdAt),
                        "attachments": [a.model_dump() for a in (m.attachments or [])],
                    }
                    for m in messages
                ]
            }

        # Pocket context — keyed by session_key, which mirrors sessionId.
        messages = (
            await Message.find({"context_type": "pocket", "session_key": session.sessionId})
            .sort("createdAt")
            .limit(limit)
            .to_list()
        )
        return {
            "messages": [
                {
                    "_id": str(m.id),
                    "role": m.role or "user",
                    "content": m.content,
                    "sender": m.sender,
                    "senderType": m.sender_type,
                    "createdAt": iso_utc(m.createdAt),
                    "attachments": [a.model_dump() for a in (m.attachments or [])],
                }
                for m in messages
            ]
        }

    # -----------------------------------------------------------------
    # Touch
    # -----------------------------------------------------------------

    @staticmethod
    async def touch(session_id: str) -> None:
        """Update lastActivity and increment messageCount."""
        session = await Session.find_one(Session.sessionId == session_id)
        # Fallback: strip websocket_ prefix
        if not session and session_id.startswith("websocket_"):
            session = await Session.find_one(Session.sessionId == session_id[10:])
        if not session:
            return
        session.lastActivity = datetime.now(UTC)
        session.messageCount += 1
        await session.save()

        await emit(
            SessionUpdated(
                data={
                    "session_id": str(session.id),
                    "user_id": session.owner,
                    "last_message_at": iso_utc(session.lastActivity),
                }
            )
        )

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    @staticmethod
    async def _get_session(session_id: str, user_id: str) -> Session:
        """Fetch by ObjectId first, then by sessionId."""
        session = None
        try:
            session = await Session.get(PydanticObjectId(session_id))
        except Exception:
            session = await Session.find_one(Session.sessionId == session_id)

        if not session or session.deleted_at:
            raise NotFound("session", session_id)
        if session.owner != user_id:
            raise Forbidden("session.not_owner", "Not the session owner")
        return session
