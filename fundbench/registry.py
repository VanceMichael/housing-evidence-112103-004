"""从信封派生的只读视图。

视图全部由原始信封重建，不在视图层保存可变状态：
- 发票/证明取同一业务键下 revision 最大的信封为当前版本，旧信封作为更正历史保留
- 决定取申请下最新一条（撤回/重审可产生新决定）
- 付款按支付状态聚合
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import TERMINAL_STATES
from .policy import PolicyBook, PolicyRule, normalize_policy
from .store import Envelope


@dataclass
class Member:
    member_id: str
    household_ref: str
    name: str
    account_balance_yuan: int
    raw: dict[str, Any]


@dataclass
class Household:
    household_ref: str
    city: str
    member_refs: tuple[str, ...]
    raw: dict[str, Any]


@dataclass
class Certificate:
    cert_id: str
    housing_id: str
    household_ref: str
    owner_member_refs: tuple[str, ...]
    city: str
    issue_date: str
    revision: int
    state: str
    history: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @property
    def withdrawn(self) -> bool:
        return self.state == "withdrawn"


@dataclass
class ReceiptClaim:
    """一张发票在某个申请下的当前版本（含全部历史信封）。"""
    receipt_id: str
    application_id: str
    issuer_ref: str
    doc_type: str
    date: str
    amount_yuan: int
    revision: int
    state: str
    source_id: str
    history: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @property
    def withdrawn(self) -> bool:
        return self.state == "withdrawn"


@dataclass
class ApplicationView:
    application_id: str
    member_ref: str
    household_ref: str
    housing_ref: str | None
    city: str
    use: str
    amount_yuan: int
    submitted_date: str
    submitted_at: str
    explicit_evidence: tuple[dict[str, Any], ...]
    raw: dict[str, Any]


@dataclass
class DecisionView:
    decision_id: str
    application_id: str
    outcome: str
    decided_at: str
    policy_city: str
    policy_revision: int
    granted_amount_yuan: int
    by: str
    note: str
    snapshot: dict[str, Any]
    source_id: str
    raw: dict[str, Any]


@dataclass
class PaymentView:
    payment_id: str
    application_id: str
    amount_yuan: int
    state: str
    paid_at: str | None
    source_id: str
    raw: dict[str, Any]


@dataclass
class Registry:
    members: dict[str, Member]
    households: dict[str, Household]
    certificates: dict[str, Certificate]
    applications: dict[str, ApplicationView]
    receipt_claims: dict[tuple[str, str], ReceiptClaim]
    decisions: dict[str, list[DecisionView]]
    payments: dict[str, list[PaymentView]]
    policy_book: PolicyBook
    envelopes: list[Envelope] = field(default_factory=list)

    # ---------- 构造 ----------
    @classmethod
    def from_envelopes(cls, envelopes: list[Envelope]) -> "Registry":
        members: dict[str, Member] = {}
        households: dict[str, Household] = {}
        cert_groups: dict[str, list[Envelope]] = {}
        app_map: dict[str, ApplicationView] = {}
        receipt_groups: dict[tuple[str, str], list[Envelope]] = {}
        decision_groups: dict[str, list[Envelope]] = {}
        payment_groups: dict[str, list[Envelope]] = {}
        policies: list[PolicyRule] = []

        for env in envelopes:
            kind = env.record_kind
            p = env.payload
            if kind == "member":
                members[p["member_id"]] = Member(
                    member_id=p["member_id"],
                    household_ref=p["household_ref"],
                    name=p.get("name", p["member_id"]),
                    account_balance_yuan=int(p.get("account_balance_yuan", 0)),
                    raw=dict(p),
                )
            elif kind == "household":
                households[p["household_ref"]] = Household(
                    household_ref=p["household_ref"],
                    city=p["city"],
                    member_refs=tuple(p.get("member_refs", ())),
                    raw=dict(p),
                )
            elif kind == "housing_certificate":
                cert_groups.setdefault(p["cert_id"], []).append(env)
            elif kind == "application":
                app_map[p["application_id"]] = cls._build_application(env)
            elif kind == "receipt":
                key = (p["receipt_id"], p["application_id"])
                receipt_groups.setdefault(key, []).append(env)
            elif kind == "decision":
                decision_groups.setdefault(p["application_id"], []).append(env)
            elif kind == "payment":
                payment_groups.setdefault(p["application_id"], []).append(env)
            elif kind == "policy":
                policies.append(normalize_policy(p))

        certificates = {cid: cls._build_certificate(group)
                        for cid, group in cert_groups.items()}
        receipt_claims = {key: cls._build_receipt(group)
                          for key, group in receipt_groups.items()}
        decisions = {aid: [cls._build_decision(e) for e in
                           sorted(group, key=lambda e: e.payload["decided_at"])]
                     for aid, group in decision_groups.items()}
        payments = {aid: [cls._build_payment(e) for e in
                          sorted(group, key=lambda e: e.collected_at)]
                    for aid, group in payment_groups.items()}

        return cls(
            members=members,
            households=households,
            certificates=certificates,
            applications=app_map,
            receipt_claims=receipt_claims,
            decisions=decisions,
            payments=payments,
            policy_book=PolicyBook(policies),
            envelopes=list(envelopes),
        )

    @staticmethod
    def _build_application(env: Envelope) -> ApplicationView:
        p = env.payload
        return ApplicationView(
            application_id=p["application_id"],
            member_ref=p["member_ref"],
            household_ref=p["household_ref"],
            housing_ref=p.get("housing_ref"),
            city=p["city"],
            use=p["use"],
            amount_yuan=int(p["amount_yuan"]),
            submitted_date=p.get("submitted_date", env.collected_at[:10]),
            submitted_at=env.collected_at,
            explicit_evidence=tuple(p.get("evidence", ())),
            raw=dict(p),
        )

    @staticmethod
    def _build_certificate(group: list[Envelope]) -> Certificate:
        ordered = sorted(group, key=lambda e: int(e.payload.get("revision", 1)))
        latest = ordered[-1].payload
        return Certificate(
            cert_id=latest["cert_id"],
            housing_id=latest["housing_id"],
            household_ref=latest["household_ref"],
            owner_member_refs=tuple(latest.get("owner_member_refs", ())),
            city=latest.get("city", ""),
            issue_date=latest.get("issue_date", ""),
            revision=int(latest.get("revision", 1)),
            state=latest.get("state", "active"),
            history=tuple(dict(e.payload, source_id=e.source_id,
                               collected_at=e.collected_at) for e in ordered),
            raw=dict(latest),
        )

    @staticmethod
    def _build_receipt(group: list[Envelope]) -> ReceiptClaim:
        ordered = sorted(group, key=lambda e: int(e.payload.get("revision", 1)))
        latest = ordered[-1].payload
        return ReceiptClaim(
            receipt_id=latest["receipt_id"],
            application_id=latest["application_id"],
            issuer_ref=latest.get("issuer_ref", ""),
            doc_type=latest.get("doc_type", ""),
            date=latest.get("date", ""),
            amount_yuan=int(latest.get("amount_yuan", 0)),
            revision=int(latest.get("revision", 1)),
            state=latest.get("state", "active"),
            source_id=ordered[-1].source_id,
            history=tuple(dict(e.payload, source_id=e.source_id,
                               collected_at=e.collected_at) for e in ordered),
            raw=dict(latest),
        )

    @staticmethod
    def _build_decision(env: Envelope) -> DecisionView:
        p = env.payload
        return DecisionView(
            decision_id=p["decision_id"],
            application_id=p["application_id"],
            outcome=p["outcome"],
            decided_at=p["decided_at"],
            policy_city=p["policy_city"],
            policy_revision=int(p["policy_revision"]),
            granted_amount_yuan=int(p.get("granted_amount_yuan", 0)),
            by=p.get("by", ""),
            note=p.get("note", ""),
            snapshot=p.get("snapshot", {}),
            source_id=env.source_id,
            raw=dict(p),
        )

    @staticmethod
    def _build_payment(env: Envelope) -> PaymentView:
        p = env.payload
        return PaymentView(
            payment_id=p["payment_id"],
            application_id=p["application_id"],
            amount_yuan=int(p.get("amount_yuan", 0)),
            state=p.get("state", "paid"),
            paid_at=p.get("paid_at", env.collected_at),
            source_id=env.source_id,
            raw=dict(p),
        )

    # ---------- 查询辅助 ----------
    def ordered_applications(self) -> list[ApplicationView]:
        return sorted(self.applications.values(),
                      key=lambda a: (a.submitted_at, a.application_id))

    def receipts_for(self, application_id: str,
                     as_at: str | None = None) -> list[ReceiptClaim]:
        claims = sorted((c for c in self.receipt_claims.values()
                         if c.application_id == application_id),
                        key=lambda c: c.receipt_id)
        if as_at is None:
            return claims
        # 时点视图：只保留 as_at 之前已收到的最新版本，模拟材料在该时点的真实状态
        result: list[ReceiptClaim] = []
        for claim in claims:
            past_envs = [e for e in self.envelopes
                         if e.record_kind == "receipt"
                         and e.payload.get("receipt_id") == claim.receipt_id
                         and e.payload.get("application_id") == application_id
                         and e.collected_at <= as_at]
            if not past_envs:
                continue
            result.append(self._build_receipt(past_envs))
        return result

    def certificate_for(self, app: ApplicationView,
                        as_at: str | None = None) -> Certificate | None:
        cert = self._certificate_ref_for(app)
        if cert is None or as_at is None:
            return cert
        past_envs = [e for e in self.envelopes
                     if e.record_kind == "housing_certificate"
                     and e.payload.get("cert_id") == cert.cert_id
                     and e.collected_at <= as_at]
        if not past_envs:
            return None
        return self._build_certificate(past_envs)

    def _certificate_ref_for(self, app: ApplicationView) -> Certificate | None:
        # 1) 申请显式 evidence 中引用的证明
        for ev in app.explicit_evidence:
            if ev.get("kind") == "housing-certificate" and ev.get("ref") in self.certificates:
                return self.certificates[ev["ref"]]
        # 2) 申请 housing_ref 对应的证明
        if app.housing_ref:
            for cert in self.certificates.values():
                if cert.housing_id == app.housing_ref:
                    return cert
        # 3) 家庭名下的证明（兼容旧申请信封）
        for cert in self.certificates.values():
            if cert.household_ref == app.household_ref:
                return cert
        return None

    def all_claims_of_receipt(self, receipt_id: str) -> list[ReceiptClaim]:
        return [c for c in self.receipt_claims.values() if c.receipt_id == receipt_id]

    def latest_decision(self, application_id: str) -> DecisionView | None:
        group = self.decisions.get(application_id, [])
        return group[-1] if group else None

    def paid_amount(self, application_id: str) -> int:
        return sum(p.amount_yuan for p in self.payments.get(application_id, [])
                   if p.state == "paid")

    def has_paid(self, application_id: str) -> bool:
        return any(p.state == "paid"
                   for p in self.payments.get(application_id, []))

    # ---------- 派生状态 ----------
    def application_state(self, application_id: str) -> str:
        """派生申请当前状态。

        paid（已付款，终态）/ rejected（已拒绝，终态）/ withdrawn（已撤回，终态）/
        approved（已批待付）/ submitted（待审）
        """
        if application_id not in self.applications:
            raise KeyError(application_id)
        if self.has_paid(application_id):
            return "paid"
        decision = self.latest_decision(application_id)
        if decision is not None:
            if decision.outcome == "rejected":
                return "rejected"
            if decision.outcome == "withdrawn":
                return "withdrawn"
            if decision.outcome == "approved":
                return "approved"
        raw_state = self.applications[application_id].raw.get("state", "submitted")
        if raw_state in TERMINAL_STATES:
            return raw_state
        return "submitted"
