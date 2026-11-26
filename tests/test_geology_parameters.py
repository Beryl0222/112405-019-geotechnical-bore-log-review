"""层位证据覆盖与参数选用规则测试。"""
import _bootstrap  # noqa: F401  隔离模式路径自举
import json
import unittest
from pathlib import Path

from geotech_ledger.geology import UNSAMPLED, confirmed_layers, evidence_coverage
from geotech_ledger.model import DepthInterval, ElevationInterval, SelectionState
from geotech_ledger.pipeline import run_pipeline

CASE = json.loads(Path(__file__).parents[1].joinpath("data/case.json").read_text(encoding="utf-8"))


class GeologyTest(unittest.TestCase):
    def setUp(self):
        self.result = run_pipeline(CASE)
        self.ledger = self.result.ledger

    def test_gap_is_unsampled_never_no_anomaly(self):
        # Z2-B 目标域在 K7 对应深度 5.2-6.2
        coverage = evidence_coverage(self.ledger, "K7", DepthInterval(5.2, 6.2))
        statuses = {g["status"] for g in coverage["gaps"]}
        self.assertIn(UNSAMPLED, statuses)
        for g in coverage["gaps"]:
            # 缺样必须表达“无证据、不能下结论”，不能写成“均无异常”
            self.assertIn("不能当", g["reason"])
            self.assertNotIn("均无异常", g["reason"])

    def test_measured_coverage_excludes_unrecovered_point_from_layer_facts(self):
        coverage = evidence_coverage(self.ledger, "K6", DepthInterval(5.0, 9.0))
        # 6.4 未回收点不产生样品覆盖，但也不抹掉 6.4-8.4 的见层事实
        flat = [iv for iv in coverage["sample_intervals"]]
        self.assertTrue(all(iv["start_m"] != 6.4 or iv["end_m"] != 6.4 for iv in flat))

    def test_out_of_domain_contact_kept_as_reference(self):
        domain = self.ledger.elevation_interval_to_depth(
            "K6", ElevationInterval(93, 94)
        )
        segs = confirmed_layers(self.ledger, "K6", domain)
        # 5.8m 硬层界在目标域外，仍作为参照保留
        self.assertTrue(any(s.depth.start == 5.8 and s.notes for s in segs))


class ParameterTest(unittest.TestCase):
    def setUp(self):
        self.result = run_pipeline(CASE)
        self.ledger = self.result.ledger

    def test_mixed_sample_strength_scoped_to_mixed_interval(self):
        sel = self.ledger.selections["PARAM-A-Z2-A"]
        self.assertTrue(any("仅适用于该混合区间" in e for e in sel.extrapolation))
        self.assertTrue(any("禁止以平均强度覆盖其他深度" in e for e in sel.extrapolation))

    def test_neighbor_hole_strength_cannot_fill_gap(self):
        sel = self.ledger.selections["PARAM-A-Z2-B"]
        self.assertEqual(sel.state, SelectionState.REQUIRES_REVIEW)
        self.assertEqual(sel.cited_sample_ids, [])
        self.assertTrue(any("禁止抄邻孔强度" in n for n in sel.review_notes))

    def test_supplement_overturns_assumption_and_triggers_review(self):
        asm = next(a for a in self.ledger.assumptions.values() if a.zone == "Z2-A")
        self.assertFalse(asm.valid)
        self.assertEqual(asm.invalidated_by[0].record_id, "BH-D3")
        sel = self.ledger.selections["PARAM-A-Z2-A"]
        self.assertEqual(sel.state, SelectionState.REQUIRES_REVIEW)

    def test_unrelated_zone_basis_remains_valid(self):
        # 软夹层在 K6；Z2-B 的假设本身不因此失效（其失效另有邻孔/无试验原因）
        asm_b = next(a for a in self.ledger.assumptions.values() if a.zone == "Z2-B")
        self.assertTrue(asm_b.valid)

    def test_signed_selection_without_matching_test_is_downgraded(self):
        sel = self.ledger.selections["PARAM-B"]
        self.assertEqual(sel.state, SelectionState.REQUIRES_REVIEW)
        self.assertTrue(any("签认暂不生效" in n for n in sel.review_notes))

    def test_assumption_is_never_marked_measured(self):
        for a in self.ledger.assumptions.values():
            self.assertTrue(a.trigger)
        # 假设不会出现在实测观察里
        kinds = {o.kind.value for o in self.ledger.observations}
        self.assertNotIn("assumption", kinds)


if __name__ == "__main__":
    unittest.main()
