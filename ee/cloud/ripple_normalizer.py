"""Minimal ripple spec normalizer — ensures envelope fields and widget IDs."""

from __future__ import annotations

import secrets
from typing import Any


def _short_id() -> str:
    return secrets.token_hex(4)


def normalize_ripple_spec(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize AI-generated rippleSpec before persistence.

    Ensures envelope fields (version, intent, lifecycle.id).
    Passes through UISpec and multi-pane specs with minimal changes.
    Generates widget IDs if missing for flat widget specs.
    """
    if not spec or not isinstance(spec, dict):
        return None

    name = spec.get("title") or spec.get("name")
    pocket_id = spec.get("id") or (spec.get("lifecycle") or {}).get("id") or f"pocket-{_short_id()}"
    meta = spec.get("metadata") or {}
    color = spec.get("color") or meta.get("color", "#0A84FF")

    envelope = {
        "lifecycle": spec.get("lifecycle") or {"type": "persistent", "id": pocket_id},
        "title": name or spec.get("title"),
        "name": name or spec.get("name"),
        "color": color,
        "metadata": {
            "category": spec.get("category") or meta.get("category", "custom"),
            "color": color,
            **meta,
        },
    }

    # Multi-pane: pass through with envelope
    if spec.get("panes") and isinstance(spec["panes"], dict):
        return {**spec, **envelope, "version": spec.get("version", "1.0")}

    # UISpec v1.0: pass through with envelope
    ui = spec.get("ui")
    if isinstance(ui, dict) and ui.get("type"):
        return {**spec, **envelope, "version": spec.get("version", "1.0")}

    # UISpec passed as a raw root node — i.e. ``{type: "flex", props,
    # children, ...}`` instead of ``{ui: {type: "flex", ...}}``. The
    # ``create_pocket`` MCP tool description tells the agent to send a
    # "UISpec v1.0 component tree", which it often interprets as the
    # node itself (no ``ui`` wrapper). Detect that shape and lift the
    # node under ``ui`` so the frontend's UISpec renderer picks it up
    # — without this, the persisted spec has no ``ui`` and no
    # ``widgets``, and the dashboard renderer falls back to the
    # "No widgets yet" empty state.
    spec_type = spec.get("type")
    if isinstance(spec_type, str) and spec_type and (
        "props" in spec or "children" in spec
    ):
        node = {k: v for k, v in spec.items() if k in ("type", "props", "children", "style", "show", "id")}
        return {**envelope, "version": spec.get("version", "1.0"), "ui": node}

    # Flat widgets: ensure IDs
    raw_widgets = spec.get("widgets")
    if isinstance(raw_widgets, list) and raw_widgets:
        widgets = []
        for i, w in enumerate(raw_widgets):
            if not isinstance(w, dict):
                continue
            w = {**w}
            if not w.get("id"):
                w["id"] = f"{pocket_id}-w{i}"
            if not w.get("title"):
                w["title"] = w.get("name", f"Widget {i + 1}")
            widgets.append(w)
        return {
            **spec,
            **envelope,
            "version": spec.get("version", "2.0"),
            "intent": spec.get("intent", "dashboard"),
            "widgets": widgets,
            "display": spec.get("display") or {"columns": 3},
            "dashboard_layout": spec.get("dashboard_layout")
            or {"type": "grid", "columns": 3, "gap": 10},
        }

    # No widgets, no ui, no panes — return as-is with envelope
    return {**spec, **envelope}
