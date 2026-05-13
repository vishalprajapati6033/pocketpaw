"""Cycles — daily snapshot job.

Captures one (scope, started, completed) data point per active cycle per
day. The output feeds the burnup chart in the paw-enterprise Cycles tab
(Linear-style: gray scope, blue dashed target — flattened on weekends via
``is_weekend`` — yellow started, solid blue completed).

This module ships the callable (``snapshot_all_active``) plus an optional
``run_forever_loop`` that sleeps 24h between passes. **It does not register
itself onto a scheduler.** Whatever cron primitive the host platform uses
(Kubernetes CronJob, Celery beat, APScheduler, OS cron) imports this module
and dispatches ``snapshot_all_active`` once per day per workspace. The
``mount_cloud()`` startup hook in ``ee/cloud/__init__.py`` is the natural
wiring point; until the platform's scheduling story converges, the captain
wires this in deployment.

When the Tasks entity (PR 2 of the Mission Control series) isn't available
on the importing branch, the job warns and returns rather than crashing —
the cycles entity continues to serve list / detail / close endpoints
normally; the daily array stays empty.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud.cycles import service as cycles_service

logger = logging.getLogger(__name__)


def _system_ctx(workspace_id: str) -> RequestContext:
    """Build a service-level RequestContext for the snapshot job.

    The job is a system actor — no user — so ``user_id`` is a sentinel.
    ``request_id`` is fixed to make log correlation across days easier.
    """
    return RequestContext(
        user_id="system.cycles.snapshot",
        workspace_id=workspace_id,
        request_id="cycle-snapshot",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def snapshot_all_active(workspace_id: str) -> int:
    """Snapshot every active cycle in a workspace.

    Returns the number of cycles successfully snapshotted. Idempotent via
    ``cycles_service._snapshot_cycle_daily`` — calling this twice in the
    same calendar day is a no-op on the second pass.
    """
    # Late import: keep ``ee.cloud.models`` off the module-level path so
    # snapshot_job stays free to be imported by deployment wiring without
    # pulling Beanie into the import graph in environments that haven't
    # initialised it.
    from ee.cloud.models.cycle import Cycle as _CycleDoc

    ctx = _system_ctx(workspace_id)
    count = 0
    async for doc in _CycleDoc.find({"workspace": workspace_id, "status": "active"}):
        cycle_id = str(doc.id)
        try:
            point = await cycles_service._snapshot_cycle_daily(ctx, cycle_id)
            if point is not None:
                count += 1
        except Exception:
            logger.warning(
                "cycle.snapshot failed for workspace=%s cycle=%s",
                workspace_id,
                cycle_id,
                exc_info=True,
            )
    logger.info("cycle.snapshot completed: workspace=%s snapshotted=%d", workspace_id, count)
    return count


async def snapshot_one(workspace_id: str, cycle_id: str) -> bool:
    """Snapshot a single cycle by id. Useful for ad-hoc CLI invocations.

    Returns ``True`` if a new point was appended, ``False`` if today's
    point already existed (idempotent), the cycle is completed, or Tasks
    isn't available.
    """
    try:
        PydanticObjectId(cycle_id)
    except Exception:
        logger.warning("cycle.snapshot: invalid cycle id %s", cycle_id)
        return False
    ctx = _system_ctx(workspace_id)
    point = await cycles_service._snapshot_cycle_daily(ctx, cycle_id)
    return point is not None


async def run_forever_loop(iter_workspaces, *, interval_seconds: float = 24 * 60 * 60) -> None:
    """Run the snapshot job in a perpetual loop.

    ``iter_workspaces`` is an awaitable callable returning the list of
    workspace ids to scan on each pass — kept as a parameter rather than
    inlined here so deployment wiring can plug in workspace discovery
    however it wants (per-tenant DB, multi-tenant collection scan,
    static config).

    The captain wires this into the host platform's scheduler if a
    persistent loop is preferred over a cron-style invocation. For a
    plain ``mount_cloud()`` setup, prefer the per-pass ``snapshot_all_active``
    triggered by an external scheduler instead — running a hot loop in
    the same process as the FastAPI app couples the job's reliability to
    request-handling uptime.
    """
    while True:
        try:
            workspaces = await iter_workspaces()
            for ws in workspaces:
                await snapshot_all_active(ws)
        except Exception:
            logger.exception("cycle.snapshot run_forever_loop pass failed")
        await asyncio.sleep(interval_seconds)


__all__ = ["run_forever_loop", "snapshot_all_active", "snapshot_one"]
