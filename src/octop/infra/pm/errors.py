"""PM-specific error helpers.

PM uses two dedicated codes so callers can distinguish "state conflict" (409)
from "input rule violated" (400) without string matching. Both are registered in
``octop.infra.errors`` next to the host catalogue.
"""

from __future__ import annotations

from typing import Any

from octop.infra.errors import ErrorCode, OctopError


def conflict(message: str, **details: Any) -> OctopError:
    return OctopError(ErrorCode.PM_CONFLICT, message, details=dict(details))


def invalid(message: str, **details: Any) -> OctopError:
    return OctopError(ErrorCode.PM_VALIDATION, message, details=dict(details))


def not_found(kind: str, key: object) -> OctopError:
    return OctopError(
        ErrorCode.NOT_FOUND,
        f"{kind} not found",
        details={"kind": kind, "id": str(key)},
    )


def forbidden(message: str = "permission required", **details: Any) -> OctopError:
    return OctopError(ErrorCode.FORBIDDEN, message, details=dict(details))
