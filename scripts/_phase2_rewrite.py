"""Phase 2 codemod: rewrite a single module-path prefix across the repo.

Usage:
    python scripts/_phase2_rewrite.py <old_dotted> <new_dotted>

Rewrites, on .py files only:
    from <old>            -> from <new>
    from <old>.X          -> from <new>.X
    import <old>          -> import <new>
    import <old>.X        -> import <new>.X
    "<old>" / "<old>.X"   -> "<new>" / "<new>.X"   (quoted module strings)

The <old>/<new> dots are escaped; a trailing word boundary stops
``instinct`` from also matching ``instinct_tools``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

EXCLUDE_DIRS = {".venv", ".git", "__pycache__", "dist", "build", "node_modules", ".tmp", "uvcache", "temp"}


def build_patterns(old: str, new: str) -> list[tuple[re.Pattern[str], str]]:
    o = re.escape(old)
    # \b after the prefix so "pocketpaw_ee.instinct" does not match
    # "pocketpaw_ee.instinct_tools"; the prefix is followed by either
    # "." (submodule), end, whitespace, quote, or other non-word char.
    return [
        (re.compile(rf"^(\s*)from {o}(\.\w|\s|$)", re.MULTILINE), rf"\1from {new}\2"),
        (re.compile(rf"^(\s*)import {o}(\.\w|\s|$)", re.MULTILINE), rf"\1import {new}\2"),
        (re.compile(rf"""(["']){o}(\.\w|["'])"""), rf"\1{new}\2"),
    ]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old, new = sys.argv[1], sys.argv[2]
    patterns = build_patterns(old, new)
    root = Path(".").resolve()
    changed = 0
    for py in root.rglob("*.py"):
        if any(part in EXCLUDE_DIRS for part in py.parts):
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        for pat, repl in patterns:
            new_text = pat.sub(repl, new_text)
        if new_text != text:
            py.write_text(new_text, encoding="utf-8")
            changed += 1
    print(f"{old} -> {new}: rewrote {changed} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
