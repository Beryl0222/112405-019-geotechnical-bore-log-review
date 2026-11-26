"""录入与台账规则测试：续尺挂接、证据分账、更正证据、事件可重放。"""
import _bootstrap  # noqa: F401  隔离模式路径自举
import json
import unittest
from pathlib import Path

from geotech_ledger.ingest import IngestionError, ingest_case
from geotech_ledger.ledger import Ledger
from geotech_ledger.model import (
    DepthInterval,
    EvidenceStatus,
    ObservationKind,
    SelectionState,
    SourceRef,
)
from geotech_ledger.pipeline import run_pipeline

CASE = json.loads(Path(__file__).parents[1].joinpath("data/case.json").read_text(encoding="utf-8"))


def build():
    ledger = Ledger(CASE["case_id"])
    ingest_case(CASE, ledger)
    return ledger


class IngestionTest(unittest.TestCase):
    def setUp(self):
        self.ledger = build()

    def test_continue_anchors_to_same_hole_last_advance(self):
        seen = [o for o in self.ledger.observations
                if o.hole == "K6" and o.kind is ObservationKind.LAYER_SEEN and o.layer == "灰黏土"]
        self.assertEqual(len(seen), 1)
        # 上次确认进尺终点 6.4，继续两米 → 6.4-8.4，绝不接邻孔
        self.assertEqual(seen[0].depth, DepthInterval(6.4, 8.4))
        self.assertEqual(seen[0].detail["continued_from"], DepthInterval(6.4, 6.4).to_dict())

    def test_continue_without_anchor_is_rejected(self):
        rogue = {
            "case_id": "x",
            "records": [{"id": "R1", "kind": "drilling", "text": "K9继续两米见砂层。"}],
            "objects": {"holes": [{"id": "K9", "mouth_elevation": 100.0}], "zones": [], "samples": []},
            "existing": [],
        }
        with self.assertRaises(IngestionError):
            run_pipeline(rogue)

    def test_empty_sampler_is_not_recovered_not_absence_of_layer(self):
        no_sample = [o for o in self.ledger.observations if o.kind is ObservationKind.NO_SAMPLE]
        self.assertEqual(len(no_sample), 1)
        self.assertEqual(no_sample[0].status, EvidenceStatus.NOT_RECOVERED)
        self.assertTrue(no_sample[0].depth.is_point)
        self.assertIn("不代表无此层", no_sample[0].detail["meaning"])

    def test_water_observation_carries_limited_window(self):
        # 案例中 K7 见水，观察只覆盖到 18 点：观察事实必须带时段边界
        seen = [o for o in self.ledger.observations if o.kind is ObservationKind.WATER_SEEN]
        self.assertTrue(seen)
        self.assertEqual(seen[0].observed_until, "18点")

    def test_water_not_seen_is_observation_limited(self):
        # 合成一条“未见水”：只覆盖观察时段，不外推为无水
        from geotech_ledger.parser import parse_record
        from geotech_ledger.model import Observation
        ledger = Ledger("synthetic")
        ledger.emit("hole_registered", {
            "hole": "K1", "collar_elevation": 100.0,
            "source": SourceRef("T", "K1孔口100.0").to_dict(),
        })
        result = parse_record({"id": "R1", "kind": "drilling",
                               "text": "K1深3.0m未见水，观察至16点。"})
        clause = result.clauses[0]
        ledger.emit("observation_added", Observation(
            obs_id="OBS-T1", kind=ObservationKind.WATER_NOT_SEEN, hole="K1",
            depth=clause.depth, status=EvidenceStatus.OBSERVATION_LIMITED,
            sources=[clause.source()], observed_until=clause.observed_until,
            detail={"meaning": "仅覆盖观察时段，不外推为永久无水"},
        ).to_dict())
        limited = [o for o in ledger.observations if o.status is EvidenceStatus.OBSERVATION_LIMITED]
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0].observed_until, "16点")

    def test_renumber_requires_identity_evidence_and_moves_tests(self):
        s06 = self.ledger.find_sample("S06")
        self.assertIsNotNone(s06)
        self.assertIn("S60", s06.aliases)
        self.assertEqual(s06.depth, DepthInterval(5.6, 6.2))  # 深度不变
        self.assertTrue(s06.tests)  # 实验归属随样号移动
        self.assertEqual({t.sample_id for t in s06.tests}, {"S06"})
        # 旧号仍可回查
        self.assertIs(self.ledger.find_sample("S60"), s06)

    def test_renumber_without_evidence_is_rejected(self):
        rogue = {
            "case_id": "x",
            "records": [
                {"id": "R1", "kind": "sample", "text": "S1来自K1深1.0-1.2m。"},
                {"id": "R2", "kind": "correction", "text": "S1应为S2，深度不变。"},
            ],
            "objects": {"holes": [{"id": "K1", "mouth_elevation": 100.0}], "zones": [],
                        "samples": [{"id": "S1", "hole": "K1", "depth": [1.0, 1.2]}]},
            "existing": [],
        }
        ledger = Ledger("x")
        ingest_case(rogue, ledger)
        self.assertIsNone(ledger.find_sample("S2"))
        self.assertIsNotNone(ledger.find_sample("S1"))
        self.assertEqual(ledger.rejected_corrections[0].reason[:2], "缺封")

    def test_lab_result_for_unknown_sample_is_rejected(self):
        rogue = {
            "case_id": "x",
            "records": [{"id": "R1", "kind": "lab", "text": "S9无侧限抗压0.3MPa。"}],
            "objects": {"holes": [], "zones": [], "samples": []},
            "existing": [],
        }
        with self.assertRaises(IngestionError):
            ingest_case(rogue, Ledger("x"))

    def test_datum_correction_changes_elevation_but_never_depth(self):
        # 深度 5.8 在 D0 标高 94.2，在 D1 标高 94.7；深度记录本身不变
        self.assertEqual(self.ledger.depth_to_elevation("K6", 5.8, "D0"), 94.2)
        self.assertEqual(self.ledger.depth_to_elevation("K6", 5.8, "D1"), 94.7)
        self.assertEqual(self.ledger.depth_to_elevation("K6", 5.8), 94.7)
        # K7 基准不受影响
        self.assertEqual(self.ledger.datum_version("K7"), "D0")

    def test_mixed_sample_flag_and_crossed_layers_preserved(self):
        s06 = self.ledger.find_sample("S06")
        self.assertTrue(s06.mixed)
        self.assertEqual(set(s06.crossed_layers), {"软层", "硬层"})
        ucs = next(t for t in s06.tests if t.test_type == "unconfined_compressive_strength")
        self.assertFalse(ucs.layered)

    def test_event_replay_rebuilds_identical_state(self):
        events = self.ledger.events
        rebuilt = Ledger(CASE["case_id"])
        rebuilt.replay(events)
        self.assertEqual(
            json.dumps(self.ledger.snapshot(), ensure_ascii=False, sort_keys=True),
            json.dumps(rebuilt.snapshot(), ensure_ascii=False, sort_keys=True),
        )

    def test_unconfirmed_claim_creates_no_fact(self):
        self.assertTrue(any(c.record_id == "BH-N1" for c in self.ledger.claims))
        n1_obs = [o for o in self.ledger.observations if o.sources and o.sources[0].record_id == "BH-N1"]
        self.assertFalse(any(o.depth for o in n1_obs))


if __name__ == "__main__":
    unittest.main()
