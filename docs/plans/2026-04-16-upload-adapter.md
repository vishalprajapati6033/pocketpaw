# Upload Adapter Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a file upload service with a pluggable `StorageAdapter` abstraction, local-disk backend, and both OSS + EE routers for chat attachments. Support bulk uploads (up to 50 files per request) from day one.

**Architecture:** Small core protocol + local impl in `src/pocketpaw/uploads/`. The `UploadService` validates (size, mime, magic-byte sniff), generates opaque storage keys, writes via the adapter, persists metadata in a tier-specific store (JSONL for OSS, Mongo for EE). Routers in `src/pocketpaw/api/v1/uploads.py` (OSS) and `ee/cloud/uploads/router.py` (EE, workspace-scoped).

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, Beanie (EE), `aiofiles` (new dep), pytest + pytest-asyncio, mongomock-motor (EE tests).

---

## Design reference

See `docs/plans/2026-04-16-upload-adapter-design.md` for the full design, scope decisions, access model, and rationale.

## File inventory

**Create (OSS core):**
- `src/pocketpaw/uploads/__init__.py`
- `src/pocketpaw/uploads/adapter.py`
- `src/pocketpaw/uploads/local.py`
- `src/pocketpaw/uploads/keys.py`
- `src/pocketpaw/uploads/errors.py`
- `src/pocketpaw/uploads/config.py`
- `src/pocketpaw/uploads/file_store.py`
- `src/pocketpaw/uploads/service.py`
- `src/pocketpaw/api/v1/uploads.py`

**Create (EE):**
- `ee/cloud/uploads/__init__.py`
- `ee/cloud/uploads/models.py`
- `ee/cloud/uploads/service.py`
- `ee/cloud/uploads/router.py`

**Create (tests):**
- `tests/uploads/__init__.py`
- `tests/uploads/conftest.py`
- `tests/uploads/test_keys.py`
- `tests/uploads/test_errors.py`
- `tests/uploads/test_local_adapter.py`
- `tests/uploads/test_file_store.py`
- `tests/uploads/test_service.py`
- `tests/uploads/test_router.py`
- `tests/cloud/uploads/__init__.py`
- `tests/cloud/uploads/test_mongo_store.py`
- `tests/cloud/uploads/test_service.py`
- `tests/cloud/uploads/test_router.py`

**Modify:**
- `pyproject.toml` — add `aiofiles>=23.2` to main deps
- `src/pocketpaw/api/v1/__init__.py` — register uploads router
- `ee/cloud/models/__init__.py` — append `FileUpload` to `ALL_DOCUMENTS`
- `ee/cloud/__init__.py` or wherever EE routers register — mount EE uploads router

## Command reference (run from `D:/paw/backend`)

| Purpose | Command |
|---|---|
| Install deps | `uv sync --dev` |
| Add aiofiles | `uv add aiofiles` |
| Run a single test file | `uv run pytest tests/uploads/test_keys.py -v` |
| Run all upload tests | `uv run pytest tests/uploads/ tests/cloud/uploads/ -v` |
| Run full fast suite | `uv run pytest --ignore=tests/e2e` |
| Lint + format | `uv run ruff check . && uv run ruff format .` |
| Type check | `uv run mypy .` |

---

## Task 1: Add `aiofiles` dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Install**

Run: `uv add aiofiles`
Expected: `aiofiles` added to `[project].dependencies`, `uv.lock` updated.

**Step 2: Verify import**

Run: `uv run python -c "import aiofiles; print(aiofiles.__version__)"`
Expected: prints a version (>= 23.2).

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add aiofiles for async file I/O in upload adapter"
```

---

## Task 2: `uploads/errors.py` — error hierarchy

**Files:**
- Create: `src/pocketpaw/uploads/__init__.py` (empty)
- Create: `src/pocketpaw/uploads/errors.py`
- Create: `tests/uploads/__init__.py` (empty)
- Create: `tests/uploads/test_errors.py`

**Step 1: Write failing tests**

`tests/uploads/test_errors.py`:

```python
from pocketpaw.uploads.errors import (
    AccessDenied,
    EmptyFile,
    NotFound,
    StorageFailure,
    TooLarge,
    UnsupportedMime,
    UploadError,
)


def test_all_errors_inherit_upload_error():
    for cls in (TooLarge, UnsupportedMime, EmptyFile, NotFound, AccessDenied, StorageFailure):
        assert issubclass(cls, UploadError)


def test_errors_carry_code_attribute():
    assert TooLarge("25mb").code == "too_large"
    assert UnsupportedMime("image/tiff").code == "unsupported_mime"
    assert EmptyFile().code == "empty"
    assert NotFound().code == "not_found"
    assert AccessDenied().code == "access_denied"
    assert StorageFailure("disk full").code == "storage_error"


def test_upload_error_preserves_message():
    err = TooLarge("file is 40MB")
    assert str(err) == "file is 40MB"
```

**Step 2: Run — expect fail**

Run: `uv run pytest tests/uploads/test_errors.py -v`
Expected: FAIL — module not found.

**Step 3: Implement**

`src/pocketpaw/uploads/errors.py`:

```python
"""Error hierarchy for the upload adapter."""

from __future__ import annotations


class UploadError(Exception):
    """Base class for all upload-related errors."""

    code: str = "upload_error"


class TooLarge(UploadError):
    code = "too_large"


class UnsupportedMime(UploadError):
    code = "unsupported_mime"


class EmptyFile(UploadError):
    code = "empty"

    def __init__(self, message: str = "file is empty") -> None:
        super().__init__(message)


class NotFound(UploadError):
    code = "not_found"

    def __init__(self, message: str = "not found") -> None:
        super().__init__(message)


class AccessDenied(UploadError):
    code = "access_denied"

    def __init__(self, message: str = "access denied") -> None:
        super().__init__(message)


class StorageFailure(UploadError):
    code = "storage_error"
```

**Step 4: Run — expect pass**

Run: `uv run pytest tests/uploads/test_errors.py -v`
Expected: PASS, 3/3 tests green.

**Step 5: Commit**

```bash
git add src/pocketpaw/uploads/__init__.py src/pocketpaw/uploads/errors.py tests/uploads/__init__.py tests/uploads/test_errors.py
git commit -m "feat(uploads): add error hierarchy with per-class codes"
```

---

## Task 3: `uploads/keys.py` — storage key generation

**Files:**
- Create: `src/pocketpaw/uploads/keys.py`
- Create: `tests/uploads/test_keys.py`

**Step 1: Write failing tests**

`tests/uploads/test_keys.py`:

```python
import re

from pocketpaw.uploads.keys import new_storage_key, sanitize_ext


def test_new_storage_key_shape():
    key = new_storage_key("chat", ".png")
    assert re.match(r"^chat/\d{6}/[a-f0-9]{32}\.png$", key), key


def test_default_kind_is_chat():
    key = new_storage_key(ext=".pdf")
    assert key.startswith("chat/")


def test_no_ext_produces_key_without_extension():
    key = new_storage_key("chat", "")
    assert re.match(r"^chat/\d{6}/[a-f0-9]{32}$", key)


def test_1000_keys_are_unique():
    keys = {new_storage_key("chat", ".bin") for _ in range(1000)}
    assert len(keys) == 1000


def test_sanitize_ext_lowercases():
    assert sanitize_ext(".PNG") == ".png"


