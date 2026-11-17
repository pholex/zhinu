"""Application metadata writes share the execution service's state-directory lease."""
from __future__ import annotations


class OwnershipUnavailable(OSError):
    """Read-only access remains possible after the application owner is lost."""


def require_owner(app) -> None:
    """Require the app lease only; session-level faults do not block safety receipts."""
    owner = getattr(getattr(app, "state", None), "execution_owner", None)
    if owner is None or not getattr(owner, "held", False):
        raise OwnershipUnavailable("工作台状态目录未持有执行所有权；仍可读取，请重启服务进程后核对。")
