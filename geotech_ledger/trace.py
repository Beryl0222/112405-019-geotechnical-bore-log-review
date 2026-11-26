"""可追溯查询：任意对象回查实测、假设、未确认与待查依据。"""
from __future__ import annotations

from typing import Optional

from .ledger import Ledger
from .model import EvidenceStatus, ObservationKind


def trace_sample(ledger: Ledger, sample_id: str) -> dict:
    sample = ledger.find_sample(sample_id)
    if sample is None:
        return {"query": sample_id, "found": False,
                "rule": "无登记样号禁止凭邻号猜同袋，只能作为待查项"}
    renumber = [c.to_dict() for c in ledger.corrections
                if c.kind == "sample_renumber" and c.detail.get("new_id") == sample.sample_id]
    return {
        "query": sample_id,
        "found": True,
        "sample": sample.to_dict(),
        "renumber_history": renumber,
        "used_by_selections": [
            s.selection_id for s in ledger.selections.values()
            if sample.sample_id in s.cited_sample_ids
        ],
    }


def trace_depth(ledger: Ledger, hole: str, depth: float) -> dict:
    """某孔某深度处：层位事实、样品、未回收、观察限制、缺样状态。"""
    facts, samples, gaps_flag = [], [], False
    for obs in ledger.observations_for(hole):
        if obs.depth is None:
            continue
        if obs.depth.contains(depth):
            facts.append(obs.to_dict())
            if obs.kind is ObservationKind.NO_SAMPLE:
                gaps_flag = True
    not_recovered_facts = [
        f for f in facts if f["kind"] == ObservationKind.NO_SAMPLE.value
    ]
    for sample in ledger.samples.values():
        if sample.hole == hole and sample.depth.contains(depth):
            samples.append(sample.sample_id)
    elevation = ledger.depth_to_elevation(hole, depth)
    if not facts and not samples:
        status = "unknown_no_record"
        meaning = "该深度无任何记录：未知，不能判为无异常"
    elif gaps_flag and not samples:
        status = EvidenceStatus.NOT_RECOVERED.value
        meaning = "该深度未取到样：未回收，不代表无此层"
    else:
        status = EvidenceStatus.MEASURED.value
        meaning = "有实测记录支撑"
    if gaps_flag:
        # 即便后补样品覆盖该深度，原始“未回收”事实仍独立保留、不被抹掉
        meaning += "；同时存在未取到样记录（未回收事实独立保留，不代表无此层）"
    return {
        "hole": hole, "depth_m": depth,
        "elevation_m_current_datum": elevation,
        "datum_version": ledger.datum_version(hole),
        "status": status, "meaning": meaning,
        "has_not_recovered_event": gaps_flag,
        "not_recovered_facts": not_recovered_facts,
        "facts": facts, "sample_ids": samples,
    }


def trace_elevation(ledger: Ledger, hole: str, elevation: float) -> dict:
    depth = ledger.elevation_to_depth(hole, elevation)
    result = trace_depth(ledger, hole, depth)
    result["query_elevation_m"] = elevation
    result["equivalent_depth_m"] = depth
    return result


def trace_zone(ledger: Ledger, zone_id: str, zone_spec) -> dict:
    holes = zone_spec.holes
    out = {"zone": zone_id, "target_elevation": zone_spec.target_elevation.to_dict(), "holes": {}}
    for hole in holes:
        depth_domain = ledger.elevation_interval_to_depth(hole, zone_spec.target_elevation)
        from .geology import evidence_coverage

        coverage = evidence_coverage(ledger, hole, depth_domain)
        out["holes"][hole] = {
            "depth_domain": depth_domain.to_dict(),
            "coverage": coverage,
            "selections": [s.to_dict() for s in ledger.selections.values() if s.zone == zone_id],
            "handoffs": [h.to_dict() for h in ledger.handoffs if h.zone == zone_id],
            "excavations": [e.to_dict() for e in ledger.excavations.values() if e.zone == zone_id],
        }
    out["unconfirmed_claims"] = [c.to_dict() for c in ledger.claims]
    out["assumptions"] = [a.to_dict() for a in ledger.assumptions.values() if a.zone == zone_id]
    return out


def pending_investigations(ledger: Ledger) -> list[dict]:
    """全部待查项：未确认主张、未回收点、假设触发、重核选用、缺样区。"""
    items: list[dict] = []
    for claim in ledger.claims:
        items.append({"type": "unconfirmed_claim", **claim.to_dict()})
    for obs in ledger.observations:
        if obs.kind is ObservationKind.NO_SAMPLE:
            items.append({"type": "not_recovered", "obs_id": obs.obs_id,
                          "hole": obs.hole,
                          "depth": obs.depth.to_dict() if obs.depth else None,
                          "sources": [s.to_dict() for s in obs.sources]})
        if obs.kind is ObservationKind.SUBMISSION_CLAIM:
            items.append({"type": "submission_claim", "obs_id": obs.obs_id,
                          "detail": obs.detail,
                          "sources": [s.to_dict() for s in obs.sources]})
    for aid, assumption in ledger.assumptions.items():
        items.append({
            "type": "assumption", "assumption_id": aid,
            "valid": assumption.valid, "trigger": assumption.trigger,
            "invalidated_by": [s.to_dict() for s in assumption.invalidated_by],
        })
    for sel in ledger.selections.values():
        if sel.state.value in ("requires_review", "draft"):
            items.append({"type": f"selection_{sel.state.value}",
                          "selection_id": sel.selection_id,
                          "review_notes": sel.review_notes,
                          "triggers": sel.triggers})
    for corr in ledger.rejected_corrections:
        items.append({"type": "rejected_correction", **corr.to_dict()})
    return items


def event_lineage(ledger: Ledger) -> list[dict]:
    return [
        {"seq": e["seq"], "kind": e["kind"],
         "source": _event_source(e)}
        for e in ledger.events
    ]


def _event_source(event: dict) -> Optional[dict]:
    p = event["payload"]
    for key in ("source",):
        if key in p:
            return p[key]
    if "sources" in p and p["sources"]:
        return p["sources"][0]
    if event["kind"] == "claim_rejected":
        return {"record_id": p["claim"]["record_id"], "quote": p["claim"]["quote"]}
    return None
