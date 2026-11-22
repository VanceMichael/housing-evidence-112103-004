"""规则引擎测试：城市规则、材料日期/金额、政策适用范围。"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fundbench.models import WorkbenchError  # noqa: E402
from fundbench.policy import normalize_policy  # noqa: E402

from tests._support import WorkbenchTestCase  # noqa: E402


class PolicySelectionTests(WorkbenchTestCase):
    def test_policy_book_picks_effective_version_by_date(self):
        book = self.wb.registry().policy_book
        # CITY-A：v3 自 2026-01-01，v4 自 2026-09-20
        self.assertEqual(
            book.rule_effective_on("CITY-A", date(2026, 8, 1)).revision, 3)
        self.assertEqual(
            book.rule_effective_on("CITY-A", date(2026, 9, 20)).revision, 4)
        self.assertIsNone(book.rule_effective_on("CITY-A", date(2025, 12, 31)))

    def test_legacy_policy_payload_normalized(self):
        rule = normalize_policy({
            "city": "CITY-X", "revision": 1, "effective_from": "2026-01-01",
            "eligible_use": ["property-fee"],
            "household_property_fee_cap_yuan": 6000,
        })
        self.assertEqual(rule.cap_for("property-fee"), 6000)
        self.assertEqual(rule.receipt_window_months, 12)
        self.assertIn("property-fee-invoice", rule.materials_for("property-fee"))

    def test_policy_change_only_blocks_use_removed_by_new_policy(self):
        # AP-86 在 v3 期间提交（购房可申请），v4 取消购房 -> 新政下硬性拦截 + 变更提示
        codes = self.blocker_codes("AP-86")
        self.assertIn("USE_NOT_ELIGIBLE", codes)
        all_codes = self.finding_codes("AP-86")
        self.assertIn("POLICY_CHANGED", all_codes)

    def test_paid_application_keeps_old_basis_even_when_new_policy_ineligible(self):
        # AP-80 已付款：实时评估会提示新政不可申请，但状态仍是 paid，决定仍冻结 v3
        self.assertEqual(self.state("AP-80"), "paid")
        decision = self.wb.registry().latest_decision("AP-80")
        self.assertEqual(decision.policy_revision, 3)
        self.assertIn("USE_NOT_ELIGIBLE", self.blocker_codes("AP-80"))
        # 红线：终态申请不能重审、不能改判
        queue = self.wb.review_queue()
        queued = {item["application_id"]
                  for group in queue.values() for item in group}
        self.assertNotIn("AP-80", queued)
        with self.assertRaises(WorkbenchError):
            self.wb.decide("AP-80", "rejected", by="clerk-x")


class MaterialsAndAmountTests(WorkbenchTestCase):
    def test_missing_invoice_and_missing_amount(self):
        # AP-82 没有任何发票信封
        ev = self.wb.evaluate("AP-82")
        self.assertIn("property-fee-invoice", ev.missing_materials)
        self.assertIn("MATERIAL_MISSING", [f.code for f in ev.blockers])
        self.assertEqual(ev.grantable_amount, 0)

    def test_duplicate_receipt_blocks_both_claimants(self):
        # INV-92 同时挂在 AP-81（已付款）与 AP-84 名下
        self.assertIn("RECEIPT_DUPLICATE", self.blocker_codes("AP-84"))
        self.assertIn("RECEIPT_DUPLICATE", self.blocker_codes("AP-81"))
        # AP-84 还因对方撤回看到当前票据已撤回
        self.assertIn("RECEIPT_WITHDRAWN", self.blocker_codes("AP-84"))

    def test_withdrawn_receipt_blocks_and_does_not_double_report_amount(self):
        codes = self.finding_codes("AP-85")
        self.assertIn("RECEIPT_WITHDRAWN", codes)
        self.assertNotIn("RECEIPT_AMOUNT_LOW", codes)  # 撤回已足够，不叠加金额问题

    def test_receipt_window_enforced_by_city(self):
        # CITY-B 窗口 6 个月：INV-97 为 2026-03-01，评估日 2026-11-22 已超窗
        self.assertIn("RECEIPT_OUT_OF_WINDOW", self.blocker_codes("AP-87"))
        # 同一城市窗口内的票据不触发
        self.assertNotIn("RECEIPT_OUT_OF_WINDOW",
                         self.blocker_codes("AP-88"))

    def test_window_evaluation_respects_as_of_date(self):
        # 把评估日退回到 2026-09-01，INV-97 尚在 6 个月窗口内
        ev = self.wb.evaluate("AP-87", as_of=date(2026, 9, 1))
        self.assertNotIn("RECEIPT_OUT_OF_WINDOW",
                         [f.code for f in ev.blockers])

    def test_corrected_receipt_reduces_grantable_amount(self):
        ev = self.wb.evaluate("AP-89")
        self.assertEqual(ev.receipt_sum, 2800)
        self.assertEqual(ev.grantable_amount, 2800)
        self.assertIn("RECEIPT_AMOUNT_LOW", [f.code for f in ev.blockers])

    def test_housing_mismatch_detected(self):
        # AP-90 引用了别的家庭/产权人的住房证明 HC-9
        codes = self.blocker_codes("AP-90")
        self.assertIn("HOUSING_MISMATCH", codes)

    def test_household_cap_partial_approval(self):
        # AP-83 申请 6500，CITY-A 物业费家庭上限 6000 -> 可批 6000，仅提示不拦截
        ev = self.wb.evaluate("AP-83")
        self.assertTrue(ev.ready)
        self.assertEqual(ev.cap_remaining, 6000)
        self.assertEqual(ev.grantable_amount, 6000)
        self.assertIn("CAP_PARTIAL", [f.code for f in ev.warnings])

    def test_personal_balance_is_separate_from_household_cap(self):
        # AP-88：家庭物业费上限 3000，但成员 M-SYN-6 余额仅 2000
        ev = self.wb.evaluate("AP-88")
        self.assertEqual(ev.cap, 3000)
        self.assertEqual(ev.account_balance, 2000)
        self.assertEqual(ev.grantable_amount, 2000)
        self.assertIn("BALANCE_PARTIAL", [f.code for f in ev.warnings])

    def test_policy_book_exposes_city_specific_rules(self):
        book = self.wb.registry().policy_book
        b = book.latest("CITY-B")
        self.assertEqual(b.receipt_window_months, 6)
        self.assertEqual(b.cap_for("property-fee"), 3000)
        self.assertNotIn("home-repair", b.eligible_use)
        # CITY-B 购房要求购房合同，CITY-A 不要求
        self.assertIn("purchase-contract", b.materials_for("home-purchase"))
