"""联合演练共享模拟窗口的分布式确认中间件。

只管理软件模拟窗口：多站点通过 HTTP 或 Python API 提交成员配置、提案、
投票、授权领取与终态回报。所有事实以哈希链事件日志持久化，重启后可还原
“真实已执行边界”和窗口仍然不能推进的原因。
"""

from .engine import (
    AUTHORIZATION_LIVE,
    AUTHORIZATION_CLOSED,
    VALUE_VALUES,
    CommandError,
    Engine,
)
from .service import ConfirmationService

__version__ = "1.0.0"

# 常见命名别名，便于不同调用习惯
ConfirmationMiddleware = ConfirmationService
Middleware = ConfirmationService
Coordinator = ConfirmationService

__all__ = [
    "ConfirmationService",
    "ConfirmationMiddleware",
    "Middleware",
    "Coordinator",
    "Engine",
    "CommandError",
    "AUTHORIZATION_LIVE",
    "AUTHORIZATION_CLOSED",
    "VALUE_VALUES",
    "__version__",
]
