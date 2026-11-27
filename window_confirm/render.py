"""把 inspect 报告渲染成运营可读的中文文本。"""

from __future__ import annotations

from typing import Any

STATUS_ZH = {
    "valid": "已签发/待领取",
    "claimed": "执行端已领取/待开始",
    "active": "执行中（已 started）",
    "done": "已完成",
    "stopped": "已终止",
    "revoked": "已撤销（不可重新启用旧授权）",
    "superseded": "已被新版本提案取代",
}

DECISION_ZH = {
    "granted": "已签发新授权",
    "reissued": "条件重新满足，已签发新的授权（旧授权仍为撤销态）",
    "returned_existing": "返回仍然有效的同一授权（幂等）",
    "execution_in_progress": "该窗口正在执行中，返回当前授权",
    "denied_quorum": "确认条件不足，拒绝签发",
    "blocked_mutex": "互斥范围被占用，暂不能下发",
    "version_superseded": "该提案版本已被取代",
    "already_terminal": "该动作已回报终态",
    "not_found": "窗口或版本不存在",
}


def _status(status: str | None) -> str:
    if status is None:
        return "尚未签发"
    return STATUS_ZH.get(status, status)


def render_window_text(report: dict[str, Any]) -> str:
    if not report.get("found", True):
        return f"未找到窗口：{report.get('id')}（{report.get('message', '')}）"

    lines: list[str] = []
    lines.append(f"窗口 {report['id']}（scope={report['scope']}）")
    lines.append(
        f"  提案版本：查看 rev={report['viewed_rev']}，当前 rev={report['current_rev']}"
    )
    basis = report["version_basis"]
    lines.append(f"  内容摘要：{basis.get('text') or basis.get('content')}")
    lines.append(f"  内容哈希：{basis['content_sha256'][:16]}…")
    lines.append(
        f"  绑定成员配置：roster r{basis['bound_roster_revision']}"
        + ("（过渡期：同时检查 prev 旧配置）" if basis["transition"] else "")
    )
    for roster in basis["roster_configurations"]:
        tag = "新组" if roster["revision"] == basis["bound_roster_revision"] else "旧组"
        lines.append(
            f"    - [{tag}] r{roster['revision']} members={roster['members']} "
            f"minimum={roster['minimum']} required={roster['required']}"
            + (f" prev={roster['prev']}" if roster.get("prev") is not None else "")
        )

    quorum = report["quorum"]
    lines.append("  确认情况：")
    for req in quorum["requirements"]:
        mark = "满足" if req["satisfied"] else "不足"
        lines.append(
            f"    - {req['side_zh']} r{req['roster_revision']}："
            f"{req['have_sites']}/{req['minimum']} 独立站点，"
            f"赞成={req['yes_members']}，缺责任方={req['missing_required'] or '无'} → {mark}"
        )
    if quorum["owed"]:
        lines.append("  仍欠：")
        for owed in quorum["owed"]:
            lines.append(f"    * {owed['fact_zh']}")
    else:
        lines.append("  仍欠：无（公布条件已满足）")

    live = report.get("live_authorization")
    if live:
        lines.append("  当前可执行授权：")
        lines.append(f"    auth_id={live['auth_id']} 状态={_status(live['status'])}")
        lines.append(f"    获权时点 at={live['issued_at']}，basis_digest={live['basis_digest'][:16]}…")
        lines.append(
            f"    started_at={live['started_at']} settled_at={live['settled_at']} "
            f"terminal={live['terminal_state']}"
        )
        lines.append(
            f"    现在还能撤回授权吗：{'能（尚未 started）' if live['withdrawable'] else '不能'}"
        )
        lines.append(f"    说明：{live['revoke_hint']}")
        if live.get("objections"):
            lines.append(f"    开始后异议记录：{len(live['objections'])} 条（不停止执行，等待终态）")
    else:
        lines.append("  当前可执行授权：无")

    if report.get("authorizations"):
        lines.append("  授权历史（只追加，旧授权不会复活）：")
        for auth in report["authorizations"]:
            lines.append(
                f"    #{auth['issuance_no']} {auth['auth_id']} "
                f"{_status(auth['status'])} issued_at={auth['issued_at']}"
                + (f" revoke_reason={auth['revoke_reason']}" if auth.get("revoke_reason") else "")
            )

    frontier = report["scope_execution_frontier"]
    lines.append(f"  互斥范围 {report['scope']} 的真实执行边界：")
    lines.append(f"    当前占用：{frontier['mutex_holder'] or '无'}")
    lines.append(f"    最近 started：{frontier['last_started']}")
    lines.append(f"    最近 settled：{frontier['last_settled']}")

    if report["blockers"] and not live:
        lines.append("  仍不能动的原因：")
        for blocker in report["blockers"]:
            fact = blocker.get("fact_zh") or blocker.get("code")
            lines.append(f"    ! {fact}")
    chain = report.get("event_chain")
    if chain:
        lines.append(f"  事件链：{chain}")
    return "\n".join(lines)


def render_scope_text(report: dict[str, Any]) -> str:
    lines = [f"资源范围 scope={report['scope']}"]
    for roster in report["roster_revisions"]:
        lines.append(
            f"  roster r{roster['revision']}: members={roster['members']} "
            f"minimum={roster['minimum']} required={roster['required']} prev={roster.get('prev')}"
        )
    frontier = report["mutex_frontier"]
    lines.append(f"  互斥占用：{frontier['mutex_holder'] or '无'}")
    lines.append(f"  最近终态：{frontier['last_settled']}")
    for window in report["windows"]:
        auth = window.get("latest_authorization")
        lines.append(
            f"  窗口 {window['id']} rev={window['current_rev']} "
            f"quorum_met={window['quorum_met']} "
            f"auth={auth['auth_id'] if auth else '-'} "
            f"status={auth['status'] if auth else '-'}"
        )
        for owed in window.get("owed", []):
            lines.append(f"      欠：{owed['fact_zh']}")
    return "\n".join(lines)


def render_any(report: dict[str, Any]) -> str:
    if not report.get("ok", True):
        return f"错误：{report.get('error')} {report.get('message', '')}"
    if "viewed_rev" in report:
        return render_window_text(report)
    if "roster_revisions" in report:
        return render_scope_text(report)
    return "全局状态：\n" + "\n".join(
        f"  scope={s['scope']} holder={s['frontier']['mutex_holder']} windows={s['window_count']}"
        for s in report.get("scopes", [])
    )
