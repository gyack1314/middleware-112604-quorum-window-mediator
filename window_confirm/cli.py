"""命令行：回放演练 JSONL、启动 HTTP 服务、重启后检查、校验事件链、演示。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__
from .httpapi import build_server
from .render import render_any
from .runner import output_jsonl, run_file
from .service import ConfirmationService
from .store import EventStore

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _cmd_replay(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files]
    all_ok = True
    for index, path in enumerate(paths):
        if args.data_dir:
            base = Path(args.data_dir)
            if len(paths) == 1:
                data_dir = base
            else:
                data_dir = base / path.stem
        else:
            data_dir = Path(tempfile.mkdtemp(prefix=f"replay_{path.stem}_"))
        service, outputs = run_file(path, data_dir=data_dir)
        try:
            if args.text:
                print(f"=== {path.name}（data_dir={service.data_dir}）===")
                for item in outputs:
                    kind = item["kind"]
                    result = item["result"]
                    if kind == "grant":
                        print(f"[at={item['at']}] grant {item['record']} -> "
                              f"{result.get('decision')} granted={result.get('granted')}")
                        for owed in result.get("evaluation", {}).get("owed", []):
                            print(f"    · {owed['fact_zh']}")
                        if result.get("mutex_holder"):
                            print(f"    · 互斥占用：{result['mutex_holder']}")
                        if result.get("command"):
                            print(f"    · 指令 {result['command']['auth_id']} "
                                  f"basis={result['command']['basis_digest'][:12]}…")
                    elif kind == "inspect":
                        print(render_any(result))
                    elif kind == "restart":
                        print(f"[at={item['at']}] restart -> 恢复到 seq={result.get('recovered_seq')}")
                    elif not result.get("ok", True):
                        print(f"[at={item['at']}] {kind} 处理失败：{result.get('error')} "
                              f"{result.get('message', '')}")
                print()
            else:
                text = output_jsonl(outputs, include_record=not args.results_only)
                if len(paths) > 1:
                    for line in text.splitlines():
                        row = json.loads(line)
                        row["file"] = path.name
                        print(json.dumps(row, ensure_ascii=False))
                else:
                    print(text)
            if args.out:
                out = Path(args.out)
                out.write_text(
                    output_jsonl(outputs, include_record=not args.results_only),
                    encoding="utf-8",
                )
            chain = service.store.verify_chain()
            all_ok = all_ok and chain.get("ok", False)
        finally:
            service.close()
    return 0 if all_ok else 2


def _cmd_serve(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    service = ConfirmationService(data_dir)
    httpd = build_server(service, args.host, args.port)
    actual_port = httpd.server_address[1]
    print(f"window-confirm {__version__} HTTP 监听 http://{args.host}:{actual_port}", flush=True)
    print(f"数据/事件日志目录：{service.data_dir}", flush=True)
    print("接口索引：GET / ；健康检查：GET /healthz", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中…", flush=True)
    finally:
        httpd.server_close()
        service.close()
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    if not (data_dir / EventStore.FILENAME).exists():
        print(f"数据目录 {data_dir} 下没有 {EventStore.FILENAME}", file=sys.stderr)
        return 2
    service = ConfirmationService(data_dir, boot_record=False)
    try:
        if args.id:
            query: dict[str, Any] = {"id": args.id}
            if args.rev is not None:
                query["rev"] = args.rev
        elif args.scope:
            query = {"scope": args.scope}
        else:
            query = {}
        report = service.inspect(query)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_any(report))
        return 0 if report.get("ok", True) else 1
    finally:
        service.close()


def _cmd_verify(args: argparse.Namespace) -> int:
    store = EventStore(Path(args.data_dir))
    try:
        result = store.verify_chain()
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def _cmd_demo(args: argparse.Namespace) -> int:
    """运行两个内置演练场景，打印可读过程并校验关键结论。"""

    scenarios = [
        EXAMPLES / "joint-membership-transition.jsonl",
        EXAMPLES / "withdraw-after-real-start.jsonl",
    ]
    failures: list[str] = []
    for path in scenarios:
        data_dir = Path(tempfile.mkdtemp(prefix=f"demo_{path.stem}_"))
        service, outputs = run_file(path, data_dir=data_dir)
        print("=" * 78)
        print(f"场景：{path.name}    持久化目录：{data_dir}")
        print("=" * 78)
        for item in outputs:
            kind = item["kind"]
            result = item["result"]
            at = item["at"]
            if kind in ("roster", "proposal") and result.get("ok"):
                print(f"[{at:>2}] {kind:8} 已接收 {item['record']}")
            elif kind == "vote":
                elig = result.get("eligibility", {})
                print(f"[{at:>2}] vote     {item['record']['member']} "
                      f"{item['record']['value']} (r{item['record']['roster']}) "
                      f"eligible={elig.get('eligible')}")
            elif kind == "grant":
                print(f"[{at:>2}] grant    -> {result.get('decision')} "
                      f"granted={result.get('granted')}")
                for owed in result.get("evaluation", {}).get("owed", []):
                    print(f"             欠：{owed['fact_zh']}")
                if result.get("mutex_holder"):
                    h = result["mutex_holder"]
                    print(f"             互斥被占用：{h['id']} rev={h['rev']} {h['status']}")
                auth = result.get("authorization")
                if auth:
                    print(f"             授权 {auth['auth_id']} issued_at={auth['issued_at']} "
                          f"可撤回={auth['withdrawable']}")
            elif kind == "started":
                print(f"[{at:>2}] started  -> accepted={result.get('accepted')} "
                      f"status={result.get('status')}")
            elif kind == "settled":
                print(f"[{at:>2}] settled  -> {result.get('status')} "
                      f"mutex_released={result.get('mutex_released')}")
            elif kind == "restart":
                print(f"[{at:>2}] restart  -> 从事件日志恢复 seq={result.get('recovered_seq')}")
            elif kind == "inspect":
                print("-" * 78)
                print(render_any(result))
                print("-" * 78)

        # 关键断言（与交付说明中的语义保持一致）
        grants = [o["result"] for o in outputs if o["kind"] == "grant"]
        if path.name.startswith("joint"):
            if grants[0].get("decision") != "denied_quorum":
                failures.append("过渡场景 at=5 的 grant 应因新旧两组都不足而被拒绝")
            owed_types = {(o["side"], o["type"]) for o in grants[0]["evaluation"]["owed"]}
            if ("old", "independent_sites") not in owed_types:
                failures.append("at=5 应说明欠旧组独立站点")
            if ("new", "required_member") not in owed_types:
                failures.append("at=5 应说明欠新组指定责任方 d")
            if grants[1].get("decision") not in ("granted", "reissued"):
                failures.append("b 补旧组、d 补新组后 at=8 应签发授权")
        else:
            if grants[0].get("decision") != "granted":
                failures.append("x 在 at=3 应获权")
            x_start = next(o["result"] for o in outputs if o["kind"] == "started")
            if x_start.get("status") != "active":
                failures.append("x 应已开始")
            withdraw = next(
                o["result"] for o in outputs
                if o["kind"] == "vote" and o["record"]["value"] == "withdraw"
            )
            if withdraw.get("authorization_status") != "active" or withdraw.get("revocable") is not False:
                failures.append("开始后的撤回不能停止执行，应明确不可撤回并等待终态")
            after_restart_y = grants[1]
            if after_restart_y.get("decision") != "blocked_mutex":
                failures.append("重启后 y at=9 仍应被 x 的未终态执行互斥阻挡")
            if grants[2].get("decision") != "granted":
                failures.append("x stopped 后 y at=11 应获权")
        service.close()

    print()
    if failures:
        print("DEMO 校验失败：", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)
        return 1
    print("DEMO 全部关键结论符合预期：过渡双条件、互斥、开始后不可撤、重启边界恢复。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="window_confirm", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_replay = sub.add_parser("replay", help="按顺序回放一个或多个 JSONL 演练记录")
    p_replay.add_argument("files", nargs="+")
    p_replay.add_argument("--data-dir", help="持久化目录；多文件时每个场景使用其子目录")
    p_replay.add_argument("--out", help="把 JSONL 结果同时写入文件")
    p_replay.add_argument("--text", action="store_true", help="输出中文过程摘要而非完整 JSON")
    p_replay.add_argument("--results-only", action="store_true", help="JSONL 中不回带 record")
    p_replay.set_defaults(func=_cmd_replay)

    p_serve = sub.add_parser("serve", help="启动 HTTP/JSON 中间件服务")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--data-dir", default=".window_confirm")
    p_serve.set_defaults(func=_cmd_serve)

    p_inspect = sub.add_parser("inspect", help="从持久化日志恢复并检查窗口/范围")
    p_inspect.add_argument("--data-dir", required=True)
    target = p_inspect.add_mutually_exclusive_group()
    target.add_argument("--id")
    target.add_argument("--scope")
    p_inspect.add_argument("--rev", type=int)
    p_inspect.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    p_inspect.add_argument("--text", action="store_true", help="输出中文文本视图（默认）")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_verify = sub.add_parser("verify", help="校验事件哈希链")
    p_verify.add_argument("--data-dir", required=True)
    p_verify.set_defaults(func=_cmd_verify)

    p_demo = sub.add_parser("demo", help="运行两个内置场景并校验关键结论")
    p_demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
