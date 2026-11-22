#!/usr/bin/env python3
"""可重放的端到端演示：在临时库上走完摄入→评估→审批→付款→重审全链路。

运行：python3 -I demo/demo_walkthrough.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fundbench.store import Store  # noqa: E402
from fundbench.workbench import WorkbenchError  # noqa: E402
from fundbench.workbench import Workbench  # noqa: E402


def section(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        wb = Workbench(Store(Path(tmp) / "demo.json"))

        section("1. 摄入合成资料（幂等，可重复执行）")
        report = wb.ingest_file(ROOT / "fixtures" / "sources.json")
        print(f"新增信封 {len(report.added)} 个；补录说明：{report.notes}")

        section("2. 申请总览")
        from fundbench.report import render_overview
        print(render_overview(wb))

        section("3. 重审队列：政策调整 vs 材料更正/撤回（终态项目不进队列）")
        from fundbench.report import render_review_queue
        print(render_review_queue(wb))

        section("4. AP-83 物业费 6500 超家庭上限 6000 → 部分批准并付款")
        decision = wb.decide("AP-83", "approved", by="clerk-zhang",
                             note="按家庭年度上限部分批准")
        print(json.dumps(decision, ensure_ascii=False))
        print(wb.register_payment("AP-83", "PAY-20", decision["granted_amount_yuan"]))

        section("5. 红线：已付款 AP-81 拒绝改判（应报错）")
        try:
            wb.decide("AP-81", "rejected", by="clerk-li")
        except WorkbenchError as exc:
            print(f"已拦截：{exc}")

        section("6. AP-84 重复发票不能批准；核实后拒绝")
        try:
            wb.decide("AP-84", "approved", by="clerk-li")
        except WorkbenchError as exc:
            print(f"已拦截：{exc}")
        print(wb.decide("AP-84", "rejected", by="clerk-li", note="发票重复提交"))

        section("7. AP-89 发票更正后特批，金额仍被约束为 2800")
        print(wb.decide("AP-89", "approved", by="clerk-wang", force=True,
                        note="核实更正属实，按更正金额特批"))

        section("8. 家庭额度台账（家庭上限 ≠ 个人账户余额）")
        from fundbench.report import render_ledger
        print(render_ledger(wb))

        section("9. 审核员追踪 AP-81：决定 → 第4版政策 → 冻结证据哈希")
        from fundbench.report import render_trace
        print(render_trace(wb, "AP-81"))

        section("10. 申请人视图 AP-82：待补什么、款项是否到账")
        from fundbench.report import render_applicant
        print(render_applicant(wb, "AP-82"))

        section("11. 事件日志摘要")
        for event in wb.store.events():
            if event["type"] != "envelope.ingested":
                print(f"  {event['type']}: {event['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
