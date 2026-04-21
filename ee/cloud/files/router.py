"""EE /files router — unified workspace files listing.

Returns a single list that the paw-enterprise FilesPanel renders without
knowing which origin each row came from. Today the listing merges chat
S3 uploads with a (stubbed) Drive branch; local filesystem entries stay
client-side because the server has no canonical copy of the user's
workspace directory.

Created: 2026-04-19 — Cluster E sub-PR 4 (UI-TESTING-GUIDE §14 gap E8/E9).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ee.cloud.files.service import UnifiedFilesService
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(
    prefix="/files",
    tags=["Files"],
    dependencies=[Depends(require_license)],
)

_SVC = UnifiedFilesService()


@router.get("")
async def list_files(
    workspace_id: str | None = Query(None),
    source: Literal["chat", "local", "drive"] | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(current_user_id),
    current_workspace: str = Depends(current_workspace_id),
) -> JSONResponse:
    """List files in the caller's current workspace.

    ``workspace_id`` is accepted for explicitness but must match the
    caller's current workspace — cross-workspace listing is rejected.
    """
    if workspace_id and workspace_id != current_workspace:
        return JSONResponse(
            status_code=403,
            content={
                "detail": "workspace.mismatch",
                "message": "Cannot list files outside your current workspace.",
            },
        )

    files, warnings = await _SVC.list_unified(
        current_workspace, source=source, limit=limit
    )

    return JSONResponse(
        content={
            "workspace_id": current_workspace,
            "source": source or "all",
            "files": [
                {
                    "id": f.id,
                    "source": f.source,
                    "filename": f.filename,
                    "mime": f.mime,
                    "size": f.size,
                    "url": f.url,
                    "created": f.created.isoformat() if f.created else None,
                    "chat_id": f.chat_id,
                }
                for f in files
            ],
            "warnings": warnings,
        }
    )
