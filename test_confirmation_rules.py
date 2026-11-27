"""覆盖成员调整、版本失效、迟到票、互斥、领取/撤销边界等核心规则。"""

import tempfile
import unittest
from pathlib import Path

from window_confirm import ConfirmationService


class RuleTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = ConfirmationService(Path(self._tmp.name) / "data")

    def tearDown(self):
        self.svc.close()
        self._tmp.cleanup()

    def roster(self, scope="line", rev=1, members=("a", "b"), minimum=2,
               required=(), prev=None, at=0, site_of=None):
        record = {
            "scope": scope, "revision": rev, "members": list(members),
            "minimum": minimum, "required": list(required),
        }
        if prev is not None:
            record["prev"] = prev
        if site_of is not None:
            record["site_of"] = site_of
        return self.svc.register_roster(record, at=at)

    def propose(self, wid="w", rev=1, roster=1, scope="line", text="open", at=1):
        return self.svc.propose(
            {"scope": scope, "id": wid, "rev": rev, "roster": roster, "text": text}, at=at
        )

    def yes(self, member, wid="w", rev=1, roster=1, at=2, value="yes"):
        return self.svc.vote(
            {"id": wid, "rev": rev, "roster": roster, "member": member, "value": value}, at=at
        )

    def grant(self, wid="w", rev=1, at=3):
        return self.svc.request_grant({"id": wid, "rev": rev}, at=at)


class QuorumRulesTests(RuleTestBase):
    def test_minimum_count_and_required_party(self):
        self.roster(members=["a", "b", "c"], minimum=2, required=["a"])
        self.propose()
        self.yes("b")
        result = self.grant(at=3)
        self.assertFalse(result["granted"])
        self.assertEqual(result["decision"], "denied_quorum")
        owed = {(o["type"], o.get("member")) for o in result["evaluation"]["owed"]}
        self.assertIn(("independent_sites", None), owed)
        self.assertIn(("required_member", "a"), owed)

        self.yes("a", at=4)
        self.assertTrue(self.grant(at=5)["granted"])

    def test_vote_is_bound_to_referenced_roster(self):
        # b 在两套名单中，但 r2 的票不能自动算进 r1；必须分别按引用计票
        self.roster(rev=1, members=["a", "b", "c"], minimum=2, required=["a"])
        self.roster(rev=2, prev=1, members=["b", "c", "d"], minimum=2, required=["d"], at=1)
        self.propose(roster=2, at=2)
        self.yes("a", roster=1, at=3)
        self.yes("b", roster=2, at=4)
        first = self.grant(at=5)
        self.assertEqual(first["decision"], "denied_quorum")
        sides = {o["side"]: o for o in first["evaluation"]["owed"]}
        self.assertIn("old", sides)
        self.assertIn("new", sides)

        self.yes("b", roster=1, at=6)
        still_missing_d = self.grant(at=7)
        self.assertEqual(still_missing_d["decision"], "denied_quorum")
        self.assertTrue(
            any(o["type"] == "required_member" and o["member"] == "d"
                for o in still_missing_d["evaluation"]["owed"])
        )

        self.yes("d", roster=2, at=8)
        ok = self.grant(at=9)
        self.assertEqual(ok["decision"], "granted")
        # 依据中冻结了两套配置与具体计票
        self.assertIsNotNone(ok["authorization"]["basis_digest"])
        report = self.svc.inspect({"id": "w"})
        roster_refs = {
            r["revision"]
            for r in report["version_basis"]["roster_configurations"]
        }
        self.assertEqual(roster_refs, {1, 2})
        req_refs = {r["roster_revision"] for r in report["quorum"]["requirements"]}
        self.assertEqual(req_refs, {1, 2})

    def test_outgoing_member_vote_for_new_roster_is_ineligible_history(self):
        self.roster(rev=1, members=["a", "b"], minimum=1)
        self.roster(rev=2, members=["b", "c"], minimum=1, at=1)
        self.propose(roster=2)
        result = self.yes("a", roster=2)
        self.assertFalse(result["eligibility"]["eligible"])
        self.assertEqual(result["eligibility"]["code"], "not_member_of_roster")
        report = self.svc.inspect({"id": "w"})
        vote_entry = report["vote_history"][0]
        self.assertFalse(vote_entry["eligible"])
        self.assertEqual(self.grant()["decision"], "denied_quorum")

    def test_independent_sites_collapse_officers_of_same_site(self):
        self.roster(
            members=["a1", "a2", "b"], minimum=2, required=[],
            site_of={"a1": "siteA", "a2": "siteA", "b": "siteB"},
        )
        self.propose()
        self.yes("a1")
        self.yes("a2", at=3)
        denied = self.grant(at=4)
        self.assertEqual(denied["decision"], "denied_quorum")
        self.assertEqual(denied["evaluation"]["requirements"][0]["have_sites"], 1)
        self.yes("b", at=5)
        self.assertEqual(self.grant(at=6)["decision"], "granted")


