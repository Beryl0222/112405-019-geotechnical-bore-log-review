"""领域模型：深度/标高约定、证据状态与事实对象。

约定：
- 深度为孔口向下米数，恒为正；区间采用左闭右开 ``[start, end)``，
  点状事件允许 ``start == end``。
- 标高为统一高程米数，各孔以本孔孔口为基准推算：
  ``标高 = 孔口标高 - 深度``。基准更正只改变推算标高，不改变原文深度。
- 每条事实都携带出处（原文编号 + 原句片段）与证据状态，
  实测、未取到样、观察时段内未见、未确认主张与假设严格分账。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Optional


class EvidenceStatus(str, Enum):
    """证据状态。未知与缺样不是无异常。"""

    MEASURED = "measured"                       # 有出处的实测事实
    NOT_RECOVERED = "not_recovered"             # 未取到样/取样筒空：仅该点未回收
    OBSERVATION_LIMITED = "observation_limited"  # 观察时段内未见，不外延
    UNCONFIRMED = "unconfirmed"                 # 语义不明、地质员未确认的主张
    ASSUMPTION = "assumption"                   # 标明触发条件的保守假设
    DERIVED = "derived"                         # 深度经孔口基准推算的标高
    CORRECTED = "corrected"                     # 经更正（重编号/基准）后的归属


class ObservationKind(str, Enum):
    LAYER_CONTACT = "layer_contact"     # 某深度入/见某层（层位界）
    LAYER_SEEN = "layer_seen"           # 区间内见某层，具体界位未定位
    SAMPLE_TAKEN = "sample_taken"       # 取样登记
    NO_SAMPLE = "no_sample"             # 取样筒空/未取到样（点状事件）
    WATER_SEEN = "water_seen"           # 见水
    WATER_NOT_SEEN = "water_not_seen"   # 观察时段内未见水
    SUBMISSION_CLAIM = "submission_claim"  # 口传/转述的样品号，未绑定登记
    NOTE = "note"                       # 现场附注（如“未记停钻”）


class SelectionState(str, Enum):
    DRAFT = "draft"                       # 草案，未签认
    SIGNED = "signed"                     # 已签认
    REQUIRES_REVIEW = "requires_review"   # 前提被新证据推翻，待重核
    SUPERSEDED = "superseded"             # 被新版本替代


class WorkKind(str, Enum):
    WORKABLE = "workable"            # 可作业区间
    STOP_PENDING = "stop_pending"    # 停待补查区间
    COMPLETED = "completed"          # 已完成施工（记录不可移动）


# 试验类型口径
TEST_UCS = "unconfined_compressive_strength"  # 无侧限抗压强度
TEST_MOISTURE = "moisture_content"            # 含水率

TEST_LABELS = {
    TEST_UCS: "无侧限抗压强度",
    TEST_MOISTURE: "含水率",
}


@dataclass(frozen=True)
class SourceRef:
    """原文出处：资料包记录编号与原句片段。"""

    record_id: str
    quote: str

    def to_dict(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "quote": self.quote}


@dataclass(frozen=True)
class DepthInterval:
    """孔口向下深度区间（米），左闭右开。"""

    start: float
    end: float

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"非法深度区间: {self.start}-{self.end}")

    @property
    def is_point(self) -> bool:
        return self.start == self.end

    def overlaps(self, other: "DepthInterval") -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, depth: float) -> bool:
        return self.start <= depth < self.end or (
            self.is_point and depth == self.start
        )

    def intersection(self, other: "DepthInterval") -> Optional["DepthInterval"]:
        lo, hi = max(self.start, other.start), min(self.end, other.end)
        return DepthInterval(lo, hi) if lo < hi else None

    def to_dict(self) -> dict[str, Any]:
        return {"start_m": self.start, "end_m": self.end}


@dataclass(frozen=True)
class ElevationInterval:
    """统一高程区间（米），low 为底板、high 为顶板。"""

    low: float
    high: float

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError(f"非法标高区间: {self.low}-{self.high}")

    def overlaps(self, other: "ElevationInterval") -> bool:
        return self.low < other.high and other.low < self.high

    def intersection(self, other: "ElevationInterval") -> Optional["ElevationInterval"]:
        lo, hi = max(self.low, other.low), min(self.high, other.high)
        return ElevationInterval(lo, hi) if lo < hi else None

    def to_dict(self) -> dict[str, Any]:
        return {"low_m": self.low, "high_m": self.high}


@dataclass
class Observation:
    """一条现场事实或有限观察。"""

    obs_id: str
    kind: ObservationKind
    hole: Optional[str]
    depth: Optional[DepthInterval]          # 点状事件为 point 区间
    status: EvidenceStatus
    sources: list[SourceRef]
    layer: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)
    observed_until: Optional[str] = None    # 观察覆盖时段终点
    contact_located: bool = True            # 层位界是否定位（续见层位时为 False）

    def to_dict(self) -> dict[str, Any]:
        return {
            "obs_id": self.obs_id,
            "kind": self.kind.value,
            "hole": self.hole,
            "depth": self.depth.to_dict() if self.depth else None,
            "status": self.status.value,
            "layer": self.layer,
            "detail": self.detail,
            "observed_until": self.observed_until,
            "contact_located": self.contact_located,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class TestResult:
    """实验结果。value 为 None 表示原文只登记试验类型、未给数值。"""

    test_type: str
    sample_id: str
    layered: bool
    sources: list[SourceRef]
    value: Optional[float] = None
    unit: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_type": self.test_type,
            "test_label": TEST_LABELS.get(self.test_type, self.test_type),
            "sample_id": self.sample_id,
            "value": self.value,
            "unit": self.unit,
            "layered": self.layered,
            "note": self.note,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class SampleRecord:
    """样品登记。aliases 保存被更正前的旧编号；identity_evidence 记录同袋证据。"""

    sample_id: str
    hole: str
    depth: DepthInterval
    sources: list[SourceRef]
    aliases: list[str] = field(default_factory=list)
    mixed: bool = False
    crossed_layers: list[str] = field(default_factory=list)
    identity_evidence: list[str] = field(default_factory=list)
    distinct_from: list[str] = field(default_factory=list)
    tests: list[TestResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "aliases": self.aliases,
            "hole": self.hole,
            "depth": self.depth.to_dict(),
            "mixed": self.mixed,
            "crossed_layers": self.crossed_layers,
            "identity_evidence": self.identity_evidence,
            "distinct_from": self.distinct_from,
            "tests": [t.to_dict() for t in self.tests],
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class Assumption:
    """保守假设：必须带补查触发条件，且永远不被当作实测。"""

    assumption_id: str
    description: str
    zone: Optional[str]
    trigger: str
    sources: list[SourceRef]
    elevation: Optional[ElevationInterval] = None
    valid: bool = True
    invalidated_by: list[SourceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assumption_id": self.assumption_id,
            "description": self.description,
            "zone": self.zone,
            "elevation": self.elevation.to_dict() if self.elevation else None,
            "trigger": self.trigger,
            "valid": self.valid,
            "invalidated_by": [s.to_dict() for s in self.invalidated_by],
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class ParameterSelection:
    """设计参数选用：证据覆盖、外推前提、假设与重核状态分账。"""

    selection_id: str
    zone: str
    hole: Optional[str]
    parameter: str
    state: SelectionState
    sources: list[SourceRef]
    value: Optional[float] = None
    unit: Optional[str] = None
    cited_sample_ids: list[str] = field(default_factory=list)
    design_elevation: Optional[ElevationInterval] = None
    evidence_depth: Optional[DepthInterval] = None
    evidence_elevation: Optional[ElevationInterval] = None
    extrapolation: list[str] = field(default_factory=list)
    assumption_ids: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_id": self.selection_id,
            "zone": self.zone,
            "hole": self.hole,
            "parameter": self.parameter,
            "value": self.value,
            "unit": self.unit,
            "state": self.state.value,
            "cited_sample_ids": self.cited_sample_ids,
            "design_elevation": (
                self.design_elevation.to_dict() if self.design_elevation else None
            ),
            "evidence_depth": (
                self.evidence_depth.to_dict() if self.evidence_depth else None
            ),
            "evidence_elevation": (
                self.evidence_elevation.to_dict() if self.evidence_elevation else None
            ),
            "extrapolation": self.extrapolation,
            "assumption_ids": self.assumption_ids,
            "triggers": self.triggers,
            "exclusions": self.exclusions,
            "review_notes": self.review_notes,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class HandoffInterval:
    """施工交接区间：可作业 / 停待补查 / 已完成，附监测条件与依据。"""

    handoff_id: str
    zone: str
    hole: Optional[str]
    kind: WorkKind
    reasons: list[str]
    sources: list[SourceRef]
    elevation: Optional[ElevationInterval] = None
    depth: Optional[DepthInterval] = None
    conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "zone": self.zone,
            "hole": self.hole,
            "kind": self.kind.value,
            "elevation": self.elevation.to_dict() if self.elevation else None,
            "depth": self.depth.to_dict() if self.depth else None,
            "conditions": self.conditions,
            "reasons": self.reasons,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class ExcavationEntry:
    """实际开挖登记。记录不可变：基准更正后只追加当前基准等效值，不移动记录。"""

    entry_id: str
    zone: str
    recorded_elevation: float
    datum_version: str
    sources: list[SourceRef]
    note: Optional[str] = None
    current_equivalent_depth: Optional[float] = None
    recorded_equivalent_depth: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "zone": self.zone,
            "recorded_elevation_m": self.recorded_elevation,
            "datum_version": self.datum_version,
            "recorded_equivalent_depth_m": self.recorded_equivalent_depth,
            "current_equivalent_depth_m": self.current_equivalent_depth,
            "note": self.note,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class UnconfirmedClaim:
    """未确认主张：保留原句与不能确认的理由，不生成任何层位事实。"""

    claim_id: str
    record_id: str
    quote: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "record_id": self.record_id,
            "quote": self.quote,
            "reason": self.reason,
        }


@dataclass
class CorrectionRecord:
    """更正事件及其证据充分性、应用结果。"""

    correction_id: str
    kind: str  # "sample_renumber" | "datum"
    applied: bool
    sources: list[SourceRef]
    detail: dict[str, Any]
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "correction_id": self.correction_id,
            "kind": self.kind,
            "applied": self.applied,
            "detail": self.detail,
            "reason": self.reason,
            "sources": [s.to_dict() for s in self.sources],
        }


def to_jsonable(value: Any) -> Any:
    """把 dataclass/Enum 递归转为可 JSON 序列化的普通对象。"""
    if is_dataclass(value):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value
