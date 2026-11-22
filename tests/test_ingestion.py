"""摄入层测试：信封幂等、付款补录、时点快照。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fundbench.registry import Registry  # noqa: E402
from fundbench.store import Store  # noqa: E402

from tests._support import FIXTURE, WorkbenchTestCase  # noqa: E402


class IngestionTests(WorkbenchTestCase):
    def test_fixture_ingested_and_idempotent(self):
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(self.report.added), len(raw["sources"]))
        # 再次摄入：全部跳过，不产生重复
        again = self.wb.ingest_file(FIXTURE)
        self.assertEqual(again.added, [])
        self.assertEqual(len(again.skipped), len(raw["sources"]))
        registry = self.wb.registry()
        self.assertEqual(len(registry.applications), 11)

    def test_invalid_envelope_rejected(self):
        with self.assertRaises(ValueError):
            self.wb.ingest([{"source_id": "x", "record_kind": "unknown",
                             "collected_at": "2026-01-01T00:00:00+08:00",
                             "payload": {}}])
        with self.assertRaises(ValueError):
            self.wb.ingest([{"source_id": "", "record_kind": "policy",
                             "collected_at": "2026-01-01T00:00:00+08:00",
                             "payload": {}}])

    def test_payment_without_decision_backfills_with_point_in_time_evidence(self):
        registry = self.wb.registry()
        # AP-81 只有付款信封、没有显式决定信封 -> 自动补录
        decision = registry.latest_decision("AP-81")
        self.assertIsNotNone(decision)
        self.assertEqual(decision.outcome, "approved")
        self.assertEqual(decision.granted_amount_yuan, 38000)
        self.assertEqual(decision.by, "system-backfill")
        # 补录依据是付款日（2026-10-11）生效的第 4 版政策
        self.assertEqual(decision.policy_revision, 4)
        # 关键：发票撤回信封 11-15 才到达，补录快照必须冻结 10-11 时点的 rev.1 有效票据
        receipt_evidence = [e for e in decision.snapshot["evidence"]
                            if e["ref"] == "INV-92"]
        self.assertEqual(len(receipt_evidence), 1)
        self.assertEqual(receipt_evidence[0]["revision"], 1)
        self.assertEqual(receipt_evidence[0]["state"], "active")

    def test_explicit_historical_decision_kept_verbatim(self):
        registry = self.wb.registry()
        decision = registry.latest_decision("AP-80")
        self.assertEqual(decision.decision_id, "D-80-1")
        self.assertEqual(decision.policy_revision, 3)  # 旧政第 3 版
        self.assertEqual(decision.granted_amount_yuan, 120000)
        self.assertTrue(registry.has_paid("AP-80"))

    def test_receipt_revisions_keep_full_history(self):
        registry = self.wb.registry()
        claim = registry.receipt_claims[("INV-98", "AP-89")]
        self.assertEqual(claim.revision, 2)
        self.assertEqual(claim.amount_yuan, 2800)
        self.assertEqual(len(claim.history), 2)  # 旧信封不删除
        withdrawn = registry.receipt_claims[("INV-95", "AP-85")]
        self.assertTrue(withdrawn.withdrawn)

    def test_rebuild_from_scratch_is_deterministic(self):
        from fundbench.ledger import HouseholdLedger
        envelopes = Store(self.store_path).envelopes()
        r1 = Registry.from_envelopes(envelopes)
        r2 = Registry.from_envelopes(envelopes)
        self.assertEqual(sorted(r1.applications), sorted(r2.applications))
        summary = lambda r: sorted(  # noqa: E731
            (e.application_id, e.state, e.granted_amount_yuan)
            for e in HouseholdLedger(r).all_entries())
        self.assertEqual(summary(r1), summary(r2))
