"""家庭年度额度台账。

关键区分（业务要求）：
- 家庭年度上限：同一家庭、同一年度、同一用途下全体成员共同占用的额度
- 个人账户余额：成员个人公积金账户里的钱，两者不是同一个数

台账从「有效决定」派生，而非单独维护可变余额：
- 有效决定 = 每个申请最新一条决定，且结果为 approved（含已付款与已批待付）
- 被后续决定拒绝/撤回的旧批准自动退出台账，不会残留占用
- 已付款项目永久保留其当时依据（决定快照冻结政策版本与证据）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import Registry


@dataclass(frozen=True)
class LedgerEntry:
    application_id: str
    household_ref: str
    member_ref: str
    year: int
    use: str
    granted_amount_yuan: int
    paid_amount_yuan: int
    policy_revision: int
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "household_ref": self.household_ref,
            "member_ref": self.member_ref,
            "year": self.year,
            "use": self.use,
            "granted_amount_yuan": self.granted_amount_yuan,
            "paid_amount_yuan": self.paid_amount_yuan,
            "policy_revision": self.policy_revision,
            "state": self.state,
        }


class HouseholdLedger:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.entries: list[LedgerEntry] = self._build()

    def _build(self) -> list[LedgerEntry]:
        entries: list[LedgerEntry] = []
        for app in self.registry.ordered_applications():
            decision = self.registry.latest_decision(app.application_id)
            if decision is None or decision.outcome != "approved":
                continue
            year = int(app.submitted_date[:4])
            state = self.registry.application_state(app.application_id)
            entries.append(LedgerEntry(
                application_id=app.application_id,
                household_ref=app.household_ref,
                member_ref=app.member_ref,
                year=year,
                use=app.use,
                granted_amount_yuan=decision.granted_amount_yuan,
                paid_amount_yuan=self.registry.paid_amount(app.application_id),
                policy_revision=decision.policy_revision,
                state=state,
            ))
        return entries

    def used(self, household_ref: str, year: int, use: str,
             exclude_application: str | None = None) -> int:
        """家庭在指定年度/用途下已占用的额度（已批待付 + 已付款）。"""
        total = 0
        for e in self.entries:
            if (e.household_ref == household_ref and e.year == year
                    and e.use == use and e.application_id != exclude_application):
                total += e.granted_amount_yuan
        return total

    def household_entries(self, household_ref: str) -> list[LedgerEntry]:
        return [e for e in self.entries if e.household_ref == household_ref]

    def all_entries(self) -> list[LedgerEntry]:
        return list(self.entries)
