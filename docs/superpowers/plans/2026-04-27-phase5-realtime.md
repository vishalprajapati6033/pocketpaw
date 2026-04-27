# Phase 5: `realtime/` → `_core/realtime/`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Move the realtime subsystem (`bus.py`, `emit.py`, `audience.py`, `events.py`) into `_core/realtime/`. It's framework, not domain — services emit events; the bus is infrastructure. 66 import sites continue to work via re-export shims at the old path.

**Architecture:** Pure file relocation + shim. No behavior changes. The `EventBus` Protocol becomes accessible from `_core/ports.py` (re-exported from the new `_core/realtime/bus.py`).

**Tech Stack:** Python; no new deps.

---

## Files moved

| From | To |
|---|---|
| `ee/cloud/realtime/__init__.py` | `ee/cloud/_core/realtime/__init__.py` |
| `ee/cloud/realtime/events.py` | `ee/cloud/_core/realtime/events.py` |
| `ee/cloud/realtime/audience.py` | `ee/cloud/_core/realtime/audience.py` |
| `ee/cloud/realtime/bus.py` | `ee/cloud/_core/realtime/bus.py` |
| `ee/cloud/realtime/emit.py` | `ee/cloud/_core/realtime/emit.py` |

After move: cross-imports inside the moved files are rewritten from `ee.cloud.realtime.X` to `ee.cloud._core.realtime.X`.

## Shims kept at the old path

`ee/cloud/realtime/{events,audience,bus,emit}.py` become re-export shims:
```python
"""Re-export shim. Canonical home moved to ``ee.cloud._core.realtime.<X>``
in Phase 5 of the cloud-restructure (2026-04-27)."""
from ee.cloud._core.realtime.<X> import *  # noqa: F401, F403
```

`__all__` is preserved so wildcard re-exports work cleanly.

## `_core/ports.py` adjustment

Add an `EventBus` re-export so consumers can import the port from the canonical location:
```python
from ee.cloud._core.realtime.bus import EventBus  # noqa: F401
```

## Tasks

### Task 1: Move files
- `git mv` each file. Preserves git history.
- Update intra-realtime imports (`from ee.cloud.realtime.X` → `from ee.cloud._core.realtime.X`)

### Task 2: Create shims
- New `ee/cloud/realtime/{events,audience,bus,emit}.py` re-export from `_core/realtime/`
- `__all__` preserves the public surface

### Task 3: Re-export EventBus from `_core/ports.py`
- Add the import + `__all__` entry

### Task 4: Identity test
- `tests/cloud/_core/test_realtime_shims.py` — `EventBus`, `InProcessBus`, `emit`, `Event`, `AudienceResolver`, `set_bus`/`get_bus`/`set_resolver`/`get_resolver` are the same Python object via either import path

### Task 5: Verify
- All cumulative tests pass
- Broader cloud baseline unchanged
