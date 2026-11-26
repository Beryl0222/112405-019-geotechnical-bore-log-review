"""施工交接与更正影响分析。

- 施工领取三类区间：可作业 / 停待补查 / 已完成，附监测条件与依据。
- 实际开挖登记不可变；基准更正后只追加当前基准等效深度，原记录不移动。
- 基准更正与样号重编同时发生时，输出受影响的区间、参数采用与未完施工段。
"""
from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger
from .model import (
    DepthInterval,
    ElevationInterval,
    HandoffInterval,
    ObservationKind,
    SelectionState,
    SourceRef,
    WorkKind,
)


@dataclass
class ConstructionSpec:
    zone_id: str
    hole: str
    target_elevation: ElevationInterval


def register_excavation(
    ledger: Ledger, *, entry_id: str, zone: str, hole: str,
    recorded_elevation: float, datum_version: str, source: SourceRef,
    note: str | None = None,
) -> None:
    """登记实际开挖。记录固定在当时基准版本，后续更正不改写原记录。

    同时记录两个深度：登记基准下的等效深度（随记录固定）与当前基准等效深度
    （只作展示换算）；两者都不移动原始标高记录。
    """
    recorded_equiv = ledger.elevation_to_depth(hole, recorded_elevation, datum_version)
    current_equiv = ledger.elevation_to_depth(hole, recorded_elevation)
    ledger.emit("excavation_registered", {
        "entry_id": entry_id,
        "zone": zone,
        "recorded_elevation": recorded_elevation,
        "datum_version": datum_version,
        "recorded_equivalent_depth": recorded_equiv,
        "current_equivalent_depth": current_equiv,
        "note": note,
        "sources": [source.to_dict()],
    })


def build_handoff(
    ledger: Ledger, *, spec: ConstructionSpec, source: SourceRef,
    monitor_conditions: list[str] | None = None,
) -> list[HandoffInterval]:
    """按可作业/停待补查/已完成生成互不重叠的交接区间，依据按区间归集。"""
    from .geology import UNSAMPLED, evidence_coverage

    target = spec.target_elevation
    hole = spec.hole
    seq = 0

    def next_id() -> str:
        nonlocal seq
        seq += 1
        return f"HO-{spec.zone_id}-{seq}"

    # reason_map：区间 → 依据列表；先各自收集，最后按合并区间汇总
    stop_map: dict[tuple[float, float], list[str]] = {}

    def add_stop(iv: ElevationInterval, reason: str) -> None:
        clipped = iv.intersection(target)
        if clipped is None:
            return
        stop_map.setdefault((clipped.low, clipped.high), []).append(reason)

    # 1. 现场触发：未回收点、软夹层
    for obs in ledger.observations_for(hole):
        if obs.depth is None:
            continue
        obs_elev = ledger.depth_interval_to_elevation(hole, obs.depth)
        if obs.kind is ObservationKind.NO_SAMPLE:
            add_stop(obs_elev, f"{obs.obs_id}: 未取到样，未回收不等于无异常")
        elif obs.layer and "软" in obs.layer:
            add_stop(obs_elev, f"{obs.obs_id}: {obs.layer}，触发停待补查")

    # 2. 选用进入重核（假设被推翻/邻孔借用/无试验）→ 整个设计标高域停待
    for sel in ledger.selections.values():
        if sel.zone != spec.zone_id or sel.state is not SelectionState.REQUIRES_REVIEW:
            continue
        if sel.design_elevation:
            for note in sel.review_notes:
                add_stop(sel.design_elevation, f"{sel.selection_id}: {note}")

    # 3. 缺样区（无实测证据）不得按可作业放行
    target_depth = ledger.elevation_interval_to_depth(hole, target)
    coverage = evidence_coverage(ledger, hole, target_depth)
    for gap in coverage["gaps"]:
        if gap["status"] != UNSAMPLED:
            continue
        add_stop(
            ElevationInterval(gap["elevation"]["low_m"], gap["elevation"]["high_m"]),
            "缺样区：无实测证据，不能按均一硬层放行，须补查后再定参数",
        )

    # 4. 合并停待区间并汇总依据
    raw_stops = [ElevationInterval(lo, hi) for lo, hi in stop_map]
    merged_stops = _merge_elev(raw_stops)
    handoffs: list[HandoffInterval] = []
    for iv in merged_stops:
        reasons = sorted({
            reason
            for (lo, hi), reasons in stop_map.items()
            if iv.overlaps(ElevationInterval(lo, hi))
            for reason in reasons
        })
        handoffs.append(HandoffInterval(
            handoff_id=next_id(), zone=spec.zone_id, hole=hole,
            kind=WorkKind.STOP_PENDING, elevation=iv,
            depth=ledger.elevation_interval_to_depth(hole, iv),
            conditions=list(monitor_conditions or []), reasons=reasons,
            sources=[source],
        ))

    # 5. 已完施工段（不可变）；已挖面未进入目标域则不产生目标域内已完成段
    done_intervals: list[ElevationInterval] = []
    for entry in ledger.excavations.values():
        if entry.zone != spec.zone_id or entry.recorded_elevation >= target.high:
            continue
        clipped = ElevationInterval(max(entry.recorded_elevation, target.low), target.high)
        if clipped.low >= clipped.high:
            continue
        done_intervals.append(clipped)
        handoffs.append(HandoffInterval(
            handoff_id=next_id(), zone=spec.zone_id, hole=hole,
            kind=WorkKind.COMPLETED, elevation=clipped,
            depth=ledger.elevation_interval_to_depth(hole, clipped),
            conditions=[],
            reasons=[f"已挖至 {entry.recorded_elevation}m（{entry.datum_version} 基准登记，记录不移动）"],
            sources=[source, entry.sources[0]],
        ))

    # 6. 可作业 = 目标域 − 停待 − 已完成，且需要签认参数生效
    workable = [target]
    for iv in merged_stops + done_intervals:
        workable = _subtract_many(workable, iv.intersection(target) or iv)
    signed = any(
        s.zone == spec.zone_id and s.state is SelectionState.SIGNED
        for s in ledger.selections.values()
    )
    for iv in workable:
        reasons = ["目标区间内有实测证据支撑且无停待触发"]
        if not signed:
            reasons.append("注意：参数选用未签认或已转重核，可作业不代表强度参数已生效")
        handoffs.append(HandoffInterval(
            handoff_id=next_id(), zone=spec.zone_id, hole=hole,
            kind=WorkKind.WORKABLE, elevation=iv,
            depth=ledger.elevation_interval_to_depth(hole, iv),
            conditions=list(monitor_conditions or []), reasons=reasons, sources=[source],
        ))
    return sorted(handoffs, key=lambda h: (h.kind.value, h.elevation.high), reverse=True)