class ProposalVersionTests(RuleTestBase):
    def test_modified_proposal_discards_old_yes_and_supersedes_unstarted_auth(self):
        self.roster()
        self.propose(rev=1, text="open-v1")
        self.yes("a")
        self.yes("b", at=3)
        first = self.grant(at=4)
        self.assertEqual(first["decision"], "granted")
        auth1 = first["authorization"]["auth_id"]

        modified = self.propose(rev=2, text="open-v2-changed", at=5)
        self.assertTrue(modified["superseded_authorizations"])

        old_grant = self.grant(rev=1, at=6)
        self.assertEqual(old_grant["decision"], "version_superseded")

        new_grant = self.grant(rev=2, at=7)
        self.assertEqual(new_grant["decision"], "denied_quorum")
        self.assertEqual(new_grant["evaluation"]["requirements"][0]["have_sites"], 0)

        self.yes("a", rev=2, at=8)
        self.yes("b", rev=2, at=8)
        second = self.grant(rev=2, at=9)
        self.assertEqual(second["decision"], "granted")
        self.assertNotEqual(second["authorization"]["auth_id"], auth1)

        report = self.svc.inspect({"id": "w", "rev": 1})
        self.assertEqual(report["authorizations"][0]["status"], "superseded")

    def test_same_revision_different_content_conflicts(self):
        self.roster()
        self.propose(rev=1, text="aaa")
        conflict = self.propose(rev=1, text="bbb")
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"], "proposal_revision_conflict")


