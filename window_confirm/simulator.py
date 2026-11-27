"""不需要真实设备的软件模拟执行端。

演示执行端如何“领取指令 → 回报 started → 回报 done/stopped”，并验证：
- 重复领取拿到同一指令；
- 未开始前授权被撤销时，领取得到 revoked，不会执行；
- 已开始后中间件不会因新的反对/撤回而另发互斥指令，只等待终态。
"""

from __future__ import annotations

from typing import Any

from .service import ConfirmationService


class SimulatedExecutor:
    def __init__(self, service: ConfirmationService, executor_id: str = "sim-executor-1"):
        self.service = service
        self.executor_id = executor_id
        # auth_id -> 已开始次数，避免演示脚本重复推进
        self.running: dict[str, dict[str, Any]] = {}

    def fetch(self, id: str, rev: int, at: int | None = None) -> dict[str, Any]:
        """领取并在接受指令后立即回报 started（模拟端接受已授动作）。"""

        claim = self.service.claim(
            {"id": id, "rev": rev, "executor_id": self.executor_id}, at=at
        )
        if claim.get("outcome") == "delivered":
            command = claim["command"]
            started = self.service.report_started(
                {"id": id, "rev": rev, "executor_id": self.executor_id},
                at=(at + 1 if at is not None else None),
            )
            self.running[command["auth_id"]] = {"id": id, "rev": rev}
            return {"claim": claim, "started": started}
        return {"claim": claim}

    def finish(
        self, id: str, rev: int, state: str = "done", at: int | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        return self.service.report_settled(
            {"id": id, "rev": rev, "state": state, "detail": detail}, at=at
        )
