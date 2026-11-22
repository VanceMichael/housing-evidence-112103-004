"""城市政策版本的归一化与按日期选版。

兼容两类政策信封：
1. 旧的精简格式（只有 eligible_use 与 household_property_fee_cap_yuan）
2. 完整格式（caps_yuan / receipt_window_months / required_materials）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .models import DEFAULT_REQUIRED_MATERIALS, USE_INVOICE_DOC


@dataclass(frozen=True)
class PolicyRule:
    city: str
    revision: int
    effective_from: date
    eligible_use: tuple[str, ...]
    caps: dict[str, int | None]
    receipt_window_months: int
    required_materials: dict[str, tuple[str, ...]]
    raw: dict[str, Any]

    def cap_for(self, use: str) -> int | None:
        return self.caps.get(use)

    def materials_for(self, use: str) -> tuple[str, ...]:
        return self.required_materials.get(use, ())

    def to_summary(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "revision": self.revision,
            "effective_from": self.effective_from.isoformat(),
            "eligible_use": list(self.eligible_use),
            "caps_yuan": self.caps,
            "receipt_window_months": self.receipt_window_months,
            "required_materials": {k: list(v) for k, v in self.required_materials.items()},
        }


def normalize_policy(payload: dict[str, Any]) -> PolicyRule:
    city = payload["city"]
    revision = int(payload["revision"])
    effective_from = date.fromisoformat(payload["effective_from"])
    eligible = tuple(payload.get("eligible_use", ()))

    caps: dict[str, int | None] = {}
    if "caps_yuan" in payload and isinstance(payload["caps_yuan"], dict):
        for use, cap in payload["caps_yuan"].items():
            caps[use] = None if cap is None else int(cap)
    # 旧格式字段映射
    if "household_property_fee_cap_yuan" in payload:
        caps.setdefault("property-fee", int(payload["household_property_fee_cap_yuan"]))

    materials: dict[str, tuple[str, ...]] = {}
    raw_materials = payload.get("required_materials")
    if isinstance(raw_materials, dict):
        for use, docs in raw_materials.items():
            materials[use] = tuple(docs)
    else:
        for use in eligible:
            if use in DEFAULT_REQUIRED_MATERIALS:
                materials[use] = tuple(DEFAULT_REQUIRED_MATERIALS[use])
    # 确保所有可申请用途都有材料清单
    for use in eligible:
        materials.setdefault(use, tuple(DEFAULT_REQUIRED_MATERIALS.get(use, ())))

    return PolicyRule(
        city=city,
        revision=revision,
        effective_from=effective_from,
        eligible_use=eligible,
        caps=caps,
        receipt_window_months=int(payload.get("receipt_window_months", 12)),
        required_materials=materials,
        raw=dict(payload),
    )


class PolicyBook:
    """按城市维护有序政策版本。"""

    def __init__(self, rules: list[PolicyRule]):
        self._by_city: dict[str, list[PolicyRule]] = {}
        for rule in rules:
            self._by_city.setdefault(rule.city, []).append(rule)
        for city_rules in self._by_city.values():
            city_rules.sort(key=lambda r: (r.effective_from, r.revision))

    def cities(self) -> list[str]:
        return sorted(self._by_city)

    def versions(self, city: str) -> list[PolicyRule]:
        return list(self._by_city.get(city, ()))

    def rule_effective_on(self, city: str, on_date: date) -> PolicyRule | None:
        """返回 on_date 当日（含）已生效的最新版本。"""
        chosen: PolicyRule | None = None
        for rule in self._by_city.get(city, ()):
            if rule.effective_from <= on_date:
                chosen = rule
            else:
                break
        return chosen

    def latest(self, city: str) -> PolicyRule | None:
        rules = self._by_city.get(city, ())
        return rules[-1] if rules else None
