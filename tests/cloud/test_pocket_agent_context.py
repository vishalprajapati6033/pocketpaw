"""Unit tests for the agent-facing pocket helpers.

These back the in-process MCP tools (``sdk_mcp_pocket.py``) that the cloud
SSE chat agent uses to read/write the pocket it lives inside. Beanie's
``Pocket`` model is mocked so the tests stay isolated from Mongo.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class _FakePocket(SimpleNamespace):
    """Minimal stand-in for the Beanie ``Pocket`` model.

    ``model_dump`` mirrors Pydantic's signature closely enough that
    ``fetch_pocket_for_agent`` and friends serialize correctly, and
    ``save`` is awaitable so write paths can ``await pocket.save()``.
    """

    def model_dump(self, **_kwargs):
        out = {k: v for k, v in self.__dict__.items() if not callable(v)}
        # Mirror by_alias=True for ``rippleSpec``.
        if "ripple_spec" in out and "rippleSpec" not in out:
            out["rippleSpec"] = out.pop("ripple_spec")
        # Drop None-valued fields the way exclude_none would.
        return {k: v for k, v in out.items() if v is not None}

    async def save(self):  # noqa: D401 - matches Beanie API
        return None


def _make_pocket(**fields):
    base = dict(
        id="p1",
        name="Test Pocket",
        description="",
        icon="",
        color="",
        widgets=[],
        rippleSpec=None,
    )
    base.update(fields)
    return _FakePocket(**base)


# ---------------------------------------------------------------------------
# update_pocket_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pocket_patches_only_provided_fields():
    pocket = _make_pocket(name="Old", description="orig", color="#000")
    with (
        patch(
            "ee.cloud.pockets.agent_context._load_pocket",
            AsyncMock(return_value=(pocket, None)),
        ),
        patch(
            "ee.cloud.ripple_normalizer.normalize_ripple_spec",
            side_effect=lambda spec: spec,
        ),
    ):
        from ee.cloud.pockets.agent_context import update_pocket_for_agent

        result = await update_pocket_for_agent("p1", name="New Name")

    assert result["ok"] is True
    assert pocket.name == "New Name"
    # Untouched fields stay put.
    assert pocket.description == "orig"
    assert pocket.color == "#000"


@pytest.mark.asyncio
async def test_update_pocket_normalizes_ripple_spec():
    pocket = _make_pocket()
    new_spec = {"version": "1.0", "ui": {"type": "flex", "children": []}}
    with (
        patch(
            "ee.cloud.pockets.agent_context._load_pocket",
            AsyncMock(return_value=(pocket, None)),
        ),
        patch(
            "ee.cloud.ripple_normalizer.normalize_ripple_spec",
            side_effect=lambda spec: {"normalized": True, **spec},
        ),
    ):
        from ee.cloud.pockets.agent_context import update_pocket_for_agent

        result = await update_pocket_for_agent("p1", ripple_spec=new_spec)

    assert result["ok"] is True
    assert pocket.rippleSpec == {"normalized": True, **new_spec}


@pytest.mark.asyncio
async def test_update_pocket_propagates_load_error():
    with patch(
        "ee.cloud.pockets.agent_context._load_pocket",
        AsyncMock(return_value=(None, "pocket xyz not found")),
    ):
        from ee.cloud.pockets.agent_context import update_pocket_for_agent

        result = await update_pocket_for_agent("xyz", name="anything")
    assert result == {"ok": False, "error": "pocket xyz not found"}


# ---------------------------------------------------------------------------
# add_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_widget_appends_with_defaults():
    pocket = _make_pocket(widgets=[])
    spec = {"name": "Sales", "type": "metric", "data": {"value": 10}}
    with patch(
        "ee.cloud.pockets.agent_context._load_pocket",
        AsyncMock(return_value=(pocket, None)),
    ):
        from ee.cloud.pockets.agent_context import add_widget_for_agent

        result = await add_widget_for_agent("p1", spec)

    assert result["ok"] is True
    assert len(pocket.widgets) == 1
    assert pocket.widgets[0].name == "Sales"
    assert pocket.widgets[0].type == "metric"
    assert pocket.widgets[0].data == {"value": 10}


@pytest.mark.asyncio
async def test_add_widget_rejects_non_dict_widget():
    from ee.cloud.pockets.agent_context import add_widget_for_agent

    result = await add_widget_for_agent("p1", "not a dict")  # type: ignore[arg-type]
    assert result["ok"] is False
    assert "JSON object" in result["error"]


# ---------------------------------------------------------------------------
# update_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_widget_patches_listed_fields():
    from ee.cloud.models.pocket import Widget

    widget = Widget(name="Sales", type="metric", data={"value": 10})
    widget_id = widget.id
    pocket = _make_pocket(widgets=[widget])
    with patch(
        "ee.cloud.pockets.agent_context._load_pocket",
        AsyncMock(return_value=(pocket, None)),
    ):
        from ee.cloud.pockets.agent_context import update_widget_for_agent

        result = await update_widget_for_agent(
            "p1", widget_id, {"name": "Revenue", "data": {"value": 20}}
        )

    assert result["ok"] is True
    assert pocket.widgets[0].name == "Revenue"
    assert pocket.widgets[0].data == {"value": 20}
    # Type wasn't in the patch payload, so it stays.
    assert pocket.widgets[0].type == "metric"


@pytest.mark.asyncio
async def test_update_widget_returns_error_when_widget_missing():
    pocket = _make_pocket(widgets=[])
    with patch(
        "ee.cloud.pockets.agent_context._load_pocket",
        AsyncMock(return_value=(pocket, None)),
    ):
        from ee.cloud.pockets.agent_context import update_widget_for_agent

        result = await update_widget_for_agent("p1", "missing", {"name": "x"})
    assert result["ok"] is False
    assert "missing" in result["error"]


# ---------------------------------------------------------------------------
# remove_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_widget_drops_matching_id():
    from ee.cloud.models.pocket import Widget

    keep = Widget(name="Keep", type="text")
    drop = Widget(name="Drop", type="text")
    pocket = _make_pocket(widgets=[keep, drop])
    with patch(
        "ee.cloud.pockets.agent_context._load_pocket",
        AsyncMock(return_value=(pocket, None)),
    ):
        from ee.cloud.pockets.agent_context import remove_widget_for_agent

        result = await remove_widget_for_agent("p1", drop.id)

    assert result["ok"] is True
    assert len(pocket.widgets) == 1
    assert pocket.widgets[0].id == keep.id


@pytest.mark.asyncio
async def test_remove_widget_errors_when_id_unknown():
    pocket = _make_pocket(widgets=[])
    with patch(
        "ee.cloud.pockets.agent_context._load_pocket",
        AsyncMock(return_value=(pocket, None)),
    ):
        from ee.cloud.pockets.agent_context import remove_widget_for_agent

        result = await remove_widget_for_agent("p1", "nonexistent")
    assert result["ok"] is False
    assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# Pocket-mutation event sink — ensure successful writes push to the SSE
# stream's queue so the frontend gets a real-time ``pocket_mutation`` event.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pocket_pushes_mutation_when_sink_attached():
    import asyncio

    from ee.cloud.chat.agent_service import (
        attach_pocket_event_sink,
        detach_pocket_event_sink,
    )

    pocket = _make_pocket(name="Old")
    queue: asyncio.Queue = asyncio.Queue()
    token = attach_pocket_event_sink(queue)
    try:
        with (
            patch(
                "ee.cloud.pockets.agent_context._load_pocket",
                AsyncMock(return_value=(pocket, None)),
            ),
            patch(
                "ee.cloud.ripple_normalizer.normalize_ripple_spec",
                side_effect=lambda spec: spec,
            ),
        ):
            from ee.cloud.pockets.agent_context import update_pocket_for_agent

            result = await update_pocket_for_agent("p1", name="Brand New")
    finally:
        detach_pocket_event_sink(token)

    assert result["ok"] is True
    assert queue.qsize() == 1
    payload = queue.get_nowait()
    assert payload["action"] == "replace"
    assert payload["pocket_id"] == "p1"
    assert payload["pocket"]["name"] == "Brand New"


@pytest.mark.asyncio
async def test_push_is_noop_without_sink():
    """Calling the mutation helpers outside an SSE stream must not raise."""
    pocket = _make_pocket(name="Old")
    with (
        patch(
            "ee.cloud.pockets.agent_context._load_pocket",
            AsyncMock(return_value=(pocket, None)),
        ),
        patch(
            "ee.cloud.ripple_normalizer.normalize_ripple_spec",
            side_effect=lambda spec: spec,
        ),
    ):
        from ee.cloud.pockets.agent_context import update_pocket_for_agent

        # No sink attached — should still return ok without exceptions.
        result = await update_pocket_for_agent("p1", name="Whatever")
    assert result["ok"] is True
