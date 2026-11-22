"""办理流与台账不变式测试：审批、付款、撤回、重审、额度红线。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fundbench.ledger import HouseholdLedger  # noqa: E402
from fundbench.models import WorkbenchError  # noqa: E402

from tests._support import WorkbenchTestCase  # noqa: E402


class DecisionAndPaymentTests(WorkbenchTestCase):
    def test_partial_cap_approval_then_payment(self):
        result = self.wb.decide("AP-83", "approved", by="clerk-1")
        self.assertEqual(result["granted_amount_yuan"], 6000)
        self.assertEqual(self.state("AP-83"), "approved")
        pay = self.wb.register_payment("AP-83", "PAY-20", 6000)
        self.assertEqual(pay["state"], "paid")
        self.assertEqual(self.state("AP-83"), "paid")
        # 申请人视图反映到账
        view = self.wb.applicant_view("AP-83")
        self.assertEqual(view["paid_amount_yuan"], 6000)
        self.assertEqual(view["state_label"], "已到账")

    def test_payment_rejects_amount_mismatch_and_double_pay(self):
        self.wb.decide("AP-83", "approved", by="clerk-1")
        with self.assertRaises(WorkbenchError):
            self.wb.register_payment("AP-83", "PAY-X", 6500)
        self.wb.register_payment("AP-83", "PAY-20", 6000)
        with self.assertRaises(WorkbenchError):
            self.wb.register_payment("AP-83", "PAY-21", 6000)

    def test_cannot_pay_without_approval(self):
        with self.assertRaises(WorkbenchError):
            self.wb.register_payment("AP-82", "PAY-X", 100)

    def test_blocker_prevents_approval_unless_force(self):
        with self.assertRaises(WorkbenchError):
            self.wb.decide("AP-84", "approved", by="clerk-1")
        forced = self.wb.decide("AP-89", "approved", by="clerk-1", force=True,
                                note="核实更正后特批")
        # 特批仍受可批准金额约束（2800，而不是申请额 3000）
        self.assertEqual(forced["granted_amount_yuan"], 2800)
        self.assertIn("RECEIPT_AMOUNT_LOW", forced["blockers"])

    def test_rejected_application_is_terminal(self):
        self.wb.decide("AP-84", "rejected", by="clerk-1", note="发票重复")
        self.assertEqual(self.state("AP-84"), "rejected")
        with self.assertRaises(WorkbenchError):
            self.wb.decide("AP-84", "approved", by="clerk-2")
        with self.assertRaises(WorkbenchError):
            self.wb.register_payment("AP-84", "PAY-X", 1)

    def test_withdraw_open_application_releases_nothing_but_excludes_ledger(self):
        # AP-85 未作出过批准决定，撤回不影响任何已占用额度
        before = {(e.application_id, e.granted_amount_yuan)
                  for e in HouseholdLedger(self.wb.registry()).all_entries()}
        self.wb.withdraw_application("AP-85", by="clerk-1")
        self.assertEqual(self.state("AP-85"), "withdrawn")
        after = {(e.application_id, e.granted_amount_yuan)
                 for e in HouseholdLedger(self.wb.registry()).all_entries()}
        self.assertEqual(before, after)

    def test_paid_application_cannot_be_withdrawn_or_redecided(self):
        with self.assertRaises(WorkbenchError):
            self.wb.withdraw_application("AP-81", by="clerk-1")
        with self.assertRaises(WorkbenchError):
            self.wb.decide("AP-81", "rejected", by="clerk-1")


class LedgerInvariantTests(WorkbenchTestCase):
    def test_household_quota_shared_between_members(self):
        ledger = HouseholdLedger(self.wb.registry())
        # 初始：AP-80 购房 120000、AP-81 装修 38000（同一家庭不同成员、不同用途桶）
        self.assertEqual(ledger.used("H-SYN-5", 2026, "home-purchase"), 120000)
        self.assertEqual(ledger.used("H-SYN-5", 2026, "home-repair"), 38000)
        self.assertEqual(ledger.used("H-SYN-5", 2026, "property-fee"), 0)
        # 不同家庭互不影响
        self.assertEqual(ledger.used("H-SYN-6", 2026, "property-fee"), 0)

    def test_cap_consumed_after_approval_blocks_further_capacity(self):
        self.wb.decide("AP-83", "approved", by="clerk-1")  # 占满物业费 6000
        ev = self.wb.evaluate("AP-82")  # 同家庭另一成员再申请物业费
        self.assertEqual(ev.cap_remaining, 0)
        self.assertEqual(ev.grantable_amount, 0)
        self.assertIn("CAP_PARTIAL", [f.code for f in ev.warnings])

    def test_invalid_material_does_not_release_other_members_quota(self):
        # 红线：AP-81 的发票事后被撤回，但它已付款，台账仍保留 38000 占用
        ledger = HouseholdLedger(self.wb.registry())
        repair_used = ledger.used("H-SYN-5", 2026, "home-repair")
        self.assertEqual(repair_used, 38000)
        entry = next(e for e in ledger.all_entries() if e.application_id == "AP-81")
        self.assertEqual(entry.paid_amount_yuan, 38000)
        self.assertEqual(entry.state, "paid")

    def test_redecision_rebuilds_ledger_automatically(self):
        # 构造：先批准 AP-89（2800，已批待付），再拒绝它 -> 台账自动移除该占用
        self.wb.decide("AP-89", "approved", by="clerk-1", force=True)
        ledger = HouseholdLedger(self.wb.registry())
        self.assertEqual(ledger.used("H-SYN-5", 2026, "home-repair",
                                     exclude_application="AP-81"), 2800)
        self.wb.decide("AP-89", "rejected", by="clerk-2", note="复核拒绝")
        ledger2 = HouseholdLedger(self.wb.registry())
        self.assertEqual(ledger2.used("H-SYN-5", 2026, "home-repair",
                                      exclude_application="AP-81"), 0)
        # 历史决定信封仍保留，可追溯
        decisions = self.wb.registry().decisions["AP-89"]
        self.assertEqual([d.outcome for d in decisions], ["approved", "rejected"])


class ReviewQueueTests(WorkbenchTestCase):
    def test_queue_excludes_terminal_and_lists_open_cases(self):
        queue = self.wb.review_queue()
        policy_ids = {i["application_id"] for i in queue["policy_driven"]}
        material_ids = {i["application_id"] for i in queue["material_driven"]}
        self.assertEqual(policy_ids, {"AP-86"})
        self.assertEqual(material_ids, {"AP-85", "AP-89"})
        # 已付款（AP-80/AP-81）与已拒绝/撤回项目永远不进队列
        all_ids = policy_ids | material_ids
        for terminal_id in ("AP-80", "AP-81"):
            self.assertNotIn(terminal_id, all_ids)

    def test_queue_clears_after_decision(self):
        self.wb.decide("AP-86", "rejected", by="clerk-1",
                       note="新政已取消购房提取")
        self.wb.withdraw_application("AP-85", by="clerk-1")
        queue = self.wb.review_queue()
        ids = {i["application_id"] for group in queue.values() for i in group}
        self.assertNotIn("AP-86", ids)
        self.assertNotIn("AP-85", ids)

    def test_approved_unpaid_still_re_reviewable_on_material_change(self):
        # 已批待付不是终态：材料更正仍须提示重审
        self.wb.decide("AP-89", "approved", by="clerk-1", force=True)
        queue = self.wb.review_queue()
        material_ids = {i["application_id"] for i in queue["material_driven"]}
        self.assertIn("AP-89", material_ids)


class TraceabilityTests(WorkbenchTestCase):
    def test_trace_links_decision_to_policy_and_evidence_hashes(self):
        self.wb.decide("AP-83", "approved", by="clerk-1")
        view = self.wb.trace_view("AP-83")
        decision = view["decision"]
        self.assertEqual(decision["policy_revision"], 4)
        refs = {e["ref"] for e in decision["frozen_snapshot"]["evidence"]}
        self.assertIn("HC-5", refs)
        self.assertIn("INV-93", refs)
        for evidence in decision["frozen_snapshot"]["evidence"]:
            self.assertEqual(len(evidence["hash"]), 16)

    def test_applicant_view_lists_missing_materials_and_payment(self):
        view = self.wb.applicant_view("AP-82")
        missing = {m["code"] for m in view["missing_materials"]}
        self.assertIn("property-fee-invoice", missing)
        self.assertIsNone(view["payment"])
        paid = self.wb.applicant_view("AP-81")
        self.assertEqual(paid["payment"]["state"], "paid")
        self.assertEqual(paid["paid_amount_yuan"], 38000)

    def test_events_audit_trail_recorded(self):
        self.wb.decide("AP-83", "approved", by="clerk-1")
        types = [e["type"] for e in self.wb.store.events()]
        self.assertIn("decision.recorded", types)
        self.assertIn("decision.backfilled", types)  # AP-81 补录留痕
