# backend/ee/ripple/manifest.py
"""Fetch and cache the Ripple widget manifest from a CDN.

The manifest is generated at ripple build time and published as
`@ripple-ui/svelte/dist/manifest.json`. This module fetches it,
caches the parse result in-process for a configurable TTL, and
formats it as a markdown block suitable for system-prompt injection.

On any failure (network, timeout, parse, schema), get_manifest()
returns None — the caller is expected to fall back to a different
source (today: kb scope search).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 5.0
_SCHEMA = "ripple.manifest/v1"

# Module-global cache: url -> (expires_at_monotonic, parsed_manifest)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def get_manifest(url: str, ttl_seconds: int) -> dict[str, Any] | None:
    """Fetch the manifest from `url`, with in-process TTL caching.

    Returns the parsed manifest dict on success, or None on any failure.
    """
    now = time.monotonic()
    cached = _cache.get(url)
    if cached is not None and cached[0] > now:
        return cached[1]

    parsed = await _fetch_and_validate(url)
    if parsed is None:
        return None

    _cache[url] = (now + ttl_seconds, parsed)
    return parsed


async def _fetch_and_validate(url: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=_FETCH_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        logger.warning("ripple manifest fetch timed out: %s", url)
        return None
    except httpx.HTTPError as exc:
        logger.warning("ripple manifest fetch failed: %s (%s)", url, exc)
        return None

    if response.status_code >= 400:
        logger.warning("ripple manifest fetch returned %s: %s", response.status_code, url)
        return None

    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        logger.error("ripple manifest is not valid JSON: %s", url)
        return None

    if not isinstance(parsed, dict):
        logger.error("ripple manifest is not a JSON object: %s", url)
        return None
    if parsed.get("schema") != _SCHEMA:
        logger.error("ripple manifest schema mismatch (got %r): %s", parsed.get("schema"), url)
        return None
    if not isinstance(parsed.get("widgets"), list):
        logger.error("ripple manifest missing widgets array: %s", url)
        return None

    return parsed


def format_for_prompt(manifest: dict[str, Any]) -> str:
    """Render the manifest as a markdown block for system-prompt injection.

    Returns "" if the manifest has no widgets — caller treats this the same
    as a fetch failure (i.e. falls back to kb search).
    """
    widgets = manifest.get("widgets") or []
    if not widgets:
        return ""

    lines: list[str] = []
    lines.append("\n\n<ripple-widget-reference>")
    lines.append(
        "The following Ripple widgets are available. Use these props, types, "
        "and example specs when building the UI."
    )
    lines.append("")

    for w in widgets:
        wtype = w.get("type", "?")
        category = w.get("category", "?")
        desc = w.get("description", "")
        lines.append(f"### `{wtype}` ({category})")
        lines.append(desc)
        props = w.get("props") or {}
        if props:
            lines.append("")
            lines.append("**Props:**")
            for name, spec in props.items():
                req = " *(required)*" if spec.get("required") else ""
                lines.append(
                    f"- `{name}`: `{spec.get('type', '?')}`{req} — {spec.get('description', '')}"
                )
        example = w.get("example")
        if example:
            lines.append("")
            lines.append("**Example:**")
            lines.append("```json")
            lines.append(json.dumps(example, indent=2))
            lines.append("```")
        lines.append("")

    lines.append("</ripple-widget-reference>")
    return "\n".join(lines)
