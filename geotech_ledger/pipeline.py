"""端到端流水线：资料包 → 录入 → 区间确认 → 参数选用 → 补样重核 → 施工交接。

只处理合成资料；全部结论带出处与证据状态，供设计/施工交接与回查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .construction import (
    ConstructionSpec,
    build_handoff,
    correction_impact,
    register_excavation,
)
from .geology import confirmed_layers, evidence_coverage
from .ingest import ingest_case
from .ledger import Ledger
from .model import (
    ElevationInterval,
    SelectionState,
    SourceRef,
    TEST_UCS,
)
from .parameters import (
    add_assumption,
    apply_supplement_evidence,
    build_zone_specs,
    evaluate_selection,
)
from .parser import Clause, ClauseKind, parse_case
from .trace import pending_investigations, trace_zone


@dataclass
class PipelineResult:
    ledger: Ledger
    warnings: list[str]
    impact: dict
    pending: list[dict]

    def to_dict(self) -> dict[str, Any]:
        snap = self.ledger.snapshot()
        snap["warnings"] = self.warnings
        snap["correction_impact"] = self.impact
        snap["pending_investigations"] = self.pending
        return snap


def run_pipeline(case: dict) -> PipelineResult:
    ledger = Ledger(case["case_id"])
    report = ingest_case(case, ledger)
    parsed = parse_case(case["records"])
    clauses = {rid: pr.clauses for rid, pr in parsed.items()}
    zones = {z.zone_id: z for z in build_zone_specs(case["objects"])}

    # ---- 假设与触发：缺样区保守假设，必须带补查触发 ---------------------
    assumptions_by_zone = _assumptions(clauses, ledger, zones)
    triggers_by_zone = _triggers(clauses, zones)

    # ---- 参数选用 ------------------------------------------------------
    existing_state = {e["id"]: e["state"] for e in case.get("existing", ())}
    _design_selections(case, ledger, clauses, zones, assumptions_by_zone, triggers_by_zone, existing_state)

    # ---- 实际开挖登记（不可变；按当时基准版本） --------------------------
    _register_construction(case, ledger, clauses)

    # ---- 补样证据：推翻前提 → 假设失效、选用进入重核 ---------------------
    apply_supplement_evidence(ledger)

    # ---- 施工交接区间 --------------------------------------------------
    monitor = ["见软夹层立即停待并复测标高", "未签认参数不得作为强度验收依据"]
    for spec in zones.values():
        for hole in spec.holes:
            handoffs = build_handoff(
                ledger,
                spec=ConstructionSpec(spec.zone_id, hole, spec.target_elevation),
                source=SourceRef("PIPELINE", f"handoff:{spec.zone_id}"),
                monitor_conditions=monitor,
            )
            for h in handoffs:
                ledger.emit("handoff_issued", h.to_dict())

    impact = correction_impact(ledger)
    pending = pending_investigations(ledger)
    return PipelineResult(ledger, report.warnings, impact, pending)


# ---------------------------------------------------------------- 内部装配
def _expand_zones(token: Optional[str], zones: dict) -> list:
    """Z2 这类族号展开为登记的 Z2-A/Z2-B；精确号只返回自身。"""
    if token is None:
        return list(zones.values())
    if "-" in token:
        return [zones[token]] if token in zones else []
    return [z for zid, z in zones.items() if zid == token or zid.startswith(token + "-")]


def _assumptions(clauses: dict[str, list[Clause]], ledger: Ledger, zones: dict) -> dict[str, str]:
    """返回 {分区: 假设ID}；族号假设展开到各登记分区，假设必须绑定具体分区。"""
    out: dict[str, str] = {}
    seq = 0
    for rid, items in clauses.items():
        for clause in items:
            if clause.kind is not ClauseKind.ASSUMPTION:
                continue
            targets = _expand_zones(clause.zone, zones)
            if not targets:
                raise ValueError(f"{rid}: 缺样区假设未绑定有效分区，不能进入设计依据")
            for spec in targets:
                seq += 1
                aid = f"ASMP-{seq}"
                add_assumption(
                    ledger,
                    assumption_id=aid,
                    description=clause.quote,
                    zone=spec.zone_id,
                    elevation=clause.elev_interval or spec.target_elevation,
                    trigger="见软夹层须停待补查；补样结果与均一前提冲突即失效",
                    source=clause.source(),
                )
                out[spec.zone_id] = aid
    return out


def _triggers(clauses: dict[str, list[Clause]], zones: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for rid, items in clauses.items():
        for clause in items:
            if clause.kind is ClauseKind.TRIGGER:
                for spec in _expand_zones(clause.zone, zones):
                    out[spec.zone_id] = clause.quote
    return out


def _design_selections(
    case: dict, ledger: Ledger, clauses, zones, assumptions_by_zone, triggers_by_zone, existing_state
) -> None:
    # 既有办理记录的稳定业务编号按来源记录映射
    source_to_business_id = {e["source"]: e["id"] for e in case.get("existing", ())}
    seq = 0
    for rid, items in clauses.items():
        for clause in items:
            if clause.kind is ClauseKind.DESIGN_SELECTION:
                targets = _expand_zones(clause.zone, zones)
                base_id = source_to_business_id.get(rid)
                for spec in targets:
                    seq += 1
                    # 族号选用展开为多分区时保留业务编号族标识并加分区后缀
                    sel_id = (
                        f"{base_id}-{spec.zone_id}" if base_id and len(targets) > 1
                        else base_id or f"PARAM-{seq}"
                    )
                    hole = spec.holes[0]
                    assumption_ids = [assumptions_by_zone[spec.zone_id]] if spec.zone_id in assumptions_by_zone else []
                    triggers = [triggers_by_zone[spec.zone_id]] if spec.zone_id in triggers_by_zone else []
                    evaluate_selection(
                        ledger,
                        selection_id=sel_id, zone=spec, hole=hole,
                        parameter=TEST_UCS,
                        cited_sample_id=clause.sample_id,
                        design_elevation=clause.elev_interval or spec.target_elevation,
                        source=clause.source(),
                        initial_state=SelectionState.DRAFT,
                        assumption_ids=assumption_ids, triggers=triggers,
                    )
            elif clause.kind is ClauseKind.ZONE_SELECTION:
                seq += 1
                targets = _expand_zones(clause.zone, zones)
                base_id = source_to_business_id.get(rid)
                for spec in targets:
                    sel_id = base_id or f"PARAM-{seq}"
                    hole = spec.holes[0]
                    state = SelectionState.SIGNED if existing_state.get(sel_id) == "signed" else SelectionState.DRAFT
                    evaluate_selection(
                        ledger,
                        selection_id=sel_id, zone=spec, hole=hole,
                        parameter=TEST_UCS,
                        cited_sample_id=clause.sample_id,
                        design_elevation=spec.target_elevation,
                        source=clause.source(),
                        initial_state=state,
                        exclusions=clause.exclusions,
                    )


def _register_construction(case: dict, ledger: Ledger, clauses) -> None:
    seq = 0
    for rid, items in clauses.items():
        for clause in items:
            if clause.kind is not ClauseKind.CONSTRUCTION_EXCAVATION:
                continue
            seq += 1
            zone = clause.zone
            hole = next(
                (h for z in case["objects"]["zones"] if z["id"] == zone for h in z["holes"]),
                None,
            )
            if hole is None:
                continue
            register_excavation(
                ledger,
                entry_id=f"EXC-{seq}", zone=zone, hole=hole,
                recorded_elevation=clause.elevation,
                datum_version="D0",
                source=clause.source(),
                note=clause.note,
            )


def geology_summary(ledger: Ledger, case: dict) -> dict:
    """供报告使用的层位与证据覆盖汇总。"""
    out = {}
    for zone in build_zone_specs(case["objects"]):
        out[zone.zone_id] = {"holes": {}}
        for hole in zone.holes:
            domain = ledger.elevation_interval_to_depth(hole, zone.target_elevation)
            out[zone.zone_id]["holes"][hole] = {
                "layers": [s.to_dict() for s in confirmed_layers(ledger, hole, domain)],
                "coverage": evidence_coverage(ledger, hole, domain),
            }
    return out
