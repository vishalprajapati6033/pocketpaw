"""Resolves an Event into the list of user_ids that should receive it."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from ee.cloud.realtime.events import Event

MemberFetcher = Callable[[str], Awaitable[list[str]]]


class AudienceResolver:
    """One branch per event type. Caches group/workspace member lookups briefly."""

    def __init__(
        self,
        *,
        group_members: MemberFetcher | None = None,
        workspace_members: MemberFetcher | None = None,
        workspace_admins: MemberFetcher | None = None,
        workspace_peers: MemberFetcher | None = None,
        cache_ttl_seconds: float = 2.0,
    ) -> None:
        self._group_members = group_members
        self._workspace_members = workspace_members
        self._workspace_admins = workspace_admins
        self._workspace_peers = workspace_peers
        self._ttl = cache_ttl_seconds
        self._cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

    def invalidate_group(self, group_id: str) -> None:
        self._cache.pop(("group", group_id), None)

    def invalidate_workspace(self, workspace_id: str) -> None:
        # Peer caches are user-scoped (keyed by user_id, not workspace_id) and are
        # handled by invalidate_user_peers or the short TTL — do not try to pop
        # them here.
        self._cache.pop(("workspace", workspace_id), None)
        self._cache.pop(("workspace_admins", workspace_id), None)

    def invalidate_user_peers(self, user_id: str) -> None:
        self._cache.pop(("workspace_peers", user_id), None)

    async def _cached(self, kind: str, key: str, fn: MemberFetcher | None) -> list[str]:
        if fn is None:
            return []
        now = time.monotonic()
        entry = self._cache.get((kind, key))
        if entry and now - entry[0] < self._ttl:
            return list(entry[1])
        value = await fn(key)
        self._cache[(kind, key)] = (now, value)
        return list(value)

    async def _group(self, gid: str) -> list[str]:
        return await self._cached("group", gid, self._group_members)

    async def _workspace(self, wid: str) -> list[str]:
        return await self._cached("workspace", wid, self._workspace_members)

    async def _admins(self, wid: str) -> list[str]:
        return await self._cached("workspace_admins", wid, self._workspace_admins)

    async def _peers(self, uid: str) -> list[str]:
        return await self._cached("workspace_peers", uid, self._workspace_peers)

    async def audience(self, event: Event) -> list[str]:  # noqa: C901
        t = event.type
        d = event.data

        # --- Groups -------------------------------------------------------------
        if t == "group.created":
            return list(d.get("member_ids", []))
        if t in {
            "group.updated",
            "group.deleted",
            "group.member_added",
            "group.member_role",
            "group.agent_added",
            "group.agent_removed",
            "group.agent_updated",
            "group.pinned",
            "group.unpinned",
        }:
            members = await self._group(d["group_id"])
            # member_added: include the new user if present
            if t == "group.member_added" and (uid := d.get("user_id")):
                return list({*members, uid})
            return members
        if t == "group.member_removed":
            members = await self._group(d["group_id"])
            return list({*members, d["user_id"]})
        if t == "group.joined":
            # Scoped hydration event: audience is exactly the new user(s)
            # carried in ``member_ids``. Existing members already have the
            # room and receive ``group.member_added`` instead.
            return list(d.get("member_ids", []))
        if t == "group.unread_delta":
            return [d["user_id"]]

        # --- Messages -----------------------------------------------------------
        if t == "message.new":
            members = await self._group(d["group_id"])
            sender = d.get("sender")  # MessageResponse.sender
            if sender:
                members = [m for m in members if m != sender]
            return members
        if t in {
            "message.edited",
            "message.deleted",
            "message.reaction.added",
            "message.reaction.removed",
            "message.reaction",
            "message.read",
        }:
            return await self._group(d["group_id"])
        if t == "message.sent":
            return [d["sender_id"]]

        # --- Workspace ----------------------------------------------------------
        if t in {"workspace.updated", "workspace.deleted", "workspace.member_role"}:
            return await self._workspace(d["workspace_id"])
        if t == "workspace.member_added":
            members = await self._workspace(d["workspace_id"])
            if uid := d.get("user_id"):
                return list({*members, uid})
            return members
        if t == "workspace.member_removed":
            members = await self._workspace(d["workspace_id"])
            return list({*members, d["user_id"]})
        if t in {
            "workspace.invite.created",
            "workspace.invite.accepted",
            "workspace.invite.revoked",
        }:
            admins = await self._admins(d["workspace_id"])
            if uid := d.get("user_id"):
                return list({*admins, uid})
            return admins

        # --- Sessions -----------------------------------------------------------
        if t in {"session.created", "session.updated", "session.deleted"}:
            user_id = d.get("user_id")
            peer_id = d.get("peer_id")
            if user_id and peer_id:
                return list({user_id, peer_id})
            if user_id:
                return [user_id]
            return []

        # --- Files --------------------------------------------------------------
        if t in {"file.ready", "file.deleted"}:
            return await self._group(d["group_id"])

        # --- Agent --------------------------------------------------------------
        if t in {
            "agent.thinking",
            "agent.tool_start",
            "agent.tool_result",
            "agent.error",
            "agent.stream_chunk",
            "agent.stream_end",
            "agent.stream_start",
            "agent.tool_use",
        }:
            return await self._group(d["group_id"])

        # --- Notifications ------------------------------------------------------
        if t in {"notification.new", "notification.read", "notification.cleared"}:
            return [d["user_id"]]

        # --- Presence -----------------------------------------------------------
        if t in {"presence.online", "presence.offline"}:
            return await self._peers(d["user_id"])

        # Room-scoped events (typing.*) are routed by the ConnectionManager directly,
        # not via this resolver. Falling through returns [] and the bus will no-op.
        return []
