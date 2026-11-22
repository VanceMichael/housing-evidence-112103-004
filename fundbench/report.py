"""文本与 HTML 报表：同一派生于注册表的数据，两种呈现。"""
from __future__ import annotations

import html
from typing import Any

from .models import DOC_LABELS, USE_LABELS
from .workbench import Workbench


# ---------- 通用辅助 ----------
def _state_label(state: str) -> str:
    return {
        "submitted": "待审核",
        "approved": "已批待付",
        "paid": "已到账",
        "rejected": "已拒绝",
        "withdrawn": "已撤回",
    }.get(state, state)


def _money(value: int | None) -> str:
    return "—" if value is None else f"{value:,} 元"


# ---------- 工作台总览（文本） ----------
def render_overview(wb: Workbench) -> str:
    registry = wb.registry()
    lines: list[str] = []
    lines.append("住房消费提取工作台 · 申请总览")
    lines.append("=" * 72)
    header = f"{'申请号':<8}{'城市':<8}{'用途':<6}{'申请金额':>10}{'状态':<8}{'硬性问题':<6}{'可批准':>10}"
    lines.append(header)
    lines.append("-" * 72)
    engine = wb.engine()
    for app in registry.ordered_applications():
        state = registry.application_state(app.application_id)
        ev = engine.evaluate(app.application_id)
        lines.append(
            f"{app.application_id:<8}{app.city:<8}"
            f"{USE_LABELS.get(app.use, app.use):<6}"
            f"{app.amount_yuan:>8,} {_state_label(state):<7}"
            f"{len(ev.blockers):^8}{ev.grantable_amount:>8,}"
        )
    lines.append("")

    queue = wb.review_queue()
    lines.append(f"待重审：政策驱动 {len(queue['policy_driven'])} 件 / "
                 f"材料驱动 {len(queue['material_driven'])} 件")
    ledger = wb.ledger()
    lines.append(f"额度台账：{len(ledger.all_entries())} 条有效决定占用记录")
    return "\n".join(lines)


def render_review_queue(wb: Workbench) -> str:
    queue = wb.review_queue()
    lines = ["重审队列（仅含未结申请；已付款/已拒绝/已撤回项目不重审）", "=" * 72]
    if not queue["policy_driven"] and not queue["material_driven"]:
        lines.append("（空）")
        return "\n".join(lines)
    lines.append("【政策调整驱动】")
    for item in queue["policy_driven"]:
        lines.append(f"  {item['application_id']}（{_state_label(item['state'])}）："
                     + "；".join(item["reasons"]))
    lines.append("")
    lines.append("【材料更正/撤回驱动】")
    for item in queue["material_driven"]:
        lines.append(f"  {item['application_id']}（{_state_label(item['state'])}）："
                     + "；".join(item["reasons"]))
        if item["blockers"]:
            lines.append(f"      当前硬性问题：{', '.join(item['blockers'])}")
        lines.append(f"      当前可批准金额：{_money(item['grantable_amount'])}")
    return "\n".join(lines)


def render_ledger(wb: Workbench) -> str:
    registry = wb.registry()
    ledger = wb.ledger()
    lines = ["家庭年度额度台账（家庭上限 ≠ 个人账户余额）", "=" * 72]
    lines.append(f"{'家庭':<10}{'年度':<6}{'用途':<8}{'申请号':<8}"
                 f"{'成员':<10}{'已批占用':>10}{'已付':>10}{'状态':<8}")
    lines.append("-" * 72)
    for e in sorted(ledger.all_entries(),
                    key=lambda x: (x.household_ref, x.year, x.use, x.application_id)):
        member = registry.members.get(e.member_ref)
        lines.append(
            f"{e.household_ref:<10}{e.year:<6}"
            f"{USE_LABELS.get(e.use, e.use):<8}{e.application_id:<8}"
            f"{(member.name if member else e.member_ref):<10}"
            f"{e.granted_amount_yuan:>8,} {e.paid_amount_yuan:>8,} "
            f"{_state_label(e.state):<7}"
        )
    lines.append("")
    lines.append("家庭上限对照：")
    for city in registry.policy_book.cities():
        rule = registry.policy_book.latest(city)
        caps = "，".join(
            f"{USE_LABELS.get(u, u)} {('不限' if c is None else f'{c:,} 元')}"
            for u, c in sorted(rule.caps.items()))
        lines.append(f"  {city} 第 {rule.revision} 版（{rule.effective_from} 起）：{caps}；"
                     f"票据受理窗口 {rule.receipt_window_months} 个月")
    lines.append("")
    lines.append("个人账户余额：")
    for member in sorted(registry.members.values(), key=lambda m: m.member_id):
        lines.append(f"  {member.member_id} {member.name}（{member.household_ref}）："
                     f"{member.account_balance_yuan:,} 元")
    return "\n".join(lines)


