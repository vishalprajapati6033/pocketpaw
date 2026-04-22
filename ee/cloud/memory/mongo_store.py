"""MongoDB implementation of MemoryStoreProtocol backed by the unified schema.

SESSION entries are stored as pocket-context rows in the `messages` collection,
keyed by ``session_key`` (mirrors the protocol's own key). LONG_TERM and DAILY
entries live in ``memory_facts``.

Session metadata (title stays user-facing / UI-owned) but per-turn upkeep —
``lastActivity`` touch and ``messageCount`` increment — is done here because
this adapter is the sole write path for chat turns. It also auto-creates a
``Session`` doc for a new ``session_key`` so the "start chatting → session
appears in the sidebar" UX works without a prior ``POST /sessions``.

Tenant scope
------------
Every row is stamped with a ``workspace_id`` so multi-tenant ee deployments
can isolate reads. For SESSION rows the adapter resolves it from the linked
Session.workspace at write time. For LONG_TERM / DAILY rows callers populate
``entry.metadata["workspace_id"]``. Reads expose ``workspace_id`` as a
parameter on the adapter-specific helpers (``list_facts_in_workspace``,
``get_session_in_workspace``); the protocol-level methods stay unscoped to
preserve the ``MemoryStoreProtocol`` contract for OSS callers.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from beanie import PydanticObjectId
from bson.errors import InvalidId

from ee.cloud.memory.documents import MemoryFactDoc
from ee.cloud.models.message import Attachment, Message
from ee.cloud.models.session import Session
from pocketpaw.memory.protocol import MemoryEntry, MemoryType  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# Channels the pocketpaw bus emits as the prefix of `InboundMessage.session_key`
# (``f"{channel.value}:{chat_id}"``). Kept in sync with
# ``pocketpaw.bus.events.Channel`` — when a new adapter is added there, append
# its value here. ``_normalize_session_key`` logs a warning when it sees a
# colon-form prefix it doesn't recognise so the drift is visible.
_KNOWN_BUS_CHANNELS = frozenset({"websocket", "telegram", "discord", "slack", "whatsapp", "cli"})


def _normalize_session_key(key: str) -> str:
    """Translate bus-style session keys to the underscore form used by Session.sessionId.

    The pocketpaw message bus forms session keys as ``"{channel}:{chat_id}"``
    (colon), while ``Session.sessionId`` and the UI use the safe-key form
    ``"{channel}_{chat_id}"`` (underscore). To keep ``messages.session_key``
    joinable with ``sessions.sessionId``, we rewrite the first ``":"`` to
    ``"_"`` on every read/write — but only when the prefix matches a known
    channel so unrelated keys (user-supplied pocket session keys, etc.) are
    left untouched. Unknown colon-prefixed keys log a warning so a missing
    channel is visible rather than silent.
    """
    if ":" not in key:
        return key
    channel, _, rest = key.partition(":")
    if channel in _KNOWN_BUS_CHANNELS:
        return f"{channel}_{rest}"
    logger.warning(
        "session_key %r looks bus-shaped (colon) but channel %r is not in the "
        "known list — left untouched. Update _KNOWN_BUS_CHANNELS if a new bus "
        "adapter was added.",
        key,
        channel,
    )
    return key


def _message_to_entry(msg: Message) -> MemoryEntry:
    """Translate a pocket-context Message to a protocol MemoryEntry."""
    ts = msg.createdAt or datetime.now(UTC)
    metadata: dict = {}
    if msg.workspace_id:
        metadata["workspace_id"] = msg.workspace_id
    return MemoryEntry(
        id=str(msg.id),
        type=MemoryType.SESSION,
        content=msg.content,
        created_at=ts,
        updated_at=ts,
        role=msg.role,
        session_key=msg.session_key,
        metadata=metadata,
    )


def _fact_to_entry(doc: MemoryFactDoc) -> MemoryEntry:
    """Translate a MemoryFactDoc to a protocol MemoryEntry."""
    ts_created = doc.createdAt or datetime.now(UTC)
    ts_updated = doc.updatedAt or ts_created
    metadata = dict(doc.metadata)
    if doc.user_id:
        metadata.setdefault("user_id", doc.user_id)
    if doc.workspace_id:
        metadata.setdefault("workspace_id", doc.workspace_id)
    return MemoryEntry(
        id=str(doc.id),
        type=MemoryType(doc.type),
        content=doc.content,
        created_at=ts_created,
        updated_at=ts_updated,
        tags=list(doc.tags),
        metadata=metadata,
    )


class MongoMemoryStore:
    """Full MemoryStoreProtocol implementation on top of the unified schema.

    - SESSION: reads/writes the `messages` collection (pocket context).
    - LONG_TERM / DAILY: reads/writes the `memory_facts` collection.

    Multi-tenant scoping
    ~~~~~~~~~~~~~~~~~~~~
    Every persisted row carries a ``workspace_id`` (derived from the linked
    ``Session.workspace`` for pocket messages, supplied via
    ``entry.metadata["workspace_id"]`` for facts). The protocol-level read
    methods stay tenant-agnostic to keep the ``MemoryStoreProtocol`` contract
    unchanged for OSS callers; ee callers that need strict isolation should
    use the adapter-specific ``*_in_workspace`` helpers, which add an explicit
    ``workspace_id`` filter.
    """

    async def save(self, entry: MemoryEntry) -> str:
        if entry.type == MemoryType.SESSION:
            if not entry.session_key:
                raise ValueError("SESSION entry must have session_key set")
            role = entry.role or "user"
            # `sender_type` mirrors the chat-message convention used by the
            # group-chat path: assistant rows land as "agent", everything
            # else (user/system) as "user". Without this both fields would
            # default to "user" and downstream UIs that read `senderType`
            # (instead of `role`) would render every message as the user.
            sender_type = "agent" if role == "assistant" else "user"
            normalized_key = _normalize_session_key(entry.session_key)

            # Dedup against a same-turn re-write. The main duplicate source
            # (chat_persistence writing in parallel) is gone — we now own
            # the single write path — but keep this guard so agent-loop
            # retries of identical content don't land twice.
            existing = await _find_recent_twin(normalized_key, role, entry.content)
            if existing is not None:
                return str(existing.id)

            # Attachments ride on the InboundMessage metadata from
            # /chat/stream so we can persist them on the same Message row
            # instead of double-writing. Malformed entries are skipped but
            # don't abort the save — the text content still gets through.
            attachment_docs: list[Attachment] = []
            raw_attachments = (entry.metadata or {}).get("attachments") or []
            if isinstance(raw_attachments, list):
                for a in raw_attachments:
                    if not isinstance(a, dict):
                        continue
                    try:
                        attachment_docs.append(Attachment(**a))
                    except Exception:
                        logger.warning("skipping malformed attachment on pocket message: %r", a)

            session, workspace_id = await _resolve_or_create_session(normalized_key, entry)
            msg = Message(
                context_type="pocket",
                session_key=normalized_key,
                role=role,  # type: ignore[arg-type]
                sender_type=sender_type,
                content=entry.content,
                workspace_id=workspace_id,
                attachments=attachment_docs,
            )
            await msg.insert()

            if session is not None:
                await _touch_session(session)

            return str(msg.id)

        # LONG_TERM / DAILY → memory_facts
        meta = dict(entry.metadata or {})
        user_id = meta.pop("user_id", None)
        workspace_id = meta.pop("workspace_id", None)
        doc = MemoryFactDoc(
            type=entry.type.value,
            content=entry.content,
            tags=list(entry.tags or []),
            user_id=user_id if isinstance(user_id, str) else None,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
            metadata=meta,
        )
        await doc.insert()
        return str(doc.id)

    async def get(self, entry_id: str) -> MemoryEntry | None:
        try:
            oid = PydanticObjectId(entry_id)
        except (InvalidId, ValueError):
            return None
        msg = await Message.get(oid)
        if msg and msg.context_type == "pocket":
            return _message_to_entry(msg)
        fact = await MemoryFactDoc.get(oid)
        if fact:
            return _fact_to_entry(fact)
        return None

    async def delete(self, entry_id: str) -> bool:
        try:
            oid = PydanticObjectId(entry_id)
        except (InvalidId, ValueError):
            return False
        msg = await Message.get(oid)
        if msg and msg.context_type == "pocket":
            await msg.delete()
            return True
        fact = await MemoryFactDoc.get(oid)
        if fact:
            await fact.delete()
            return True
        return False

    async def search(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        # Substring-only search (no vectors). Dispatches by type:
        # SESSION → messages; LONG_TERM/DAILY → memory_facts; None → facts
        # across both fact types (mirrors FileMemoryStore's default search).
        if memory_type == MemoryType.SESSION:
            filters: dict = {"context_type": "pocket"}
            if tags:
                raise NotImplementedError("tag search is not supported for SESSION messages in v1")
            if query:
                filters["content"] = {"$regex": re.escape(query), "$options": "i"}
            messages = await Message.find(filters).sort("-createdAt").limit(limit).to_list()
            return [_message_to_entry(m) for m in messages]

        fact_filters: dict = {}
        if memory_type is not None:
            fact_filters["type"] = memory_type.value
        if tags:
            fact_filters["tags"] = {"$in": tags}
        if query:
            fact_filters["content"] = {"$regex": re.escape(query), "$options": "i"}
        facts = await MemoryFactDoc.find(fact_filters).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]

    async def get_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100,
        user_id: str | None = None,
    ) -> list[MemoryEntry]:
        if memory_type == MemoryType.SESSION:
            messages = (
                await Message.find({"context_type": "pocket"})
                .sort("-createdAt")
                .limit(limit)
                .to_list()
            )
            return [_message_to_entry(m) for m in messages]

        filters: dict = {"type": memory_type.value}
        if user_id is not None:
            filters["user_id"] = user_id
        facts = await MemoryFactDoc.find(filters).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]

    async def get_session(self, session_key: str) -> list[MemoryEntry]:
        key = _normalize_session_key(session_key)
        messages = (
            await Message.find({"context_type": "pocket", "session_key": key})
            .sort("createdAt")
            .to_list()
        )
        return [_message_to_entry(m) for m in messages]

    async def clear_session(self, session_key: str) -> int:
        key = _normalize_session_key(session_key)
        messages = await Message.find({"context_type": "pocket", "session_key": key}).to_list()
        count = len(messages)
        for m in messages:
            await m.delete()
        return count

    # ---- Adapter-specific (not in MemoryStoreProtocol) ----------------

    async def get_session_info(self, session_key: str) -> Session | None:
        """Return the Session metadata row for ``session_key`` if it exists.

        The adapter never auto-creates `sessions` rows — that's the API layer's
        job (`SessionService`). A None return means no user-facing session
        metadata exists, even if messages do.
        """
        return await Session.find_one(Session.sessionId == session_key)

    async def _load_session_index_async(self) -> dict:
        """Build a session-index dict from pocket-context Session docs.

        Shape-compatible with ``FileMemoryStore._load_session_index`` so the
        ``GET /sessions/runtime`` endpoint is backend-agnostic. Returns a mapping
        ``{sessionId: {title, channel, last_activity, message_count}}`` for all
        non-deleted pocket sessions.
        """
        docs = await Session.find(
            {"context_type": "pocket", "deleted_at": None}
        ).to_list()

        index: dict[str, dict] = {}
        for doc in docs:
            session_id = doc.sessionId
            # Derive channel from the safe_key prefix (websocket_xxx → "websocket").
            channel = session_id.split("_", 1)[0] if "_" in session_id else "unknown"
            # Mongo strips tzinfo on persistence; re-anchor as UTC so the
            # serialized ISO string stays unambiguous for the frontend.
            last_activity = ""
            if doc.lastActivity:
                dt = doc.lastActivity
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                last_activity = dt.isoformat()
            index[session_id] = {
                "title": doc.title or "New Chat",
                "channel": channel,
                "last_activity": last_activity,
                "message_count": doc.messageCount,
            }
        return index

    async def get_session_with_messages(
        self, session_key: str, limit: int | None = None
    ) -> tuple[Session | None, list[MemoryEntry]]:
        """Return session metadata (if any) plus its messages in one call.

        Two queries but a single adapter entry point. When ``limit`` is None
        all messages are returned; otherwise only the most recent ``limit`` in
        ascending order.
        """
        session = await self.get_session_info(session_key)
        key = _normalize_session_key(session_key)
        query = Message.find({"context_type": "pocket", "session_key": key})
        if limit is None:
            messages = await query.sort("createdAt").to_list()
        else:
            recent = await query.sort("-createdAt").limit(limit).to_list()
            messages = list(reversed(recent))
        return session, [_message_to_entry(m) for m in messages]

    # ---- Tenant-scoped reads (ee callers should prefer these) ----------

    async def get_session_in_workspace(
        self, session_key: str, workspace_id: str
    ) -> list[MemoryEntry]:
        """Like ``get_session`` but enforces a workspace boundary.

        Returns an empty list if the session_key exists but belongs to a
        different workspace, so a leaked or guessed key cannot expose a
        tenant's messages.
        """
        key = _normalize_session_key(session_key)
        messages = (
            await Message.find(
                {
                    "context_type": "pocket",
                    "session_key": key,
                    "workspace_id": workspace_id,
                }
            )
            .sort("createdAt")
            .to_list()
        )
        return [_message_to_entry(m) for m in messages]

    async def list_facts_in_workspace(
        self,
        workspace_id: str,
        memory_type: MemoryType | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """List LONG_TERM / DAILY facts scoped to a workspace.

        Rows without a ``workspace_id`` (legacy / OSS data) are excluded so
        cross-tenant leakage is impossible by construction.
        """
        filters: dict = {"workspace_id": workspace_id}
        if memory_type is not None:
            filters["type"] = memory_type.value
        if user_id is not None:
            filters["user_id"] = user_id
        facts = await MemoryFactDoc.find(filters).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]


# Window for treating an existing Message as a duplicate of the current
# write. The dual-write race (chat endpoint + agent loop both saving) is
# synchronous in the same request, so 5s is plenty — and short enough that a
# real user sending back-to-back "ok" messages doesn't see one silently
# swallowed.
_DEDUP_WINDOW_SECONDS = 5


async def _find_recent_twin(
    session_key: str,
    role: str,
    content: str,
) -> Message | None:
    """Return an existing Message row with the same content written recently.

    Covers the synchronous in-request race where the chat endpoint persists
    the message once (with attachments) and the agent loop's
    ``memory.add_to_session`` then calls us with the same content for
    agent-context memory. The first writer wins — attachments and ordering
    on the canonical record are preserved. Legitimate back-to-back resends
    of the same short text (``"ok"``) fall outside the 5s window.
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=_DEDUP_WINDOW_SECONDS)
    try:
        return await Message.find_one(
            {
                "context_type": "pocket",
                "session_key": session_key,
                "role": role,
                "content": content,
                "createdAt": {"$gte": cutoff},
            }
        )
    except Exception:
        logger.exception("memory dedup lookup failed for session=%s", session_key)
        return None