def correction_impact(ledger: Ledger) -> dict:
    """基准更正与样号重编的联合影响面（使用者视角）。"""
    datum_corrections = [c for c in ledger.corrections if c.kind == "datum"]
    renumber_corrections = [c for c in ledger.corrections if c.kind == "sample_renumber"]

    affected_holes = sorted({c.detail["hole"] for c in datum_corrections})
    affected_samples = sorted({
        f"{c.detail['old_id']}→{c.detail['new_id']}" for c in renumber_corrections
    })

    zones_affected = set()
    for h in ledger.handoffs:
        if h.hole in affected_holes:
            zones_affected.add(h.zone)

    selections_affected = []
    for sel in ledger.selections.values():
        touched = any(sid in {c.detail["new_id"] for c in renumber_corrections}
                      for sid in sel.cited_sample_ids)
        if sel.hole in affected_holes or touched:
            zones_affected.add(sel.zone)
            selections_affected.append({
                "selection_id": sel.selection_id,
                "state": sel.state.value,
                "reason": _why(sel, affected_holes, renumber_corrections),
            })

    unfinished = [
        h.to_dict() for h in ledger.handoffs
        if h.kind is not WorkKind.COMPLETED and h.hole in affected_holes
    ]
    excavations = [e.to_dict() for e in ledger.excavations.values() if e.zone in zones_affected]
    return {
        "datum_corrections": [c.to_dict() for c in datum_corrections],
        "renumber_corrections": [c.to_dict() for c in renumber_corrections],
        "rejected_corrections": [c.to_dict() for c in ledger.rejected_corrections],
        "affected_holes": affected_holes,
        "affected_sample_mappings": affected_samples,
        "affected_zones": sorted(zones_affected),
        "selections_to_recheck": selections_affected,
        "unfinished_work_segments": unfinished,
        "immutable_excavations": excavations,
        "rule": "深度与已完施工记录不移动；标高按新基准重算，样号只改归属不改深度",
    }


def _why(sel, affected_holes, renumber_corrections) -> list[str]:
    why = []
    if sel.hole in affected_holes:
        why.append("孔口基准已更正，设计标高区间对应的深度/标高需按新版本复核")
    renamed = {c.detail["new_id"]: c.detail["old_id"] for c in renumber_corrections}
    for sid in sel.cited_sample_ids:
        if sid in renamed:
            why.append(f"引用样已由 {renamed[sid]} 重编号为 {sid}，实验归属随更改变更")
    return why


def _merge_elev(intervals: list[ElevationInterval]) -> list[ElevationInterval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: (i.low, i.high))
    out = [ElevationInterval(ordered[0].low, ordered[0].high)]
    for iv in ordered[1:]:
        if iv.low <= out[-1].high:
            out[-1] = ElevationInterval(out[-1].low, max(out[-1].high, iv.high))
        else:
            out.append(ElevationInterval(iv.low, iv.high))
    return out


def _subtract_many(domains: list[ElevationInterval], cov: ElevationInterval) -> list[ElevationInterval]:
    result: list[ElevationInterval] = []
    for d in domains:
        if cov.high <= d.low or cov.low >= d.high:
            result.append(d)
            continue
        if cov.low > d.low:
            result.append(ElevationInterval(d.low, min(cov.low, d.high)))
        if cov.high < d.high:
            result.append(ElevationInterval(max(cov.high, d.low), d.high))
    return result
