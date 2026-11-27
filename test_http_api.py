"""通过真实套接字调用标准库 HTTP API，验证持续接收消息与领取流程。"""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from window_confirm import ConfirmationService
from window_confirm.httpapi import serve_in_background


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ConfirmationService(Path(self._tmp.name) / "state")
        self.httpd, self.thread = serve_in_background(self.service, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self._tmp.cleanup()

    def call(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def post_message(self, at, kind, record):
        return self.call("POST", "/v1/messages",
                         {"at": at, "kind": kind, "record": record})

    def test_health_and_index(self):
        status, body = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("endpoints", body)

    def test_full_claim_revoke_and_restart_flow_over_http(self):
        self.post_message(0, "roster", {
            "scope": "line", "revision": 1, "members": ["a", "b"],
            "minimum": 2, "required": []})
        self.post_message(1, "proposal", {
            "scope": "line", "id": "w", "rev": 1, "roster": 1, "text": "open"})
        self.post_message(2, "vote", {
            "id": "w", "rev": 1, "roster": 1, "member": "a", "value": "yes"})
        self.post_message(2, "vote", {
            "id": "w", "rev": 1, "roster": 1, "member": "b", "value": "yes"})

        status, grant = self.post_message(3, "grant", {"id": "w", "rev": 1})
        self.assertEqual(status, 200)
        self.assertTrue(grant["granted"])
        auth_id = grant["authorization"]["auth_id"]
        token = grant["command"]["token"]

        status, claim = self.call("POST", "/v1/authorizations/claim",
                                  {"auth_id": auth_id, "token": token})
        self.assertEqual(status, 200)
        self.assertEqual(claim["outcome"], "delivered")

        # 领取后、started 前撤回 -> 授权停止；再 started 被拒绝
        _, withdrawn = self.post_message(4, "vote", {
            "id": "w", "rev": 1, "roster": 1, "member": "a", "value": "withdraw"})
        self.assertEqual(withdrawn["authorization_revoked"], auth_id)

        status, claim_again = self.call("POST", "/v1/authorizations/claim",
                                        {"id": "w", "rev": 1})
        self.assertEqual(status, 410)
        self.assertEqual(claim_again["outcome"], "revoked")

        status, started = self.call("POST", "/v1/executions/started",
                                    {"id": "w", "rev": 1})
        self.assertGreaterEqual(status, 400)
        self.assertFalse(started["accepted"])

        # HTTP 触发重启（同一数据目录重建），撤销事实仍可查
        status, restarted = self.call("POST", "/v1/restart", {})
        self.assertEqual(status, 200)
        self.assertTrue(restarted["restarted"])

        status, inspected = self.call("GET", "/v1/windows/w")
        self.assertEqual(status, 200)
        self.assertIsNone(inspected["live_authorization"])
        self.assertEqual(inspected["authorizations"][0]["status"], "revoked")
        self.assertFalse(inspected["live_authorization"] or False)

        # 事件审计接口可追查完整链路
        status, events = self.call("GET", "/v1/events")
        self.assertEqual(status, 200)
        self.assertTrue(events["chain"]["ok"])
        kinds = [e["type"] for e in events["events"]]
        self.assertIn("grant", kinds)
        self.assertIn("claim", kinds)

    def test_mutex_blocks_second_scope_window(self):
        for scope, wid in (("line", "x"), ("line", "y")):
            self.post_message(0, "roster", {
                "scope": scope, "revision": 1, "members": ["a", "b"],
                "minimum": 2, "required": []})
        for wid in ("x", "y"):
            self.post_message(1, "proposal", {
                "scope": "line", "id": wid, "rev": 1, "roster": 1, "text": "t"})
            for m in ("a", "b"):
                self.post_message(2, "vote", {
                    "id": wid, "rev": 1, "roster": 1, "member": m, "value": "yes"})
        _, gx = self.post_message(3, "grant", {"id": "x", "rev": 1})
        self.assertTrue(gx["granted"])
        self.call("POST", "/v1/executions/started", {"id": "x", "rev": 1})

        _, gy = self.post_message(4, "grant", {"id": "y", "rev": 1})
        self.assertEqual(gy["decision"], "blocked_mutex")

        self.call("POST", "/v1/executions/settled",
                  {"id": "x", "rev": 1, "state": "done"})
        _, gy2 = self.post_message(5, "grant", {"id": "y", "rev": 1})
        self.assertTrue(gy2["granted"])

        status, scope_view = self.call("GET", "/v1/scopes/line")
        self.assertEqual(status, 200)
        self.assertEqual(len(scope_view["windows"]), 2)


if __name__ == "__main__":
    unittest.main()
