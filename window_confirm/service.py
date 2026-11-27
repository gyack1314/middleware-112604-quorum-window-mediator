"""服务层：把引擎决策串行化并写入哈希链事件日志。

对站点侧暴露统一的 :meth:`ConfirmationService.handle_message`，其消息名称与
``examples/message_types.json`` 完全一致；另外提供执行端“领取指令”的
``claim`` 入口。
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .engine import CommandError, Engine
from .store import EventStore


class ConfirmationService:
    def __init__(self, data_dir: str | Path | None = None, *, boot_record: bool = True):
        if data_dir is None:
            data_dir = Path(tempfile.mkdtemp(prefix="window_confirm_"))
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.store = EventStore(self.data_dir)
        events = self.store.read_all()
        self.engine = Engine.replay(events)
        if boot_record:
            self._record(
                self._now(),
                "boot",
                {"reason": "service_start", "recovered_seq": self.engine.seq,
                 "event_count": len(events)},
            )

    @staticmethod
    def _now() -> int:
        return int(time.time())

    def _record(self, at: int | None, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.store.append(at, event_type, payload)
        self.engine.apply(event)
        return event

    # ------------------------------------------------------------- main entry

    def handle_message(
        self,
        kind: str,
        record: dict[str, Any] | None = None,
        at: int | float | str | None = None,
    ) -> dict[str, Any]:
        record = record if record is not None else {}
        try:
            timestamp = int(at) if at is not None else self._now()
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_field",
                    "message": f"at 必须是整数秒点，实际为 {at!r}", "http_status": 400}
        if kind == "inspect":
            return self.inspect(record)
        if kind == "restart":
            return self.simulate_restart(timestamp, record)
        with self.lock:
            try:
                result, events = self.engine.plan_message(kind, timestamp, record)
            except CommandError as error:
                return {
                    "ok": False,
                    "error": error.code,
                    "message": str(error),
                    "http_status": error.http_status,
                }
            for event_type, payload in events:
                self._record(timestamp, event_type, payload)
            return result

    # Convenience named methods ----------------------------------------------

    def register_roster(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.handle_message("roster", record, at)

    def propose(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.handle_message("proposal", record, at)

    def vote(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.handle_message("vote", record, at)

    def request_grant(self, id_or_record: str | dict[str, Any], rev: int | None = None,
                      at: int | None = None) -> dict[str, Any]:
        if isinstance(id_or_record, dict):
            return self.handle_message("grant", id_or_record, at)
        return self.handle_message("grant", {"id": id_or_record, "rev": rev}, at)

    def claim(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.handle_message("claim", record, at)

    def report_started(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.handle_message("started", record, at)

    def report_settled(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.handle_message("settled", record, at)

    # ----------------------------------------------------------------- inspect

    def inspect(self, query: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            report = self.engine.inspect(query or {})
            try:
                report["event_chain"] = self.store.verify_chain()
            except (OSError, ValueError) as error:
                report["event_chain"] = {"ok": False, "error": str(error)}
            report["data_dir"] = str(self.data_dir)
            return report

    # ---------------------------------------------------------------- restart

    def simulate_restart(
        self, at: int | None = None, record: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """关闭并从事件日志重建内存状态，等价于进程重启后的恢复过程。"""

        timestamp = int(at) if at is not None else self._now()
        with self.lock:
            recovered = self.store.read_all()
            recovered_seq = recovered[-1]["seq"] if recovered else 0
            self.store.close()
            self.store = EventStore(self.data_dir)
            self.engine = Engine.replay(recovered)
            boot = self._record(
                timestamp,
                "boot",
                {"reason": "restart_message", "recovered_seq": recovered_seq,
                 "event_count": len(recovered), "record": record or {}},
            )
            overview = self.engine.inspect_global()
            return {
                "ok": True,
                "kind": "restart",
                "restarted": True,
                "at": timestamp,
                "boot_seq": boot["seq"],
                "recovered_seq": recovered_seq,
                "scopes": overview["scopes"],
                "message": "已从持久化事件日志重建；真实已执行边界与互斥占用均已恢复",
            }

    def close(self) -> None:
        with self.lock:
            self.store.close()