def test_sanitize_ext_strips_non_alnum():
    assert sanitize_ext(".p!ng") == ".png"


def test_sanitize_ext_caps_length():
    assert sanitize_ext(".abcdefghijklmno") == ".abcdefgh"  # 8 chars max


def test_sanitize_ext_empty_returns_empty():
    assert sanitize_ext("") == ""
    assert sanitize_ext(".") == ""


def test_sanitize_ext_adds_leading_dot():
    assert sanitize_ext("png") == ".png"
```

**Step 2: Run — expect fail**

Run: `uv run pytest tests/uploads/test_keys.py -v`
Expected: FAIL — module not found.

**Step 3: Implement**

`src/pocketpaw/uploads/keys.py`:

```python
"""Storage-key generation for uploads.

Keys are opaque to external callers and namespaced by "kind" + a yyyymm bucket.
The UUID4 hex tail guarantees uniqueness.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

_EXT_RE = re.compile(r"[^a-z0-9]")
_MAX_EXT_LEN = 8


def sanitize_ext(ext: str) -> str:
    """Normalize a file extension to ``.{alnum,<=8}`` or empty."""
    if not ext:
        return ""
    tail = ext.lstrip(".").lower()
    tail = _EXT_RE.sub("", tail)[:_MAX_EXT_LEN]
    return f".{tail}" if tail else ""


def new_storage_key(kind: str = "chat", ext: str = "") -> str:
    """Return a fresh unique storage key ``{kind}/{yyyymm}/{uuid32}{ext}``."""
    yyyymm = datetime.now(UTC).strftime("%Y%m")
    safe_ext = sanitize_ext(ext)
    return f"{kind}/{yyyymm}/{uuid.uuid4().hex}{safe_ext}"
```

**Step 4: Run — expect pass**

Run: `uv run pytest tests/uploads/test_keys.py -v`
Expected: PASS, 9/9 tests green.

**Step 5: Commit**

```bash
git add src/pocketpaw/uploads/keys.py tests/uploads/test_keys.py
git commit -m "feat(uploads): storage key generator with safe extension sanitization"
```

---

## Task 4: `uploads/config.py` — upload settings

**Files:**
- Create: `src/pocketpaw/uploads/config.py`

No tests needed — this is config data. Exercised via `test_service.py`.

**Step 1: Implement**

`src/pocketpaw/uploads/config.py`:

```python
"""Upload configuration — size limits, mime allowlist, storage root."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Mimes safe to render inline (images, pdf, plain text). Everything else gets
# Content-Disposition: attachment to avoid in-origin HTML/SVG tricks.
INLINE_MIMES: frozenset[str] = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "application/pdf",
    "text/plain", "text/markdown", "text/csv",
})

DEFAULT_ALLOWED_MIMES: frozenset[str] = frozenset({
    # Images
    "image/png", "image/jpeg", "image/gif", "image/webp",
    # Documents
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",         # .xlsx
    # Text / code
    "text/plain", "text/markdown", "text/csv",
    "application/json",
})


@dataclass
class UploadSettings:
    """Static configuration for the upload pipeline."""

    max_file_bytes: int = 25 * 1024 * 1024          # 25 MiB
    max_files_per_batch: int = 50
    allowed_mimes: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_MIMES)
    local_root: Path = field(default_factory=lambda: Path.home() / ".pocketpaw" / "uploads")


_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
}


def extension_for(mime: str) -> str:
    """Map a canonical mime type to a file extension. Returns ``""`` if unknown."""
    return _MIME_TO_EXT.get(mime, "")
```

**Step 2: Verify import**

Run: `uv run python -c "from pocketpaw.uploads.config import UploadSettings, extension_for; print(extension_for('image/png'))"`
Expected: prints `.png`.

**Step 3: Commit**

```bash
git add src/pocketpaw/uploads/config.py
git commit -m "feat(uploads): static config — size caps, mime allowlist, local root"
```

---

## Task 5: `uploads/adapter.py` — `StorageAdapter` protocol

**Files:**
- Create: `src/pocketpaw/uploads/adapter.py`

**Step 1: Implement**

`src/pocketpaw/uploads/adapter.py`:

```python
"""StorageAdapter protocol — the swap point for local, S3, etc."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredObject:
    """Return value of ``StorageAdapter.put``."""

    key: str
    size: int
    mime: str


class StorageAdapter(Protocol):
    """Abstract byte storage. Knows nothing about metadata, auth, or mime logic.

    Implementations must be safe to call from asyncio contexts.
    """

    async def put(
        self, key: str, stream: AsyncIterator[bytes], mime: str
    ) -> StoredObject:
        """Persist ``stream`` at ``key``. Returns the canonical ``StoredObject``."""

    async def open(self, key: str) -> AsyncIterator[bytes]:  # pragma: no cover
        """Yield the stored bytes in chunks. Raises ``NotFound`` if missing."""

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present. Idempotent."""

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` is currently stored."""
```

**Step 2: Verify import**

Run: `uv run python -c "from pocketpaw.uploads.adapter import StorageAdapter, StoredObject; print(StoredObject(key='k', size=0, mime='t'))"`
Expected: prints the `StoredObject(...)` repr.

**Step 3: Commit**

```bash
git add src/pocketpaw/uploads/adapter.py
git commit -m "feat(uploads): StorageAdapter protocol + StoredObject"
```

---

## Task 6: `uploads/local.py` — `LocalStorageAdapter`

**Files:**
- Create: `src/pocketpaw/uploads/local.py`
- Create: `tests/uploads/conftest.py`
- Create: `tests/uploads/test_local_adapter.py`

**Step 1: Write the conftest + failing tests**

`tests/uploads/conftest.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_upload_root(tmp_path: Path) -> Path:
    """Isolated root for each test."""
    root = tmp_path / "uploads"
    root.mkdir()
    return root
```

`tests/uploads/test_local_adapter.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pocketpaw.uploads.errors import AccessDenied, NotFound, StorageFailure
from pocketpaw.uploads.local import LocalStorageAdapter


async def _astream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


class TestLocalStorageAdapter:
    async def test_put_writes_bytes_and_returns_size(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        obj = await adapter.put("chat/202604/abc.png", _astream([b"hello"]), "image/png")
        assert obj.key == "chat/202604/abc.png"
        assert obj.size == 5
        assert obj.mime == "image/png"
        assert (tmp_upload_root / "chat" / "202604" / "abc.png").read_bytes() == b"hello"

    async def test_put_creates_parent_dirs(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.put("a/b/c/d.bin", _astream([b"x"]), "application/octet-stream")
        assert (tmp_upload_root / "a" / "b" / "c" / "d.bin").exists()

    async def test_put_concatenates_chunks(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        obj = await adapter.put("k/file.bin", _astream([b"foo", b"bar", b"baz"]), "application/octet-stream")
        assert obj.size == 9
        assert (tmp_upload_root / "k/file.bin").read_bytes() == b"foobarbaz"

    async def test_put_atomic_no_partial_on_stream_error(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)

        async def bad_stream() -> AsyncIterator[bytes]:
            yield b"part1"
            raise RuntimeError("boom")

        with pytest.raises(StorageFailure):
            await adapter.put("k/file.bin", bad_stream(), "application/octet-stream")

        # Final file must NOT exist
        assert not (tmp_upload_root / "k" / "file.bin").exists()

    async def test_open_streams_bytes(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.put("k/file.bin", _astream([b"hello world"]), "application/octet-stream")
        chunks: list[bytes] = [c async for c in adapter.open("k/file.bin")]
        assert b"".join(chunks) == b"hello world"

    async def test_open_missing_raises_not_found(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(NotFound):
            _ = [c async for c in adapter.open("nope")]

    async def test_delete_idempotent(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.put("k/file.bin", _astream([b"x"]), "application/octet-stream")
        await adapter.delete("k/file.bin")
        await adapter.delete("k/file.bin")  # second call: no error
        assert not (tmp_upload_root / "k" / "file.bin").exists()

    async def test_exists_true_after_put(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        assert await adapter.exists("k/file.bin") is False
        await adapter.put("k/file.bin", _astream([b"x"]), "application/octet-stream")
        assert await adapter.exists("k/file.bin") is True

    async def test_put_rejects_path_traversal_key(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(AccessDenied):
            await adapter.put("../../evil.bin", _astream([b"x"]), "application/octet-stream")

    async def test_open_rejects_path_traversal_key(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(AccessDenied):
            _ = [c async for c in adapter.open("../outside.bin")]
```

