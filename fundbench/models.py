"""领域模型与枚举。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 用途中文标签
USE_LABELS = {
    "home-purchase": "购房",
    "home-repair": "装修",
    "property-fee": "物业费",
}

# 材料中文标签
DOC_LABELS = {
    "housing-certificate": "住房证明",
    "purchase-contract": "购房合同",
    "purchase-invoice": "购房发票",
    "repair-invoice": "装修发票",
    "property-fee-invoice": "物业费发票",
}

# 各用途默认所需材料（政策可覆盖）
DEFAULT_REQUIRED_MATERIALS = {
    "home-purchase": ["housing-certificate", "purchase-invoice"],
    "home-repair": ["housing-certificate", "repair-invoice"],
    "property-fee": ["housing-certificate", "property-fee-invoice"],
}

# 用途对应的发票材料类型
USE_INVOICE_DOC = {
    "home-purchase": "purchase-invoice",
    "home-repair": "repair-invoice",
    "property-fee": "property-fee-invoice",
}

# 申请终态
TERMINAL_STATES = {"paid", "rejected", "withdrawn"}

# 发现项：code -> (级别, 中文说明模板)
FINDING_CATALOG = {
    "USE_NOT_ELIGIBLE": ("blocker", "当前生效政策不允许以「{use}」用途申请提取"),
    "MATERIAL_MISSING": ("blocker", "缺少必备材料：{doc}"),
    "RECEIPT_WITHDRAWN": ("blocker", "票据 {receipt_id} 已被撤回，不能作为办理依据"),
    "CERT_WITHDRAWN": ("blocker", "住房证明 {cert_id} 已被撤回，不能作为办理依据"),
    "RECEIPT_DUPLICATE": ("blocker", "票据 {receipt_id} 已被申请 {other_id} 同时提交，疑似重复报销"),
    "RECEIPT_OUT_OF_WINDOW": ("blocker", "票据 {receipt_id} 日期 {date} 超出政策规定的 {months} 个月受理窗口"),
    "RECEIPT_AMOUNT_LOW": ("blocker", "有效票据金额合计 {actual} 元，低于申请金额 {expected} 元"),
    "HOUSING_MISMATCH": ("blocker", "住房证明与申请人/家庭/房屋不匹配：{detail}"),
    "POLICY_CHANGED": ("warning", "政策已由第 {old_rev} 版调整为第 {new_rev} 版，需按新政重审"),
    "CAP_PARTIAL": ("warning", "家庭年度上限剩余 {remaining} 元，低于申请金额，仅可批准 {grant} 元"),
    "BALANCE_PARTIAL": ("warning", "个人账户余额仅 {balance} 元，低于申请金额，仅可批准 {grant} 元"),
}

RECORD_KINDS = {
    "policy", "application", "receipt", "payment", "housing_certificate",
    "member", "household", "decision",
}


@dataclass
class Finding:
    code: str
    level: str  # blocker | warning
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "level": self.level, "message": self.message,
                "context": self.context}


@dataclass
class Evaluation:
    application_id: str
    as_of: str
    policy_at_submission: dict[str, Any] | None
    policy_at_decision: dict[str, Any] | None
    findings: list[Finding]
    missing_materials: list[str]
    attached_evidence: list[dict[str, Any]]
    requested_amount: int
    receipt_sum: int
    cap: int | None
    cap_used: int
    cap_remaining: int | None
    account_balance: int | None
    grantable_amount: int

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "blocker"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "as_of": self.as_of,
            "ready": self.ready,
            "policy_at_submission": self.policy_at_submission,
            "policy_at_decision": self.policy_at_decision,
            "findings": [f.to_dict() for f in self.findings],
            "missing_materials": self.missing_materials,
            "attached_evidence": self.attached_evidence,
            "requested_amount": self.requested_amount,
            "receipt_sum": self.receipt_sum,
            "cap": self.cap,
            "cap_used": self.cap_used,
            "cap_remaining": self.cap_remaining,
            "account_balance": self.account_balance,
            "grantable_amount": self.grantable_amount,
        }


class WorkbenchError(Exception):
    """业务规则错误。"""
