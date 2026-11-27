"""核心领域引擎：纯状态折叠 + 决策。

引擎不做 IO、不碰线程锁；:class:`ConfirmationService` 负责持久化与串行化。
所有规则都集中在本文件，便于单测与追查。

关键不变量
----------
1. 一票绑定到它引用的 roster 修订，且成员必须属于该 roster；旧 roster 的赞成
   绝不会被混入新 roster 的计数（修复“确认被混进新名单”的问题）。
2. roster 带 ``prev`` 时，提案绑定该 roster 即进入过渡条件：新、旧两套条件
   必须同时满足。
3. 提案按 ``(id, rev)`` 不可变；新 rev 不继承旧 rev 的赞成。
4. 每个 scope（资源/互斥范围）最多有一个持有互斥槽位的授权
   （valid/claimed/active）。
5. 已撤销的授权对象永远不会回到 valid；再次满足条件只会产生新的授权。
"""

from __future__ import annotations

from typing import Any

from .store import canonical_json, sha256_digest

VALUE_VALUES = ("yes", "no", "withdraw")

# 持有互斥槽位、可被执行端领取/开始/执行中的状态
AUTHORIZATION_LIVE = ("valid", "claimed", "active")
# 不可再就同一 (id, rev) 重新签发的终态
AUTHORIZATION_CLOSED = ("done", "stopped", "superseded")

SIDE_LABELS_ZH = {"bound": "本届配置", "current": "新组", "previous": "旧组", "only": "唯一配置"}


class CommandError(Exception):
    """输入不合法或与当前版本冲突。"""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _as_int(value: Any, field: str, *, required: bool = True) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        # 容忍数字字符串，但不接受布尔值
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise CommandError("invalid_field", f"字段 {field} 必须是整数，实际为 {value!r}")
    return value


