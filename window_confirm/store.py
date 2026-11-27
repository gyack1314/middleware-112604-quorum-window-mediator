"""追加式事件日志与哈希链。

中间件的全部状态都是事件日志的折叠（event sourcing）。每条事件包含前一条
事件的哈希，形成可校验的哈希链，便于事后追查“当时依据了什么”。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    """规范化 JSON：键排序、无空白、不转义非 ASCII。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def event_hash(event_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(event_without_hash).encode("utf-8")
    ).hexdigest()


class EventStore:
    """每行一个事件的 JSONL 追加日志。"""

    FILENAME = "eventlog.jsonl"

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / self.FILENAME
        self._fh = self.path.open("a+", encoding="utf-8")
        self.last_seq = 0
        self.tip = GENESIS_HASH
        for event in self.read_all():
            self.last_seq = int(event["seq"])
            self.tip = str(event["hash"])

    def append(self, at: int | None, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.last_seq += 1
        event: dict[str, Any] = {
            "seq": self.last_seq,
            "at": at,
            "type": event_type,
            "payload": payload,
            "prev_hash": self.tip,
        }
        event["hash"] = event_hash(event)
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.tip = event["hash"]
        return event

    def read_all(self) -> list[dict[str, Any]]:
        """从磁盘读取全部事件；文件不存在或为空时返回空列表。"""

        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as source:
            for line_number, raw in enumerate(source, 1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"事件日志第 {line_number} 行不是合法 JSON：{error}"
                    ) from error
                events.append(event)
        return events

    def close(self) -> None:
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        finally:
            self._fh.close()

    def verify_chain(self) -> dict[str, Any]:
        """校验哈希链，返回 {ok, events, error?}。"""

        events = self.read_all()
        prev = GENESIS_HASH
        expected_seq = 1
        for event in events:
            if event.get("seq") != expected_seq:
                return {
                    "ok": False,
                    "events": len(events),
                    "error": f"事件序号不连续：期望 {expected_seq}，实际 {event.get('seq')}",
                }
            if event.get("prev_hash") != prev:
                return {
                    "ok": False,
                    "events": len(events),
                    "error": f"事件 {expected_seq} 的 prev_hash 不衔接",
                }
            stored_hash = event.get("hash")
            body = {k: v for k, v in event.items() if k != "hash"}
            if stored_hash != event_hash(body):
                return {
                    "ok": False,
                    "events": len(events),
                    "error": f"事件 {expected_seq} 的哈希不匹配（日志可能被篡改或损坏）",
                }
            prev = str(stored_hash)
            expected_seq += 1
        return {"ok": True, "events": len(events), "tip": prev}


def verify_file(path: str | Path) -> dict[str, Any]:
    """校验一个任意 JSONL 事件日志文件。"""

    p = Path(path)
    events: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as source:
        for raw in source:
            if raw.strip():
                events.append(json.loads(raw))
    prev = GENESIS_HASH
    for expected_seq, event in enumerate(events, 1):
        body = {k: v for k, v in event.items() if k != "hash"}
        if event.get("seq") != expected_seq or event.get("prev_hash") != prev:
            return {"ok": False, "events": len(events), "error": f"第 {expected_seq} 条事件链断裂"}
        if event.get("hash") != event_hash(body):
            return {"ok": False, "events": len(events), "error": f"第 {expected_seq} 条事件哈希不符"}
        prev = event["hash"]
    return {"ok": True, "events": len(events), "tip": prev}
