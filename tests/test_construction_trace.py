"""施工交接、更正影响面与可追溯查询测试。"""
import _bootstrap  # noqa: F401  隔离模式路径自举
import json
import unittest
from pathlib import Path

from geotech_ledger.ingest import ingest_case
from geotech_ledger.ledger import Ledger
from geotech_ledger.model import WorkKind
from geotech_ledger.pipeline import run_pipeline
from geotech_ledger.trace import (
    pending_investigations,
    trace_depth,
    trace_sample,
    trace_zone,
)
from geotech_ledger.parameters import build_zone_specs

CASE = json.loads(Path(__file__).parents[1].joinpath("data/case.json").read_text(encoding="utf-8"))


class ConstructionTest(unittest.TestCase):
    def setUp(self):
        self.result = run_pipeline(CASE)
        self.ledger = self.result.ledger

    def test_handoff_intervals_are_partitioned(self):
        by_zone: dict[str, list] = {}
        for h in self.ledger.handoffs:
            by_zone.setdefault(h.zone, []).append(h.elevation)
        for zone, ivs in by_zone.items():
            ordered = sorted(ivs, key=lambda i: i.low)
            for a, b in zip(ordered, ordered[1:]):
                self.assertLessEqual(a.high, b.low + 1e-9, zone)

    def test_soft_interlayer_and_unsampled_become_stop_pending(self):
        stops = [h for h in self.ledger.handoffs if h.kind is WorkKind.STOP_PENDING]
        reasons = " ".join(r for h in stops for r in h.reasons)
        self.assertIn("软夹层", reasons)
        self.assertIn("缺样区", reasons)
        self.assertTrue(all(h.conditions for h in stops))

    def test_completed_excavation_record_is_immutable_across_datum_change(self):
        exc = next(iter(self.ledger.excavations.values()))
        # 原记录固定在 D0：94.20 标高对应 5.8m；当前 D1 等效 6.3m，记录未被移动
        self.assertEqual(exc.datum_version, "D0")
        self.assertEqual(exc.recorded_elevation, 94.2)
        self.assertEqual(exc.recorded_equivalent_depth, 5.8)
        self.assertEqual(exc.current_equivalent_depth, 6.3)
        # 已挖面尚未进入目标域（94.20 > 顶板 94.0），不生成目标域内已完成段
        self.assertFalse(any(h.kind is WorkKind.COMPLETED for h in self.ledger.handoffs))

    def test_correction_impact_lists_affected_zone_and_immutable_work(self):
        impact = self.result.impact
        self.assertEqual(impact["affected_holes"], ["K6"])
        self.assertIn("S60→S06", impact["affected_sample_mappings"])
        self.assertIn("Z2-A", impact["affected_zones"])
        self.assertTrue(impact["immutable_excavations"])
        self.assertIn("不移动", impact["rule"])
        # K7 独立分区 Z2-B 的已签认依据不列入基准更正影响
        recheck_ids = {s["selection_id"] for s in impact["selections_to_recheck"]}
        self.assertIn("PARAM-A-Z2-A", recheck_ids)


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.result = run_pipeline(CASE)
        self.ledger = self.result.ledger

    def test_trace_sample_follows_alias(self):
        traced = trace_sample(self.ledger, "S60")
        self.assertTrue(traced["found"])
        self.assertEqual(traced["sample"]["sample_id"], "S06")
        self.assertIn("PARAM-A-Z2-A", traced["used_by_selections"])

    def test_trace_unknown_sample_does_not_guess(self):
        traced = trace_sample(self.ledger, "S99")
        self.assertFalse(traced["found"])
        self.assertIn("禁止凭邻号猜同袋", traced["rule"])

    def test_trace_depth_distinguishes_not_recovered_from_unknown(self):
        at_64 = trace_depth(self.ledger, "K6", 6.4)
        # 6.4m 原取样筒空；后补样 S07 覆盖该深度，主状态为有实测，
        # 但原始未回收事实必须独立保留、不被补样抹掉
        self.assertTrue(at_64["has_not_recovered_event"])
        self.assertTrue(at_64["not_recovered_facts"])
        self.assertIn("不代表无此层", at_64["meaning"])
        at_88 = trace_depth(self.ledger, "K6", 8.8)
        self.assertEqual(at_88["status"], "unknown_no_record")
        self.assertIn("不能判为无异常", at_88["meaning"])

    def test_trace_pure_not_recovered_point_status(self):
        # 构造只有未回收、没有样品覆盖的孔，主状态必须是 not_recovered
        from geotech_ledger.ingest import ingest_case
        synthetic = {
            "case_id": "synth-not-recovered",
            "records": [
                {"id": "D1", "kind": "drilling",
                 "text": "K1孔口100.00，深2.0m至2.2m取样筒空。"},
            ],
            "objects": {"holes": [{"id": "K1", "mouth_elevation": 100.0}],
                        "zones": [], "samples": []},
            "existing": [],
        }
        ledger = Ledger("synth")
        ingest_case(synthetic, ledger)
        result = trace_depth(ledger, "K1", 2.2)
        self.assertEqual(result["status"], "not_recovered")
        self.assertIn("不代表无此层", result["meaning"])

    def test_trace_zone_contains_assumptions_and_claims(self):
        specs = {s.zone_id: s for s in build_zone_specs(CASE["objects"])}
        view = trace_zone(self.ledger, "Z2-A", specs["Z2-A"])
        self.assertTrue(view["assumptions"])
        self.assertTrue(view["unconfirmed_claims"])

    def test_pending_investigations_cover_all_open_items(self):
        pending = pending_investigations(self.ledger)
        types = {p["type"] for p in pending}
        self.assertIn("unconfirmed_claim", types)
        self.assertIn("not_recovered", types)
        self.assertIn("submission_claim", types)
        self.assertTrue(any(t.startswith("selection_") for t in types))

    def test_every_fact_is_sourceable(self):
        for obs in self.ledger.observations:
            self.assertTrue(obs.sources)
            self.assertTrue(all(s.record_id for s in obs.sources))
        for sel in self.ledger.selections.values():
            self.assertTrue(sel.sources)


if __name__ == "__main__":
    unittest.main()
