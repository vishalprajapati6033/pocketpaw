"""Agent-facing pocket fetch — returns the full pocket document as a
compact JSON string for injection into agent tool responses.

Lives under ee/cloud/pockets/ because it's pocket-domain logic. The thin
MCP binding that exposes this to the Claude Agent SDK is in
``src/pocketpaw/agents/sdk_mcp_pocket.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fields dropped from the agent-visible pocket payload:
# - secrets that the agent should never see
# - bulk relationship fields the agent doesn't need to reason about the
#   pocket's contents or layout
_AGENT_INVISIBLE_FIELDS = (
    "share_link_token",
    "shared_with",
    "team",
    "agents",
)


def _json_safe(doc: Any) -> Any:
    """Normalize a Mongo/Beanie document so ``json.dumps`` can serialize it."""
    return json.loads(json.dumps(doc, default=str))


async def fetch_pocket_for_agent(pocket_id: str) -> dict[str, Any]:
    """Return the full pocket document for an agent, or an error dict.

    Shape on success:
        {"ok": True, "pocket": {...}}
    Shape on failure:
        {"ok": False, "error": "..."}
    """
    if not pocket_id or not isinstance(pocket_id, str):
        return {"ok": False, "error": "pocket_id is required (string)"}

    try:
        from beanie import PydanticObjectId

        from ee.cloud.models.pocket import Pocket

        pocket = await Pocket.get(PydanticObjectId(pocket_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_pocket_for_agent: lookup failed for %s: %s", pocket_id, exc)
        return {"ok": False, "error": f"could not load pocket {pocket_id}: {exc}"}

    if pocket is None:
        return {"ok": False, "error": f"pocket {pocket_id} not found"}

    doc = pocket.model_dump(mode="json", by_alias=True, exclude_none=True)
    for k in _AGENT_INVISIBLE_FIELDS:
        doc.pop(k, None)
    return {"ok": True, "pocket": _json_safe(doc)}