**Step 2: Run — expect fail**

Run: `uv run pytest tests/uploads/test_local_adapter.py -v`
Expected: FAIL — module not found.

**Step 3: Implement**

`src/pocketpaw/uploads/local.py`:

```python
"""Local-disk StorageAdapter backed by aiofiles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles
import aiofiles.os

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.errors import AccessDenied, NotFound, StorageFailure

_CHUNK_SIZE = 64 * 1024


class LocalStorageAdapter(StorageAdapter):
    """Store blobs under ``root``. Atomic writes via .tmp + rename.

    Rejects keys that would escape ``root`` after normalization.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        target = (self._root / key).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise AccessDenied(f"key escapes storage root: {key!r}") from exc
        return target

    async def put(
        self, key: str, stream: AsyncIterator[bytes], mime: str
    ) -> StoredObject:
        final = self._resolve(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.name + ".tmp")
        size = 0
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                async for chunk in stream:
                    await fh.write(chunk)
                    size += len(chunk)
            await aiofiles.os.replace(str(tmp), str(final))
        except Exception as exc:
            # Best-effort cleanup of the partial .tmp
            try:
                await aiofiles.os.remove(str(tmp))
            except FileNotFoundError:
                pass
            raise StorageFailure(str(exc)) from exc
        return StoredObject(key=key, size=size, mime=mime)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        target = self._resolve(key)
        if not target.exists():
            raise NotFound(f"missing: {key}")
        async with aiofiles.open(target, "rb") as fh:
            while True:
                chunk = await fh.read(_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    async def delete(self, key: str) -> None:
        target = self._resolve(key)
        try:
            await aiofiles.os.remove(str(target))
        except FileNotFoundError:
            pass

    async def exists(self, key: str) -> bool:
        target = self._resolve(key)
        return target.exists()
```

**Step 4: Run — expect pass**

Run: `uv run pytest tests/uploads/test_local_adapter.py -v`
Expected: PASS, 10/10 tests green.

**Step 5: Commit**

```bash
git add src/pocketpaw/uploads/local.py tests/uploads/conftest.py tests/uploads/test_local_adapter.py
git commit -m "feat(uploads): LocalStorageAdapter with atomic writes + path safety"
```

---

## Task 7: `uploads/file_store.py` — OSS JSONL metadata store

**Files:**
- Create: `src/pocketpaw/uploads/file_store.py`
- Create: `tests/uploads/test_file_store.py`

**Step 1: Write failing tests**

`tests/uploads/test_file_store.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore


def _record(file_id: str = "f1", **overrides) -> FileRecord:
    defaults = {
        "id": file_id,
        "storage_key": "chat/202604/abc.png",
        "filename": "cat.png",
        "mime": "image/png",
        "size": 1234,
        "owner_id": "user-1",
        "chat_id": "chat-1",
        "created": datetime(2026, 4, 16, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


class TestJSONLFileStore:
    def test_save_then_get_roundtrip(self, tmp_path: Path):
        store = JSONLFileStore(path=tmp_path / "idx.jsonl")
        store.save(_record())
        got = store.get("f1")
        assert got is not None
        assert got.filename == "cat.png"
        assert got.size == 1234

    def test_get_missing_returns_none(self, tmp_path: Path):
        store = JSONLFileStore(path=tmp_path / "idx.jsonl")
        assert store.get("nope") is None

    def test_soft_delete_hides_from_get(self, tmp_path: Path):
        store = JSONLFileStore(path=tmp_path / "idx.jsonl")
        store.save(_record())
        store.soft_delete("f1")
        assert store.get("f1") is None

    def test_cold_reload_preserves_state(self, tmp_path: Path):
        path = tmp_path / "idx.jsonl"
        s1 = JSONLFileStore(path=path)
        s1.save(_record("a"))
        s1.save(_record("b", filename="b.png"))
        s1.soft_delete("a")

        s2 = JSONLFileStore(path=path)  # reload
        assert s2.get("a") is None
        assert s2.get("b") is not None
        assert s2.get("b").filename == "b.png"

    def test_corrupt_line_is_skipped(self, tmp_path: Path):
        path = tmp_path / "idx.jsonl"
        path.write_text('{"op": "save", "record": {"id": "a", "storage_key": "k", "filename": "a", '
                        '"mime": "text/plain", "size": 1, "owner_id": "u", "chat_id": null, '
                        '"created": "2026-04-16T12:00:00+00:00"}}\n'
                        'THIS IS NOT JSON\n'
                        '{"op": "save", "record": {"id": "b", "storage_key": "k2", "filename": "b", '
                        '"mime": "text/plain", "size": 1, "owner_id": "u", "chat_id": null, '
                        '"created": "2026-04-16T12:00:00+00:00"}}\n',
                        encoding="utf-8")
        store = JSONLFileStore(path=path)
        assert store.get("a") is not None
        assert store.get("b") is not None
```

**Step 2: Run — expect fail**

Run: `uv run pytest tests/uploads/test_file_store.py -v`
Expected: FAIL — module not found.

**Step 3: Implement**

`src/pocketpaw/uploads/file_store.py`:

```python
"""OSS metadata store — append-only JSONL keyed by file_id."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class FileRecord:
    id: str
    storage_key: str
    filename: str
    mime: str
    size: int
    owner_id: str
    chat_id: str | None
    created: datetime


class JSONLFileStore:
    """Append-only JSONL store with in-memory cache.

    Each line is either:
      - ``{"op": "save",   "record": {...FileRecord...}}``
      - ``{"op": "delete", "id": "<file_id>"}``

    Deleted records are hidden from ``get`` but the historical line stays in
    the file for audit.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, FileRecord] = {}
        self._deleted: set[str] = set()
        self._lock = Lock()
        self._reload()

    def _reload(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping corrupt upload-index line: %r", line[:120])
                continue
            op = row.get("op")
            if op == "save":
                rec = row.get("record") or {}
                try:
                    rec["created"] = datetime.fromisoformat(rec["created"])
                    self._records[rec["id"]] = FileRecord(**rec)
                except (KeyError, TypeError, ValueError):
                    logger.warning("skipping malformed record line: %r", line[:120])
            elif op == "delete":
                fid = row.get("id")
                if isinstance(fid, str):
                    self._deleted.add(fid)

    def _append(self, line: dict) -> None:
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=_json_default) + "\n")

    def save(self, record: FileRecord) -> None:
        self._records[record.id] = record
        self._deleted.discard(record.id)
        self._append({"op": "save", "record": asdict(record)})

    def get(self, file_id: str) -> FileRecord | None:
        if file_id in self._deleted:
            return None
        return self._records.get(file_id)

    def soft_delete(self, file_id: str) -> None:
        self._deleted.add(file_id)
        self._append({"op": "delete", "id": file_id, "at": datetime.now(UTC).isoformat()})


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value)}")
```

