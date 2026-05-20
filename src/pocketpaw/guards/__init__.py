# RBAC + ABAC guards — re-exports for clean imports.
# Created: 2026-04-10

from pocketpaw.guards.abac import (
    ACTION_ROLES,
    PLAN_FEATURES,
    ROLE_TOOL_LIMITS,
    evaluate_policy,
)
from pocketpaw.guards.actions import (
    ACTIONS,
    ActionRule,
    GroupRole,
    check_action,
    check_group_role,
    get_rule,
)
from pocketpaw.guards.audit import log_denial, log_privileged_action
from pocketpaw.guards.deps import (
    check_group_action,
    check_workspace_action,
    make_require_action,
    require_plan_feature,
    require_pocket_access,
    require_policy,
    require_role,
    resolve_group_role,
    resolve_workspace_role,
)
from pocketpaw.guards.policy import PolicyContext, PolicyResult
from pocketpaw.guards.rbac import (
    Forbidden,
    PocketAccess,
    WorkspaceRole,
    check_pocket_access,
    check_workspace_role,
)

__all__ = [
    "ACTION_ROLES",
    "ACTIONS",
    "ActionRule",
    "Forbidden",
    "GroupRole",
    "PLAN_FEATURES",
    "ROLE_TOOL_LIMITS",
    "PocketAccess",
    "PolicyContext",
    "PolicyResult",
    "WorkspaceRole",
    "check_action",
    "check_group_action",
    "check_group_role",
    "check_pocket_access",
    "check_workspace_action",
    "check_workspace_role",
    "evaluate_policy",
    "get_rule",
    "log_denial",
    "log_privileged_action",
    "make_require_action",
    "require_plan_feature",
    "require_pocket_access",
    "require_policy",
    "require_role",
    "resolve_group_role",
    "resolve_workspace_role",
]