async def _resolve_or_create_session(
    session_key: str, entry: MemoryEntry
) -> tuple[Session | None, str | None]:
    """Return (session, workspace_id) for a SESSION row at write time.

    Lookup order:
    1. Existing ``Session`` row where ``sessionId == session_key`` — the
       common case (client POSTed ``/sessions`` first or the session was
       auto-created on a previous turn).
    2. Auto-create a pocket ``Session`` so the "start chatting → session
       appears in the sidebar" UX works when the client skipped the
       explicit create. Owner is picked the same way the old
       ``chat_persistence`` bridge did: first user with a workspace.

    The workspace_id prefers ``entry.metadata["workspace_id"]`` when the
    caller already knows it (e.g. an HTTP handler with active workspace in
    scope), falling back to ``Session.workspace``.

    Returns ``(None, workspace_id_or_None)`` only when no session could be
    resolved or created (no users/workspaces exist yet). The message row
    still persists — it's just not counted against a session. Tenant-scoped
    reads exclude untagged rows, which is the correct safe default.
    """
    md_ws = (entry.metadata or {}).get("workspace_id")
    md_ws = md_ws if isinstance(md_ws, str) and md_ws else None

    session = await Session.find_one(Session.sessionId == session_key)
    if session is not None:
        return session, md_ws or session.workspace

    session = await _auto_create_pocket_session(session_key, workspace_id=md_ws)
    if session is None:
        return None, md_ws

    return session, session.workspace