**Step 4: Run — expect pass**

Run: `uv run pytest tests/uploads/test_file_store.py -v`
Expected: PASS, 5/5 tests green.

**Step 5: Commit**

```bash
git add src/pocketpaw/uploads/file_store.py tests/uploads/test_file_store.py
git commit -m "feat(uploads): JSONL metadata store with soft-delete and corruption tolerance"
```

---

## Task 8: `uploads/service.py` — validation + orchestration

**Files:**
- Create: `src/pocketpaw/uploads/service.py`
- Create: `tests/uploads/test_service.py`

**Step 1: Write failing tests**

`tests/uploads/test_service.py`:

```python
from __future__ import annotations

import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import (
    AccessDenied,
    EmptyFile,
    NotFound,
    TooLarge,
    UnsupportedMime,
)
from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore
from pocketpaw.uploads.service import UploadService


# --- Fake adapter --------------------------------------------------------

class _FakeAdapter(StorageAdapter):
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        data = b""
        async for chunk in stream:
            data += chunk
        self.blobs[key] = data
        return StoredObject(key=key, size=len(data), mime=mime)

    async def open(self, key):
        if key not in self.blobs:
            raise NotFound()
        yield self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)

    async def exists(self, key):
        return key in self.blobs


# --- Helpers -------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"rest"
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"rest"

def _upload(content: bytes, filename: str, content_type: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers={"content-type": content_type},  # type: ignore[arg-type]
    )


@pytest.fixture()
def service(tmp_path: Path):
    adapter = _FakeAdapter()
    meta = JSONLFileStore(path=tmp_path / "idx.jsonl")
    cfg = UploadSettings(local_root=tmp_path)
    return UploadService(adapter=adapter, meta=meta, cfg=cfg), adapter, meta


# --- Tests ---------------------------------------------------------------

class TestUploadServiceSingle:
    async def test_happy_path_returns_record(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id="c1")
        assert rec.filename == "cat.png"
        assert rec.mime == "image/png"
        assert rec.size == len(PNG_MAGIC)
        assert rec.owner_id == "u1"
        assert rec.chat_id == "c1"
        assert rec.id

    async def test_rejects_oversize(self, service):
        svc, _, _ = service
        svc._cfg = UploadSettings(max_file_bytes=10, local_root=svc._cfg.local_root)
        file = _upload(b"x" * 100, "big.bin", "application/octet-stream")
        with pytest.raises(TooLarge):
            await svc.upload(file, owner_id="u1", chat_id=None)

    async def test_rejects_disallowed_mime(self, service):
        svc, _, _ = service
        file = _upload(b"<svg/>", "x.svg", "image/svg+xml")
        with pytest.raises(UnsupportedMime):
            await svc.upload(file, owner_id="u1", chat_id=None)

    async def test_rejects_empty_file(self, service):
        svc, _, _ = service
        file = _upload(b"", "empty.txt", "text/plain")
        with pytest.raises(EmptyFile):
            await svc.upload(file, owner_id="u1", chat_id=None)

    async def test_magic_byte_sniff_overrides_content_type(self, service):
        # Client claims image/jpeg but bytes are PNG; saved mime should be image/png.
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "x.jpg", "image/jpeg")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        assert rec.mime == "image/png"

    async def test_filename_with_path_separators_sanitized(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "../evil.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        assert rec.filename == "evil.png"


class TestUploadServiceBulk:
    async def test_all_succeed(self, service):
        svc, _, _ = service
        files = [
            _upload(PNG_MAGIC, "a.png", "image/png"),
            _upload(JPEG_MAGIC, "b.jpg", "image/jpeg"),
            _upload(b"hi", "c.txt", "text/plain"),
        ]
        result = await svc.upload_many(files, owner_id="u1", chat_id=None)
        assert len(result.uploaded) == 3
        assert len(result.failed) == 0

    async def test_partial_failure(self, service):
        svc, _, _ = service
        svc._cfg = UploadSettings(max_file_bytes=100, local_root=svc._cfg.local_root)
        files = [
            _upload(PNG_MAGIC, "good.png", "image/png"),
            _upload(b"x" * 500, "big.bin", "application/octet-stream"),
            _upload(b"<svg/>", "bad.svg", "image/svg+xml"),
        ]
        result = await svc.upload_many(files, owner_id="u1", chat_id=None)
        assert len(result.uploaded) == 1
        assert result.uploaded[0].filename == "good.png"
        assert len(result.failed) == 2
        codes = {f.code for f in result.failed}
        assert codes == {"too_large", "unsupported_mime"}

    async def test_empty_batch_raises_too_large(self, service):
        svc, _, _ = service
        with pytest.raises(ValueError, match="empty"):
            await svc.upload_many([], owner_id="u1", chat_id=None)

    async def test_batch_over_cap_raises(self, service):
        svc, _, _ = service
        svc._cfg = UploadSettings(max_files_per_batch=2, local_root=svc._cfg.local_root)
        files = [_upload(PNG_MAGIC, f"{i}.png", "image/png") for i in range(3)]
        with pytest.raises(ValueError, match="too many"):
            await svc.upload_many(files, owner_id="u1", chat_id=None)


class TestStreamAndDelete:
    async def test_stream_happy_path(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        got_rec, it = await svc.stream(rec.id, requester_id="u1")
        chunks = [c async for c in it]
        assert b"".join(chunks) == PNG_MAGIC
        assert got_rec.id == rec.id

    async def test_stream_wrong_owner_returns_not_found(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id="someone-else")

    async def test_stream_missing_raises_not_found(self, service):
        svc, _, _ = service
        with pytest.raises(NotFound):
            await svc.stream("nope", requester_id="u1")

    async def test_delete_owner_succeeds_idempotent(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        await svc.delete(rec.id, requester_id="u1")
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id="u1")
        # Second delete: not found (already soft-deleted)
        with pytest.raises(NotFound):
            await svc.delete(rec.id, requester_id="u1")

    async def test_delete_non_owner_raises_not_found(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        with pytest.raises(NotFound):
            await svc.delete(rec.id, requester_id="someone-else")
```

**Step 2: Run — expect fail**

Run: `uv run pytest tests/uploads/test_service.py -v`
Expected: FAIL — module not found.

**Step 3: Implement**

`src/pocketpaw/uploads/service.py`:

