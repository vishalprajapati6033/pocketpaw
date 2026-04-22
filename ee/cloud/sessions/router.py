"""Sessions domain — FastAPI router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from ee.cloud.license import require_license
from ee.cloud.sessions.schemas import (
    CreateSessionRequest,
    UpdateSessionRequest,
)
from ee.cloud.sessions.service import SessionService
from ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

router = APIRouter(prefix="/sessions", tags=["Sessions"], dependencies=[Depends(require_license)])

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def create_session(
    body: CreateSessionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await SessionService.create(workspace_id, user_id, body)


@router.get("", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def list_sessions(
    agent_id: str | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    """List the user's sessions. ``?agent_id=X`` filters to DM sessions for
    that agent (used by the frontend to resolve the DM room)."""
    if agent_id:
        return await SessionService.list_by_agent(workspace_id, user_id, agent_id)
    return await SessionService.list_sessions(workspace_id, user_id)


@router.get("/runtime")
async def list_runtime_sessions(limit: int = 50) -> dict:
    """List sessions from the active memory store's session index.

    Dispatches on the store: MongoMemoryStore exposes an async variant,
    FileMemoryStore a sync one. Stores without either return empty.
    """
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "_load_session_index_async"):
        index = await store._load_session_index_async()
    elif hasattr(store, "_load_session_index"):
        index = store._load_session_index()
    else:
        return {"sessions": [], "total": 0}

    entries = sorted(
        index.items(),
        key=lambda kv: kv[1].get("last_activity", ""),
        reverse=True,
    )[:limit]

    sessions = [{"id": safe_key, **meta} for safe_key, meta in entries]

    return {"sessions": sessions, "total": len(index)}


@router.post("/runtime/create")
async def create_runtime_session() -> dict:
    """Create a new runtime session (no MongoDB — just a session key)."""
    import uuid

    safe_key = f"websocket_{uuid.uuid4().hex[:12]}"
    return {"id": safe_key, "title": "New Chat"}


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await SessionService.get(session_id, user_id)


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    body: UpdateSessionRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await SessionService.update(session_id, user_id, body)


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await SessionService.delete(session_id, user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# History proxy & activity tracking
# ---------------------------------------------------------------------------


@router.get("/{session_id}/history")
async def get_session_history(
    session_id: str,
    limit: int = 50,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return session history from the unified Mongo messages store."""
    from ee.cloud.shared.errors import NotFound

    try:
        return await SessionService.get_history(session_id, user_id, limit=limit)
    except NotFound:
        return {"messages": []}


@router.post("/{session_id}/touch", status_code=204)
async def touch_session(session_id: str) -> Response:
    await SessionService.touch(session_id)
    return Response(status_code=204)
