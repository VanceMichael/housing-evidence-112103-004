"""命令行入口。

示例：
  python3 -m fundbench --store data/wb.json ingest fixtures/sources.json
  python3 -m fundbench --store data/wb.json list
  python3 -m fundbench --store data/wb.json evaluate AP-83
  python3 -m fundbench --store data/wb.json decide AP-83 approved --by clerk-1
  python3 -m fundbench --store data/wb.json pay AP-83 PAY-20
  python3 -m fundbench --store data/wb.json queue
  python3 -m fundbench --store data/wb.json trace AP-81
  python3 -m fundbench --store data/wb.json applicant AP-82
  python3 -m fundbench --store data/wb.json ledger
  python3 -m fundbench --store data/wb.json html - report.html
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .models import WorkbenchError
from .report import (
    render_applicant,
    render_html,
    render_ledger,
    render_overview,
    render_review_queue,
    render_trace,
)
from .store import Store
from .workbench import Workbench


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fundbench",
        description="住房消费提取资料采集与办理工作台（合成数据，离线运行）")
    parser.add_argument("--store", default="data/workbench.json",
                        help="工作台数据文件路径（默认 data/workbench.json）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="摄入合成资料信封文件（可重复执行，按 source_id 幂等）")
    p.add_argument("path", help="包含 sources 数组的 JSON 文件")
    p.add_argument("--reset", action="store_true", help="摄入前清空现有工作台数据")

    sub.add_parser("list", help="申请总览")
    sub.add_parser("queue", help="重审队列（政策驱动 / 材料驱动）")
    sub.add_parser("ledger", help="家庭年度额度台账与个人账户余额")

    p = sub.add_parser("evaluate", help="对单条申请做规则评估")
    p.add_argument("application_id")
    p.add_argument("--as-of", help="评估日期 YYYY-MM-DD，默认今天")

    p = sub.add_parser("decide", help="记录审核决定")
    p.add_argument("application_id")
    p.add_argument("outcome", choices=["approved", "rejected"])
    p.add_argument("--by", required=True, help="审核员工号")
    p.add_argument("--note", default="")
    p.add_argument("--as-of", help="决定日期 YYYY-MM-DD，默认今天")
    p.add_argument("--force", action="store_true", help="存在硬性问题时强制批准（特批留痕）")

    p = sub.add_parser("pay", help="对已批准申请登记付款")
    p.add_argument("application_id")
    p.add_argument("payment_id")
    p.add_argument("--amount", type=int, help="付款金额，默认取批准金额")
    p.add_argument("--paid-at", help="到账时间 ISO8601")

    p = sub.add_parser("withdraw", help="撤回未结申请")
    p.add_argument("application_id")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("trace", help="审核员追踪视图（决定→政策版本→证据哈希）")
    p.add_argument("application_id")

    p = sub.add_parser("applicant", help="申请人视图（待补材料与到账进度）")
    p.add_argument("application_id")

    p = sub.add_parser("html", help="导出单文件 HTML 报表")
    p.add_argument("output", help="输出路径，- 表示标准输出")

    sub.add_parser("events", help="查看工作台事件日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.store)
    if args.command == "ingest" and args.reset:
        store.reset()
    wb = Workbench(store)

    try:
        if args.command == "ingest":
            report = wb.ingest_file(Path(args.path))
            if args.json:
                print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(f"新增 {len(report.added)} 个信封，跳过 {len(report.skipped)} 个已存在信封")
                for note in report.notes:
                    print(f"  · {note}")
            return 0

        if args.command == "list":
            print(json.dumps([_app_json(wb, a.application_id)
                              for a in wb.registry().ordered_applications()],
                             ensure_ascii=False, indent=2)
                  if args.json else render_overview(wb))
            return 0

        if args.command == "queue":
            queue = wb.review_queue()
            print(json.dumps(queue, ensure_ascii=False, indent=2)
                  if args.json else render_review_queue(wb))
            return 0

        if args.command == "ledger":
            ledger = wb.ledger()
            if args.json:
                print(json.dumps([e.to_dict() for e in ledger.all_entries()],
                                 ensure_ascii=False, indent=2))
            else:
                print(render_ledger(wb))
            return 0

        if args.command == "evaluate":
            as_of = date.fromisoformat(args.as_of) if args.as_of else None
            ev = wb.evaluate(args.application_id, as_of)
            print(json.dumps(ev.to_dict(), ensure_ascii=False, indent=2)
                  if args.json else _render_evaluation_text(ev.to_dict()))
            return 0

        if args.command == "decide":
            as_of = date.fromisoformat(args.as_of) if args.as_of else None
            result = wb.decide(args.application_id, args.outcome, by=args.by,
                               note=args.note, as_of=as_of, force=args.force)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else f"已记录决定 {result['decision_id']}：{result['outcome']}，"
                       f"批准 {result['granted_amount_yuan']:,} 元")
            return 0

        if args.command == "pay":
            registry = wb.registry()
            decision = registry.latest_decision(args.application_id)
            amount = args.amount or (decision.granted_amount_yuan if decision else None)
            if amount is None:
                raise WorkbenchError("申请尚未批准，无法付款")
            result = wb.register_payment(args.application_id, args.payment_id,
                                         amount, paid_at=args.paid_at)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else f"付款 {result['payment_id']} 已登记：{result['amount_yuan']:,} 元到账")
            return 0

        if args.command == "withdraw":
            result = wb.withdraw_application(args.application_id, by=args.by,
                                             reason=args.reason)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else f"申请 {result['application_id']} 已撤回")
            return 0

        if args.command == "trace":
            view = wb.trace_view(args.application_id)
            print(json.dumps(view, ensure_ascii=False, indent=2) if args.json
                  else render_trace(wb, args.application_id))
            return 0

        if args.command == "applicant":
            view = wb.applicant_view(args.application_id)
            print(json.dumps(view, ensure_ascii=False, indent=2) if args.json
                  else render_applicant(wb, args.application_id))
            return 0

        if args.command == "html":
            content = render_html(wb)
            if args.output == "-":
                sys.stdout.write(content)
            else:
                Path(args.output).write_text(content, encoding="utf-8")
                print(f"HTML 报表已写入 {args.output}")
            return 0

        if args.command == "events":
            print(json.dumps(store.events(), ensure_ascii=False, indent=2))
            return 0

    except (WorkbenchError, ValueError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 1


def _app_json(wb: Workbench, application_id: str) -> dict:
    registry = wb.registry()
    ev = wb.evaluate(application_id)
    return {
        "application_id": application_id,
        "state": registry.application_state(application_id),
        "blockers": [f.code for f in ev.blockers],
        "warnings": [f.code for f in ev.warnings],
        "grantable_amount": ev.grantable_amount,
    }


def _render_evaluation_text(ev: dict) -> str:
    lines = [f"评估 · {ev['application_id']}（as_of={ev['as_of']}）",
             f"可办理：{'是' if ev['ready'] else '否'}　"
             f"可批准金额：{ev['grantable_amount']:,} 元",
             f"申请 {ev['requested_amount']:,} 元 / 有效票据 {ev['receipt_sum']:,} 元 / "
             f"家庭上限剩余 {ev['cap_remaining']} / 账户余额 {ev['account_balance']}"]
    lines.append("发现项：")
    if not ev["findings"]:
        lines.append("  （无）")
    for f in ev["findings"]:
        tag = "硬性" if f["level"] == "blocker" else "提示"
        lines.append(f"  [{tag}] {f['code']}: {f['message']}")
    lines.append("证据：")
    for item in ev["attached_evidence"]:
        lines.append(f"  - {item['kind']} {item['ref']} rev.{item['revision']} "
                     f"状态={item['state']} 哈希={item['hash']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