```python
"""UploadService — validates, stores, and persists metadata."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.config import UploadSettings, extension_for
from pocketpaw.uploads.errors import (
    AccessDenied,
    EmptyFile,
    NotFound,
    StorageFailure,
    TooLarge,
    UnsupportedMime,
    UploadError,
)
from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore
from pocketpaw.uploads.keys import new_storage_key

_SNIFF_BYTES = 512


# --- Magic-byte sniff ----------------------------------------------------

def _sniff_mime(head: bytes, fallback: str) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        # ZIP container — docx/xlsx both use this. Keep fallback if it matches.
        if fallback in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            return fallback
    return fallback


# --- Public types --------------------------------------------------------

FailCode = Literal["too_large", "unsupported_mime", "empty", "storage_error"]


@dataclass
class FailedUpload:
    filename: str
    reason: str
    code: FailCode


@dataclass
class BulkUploadResult:
    uploaded: list[FileRecord]
    failed: list[FailedUpload]


# --- Service -------------------------------------------------------------

class UploadService:
    def __init__(
        self,
        adapter: StorageAdapter,
        meta: JSONLFileStore,
        cfg: UploadSettings,
    ) -> None:
        self._adapter = adapter
        self._meta = meta
        self._cfg = cfg

    async def upload(
        self, file: UploadFile, owner_id: str, chat_id: str | None
    ) -> FileRecord:
        result = await self.upload_many([file], owner_id, chat_id)
        if result.failed:
            f = result.failed[0]
            _raise(f.code, f.reason)
        return result.uploaded[0]

    async def upload_many(
        self, files: list[UploadFile], owner_id: str, chat_id: str | None,
    ) -> BulkUploadResult:
        if not files:
            raise ValueError("empty upload batch")
        if len(files) > self._cfg.max_files_per_batch:
            raise ValueError(
                f"too many files: {len(files)} > {self._cfg.max_files_per_batch}"
            )

        uploaded: list[FileRecord] = []
        failed: list[FailedUpload] = []

        for file in files:
            try:
                rec = await self._upload_one(file, owner_id, chat_id)
                uploaded.append(rec)
            except TooLarge as e:
                failed.append(FailedUpload(filename=_basename(file.filename), reason=str(e), code="too_large"))
            except UnsupportedMime as e:
                failed.append(FailedUpload(filename=_basename(file.filename), reason=str(e), code="unsupported_mime"))
            except EmptyFile as e:
                failed.append(FailedUpload(filename=_basename(file.filename), reason=str(e), code="empty"))
            except StorageFailure as e:
                failed.append(FailedUpload(filename=_basename(file.filename), reason=str(e), code="storage_error"))

        return BulkUploadResult(uploaded=uploaded, failed=failed)

    async def _upload_one(
        self, file: UploadFile, owner_id: str, chat_id: str | None,
    ) -> FileRecord:
        head = await file.read(_SNIFF_BYTES)
        if not head:
            raise EmptyFile()

        mime = _sniff_mime(head, file.content_type or "application/octet-stream")
        if mime not in self._cfg.allowed_mimes:
            raise UnsupportedMime(f"mime not allowed: {mime}")

        ext = extension_for(mime)
        key = new_storage_key("chat", ext)

        # Stream the rest while counting size; raise TooLarge mid-stream
        cap = self._cfg.max_file_bytes
        first = head

        async def _body() -> AsyncIterator[bytes]:
            nonlocal_size = {"n": len(first)}
            if nonlocal_size["n"] > cap:
                raise TooLarge(f"file exceeds {cap} bytes")
            yield first
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                nonlocal_size["n"] += len(chunk)
                if nonlocal_size["n"] > cap:
                    raise TooLarge(f"file exceeds {cap} bytes")
                yield chunk

        obj = await self._adapter.put(key, _body(), mime)

        file_id = uuid.uuid4().hex
        filename = _basename(file.filename) or "upload"
        record = FileRecord(
            id=file_id,
            storage_key=obj.key,
            filename=filename,
            mime=obj.mime,
            size=obj.size,
            owner_id=owner_id,
            chat_id=chat_id,
            created=datetime.now(UTC),
        )
        self._meta.save(record)
        return record

    async def stream(
        self, file_id: str, requester_id: str
    ) -> tuple[FileRecord, AsyncIterator[bytes]]:
        rec = self._meta.get(file_id)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()  # 404-not-403 to avoid leaking existence
        return rec, self._adapter.open(rec.storage_key)

    async def delete(self, file_id: str, requester_id: str) -> None:
        rec = self._meta.get(file_id)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()
        await self._adapter.delete(rec.storage_key)
        self._meta.soft_delete(file_id)


# --- Helpers -------------------------------------------------------------

def _basename(name: str | None) -> str:
    if not name:
        return ""
    return os.path.basename(name.replace("\\", "/"))


def _raise(code: FailCode, reason: str) -> None:
    mapping: dict[FailCode, type[UploadError]] = {
        "too_large": TooLarge,
        "unsupported_mime": UnsupportedMime,
        "empty": EmptyFile,
        "storage_error": StorageFailure,
    }
    raise mapping[code](reason)
```

**Step 4: Run — expect pass**

Run: `uv run pytest tests/uploads/test_service.py -v`
Expected: PASS, all tests green (15+ tests across the 3 classes).

**Step 5: Commit**

```bash
git add src/pocketpaw/uploads/service.py tests/uploads/test_service.py
git commit -m "feat(uploads): UploadService with validation, magic-byte sniff, bulk, soft-delete"
```

---

## Task 9: `api/v1/uploads.py` — OSS router

**Files:**
- Create: `src/pocketpaw/api/v1/uploads.py`
- Modify: `src/pocketpaw/api/v1/__init__.py`
- Create: `tests/uploads/test_router.py`

**Step 1: Write failing tests**

`tests/uploads/test_router.py`:

```python
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.uploads import make_router

PNG = b"\x89PNG\r\n\x1a\n" + b"body"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    # Stub auth: requester_id comes from a header for testing
    async def _fake_requester(x_user: str = "u1") -> str:
        return x_user

    app.include_router(
        make_router(upload_root=tmp_path / "u", index_path=tmp_path / "u/_idx.jsonl",
                    requester_dep=_fake_requester),
        prefix="/api/v1",
    )
    return TestClient(app)


def test_upload_single_roundtrip(client: TestClient):
    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        headers={"x-user": "u1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["uploaded"]) == 1
    assert data["uploaded"][0]["filename"] == "cat.png"
    assert data["uploaded"][0]["mime"] == "image/png"
    fid = data["uploaded"][0]["id"]

    r2 = client.get(f"/api/v1/uploads/{fid}", headers={"x-user": "u1"})
    assert r2.status_code == 200
    assert r2.content == PNG
    assert r2.headers["content-type"].startswith("image/png")
    assert "inline" in r2.headers["content-disposition"]


def test_bulk_upload_partial_success(client: TestClient):
    r = client.post(
        "/api/v1/uploads",
        files=[
            ("files", ("good.png", PNG, "image/png")),
            ("files", ("bad.svg", b"<svg/>", "image/svg+xml")),
        ],
        headers={"x-user": "u1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["uploaded"]) == 1
    assert len(data["failed"]) == 1
    assert data["failed"][0]["code"] == "unsupported_mime"


def test_download_wrong_user_is_not_found(client: TestClient):
    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        headers={"x-user": "alice"},
    )
    fid = r.json()["uploaded"][0]["id"]

    r2 = client.get(f"/api/v1/uploads/{fid}", headers={"x-user": "bob"})
    assert r2.status_code == 404


def test_delete_owner_then_get_not_found(client: TestClient):
    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        headers={"x-user": "u1"},
    )
    fid = r.json()["uploaded"][0]["id"]

    r2 = client.delete(f"/api/v1/uploads/{fid}", headers={"x-user": "u1"})
    assert r2.status_code == 204

    r3 = client.get(f"/api/v1/uploads/{fid}", headers={"x-user": "u1"})
    assert r3.status_code == 404


def test_empty_batch_is_400(client: TestClient):
    r = client.post("/api/v1/uploads", headers={"x-user": "u1"})
    assert r.status_code == 400


def test_docx_gets_attachment_disposition(client: TestClient):
    # ZIP magic (docx container). With the officedocument mime, sniff keeps it.
    docx = b"PK\x03\x04" + b"rest"
    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("doc.docx", docx,
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        headers={"x-user": "u1"},
    )
    fid = r.json()["uploaded"][0]["id"]
    r2 = client.get(f"/api/v1/uploads/{fid}", headers={"x-user": "u1"})
    assert r2.status_code == 200
    assert "attachment" in r2.headers["content-disposition"]
```

