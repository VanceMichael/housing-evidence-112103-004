"""信封存储：所有来源资料以不可变信封形式落盘。

设计要点：
- 按 source_id 幂等：重复摄入同一信封不会产生重复记录
- 附件类信封（发票、证明）按业务键保留全部历史信封，视图取最新 revision
- JSON 原子写（临时文件 + rename），无第三方依赖
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import RECORD_KINDS


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Envelope:
    source_id: str
    record_kind: str
    collected_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "record_kind": self.record_kind,
            "collected_at": self.collected_at,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        return cls(
            source_id=data["source_id"],
            record_kind=data["record_kind"],
            collected_at=data["collected_at"],
            payload=data["payload"],
        )


class Store:
    """JSON 文件存储，结构：
    {
      "created_at": ...,
      "envelopes": [信封...],
      "events": [{"at": ..., "type": ..., "detail": ...}]
    }
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ---------- 初始化 / 持久化 ----------
    def exists(self) -> bool:
        return self.path.exists()

    def _empty_document(self) -> dict[str, Any]:
        return {"created_at": now_iso(), "envelopes": [], "events": []}

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_document()
        with self.path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc.setdefault("envelopes", [])
        doc.setdefault("events", [])
        return doc

    def save(self, doc: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".wb-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()

    # ---------- 写入 ----------
    def ingest_envelope(self, raw: dict[str, Any]) -> tuple[Envelope, bool]:
        """摄入单个信封，返回 (信封, 是否为新增)。"""
        self._validate_envelope(raw)
        doc = self.load()
        known = {e["source_id"] for e in doc["envelopes"]}
        if raw["source_id"] in known:
            idx = next(i for i, e in enumerate(doc["envelopes"])
                       if e["source_id"] == raw["source_id"])
            return Envelope.from_dict(doc["envelopes"][idx]), False
        env = Envelope.from_dict(raw)
        doc["envelopes"].append(env.to_dict())
        doc["events"].append({
            "at": now_iso(),
            "type": "envelope.ingested",
            "detail": {"source_id": env.source_id, "record_kind": env.record_kind},
        })
        self.save(doc)
        return env, True

    def ingest_many(self, raws: Iterable[dict[str, Any]]) -> list[tuple[Envelope, bool]]:
        results = []
        doc = self.load()
        known = {e["source_id"] for e in doc["envelopes"]}
        changed = False
        for raw in raws:
            self._validate_envelope(raw)
            if raw["source_id"] in known:
                idx = next(i for i, e in enumerate(doc["envelopes"])
                           if e["source_id"] == raw["source_id"])
                results.append((Envelope.from_dict(doc["envelopes"][idx]), False))
                continue
            env = Envelope.from_dict(raw)
            doc["envelopes"].append(env.to_dict())
            doc["events"].append({
                "at": now_iso(),
                "type": "envelope.ingested",
                "detail": {"source_id": env.source_id, "record_kind": env.record_kind},
            })
            known.add(env.source_id)
            results.append((env, True))
            changed = True
        if changed:
            self.save(doc)
        return results

    def append_event(self, event_type: str, detail: dict[str, Any]) -> None:
        doc = self.load()
        doc["events"].append({"at": now_iso(), "type": event_type, "detail": detail})
        self.save(doc)

    # ---------- 读取 ----------
    def envelopes(self, record_kind: str | None = None) -> list[Envelope]:
        doc = self.load()
        items = [Envelope.from_dict(e) for e in doc["envelopes"]]
        if record_kind:
            items = [e for e in items if e.record_kind == record_kind]
        return items

    def events(self) -> list[dict[str, Any]]:
        return list(self.load()["events"])

    # ---------- 校验 ----------
    @staticmethod
    def _validate_envelope(raw: Any) -> None:
        if not isinstance(raw, dict):
            raise ValueError("信封必须是 JSON 对象")
        for key in ("source_id", "record_kind", "collected_at", "payload"):
            if key not in raw or raw[key] in (None, ""):
                raise ValueError(f"信封缺少字段或字段为空: {key}")
        if not isinstance(raw["payload"], dict):
            raise ValueError("信封 payload 必须是对象")
        if raw["record_kind"] not in RECORD_KINDS:
            raise ValueError(f"未知 record_kind: {raw['record_kind']}")
        # collected_at 必须是合法 ISO 时间
        datetime.fromisoformat(raw["collected_at"])
