# Upload Adapter Service — Design

**Date:** 2026-04-16
**Scope:** Chat file uploads, shared adapter across OSS and EE tiers, local-disk backend now, S3 later
**Status:** Approved — ready for implementation plan

## Goal

Add a file upload service used by chat attachments. Ship a clean `StorageAdapter` abstraction so later work (S3, Azure Blob, etc.) swaps implementations without touching call sites. Support bulk uploads from day one to match the chat input's multi-attachment UX.

## Scope decisions

- **Use case**: chat attachments only. Existing ad-hoc upload surfaces (avatar, knowledge ingestion, soul imports) are out of scope — they can migrate to this adapter later.
- **Tiering**: both OSS and EE share the core adapter/service; each tier has its own router + metadata store.
  - OSS: single-user, local disk, JSONL metadata.
  - EE: multi-tenant, local-now / S3-later, Mongo metadata.
- **Access model**: auth-scoped opaque `file_id`. Clients never see the storage key. `GET /uploads/{id}` streams bytes after permission check.
- **Lifecycle**: persistent. Soft-delete on `DELETE`. Cascade from chat/message deletion is the chat code's problem, not the adapter's.
- **Out-of-scope for this PR**: AV/malware scanning, presigned URLs, cross-region replication, CDN, background cleanup of orphans, migration of existing avatar/KB uploads.

## Key constants

- `max_file_bytes = 25 MiB`
- `max_files_per_batch = 50` (matches ChatInput attachment cap)
- `allowed_mimes` — images (png/jpeg/webp/gif), pdf, plain text + markdown + csv, common source files, docx/xlsx. Configurable via env.
- Disallowed by design: `.html`, `.svg`, `.xml`, `.js`, executables, archives.

## Architecture

```
src/pocketpaw/uploads/                     ← OSS core (shared)
├── __init__.py
├── adapter.py         StorageAdapter protocol + StoredObject
├── local.py           LocalStorageAdapter (aiofiles, atomic writes)
├── errors.py          UploadError / TooLarge / UnsupportedMime / NotFound / AccessDenied
├── keys.py            new_storage_key(kind, ext) → "{kind}/{yyyymm}/{uuid}{ext}"
├── service.py         UploadService: validate → put → persist metadata
├── config.py          size/mime limits, local root, batch cap
└── file_store.py      OSS JSONL metadata store

src/pocketpaw/api/v1/uploads.py           ← OSS router
  POST   /uploads       multipart (files: list) → { uploaded, failed }
  GET    /uploads/{id}  auth check → stream bytes
  DELETE /uploads/{id}  owner check → adapter.delete + soft-delete metadata

ee/cloud/uploads/
├── __init__.py
├── models.py          FileUpload Beanie Document (Mongo)
├── service.py         EEUploadService — workspace scoping + RBAC
└── router.py          EE /uploads router (workspace-scoped deps)
```

**Principles**

- Adapter is dumb bytes-in/bytes-out keyed by string. No metadata, no auth, no mime logic.
- Metadata lives in the service layer. Different stores per tier (JSONL in OSS, Mongo in EE).
- Adapter wiring via config, not a plugin loader. `settings.upload_adapter = "local"` (default). S3 added as a branch in one factory when it lands.
- Storage keys are opaque and never exposed via API. Clients see only `file_id`. That keeps local/S3 interchangeable.

## Components

### `adapter.py`

```python
@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    mime: str

class StorageAdapter(Protocol):
    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str) -> StoredObject: ...
    async def open(self, key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
```

Streaming from day one so the S3 swap doesn't change the contract.

### `local.py`

`LocalStorageAdapter(root: Path)`. Writes to `root / key`. Creates parent dirs on `put`. Atomic: stream to `.tmp`, `os.replace` on completion. `delete` is idempotent. Asserts `key` resolves inside `root` after normalization (defense-in-depth).

### `keys.py`

```python
def new_storage_key(kind: str = "chat", ext: str = "") -> str
```

Returns `"{kind}/{yyyymm}/{uuid4hex}{ext}"`. `ext` is sanitized: lowercase, alphanumeric, capped at 8 chars.

### `errors.py`

`UploadError` base + subclasses: `TooLarge`, `UnsupportedMime`, `EmptyFile`, `NotFound`, `AccessDenied`, `StorageFailure`. Router maps each to an HTTP status.

### `config.py`

```python
@dataclass
class UploadSettings:
    max_file_bytes: int = 25 * 1024 * 1024
    max_files_per_batch: int = 50
    allowed_mimes: frozenset[str] = <sensible defaults>
    local_root: Path = Path.home() / ".pocketpaw" / "uploads"
```

Populated from `POCKETPAW_UPLOAD_*` env + JSON config.

### `service.py`

