# ee/ripple/__init__.py — System prompts for Ripple UI generation (Enterprise).
# Licensed under FSL 1.1 — see ee/LICENSE.
#
# Three system prompts, one per surface:
#
#   * INLINE_RIPPLE_SYSTEM_PROMPT — chat-message-level UI. The agent emits a
#     short text reply plus an optional <ripple-spec> block; clicks on the
#     rendered UI round-trip back as user messages via `chat.send`.
#
#   * POCKET_CREATION_PROMPT — when the user asks us to build a NEW pocket
#     (full-page dashboard). Covers the create_pocket CLI bridge, the three
#     spec formats (UISpec / flat widgets / multi-pane), the layout system,
#     and the multi-agent research rules.
#
#   * POCKET_INTERACTION_PROMPT — when a <current-pocket> tag is present and
#     the user is asking about or modifying an existing pocket. Routes via
#     the read / write / chat intent flow with `get_pocket` first.
#
# Model selection, sampling parameters, and prompt caching are decided by
# the agent backend — this module exports prompt strings only.

from __future__ import annotations

from ee.ripple._inline import INLINE_RIPPLE_SYSTEM_PROMPT
from ee.ripple._pockets import POCKET_CREATION_PROMPT, POCKET_INTERACTION_PROMPT

__all__ = [
    "INLINE_RIPPLE_SYSTEM_PROMPT",
    "POCKET_CREATION_PROMPT",
    "POCKET_INTERACTION_PROMPT",
]