def _require_str(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise CommandError("invalid_field", f"字段 {field} 必须是非空字符串")
    return value


class Engine:
    def __init__(self) -> None:
        self.seq = 0
        self.auth_counter = 0
        # scope -> {revision(int): roster(dict)}
        self.rosters: dict[str, dict[int, dict[str, Any]]] = {}
        # id -> window dict
        self.windows: dict[str, dict[str, Any]] = {}
        # 提案尚不存在时先到达的投票
        self.orphan_votes: list[dict[str, Any]] = []
        self.anomalies: list[dict[str, Any]] = []
        self.boots: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ replay

    @classmethod
    def replay(cls, events: list[dict[str, Any]]) -> "Engine":
        engine = cls()
        for event in events:
            engine.apply(event)
        return engine

    def apply(self, event: dict[str, Any]) -> None:
        self.seq = int(event["seq"])
        event_type = event["type"]
        payload = event.get("payload") or {}
        at = event.get("at")

        if event_type == "boot":
            self.boots.append({**payload, "at": at, "seq": event["seq"]})
        elif event_type == "anomaly":
            self.anomalies.append({**payload, "at": at, "seq": event["seq"]})
        elif event_type == "roster":
            scope = payload["scope"]
            revision = int(payload["revision"])
            self.rosters.setdefault(scope, {})[revision] = payload
        elif event_type == "proposal":
            self._apply_proposal(event, payload, at)
        elif event_type == "vote":
            self._apply_vote(event, payload)
        elif event_type == "grant":
            self._apply_grant(event, payload)
        elif event_type in ("claim", "started", "settled"):
            self._apply_report(event_type, event, payload)
        else:  # pragma: no cover - 前向兼容：未知事件保留为异常记录
            self.anomalies.append({"unknown_event_type": event_type, "event": event})

    def _apply_proposal(self, event: dict[str, Any], payload: dict[str, Any], at: Any) -> None:
        wid = payload["id"]
        window = self.windows.get(wid)
        if window is None:
            window = {
                "id": wid,
                "scope": payload["scope"],
                "created_at": at,
                "created_seq": event["seq"],
                "revisions": {},
                "current_rev": int(payload["rev"]),
                "votes": [],
                "grant_log": [],
                "report_log": [],
                "issuances": {},
            }
            self.windows[wid] = window
            # 收容早于提案到达的投票
            leftovers = []
            for vote in self.orphan_votes:
                if vote.get("id") == wid:
                    window["votes"].append(vote)
                else:
                    leftovers.append(vote)
            self.orphan_votes = leftovers
        rev = int(payload["rev"])
        window["revisions"][str(rev)] = {
            "rev": rev,
            "roster": int(payload["roster"]),
            "text": payload.get("text"),
            "content": payload.get("content"),
            "raw_hash_fields": payload.get("raw_hash_fields"),
            "content_sha256": payload["content_sha256"],
            "at": at,
            "seq": event["seq"],
        }
        window["current_rev"] = max(int(window["current_rev"]), rev)
        for ref in payload.get("supersedes", []):
            issuance = self._find_issuance(wid, int(ref["rev"]), ref["auth_id"])
            if issuance is not None and issuance["status"] in ("valid", "claimed"):
                issuance["status"] = "superseded"
                issuance["closed_at"] = at
                issuance["closed_seq"] = event["seq"]
                issuance["close_reason"] = "proposal_revision_superseded"
                issuance["history"].append(
                    {"status": "superseded", "at": at, "seq": event["seq"],
                     "reason": "proposal_revision_superseded", "by_rev": rev}
                )

    def _apply_vote(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        wid = payload["id"]
        window = self.windows.get(wid)
        if window is None:
            self.orphan_votes.append(payload)
            return
        # 以事件流中的权威 seq/at 为准，保证重启回放与首次运行一致
        payload["seq"] = event["seq"]
        payload["at"] = event.get("at")
        window["votes"].append(payload)
        revocation = payload.get("revocation")
        if revocation:
            issuance = self._find_issuance(wid, int(payload["rev"]), revocation["auth_id"])
            if issuance is not None and issuance["status"] in ("valid", "claimed"):
                issuance["status"] = "revoked"
                issuance["revoked_at"] = event.get("at")
                issuance["revoke_seq"] = event["seq"]
                issuance["revoke_reason"] = revocation.get("reason", "quorum_lost")
                issuance["revoke_detail"] = revocation.get("failed_requirements")
                issuance["history"].append(
                    {"status": "revoked", "at": event.get("at"),
                     "seq": event["seq"], "reason": issuance["revoke_reason"]}
                )
        objection = payload.get("post_start_objection")
        if objection:
            issuance = self._find_issuance(wid, int(payload["rev"]), objection["auth_id"])
            if issuance is not None:
                record = {**objection, "seq": event["seq"], "at": event.get("at")}
                issuance.setdefault("objections", []).append(record)

    def _apply_grant(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        wid = payload["id"]
        window = self.windows.get(wid)
        if window is None:
            return
        window["grant_log"].append(payload)
        issuance = payload.get("issuance")
        if issuance:
            issuance["issued_seq"] = event["seq"]
            if issuance.get("history"):
                issuance["history"][0]["seq"] = event["seq"]
            rev = str(issuance["rev"])
            window["issuances"].setdefault(rev, []).append(issuance)
            self.auth_counter = max(self.auth_counter, int(issuance["auth_seq"]))

    def _apply_report(
        self, event_type: str, event: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        seq = event["seq"]
        at = event.get("at")
        wid = payload.get("id")
        window = self.windows.get(wid) if isinstance(wid, str) else None
        if window is None:
            return
        payload = {**payload, "seq": seq, "at": at}
        window["report_log"].append({"type": event_type, **payload})
        auth_id = payload.get("auth_id")
        rev = payload.get("rev")
        issuance = self._find_issuance(wid, int(rev), auth_id) if rev is not None else None
        if issuance is None:
            return
        outcome = payload.get("outcome")
        if event_type == "claim" and outcome == "delivered":
            issuance["status"] = "claimed"
            issuance["claimed_at"] = at
            issuance["claim_seq"] = seq
            issuance["claim_count"] = issuance.get("claim_count", 0) + 1
            issuance["executor_id"] = payload.get("executor_id") or issuance.get("executor_id")
            issuance.setdefault("claims", []).append(
                {"at": at, "seq": seq, "executor_id": payload.get("executor_id"), "outcome": outcome}
            )
            issuance["history"].append({"status": "claimed", "at": at, "seq": seq})
        elif event_type == "claim" and outcome == "redelivered":
            issuance["claim_count"] = issuance.get("claim_count", 0) + 1
            issuance.setdefault("claims", []).append(
                {"at": at, "seq": seq, "executor_id": payload.get("executor_id"), "outcome": outcome}
            )
        elif event_type == "started" and outcome == "accepted":
            issuance["status"] = "active"
            issuance["started_at"] = at
            issuance["started_seq"] = seq
            issuance["executor_id"] = payload.get("executor_id") or issuance.get("executor_id")
            issuance["history"].append({"status": "active", "at": at, "seq": seq})
        elif event_type == "settled" and outcome == "accepted":
            new_state = payload["state"]  # done / stopped
            issuance["status"] = new_state
            issuance["terminal_state"] = new_state
            issuance["settled_at"] = at
            issuance["settled_seq"] = seq
            issuance["settle_detail"] = payload.get("detail")
            issuance["history"].append(
                {"status": new_state, "at": at, "seq": seq, "detail": payload.get("detail")}
            )

    def _find_issuance(self, wid: str, rev: int, auth_id: str | None = None) -> dict[str, Any] | None:
        window = self.windows.get(wid)
        if window is None:
            return None
        items = window["issuances"].get(str(rev), [])
        if auth_id is not None:
            for issuance in items:
                if issuance["auth_id"] == auth_id:
                    return issuance
            return None
        return items[-1] if items else None

    # ------------------------------------------------------------- plan dispatch

    def plan_message(
        self, kind: str, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        if not isinstance(record, dict):
            raise CommandError("invalid_record", "record 必须是 JSON 对象")
        if kind == "roster":
            return self._plan_roster(at, record)
        if kind == "proposal":
            return self._plan_proposal(at, record)
        if kind == "vote":
            return self._plan_vote(at, record)
        if kind == "grant":
            return self._plan_grant(at, record)
        if kind in ("claim", "fetch", "collect"):
            return self._plan_claim(at, record)
        if kind == "started":
            return self._plan_started(at, record)
        if kind == "settled":
            return self._plan_settled(at, record)
        raise CommandError("unknown_kind", f"未登记的消息名称：{kind!r}", http_status=404)

    # -------------------------------------------------------------------- roster

    def _plan_roster(
        self, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        scope = _require_str(record, "scope")
        revision = _as_int(record.get("revision"), "revision")
        assert revision is not None
        members_raw = record.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise CommandError("invalid_field", "members 必须是非空数组")
        if not all(isinstance(m, str) and m for m in members_raw):
            raise CommandError("invalid_field", "members 必须全部是非空字符串")
        if len(set(members_raw)) != len(members_raw):
            raise CommandError("invalid_field", "members 中存在重复标识")
        members = list(members_raw)
        minimum = _as_int(record.get("minimum"), "minimum")
        assert minimum is not None
        if minimum < 0:
            raise CommandError("invalid_field", "minimum 不能为负数")
        required_raw = record.get("required", [])
        if not isinstance(required_raw, list) or not all(isinstance(m, str) for m in required_raw):
            raise CommandError("invalid_field", "required 必须是字符串数组")
        missing = [m for m in required_raw if m not in members]
        if missing:
            raise CommandError(
                "required_not_in_members",
                f"required 中的责任方不在 members 内：{missing}",
                http_status=422,
            )
        prev = record.get("prev")
        prev_rev = _as_int(prev, "prev", required=False)
        scoped = self.rosters.get(scope, {})
        if prev_rev is not None and prev_rev not in scoped:
            raise CommandError(
                "unknown_prev_roster",
                f"scope={scope} 的 prev={prev_rev} 配置尚未公布",
                http_status=409,
            )
        site_of_raw = record.get("site_of")
        site_of: dict[str, str] = {}
        if site_of_raw is not None:
            if not isinstance(site_of_raw, dict):
                raise CommandError("invalid_field", "site_of 必须是 member -> site 的对象")
            for member, site in site_of_raw.items():
                if member not in members or not isinstance(site, str) or not site:
                    raise CommandError("invalid_field", f"site_of 映射非法：{member}->{site!r}")
                site_of[member] = site

        payload = {
            "scope": scope,
            "revision": revision,
            "members": members,
            "minimum": minimum,
            "required": list(required_raw),
            "prev": prev_rev,
            "site_of": site_of,
        }
        existing = scoped.get(revision)
        if existing is not None:
            if canonical_json(existing) == canonical_json(payload):
                return (
                    {"ok": True, "duplicate": True, "kind": "roster",
                     "scope": scope, "revision": revision},
                    [],
                )
            raise CommandError(
                "roster_revision_conflict",
                f"scope={scope} roster revision={revision} 已以不同内容公布，版本不可变",
                http_status=409,
            )
        return {"ok": True, "kind": "roster", "scope": scope, "revision": revision,
                "transition": prev_rev is not None}, [("roster", payload)]

    # ------------------------------------------------------------------ proposal

    def _plan_proposal(
        self, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        scope = _require_str(record, "scope")
        wid = _require_str(record, "id")
        rev = _as_int(record.get("rev"), "rev")
        roster_rev = _as_int(record.get("roster"), "roster")
        assert rev is not None and roster_rev is not None
        if record.get("text") is None and record.get("content") is None:
            raise CommandError("invalid_field", "提案必须给出 text 或 content（明确内容）")
        scoped_rosters = self.rosters.get(scope)
        if not scoped_rosters or roster_rev not in scoped_rosters:
            raise CommandError(
                "unknown_roster",
                f"提案引用的 scope={scope} roster={roster_rev} 尚未公布",
                http_status=409,
            )
        window = self.windows.get(wid)
        if window is not None and window["scope"] != scope:
            raise CommandError(
                "id_scope_conflict",
                f"窗口 id={wid} 已属于 scope={window['scope']}，不能在 {scope} 下重用",
                http_status=409,
            )
        content_sha = sha256_digest(record)
        if window is not None and str(rev) in window["revisions"]:
            old = window["revisions"][str(rev)]
            if old["content_sha256"] == content_sha:
                return {"ok": True, "duplicate": True, "kind": "proposal",
                        "id": wid, "rev": rev}, []
            raise CommandError(
                "proposal_revision_conflict",
                f"窗口 {wid} rev={rev} 已存在且内容不同；同一版本不可修改，请使用新 rev",
                http_status=409,
            )
        if window is not None and rev <= int(window["current_rev"]):
            raise CommandError(
                "revision_not_monotonic",
                f"窗口 {wid} 已到 rev={window['current_rev']}，新提案版本必须更大",
                http_status=409,
            )

        supersedes: list[dict[str, Any]] = []
        if window is not None:
            for old_rev, items in window["issuances"].items():
                latest = items[-1] if items else None
                if latest is not None and latest["status"] in ("valid", "claimed"):
                    supersedes.append(
                        {"rev": int(old_rev), "auth_id": latest["auth_id"]}
                    )

        payload = {
            "scope": scope,
            "id": wid,
            "rev": rev,
            "roster": roster_rev,
            "text": record.get("text"),
            "content": record.get("content"),
            "raw_hash_fields": sorted(
                k for k in record.keys() if k not in ("scope", "id", "rev", "roster")
            ),
            "content_sha256": content_sha,
            "supersedes": supersedes,
        }
        result: dict[str, Any] = {
            "ok": True, "kind": "proposal", "id": wid, "rev": rev,
            "roster": roster_rev, "superseded_authorizations": supersedes,
        }
        if supersedes:
            result["notice"] = "旧版本尚未开始的授权已被修改后的提案废止"
        return result, [("proposal", payload)]

    # ---------------------------------------------------------------------- vote

    def _plan_vote(
        self, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        wid = _require_str(record, "id")
        rev = _as_int(record.get("rev"), "rev")
        roster_ref = _as_int(record.get("roster"), "roster")
        member = _require_str(record, "member")
        value = _require_str(record, "value")
        if value not in VALUE_VALUES:
            raise CommandError(
                "invalid_vote_value",
                f"value 必须是 {VALUE_VALUES} 之一，实际 {value!r}",
            )
        assert rev is not None and roster_ref is not None

        vote: dict[str, Any] = {
            "id": wid,
            "rev": rev,
            "roster": roster_ref,
            "member": member,
            "value": value,
            "note": record.get("note"),
            "at": at,
            "seq": self.seq + 1,
        }
        window = self.windows.get(wid)
        eligibility = self._vote_eligibility(window, roster_ref, member)
        result: dict[str, Any] = {
            "ok": True,
            "kind": "vote",
            "id": wid,
            "rev": rev,
            "roster": roster_ref,
            "member": member,
            "value": value,
            "eligibility": eligibility,
        }
        if not eligibility["eligible"]:
            result["notice"] = (
                "该投票只作为历史记录保留，不计入任何法定条件："
                + eligibility["reason_zh"]
            )

        revocation: dict[str, Any] | None = None
        post_start: dict[str, Any] | None = None
        if window is not None and str(rev) in window["revisions"]:
            latest = self._find_issuance(wid, rev)
            if latest is not None and latest["status"] in ("valid", "claimed"):
                evaluation = self.evaluate_rev(window, rev, extra_vote=vote)
                if not evaluation["quorum_met"]:
                    revocation = {
                        "auth_id": latest["auth_id"],
                        "reason": "quorum_lost_after_" + value,
                        "failed_requirements": evaluation["owed"],
                        "at": at,
                    }
                    vote["revocation"] = revocation
                    result["authorization_revoked"] = latest["auth_id"]
                    result["revocable"] = True
                    result["notice"] = "授权尚未被模拟端开始，新的反对/撤回使条件不再满足，已停止该授权"
                else:
                    result["authorization_status"] = latest["status"]
                    result["notice"] = "投票后条件仍满足，授权继续有效"
            elif latest is not None and latest["status"] == "active" and value in ("no", "withdraw"):
                post_start = {
                    "auth_id": latest["auth_id"],
                    "at": at,
                    "member": member,
                    "value": value,
                }
                vote["post_start_objection"] = post_start
                result["revocable"] = False
                result["authorization_status"] = "active"
                result["notice"] = (
                    "模拟端已开始执行，不能直接撤销或向互斥窗口下发新指令；"
                    "该异议已记录，必须等待 done/stopped 终态回报"
                )
        return result, [("vote", vote)]

    def _vote_eligibility(
        self, window: dict[str, Any] | None, roster_ref: int, member: str
    ) -> dict[str, Any]:
        if window is None:
            return {
                "eligible": False,
                "code": "window_unknown_yet",
                "reason_zh": "窗口提案尚未到达，投票先作为历史留存，待提案到达后按其引用配置判定",
            }
        roster = self.rosters.get(window["scope"], {}).get(roster_ref)
        if roster is None:
            return {
                "eligible": False,
                "code": "roster_unknown",
                "reason_zh": f"成员配置 revision={roster_ref} 尚未公布",
            }
        if member not in roster["members"]:
            return {
                "eligible": False,
                "code": "not_member_of_roster",
                "reason_zh": f"{member} 不属于 roster={roster_ref} 的在任成员，其确认只能用于仍在任的那一边",
            }
        return {"eligible": True, "code": "member_in_roster",
                "reason_zh": f"{member} 是 roster={roster_ref} 的在任成员"}

    # ------------------------------------------------------------- quorum logic

    def _slots_and_ledger(
        self,
        window: dict[str, Any],
        rev: int,
        extra_vote: dict[str, Any] | None = None,
    ) -> tuple[dict[tuple[int, str], dict[str, Any]], list[dict[str, Any]]]:
        scope = window["scope"]
        rosters = self.rosters.get(scope, {})
        slots: dict[tuple[int, str], dict[str, Any]] = {}
        ledger: list[dict[str, Any]] = []
        votes = list(window.get("votes", []))
        if extra_vote is not None:
            votes.append(extra_vote)
        for vote in votes:
            if _as_int(vote.get("rev"), "rev") != rev:
                continue
            roster_ref = _as_int(vote.get("roster"), "roster")
            member = vote.get("member")
            roster = rosters.get(roster_ref) if roster_ref is not None else None
            eligible = bool(
                isinstance(roster, dict)
                and isinstance(member, str)
                and member in roster.get("members", [])
            )
            entry = dict(vote)
            entry["eligible"] = eligible
            if not eligible:
                entry["ineligible_reason"] = (
                    "roster_unknown" if roster is None else "not_member_of_referenced_roster"
                )
            ledger.append(entry)
            if eligible and vote.get("value") in VALUE_VALUES:
                # 按 seq/到达顺序遍历，同一 (roster, member) 的最新决定覆盖旧决定
                slots[(roster_ref, member)] = entry  # type: ignore[index]
        return slots, ledger

    def evaluate_rev(
        self,
        window: dict[str, Any],
        rev: int,
        extra_vote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """计算某个提案版本当前是否满足它绑定的一套/两套公布条件。"""

        rev_entry = window["revisions"][str(rev)]
        bound_rev = int(rev_entry["roster"])
        rosters = self.rosters[window["scope"]]
        bound = rosters[bound_rev]

        pair: list[tuple[int, str, str]] = [(bound_rev, "bound", "current")]
        prev_rev = bound.get("prev")
        if prev_rev is not None:
            pair.append((int(prev_rev), "previous", "previous"))

        slots, ledger = self._slots_and_ledger(window, rev, extra_vote)
        requirements: list[dict[str, Any]] = []
        owed: list[dict[str, Any]] = []

        for order, (roster_rev, relation, side) in enumerate(pair):
            roster = rosters[roster_rev]
            site_of = roster.get("site_of") or {}
            yes_entries = [
                slots[(roster_rev, m)]
                for m in roster["members"]
                if (roster_rev, m) in slots and slots[(roster_rev, m)].get("value") == "yes"
            ]
            yes_members = sorted(e["member"] for e in yes_entries)
            yes_sites = sorted({site_of.get(m, m) for m in yes_members})
            missing_required = [
                m
                for m in roster["required"]
                if not (
                    (roster_rev, m) in slots
                    and slots[(roster_rev, m)].get("value") == "yes"
                )
            ]
            have = len(yes_sites)
            minimum = int(roster["minimum"])
            short_sites = max(0, minimum - have)
            satisfied = short_sites == 0 and not missing_required
            side_key = "new" if len(pair) == 2 and order == 0 else (
                "old" if len(pair) == 2 else "only"
            )
            side_zh = {"new": "新组", "old": "旧组", "only": "唯一配置"}[side_key]

            counted_votes = [
                {
                    "member": e["member"],
                    "site": site_of.get(e["member"], e["member"]),
                    "roster": roster_rev,
                    "at": e.get("at"),
                    "seq": e.get("seq"),
                }
                for e in yes_entries
            ]
            requirement = {
                "roster_revision": roster_rev,
                "relation": relation,
                "side": side_key,
                "side_zh": side_zh,
                "members": list(roster["members"]),
                "minimum": minimum,
                "required": list(roster["required"]),
                "prev": roster.get("prev"),
                "yes_members": yes_members,
                "yes_sites": yes_sites,
                "have_sites": have,
                "short_sites": short_sites,
                "missing_required": missing_required,
                "satisfied": satisfied,
                "counted_votes": counted_votes,
            }
            requirements.append(requirement)

            if short_sites > 0:
                owed.append(
                    {
                        "type": "independent_sites",
                        "target": f"roster:{roster_rev}",
                        "side": side_key,
                        "side_zh": side_zh,
                        "roster_revision": roster_rev,
                        "missing": short_sites,
                        "have": have,
                        "minimum": minimum,
                        "fact_zh": f"尚欠{side_zh}（roster r{roster_rev}）{short_sites} 个独立站点确认"
                                  f"（现有 {have}/{minimum}）",
                    }
                )
            for member in missing_required:
                owed.append(
                    {
                        "type": "required_member",
                        "target": f"roster:{roster_rev}:member:{member}",
                        "side": side_key,
                        "side_zh": side_zh,
                        "roster_revision": roster_rev,
                        "member": member,
                        "fact_zh": f"{side_zh}（roster r{roster_rev}）尚缺指定责任方 {member} 的同意",
                    }
                )

        return {
            "quorum_met": all(r["satisfied"] for r in requirements),
            "transition": len(requirements) == 2,
            "requirements": requirements,
            "owed": owed,
            "eligible_vote_count": sum(1 for e in ledger if e.get("eligible")),
            "ledger_size": len(ledger),
        }

    # ------------------------------------------------------------------- mutex

    def live_holder(
        self, scope: str, exclude: tuple[str, int] | None = None
    ) -> dict[str, Any] | None:
        for wid, window in self.windows.items():
            if window["scope"] != scope:
                continue
            for rev_str, items in window["issuances"].items():
                for issuance in items:
                    if issuance["status"] not in AUTHORIZATION_LIVE:
                        continue
                    if exclude is not None and wid == exclude[0] and int(rev_str) == exclude[1]:
                        continue
                    return {
                        "id": wid,
                        "rev": int(rev_str),
                        "auth_id": issuance["auth_id"],
                        "status": issuance["status"],
                        "issued_at": issuance.get("issued_at"),
                        "started_at": issuance.get("started_at"),
                    }
        return None

    def scope_frontier(self, scope: str) -> dict[str, Any]:
        holder = self.live_holder(scope)
        last_started: dict[str, Any] | None = None
        last_settled: dict[str, Any] | None = None
        for wid, window in self.windows.items():
            if window["scope"] != scope:
                continue
            for rev_str, items in window["issuances"].items():
                for issuance in items:
                    if issuance.get("started_at") is not None:
                        candidate = {
                            "id": wid, "rev": int(rev_str), "auth_id": issuance["auth_id"],
                            "at": issuance["started_at"], "seq": issuance.get("started_seq"),
                        }
                        if last_started is None or self._order_key(candidate) > self._order_key(last_started):
                            last_started = candidate
                    if issuance.get("settled_at") is not None:
                        candidate = {
                            "id": wid, "rev": int(rev_str), "auth_id": issuance["auth_id"],
                            "state": issuance.get("terminal_state"),
                            "at": issuance["settled_at"], "seq": issuance.get("settled_seq"),
                        }
                        if last_settled is None or self._order_key(candidate) > self._order_key(last_settled):
                            last_settled = candidate
        return {
            "scope": scope,
            "mutex_holder": holder,
            "mutex_held": holder is not None,
            "last_started": last_started,
            "last_settled": last_settled,
        }

    @staticmethod
    def _order_key(candidate: dict[str, Any]) -> tuple[int, int]:
        return (int(candidate.get("seq") or 0), int(candidate.get("at") or 0))

    # ------------------------------------------------------------------- grants

    def _plan_grant(
        self, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        wid = _require_str(record, "id")
        rev = _as_int(record.get("rev"), "rev")
        assert rev is not None
        window = self.windows.get(wid)
        if window is None or str(rev) not in window["revisions"]:
            result = {
                "ok": False, "granted": False, "decision": "not_found",
                "error": "unknown_window_or_rev",
                "message": f"窗口 {wid} rev={rev} 不存在，不能授权",
            }
            return result, [("anomaly", {"kind": "grant", "record": record, "error": "not_found"})]

        items = window["issuances"].get(str(rev), [])
        latest = items[-1] if items else None

        if latest is not None and latest["status"] in AUTHORIZATION_LIVE:
            decision = "returned_existing" if latest["status"] != "active" else "execution_in_progress"
            result = {
                "ok": True, "granted": True, "decision": decision,
                "id": wid, "rev": rev, "authorization": self.authorization_view(latest),
                "command": latest["command"],
                "notice": "已存在有效的授权，重复 grant 返回同一授权，不会重复签发或下发",
            }
            payload = {"id": wid, "rev": rev, "decision": decision,
                       "auth_id": latest["auth_id"], "at": at}
            return result, [("grant", payload)]

        # 旧提案版本已被新版本取代，不能再授权旧 rev
        if rev < int(window["current_rev"]) or (
            latest is not None and latest["status"] == "superseded"
        ):
            result = {
                "ok": False, "granted": False, "decision": "version_superseded",
                "id": wid, "rev": rev, "current_rev": int(window["current_rev"]),
                "message": "该提案版本已被修改后的新版本取代，旧赞成不能继续使用；请按当前版本重新确认",
            }
            return result, [("grant", {"id": wid, "rev": rev,
                                       "decision": "version_superseded", "at": at})]

        if latest is not None and latest["status"] in ("done", "stopped"):
            result = {
                "ok": False, "granted": False, "decision": "already_terminal",
                "id": wid, "rev": rev, "terminal_state": latest["status"],
                "settled_at": latest.get("settled_at"),
                "message": "该 (id, rev) 已回报终态，执行边界已固定；如需再次演练请提出新的提案版本",
            }
            return result, [("grant", {"id": wid, "rev": rev,
                                       "decision": "already_terminal",
                                       "auth_id": latest["auth_id"], "at": at})]

        evaluation = self.evaluate_rev(window, rev)
        frontier = self.scope_frontier(window["scope"])
        holder = frontier["mutex_holder"]

        if not evaluation["quorum_met"]:
            result = {
                "ok": True, "granted": False, "decision": "denied_quorum",
                "id": wid, "rev": rev, "scope": window["scope"],
                "evaluation": evaluation,
                "mutex": frontier,
                "message": "确认集合未满足本次公布条件，不能签发可执行授权",
            }
            return result, [("grant", {"id": wid, "rev": rev,
                                       "decision": "denied_quorum",
                                       "owed": evaluation["owed"], "at": at})]

        if holder is not None:
            result = {
                "ok": True, "granted": False, "decision": "blocked_mutex",
                "id": wid, "rev": rev, "scope": window["scope"],
                "evaluation": evaluation,
                "mutex_holder": holder,
                "message": (
                    f"互斥范围 {window['scope']} 正被窗口 {holder['id']} "
                    f"rev={holder['rev']}（{holder['status']}）占用，"
                    "在其回报 done/stopped 前不能下发新的可执行指令"
                ),
            }
            return result, [("grant", {"id": wid, "rev": rev,
                                       "decision": "blocked_mutex",
                                       "holder": holder, "at": at})]

        # 条件满足且互斥槽空闲：签发新授权（序号由 apply 落事件时推进）
        auth_seq = self.auth_counter + 1
        auth_id = f"AUTH-{auth_seq:05d}"
        issuance_no = len(items) + 1
        rentry = window["revisions"][str(rev)]
        basis = self._build_basis(window, rentry, evaluation)
        token = sha256_digest({"auth_id": auth_id, "basis": basis["digest"]})[:24]
        command = {
            "envelope": "simulation-window-command/v1",
            "command_type": "open_simulation_window",
            "auth_id": auth_id,
            "token": token,
            "scope": window["scope"],
            "window_id": wid,
            "revision": rev,
            "text": rentry.get("text"),
            "content": rentry.get("content"),
            "content_sha256": rentry["content_sha256"],
            "roster_revision": rentry["roster"],
            "basis_digest": basis["digest"],
            "issued_at": at,
            "callback_paths": {
                "claim": "/v1/authorizations/claim",
                "started": "/v1/executions/started",
                "settled": "/v1/executions/settled",
            },
        }
        issuance: dict[str, Any] = {
            "auth_id": auth_id,
            "auth_seq": auth_seq,
            "issuance_no": issuance_no,
            "rev": rev,
            "status": "valid",
            "issued_at": at,
            "issued_seq": self.seq + 1,
            "basis": basis,
            "command": command,
            "token": token,
            "claimed_at": None,
            "claim_count": 0,
            "executor_id": None,
            "started_at": None,
            "settled_at": None,
            "terminal_state": None,
            "revoked_at": None,
            "revoke_reason": None,
            "history": [{"status": "valid", "at": at, "seq": self.seq + 1}],
            "objections": [],
            "claims": [],
        }
        decision = "granted" if not items else "reissued"
        result = {
            "ok": True, "granted": True, "decision": decision,
            "id": wid, "rev": rev, "scope": window["scope"],
            "authorization": self.authorization_view(issuance),
            "command": command,
            "basis_digest": basis["digest"],
        }
        payload = {
            "id": wid, "rev": rev, "decision": decision,
            "auth_id": auth_id, "issuance": issuance, "at": at,
        }
        return result, [("grant", payload)]

    def _build_basis(
        self, window: dict[str, Any], rentry: dict[str, Any], evaluation: dict[str, Any]
    ) -> dict[str, Any]:
        proposal_basis = {
            "scope": window["scope"],
            "id": window["id"],
            "rev": rentry["rev"],
            "roster_revision": rentry["roster"],
            "text": rentry.get("text"),
            "content": rentry.get("content"),
            "content_sha256": rentry["content_sha256"],
        }
        requirement_basis = []
        counted_votes = []
        for req in evaluation["requirements"]:
            requirement_basis.append(
                {
                    key: req[key]
                    for key in (
                        "roster_revision", "relation", "side", "members",
                        "minimum", "required", "yes_sites", "yes_members",
                        "missing_required", "satisfied",
                    )
                }
            )
            counted_votes.extend(req["counted_votes"])
        digest = sha256_digest(
            {
                "proposal": proposal_basis,
                "requirements": requirement_basis,
                "counted_votes": counted_votes,
            }
        )
        return {
            "proposal": proposal_basis,
            "requirements": requirement_basis,
            "counted_votes": counted_votes,
            "digest": digest,
        }

    def authorization_view(self, issuance: dict[str, Any]) -> dict[str, Any]:
        status = issuance["status"]
        revocable = status in ("valid", "claimed")
        if revocable:
            revoke_hint = "尚未观测到 started；新的反对/撤回若使条件不足，将停止本授权"
        elif status == "active":
            revoke_hint = "模拟端已 started，不能撤回；必须等待 done/stopped 终态回报"
        elif status == "revoked":
            revoke_hint = "授权已撤销且不可重新启用；重新满足条件后 grant 会产生新的 auth_id"
        elif status in ("done", "stopped"):
            revoke_hint = "已终态，执行边界固定"
        else:
            revoke_hint = "已被新版本提案取代"
        return {
            "auth_id": issuance["auth_id"],
            "issuance_no": issuance["issuance_no"],
            "rev": issuance["rev"],
            "status": status,
            "issued_at": issuance.get("issued_at"),
            "claimed_at": issuance.get("claimed_at"),
            "started_at": issuance.get("started_at"),
            "settled_at": issuance.get("settled_at"),
            "terminal_state": issuance.get("terminal_state"),
            "revoked_at": issuance.get("revoked_at"),
            "revoke_reason": issuance.get("revoke_reason"),
            "revocable_before_start": revocable,
            "withdrawable": revocable,
            "revoke_hint": revoke_hint,
            "basis_digest": issuance["basis"]["digest"],
            "executor_id": issuance.get("executor_id"),
            "claim_count": issuance.get("claim_count", 0),
            "objections": issuance.get("objections", []),
        }

    # ------------------------------------------------------- claim / lifecycle

    def _resolve_issuance_for_claim(
        self, record: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        """返回 (window, issuance, error_code)。"""

        auth_id = record.get("auth_id")
        token = record.get("token")
        if isinstance(auth_id, str) and auth_id:
            for window in self.windows.values():
                for items in window["issuances"].values():
                    for issuance in items:
                        if issuance["auth_id"] == auth_id:
                            if token is not None and issuance.get("token") != token:
                                return window, None, "bad_token"
                            return window, issuance, None
            return None, None, "unknown_authorization"
        wid = record.get("id")
        rev = _as_int(record.get("rev"), "rev", required=False)
        if isinstance(wid, str) and rev is not None:
            window = self.windows.get(wid)
            issuance = self._find_issuance(wid, rev) if window else None
            if issuance is None:
                return window, None, "unknown_authorization"
            if token is not None and issuance.get("token") != token:
                return window, None, "bad_token"
            return window, issuance, None
        raise CommandError("invalid_field", "claim 需要 auth_id(+token) 或 id+rev")

    def _plan_claim(
        self, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        executor_id = record.get("executor_id")
        window, issuance, error = self._resolve_issuance_for_claim(record)
        if error == "bad_token":
            return (
                {"ok": False, "error": "bad_token", "message": "授权令牌不匹配，拒绝领取"},
                [("anomaly", {"kind": "claim", "reason": "bad_token", "record": record})],
            )
        if issuance is None:
            return (
                {"ok": False, "error": error or "unknown_authorization",
                 "message": "没有可领取的授权"},
                [("claim", {"id": record.get("id"), "rev": record.get("rev"),
                            "outcome": "not_found", "at": at})],
            )
        assert window is not None
        base_payload = {
            "id": window["id"], "rev": int(issuance["rev"]),
            "auth_id": issuance["auth_id"], "executor_id": executor_id, "at": at,
        }
        status = issuance["status"]
        if status == "valid":
            payload = {**base_payload, "outcome": "delivered", "seq": self.seq + 1}
            result = {"ok": True, "claimed": True, "outcome": "delivered",
                      "command": issuance["command"],
                      "authorization": self.authorization_view(issuance)}
            return result, [("claim", payload)]
        if status == "claimed":
            payload = {**base_payload, "outcome": "redelivered", "seq": self.seq + 1}
            result = {"ok": True, "claimed": True, "outcome": "redelivered",
                      "command": issuance["command"],
                      "notice": "重复领取，返回同一指令（防止响应丢失导致执行端取不到）"}
            return result, [("claim", payload)]
        if status == "active":
            payload = {**base_payload, "outcome": "polled_active"}
            advisory = None
            if issuance.get("objections"):
                advisory = (
                    "已有新的反对/撤回，但执行已开始；中间件不会另发互斥指令，"
                    "请按演练规程终止并回报 stopped"
                )
            result = {"ok": True, "claimed": False, "outcome": "already_started",
                      "authorization": self.authorization_view(issuance),
                      "stop_advisory": advisory}
            return result, [("claim", payload)]
        if status == "revoked":
            payload = {**base_payload, "outcome": "denied_revoked"}
            result = {"ok": False, "claimed": False, "outcome": "revoked",
                      "error": "authorization_revoked",
                      "message": "授权在开始前已被停止，不得执行该指令；已撤授权不会重新启用",
                      "revoke_reason": issuance.get("revoke_reason"),
                      "revoked_at": issuance.get("revoked_at")}
            return result, [("claim", payload)]
        # done / stopped / superseded
        payload = {**base_payload, "outcome": "terminal_or_closed"}
        result = {"ok": False, "claimed": False, "outcome": status,
                  "error": f"authorization_{status}",
                  "message": f"授权处于 {status}，不能领取执行"}
        return result, [("claim", payload)]

    def _plan_started(
        self, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        wid = _require_str(record, "id")
        rev = _as_int(record.get("rev"), "rev")
        assert rev is not None
        window = self.windows.get(wid)
        issuance = self._find_issuance(wid, rev) if window else None
        executor_id = record.get("executor_id")
        if issuance is None:
            return (
                {"ok": False, "accepted": False, "error": "no_authorization",
                 "message": f"窗口 {wid} rev={rev} 没有已签发授权，不能 started"},
                [("started", {"id": wid, "rev": rev, "outcome": "no_authorization", "at": at})],
            )
        payload_base = {"id": wid, "rev": rev, "auth_id": issuance["auth_id"],
                        "executor_id": executor_id, "at": at, "seq": self.seq + 1}
        status = issuance["status"]
        if status in ("valid", "claimed"):
            payload = {**payload_base, "outcome": "accepted", "from_status": status}
            return (
                {"ok": True, "accepted": True, "status": "active",
                 "auth_id": issuance["auth_id"], "started_at": at},
                [("started", payload)],
            )
        if status == "active":
            return (
                {"ok": True, "accepted": True, "duplicate": True, "status": "active",
                 "auth_id": issuance["auth_id"], "started_at": issuance.get("started_at")},
                [("started", {**payload_base, "outcome": "duplicate"})],
            )
        # revoked / done / stopped / superseded
        message = {
            "revoked": "授权在开始前已撤销，拒绝 started；该动作不得执行",
            "superseded": "授权已被新提案版本取代，拒绝 started",
            "done": "该授权已回报 done，不能重复开始",
            "stopped": "该授权已回报 stopped，不能重复开始",
        }.get(status, f"授权状态 {status} 不允许 started")
        return (
            {"ok": False, "accepted": False, "error": f"start_rejected_{status}",
             "status": status, "auth_id": issuance["auth_id"], "message": message},
            [("started", {**payload_base, "outcome": f"rejected_{status}"})],
        )

    def _plan_settled(
        self, at: int | None, record: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        wid = _require_str(record, "id")
        rev = _as_int(record.get("rev"), "rev")
        state = _require_str(record, "state")
        if state not in ("done", "stopped"):
            raise CommandError("invalid_field", "settled.state 必须是 done 或 stopped")
        assert rev is not None
        window = self.windows.get(wid)
        issuance = self._find_issuance(wid, rev) if window else None
        if issuance is None:
            return (
                {"ok": False, "accepted": False, "error": "no_authorization",
                 "message": "没有对应授权，无法结算终态"},
                [("settled", {"id": wid, "rev": rev, "state": state,
                              "outcome": "no_authorization", "at": at})],
            )
        base = {"id": wid, "rev": rev, "auth_id": issuance["auth_id"],
                "state": state, "detail": record.get("detail"),
                "at": at, "seq": self.seq + 1}
        status = issuance["status"]
        if status == "active":
            return (
                {"ok": True, "accepted": True, "status": state,
                 "auth_id": issuance["auth_id"], "settled_at": at,
                 "mutex_released": True},
                [("settled", {**base, "outcome": "accepted"})],
            )
        if status in ("done", "stopped"):
            same = issuance["status"] == state
            return (
                {"ok": same, "accepted": same, "duplicate": same,
                 "error": None if same else "terminal_conflict",
                 "status": issuance["status"],
                 "message": (
                     "重复的相同终态回报，保持幂等"
                     if same else
                     f"已固定终态 {issuance['status']}，后来的 {state} 回报只记录为冲突，不覆盖真实执行边界"
                 )},
                [("settled", {**base,
                              "outcome": "duplicate" if same else "conflict_kept_first"})],
            )
        if status in ("valid", "claimed"):
            return (
                {"ok": False, "accepted": False, "error": "not_started",
                 "status": status,
                 "message": "执行端尚未 started 该授权，不能直接 settled；如未执行请由反对/撤回停止授权"},
                [("settled", {**base, "outcome": "rejected_not_started"})],
            )
        return (
            {"ok": False, "accepted": False, "error": f"settle_ignored_{status}",
             "status": status,
             "message": f"授权状态为 {status}，终态回报只入审计，不改变授权"},
            [("settled", {**base, "outcome": f"ignored_{status}"})],
        )

    # ----------------------------------------------------------------- inspect

    def inspect(self, query: dict[str, Any] | None = None) -> dict[str, Any]:
        query = query or {}
        wid = query.get("id")
        scope = query.get("scope")
        if isinstance(wid, str) and wid:
            rev = _as_int(query.get("rev"), "rev", required=False)
            return self.inspect_window(wid, rev)
        if isinstance(scope, str) and scope:
            return self.inspect_scope(scope)
        return self.inspect_global()

    def inspect_window(self, wid: str, rev: int | None = None) -> dict[str, Any]:
        window = self.windows.get(wid)
        if window is None:
            return {"ok": False, "found": False, "id": wid,
                    "message": "未收到过该窗口的提案"}
        target_rev = rev if rev is not None else int(window["current_rev"])
        rev_entry = window["revisions"].get(str(target_rev))
        if rev_entry is None:
            return {"ok": False, "found": False, "id": wid, "rev": target_rev,
                    "current_rev": int(window["current_rev"]),
                    "message": "该提案版本不存在"}

        evaluation = self.evaluate_rev(window, target_rev)
        items = window["issuances"].get(str(target_rev), [])
        latest = items[-1] if items else None
        holder = self.live_holder(window["scope"], exclude=(wid, target_rev))
        scope_frontier = self.scope_frontier(window["scope"])

        blockers: list[dict[str, Any]] = []
        live_authorization = None
        current_authorization = (
            self.authorization_view(latest) if latest is not None else None
        )
        if latest is not None and latest["status"] in AUTHORIZATION_LIVE:
            live_authorization = self.authorization_view(latest)
        elif target_rev < int(window["current_rev"]):
            blockers.append({"code": "version_superseded",
                             "fact_zh": f"正在查看旧版本 rev={target_rev}，当前版本为 rev={window['current_rev']}"})
        elif latest is not None and latest["status"] in ("done", "stopped"):
            blockers.append({"code": "terminal", "state": latest["status"],
                             "fact_zh": f"该动作已终态 {latest['status']}，执行边界固定"})
        elif latest is not None and latest["status"] == "superseded":
            blockers.append({"code": "superseded", "fact_zh": "该版本已被新提案版本取代"})
        else:
            blockers.extend(evaluation["owed"])
            if evaluation["quorum_met"] and holder is not None:
                blockers.append({
                    "code": "mutex_held",
                    "holder": holder,
                    "fact_zh": (
                        f"互斥范围 {window['scope']} 被 {holder['id']} rev={holder['rev']} "
                        f"({holder['status']}) 占用，必须等待其 done/stopped"
                    ),
                })

        can_grant_new = (
            latest is None or latest["status"] == "revoked"
        ) and evaluation["quorum_met"] and holder is None and target_rev == int(window["current_rev"])

        slots, ledger = self._slots_and_ledger(window, target_rev)
        vote_history = [
            {
                "at": v.get("at"),
                "seq": v.get("seq"),
                "member": v.get("member"),
                "roster": v.get("roster"),
                "value": v.get("value"),
                "eligible": v.get("eligible"),
                "ineligible_reason": v.get("ineligible_reason"),
                "revocation": v.get("revocation"),
                "post_start_objection": v.get("post_start_objection") is not None,
            }
            for v in ledger
        ]

        roster_basis = []
        bound_roster = self.rosters[window["scope"]][int(rev_entry["roster"])]
        roster_refs = [int(rev_entry["roster"])]
        if bound_roster.get("prev") is not None:
            roster_refs.append(int(bound_roster["prev"]))
        for roster_rev in roster_refs:
            roster_basis.append(self.rosters[window["scope"]][roster_rev])

        return {
            "ok": True,
            "found": True,
            "id": wid,
            "scope": window["scope"],
            "current_rev": int(window["current_rev"]),
            "viewed_rev": target_rev,
            "version_basis": {
                "proposal_revision": target_rev,
                "proposal_created_at": rev_entry["at"],
                "text": rev_entry.get("text"),
                "content": rev_entry.get("content"),
                "content_sha256": rev_entry["content_sha256"],
                "bound_roster_revision": int(rev_entry["roster"]),
                "transition": bound_roster.get("prev") is not None,
                "roster_configurations": roster_basis,
            },
            "quorum": evaluation,
            "vote_history": vote_history,
            "live_authorization": live_authorization,
            "current_authorization": current_authorization,
            "authorizations": [self.authorization_view(a) for a in items],
            "can_grant_new_authorization": can_grant_new,
            "blockers": blockers,
            "scope_execution_frontier": scope_frontier,
            "grant_log": window.get("grant_log", []),
            "report_log": window.get("report_log", []),
        }

    def inspect_scope(self, scope: str) -> dict[str, Any]:
        rosters = self.rosters.get(scope, {})
        windows = [w for w in self.windows.values() if w["scope"] == scope]
        window_summaries = []
        for window in sorted(windows, key=lambda w: w["id"]):
            current_rev = int(window["current_rev"])
            items = window["issuances"].get(str(current_rev), [])
            latest = items[-1] if items else None
            evaluation = None
            try:
                evaluation = self.evaluate_rev(window, current_rev)
            except KeyError:
                pass
            window_summaries.append({
                "id": window["id"],
                "current_rev": current_rev,
                "proposal_at": window["revisions"][str(current_rev)]["at"],
                "quorum_met": evaluation["quorum_met"] if evaluation else None,
                "owed": evaluation["owed"] if evaluation else [],
                "latest_authorization": (
                    self.authorization_view(latest) if latest else None
                ),
            })
        return {
            "ok": True,
            "scope": scope,
            "roster_revisions": [rosters[r] for r in sorted(rosters)],
            "mutex_frontier": self.scope_frontier(scope),
            "windows": window_summaries,
        }

    def inspect_global(self) -> dict[str, Any]:
        scopes = sorted(set(self.rosters.keys()) | {w["scope"] for w in self.windows.values()})
        return {
            "ok": True,
            "seq": self.seq,
            "auth_counter": self.auth_counter,
            "scopes": [
                {
                    "scope": scope,
                    "frontier": self.scope_frontier(scope),
                    "roster_revisions": sorted(self.rosters.get(scope, {}).keys()),
                    "window_count": sum(1 for w in self.windows.values() if w["scope"] == scope),
                }
                for scope in scopes
            ],
            "window_ids": sorted(self.windows.keys()),
            "orphan_vote_count": len(self.orphan_votes),
            "anomaly_count": len(self.anomalies),
            "boots": self.boots,
            "anomalies": self.anomalies[-20:],
        }
