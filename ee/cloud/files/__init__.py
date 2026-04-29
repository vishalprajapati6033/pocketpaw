"""Files aggregation module — unified /files endpoint + Files Tab v2 (tree/browse)."""

from ee.cloud.files.bootstrap import build_files_router
from ee.cloud.files.dto import (
    Capability,
    FileEntry,
    FolderNode,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
    Scope,
    SearchQuery,
)
from ee.cloud.files.registry import FolderProvider, ProviderRegistry
from ee.cloud.files.router import build_router, router  # noqa: F401

__all__ = [
    "Capability",
    "FileEntry",
    "FolderNode",
    "FolderProvider",
    "MountConfig",
    "Page",
    "Permission",
    "ProviderRegistry",
    "RequestContext",
    "ResolvedMount",
    "Scope",
    "SearchQuery",
    "build_files_router",
    "build_router",
    "router",
]
