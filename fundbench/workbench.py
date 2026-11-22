"""工作台门面：摄入、评估、审批、付款、撤回与重审队列。

不变式（业务红线）：
1. 已付款申请为终态，任何新决定/撤回/重入都被拒绝，决定快照永久保留当时依据
2. 材料更正或撤回只把「未结申请」放入重审队列，不动已付款项目
3. 家庭已用额度只来自有效决定；一件材料失效不会自动释放其他成员已占用的额度，
   必须由审核员对该申请作出新决定后台账才重算
4. 政策调整只影响尚未作出最终决定的申请
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .engine import EvaluationEngine, evidence_hash
from .ledger import HouseholdLedger
from .models import (
    DOC_LABELS,
    USE_LABELS,
    WorkbenchError,
)
from .registry import ApplicationView, DecisionView, Registry
from .store import Envelope, Store, now_iso


@dataclass
class IngestReport:
    added: list[str]
    skipped: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"added": self.added, "skipped": self.skipped, "notes": self.notes}


class Workbench:
    def __init__(self, store: Store):
        self.store = store

    # ---------- 注册表 ----------
    def registry(self) -> Registry:
        return Registry.from_envelopes(self.store.envelopes())

    def engine(self) -> EvaluationEngine:
        return EvaluationEngine(self.registry())

    # ---------- 摄入 ----------
    def ingest_file(self, path: str | Path) -> IngestReport:
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            document = json.load(fh)
        raws = document.get("sources", document if isinstance(document, list) else [])
        if not isinstance(raws, list):
            raise WorkbenchError("资料文件需要包含 sources 数组")
        return self.ingest(raws)

    def ingest(self, raws: list[dict[str, Any]]) -> IngestReport:
        results = self.store.ingest_many(raws)
        report = IngestReport(added=[], skipped=[], notes=[])
        for env, is_new in results:
            (report.added if is_new else report.skipped).append(env.source_id)

        # 新付款信封可能需要为历史申请补录决定（如基线数据中的 AP-81）
        registry = self.registry()
        for env, is_new in results:
            if not is_new or env.record_kind != "payment":
                continue
            app_id = env.payload.get("application_id")
            if app_id not in registry.applications:
                report.notes.append(f"付款 {env.payload.get('payment_id')} 找不到申请 {app_id}")
                continue
            if registry.latest_decision(app_id) is None:
                note = self._backfill_decision_for_payment(registry, env)
                report.notes.append(note)
                registry = self.registry()  # 重建后续判断的视图
        return report

    def _backfill_decision_for_payment(self, registry: Registry,
                                       payment_env: Envelope) -> str:
        """付款先于决定到达：按付款当日生效政策补录一条冻结决定。"""
        app_id = payment_env.payload["application_id"]
        app = registry.applications[app_id]
        paid_on = date.fromisoformat(
            (payment_env.payload.get("paid_at") or payment_env.collected_at)[:10])
        rule = registry.policy_book.rule_effective_on(app.city, paid_on)
        if rule is None:
            raise WorkbenchError(f"申请 {app_id} 付款日 {paid_on} 无可用政策版本，无法补录决定")
        amount = payment_env.payload["amount_yuan"]
        decision_id = f"D-{app_id.split('-')[-1]}-backfill"
        snapshot = self._build_snapshot(registry, app, rule,
                                        decided_at=payment_env.collected_at,
                                        as_at=payment_env.collected_at)
        envelope = {
            "source_id": f"decision-backfill-{app_id.lower()}",
            "record_kind": "decision",
            "collected_at": payment_env.collected_at,
            "payload": {
                "decision_id": decision_id,
                "application_id": app_id,
                "outcome": "approved",
                "decided_at": payment_env.collected_at,
                "policy_city": rule.city,
                "policy_revision": rule.revision,
                "granted_amount_yuan": amount,
                "by": "system-backfill",
                "note": "付款回执先于审批记录到达，按付款当日生效政策补录",
                "snapshot": snapshot,
            },
        }
        self.store.ingest_envelope(envelope)
        self.store.append_event("decision.backfilled",
                                {"application_id": app_id, "decision_id": decision_id})
        return f"申请 {app_id} 已按第 {rule.revision} 版政策补录批准决定 {decision_id}"

    # ---------- 评估 ----------
    def evaluate(self, application_id: str, as_of: date | None = None):
        return self.engine().evaluate(application_id, as_of)

    # ---------- 审批 ----------
    def decide(self, application_id: str, outcome: str, *,
               by: str, note: str = "", as_of: date | None = None,
               force: bool = False) -> dict[str, Any]:
        if outcome not in ("approved", "rejected"):
            raise WorkbenchError("决定结果只能是 approved 或 rejected")
        registry = self.registry()
        if application_id not in registry.applications:
            raise WorkbenchError(f"申请 {application_id} 不存在")
        state = registry.application_state(application_id)
        if state == "paid":
            raise WorkbenchError(f"申请 {application_id} 已付款，终态项目不得再次提交或改判")
        if state in ("rejected", "withdrawn"):
            raise WorkbenchError(f"申请 {application_id} 已处于终态 {state}，如需办理请重新申请")

        evaluation = self.engine().evaluate(application_id, as_of)
        if outcome == "approved" and evaluation.blockers and not force:
            raise WorkbenchError(
                f"申请 {application_id} 存在 {len(evaluation.blockers)} 项硬性问题，"
                "不能批准；可先拒绝或使用 --force 记录特批")
        granted = evaluation.grantable_amount if outcome == "approved" else 0
        if outcome == "approved" and granted <= 0 and not force:
            raise WorkbenchError(f"申请 {application_id} 可批准金额为 0，无法批准")

        decided_at = now_iso()
        as_of = as_of or date.today()
        rule = registry.policy_book.rule_effective_on(
            registry.applications[application_id].city, as_of)
        if rule is None:
            raise WorkbenchError("当前日期无可用政策版本")
        seq = len(registry.decisions.get(application_id, [])) + 1
        decision_id = f"D-{application_id.split('-')[-1]}-{seq}"
        snapshot = self._build_snapshot(registry,
                                       registry.applications[application_id],
                                       rule, decided_at=decided_at,
                                       evaluation=evaluation)
        envelope = {
            "source_id": f"decision-{application_id.lower()}-{seq}",
            "record_kind": "decision",
            "collected_at": decided_at,
            "payload": {
                "decision_id": decision_id,
                "application_id": application_id,
                "outcome": outcome,
                "decided_at": decided_at,
                "policy_city": rule.city,
                "policy_revision": rule.revision,
                "granted_amount_yuan": granted,
                "by": by,
                "note": note,
                "snapshot": snapshot,
            },
        }
        self.store.ingest_envelope(envelope)
        self.store.append_event("decision.recorded", {
            "application_id": application_id,
            "decision_id": decision_id,
            "outcome": outcome,
            "granted_amount_yuan": granted,
        })
        return {"decision_id": decision_id, "application_id": application_id,
                "outcome": outcome, "granted_amount_yuan": granted,
                "blockers": [f.code for f in evaluation.blockers],
                "warnings": [f.code for f in evaluation.warnings]}

    def _build_snapshot(self, registry: Registry, app: ApplicationView,
                        rule, *, decided_at: str, evaluation=None,
                        as_at: str | None = None) -> dict[str, Any]:
        """冻结决定依据：政策版本、申请信封哈希、证据清单与评估摘要。

        as_at 用于历史补录：只冻结该时点之前已收到的证据版本，
        保证后来的材料更正/撤回不会污染已付款项目的历史依据。
        """
        evidence = []
        cert = registry.certificate_for(app, as_at=as_at)
        if cert:
            evidence.append({
                "kind": "housing-certificate", "ref": cert.cert_id,
                "revision": cert.revision, "state": cert.state,
                "hash": evidence_hash(cert.raw),
            })
        from .models import USE_INVOICE_DOC
        invoice_doc = USE_INVOICE_DOC.get(app.use)
        for claim in registry.receipts_for(app.application_id, as_at=as_at):
            evidence.append({
                "kind": claim.doc_type or invoice_doc or "receipt", "ref": claim.receipt_id,
                "revision": claim.revision, "state": claim.state,
                "amount_yuan": claim.amount_yuan, "date": claim.date,
                "hash": evidence_hash(claim.raw),
            })
        snapshot = {
            "decided_at": decided_at,
            "policy": rule.to_summary(),
            "application": {"payload": dict(app.raw),
                            "hash": evidence_hash(dict(app.raw))},
            "evidence": evidence,
        }
        if evaluation is not None:
            snapshot["evaluation"] = {
                "findings": [f.to_dict() for f in evaluation.findings],
                "receipt_sum": evaluation.receipt_sum,
                "cap_used": evaluation.cap_used,
                "cap_remaining": evaluation.cap_remaining,
                "account_balance": evaluation.account_balance,
                "grantable_amount": evaluation.grantable_amount,
            }
        return snapshot

    # ---------- 付款 ----------
    def register_payment(self, application_id: str, payment_id: str,
                         amount: int, *, paid_at: str | None = None) -> dict[str, Any]:
        registry = self.registry()
        if application_id not in registry.applications:
            raise WorkbenchError(f"申请 {application_id} 不存在")
        if registry.has_paid(application_id):
            raise WorkbenchError(f"申请 {application_id} 已付款，不能重复付款")
        decision = registry.latest_decision(application_id)
        if decision is None or decision.outcome != "approved":
            raise WorkbenchError(f"申请 {application_id} 尚未批准，不能登记付款")
        if amount != decision.granted_amount_yuan:
            raise WorkbenchError(
                f"付款金额 {amount} 与批准金额 {decision.granted_amount_yuan} 不一致")
        paid_at = paid_at or now_iso()
        envelope = {
            "source_id": f"payment-manual-{application_id.lower()}",
            "record_kind": "payment",
            "collected_at": paid_at,
            "payload": {"payment_id": payment_id, "application_id": application_id,
                        "amount_yuan": amount, "state": "paid", "paid_at": paid_at},
        }
        self.store.ingest_envelope(envelope)
        self.store.append_event("payment.registered",
                                {"application_id": application_id,
                                 "payment_id": payment_id, "amount_yuan": amount})
        return {"application_id": application_id, "payment_id": payment_id,
                "amount_yuan": amount, "state": "paid"}

    # ---------- 撤回申请 ----------
    def withdraw_application(self, application_id: str, *, by: str,
                             reason: str = "") -> dict[str, Any]:
        registry = self.registry()
        if application_id not in registry.applications:
            raise WorkbenchError(f"申请 {application_id} 不存在")
        if registry.has_paid(application_id):
            raise WorkbenchError(f"申请 {application_id} 已付款，不能撤回")
        state = registry.application_state(application_id)
        if state in ("rejected", "withdrawn"):
            raise WorkbenchError(f"申请 {application_id} 已是终态 {state}")
        seq = len(registry.decisions.get(application_id, [])) + 1
        envelope = {
            "source_id": f"withdraw-{application_id.lower()}-{seq}",
            "record_kind": "decision",
            "collected_at": now_iso(),
            "payload": {
                "decision_id": f"D-{application_id.split('-')[-1]}-W{seq}",
                "application_id": application_id,
                "outcome": "withdrawn",
                "decided_at": now_iso(),
                "policy_city": registry.applications[application_id].city,
                "policy_revision": (
                    registry.latest_decision(application_id).policy_revision
                    if registry.latest_decision(application_id) else 0),
                "granted_amount_yuan": 0,
                "by": by,
                "note": reason or "申请人撤回",
            },
        }
        self.store.ingest_envelope(envelope)
        self.store.append_event("application.withdrawn",
                                {"application_id": application_id, "by": by})
        return {"application_id": application_id, "state": "withdrawn"}

    # ---------- 重审队列（派生视图） ----------
    def review_queue(self) -> dict[str, list[dict[str, Any]]]:
        """返回需要重审的未结申请，按触发原因分组。

        policy_driven: 提交后政策版本发生变化且尚无决定
        material_driven: 关联票据/证明存在更正或撤回（终态申请一律不进队列）
        """
        registry = self.registry()
        engine = EvaluationEngine(registry)
        policy_queue: list[dict[str, Any]] = []
        material_queue: list[dict[str, Any]] = []

        for app in registry.ordered_applications():
            state = registry.application_state(app.application_id)
            if state in ("paid", "rejected", "withdrawn"):
                continue  # 红线：终态项目不重审、不重复提交

            reasons: list[str] = []
            material_trigger = False

            # 政策驱动
            submitted = date.fromisoformat(app.submitted_date)
            rule_then = registry.policy_book.rule_effective_on(app.city, submitted)
            rule_now = registry.policy_book.rule_effective_on(app.city, date.today())
            if rule_then and rule_now and rule_then.revision != rule_now.revision:
                reasons.append(
                    f"政策第 {rule_then.revision} 版 → 第 {rule_now.revision} 版")
                policy_queue.append({
                    "application_id": app.application_id,
                    "state": state,
                    "reasons": reasons,
                    "submitted_policy_revision": rule_then.revision,
                    "current_policy_revision": rule_now.revision,
                })

            # 材料驱动：票据存在多版本（更正）或被撤回
            for claim in registry.receipts_for(app.application_id):
                if claim.withdrawn:
                    material_trigger = True
                    reasons.append(f"票据 {claim.receipt_id} 已撤回")
                elif len(claim.history) > 1:
                    material_trigger = True
                    reasons.append(
                        f"票据 {claim.receipt_id} 已更正至第 {claim.revision} 版"
                        f"（金额 {claim.amount_yuan} 元）")
            cert = registry.certificate_for(app)
            if cert:
                if cert.withdrawn:
                    material_trigger = True
                    reasons.append(f"住房证明 {cert.cert_id} 已撤回")
                elif len(cert.history) > 1:
                    material_trigger = True
                    reasons.append(f"住房证明 {cert.cert_id} 已更正至第 {cert.revision} 版")

            if material_trigger:
                evaluation = engine.evaluate(app.application_id)
                material_queue.append({
                    "application_id": app.application_id,
                    "state": state,
                    "reasons": reasons,
                    "blockers": [f.code for f in evaluation.blockers],
                    "grantable_amount": evaluation.grantable_amount,
                })

        return {"policy_driven": policy_queue, "material_driven": material_queue}

    # ---------- 台账 ----------
    def ledger(self) -> HouseholdLedger:
        return HouseholdLedger(self.registry())

    # ---------- 追踪视图（审核员） ----------
    def trace_view(self, application_id: str) -> dict[str, Any]:
        registry = self.registry()
        if application_id not in registry.applications:
            raise WorkbenchError(f"申请 {application_id} 不存在")
        app = registry.applications[application_id]
        decision = registry.latest_decision(application_id)
        payments = registry.payments.get(application_id, [])
        evaluation = self.engine().evaluate(application_id)
        return {
            "application": {
                "application_id": app.application_id,
                "city": app.city,
                "use": app.use,
                "use_label": USE_LABELS.get(app.use, app.use),
                "amount_yuan": app.amount_yuan,
                "submitted_date": app.submitted_date,
                "state": registry.application_state(application_id),
                "member_ref": app.member_ref,
                "household_ref": app.household_ref,
            },
            "evaluation": evaluation.to_dict(),
            "decision": None if decision is None else {
                "decision_id": decision.decision_id,
                "outcome": decision.outcome,
                "decided_at": decision.decided_at,
                "by": decision.by,
                "note": decision.note,
                "granted_amount_yuan": decision.granted_amount_yuan,
                "policy_city": decision.policy_city,
                "policy_revision": decision.policy_revision,
                "frozen_snapshot": decision.snapshot,
            },
            "payments": [
                {"payment_id": p.payment_id, "amount_yuan": p.amount_yuan,
                 "state": p.state, "paid_at": p.paid_at}
                for p in payments
            ],
            "receipt_history": [
                {"receipt_id": c.receipt_id, "revision": c.revision,
                 "state": c.state, "amount_yuan": c.amount_yuan,
                 "date": c.date, "source_id": c.source_id,
                 "history": list(c.history)}
                for c in registry.receipts_for(application_id)
            ],
        }

    # ---------- 申请人视图 ----------
    def applicant_view(self, application_id: str) -> dict[str, Any]:
        registry = self.registry()
        if application_id not in registry.applications:
            raise WorkbenchError(f"申请 {application_id} 不存在")
        app = registry.applications[application_id]
        state = registry.application_state(application_id)
        evaluation = self.engine().evaluate(application_id)
        member = registry.members.get(app.member_ref)
        missing = [
            {"code": doc, "label": DOC_LABELS.get(doc, doc)}
            for doc in evaluation.missing_materials
        ]
        blockers = [{"code": f.code, "message": f.message}
                    for f in evaluation.blockers]
        payments = registry.payments.get(application_id, [])
        paid = sum(p.amount_yuan for p in payments if p.state == "paid")
        decision = registry.latest_decision(application_id)

        state_label = {
            "submitted": "待审核",
            "approved": "已批准，等待付款",
            "paid": "已到账",
            "rejected": "已拒绝",
            "withdrawn": "已撤回",
        }.get(state, state)

        return {
            "application_id": app.application_id,
            "applicant": member.name if member else app.member_ref,
            "city": app.city,
            "use_label": USE_LABELS.get(app.use, app.use),
            "requested_amount_yuan": app.amount_yuan,
            "state": state,
            "state_label": state_label,
            "submitted_date": app.submitted_date,
            "missing_materials": missing,
            "issues": blockers,
            "warnings": [{"code": f.code, "message": f.message}
                         for f in evaluation.warnings],
            "granted_amount_yuan": decision.granted_amount_yuan if decision else None,
            "paid_amount_yuan": paid,
            "payment": None if not payments else {
                "payment_id": payments[-1].payment_id,
                "amount_yuan": payments[-1].amount_yuan,
                "paid_at": payments[-1].paid_at,
                "state": payments[-1].state,
            },
        }
