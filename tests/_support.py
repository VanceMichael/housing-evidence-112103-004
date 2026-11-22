"""测试公共夹具：每个用例使用独立临时工作台，从合成资料全量摄入。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fundbench.store import Store  # noqa: E402
from fundbench.workbench import Workbench  # noqa: E402

FIXTURE = ROOT / "fixtures" / "sources.json"


class WorkbenchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "wb.json"
        self.wb = Workbench(Store(self.store_path))
        self.report = self.wb.ingest_file(FIXTURE)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def finding_codes(self, application_id: str):
        ev = self.wb.evaluate(application_id)
        return [f.code for f in ev.findings]

    def blocker_codes(self, application_id: str):
        ev = self.wb.evaluate(application_id)
        return [f.code for f in ev.blockers]

    def state(self, application_id: str) -> str:
        return self.wb.registry().application_state(application_id)
