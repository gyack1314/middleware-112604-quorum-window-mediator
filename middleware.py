"""分布式确认中间件的根级门面。

真正实现位于 :package:`window_confirm` 包。本模块仅用任务措辞中最直接的名字
再导出一层，方便 ``from middleware import DistributedConfirmationMiddleware``
这样的调用方式。

快速使用::

    from middleware import DistributedConfirmationMiddleware

    mw = DistributedConfirmationMiddleware("./state")
    mw.handle_message("roster", {...}, at=0)
    print(mw.request_grant({"id": "go", "rev": 1}, at=3))
"""

from __future__ import annotations

from window_confirm import (
    AUTHORIZATION_CLOSED,
    AUTHORIZATION_LIVE,
    CommandError,
    ConfirmationService,
    Coordinator,
    Engine,
    Middleware,
)
from window_confirm.httpapi import build_server
from window_confirm.runner import run_file

__all__ = [
    "DistributedConfirmationMiddleware",
    "ConfirmationService",
    "Middleware",
    "Coordinator",
    "Engine",
    "CommandError",
    "AUTHORIZATION_LIVE",
    "AUTHORIZATION_CLOSED",
    "build_server",
    "run_file",
]


class DistributedConfirmationMiddleware(ConfirmationService):
    """就是 :class:`window_confirm.ConfirmationService`，命名更贴任务描述。"""
