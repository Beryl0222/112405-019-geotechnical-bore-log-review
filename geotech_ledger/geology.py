"""层位事实确认与证据覆盖分析。

把台账中按孔的观察整理为层位段，区分：
- 已定位层位界（contact）与仅“区间内见层”（界位未定位）；
- 已取样支撑、未回收点（NOT_RECOVERED）、观察时段有限（OBSERVATION_LIMITED）；
- 缺样区间。缺样只表达“没有实测证据”，不得解释为均一或无异常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ledger import Ledger
from .model import (
    DepthInterval,
    ElevationInterval,
    EvidenceStatus,
    Observation,
    ObservationKind,
    SampleRecord,
)

LAYER_MATERIAL = "material"          # 有层位观察的事实段
UNSAMPLED = "unsampled"              # 缺样区间（无实测，不是无异常）
NOT_RECOVERED = "not_recovered"      # 未回收点
LIMITED_OBSERVATION = "limited"      # 观察时段有限的事实段
UNKNOWN = "unknown"                  # 无任何记录的未知段


@dataclass
class LayerSegment:
    hole: str
    depth: DepthInterval
    elevation: ElevationInterval
    classification: str
    layers: list[str] = field(default_factory=list)
    contact_located: bool = False
    evidence_ids: list[str] = field(default_factory=list)
    sample_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hole": self.hole,
            "depth": self.depth.to_dict(),
            "elevation": self.elevation.to_dict(),
            "classification": self.classification,
            "layers": self.layers,
            "contact_located": self.contact_located,
            "evidence_ids": self.evidence_ids,
            "sample_ids": self.sample_ids,
            "notes": self.notes,
        }


@dataclass
class Gap:
    """缺样/证据缺口。status 描述缺口性质，绝不写成“无异常”。"""

    hole: str
    depth: DepthInterval
    elevation: ElevationInterval
    status: str
    reason: str
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hole": self.hole,
            "depth": self.depth.to_dict(),
            "elevation": self.elevation.to_dict(),
            "status": self.status,
            "reason": self.reason,
            "evidence_ids": self.evidence_ids,
        }


def _merge(intervals: list[DepthInterval]) -> list[DepthInterval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: (i.start, i.end))
    out = [DepthInterval(ordered[0].start, ordered[0].end)]
    for iv in ordered[1:]:
        if iv.start <= out[-1].end:
            out[-1] = DepthInterval(out[-1].start, max(out[-1].end, iv.end))
        else:
            out.append(DepthInterval(iv.start, iv.end))
    return out


def _subtract(domain: DepthInterval, covered: list[DepthInterval]) -> list[DepthInterval]:
    remain = [domain]
    for cov in _merge(covered):
        nxt: list[DepthInterval] = []
        for seg in remain:
            if cov.end <= seg.start or cov.start >= seg.end:
                nxt.append(seg)
                continue
            if cov.start > seg.start:
                nxt.append(DepthInterval(seg.start, min(cov.start, seg.end)))
            if cov.end < seg.end:
                nxt.append(DepthInterval(max(cov.end, seg.start), seg.end))
        remain = nxt
    return remain


def confirmed_layers(ledger: Ledger, hole: str, clip_to: Optional[DepthInterval] = None) -> list[LayerSegment]:
    """整理同孔层位段，按深度排序，标注界位是否定位。

    clip_to 给定时只保留与目标域相交的段，点层位界落在域外的保留为域外参照
    （不裁剪点），避免把目标域顶板以上的关键事实（如硬层界、未回收点）整段丢失。
    """
    segments: list[LayerSegment] = []
    for obs in ledger.observations_for(hole):
        if obs.kind not in (ObservationKind.LAYER_CONTACT, ObservationKind.LAYER_SEEN):
            continue
        if obs.depth is None or not obs.layer:
            continue
        depth = obs.depth
        outside_note = ""
        if clip_to is not None:
            if obs.depth.is_point and not clip_to.contains(obs.depth.start):
                outside_note = "域外参照点，不在目标区间内"
            elif not obs.depth.is_point:
                depth = obs.depth.intersection(clip_to)
                if depth is None:
                    continue
                if depth != obs.depth:
                    outside_note = "区间观察已裁剪到目标域，界位仍未定位"
        elev = ledger.depth_interval_to_elevation(hole, depth)
        segments.append(LayerSegment(
            hole=hole, depth=depth, elevation=elev,
            classification=LAYER_MATERIAL,
            layers=[obs.layer],
            contact_located=(obs.kind is ObservationKind.LAYER_CONTACT),
            evidence_ids=[obs.obs_id],
            notes=[outside_note] if outside_note else [],
        ))
    return sorted(segments, key=lambda s: s.depth.start)


def samples_for_hole(ledger: Ledger, hole: str) -> list[SampleRecord]:
    return sorted(
        (s for s in ledger.samples.values() if s.hole == hole),
        key=lambda s: s.depth.start,
    )


def evidence_coverage(
    ledger: Ledger, hole: str, domain: DepthInterval
) -> dict:
    """在给定深度域内统计实测覆盖与缺样区间。"""
    sample_intervals = _merge([s.depth for s in samples_for_hole(ledger, hole) if s.depth.overlaps(domain)])
    layer_intervals = _merge([
        o.depth for o in ledger.observations_for(hole)
        if o.depth and o.kind in (ObservationKind.LAYER_CONTACT, ObservationKind.LAYER_SEEN)
        and o.depth.overlaps(domain)
    ])
    measured = _merge(sample_intervals + layer_intervals)

    gaps_raw = _subtract(domain, measured)
    gaps: list[Gap] = []
    not_recovered = [
        o for o in ledger.observations_for(hole)
        if o.kind is ObservationKind.NO_SAMPLE and o.depth and o.depth.is_point
    ]
    limited = [
        o for o in ledger.observations_for(hole)
        if o.status is EvidenceStatus.OBSERVATION_LIMITED and o.depth
    ]
    for iv in gaps_raw:
        hit = next((o for o in not_recovered if iv.contains(o.depth.start)), None)
        if hit:
            gaps.append(Gap(hole, iv, ledger.depth_interval_to_elevation(hole, iv),
                            NOT_RECOVERED,
                            "该深度取样筒空/未取到样：未回收，不代表无此层或无异常",
                            [hit.obs_id]))
        else:
            gaps.append(Gap(hole, iv, ledger.depth_interval_to_elevation(hole, iv),
                            UNSAMPLED, "缺样区：无实测证据，不能当均一硬层或无异常"))
    for o in limited:
        if domain.overlaps(o.depth):
            gaps.append(Gap(
                hole, o.depth, ledger.depth_interval_to_elevation(hole, o.depth),
                LIMITED_OBSERVATION,
                f"未见水仅覆盖观察时段（至 {o.observed_until}），不外推",
                [o.obs_id],
            ))
    return {
        "hole": hole,
        "domain": domain.to_dict(),
        "measured_intervals": [iv.to_dict() for iv in measured],
        "sample_intervals": [iv.to_dict() for iv in sample_intervals],
        "gaps": [g.to_dict() for g in gaps],
    }


def find_observation(ledger: Ledger, obs_id: str) -> Optional[Observation]:
    return next((o for o in ledger.observations if o.obs_id == obs_id), None)


def unconfirmed_claims(ledger: Ledger) -> list[dict]:
    return [c.to_dict() for c in ledger.claims]