class RevocationAndLateVotesTests(RuleTestBase):
    def test_withdraw_before_start_revokes_and_late_yes_does_not_revive(self):
        self.roster()
        self.propose()
        self.yes("a")
        self.yes("b", at=3)
        issued = self.grant(at=4)
        auth1 = issued["authorization"]["auth_id"]

        withdraw = self.yes("a", value="withdraw", at=5)
        self.assertEqual(withdraw.get("authorization_revoked"), auth1)

        claim_after = self.svc.claim({"id": "w", "rev": 1}, at=6)
        self.assertFalse(claim_after["ok"])
        self.assertEqual(claim_after["outcome"], "revoked")

        # 前任/成员迟来的同意只恢复计数，绝不重新启用旧授权
        late_yes = self.yes("a", at=7)
        self.assertNotIn("authorization_revoked", late_yes)
        report = self.svc.inspect({"id": "w"})
        self.assertIsNone(report["live_authorization"])
        self.assertEqual(report["authorizations"][0]["status"], "revoked")

        again = self.grant(at=8)
        self.assertEqual(again["decision"], "reissued")
        self.assertNotEqual(again["authorization"]["auth_id"], auth1)
        report = self.svc.inspect({"id": "w"})
        statuses = [a["status"] for a in report["authorizations"]]
        self.assertEqual(statuses, ["revoked", "valid"])

    def test_surplus_yes_means_one_withdrawal_keeps_quorum(self):
        self.roster(members=["a", "b", "c"], minimum=2)
        self.propose()
        for i, m in enumerate(["a", "b", "c"]):
            self.yes(m, at=2 + i)
        issued = self.grant(at=5)
        self.assertTrue(issued["granted"])
        result = self.yes("c", value="withdraw", at=6)
        self.assertNotIn("authorization_revoked", result)
        report = self.svc.inspect({"id": "w"})
        self.assertEqual(report["live_authorization"]["status"], "valid")

    def test_objection_after_start_cannot_revoke(self):
        self.roster()
        self.propose()
        self.yes("a")
        self.yes("b")
        self.grant(at=3)
        started = self.svc.report_started({"id": "w", "rev": 1}, at=4)
        self.assertEqual(started["status"], "active")

        objection = self.yes("a", value="no", at=5)
        self.assertFalse(objection["revocable"])
        self.assertEqual(objection["authorization_status"], "active")

        # 迟到的 started 拒绝窗口（顺序上撤销/反对在先时才会拒绝；这里是重复 started，幂等）
        dup = self.svc.report_started({"id": "w", "rev": 1}, at=6)
        self.assertTrue(dup["duplicate"])

        # 未回报终态前，互斥仍被占用，别的窗口不能获权
        self.propose(wid="w2", at=7)
        self.yes("a", wid="w2", at=8)
        self.yes("b", wid="w2", at=8)
        blocked = self.grant(wid="w2", at=9)
        self.assertEqual(blocked["decision"], "blocked_mutex")

        settled = self.svc.report_settled({"id": "w", "rev": 1, "state": "stopped"}, at=10)
        self.assertTrue(settled["mutex_released"])
        self.assertEqual(self.grant(wid="w2", at=11)["decision"], "granted")

    def test_start_after_revocation_is_rejected(self):
        self.roster()
        self.propose()
        self.yes("a")
        self.yes("b")
        self.grant(at=3)
        self.yes("b", value="withdraw", at=4)
        started = self.svc.report_started({"id": "w", "rev": 1}, at=5)
        self.assertFalse(started["ok"])
        self.assertTrue(started["error"].startswith("start_rejected_revoked"))

    def test_terminal_is_fixed_and_conflicting_report_kept_first(self):
        self.roster()
        self.propose()
        self.yes("a")
        self.yes("b")
        self.grant(at=3)
        self.svc.report_started({"id": "w", "rev": 1}, at=4)
        done = self.svc.report_settled({"id": "w", "rev": 1, "state": "done"}, at=5)
        self.assertEqual(done["status"], "done")

        conflict = self.svc.report_settled(
            {"id": "w", "rev": 1, "state": "stopped"}, at=6
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"], "terminal_conflict")

        again = self.grant(at=7)
        self.assertEqual(again["decision"], "already_terminal")
        report = self.svc.inspect({"id": "w"})
        self.assertIsNone(report["live_authorization"])
        self.assertEqual(report["current_authorization"]["terminal_state"], "done")


class MutexScopeTests(RuleTestBase):
    def test_other_resource_scope_continues(self):
        self.roster(scope="line")
        self.roster(scope="lab", at=0)
        self.propose(wid="line-win", scope="line")
        self.propose(wid="lab-win", scope="lab", at=1)
        for wid in ("line-win", "lab-win"):
            self.yes("a", wid=wid)
            self.yes("b", wid=wid)
        self.assertTrue(self.grant(wid="line-win", at=3)["granted"])
        self.assertTrue(self.grant(wid="lab-win", at=3)["granted"])
        self.svc.report_started({"id": "line-win", "rev": 1}, at=4)

        second_line = self.propose(wid="line-2", scope="line", at=5)
        self.assertTrue(second_line["ok"])
        self.yes("a", wid="line-2", at=6)
        self.yes("b", wid="line-2", at=6)
        self.assertEqual(self.grant(wid="line-2", at=7)["decision"], "blocked_mutex")

        # 另一个资源范围不受影响：lab-win 未开始，授权仍在；新 lab 窗口与它争用
        self.svc.report_started({"id": "lab-win", "rev": 1}, at=4)
        self.propose(wid="lab-2", scope="lab", at=5)
        self.yes("a", wid="lab-2", at=6)
        self.yes("b", wid="lab-2", at=6)
        self.assertEqual(self.grant(wid="lab-2", at=7)["decision"], "blocked_mutex")

        # line 终态后 line-2 立刻可授权；lab 仍独立阻塞
        self.svc.report_settled({"id": "line-win", "rev": 1, "state": "done"}, at=8)
        self.assertEqual(self.grant(wid="line-2", at=9)["decision"], "granted")
        lab_report = self.svc.inspect({"scope": "lab"})
        self.assertTrue(lab_report["mutex_frontier"]["mutex_held"])


class ClaimTests(RuleTestBase):
    def test_claim_is_idempotent_and_carries_traceability(self):
        self.roster()
        self.propose()
        self.yes("a")
        self.yes("b")
        issued = self.grant(at=3)
        token = issued["command"]["token"]

        first = self.svc.claim({"auth_id": issued["authorization"]["auth_id"],
                               "token": token, "executor_id": "sim"}, at=4)
        self.assertEqual(first["outcome"], "delivered")
        second = self.svc.claim({"id": "w", "rev": 1}, at=5)
        self.assertEqual(second["outcome"], "redelivered")
        self.assertEqual(second["command"]["auth_id"], issued["authorization"]["auth_id"])

        bad = self.svc.claim({"auth_id": issued["authorization"]["auth_id"],
                              "token": "wrong"}, at=6)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_token")

    def test_ineligible_objection_does_not_revoke(self):
        # a 已不在 r2 名单：它对 r2 投 no 无资格，不能撤销按 r2 建立的授权
        self.roster(rev=1, members=["a", "b"], minimum=2)
        self.roster(rev=2, members=["b", "c"], minimum=2, at=1)
        self.propose(roster=2, at=2)
        self.yes("b", roster=2, at=3)
        self.yes("c", roster=2, at=3)
        issued = self.grant(at=4)
        self.assertEqual(issued["decision"], "granted")

        objection = self.yes("a", roster=2, value="no", at=5)
        self.assertFalse(objection["eligibility"]["eligible"])
        self.assertNotIn("authorization_revoked", objection)
        report = self.svc.inspect({"id": "w"})
        self.assertEqual(report["live_authorization"]["status"], "valid")

    def test_withdraw_only_affects_referenced_roster_slot(self):
        self.roster(rev=1, members=["a", "b", "c"], minimum=2, required=["a"])
        self.roster(rev=2, prev=1, members=["b", "c", "d"], minimum=2, required=["d"], at=1)
        self.propose(roster=2, at=2)
        self.yes("a", roster=1, at=3)
        self.yes("b", roster=2, at=4)
        self.yes("b", roster=1, at=5)
        self.yes("d", roster=2, at=6)
        self.assertEqual(self.grant(at=7)["decision"], "granted")
        # b 撤回 r2 一边：新组条件破裂，未开始授权被撤销；旧组 r1 的 b 票仍在历史
        revoked = self.yes("b", roster=2, value="withdraw", at=8)
        self.assertIn("authorization_revoked", revoked)
        report = self.svc.inspect({"id": "w"})
        slots = {(v["roster"], v["member"]): v["value"] for v in report["vote_history"]}
        self.assertEqual(slots[(1, "b")], "yes")
        self.assertEqual(slots[(2, "b")], "withdraw")

    def test_orphan_vote_is_kept_and_later_applied(self):
        early = self.yes("a", at=0)
        self.assertTrue(early["ok"])
        self.assertEqual(early["eligibility"]["code"], "window_unknown_yet")
        self.roster(at=1)
        self.propose(at=2)
        self.yes("b", at=3)
        self.assertTrue(self.grant(at=4)["granted"])


if __name__ == "__main__":
    unittest.main()