async def _auto_create_pocket_session(
    session_key: str, *, workspace_id: str | None = None
) -> Session | None:
    """Create a pocket ``Session`` doc for ``session_key`` when none exists.

    Picks the first user in the target workspace (or any workspace user if
    ``workspace_id`` is not provided) so the session has an owner. Returns
    ``None`` when no suitable user exists — caller keeps writing the
    message row without Session linkage.
    """
    from ee.cloud.models.user import User

    query: dict
    if workspace_id:
        query = {"workspaces.workspace": workspace_id}
    else:
        query = {"workspaces": {"$ne": []}}
    users = await User.find(query).limit(1).to_list()
    if not users:
        logger.warning("auto_create_pocket_session: no user with a workspace")
        return None

    user = users[0]
    if workspace_id is None:
        workspace_id = user.workspaces[0].workspace if user.workspaces else None
    if not workspace_id:
        return None

    session = Session(
        sessionId=session_key,
        context_type="pocket",
        workspace=workspace_id,
        owner=str(user.id),
        title="Chat",
    )
    await session.insert()
    logger.info("auto-created pocket session: sessionId=%s owner=%s", session_key, user.id)
    return session


async def _touch_session(session: Session) -> None:
    """Increment ``messageCount`` and refresh ``lastActivity`` on a Session.

    Runs after every persisted pocket SESSION entry so the sidebar reflects
    recency and totals without a separate API-layer hook. Failures log but
    don't raise — the message row is already committed.
    """
    try:
        session.lastActivity = datetime.now(UTC)
        session.messageCount = (session.messageCount or 0) + 1
        await session.save()
    except Exception:
        logger.warning("failed to touch Session %s", session.sessionId, exc_info=True)
