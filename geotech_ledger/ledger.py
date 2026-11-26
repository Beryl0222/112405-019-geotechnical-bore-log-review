"""事件溯源台账：全部状态变化都是可重放的事件。

设计要点：
- 事件只追加、不修改；重建状态 = 从头重放全部事件。
- 基准更正与样号重编是独立事件类型，带证据充分性判定；
  深度是原文事实，任何更正都不移动深度记录，只改推算标高与归属。
- 已完施工开挖记录不可变，更正时追加“当前基准等效值”而不是改写原记录。
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .model import (
    Assumption,
    CorrectionRecord,
    DepthInterval,
    ElevationInterval,
    EvidenceStatus,
    ExcavationEntry,
    HandoffInterval,
    Observation,
    ObservationKind,
    ParameterSelection,
    SampleRecord,
    SelectionState,
    SourceRef,
    TestResult,
    UnconfirmedClaim,
    WorkKind,
    to_jsonable,
)

EVENT_KINDS = {
    "hole_registered", "collar_corrected",
    "observation_added", "claim_rejected",
    "sample_registered", "sample_renumbered", "sample_enriched",
    "test_added", "test_annotated",
    "assumption_added", "assumption_invalidated",
    "selection_added", "selection_reviewed",
    "handoff_issued",
    "excavation_registered",
    "correction_recorded", "correction_rejected",
}


class Ledger:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.events: list[dict[str, Any]] = []
        # 孔：当前孔口标高、原始孔口标高、基准版本与出处
        self.holes: dict[str, dict[str, Any]] = {}
        self.observations: list[Observation] = []
        self.claims: list[UnconfirmedClaim] = []
        self.samples: dict[str, SampleRecord] = {}
        self.corrections: list[CorrectionRecord] = []
        self.assumptions: dict[str, Assumption] = {}
        self.selections: dict[str, ParameterSelection] = {}
        self.handoffs: list[HandoffInterval] = []
        self.excavations: dict[str, ExcavationEntry] = {}
        self.rejected_corrections: list[CorrectionRecord] = []

    # ------------------------------------------------------------------ 事件
    def emit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise ValueError(f"未知事件类型: {kind}")
        event = {"seq": len(self.events) + 1, "kind": kind, "payload": payload}
        self._apply(event)
        self.events.append(event)
        return event

    def save_events(self, path: str | Path) -> None:
        Path(path).write_text(
            "\n".join(json.dumps(e, ensure_ascii=False, default=_json_default) for e in self.events),
            encoding="utf-8",
        )

    def replay(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            self._apply(event)
        self.events.extend(events)

    # ------------------------------------------------------------------ 应用
    def _apply(self, event: dict[str, Any]) -> None:
        kind, p = event["kind"], event["payload"]
        if kind == "hole_registered":
            self.holes[p["hole"]] = {
                "hole": p["hole"],
                "collar_elevation": p["collar_elevation"],
                "original_collar_elevation": p["collar_elevation"],
                "datum_version": "D0",
                "collar_history": [
                    {"version": "D0", "collar_elevation": p["collar_elevation"],
                     "source": p["source"]}
                ],
                "sources": [p["source"]],
            }
        elif kind == "collar_corrected":
            hole = self.holes[p["hole"]]
            versions = [h["version"] for h in hole["collar_history"]]
            new_version = f"D{len(versions)}"
            hole["collar_elevation"] = p["new_collar_elevation"]
            hole["datum_version"] = new_version
            hole["collar_history"].append({
                "version": new_version,
                "collar_elevation": p["new_collar_elevation"],
                "source": p["source"],
                "reason": p.get("reason"),
            })
        elif kind == "observation_added":
            self.observations.append(_obs_from_dict(p))
        elif kind == "claim_rejected":
            self.claims.append(UnconfirmedClaim(**p["claim"]))
        elif kind == "sample_registered":
            self.samples[p["sample_id"]] = _sample_from_dict(p)
        elif kind == "sample_renumbered":
            old, new = p["old_id"], p["new_id"]
            sample = self.samples.pop(old)
            sample.sample_id = new
            if old not in sample.aliases:
                sample.aliases.append(old)
            sample.identity_evidence = p["identity_evidence"]
            self.samples[new] = sample
            for t in sample.tests:
                t.sample_id = new
        elif kind == "sample_enriched":
            sample = self.samples[p["sample_id"]]
            if p.get("mixed"):
                sample.mixed = True
            for layer in p.get("crossed_layers", ()):
                if layer not in sample.crossed_layers:
                    sample.crossed_layers.append(layer)
            for other in p.get("distinct_from", ()):
                if other not in sample.distinct_from:
                    sample.distinct_from.append(other)
            ref = SourceRef(**p["source"])
            if ref not in sample.sources:
                sample.sources.append(ref)
        elif kind == "test_added":
            sample = self.samples[p["sample_id"]]
            sample.tests.append(_test_from_dict(p["test"]))
        elif kind == "test_annotated":
            sample = self.samples[p["sample_id"]]
            test = next(t for t in sample.tests if t.test_type == p["test_type"])
            test.layered = p["layered"]
            if p.get("note"):
                test.note = p["note"]
            test.sources.append(SourceRef(**p["source"]))
        elif kind == "assumption_added":
            self.assumptions[p["assumption_id"]] = _assumption_from_dict(p)
        elif kind == "assumption_invalidated":
            a = self.assumptions[p["assumption_id"]]
            a.valid = False
            a.invalidated_by.append(SourceRef(**p["by"]))
        elif kind == "selection_added":
            self.selections[p["selection_id"]] = _selection_from_dict(p)
        elif kind == "selection_reviewed":
            sel = self.selections[p["selection_id"]]
            sel.state = _enum(SelectionState, p["state"])
            sel.review_notes.extend(p.get("notes", ()))
        elif kind == "handoff_issued":
            self.handoffs.append(_handoff_from_dict(p))
        elif kind == "excavation_registered":
            self.excavations[p["entry_id"]] = _excavation_from_dict(p)
        elif kind == "correction_recorded":
            self.corrections.append(_correction_from_dict(p))
        elif kind == "correction_rejected":
            self.rejected_corrections.append(_correction_from_dict(p))
        else:  # pragma: no cover - EVENT_KINDS 已约束
            raise ValueError(f"未处理事件: {kind}")

    # ------------------------------------------------------------------ 查询
    def collar(self, hole: str) -> float:
        return self.holes[hole]["collar_elevation"]

    def datum_version(self, hole: str) -> str:
        return self.holes[hole]["datum_version"]

    def depth_to_elevation(self, hole: str, depth: float, version: Optional[str] = None) -> float:
        """标高 = 孔口标高(指定基准版本) - 深度。深度本身永不改写。"""
        collar = self._collar_at(hole, version)
        return round(collar - depth, 4)

    def elevation_to_depth(self, hole: str, elevation: float, version: Optional[str] = None) -> float:
        collar = self._collar_at(hole, version)
        return round(collar - elevation, 4)

    def _collar_at(self, hole: str, version: Optional[str]) -> float:
        if version is None:
            return self.holes[hole]["collar_elevation"]
        for h in self.holes[hole]["collar_history"]:
            if h["version"] == version:
                return h["collar_elevation"]
        raise KeyError(f"孔 {hole} 无基准版本 {version}")

    def depth_interval_to_elevation(
        self, hole: str, depth: DepthInterval, version: Optional[str] = None
    ) -> ElevationInterval:
        # 深度大者对应标高低者
        high = self.depth_to_elevation(hole, depth.start, version)
        low = self.depth_to_elevation(hole, depth.end, version)
        return ElevationInterval(low, high)

    def elevation_interval_to_depth(
        self, hole: str, elev: ElevationInterval, version: Optional[str] = None
    ) -> DepthInterval:
        start = self.elevation_to_depth(hole, elev.high, version)
        end = self.elevation_to_depth(hole, elev.low, version)
        return DepthInterval(start, end)

    def find_sample(self, sample_id: str) -> Optional[SampleRecord]:
        if sample_id in self.samples:
            return self.samples[sample_id]
        for s in self.samples.values():
            if sample_id in s.aliases:
                return s
        return None

    def observations_for(self, hole: str) -> list[Observation]:
        return [o for o in self.observations if o.hole == hole]

    def unconfirmed_for(self, record_id: str) -> list[UnconfirmedClaim]:
        return [c for c in self.claims if c.record_id == record_id]

    def snapshot(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "holes": self.holes,
            "observations": [o.to_dict() for o in self.observations],
            "unconfirmed_claims": [c.to_dict() for c in self.claims],
            "samples": [s.to_dict() for s in sorted(self.samples.values(), key=lambda x: x.sample_id)],
            "corrections": [c.to_dict() for c in self.corrections],
            "rejected_corrections": [c.to_dict() for c in self.rejected_corrections],
            "assumptions": [a.to_dict() for a in self.assumptions.values()],
            "selections": [s.to_dict() for s in self.selections.values()],
            "handoffs": [h.to_dict() for h in self.handoffs],
            "excavations": [e.to_dict() for e in self.excavations.values()],
        }


# ------------------------------------------------------------- dict → 对象
def _enum(enum_cls, value):
    return enum_cls(value) if not isinstance(value, enum_cls) else value


def _depth(d: dict) -> DepthInterval:
    return DepthInterval(d.get("start", d.get("start_m")), d.get("end", d.get("end_m")))


def _elevation(d: dict) -> ElevationInterval:
    return ElevationInterval(d.get("low", d.get("low_m")), d.get("high", d.get("high_m")))


def _source_refs(items: list[dict]) -> list[SourceRef]:
    return [SourceRef(**i) for i in items]


def _obs_from_dict(p: dict) -> Observation:
    d = p.get("depth")
    return Observation(
        obs_id=p["obs_id"],
        kind=ObservationKind(p["kind"]),
        hole=p["hole"],
        depth=_depth(d) if d else None,
        status=EvidenceStatus(p["status"]),
        sources=_source_refs(p["sources"]),
        layer=p.get("layer"),
        detail=p.get("detail", {}),
        observed_until=p.get("observed_until"),
        contact_located=p.get("contact_located", True),
    )


def _test_from_dict(p: dict) -> TestResult:
    return TestResult(
        test_type=p["test_type"], sample_id=p["sample_id"], layered=p["layered"],
        sources=_source_refs(p["sources"]), value=p.get("value"),
        unit=p.get("unit"), note=p.get("note"),
    )


def _sample_from_dict(p: dict) -> SampleRecord:
    return SampleRecord(
        sample_id=p["sample_id"], hole=p["hole"],
        depth=_depth(p["depth"]), sources=_source_refs(p["sources"]),
        aliases=list(p.get("aliases", ())), mixed=p.get("mixed", False),
        crossed_layers=list(p.get("crossed_layers", ())),
        identity_evidence=list(p.get("identity_evidence", ())),
        distinct_from=list(p.get("distinct_from", ())),
    )


def _assumption_from_dict(p: dict) -> Assumption:
    e = p.get("elevation")
    return Assumption(
        assumption_id=p["assumption_id"], description=p["description"],
        zone=p.get("zone"), trigger=p["trigger"], sources=_source_refs(p["sources"]),
        elevation=_elevation(e) if e else None, valid=p.get("valid", True),
        invalidated_by=_source_refs(p.get("invalidated_by", ())),
    )


def _selection_from_dict(p: dict) -> ParameterSelection:
    return ParameterSelection(
        selection_id=p["selection_id"], zone=p["zone"], hole=p.get("hole"),
        parameter=p["parameter"], state=SelectionState(p["state"]),
        sources=_source_refs(p["sources"]), value=p.get("value"), unit=p.get("unit"),
        cited_sample_ids=list(p.get("cited_sample_ids", ())),
        design_elevation=_elevation(p["design_elevation"]) if p.get("design_elevation") else None,
        evidence_depth=_depth(p["evidence_depth"]) if p.get("evidence_depth") else None,
        evidence_elevation=_elevation(p["evidence_elevation"]) if p.get("evidence_elevation") else None,
        extrapolation=list(p.get("extrapolation", ())),
        assumption_ids=list(p.get("assumption_ids", ())),
        triggers=list(p.get("triggers", ())),
        exclusions=list(p.get("exclusions", ())),
        review_notes=list(p.get("review_notes", ())),
    )


def _handoff_from_dict(p: dict) -> HandoffInterval:
    return HandoffInterval(
        handoff_id=p["handoff_id"], zone=p["zone"], hole=p.get("hole"),
        kind=WorkKind(p["kind"]), reasons=list(p["reasons"]),
        sources=_source_refs(p["sources"]),
        elevation=_elevation(p["elevation"]) if p.get("elevation") else None,
        depth=_depth(p["depth"]) if p.get("depth") else None,
        conditions=list(p.get("conditions", ())),
    )


def _excavation_from_dict(p: dict) -> ExcavationEntry:
    return ExcavationEntry(
        entry_id=p["entry_id"], zone=p["zone"],
        recorded_elevation=p["recorded_elevation"],
        datum_version=p["datum_version"], sources=_source_refs(p["sources"]),
        note=p.get("note"),
        current_equivalent_depth=p.get("current_equivalent_depth"),
        recorded_equivalent_depth=p.get("recorded_equivalent_depth"),
    )


def _correction_from_dict(p: dict) -> CorrectionRecord:
    return CorrectionRecord(
        correction_id=p["correction_id"], kind=p["kind"], applied=p["applied"],
        sources=_source_refs(p["sources"]), detail=dict(p["detail"]),
        reason=p.get("reason"),
    )


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return to_jsonable(obj)
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"不可序列化: {type(obj)}")
