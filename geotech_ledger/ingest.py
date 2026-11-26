"""录入编排：解析条款 → 台账事件，集中落实业务规则。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .ledger import Ledger
from .model import (
    CorrectionRecord,
    DepthInterval,
    ElevationInterval,
    EvidenceStatus,
    Observation,
    ObservationKind,
    SourceRef,
)
from .parser import Clause, ClauseKind, parse_case


class IngestionError(ValueError):
    """录入冲突或规则违例（如同孔续尺找不到锚点、邻孔借用）。"""


@dataclass
class IngestionReport:
    record_ids: list[str]
    correction_count: int
    rejected_correction_count: int
    unconfirmed_count: int
    event_count: int
    warnings: list[str]


def ingest_case(case: dict, ledger: Ledger) -> IngestionReport:
    warnings: list[str] = []
    parsed = parse_case(case["records"])

    # 1. 对象登记中的孔（孔口基准 D0）
    for hole in case["objects"].get("holes", ()):
        ledger.emit("hole_registered", {
            "hole": hole["id"],
            "collar_elevation": hole["mouth_elevation"],
            "source": SourceRef("OBJECTS", f"objects.holes:{hole['id']}").to_dict(),
        })

    # 2. 对象登记中的样品（原文登记仍以叙述出处为准，这里做结构化对齐）
    registered_text_samples: set[str] = set()
    for sample in case["objects"].get("samples", ()):
        _register_sample(
            ledger,
            sample_id=sample["id"], hole=sample["hole"],
            depth=DepthInterval(sample["depth"][0], sample["depth"][1]),
            source=SourceRef("OBJECTS", f"objects.samples:{sample['id']}"),
        )

    # 3. 逐记录处理
    for record in case["records"]:
        result = parsed[record["id"]]
        for claim in result.unconfirmed:
            ledger.emit("claim_rejected", {"claim": claim.to_dict()})
        for clause in result.clauses:
            _handle_clause(clause, ledger, registered_text_samples, warnings)

    # 4. 结构化登记与叙述登记一致性核对
    text_ids = registered_text_samples
    object_ids = {s["id"] for s in case["objects"].get("samples", ())}
    for sid in sorted(text_ids - object_ids):
        warnings.append(f"样品 {sid} 仅见于原文叙述，结构化登记缺失，已按原文建账待核对")

    correction_count = len(ledger.corrections)
    return IngestionReport(
        record_ids=[r["id"] for r in case["records"]],
        correction_count=correction_count,
        rejected_correction_count=len(ledger.rejected_corrections),
        unconfirmed_count=len(ledger.claims),
        event_count=len(ledger.events),
        warnings=warnings,
    )


# ----------------------------------------------------------------- 条款处理
def _handle_clause(
    clause: Clause,
    ledger: Ledger,
    registered_text_samples: set[str],
    warnings: list[str],
) -> None:
    k = clause.kind
    src = clause.source()

    if k is ClauseKind.COLLAR:
        if clause.hole not in ledger.holes:
            ledger.emit("hole_registered", {
                "hole": clause.hole,
                "collar_elevation": clause.collar_elevation,
                "source": src.to_dict(),
            })
        # 与对象登记不一致仅提示，更正须走 datum_correction
        elif abs(ledger.collar(clause.hole) - clause.collar_elevation) > 1e-9:
            warnings.append(f"{clause.hole} 叙述孔口标高与登记不一致，等待更正证据")

    elif k is ClauseKind.LAYER_ENTRY:
        _need(clause, "hole", clause.hole, src)
        _need(clause, "depth", clause.depth, src)
        ledger.emit("observation_added", Observation(
            obs_id=f"OBS-{clause.clause_id}",
            kind=ObservationKind.LAYER_CONTACT,
            hole=clause.hole, depth=clause.depth,
            status=EvidenceStatus.MEASURED, sources=[src],
            layer=clause.layer, contact_located=True,
        ).to_dict())

    elif k is ClauseKind.LAYER_SEEN:
        _need(clause, "hole", clause.hole, src)
        _need(clause, "depth", clause.depth, src)
        ledger.emit("observation_added", Observation(
            obs_id=f"OBS-{clause.clause_id}",
            kind=ObservationKind.LAYER_SEEN,
            hole=clause.hole, depth=clause.depth,
            status=EvidenceStatus.MEASURED, sources=[src],
            layer=clause.layer, contact_located=False,
        ).to_dict())

    elif k is ClauseKind.EMPTY_SAMPLER:
        # 未取到样是点状“未回收”，绝不解释为无此层
        _need(clause, "hole", clause.hole, src)
        ledger.emit("observation_added", Observation(
            obs_id=f"OBS-{clause.clause_id}",
            kind=ObservationKind.NO_SAMPLE,
            hole=clause.hole, depth=clause.depth,
            status=EvidenceStatus.NOT_RECOVERED, sources=[src],
            detail={"meaning": "该点未回收岩芯/取样筒空，不代表无此层或无异常"},
        ).to_dict())

    elif k is ClauseKind.CONTINUE:
        _apply_continue(clause, ledger)

    elif k in (ClauseKind.WATER_SEEN, ClauseKind.WATER_NOT_SEEN):
        _need(clause, "hole", clause.hole, src)
        kind = ObservationKind.WATER_SEEN if k is ClauseKind.WATER_SEEN else ObservationKind.WATER_NOT_SEEN
        status = EvidenceStatus.MEASURED if kind is ObservationKind.WATER_SEEN else EvidenceStatus.OBSERVATION_LIMITED
        detail = {}
        if kind is ObservationKind.WATER_NOT_SEEN:
            detail["meaning"] = "仅覆盖观察时段，不外推为永久无水"
        ledger.emit("observation_added", Observation(
            obs_id=f"OBS-{clause.clause_id}",
            kind=kind, hole=clause.hole, depth=clause.depth,
            status=status, sources=[src],
            observed_until=clause.observed_until, detail=detail,
        ).to_dict())

    elif k is ClauseKind.SAMPLE_REGISTER:
        _need(clause, "hole", clause.hole, src)
        _need(clause, "depth", clause.depth, src)
        _register_sample(
            ledger,
            sample_id=clause.sample_id, hole=clause.hole, depth=clause.depth,
            source=src,
            mixed=bool(clause.mixed), crossed_layers=list(clause.crossed_layers),
        )
        registered_text_samples.add(clause.sample_id)

    elif k is ClauseKind.SUPPLEMENT_SAMPLE:
        _need(clause, "hole", clause.hole, src)
        _need(clause, "depth", clause.depth, src)
        _register_sample(
            ledger,
            sample_id=clause.sample_id, hole=clause.hole, depth=clause.depth,
            source=src,
            distinct_from=list(clause.distinct_from),
        )
        registered_text_samples.add(clause.sample_id)
        if clause.layer:
            ledger.emit("observation_added", Observation(
                obs_id=f"OBS-{clause.clause_id}",
                kind=ObservationKind.LAYER_SEEN, hole=clause.hole,
                depth=clause.depth, status=EvidenceStatus.MEASURED,
                sources=[src], layer=clause.layer, contact_located=False,
                detail={"via": "supplement_sample"},
            ).to_dict())

    elif k is ClauseKind.LAB_TEST:
        _need(clause, "sample_id", clause.sample_id, src)
        sample = ledger.find_sample(clause.sample_id)
        if sample is None:
            raise IngestionError(f"{src.record_id}: 实验结果归属样 {clause.sample_id} 无登记，禁止凭邻号猜测")
        if getattr(clause, "merge_into", None):
            ledger.emit("test_annotated", {
                "sample_id": sample.sample_id,
                "test_type": clause.test_type,
                "layered": False,
                "note": clause.note or "混合样未分层试验",
                "source": src.to_dict(),
            })
            return
        ledger.emit("test_added", {
            "sample_id": sample.sample_id,
            "test": {
                "test_type": clause.test_type or "unconfined_compressive_strength",
                "sample_id": sample.sample_id,
                "value": clause.test_value,
                "unit": clause.test_unit,
                "layered": bool(clause.layered),
                "note": clause.note,
                "sources": [src.to_dict()],
            },
        })

    elif k is ClauseKind.RENUMBER:
        _apply_renumber(clause, ledger)

    elif k is ClauseKind.DATUM_CORRECTION:
        _apply_datum(clause, ledger)

    elif k is ClauseKind.SUBMISSION_CLAIM:
        ledger.emit("observation_added", Observation(
            obs_id=f"OBS-{clause.clause_id}",
            kind=ObservationKind.SUBMISSION_CLAIM, hole=clause.hole,
            depth=None, status=EvidenceStatus.UNCONFIRMED, sources=[src],
            detail=dict(clause.extra, note=clause.note),
        ).to_dict())

    elif k is ClauseKind.NOTE:
        ledger.emit("observation_added", Observation(
            obs_id=f"OBS-{clause.clause_id}",
            kind=ObservationKind.NOTE, hole=clause.hole,
            depth=None, status=EvidenceStatus.MEASURED, sources=[src],
            detail={"note": clause.note},
        ).to_dict())

    elif k in (ClauseKind.DESIGN_SELECTION, ClauseKind.ZONE_SELECTION,
               ClauseKind.ASSUMPTION, ClauseKind.TRIGGER,
               ClauseKind.CONSTRUCTION_EXCAVATION):
        # 设计与施工类条款由各自领域模块消费，这里保留可追溯事件
        ledger.emit("observation_added", Observation(
            obs_id=f"OBS-{clause.clause_id}",
            kind=ObservationKind.NOTE, hole=clause.hole, depth=None,
            status=EvidenceStatus.MEASURED, sources=[src],
            detail={"deferred": k.value, **clause.extra},
        ).to_dict())
    else:
        warnings.append(f"{src.record_id}: 条款未识别类型 {k.value}: {clause.quote}")


def _apply_continue(clause: Clause, ledger: Ledger) -> None:
    """“继续两米”只能接同孔上次确认进尺；无锚点或想借邻孔一律拒绝。"""
    src = clause.source()
    _need(clause, "hole", clause.hole, src)
    anchor: Optional[DepthInterval] = None
    for obs in reversed(ledger.observations):
        if obs.hole == clause.hole and obs.depth is not None:
            anchor = obs.depth
            break
    if anchor is None:
        raise IngestionError(
            f"{src.record_id}: “{clause.quote}” 找不到同孔 {clause.hole} 上次确认进尺，"
            "不能接邻孔或页码"
        )
    depth = DepthInterval(anchor.end, anchor.end + clause.amount)
    ledger.emit("observation_added", Observation(
        obs_id=f"OBS-{clause.clause_id}",
        kind=ObservationKind.LAYER_SEEN, hole=clause.hole, depth=depth,
        status=EvidenceStatus.MEASURED, sources=[src],
        layer=clause.layer, contact_located=False,
        detail={"continued_from": anchor.to_dict(), "rule": "续尺只接同孔上次确认进尺"},
    ).to_dict())


def _apply_renumber(clause: Clause, ledger: Ledger) -> None:
    src = clause.source()
    old, new = clause.old_sample_id, clause.sample_id
    old_sample = ledger.find_sample(old)
    if old_sample is None:
        ledger.emit("correction_rejected", CorrectionRecord(
            correction_id=f"CORR-{clause.clause_id}", kind="sample_renumber",
            applied=False, sources=[src],
            detail={"old_id": old, "new_id": new},
            reason="旧样号无登记，不能凭邻号猜同袋样本",
        ).to_dict())
        return
    if not clause.evidence:
        ledger.emit("correction_rejected", CorrectionRecord(
            correction_id=f"CORR-{clause.clause_id}", kind="sample_renumber",
            applied=False, sources=[src],
            detail={"old_id": old, "new_id": new},
            reason="缺封签/接收单等同袋证据，重编号不予应用",
        ).to_dict())
        return
    # 深度不变，只改归属；实验结果随样品改挂
    ledger.emit("sample_renumbered", {
        "old_id": old, "new_id": new,
        "identity_evidence": clause.evidence,
        "source": src.to_dict(),
    })
    ledger.emit("correction_recorded", CorrectionRecord(
        correction_id=f"CORR-{clause.clause_id}", kind="sample_renumber",
        applied=True, sources=[src],
        detail={"old_id": old, "new_id": new, "identity_evidence": clause.evidence,
                "depth_unchanged": True},
        reason="同袋证据充分，实验归属随样号更正",
    ).to_dict())


def _apply_datum(clause: Clause, ledger: Ledger) -> None:
    src = clause.source()
    if not clause.hole or clause.collar_elevation is None:
        raise IngestionError(f"{src.record_id}: 基准更正缺少孔号或新孔口标高")
    if clause.hole not in ledger.holes:
        raise IngestionError(f"{src.record_id}: 孔 {clause.hole} 未登记，不能更正基准")
    ledger.emit("collar_corrected", {
        "hole": clause.hole,
        "new_collar_elevation": clause.collar_elevation,
        "reason": clause.reason,
        "source": src.to_dict(),
    })
    ledger.emit("correction_recorded", CorrectionRecord(
        correction_id=f"CORR-{clause.clause_id}", kind="datum",
        applied=True, sources=[src],
        detail={
            "hole": clause.hole,
            "new_collar_elevation": clause.collar_elevation,
            "datum_version": ledger.datum_version(clause.hole),
            "depth_unchanged": True,
            "other_hole_unchanged": clause.extra.get("other_hole_unchanged", False),
        },
        reason="孔口原抄错；只改推算标高，原文深度与已完施工记录不移动",
    ).to_dict())


def _register_sample(
    ledger: Ledger, *, sample_id: str, hole: str, depth: DepthInterval,
    source: SourceRef, mixed: bool = False,
    crossed_layers: Optional[list[str]] = None,
    distinct_from: Optional[list[str]] = None,
) -> None:
    existing = ledger.find_sample(sample_id)
    if existing is not None:
        if existing.hole != hole or existing.depth.start != depth.start or existing.depth.end != depth.end:
            raise IngestionError(
                f"样品 {sample_id} 重复登记且孔号/深度冲突：{existing.hole} {existing.depth} vs {hole} {depth}"
            )
        # 同一对象的叙述补充属性（如混合样跨层）以事件合并，保证重放一致
        ledger.emit("sample_enriched", {
            "sample_id": existing.sample_id,
            "mixed": mixed,
            "crossed_layers": crossed_layers or [],
            "distinct_from": distinct_from or [],
            "source": source.to_dict(),
        })
        return
    ledger.emit("sample_registered", {
        "sample_id": sample_id, "hole": hole,
        "depth": depth.to_dict(),
        "mixed": mixed,
        "crossed_layers": crossed_layers or [],
        "distinct_from": distinct_from or [],
        "sources": [source.to_dict()],
    })


def _need(clause: Clause, name: str, value: Any, src: SourceRef) -> None:
    if value is None:
        raise IngestionError(f"{src.record_id}: 条款“{clause.quote}”缺少 {name}，按规则不补造")