def render_trace(wb: Workbench, application_id: str) -> str:
    view = wb.trace_view(application_id)
    app = view["application"]
    ev = view["evaluation"]
    lines = [f"审核追踪 · {application_id}", "=" * 72]
    lines.append(f"申请人/家庭：{app['member_ref']} / {app['household_ref']}")
    lines.append(f"城市/用途：{app['city']} / {app['use_label']}")
    lines.append(f"申请金额：{_money(app['amount_yuan'])}　提交日期：{app['submitted_date']}"
                 f"　状态：{_state_label(app['state'])}")
    lines.append("")
    lines.append("评估发现：")
    if not ev["findings"]:
        lines.append("  （无）")
    for f in ev["findings"]:
        tag = "硬性" if f["level"] == "blocker" else "提示"
        lines.append(f"  [{tag}] {f['code']}: {f['message']}")
    lines.append(f"有效票据合计：{_money(ev['receipt_sum'])}　"
                 f"家庭上限剩余：{_money(ev['cap_remaining'])}　"
                 f"账户余额：{_money(ev['account_balance'])}　"
                 f"可批准：{_money(ev['grantable_amount'])}")
    lines.append("")
    decision = view["decision"]
    if decision:
        lines.append(f"决定：{decision['decision_id']} / {decision['outcome']} / "
                     f"批准 {_money(decision['granted_amount_yuan'])} / "
                     f"审核员 {decision['by']} / {decision['decided_at']}")
        lines.append(f"依据政策：{decision['policy_city']} 第 "
                     f"{decision['policy_revision']} 版")
        snap = decision.get("frozen_snapshot") or {}
        if snap:
            lines.append("冻结证据清单：")
            for item in snap.get("evidence", []):
                lines.append(
                    f"  - {DOC_LABELS.get(item['kind'], item['kind'])} {item['ref']} "
                    f"rev.{item['revision']} 状态={item['state']} 哈希={item['hash']}")
            policy = snap.get("policy", {})
            lines.append(f"冻结政策哈希依据：{policy.get('city')} rev."
                         f"{policy.get('revision')} 生效 {policy.get('effective_from')}")
    else:
        lines.append("决定：尚未作出")
    lines.append("")
    lines.append("付款记录：")
    if not view["payments"]:
        lines.append("  （无）")
    for p in view["payments"]:
        lines.append(f"  {p['payment_id']} {_money(p['amount_yuan'])} "
                     f"{p['state']} 到账时间 {p['paid_at']}")
    lines.append("")
    lines.append("票据版本历史：")
    for r in view["receipt_history"]:
        lines.append(f"  {r['receipt_id']} 当前 rev.{r['revision']} 状态={r['state']}")
        for h in r["history"]:
            lines.append(f"      信封 {h['source_id']} @ {h['collected_at']} "
                         f"rev.{h.get('revision', 1)} 金额 {h.get('amount_yuan')} "
                         f"状态 {h.get('state', 'active')}")
    return "\n".join(lines)


def render_applicant(wb: Workbench, application_id: str) -> str:
    view = wb.applicant_view(application_id)
    lines = [f"申请人进度 · {application_id}", "=" * 72]
    lines.append(f"申请人：{view['applicant']}　城市：{view['city']}　"
                 f"用途：{view['use_label']}")
    lines.append(f"申请金额：{_money(view['requested_amount_yuan'])}　"
                 f"当前状态：{view['state_label']}　提交日期：{view['submitted_date']}")
    lines.append("")
    lines.append("待补材料：")
    if not view["missing_materials"]:
        lines.append("  材料齐备")
    for m in view["missing_materials"]:
        lines.append(f"  - {m['label']}（{m['code']}）")
    lines.append("")
    lines.append("审核问题：")
    if not view["issues"]:
        lines.append("  （无硬性问题）")
    for issue in view["issues"]:
        lines.append(f"  - {issue['message']}")
    for w in view["warnings"]:
        lines.append(f"  （提示）{w['message']}")
    lines.append("")
    lines.append(f"批准金额：{_money(view['granted_amount_yuan'])}　"
                 f"已到账：{_money(view['paid_amount_yuan'])}")
    if view["payment"]:
        p = view["payment"]
        lines.append(f"付款回执：{p['payment_id']} {p['state']} 到账时间 {p['paid_at']}")
    return "\n".join(lines)