**Step 2: Run — expect fail**

Run: `uv run pytest tests/uploads/test_router.py -v`
Expected: FAIL — module not found.

**Step 3: Implement**

`src/pocketpaw/api/v1/uploads.py`:

```python
"""OSS uploads router — /uploads POST/GET/DELETE."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from pocketpaw.uploads.config import INLINE_MIMES, UploadSettings
from pocketpaw.uploads.errors import (
    EmptyFile,
    NotFound,
    TooLarge,
    UnsupportedMime,
    UploadError,
)
from pocketpaw.uploads.file_store import JSONLFileStore
from pocketpaw.uploads.local import LocalStorageAdapter
from pocketpaw.uploads.service import UploadService


def make_router(
    upload_root: Path,
    index_path: Path,
    requester_dep: Callable[..., Awaitable[str]],
    cfg: UploadSettings | None = None,
) -> APIRouter:
    """Build the uploads router.

    ``requester_dep`` is a FastAPI dependency returning the authenticated user
    id. Caller plugs in their app-specific auth here.
    """
    cfg = cfg or UploadSettings(local_root=upload_root)
    adapter = LocalStorageAdapter(root=upload_root)
    meta = JSONLFileStore(path=index_path)
    service = UploadService(adapter=adapter, meta=meta, cfg=cfg)

    router = APIRouter(prefix="/uploads", tags=["Uploads"])

    @router.post("")
    async def upload(
        files: Annotated[list[UploadFile], File(...)],
        chat_id: Annotated[str | None, Form()] = None,
        requester_id: str = Depends(requester_dep),
    ) -> dict:
        try:
            result = await service.upload_many(files, requester_id, chat_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        return {
            "uploaded": [_record_to_dict(r) for r in result.uploaded],
            "failed": [asdict(f) for f in result.failed],
        }

    @router.get("/{file_id}")
    async def download(
        file_id: str, requester_id: str = Depends(requester_dep),
    ) -> StreamingResponse:
        try:
            rec, it = await service.stream(file_id, requester_id)
        except NotFound as e:
            raise HTTPException(status_code=404, detail="not found") from e
        disposition = "inline" if rec.mime in INLINE_MIMES else "attachment"
        return StreamingResponse(
            it,
            media_type=rec.mime,
            headers={
                "Content-Disposition": f'{disposition}; filename="{rec.filename}"',
            },
        )

    @router.delete("/{file_id}", status_code=204)
    async def delete(
        file_id: str, requester_id: str = Depends(requester_dep),
    ) -> Response:
        try:
            await service.delete(file_id, requester_id)
        except NotFound as e:
            raise HTTPException(status_code=404, detail="not found") from e
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _record_to_dict(rec) -> dict:
    return {
        "id": rec.id,
        "filename": rec.filename,
        "mime": rec.mime,
        "size": rec.size,
        "url": f"/api/v1/uploads/{rec.id}",
        "created": rec.created.isoformat(),
    }
```

**Step 4: Register in the v1 app**

Edit `src/pocketpaw/api/v1/__init__.py`. Find where other routers are included (e.g. `router.include_router(sessions.router)`). Add:

```python
from pathlib import Path
from pocketpaw.api.v1 import uploads as _uploads
from pocketpaw.api.deps import require_scope  # or whatever auth dep is used elsewhere

# After the existing routers block:
async def _upload_requester(user_id: str = Depends(require_scope("uploads"))) -> str:
    return user_id  # replace with however v1 obtains the user id

router.include_router(
    _uploads.make_router(
        upload_root=Path.home() / ".pocketpaw" / "uploads",
        index_path=Path.home() / ".pocketpaw" / "uploads" / "_idx.jsonl",
        requester_dep=_upload_requester,
    )
)
```

**If uncertain about the exact auth pattern** in the v1 app, STOP and flag as NEEDS_CONTEXT — the right pattern is whatever `sessions.py` or `chat.py` uses for `user_id`. Do not invent an auth path.

**Step 5: Run — expect pass**

Run: `uv run pytest tests/uploads/test_router.py -v`
Expected: PASS.

**Step 6: Run full upload suite**

Run: `uv run pytest tests/uploads/ -v`
Expected: all green across Tasks 2–9.

**Step 7: Commit**

```bash
git add src/pocketpaw/api/v1/uploads.py src/pocketpaw/api/v1/__init__.py tests/uploads/test_router.py
git commit -m "feat(uploads): OSS /uploads router with single + bulk + stream + delete"
```

---

## Task 10: EE `FileUpload` Beanie document

**Files:**
- Create: `ee/cloud/uploads/__init__.py` (empty)
- Create: `ee/cloud/uploads/models.py`
- Modify: `ee/cloud/models/__init__.py` — append `FileUpload` to `ALL_DOCUMENTS`
- Create: `tests/cloud/uploads/__init__.py` (empty)

**Step 1: Read the existing pattern**

Read `ee/cloud/models/session.py` and `ee/cloud/models/__init__.py` so the new document follows the same shape (TimestampedDocument base, Indexed, alias/populate_by_name settings, indexes list).

**Step 2: Implement**

`ee/cloud/uploads/models.py`:

```python
"""EE FileUpload document — Mongo metadata for uploaded blobs."""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import Field

from ee.cloud.models.base import TimestampedDocument


class FileUpload(TimestampedDocument):
    """Metadata for one uploaded file. Blob bytes live in the StorageAdapter."""

    file_id: Indexed(str, unique=True)  # type: ignore[valid-type]
    storage_key: str
    filename: str
    mime: str
    size: int
    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    chat_id: Indexed(str) | None = None  # type: ignore[valid-type]
    deleted_at: datetime | None = None

    class Settings:
        name = "file_uploads"
        indexes = [
            [("workspace", 1), ("chat_id", 1), ("createdAt", -1)],
            [("workspace", 1), ("owner", 1), ("createdAt", -1)],
        ]
```

Append to `ee/cloud/models/__init__.py` the `FileUpload` import and include in `ALL_DOCUMENTS` tuple.

**Step 3: Verify imports**

Run: `uv run python -c "from ee.cloud.uploads.models import FileUpload; from ee.cloud.models import ALL_DOCUMENTS; print(FileUpload in ALL_DOCUMENTS)"`
Expected: `True`.

**Step 4: Commit**

```bash
git add ee/cloud/uploads/__init__.py ee/cloud/uploads/models.py ee/cloud/models/__init__.py tests/cloud/uploads/__init__.py
git commit -m "feat(ee-uploads): FileUpload Beanie document + ALL_DOCUMENTS registration"
```

---

## Task 11: EE `MongoFileStore` — metadata persistence

