"""验证关掉服务、用同一数据目录重新构造（真实进程重启）后状态不丢。"""

import json
import tempfile
import unittest
from pathlib import Path

from window_confirm import ConfirmationService
from window_confirm.store import EventStore


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def test_execution_frontier_and_blockers_survive_real_reopen(self):
        svc = ConfirmationService(self.dir)
        svc.register_roster(
            {"scope": "line", "revision": 1, "members": ["a", "b"],
             "minimum": 2, "required": []}, at=0,
        )
        svc.propose(
            {"scope": "line", "id": "x", "rev": 1, "roster": 1, "text": "open"}, at=1
        )
        svc.vote({"id": "x", "rev": 1, "roster": 1, "member": "a", "value": "yes"}, at=2)
        svc.vote({"id": "x", "rev": 1, "roster": 1, "member": "b", "value": "yes"}, at=2)
        grant = svc.request_grant({"id": "x", "rev": 1}, at=3)
        auth_id = grant["authorization"]["auth_id"]
        svc.report_started({"id": "x", "rev": 1}, at=4)
        svc.close()

        # “进程重启”：全新对象从事件日志回放
        revived = ConfirmationService(self.dir)
        try:
            report = revived.inspect({"id": "x"})
            self.assertEqual(report["live_authorization"]["status"], "active")
            self.assertEqual(report["live_authorization"]["auth_id"], auth_id)
            self.assertEqual(report["scope_execution_frontier"]["mutex_held"], True)
            self.assertEqual(
                report["scope_execution_frontier"]["mutex_holder"]["id"], "x"
            )

            # 新窗口在重启后仍能收到提案/投票，但被真实执行边界挡住
            revived.propose(
                {"scope": "line", "id": "y", "rev": 1, "roster": 1, "text": "open"}, at=7
            )
            for m in ("a", "b"):
                revived.vote(
                    {"id": "y", "rev": 1, "roster": 1, "member": m, "value": "yes"}, at=8
                )
            blocked = revived.request_grant({"id": "y", "rev": 1}, at=9)
            self.assertEqual(blocked["decision"], "blocked_mutex")

            settled = revived.report_settled(
                {"id": "x", "rev": 1, "state": "stopped"}, at=10
            )
            self.assertTrue(settled["mutex_released"])
            allowed = revived.request_grant({"id": "y", "rev": 1}, at=11)
            self.assertEqual(allowed["decision"], "granted")

            chain = revived.store.verify_chain()
            self.assertTrue(chain["ok"], chain)
        finally:
            revived.close()

        # 再次重启：y 的已签发授权与 x 的 stopped 边界都在
        again = ConfirmationService(self.dir, boot_record=False)
        try:
            y = again.inspect({"id": "y"})
            self.assertEqual(y["live_authorization"]["status"], "valid")
            x = again.inspect({"id": "x"})
            self.assertIsNone(x["live_authorization"])
            self.assertEqual(x["current_authorization"]["terminal_state"], "stopped")
            # x 已释放；互斥槽由新获权的 y 持有
            self.assertTrue(x["scope_execution_frontier"]["mutex_held"])
            self.assertEqual(
                x["scope_execution_frontier"]["mutex_holder"]["id"], "y"
            )
        finally:
            again.close()

    def test_revoked_authorization_stays_revoked_across_restart(self):
        svc = ConfirmationService(self.dir)
        svc.register_roster(
            {"scope": "s", "revision": 1, "members": ["a", "b"],
             "minimum": 2, "required": []}, at=0
        )
        svc.propose({"scope": "s", "id": "p", "rev": 1, "roster": 1, "text": "t"}, at=1)
        for m in ("a", "b"):
            svc.vote({"id": "p", "rev": 1, "roster": 1, "member": m, "value": "yes"}, at=2)
        first = svc.request_grant({"id": "p", "rev": 1}, at=3)
        self.assertTrue(first["granted"])
        svc.vote({"id": "p", "rev": 1, "roster": 1, "member": "a", "value": "withdraw"}, at=4)
        self.assertEqual(
            svc.inspect({"id": "p"})["authorizations"][0]["status"], "revoked"
        )
        svc.close()

        revived = ConfirmationService(self.dir, boot_record=False)
        try:
            report = revived.inspect({"id": "p"})
            self.assertEqual(report["authorizations"][0]["status"], "revoked")
            self.assertIsNone(report["live_authorization"])
            claim = revived.claim({"id": "p", "rev": 1})
            self.assertEqual(claim["outcome"], "revoked")
        finally:
            revived.close()

    def test_event_chain_detects_tampering(self):
        svc = ConfirmationService(self.dir)
        svc.register_roster(
            {"scope": "s", "revision": 1, "members": ["a"], "minimum": 1,
             "required": []}, at=0
        )
        svc.close()
        log = self.dir / EventStore.FILENAME
        lines = log.read_text(encoding="utf-8").splitlines()
        tampered = False
        for i, line in enumerate(lines):
            row = json.loads(line)
            if row["type"] == "roster":
                row["payload"]["minimum"] = 999
                lines[i] = json.dumps(row, ensure_ascii=False)
                tampered = True
                break
        self.assertTrue(tampered)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        verifier = EventStore(self.dir)
        try:
            result = verifier.verify_chain()
        finally:
            verifier.close()
        self.assertFalse(result["ok"])
        self.assertIn("哈希", result["error"])


if __name__ == "__main__":
    unittest.main()
