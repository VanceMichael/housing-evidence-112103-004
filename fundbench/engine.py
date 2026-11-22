"""申请评估引擎：把城市政策、材料、票据与家庭台账组合成可解释的结论。

评估是纯函数式的：同一份注册表 + 同一个 as_of 日期，结论可重复计算。
blocker（硬性问题）存在时不能批准；warning 只提示并影响可批准金额。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from .ledger import HouseholdLedger
from .models import (
    DOC_LABELS,
    FINDING_CATALOG,
    USE_INVOICE_DOC,
    Evaluation,
    Finding,
)
from .policy import PolicyRule
from .registry import ApplicationView, Registry


def _finding(code: str, **context: Any) -> Finding:
    level, template = FINDING_CATALOG[code]
    return Finding(code=code, level=level, message=template.format(**context), context=context)


def evidence_hash(payload: dict[str, Any]) -> str:
    """证据完整性哈希：规范化 JSON 后取 SHA-256 前 16 位。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    # 月末兜底（如 3 月 31 日 + 6 个月 -> 9 月 30 日）
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


class EvaluationEngine:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.ledger = HouseholdLedger(registry)

    def evaluate(self, application_id: str, as_of: date | None = None) -> Evaluation:
        app = self.registry.applications[application_id]
        as_of = as_of or date.today()
        submitted = date.fromisoformat(app.submitted_date)
        year = submitted.year

        rule_now = self.registry.policy_book.rule_effective_on(app.city, as_of)
        rule_then = self.registry.policy_book.rule_effective_on(app.city, submitted)

        findings: list[Finding] = []
        evidence_items: list[dict[str, Any]] = []
        missing: list[str] = []

        # ---- 1. 用途在当前政策下是否可申请 ----
        if rule_now is None:
            findings.append(_finding("USE_NOT_ELIGIBLE", use=app.use))
            effective_rule = rule_then
        else:
            effective_rule = rule_now
            if app.use not in rule_now.eligible_use:
                findings.append(_finding("USE_NOT_ELIGIBLE", use=app.use))

        # 政策调整提示（只提示，不直接构成硬性问题；用途取消已由上一条拦截）
        if (rule_then is not None and rule_now is not None
                and rule_then.revision != rule_now.revision):
            findings.append(_finding(
                "POLICY_CHANGED",
                old_rev=rule_then.revision, new_rev=rule_now.revision,
            ))

        # ---- 2. 材料齐备性 ----
        required = effective_rule.materials_for(app.use) if effective_rule else ()
        self._check_certificate(app, required, findings, missing, evidence_items)
        self._check_receipts(app, as_of, effective_rule, required,
                             findings, missing, evidence_items)

        # ---- 3. 金额与额度 ----
        active_receipts = [c for c in self.registry.receipts_for(application_id)
                           if not c.withdrawn]
        receipt_sum = sum(c.amount_yuan for c in active_receipts)
        # 票据被撤回时 RECEIPT_WITHDRAWN 已构成硬性问题，不再叠加金额不足，
        # 避免同一份失效材料产生重复结论
        has_invoice = bool(self.registry.receipts_for(application_id))
        if receipt_sum < app.amount_yuan and not (
                has_invoice and not active_receipts):
            findings.append(_finding(
                "RECEIPT_AMOUNT_LOW",
                actual=receipt_sum, expected=app.amount_yuan,
            ))

        member = self.registry.members.get(app.member_ref)
        balance = member.account_balance_yuan if member else None

        cap = effective_rule.cap_for(app.use) if effective_rule else None
        cap_used = self.ledger.used(app.household_ref, year, app.use,
                                    exclude_application=application_id)
        cap_remaining = None if cap is None else max(0, cap - cap_used)

        if cap_remaining is not None and cap_remaining < app.amount_yuan:
            findings.append(_finding(
                "CAP_PARTIAL",
                remaining=cap_remaining, grant=max(0, cap_remaining),
            ))
        if balance is not None and balance < app.amount_yuan:
            findings.append(_finding(
                "BALANCE_PARTIAL",
                balance=balance, grant=max(0, balance),
            ))

        # 可批准金额：申请额、有效票据额、家庭剩余额度、个人账户余额的最小值
        grantable = app.amount_yuan
        grantable = min(grantable, receipt_sum)
        if cap_remaining is not None:
            grantable = min(grantable, cap_remaining)
        if balance is not None:
            grantable = min(grantable, balance)
        grantable = max(0, grantable)

        return Evaluation(
            application_id=application_id,
            as_of=as_of.isoformat(),
            policy_at_submission=rule_then.to_summary() if rule_then else None,
            policy_at_decision=rule_now.to_summary() if rule_now else None,
            findings=findings,
            missing_materials=missing,
            attached_evidence=evidence_items,
            requested_amount=app.amount_yuan,
            receipt_sum=receipt_sum,
            cap=cap,
            cap_used=cap_used,
            cap_remaining=cap_remaining,
            account_balance=balance,
            grantable_amount=grantable,
        )

    # ---------- 材料检查 ----------
    def _check_certificate(self, app: ApplicationView, required: tuple[str, ...],
                           findings: list[Finding], missing: list[str],
                           evidence_items: list[dict[str, Any]]) -> None:
        if "housing-certificate" not in required:
            return
        cert = self.registry.certificate_for(app)
        if cert is None:
            findings.append(_finding("MATERIAL_MISSING", doc="housing-certificate"))
            missing.append("housing-certificate")
            return
        if cert.withdrawn:
            findings.append(_finding("CERT_WITHDRAWN", cert_id=cert.cert_id))
        # 归属校验：城市、家庭、申请人、房屋四要素
        problems = []
        if cert.city and app.city and cert.city != app.city:
            problems.append(f"城市 {cert.city}≠{app.city}")
        if cert.household_ref != app.household_ref:
            problems.append(f"家庭 {cert.household_ref}≠{app.household_ref}")
        if app.member_ref not in cert.owner_member_refs:
            problems.append(f"申请人 {app.member_ref} 不在产权人名单 {list(cert.owner_member_refs)}")
        if app.housing_ref and cert.housing_id != app.housing_ref:
            problems.append(f"房屋 {cert.housing_id}≠{app.housing_ref}")
        if problems:
            findings.append(_finding("HOUSING_MISMATCH", detail="；".join(problems)))
        evidence_items.append({
            "kind": "housing-certificate",
            "ref": cert.cert_id,
            "state": cert.state,
            "revision": cert.revision,
            "hash": evidence_hash(cert.raw),
        })

    def _check_receipts(self, app: ApplicationView, as_of: date,
                        rule: PolicyRule | None, required: tuple[str, ...],
                        findings: list[Finding], missing: list[dict],
                        evidence_items: list[dict[str, Any]]) -> None:
        invoice_doc = USE_INVOICE_DOC.get(app.use)
        claims = self.registry.receipts_for(app.application_id)
        has_invoice = bool(claims)
        if invoice_doc and invoice_doc in required and not has_invoice:
            findings.append(_finding("MATERIAL_MISSING", doc=invoice_doc))
            missing.append(invoice_doc)

        window_months = rule.receipt_window_months if rule else 12
        for claim in claims:
            item = {
                "kind": claim.doc_type or invoice_doc or "receipt",
                "ref": claim.receipt_id,
                "state": claim.state,
                "revision": claim.revision,
                "amount_yuan": claim.amount_yuan,
                "date": claim.date,
                "hash": evidence_hash(claim.raw),
            }
            evidence_items.append(item)

            # 同一张发票在所有申请下的引用：用于识别重复提交与跨申请撤回
            all_claims = self.registry.all_claims_of_receipt(claim.receipt_id)
            sibling_claims = [c for c in all_claims
                              if c.application_id != app.application_id]
            if sibling_claims:
                other_ids = sorted({c.application_id for c in sibling_claims})
                findings.append(_finding(
                    "RECEIPT_DUPLICATE",
                    receipt_id=claim.receipt_id, other_id="、".join(other_ids),
                ))

            # 撤回既可能针对本申请的引用，也可能针对同票的其他引用
            withdrawn_claims = [c for c in all_claims if c.withdrawn]
            if withdrawn_claims:
                findings.append(_finding("RECEIPT_WITHDRAWN",
                                         receipt_id=claim.receipt_id))
                continue

            # 受理窗口：票据日期与评估日之间不得超过政策规定月数
            try:
                receipt_date = date.fromisoformat(claim.date)
                if as_of > _add_months(receipt_date, window_months):
                    findings.append(_finding(
                        "RECEIPT_OUT_OF_WINDOW",
                        receipt_id=claim.receipt_id, date=claim.date,
                        months=window_months,
                    ))
            except ValueError:
                # 日期格式异常按缺材料处理，避免脏数据静默通过
                findings.append(_finding("MATERIAL_MISSING",
                                         doc=f"{claim.receipt_id}(日期异常)"))
                missing.append(f"{claim.receipt_id}(日期异常)")
