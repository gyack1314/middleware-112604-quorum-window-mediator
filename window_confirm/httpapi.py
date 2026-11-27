"""仅依赖标准库的 HTTP/JSON 中间件接口。

站点与模拟执行端可以持续 POST 消息；也可以直接把演练 JSONL 的每一行
``{"at":..,"kind":..,"record":..}`` 依次 POST 到 ``/v1/messages``。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .service import ConfirmationService

POST_ROUTES = {
    "/v1/rosters": "roster",
    "/v1/proposals": "proposal",
    "/v1/votes": "vote",
    "/v1/grants": "grant",
    "/v1/authorizations/claim": "claim",
    "/v1/claims": "claim",
    "/v1/executions/started": "started",
    "/v1/started": "started",
    "/v1/executions/settled": "settled",
    "/v1/settled": "settled",
    "/v1/restart": "restart",
    "/v1/inspect": "inspect",
}

ERROR_STATUS = {
    "not_found": 404,
    "unknown_window_or_rev": 404,
    "no_authorization": 404,
    "unknown_authorization": 404,
    "unknown_roster": 409,
    "unknown_prev_roster": 409,
    "roster_revision_conflict": 409,
    "proposal_revision_conflict": 409,
    "revision_not_monotonic": 409,
    "id_scope_conflict": 409,
    "bad_token": 403,
    "authorization_revoked": 410,
}


def _status_for(result: dict[str, Any]) -> int:
    if result.get("ok"):
        return 200
    if isinstance(result.get("http_status"), int):
        return int(result["http_status"])
    return ERROR_STATUS.get(str(result.get("error")), 400)


def build_handler(service: ConfirmationService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "WindowConfirm/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 保持测试输出安静
            return

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                value = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(400, {"ok": False, "error": "bad_json", "message": str(error)})
                return None
            if not isinstance(value, dict):
                self._send_json(400, {"ok": False, "error": "bad_json",
                                      "message": "请求体必须是 JSON 对象"})
                return None
            return value

        # ------------------------------------------------------------ GET

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._dispatch_get()
            except Exception as error:  # 兜底：始终返回 JSON，不断连
                try:
                    self._send_json(500, {"ok": False, "error": "internal_error",
                                          "message": str(error)})
                except Exception:
                    pass

        def _dispatch_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}

            if path == "/healthz":
                self._send_json(200, {"ok": True, "service": "window-confirm"})
                return
            if path == "/":
                self._send_json(200, {
                    "service": "window-confirm",
                    "version": "v1",
                    "endpoints": {
                        "messages": "POST /v1/messages  {at?,kind,record}",
                        "roster": "POST /v1/rosters",
                        "proposal": "POST /v1/proposals",
                        "vote": "POST /v1/votes",
                        "grant": "POST /v1/grants",
                        "claim": "POST /v1/authorizations/claim",
                        "started": "POST /v1/executions/started",
                        "settled": "POST /v1/executions/settled",
                        "restart": "POST /v1/restart",
                        "inspect": "GET /v1/inspect?id=...&scope=...",
                        "window": "GET /v1/windows/{id}",
                        "scope_view": "GET /v1/scopes/{scope}",
                        "events": "GET /v1/events",
                    },
                })
                return
            if path == "/v1/inspect":
                result = service.inspect(query)
                self._send_json(200 if result.get("found", True) else 404, result)
                return
            if path.startswith("/v1/windows/"):
                wid = path.rsplit("/", 1)[-1]
                result = service.inspect({"id": wid, **({"rev": query["rev"]} if "rev" in query else {})})
                self._send_json(200 if result.get("found", True) else 404, result)
                return
            if path.startswith("/v1/scopes/"):
                scope = path.rsplit("/", 1)[-1]
                self._send_json(200, service.inspect({"scope": scope}))
                return
            if path == "/v1/events":
                with service.lock:
                    events = service.store.read_all()
                    chain = service.store.verify_chain()
                self._send_json(200, {"ok": True, "chain": chain, "events": events})
                return
            self._send_json(404, {"ok": False, "error": "not_found", "path": path})

        # ------------------------------------------------------------ POST

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._dispatch_post()
            except Exception as error:  # 兜底：始终返回 JSON，不断连
                try:
                    self._send_json(500, {"ok": False, "error": "internal_error",
                                          "message": str(error)})
                except Exception:
                    pass

        def _dispatch_post(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            body = self._read_json()
            if body is None:
                return

            if path == "/v1/messages":
                kind = body.get("kind")
                record = body.get("record")
                at = body.get("at")
                if not isinstance(kind, str):
                    self._send_json(400, {"ok": False, "error": "invalid_field",
                                          "message": "缺少 kind"})
                    return
                if record is None:
                    # 允许把 record 字段直接平铺在 body 中
                    record = {k: v for k, v in body.items() if k not in ("kind", "at")}
                result = service.handle_message(kind, record, at=at)
                self._send_json(_status_for(result), result)
                return

            kind = POST_ROUTES.get(path)
            if kind is None:
                self._send_json(404, {"ok": False, "error": "not_found", "path": path})
                return
            at = body.pop("at", None)
            result = service.handle_message(kind, body, at=at)
            self._send_json(_status_for(result), result)

    return Handler


def build_server(
    service: ConfirmationService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), build_handler(service))
    httpd.daemon_threads = True
    return httpd


def serve_in_background(
    service: ConfirmationService, host: str = "127.0.0.1", port: int = 0
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """测试/演示辅助：后台线程启动服务，返回 server 与线程。"""

    httpd = build_server(service, host, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread
