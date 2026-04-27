"""Canonical error hierarchy for ee/cloud.

In Phase 0 this module re-exports the existing classes from
`ee/cloud/shared/errors.py` and adds two missing ones (`RateLimited`,
`Internal`) plus a `with_cause` helper. Phase 1 moves the canonical
definitions here and turns `shared/errors.py` into a re-export shim.

Routers must never raise `HTTPException`; services raise these
`CloudError` subclasses and `_core.http.cloud_error_handler` maps them
to JSON responses.
"""

from __future__ import annotations

from ee.cloud.shared.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
    ValidationError,
)


class RateLimited(CloudError):
    """Rate limit exceeded (429)."""

    def __init__(self, code: str, message: str = "Rate limit exceeded") -> None:
        super().__init__(429, code, message)


class Internal(CloudError):
    """Unexpected internal error (500). Use sparingly — prefer specific codes."""

    def __init__(self, code: str = "internal", message: str = "Internal server error") -> None:
        super().__init__(500, code, message)


def with_cause(error: CloudError, cause: BaseException) -> CloudError:
    """Attach an underlying exception for log context. Returns the same error
    so it can be used fluently: `raise with_cause(NotFound(...), exc)`.

    The cause is stored on `__cause__` (Python's standard exception-chaining
    slot). The `to_dict()` envelope sent to clients still contains only
    `code` and `message`; the cause is not leaked.
    """
    error.__cause__ = cause
    return error


__all__ = [
    "CloudError",
    "ConflictError",
    "Forbidden",
    "Internal",
    "NotFound",
    "RateLimited",
    "SeatLimitError",
    "ValidationError",
    "with_cause",
]
