"""解析器规则测试：中文、单位简写、省略句、歧义隔离。"""
import _bootstrap  # noqa: F401  隔离模式路径自举
import json
import unittest
from pathlib import Path

from geotech_ledger.parser import ClauseKind, parse_case, parse_record

CASE = json.loads(Path(__file__).parents[1].joinpath("data/case.json").read_text(encoding="utf-8"))
RECORDS = {r["id"]: r for r in CASE["records"]}


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_case(CASE["records"])

    def test_hole_and_depth_unit_variants(self):
        clauses = self.parsed["BH-D1"].clauses
        collar = next(c for c in clauses if c.kind is ClauseKind.COLLAR)
        self.assertEqual(collar.hole, "K6")
        self.assertEqual(collar.collar_elevation, 100.0)
        entry = next(c for c in clauses if c.kind is ClauseKind.LAYER_ENTRY)
        self.assertEqual(entry.point_depth, 5.8)
        self.assertEqual(entry.layer, "硬层")

    def test_ellipsis_inherits_same_record_hole_only(self):
        # “至6.4m取样筒空”“继续两米”均无孔号，必须继承同记录的 K6
        clauses = self.parsed["BH-D1"].clauses
        empty = next(c for c in clauses if c.kind is ClauseKind.EMPTY_SAMPLER)
        self.assertEqual(empty.hole, "K6")
        self.assertEqual(empty.point_depth, 6.4)
        cont = next(c for c in clauses if c.kind is ClauseKind.CONTINUE)
        self.assertEqual(cont.hole, "K6")
        self.assertEqual(cont.amount, 2.0)
        self.assertEqual(cont.layer, "灰黏土")

    def test_continue_carries_no_absolute_depth(self):
        # 解析器不自行计算续尺区间，交给台账按同孔锚点挂接
        cont = next(c for c in self.parsed["BH-D1"].clauses if c.kind is ClauseKind.CONTINUE)
        self.assertIsNone(cont.depth)

    def test_water_observation_window_merges_into_same_record_clause(self):
        clauses = self.parsed["BH-D2"].clauses
        water = next(c for c in clauses if c.kind is ClauseKind.WATER_SEEN)
        self.assertEqual(water.hole, "K7")
        self.assertEqual(water.point_depth, 6.1)
        self.assertEqual(water.observed_until, "18点")
        # “观察至18点”不应再作为独立条款残留
        self.assertFalse(any(c.kind is ClauseKind.NOTE and c.observed_until for c in clauses))

    def test_ambiguous_oral_claim_is_quarantined(self):
        claims = self.parsed["BH-N1"].unconfirmed
        self.assertEqual(len(claims), 1)
        self.assertIn("未确认", claims[0].reason)
        # 歧义句不产生任何带深度的条款
        self.assertFalse(any(c.depth for c in self.parsed["BH-N1"].clauses))

    def test_renumber_keeps_depth_and_extracts_identity_evidence(self):
        c = next(c for c in self.parsed["BH-C1"].clauses if c.kind is ClauseKind.RENUMBER)
        self.assertEqual(c.old_sample_id, "S60")
        self.assertEqual(c.sample_id, "S06")
        self.assertEqual(c.evidence, ["封签", "接收单"])
        self.assertTrue(c.extra["depth_unchanged"])

    def test_datum_correction_does_not_fire_on_other_hole_unchanged_clause(self):
        kinds = [c.kind for c in self.parsed["BH-C2"].clauses]
        datum = [c for c in self.parsed["BH-C2"].clauses if c.kind is ClauseKind.DATUM_CORRECTION]
        self.assertEqual(len(datum), 1)
        self.assertEqual(datum[0].hole, "K6")
        self.assertEqual(datum[0].collar_elevation, 100.5)
        self.assertNotIn(ClauseKind.DATUM_CORRECTION, kinds[1:])

    def test_lab_continuation_inherits_sample_id(self):
        clauses = self.parsed["BH-L1"].clauses
        lab = [c for c in clauses if c.kind is ClauseKind.LAB_TEST]
        self.assertEqual({c.sample_id for c in lab}, {"S60", "S61"})
        merged = next(c for c in lab if c.merge_into)
        self.assertEqual(merged.sample_id, "S60")
        self.assertFalse(merged.layered)

    def test_design_statement_splits_into_selection_assumption_trigger(self):
        kinds = [c.kind for c in self.parsed["BH-P1"].clauses]
        self.assertIn(ClauseKind.DESIGN_SELECTION, kinds)
        self.assertIn(ClauseKind.ASSUMPTION, kinds)
        self.assertIn(ClauseKind.TRIGGER, kinds)
        sel = next(c for c in self.parsed["BH-P1"].clauses if c.kind is ClauseKind.DESIGN_SELECTION)
        self.assertEqual(sel.zone, "Z2")  # 族号保留，由流水线展开
        self.assertEqual(sel.elev_interval.low, 93.0)
        self.assertEqual(sel.elev_interval.high, 94.0)

    def test_supplement_sample_layer_name_is_clean(self):
        c = next(c for c in self.parsed["BH-D3"].clauses if c.kind is ClauseKind.SUPPLEMENT_SAMPLE)
        self.assertEqual(c.layer, "软夹层")
        self.assertEqual(c.distinct_from, ["S06"])

    def test_oral_submission_does_not_bind_registered_sample(self):
        c = next(c for c in self.parsed["BH-D2"].clauses if c.kind is ClauseKind.SUBMISSION_CLAIM)
        self.assertEqual(c.extra["oral_sample_no"], "六")
        self.assertIsNone(c.depth)

    def test_generic_ambiguous_phrase_above_six_meters_is_quarantined(self):
        result = parse_record({"id": "X1", "kind": "field-note", "text": "六米以上未取到样。"})
        self.assertEqual(len(result.unconfirmed), 1)
        self.assertIn("无界", result.unconfirmed[0].reason)


if __name__ == "__main__":
    unittest.main()