**Files:**
- Create: `ee/cloud/uploads/mongo_store.py`
- Create: `tests/cloud/uploads/conftest.py` (Beanie fixture)
- Create: `tests/cloud/uploads/test_mongo_store.py`

**Step 1: Write the Beanie fixture**

`tests/cloud/uploads/conftest.py`:

```python
from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
async def beanie_upload_db():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    from ee.cloud.uploads.models import FileUpload

    db_name = f"test_uploads_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[FileUpload])
    yield db


@pytest.fixture()
async def store(beanie_upload_db):
    from ee.cloud.uploads.mongo_store import MongoFileStore
    return MongoFileStore()
```

**Step 2: Write failing tests**

`tests/cloud/uploads/test_mongo_store.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": "chat/202604/aaa.png",
        "filename": "cat.png",
        "mime": "image/png",
        "size": 1,
        "owner_id": "u1",
        "chat_id": "c1",
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


class TestMongoFileStore:
    async def test_save_then_get(self, store):
        await store.save_scoped(_record(), workspace="w1")
        got = await store.get_scoped("f1", workspace="w1")
        assert got is not None
        assert got.filename == "cat.png"

    async def test_cross_workspace_get_returns_none(self, store):
        await store.save_scoped(_record(), workspace="w1")
        assert await store.get_scoped("f1", workspace="w2") is None

    async def test_soft_delete_hides(self, store):
        await store.save_scoped(_record(), workspace="w1")
        await store.soft_delete_scoped("f1", workspace="w1")
        assert await store.get_scoped("f1", workspace="w1") is None
```

**Step 3: Run — expect fail**

Run: `uv run pytest tests/cloud/uploads/test_mongo_store.py -v`
Expected: FAIL — module not found.

**Step 4: Implement**

`ee/cloud/uploads/mongo_store.py`:

```python
"""Mongo-backed metadata store, workspace-scoped."""

from __future__ import annotations

from datetime import UTC, datetime

from ee.cloud.uploads.models import FileUpload
from pocketpaw.uploads.file_store import FileRecord


class MongoFileStore:
    """Workspace-scoped metadata store for EE uploads."""

    async def save_scoped(self, record: FileRecord, workspace: str) -> None:
        doc = FileUpload(
            file_id=record.id,
            storage_key=record.storage_key,
            filename=record.filename,
            mime=record.mime,
            size=record.size,
            workspace=workspace,
            owner=record.owner_id,
            chat_id=record.chat_id,
        )
        await doc.insert()

    async def get_scoped(self, file_id: str, workspace: str) -> FileRecord | None:
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.workspace == workspace,
            FileUpload.deleted_at == None,  # noqa: E711 beanie needs literal
        )
        if doc is None:
            return None
        return FileRecord(
            id=doc.file_id,
            storage_key=doc.storage_key,
            filename=doc.filename,
            mime=doc.mime,
            size=doc.size,
            owner_id=doc.owner,
            chat_id=doc.chat_id,
            created=doc.createdAt or datetime.now(UTC),
        )

    async def soft_delete_scoped(self, file_id: str, workspace: str) -> None:
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.workspace == workspace,
        )
        if doc is None:
            return
        doc.deleted_at = datetime.now(UTC)
        await doc.save()
```

**Step 5: Run — expect pass**

Run: `uv run pytest tests/cloud/uploads/test_mongo_store.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
git add ee/cloud/uploads/mongo_store.py tests/cloud/uploads/conftest.py tests/cloud/uploads/test_mongo_store.py
git commit -m "feat(ee-uploads): MongoFileStore with workspace-scoped reads"
```

---

## Task 12: EE `EEUploadService` — workspace-scoped wrapper

**Files:**
- Create: `ee/cloud/uploads/service.py`
- Create: `tests/cloud/uploads/test_service.py`

**Step 1: Write failing tests**

`tests/cloud/uploads/test_service.py`:

```python
from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"


class _MemAdapter(StorageAdapter):
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        buf = b""
        async for c in stream:
            buf += c
        self.blobs[key] = buf
        return StoredObject(key=key, size=len(buf), mime=mime)

    async def open(self, key):
        if key not in self.blobs:
            raise NotFound()
        yield self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)

    async def exists(self, key):
        return key in self.blobs


def _upload(content: bytes, filename: str, mime: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers={"content-type": mime},  # type: ignore[arg-type]
    )


class TestEEUploadService:
    async def test_upload_stores_record_in_workspace(self, store, tmp_path: Path):
        from ee.cloud.uploads.service import EEUploadService

        svc = EEUploadService(
            adapter=_MemAdapter(), meta=store, cfg=UploadSettings(local_root=tmp_path),
        )
        rec = await svc.upload(_upload(PNG, "cat.png", "image/png"),
                               owner_id="u1", chat_id="c1", workspace="w1")
        assert rec.owner_id == "u1"
        got = await store.get_scoped(rec.id, workspace="w1")
        assert got is not None

    async def test_stream_enforces_workspace(self, store, tmp_path: Path):
        from ee.cloud.uploads.service import EEUploadService

        svc = EEUploadService(
            adapter=_MemAdapter(), meta=store, cfg=UploadSettings(local_root=tmp_path),
        )
        rec = await svc.upload(_upload(PNG, "cat.png", "image/png"),
                               owner_id="u1", chat_id="c1", workspace="w1")
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id="u1", workspace="w2")
```

**Step 2: Run — expect fail**

Run: `uv run pytest tests/cloud/uploads/test_service.py -v`
Expected: FAIL.

**Step 3: Implement**

`ee/cloud/uploads/service.py`:

```python
"""EEUploadService — workspace-scoped upload pipeline."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import UploadFile

from ee.cloud.uploads.mongo_store import MongoFileStore
from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.service import (
    BulkUploadResult,
    UploadService,
)


class EEUploadService:
    """Thin adapter around UploadService that plumbs workspace through.

    Not a subclass — wrapping lets us keep the OSS UploadService's metadata
    parameter typed as JSONLFileStore while the EE variant uses Mongo.
    """

    def __init__(
        self,
        adapter: StorageAdapter,
        meta: MongoFileStore,
        cfg: UploadSettings,
    ) -> None:
        self._adapter = adapter
        self._meta = meta
        self._cfg = cfg
        # Reuse OSS validation + magic sniff via a stub meta that we never write to
        self._oss = UploadService(
            adapter=adapter,
            meta=_NullMeta(),  # validation only; we persist via Mongo below
            cfg=cfg,
        )

    async def upload(
        self, file: UploadFile, owner_id: str, chat_id: str | None, workspace: str,
    ) -> FileRecord:
        result = await self.upload_many([file], owner_id, chat_id, workspace)
        if result.failed:
            # Mirror OSS single-upload behavior
            from pocketpaw.uploads.service import _raise
            f = result.failed[0]
            _raise(f.code, f.reason)
        return result.uploaded[0]

    async def upload_many(
        self, files: list[UploadFile], owner_id: str, chat_id: str | None, workspace: str,
    ) -> BulkUploadResult:
        # Delegate validation + adapter write, then re-persist in Mongo
        result = await self._oss.upload_many(files, owner_id, chat_id)
        # _NullMeta ate the saves; re-save to Mongo with workspace
        for rec in result.uploaded:
            await self._meta.save_scoped(rec, workspace=workspace)
        return result

    async def stream(
        self, file_id: str, requester_id: str, workspace: str,
    ) -> tuple[FileRecord, AsyncIterator[bytes]]:
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            # v1 access: owner only. Chat-member check is a follow-up.
            raise NotFound()
        return rec, self._adapter.open(rec.storage_key)

    async def delete(
        self, file_id: str, requester_id: str, workspace: str,
    ) -> None:
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()
        await self._adapter.delete(rec.storage_key)
        await self._meta.soft_delete_scoped(file_id, workspace=workspace)


class _NullMeta:
    """Stub JSONLFileStore interface — swallows saves (EE persists in Mongo)."""

    def save(self, record: FileRecord) -> None:
        pass

    def get(self, file_id: str):
        return None

    def soft_delete(self, file_id: str) -> None:
        pass
```

