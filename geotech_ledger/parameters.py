"""设计参数选用：在有效证据内选参数，外推前提、假设与触发条件分账。

规则：
- 实验值不是自动可用参数；混合样参数只适用于该混合区间与对应试验类型，
  不能用平均强度覆盖所有深度。
- 缺样区只能暂用带触发条件的保守假设，假设永远标注为 ASSUMPTION，不当实测。
- 补样若推翻假设前提（如出现软夹层），关联选用说明与施工允许范围进入重核；
  无关层/无关孔的既有依据保持有效。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ledger import Ledger
from .model import (
    Assumption,
    DepthInterval,
    ElevationInterval,
    EvidenceStatus,
    ObservationKind,
    ParameterSelection,
    SelectionState,
    SourceRef,
)


@dataclass
class ZoneSpec:
    zone_id: str
    holes: list[str]
    target_elevation: ElevationInterval


def build_zone_specs(objects: dict) -> list[ZoneSpec]:
    specs = []
    for z in objects.get("zones", ()):
        lo, hi = z["target_elevation"]
        specs.append(ZoneSpec(z["id"], list(z["holes"]), ElevationInterval(min(lo, hi), max(lo, hi))))
    return specs


def add_assumption(
    ledger: Ledger, *, assumption_id: str, description: str, zone: Optional[str],
    elevation: Optional[ElevationInterval], trigger: str, source: SourceRef,
) -> Assumption:
    ledger.emit("assumption_added", {
        "assumption_id": assumption_id,
        "description": description,
        "zone": zone,
        "elevation": elevation.to_dict() if elevation else None,
        "trigger": trigger,
        "sources": [source.to_dict()],
    })
    return ledger.assumptions[assumption_id]


def evaluate_selection(
    ledger: Ledger,
    *,
    selection_id: str,
    zone: ZoneSpec,
    hole: str,
    parameter: str,
    cited_sample_id: Optional[str],
    design_elevation: ElevationInterval,
    source: SourceRef,
    initial_state: SelectionState,
    assumption_ids: Optional[list[str]] = None,
    triggers: Optional[list[str]] = None,
    exclusions: Optional[list[str]] = None,
) -> ParameterSelection:
    """计算一次参数选用的证据覆盖、外推前提与适用边界。"""
    design_depth = ledger.elevation_interval_to_depth(hole, design_elevation)
    extrapolation: list[str] = []
    review_notes: list[str] = []
    value = unit = None
    evidence_depth = evidence_elev = None
    resolved_id = None

    if cited_sample_id:
        sample = ledger.find_sample(cited_sample_id)
        if sample is None:
            raise KeyError(f"选用 {selection_id} 引用样 {cited_sample_id} 无登记，禁止用邻孔强度填空")
        if sample.hole != hole:
            # 邻孔强度不能抄：本分区该选用无有效引用，留痕并转补查，不借用任何数值
            review_notes.append(
                f"引用样 {sample.sample_id} 属于邻孔 {sample.hole}，本孔 {hole} 禁止抄邻孔强度填空；"
                "本分区无有效实测引用，须补查本孔样品后重选"
            )
            sample = None
        else:
            resolved_id = sample.sample_id
            evidence_depth = sample.depth
            evidence_elev = ledger.depth_interval_to_elevation(hole, sample.depth)
            test = next((t for t in sample.tests if t.test_type == parameter), None)
            if test is not None:
                value, unit = test.value, test.unit
                if sample.mixed and not test.layered:
                    extrapolation.append(
                        f"{sample.sample_id} 为跨 {('/'.join(sample.crossed_layers)) or '多'} 层混合样且未分层试验，"
                        f"{parameter} 仅适用于该混合区间 {sample.depth.start}-{sample.depth.end}m，"
                        "禁止以平均强度覆盖其他深度"
                    )
            else:
                review_notes.append(f"{sample.sample_id} 无 {parameter} 试验，引用无效，需补查")

            # 证据区间 vs 设计部位
            overlap = design_depth.intersection(sample.depth)
            if overlap is None:
                extrapolation.append(
                    f"证据深度 {sample.depth.start}-{sample.depth.end}m 与设计部位 "
                    f"{design_depth.start}-{design_depth.end}m 不重叠，属无实测支撑外推"
                )
            elif overlap.end - overlap.start < (design_depth.end - design_depth.start) - 1e-9:
                extrapolation.append(
                    f"证据仅覆盖设计部位的 {overlap.start}-{overlap.end}m，"
                    f"其余 {design_depth.start}-{design_depth.end}m 为外推，须列明前提"
                )
    else:
        extrapolation.append("未引用实测样品，全部按保守假设处理，不得标为实测")

    # 无有效实测引用（邻孔借用被拒或无试验）时，任何签认都不生效，转重核
    effective_state = initial_state
    if review_notes:
        if initial_state is SelectionState.SIGNED:
            review_notes.append("原签认依据不成立，签认暂不生效，待补查后重签")
        effective_state = SelectionState.REQUIRES_REVIEW

    ledger.emit("selection_added", {
        "selection_id": selection_id,
        "zone": zone.zone_id,
        "hole": hole,
        "parameter": parameter,
        "state": effective_state.value,
        "value": value,
        "unit": unit,
        "cited_sample_ids": [resolved_id] if resolved_id else [],
        "design_elevation": design_elevation.to_dict(),
        "evidence_depth": evidence_depth.to_dict() if evidence_depth else None,
        "evidence_elevation": evidence_elev.to_dict() if evidence_elev else None,
        "extrapolation": extrapolation,
        "assumption_ids": assumption_ids or [],
        "triggers": triggers or [],
        "exclusions": exclusions or [],
        "review_notes": review_notes,
        "sources": [source.to_dict()],
    })
    return ledger.selections[selection_id]


def apply_supplement_evidence(ledger: Ledger) -> list[ParameterSelection]:
    """补样证据扫描：软夹层等推翻假设前提时，把关联选用转入重核。

    无关层/无关孔的依据不受影响；独立样且不引用被推翻假设的选用保持原态。
    """
    impacted: list[ParameterSelection] = []
    soft_obs = [
        o for o in ledger.observations
        if o.layer and "软" in o.layer and o.depth is not None
    ]
    for obs in soft_obs:
        obs_elev = ledger.depth_interval_to_elevation(obs.hole, obs.depth)
        for aid, assumption in ledger.assumptions.items():
            if not assumption.valid:
                continue
            if assumption.zone is None:
                continue
            sel_hits = [s for s in ledger.selections.values() if aid in s.assumption_ids]
            zone_holes = {s.hole for s in sel_hits if s.hole}
            if obs.hole not in zone_holes:
                continue  # 无关孔，依据仍有效
            target = assumption.elevation or _zone_design_elevation(sel_hits)
            if target is None or not obs_elev.overlaps(target):
                continue
            source = obs.sources[0]
            ledger.emit("assumption_invalidated", {
                "assumption_id": aid,
                "by": source.to_dict(),
            })
            for sel in sel_hits:
                if sel.state in (SelectionState.REQUIRES_REVIEW, SelectionState.SUPERSEDED):
                    continue
                note = (
                    f"补样 {source.record_id} 在 {obs.hole} 深度 {obs.depth.start}-{obs.depth.end}m "
                    f"发现{obs.layer}，与假设“{assumption.description}”冲突，前提被推翻；"
                    "选用说明与施工允许范围须重核"
                )
                ledger.emit("selection_reviewed", {
                    "selection_id": sel.selection_id,
                    "state": SelectionState.REQUIRES_REVIEW.value,
                    "notes": [note],
                })
                impacted.append(ledger.selections[sel.selection_id])
    return impacted


def _zone_design_elevation(selections: list[ParameterSelection]) -> Optional[ElevationInterval]:
    for s in selections:
        if s.design_elevation:
            return s.design_elevation
    return None


def selection_trace(ledger: Ledger, selection_id: str) -> dict:
    """单个选用的完整证据链：样品→实验→假设→外推→重核。"""
    sel = ledger.selections[selection_id]
    chain = {
        "selection": sel.to_dict(),
        "samples": [],
        "assumptions": [ledger.assumptions[a].to_dict() for a in sel.assumption_ids if a in ledger.assumptions],
        "source_records": sorted({s.record_id for s in sel.sources}),
    }
    for sid in sel.cited_sample_ids:
        sample = ledger.find_sample(sid)
        if sample:
            chain["samples"].append(sample.to_dict())
    return chain