```python
@dataclass
class FileRecord:
    id: str; filename: str; mime: str; size: int
    url: str; created: datetime
    owner_id: str; chat_id: str | None; storage_key: str

@dataclass
class FailedUpload:
    filename: str
    reason: str                              # human-readable
    code: Literal["too_large", "unsupported_mime", "empty", "storage_error"]

@dataclass
class BulkUploadResult:
    uploaded: list[FileRecord]
    failed: list[FailedUpload]

class UploadService:
    def __init__(self, adapter: StorageAdapter, meta: MetadataStore, cfg: UploadSettings)
    async def upload(self, file: UploadFile, owner_id: str, chat_id: str | None) -> FileRecord
    async def upload_many(
        self, files: list[UploadFile], owner_id: str, chat_id: str | None,
    ) -> BulkUploadResult
    async def stream(self, file_id: str, requester_id: str) -> tuple[FileRecord, AsyncIterator[bytes]]
    async def delete(self, file_id: str, requester_id: str) -> None
```

`upload_many` iterates files; per-file errors accumulate into `failed[]` — they do not abort the batch. `upload` is thin sugar over `upload_many`.

### OSS `MetadataStore` (`file_store.py`)

JSONL at `~/.pocketpaw/uploads/_index.jsonl`. Append on create, append tombstone on delete. Load-all-on-startup into in-memory dict. Corrupt lines are skipped with a warning.

### EE `FileUpload` Beanie Document (`ee/cloud/uploads/models.py`)

```python
class FileUpload(TimestampedDocument):
    file_id: Indexed(str, unique=True)
    storage_key: str
    filename: str
    mime: str
    size: int
    workspace: Indexed(str)
    owner: str
    chat_id: Indexed(str) | None = None
    deleted_at: datetime | None = None

    class Settings:
        name = "file_uploads"
        indexes = [
            [("workspace", 1), ("chat_id", 1), ("createdAt", -1)],
            [("workspace", 1), ("owner", 1), ("createdAt", -1)],
        ]
```

### EE `EEUploadService`

Extends `UploadService`. Injects `workspace_id` and enforces tenant isolation on every read/delete. Cross-workspace access returns `NotFound` (not `AccessDenied`) to avoid leaking existence.

### Routers

`POST /uploads` (multipart form, `files: list[UploadFile]`, optional `chat_id`) — always returns 200 with `{uploaded, failed}`. 400 only for malformed requests (empty batch, > `max_files_per_batch`).

`GET /uploads/{id}` — `StreamingResponse` with `Content-Disposition: inline` for inline-safe mimes, `attachment` for everything else.

`DELETE /uploads/{id}` — owner-only (OSS); owner or workspace admin (EE). Idempotent.

## Data flow

**Upload single file**
```
client multipart
  → router: auth → UploadFile + chat_id (form)
  → service.upload(file, owner_id, chat_id)  # wraps upload_many([file])
      1. size check (mid-stream counter; reject > cap)
      2. mime check (magic-byte sniff on first 512 bytes; trust bytes over header)
      3. ext = mime→extension map
      4. key = new_storage_key("chat", ext)
      5. adapter.put(key, stream, mime) → StoredObject
      6. file_id = uuid4().hex
      7. meta.save(FileRecord(...))
  → { uploaded: [record], failed: [] }
```

**Bulk upload**

Same endpoint. For each file, independent validation + adapter call. Per-file failures land in `failed[]`; successful files still persist. No two-phase commit — partial success is explicit in the response and the chat UI renders accordingly.

**Download**
```
router: auth → requester_id
  → service.stream(file_id, requester_id)
      1. record = meta.get(file_id) or raise NotFound
      2. access check (OSS: owner match; EE: workspace match + (chat-member OR owner))
      3. return (record, adapter.open(record.storage_key))
  → StreamingResponse(iterator, mime=record.mime, headers={"Content-Disposition": ...})
```

**Delete**
```
router: auth → requester_id
  → service.delete(file_id, requester_id)
      1. record = meta.get(file_id) or raise NotFound
      2. access check (owner, or workspace admin in EE)
      3. adapter.delete(record.storage_key)  # idempotent
      4. meta.soft_delete(file_id)  # sets deleted_at; record retained for audit
```

**Adapter invariants**

- `storage_key` is opaque; clients never see it. `file_id` is the stable external handle.
- Adapter has no auth knowledge. Permission is enforced in the service.
- `put` must be atomic from the caller's perspective. Partial `.tmp` files on failure are adapter-local cleanup.

## Error handling

**Validation (4xx)**
- Size > cap → `TooLarge` (413 on single-file; `failed[]` entry with `code: "too_large"` in bulk)
- Disallowed mime → `UnsupportedMime` (415 / `code: "unsupported_mime"`)
- Empty file → `EmptyFile` (400 / `code: "empty"`)
- Filename with path separators → sanitized to basename; never used in storage key
- Empty batch or batch > `max_files_per_batch` → 400 (whole request rejected before any file is processed)

