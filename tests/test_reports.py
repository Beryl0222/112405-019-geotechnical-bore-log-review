"""交接报告渲染测试：中文口径与待查项可读、可回查。"""
import _bootstrap  # noqa: F401  隔离模式路径自举
import json
import unittest
from pathlib import Path

from geotech_ledger.pipeline import run_pipeline
from geotech_ledger.reports import build_report, render_markdown

CASE = json.loads(Path(__file__).parents[1].joinpath("data/case.json").read_text(encoding="utf-8"))


class ReportTest(unittest.TestCase):
    def setUp(self):
        result = run_pipeline(CASE)
        self.md = render_markdown(build_report(result, CASE))

    def test_markdown_uses_chinese_test_label(self):
        self.assertNotIn("unconfined_compressive_strength", self.md)
        self.assertIn("无侧限抗压强度", self.md)

    def test_pending_depth_renders_readable(self):
        self.assertIn("深度 6.4-6.4m 未回收", self.md)

    def test_disclaimer_present(self):
        self.assertIn("不用于真实设计", self.md)

    def test_gap_never_says_no_anomaly(self):
        self.assertIn("不能当均一硬层或无异常", self.md)
        self.assertNotIn("均无异常", self.md)

    def test_immutable_excavation_shows_two_datum_depths(self):
        self.assertIn("D0 基准，等效深度 5.8m", self.md)
        self.assertIn("当前基准等效深度 6.3m", self.md)


if __name__ == "__main__":
    unittest.main()
