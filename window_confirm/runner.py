"""把演练 JSONL 按到达顺序持续送入 :class:`ConfirmationService`。

每个 JSONL 文件是一段独立场景，使用独立数据目录；遇到 ``restart`` 消息时
从哈希链事件日志重建服务，以验证重启后的真实状态。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

# 复用仓库根目录中已登记消息类型的读取器
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scenario_reader import iter_messages  # noqa: E402

from .service import ConfirmationService  # noqa: E402

DEFAULT_KINDS = {
    "roster", "proposal", "vote", "grant", "started", "settled", "restart", "inspect",
}


def load_supported_kinds(examples_dir: Path | None = None) -> set[str]:
    candidates: list[Path] = []
    if examples_dir is not None:
        candidates.append(Path(examples_dir) / "message_types.json")
    candidates.append(_ROOT / "examples" / "message_types.json")
    for path in candidates:
        if path.exists():
            return set(json.loads(path.read_text(encoding="utf-8"))) | {
                "claim", "fetch", "collect",
            }
    return set(DEFAULT_KINDS) | {"claim", "fetch", "collect"}


def run_messages(
    messages: Iterable[dict[str, Any]],
    service: ConfirmationService,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for row in messages:
        at = row["at"]
        kind = row["kind"]
        record = row["record"]
        result = service.handle_message(kind, record, at=at)
        outputs.append({"at": at, "kind": kind, "record": record, "result": result})
    return outputs


def run_file(
    path: str | Path,
    *,
    data_dir: str | Path | None = None,
    service: ConfirmationService | None = None,
    supported: set[str] | None = None,
) -> tuple[ConfirmationService, list[dict[str, Any]]]:
    """回放单个 JSONL。返回 service（便于测试继续查询）与逐行输出。"""

    path = Path(path)
    if supported is None:
        sibling_registry = path.parent / "message_types.json"
        supported = load_supported_kinds(
            path.parent if sibling_registry.exists() else None
        )
    if service is None:
        if data_dir is None:
            data_dir = Path(tempfile.mkdtemp(prefix=f"scenario_{path.stem}_"))
        service = ConfirmationService(data_dir)
    messages = list(iter_messages(path, supported))
    outputs = run_messages(messages, service)
    return service, outputs


def output_jsonl(outputs: list[dict[str, Any]], *, include_record: bool = True) -> str:
    lines = []
    for item in outputs:
        row = {"at": item["at"], "kind": item["kind"]}
        if include_record:
            row["record"] = item["record"]
        row["result"] = item["result"]
        lines.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(lines)