# ---------- HTML 单文件报表 ----------
def render_html(wb: Workbench) -> str:
    registry = wb.registry()
    engine = wb.engine()
    queue = wb.review_queue()
    ledger = wb.ledger()

    app_rows = []
    for app in registry.ordered_applications():
        state = registry.application_state(app.application_id)
        ev = engine.evaluate(app.application_id)
        blocker_txt = "<br>".join(html.escape(f.message) for f in ev.blockers) or "—"
        app_rows.append(
            "<tr>"
            f"<td>{html.escape(app.application_id)}</td>"
            f"<td>{html.escape(app.city)}</td>"
            f"<td>{html.escape(USE_LABELS.get(app.use, app.use))}</td>"
            f"<td class='num'>{app.amount_yuan:,}</td>"
            f"<td><span class='state {html.escape(state)}'>{html.escape(_state_label(state))}</span></td>"
            f"<td>{blocker_txt}</td>"
            f"<td class='num'>{ev.grantable_amount:,}</td>"
            "</tr>"
        )

    def queue_rows(items: list[dict[str, Any]]) -> str:
        if not items:
            return "<tr><td colspan='3'>（空）</td></tr>"
        return "".join(
            "<tr>"
            f"<td>{html.escape(i['application_id'])}</td>"
            f"<td>{html.escape(_state_label(i['state']))}</td>"
            f"<td>{html.escape('；'.join(i['reasons']))}</td>"
            "</tr>" for i in items
        )

    ledger_rows = "".join(
        "<tr>"
        f"<td>{html.escape(e.household_ref)}</td><td>{e.year}</td>"
        f"<td>{html.escape(USE_LABELS.get(e.use, e.use))}</td>"
        f"<td>{html.escape(e.application_id)}</td>"
        f"<td>{html.escape(e.member_ref)}</td>"
        f"<td class='num'>{e.granted_amount_yuan:,}</td>"
        f"<td class='num'>{e.paid_amount_yuan:,}</td>"
        f"<td>{html.escape(_state_label(e.state))}</td>"
        "</tr>" for e in ledger.all_entries()
    ) or "<tr><td colspan='8'>（空）</td></tr>"

    policy_rows = []
    for city in registry.policy_book.cities():
        for rule in registry.policy_book.versions(city):
            caps = "，".join(
                f"{USE_LABELS.get(u, u)}: {'不限' if c is None else f'{c:,}'}"
                for u, c in sorted(rule.caps.items()))
            policy_rows.append(
                "<tr>"
                f"<td>{html.escape(city)}</td><td>{rule.revision}</td>"
                f"<td>{rule.effective_from}</td>"
                f"<td>{html.escape('、'.join(USE_LABELS.get(u, u) for u in rule.eligible_use))}</td>"
                f"<td>{html.escape(caps)}</td>"
                f"<td>{rule.receipt_window_months} 个月</td>"
                "</tr>"
            )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>住房消费提取工作台</title>
<style>
body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #1f2933; }}
h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; border-left: 4px solid #2b6cb0; padding-left: 8px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid #cbd2d9; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f4f8; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.state {{ padding: 2px 8px; border-radius: 10px; font-size: 12px; }}
.state.paid {{ background: #c6f6d5; }} .state.approved {{ background: #bee3f8; }}
.state.submitted {{ background: #edf2f7; }} .state.rejected {{ background: #fed7d7; }}
.state.withdrawn {{ background: #e9d8fd; }}
small {{ color: #616e7c; }}
</style></head><body>
<h1>住房消费提取工作台 · 办理总览</h1>
<small>全部为虚构合成资料；政策调整只影响未结申请，已付款项目保留当时依据。</small>
<h2>申请清单</h2>
<table><thead><tr><th>申请号</th><th>城市</th><th>用途</th><th>申请金额</th><th>状态</th><th>硬性问题</th><th>可批准金额</th></tr></thead>
<tbody>{''.join(app_rows)}</tbody></table>
<h2>重审队列 · 政策驱动</h2>
<table><thead><tr><th>申请号</th><th>状态</th><th>原因</th></tr></thead>
<tbody>{queue_rows(queue['policy_driven'])}</tbody></table>
<h2>重审队列 · 材料驱动</h2>
<table><thead><tr><th>申请号</th><th>状态</th><th>原因</th></tr></thead>
<tbody>{queue_rows(queue['material_driven'])}</tbody></table>
<h2>家庭年度额度台账</h2>
<table><thead><tr><th>家庭</th><th>年度</th><th>用途</th><th>申请号</th><th>成员</th><th>已批占用</th><th>已付</th><th>状态</th></tr></thead>
<tbody>{ledger_rows}</tbody></table>
<h2>政策版本</h2>
<table><thead><tr><th>城市</th><th>版本</th><th>生效日</th><th>可申请用途</th><th>家庭年度上限</th><th>票据窗口</th></tr></thead>
<tbody>{''.join(policy_rows)}</tbody></table>
</body></html>
"""