**Step 4: Run — expect pass**

Run: `uv run pytest tests/cloud/uploads/test_service.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add ee/cloud/uploads/service.py tests/cloud/uploads/test_service.py
git commit -m "feat(ee-uploads): EEUploadService with workspace-scoped reads + writes"
```

---

## Task 13: EE uploads router

**Files:**
- Create: `ee/cloud/uploads/router.py`
- Create: `tests/cloud/uploads/test_router.py`
- Modify: wherever EE routers are mounted (check `ee/cloud/__init__.py` or the main EE app builder)

**Step 1: Read the pattern**

Read `ee/cloud/sessions/router.py` to see how EE routers obtain `workspace_id` + `user_id` (via `current_workspace_id`, `current_user_id` deps). The uploads router must use the same.

**Step 2: Write failing tests**

`tests/cloud/uploads/test_router.py`: mirror the OSS router tests, but with a fake `current_workspace_id` + `current_user_id` dependency set. Include:

- happy-path upload + download round-trip in workspace w1
- cross-workspace GET returns 404 (user in w2 tries to read w1's upload)
- bulk partial success
- DELETE by owner in right workspace → 204

(Full test file ~100 lines; model closely on `tests/cloud/uploads/conftest.py` + the OSS router test.)

**Step 3: Implement**

`ee/cloud/uploads/router.py` — mirror OSS shape but with workspace dep injection. Wire:
- `adapter = LocalStorageAdapter(root=workspace_root(workspace_id))` lazily per request (or pass in a factory that knows the workspace)
- Actually cleaner: one global adapter rooted at `~/.pocketpaw/uploads`, and EE storage keys are prefixed with workspace: e.g. key = `"{workspace}/chat/{yyyymm}/{uuid}{ext}"`. That matches the design doc.

```python
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.uploads.mongo_store import MongoFileStore
from ee.cloud.uploads.service import EEUploadService
from pocketpaw.uploads.config import INLINE_MIMES, UploadSettings
from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.local import LocalStorageAdapter

router = APIRouter(prefix="/uploads", tags=["Uploads"], dependencies=[Depends(require_license)])

# Single shared adapter + store. EE service prefixes keys with workspace.
_ROOT = Path.home() / ".pocketpaw" / "uploads"
_ADAPTER = LocalStorageAdapter(root=_ROOT)
_META = MongoFileStore()
_CFG = UploadSettings(local_root=_ROOT)
_SVC = EEUploadService(adapter=_ADAPTER, meta=_META, cfg=_CFG)


@router.post("")
async def upload(
    files: Annotated[list[UploadFile], File(...)],
    chat_id: Annotated[str | None, Form()] = None,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    try:
        result = await _SVC.upload_many(files, user_id, chat_id, workspace)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "uploaded": [
            {
                "id": r.id, "filename": r.filename, "mime": r.mime, "size": r.size,
                "url": f"/api/v1/uploads/{r.id}", "created": r.created.isoformat(),
            } for r in result.uploaded
        ],
        "failed": [asdict(f) for f in result.failed],
    }


@router.get("/{file_id}")
async def download(
    file_id: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> StreamingResponse:
    try:
        rec, it = await _SVC.stream(file_id, user_id, workspace)
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e
    disposition = "inline" if rec.mime in INLINE_MIMES else "attachment"
    return StreamingResponse(
        it, media_type=rec.mime,
        headers={"Content-Disposition": f'{disposition}; filename="{rec.filename}"'},
    )


@router.delete("/{file_id}", status_code=204)
async def delete_upload(
    file_id: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    try:
        await _SVC.delete(file_id, user_id, workspace)
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

**Step 4: Mount the router in EE app**

Find the EE router aggregator (search `from ee.cloud.sessions import router as sessions_router` or similar). Add the uploads router alongside:

```python
from ee.cloud.uploads.router import router as uploads_router
app.include_router(uploads_router, prefix="/api/v1")
```

Exact file depends on EE's app composition — mirror whatever pattern `sessions_router` uses.

**Step 5: Run — expect pass**

Run: `uv run pytest tests/cloud/uploads/test_router.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
git add ee/cloud/uploads/router.py tests/cloud/uploads/test_router.py <app-mount-file>
git commit -m "feat(ee-uploads): workspace-scoped /uploads router mounted in EE app"
```

---

## Task 14: Full verification + manual checklist

**Step 1: Lint + format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: no issues in new files.

**Step 2: Type check**

Run: `uv run mypy src/pocketpaw/uploads ee/cloud/uploads`
Expected: no errors in new modules.

**Step 3: Full fast test suite**

Run: `uv run pytest --ignore=tests/e2e -q`
Expected: all green (new upload suite + no regressions).

**Step 4: Manual smoke**

Start the app: `uv run pocketpaw --dev`

With `curl` or HTTPie, exercise:
- [ ] Single PNG upload → 200 with `uploaded[0]`
- [ ] Download same file → matching bytes, `Content-Disposition: inline`
- [ ] Upload 26 MiB file → `failed[0].code == "too_large"`
- [ ] Upload `.html` → `failed[0].code == "unsupported_mime"`
- [ ] Upload 3 files (1 good + 1 oversize + 1 bad-mime) → `uploaded=1, failed=2`
- [ ] Delete → 204; subsequent GET → 404
- [ ] Restart backend → previously uploaded file still downloadable
- [ ] Blob path is `~/.pocketpaw/uploads/chat/{yyyymm}/...` on disk

**Step 5: Final commit if any polish**

```bash
git add -u
git commit -m "chore(uploads): polish after manual verification"
```

---

## Open items (out of this PR, log for follow-up)

- Frontend integration: wire `ChatInput.svelte` to `POST /api/v1/uploads` and surface upload progress / failure reasons in the attachment chips.
- Chat-member access check on EE downloads (currently owner-only; group members can't fetch each other's attachments yet).
- S3 adapter (`ee/cloud/uploads/s3.py`) — additive; no change to OSS or service surface. Reuse `test_local_adapter.py` contract tests via parametrization.
- Cleanup job: scan for `.tmp` orphans older than 1h; scan metadata for records whose blob is missing.
- `on_put` hook on `LocalStorageAdapter` for future AV/malware scanning.
- Migrate existing ad-hoc upload sites (avatar, knowledge, soul imports) to `UploadService`.

---

## Plan complete

Plan saved to `docs/plans/2026-04-16-upload-adapter.md`.

Two execution options:

1. **Subagent-Driven (this session)** — I dispatch a fresh subagent per task, review between tasks.
2. **Parallel Session (separate)** — open a new Claude Code session with `superpowers:executing-plans`.

Which approach?