**Access (4xx)**
- Unauthenticated `GET` → 401
- Wrong workspace (EE) → 404 (not 403; don't leak existence)
- Not a chat member (EE) → 404
- Deleted record → 404 (not 410)

**Adapter (5xx)**
- Disk-full / IOError on `put` → `StorageFailure` (507 single / `code: "storage_error"` bulk). Metadata NOT saved — service is transactional at the adapter level.
- Orphaned metadata (key missing at stream time) → 500 + warning log. Record kept for cleanup.
- Delete of missing key → idempotent; metadata still soft-deleted.

**Security**
- Trust bytes, not client-declared `Content-Type`. Magic-byte sniff decides the canonical mime.
- `Content-Disposition: inline` only for images, pdf, plain text. Everything else gets `attachment`.
- Explicitly disallowed: `.html`, `.svg`, `.js`, `.xml`. No in-origin HTML/SVG rendering from user uploads.
- Path traversal: keys are generated server-side; adapter double-checks `root` containment after resolving the key.
- Size-limit spoof: `Content-Length` is never trusted; bytes are counted as they stream into the adapter.

**Races**
- Concurrent uploads of the same filename: each gets unique `file_id` + random `storage_key`. No collision possible.
- Delete during in-flight download: stream holds an OS file handle and continues. On Windows, `os.remove` on an open handle raises; wrap in try/except and defer to cleanup.
- Crash mid-upload: `.tmp` files orphaned; cleanup job (out of scope) handles eventually.

**Metadata store**
- OSS JSONL corruption: skip bad line, warn, continue.
- JSONL/Mongo write failure after a successful adapter `put`: record the adapter blob as orphaned; raise 500.

## Testing

**Unit — adapter** (`tests/uploads/test_local_adapter.py`)
- `put` writes correct bytes, returns `StoredObject` with matching size
- `put` creates missing parent dirs
- `put` atomic: mid-stream failure leaves no file at `key`, only `.tmp` (or nothing) — no partial read possible
- `open` streams; raises `NotFound` on missing key
- `delete` idempotent; path-traversal key → `AccessDenied`
- `exists` returns correct boolean

**Unit — keys** (`tests/uploads/test_keys.py`)
- Shape `{kind}/{yyyymm}/{hex32}{ext}`
- Extension sanitization: strips non-alphanumeric, lowercases, 8-char cap
- 1000 calls → 1000 unique keys

**Unit — service** (`tests/uploads/test_service.py`) against fake adapter + fake meta
- Happy path single and bulk
- Bulk partial-success: 1 good + 1 oversize + 1 bad-mime → `uploaded=1, failed=2` with distinct `code`s
- Empty batch → 400
- Batch > `max_files_per_batch` → 400 (reject before any processing)
- Magic-byte sniff overrides misdeclared `Content-Type`
- Empty file → `EmptyFile`
- `stream` with wrong owner → `NotFound` (not `AccessDenied`)
- `delete` idempotent
- Adapter `put` failure → meta NOT saved (transactional)

**Unit — OSS metadata store** (`tests/uploads/test_file_store.py`)
- Save → get roundtrip
- Soft-delete → `get` returns `None`
- Corrupt JSONL line skipped, valid lines loaded
- Cold start reload matches prior state

**Unit — EE Mongo store** (`tests/cloud/uploads/test_mongo_store.py`) against mongomock-motor
- Workspace isolation (wrong workspace → `None`)
- Chat-scoped listing
- Index shape

**Integration — routers** (`tests/uploads/test_router.py`, `tests/cloud/uploads/test_router.py`)
- `POST /uploads` multipart round-trip: upload → download same bytes
- Bulk: 3 files, one each of (good, too-large, bad-mime) → 200 with mixed response
- `GET` unauthenticated → 401; cross-workspace (EE) → 404
- `DELETE` non-owner → 404
- `Content-Disposition` `inline` for images; `attachment` for docx

**Contract test** — one parametrized suite any `StorageAdapter` must pass. Future `S3StorageAdapter` reuses the same tests with its own fixture.

**Out of scope**
- Load/performance tests (no SLA yet)
- Malware scanning (hook point documented only)
- Concurrency stress (rely on uvicorn defaults)

**Manual verification before merge**
- [ ] Upload 24 MiB file succeeds; 26 MiB fails with 413 (or `too_large` in bulk)
- [ ] Bulk upload 3 files (one oversize) → 200 with 2 `uploaded`, 1 `failed`
- [ ] End-to-end from ChatInput once frontend is wired (follow-up PR)
- [ ] `~/.pocketpaw/uploads/chat/{yyyymm}/` contains the blob
- [ ] Backend restart preserves uploads + metadata (OSS JSONL reload)
- [ ] EE: upload under workspace A → user in workspace B gets 404

## Dependencies

- Add: `aiofiles` (atomic async disk I/O)
- Add: `python-magic` OR manual magic-byte sniffing (prefer manual for no system-lib dep)
- Reuse: FastAPI `UploadFile`, Beanie (EE only)
- No new deps for S3 yet — added when S3 adapter lands

## Open items for implementation plan

- Exact mime allowlist list (compile default from the chat input accepts)
- Whether OSS serves files via FastAPI static mount OR always through `/uploads/{id}` — default to always-through-router for consistency with EE
- Where to bolt the `on_put` hook for future AV scanning (list of async callbacks on `LocalStorageAdapter`? Middleware on service? Defer to implementation)
