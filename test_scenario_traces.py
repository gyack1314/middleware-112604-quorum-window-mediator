"""直接回放 examples/ 下的两个契约场景，锁定关键轨迹。"""

import tempfile
import unittest
from pathlib import Path

from window_confirm import ConfirmationService
from window_confirm.runner import run_file

EXAMPLES = Path(__file__).with_name("examples")


class ExampleScenarioTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, name):
        service, outputs = run_file(EXAMPLES / name, data_dir=self.data / name)
        self.addCleanup(service.close)
        return service, outputs

    def test_joint_membership_transition_trace(self):
        service, outputs = self._run("joint-membership-transition.jsonl")
        grants = [o for o in outputs if o["kind"] == "grant"]
        inspects = [o for o in outputs if o["kind"] == "inspect"]

        denied = grants[0]["result"]
        self.assertFalse(denied["granted"])
        self.assertEqual(denied["decision"], "denied_quorum")
        owed = denied["evaluation"]["owed"]
        self.assertTrue(
            any(o["side"] == "old" and o["type"] == "independent_sites" and o["missing"] == 1
                for o in owed)
        )
        self.assertTrue(
            any(o["side"] == "new" and o["type"] == "independent_sites" and o["missing"] == 1
                for o in owed)
        )
        self.assertTrue(
            any(o["side"] == "new" and o["type"] == "required_member" and o["member"] == "d"
                for o in owed)
        )

        approved = grants[1]["result"]
        self.assertTrue(approved["granted"])
        self.assertEqual(approved["decision"], "granted")
        self.assertEqual(approved["authorization"]["issued_at"], 8)

        report = inspects[0]["result"]
        self.assertTrue(report["found"])
        self.assertEqual(report["viewed_rev"], 1)
        basis = report["version_basis"]
        self.assertEqual(basis["bound_roster_revision"], 2)
        self.assertTrue(basis["transition"])
        members = {r["revision"]: r["members"] for r in basis["roster_configurations"]}
        self.assertEqual(members[1], ["a", "b", "c"])
        self.assertEqual(members[2], ["b", "c", "d"])

        live = report["live_authorization"]
        self.assertEqual(live["status"], "valid")
        self.assertTrue(live["withdrawable"])
        self.assertEqual(live["issued_at"], 8)
        self.assertTrue(report["event_chain"]["ok"])

        # 同一 grant 重试必须幂等返回同一授权
        again = service.request_grant({"id": "go", "rev": 1}, at=9)
        self.assertEqual(again["decision"], "returned_existing")
        self.assertEqual(again["authorization"]["auth_id"], live["auth_id"])

    def test_withdraw_after_real_start_trace(self):
        service, outputs = self._run("withdraw-after-real-start.jsonl")
        by_kind = {}
        for item in outputs:
            by_kind.setdefault(item["kind"], []).append(item)

        first_grant = by_kind["grant"][0]["result"]
        self.assertEqual(first_grant["decision"], "granted")
        self.assertEqual(first_grant["authorization"]["issued_at"], 3)
        self.assertTrue(first_grant["authorization"]["withdrawable"])

        started = by_kind["started"][0]["result"]
        self.assertTrue(started["accepted"])
        self.assertEqual(started["status"], "active")

        withdraw = by_kind["vote"][-1]["result"] if False else next(
            o["result"] for o in by_kind["vote"]
            if o["record"]["value"] == "withdraw"
        )
        self.assertFalse(withdraw["revocable"])
        self.assertEqual(withdraw["authorization_status"], "active")
        self.assertNotIn("authorization_revoked", withdraw)

        restart = by_kind["restart"][0]["result"]
        self.assertTrue(restart["restarted"])

        # restart 的返回视图里，真实执行边界已恢复：x active 且持有 line 互斥
        line_scope = next(s for s in restart["scopes"] if s["scope"] == "line")
        self.assertEqual(line_scope["frontier"]["mutex_holder"]["id"], "x")
        self.assertEqual(line_scope["frontier"]["mutex_holder"]["status"], "active")

        # 重启后 y at=9 仍应被 x 的未终态执行互斥阻挡
        grant_y_blocked = by_kind["grant"][1]["result"]
        self.assertEqual(grant_y_blocked["decision"], "blocked_mutex")
        self.assertEqual(grant_y_blocked["mutex_holder"]["id"], "x")

        settled = by_kind["settled"][0]["result"]
        self.assertEqual(settled["status"], "stopped")
        self.assertTrue(settled["mutex_released"])

        grant_y_ok = by_kind["grant"][2]["result"]
        self.assertEqual(grant_y_ok["decision"], "granted")
        self.assertEqual(grant_y_ok["authorization"]["issued_at"], 11)

        # 整个场景跑完后，x 的真实边界固定为 stopped，y 持有新的有效授权
        report_x = service.inspect({"id": "x"})
        self.assertIsNone(report_x["live_authorization"])
        self.assertEqual(report_x["current_authorization"]["terminal_state"], "stopped")
        report_y = service.inspect({"id": "y"})
        self.assertEqual(report_y["live_authorization"]["status"], "valid")
        self.assertTrue(report_y["event_chain"]["ok"])

    def test_reader_validation_still_applies_to_runner(self):
        # 空 supported 集合应导致场景消息无法通过读取器校验
        service = ConfirmationService(self.data / "strict")
        try:
            with self.assertRaises(ValueError):
                run_file(
                    EXAMPLES / "withdraw-after-real-start.jsonl",
                    service=service,
                    supported=set(),
                )
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
