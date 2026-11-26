"""中文钻探叙述的规则解析器。

解析器只做确定性的分句与模式抽取，不做跨记录推断：
- 孔号、深度、标高、样号、层位、观察性质按规则提取；
- 省略句继承同一记录内上一句已确认的孔号，绝不借用邻孔或页码；
- “继续 N 米”只产出待解析续尺条款，由台账挂接同孔上次确认进尺；
- 语义不明（如“六米上没有”“六米以上未取到样”）进入未确认主张，
  不生成任何深度/层位事实。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .model import DepthInterval, ElevationInterval, SourceRef, UnconfirmedClaim

METER_UNITS = r"(?:m|M|米|公尺)"
HOLE_RE = re.compile(r"K\d+")
ZONE_RE = re.compile(r"Z\d+(?:-[A-Z])?")
SAMPLE_RE = re.compile(r"S\d+")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
CN_NUM = {"一": 1.0, "二": 2.0, "两": 2.0, "三": 3.0, "四": 4.0, "五": 5.0, "六": 6.0}


class ClauseKind(str, Enum):
    COLLAR = "collar"                       # 孔口标高登记
    LAYER_ENTRY = "layer_entry"             # 某深度入某层（层位界）
    LAYER_SEEN = "layer_seen"               # 区间内见某层（界位未定位）
    EMPTY_SAMPLER = "empty_sampler"         # 取样筒空/未取到样（点状）
    CONTINUE = "continue"                   # 继续 N 米（待同孔挂接）
    WATER_SEEN = "water_seen"
    WATER_NOT_SEEN = "water_not_seen"
    SAMPLE_REGISTER = "sample_register"
    LAB_TEST = "lab_test"
    RENUMBER = "renumber"
    DATUM_CORRECTION = "datum_correction"
    DESIGN_SELECTION = "design_selection"
    ZONE_SELECTION = "zone_selection"
    ASSUMPTION = "assumption"
    TRIGGER = "trigger"
    CONSTRUCTION_EXCAVATION = "construction_excavation"
    SUPPLEMENT_SAMPLE = "supplement_sample"
    SUBMISSION_CLAIM = "submission_claim"
    NOTE = "note"
    UNCONFIRMED = "unconfirmed"
    UNPARSED = "unparsed"


@dataclass
class Clause:
    clause_id: str
    record_id: str
    quote: str
    kind: ClauseKind
    hole: Optional[str] = None
    zone: Optional[str] = None
    depth: Optional[DepthInterval] = None
    point_depth: Optional[float] = None
    collar_elevation: Optional[float] = None
    elev_interval: Optional[ElevationInterval] = None
    elevation: Optional[float] = None
    layer: Optional[str] = None
    sample_id: Optional[str] = None
    old_sample_id: Optional[str] = None
    amount: Optional[float] = None
    test_type: Optional[str] = None
    test_value: Optional[float] = None
    test_unit: Optional[str] = None
    layered: Optional[bool] = None
    mixed: Optional[bool] = None
    crossed_layers: list[str] = field(default_factory=list)
    observed_until: Optional[str] = None
    description: Optional[str] = None
    trigger: Optional[str] = None
    evidence: list[str] = field(default_factory=list)
    distinct_from: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    note: Optional[str] = None
    merge_into: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def source(self) -> SourceRef:
        return SourceRef(self.record_id, self.quote)


@dataclass
class ParseResult:
    record_id: str
    clauses: list[Clause] = field(default_factory=list)
    unconfirmed: list[UnconfirmedClaim] = field(default_factory=list)


def _to_number(token: str) -> float:
    return float(token) if re.fullmatch(r"\d+(?:\.\d+)?", token) else CN_NUM[token]


def _find_hole(text: str) -> Optional[str]:
    m = HOLE_RE.search(text)
    return m.group(0) if m else None


def _find_zone(text: str) -> Optional[str]:
    m = ZONE_RE.search(text)
    return m.group(0) if m else None


def _find_sample(text: str) -> Optional[str]:
    m = SAMPLE_RE.search(text)
    return m.group(0) if m else None


def _depth_interval(text: str) -> Optional[DepthInterval]:
    """匹配 深5.6-6.2m / 深5.6—6.2米 这类显式区间。"""
    m = re.search(r"深\s*(\d+(?:\.\d+)?)\s*[-—~至]\s*(\d+(?:\.\d+)?)\s*" + METER_UNITS + r"?", text)
    if m:
        return DepthInterval(float(m.group(1)), float(m.group(2)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-—~]\s*(\d+(?:\.\d+)?)\s*" + METER_UNITS, text)
    if m:
        return DepthInterval(float(m.group(1)), float(m.group(2)))
    return None


def _point_depth(text: str) -> Optional[float]:
    """匹配单点深度（米）。含“至N m”时优先取“至”后的终点深度。"""
    m = re.search(r"至\s*(\d+(?:\.\d+)?)\s*" + METER_UNITS + r"?", text)
    if m:
        return float(m.group(1))
    m = re.search(r"深\s*(\d+(?:\.\d+)?)\s*" + METER_UNITS + r"?", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*" + METER_UNITS + r"?\s*(?:入|见水|见|取样)", text)
    if m:
        return float(m.group(1))
    return None


def _elev_interval(text: str) -> Optional[ElevationInterval]:
    m = re.search(r"高程\s*(\d+(?:\.\d+)?)\s*[-—~]\s*(\d+(?:\.\d+)?)", text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return ElevationInterval(min(a, b), max(a, b))
    return None


class _ClauseParser:
    def __init__(self, record_id: str, text: str):
        self.record_id = record_id
        self.text = text
        self.last_hole: Optional[str] = None
        self.last_sample: Optional[str] = None
        self.last_zone: Optional[str] = None
        self.lab_tests: list[Clause] = []
        self.seq = 0

    def _prior_lab_tests(self) -> list[Clause]:
        return self.lab_tests

    def parse(self) -> ParseResult:
        result = ParseResult(self.record_id)
        sentences = [p.strip() for p in re.split(r"[。；;]", self.text) if p.strip()]
        for sentence in sentences:
            # 未确认主张整句隔离，不再切成多条
            if "司机说" in sentence or "地质员未确认" in sentence:
                result.unconfirmed.append(self._whole_sentence_claim(sentence))
                continue
            # 更正/补样/基准/设计说明的关键修饰由逗号承载，整句解析
            if self._is_whole_sentence_statement(sentence):
                self.seq += 1
                clause = self._parse_piece(sentence)
                if clause.kind is ClauseKind.UNCONFIRMED:
                    result.unconfirmed.append(UnconfirmedClaim(
                        claim_id=f"{self.record_id}-U{len(result.unconfirmed) + 1}",
                        record_id=self.record_id, quote=sentence,
                        reason=clause.reason or "语义未确认"))
                elif clause.kind is ClauseKind.DESIGN_SELECTION:
                    for sub in self._parse_design_statement(sentence):
                        result.clauses.append(sub)
                else:
                    result.clauses.append(clause)
                continue
            for piece in [p.strip() for p in sentence.split("，") if p.strip()]:
                self.seq += 1
                clause = self._parse_piece(piece)
                if clause.kind is ClauseKind.UNCONFIRMED:
                    result.unconfirmed.append(UnconfirmedClaim(
                        claim_id=f"{self.record_id}-U{len(result.unconfirmed) + 1}",
                        record_id=self.record_id, quote=piece,
                        reason=clause.reason or "语义未确认"))
                    continue
                result.clauses.append(clause)
        self._merge_context(result)
        return result

    @staticmethod
    def _is_whole_sentence_statement(sentence: str) -> bool:
        return any(k in sentence for k in ("应为", "孔口应", "补样", "拟用"))

    def _whole_sentence_claim(self, sentence: str) -> UnconfirmedClaim:
        m = re.search(r"未说明([^，。；]*)", sentence)
        reason = (
            f"未说明{m.group(1)}，且地质员未确认，不能生成层位/深度事实"
            if m else "地质员未确认，转述不能当独立观察"
        )
        return UnconfirmedClaim(
            claim_id=f"{self.record_id}-U{self._claim_seq()}",
            record_id=self.record_id, quote=sentence, reason=reason,
        )

    def _claim_seq(self) -> int:
        return self.seq

    def _merge_context(self, result: ParseResult) -> None:
        """同记录内的省略句合并：观察时段挂回见水句，不跨记录/跨孔。"""
        last_water: Optional[Clause] = None
        kept: list[Clause] = []
        for clause in result.clauses:
            if clause.kind in (ClauseKind.WATER_SEEN, ClauseKind.WATER_NOT_SEEN):
                last_water = clause
                kept.append(clause)
                continue
            if clause.kind is ClauseKind.NOTE and clause.observed_until and last_water is not None:
                if clause.hole in (None, last_water.hole):
                    last_water.observed_until = clause.observed_until
                    continue
            kept.append(clause)
        result.clauses = kept

    def _cid(self) -> str:
        return f"{self.record_id}-C{self.seq}"

    def _base(self, piece: str, kind: ClauseKind) -> Clause:
        hole = _find_hole(piece)
        if hole:
            self.last_hole = hole
        sample = _find_sample(piece)
        if sample:
            self.last_sample = sample
        zone = _find_zone(piece)
        if zone:
            self.last_zone = zone
        return Clause(
            clause_id=self._cid(),
            record_id=self.record_id,
            quote=piece,
            kind=kind,
            hole=hole or self.last_hole,
        )

    def _parse_design_statement(self, piece: str) -> list[Clause]:
        """把一条设计选用说明拆成选用、假设、触发、签认四类子条款。"""
        out: list[Clause] = []
        zone_token = _find_zone(piece)  # 如 Z2（全区）或 Z2-A
        zone = zone_token
        if zone_token and "-" not in zone_token:
            # “Z2全区”不是具体分区，解析阶段保留族号，由流水线展开到登记分区
            zone = zone_token

        # 10.1 选用主体
        self.seq += 1
        sel = Clause(
            clause_id=self._cid(), record_id=self.record_id,
            quote=piece, kind=ClauseKind.DESIGN_SELECTION,
            hole=_find_hole(piece), zone=zone,
            sample_id=_find_sample(piece),
            elev_interval=_elev_interval(piece),
        )
        m = re.search(r"(\d+(?:\.\d+)?)\s*(MPa|kPa|Pa)", piece)
        if m:
            sel.test_value, sel.test_unit = float(m.group(1)), m.group(2)
        m = re.search(r"(硬层|软层|黏土层|灰黏土)", piece)
        if m:
            sel.layer = m.group(1)
        out.append(sel)

        # 10.2 缺样区假设
        m = re.search(r"(缺样区[^，。；]*假设[^，。；]*)", piece)
        if m:
            self.seq += 1
            asm = Clause(
                clause_id=self._cid(), record_id=self.record_id,
                quote=m.group(1), kind=ClauseKind.ASSUMPTION,
                hole=sel.hole, zone=zone,
                elev_interval=sel.elev_interval,
                description=m.group(1),
            )
            out.append(asm)

        # 10.3 补查触发
        m = re.search(r"([^，。；]*停待[^，。；]*)", piece)
        if m:
            self.seq += 1
            trig = Clause(
                clause_id=self._cid(), record_id=self.record_id,
                quote=m.group(1), kind=ClauseKind.TRIGGER,
                hole=sel.hole, zone=zone, trigger=m.group(1),
            )
            out.append(trig)

        # 10.4 签认状态
        if "未签认" in piece:
            self.seq += 1
            out.append(Clause(
                clause_id=self._cid(), record_id=self.record_id,
                quote="未签认", kind=ClauseKind.NOTE,
                hole=sel.hole, zone=zone, note="未签认（草案）",
            ))
        return out

    def _parse_piece(self, piece: str) -> Clause:
        # 1. 未确认主张：明确未确认，或“X米以上/上没有”这类无界、无孔号表述
        #    但“未取到样”“未见水”等有明确观察性质的词不按未确认处理
        if "未确认" in piece or "司机说" in piece:
            c = self._base(piece, ClauseKind.UNCONFIRMED)
            m = re.search(r"未说明([^，。；]*)", piece)
            c.reason = (
                f"未说明{m.group(1)}，且地质员未确认，不能生成层位/深度事实"
                if m
                else "地质员未确认，转述不能当独立观察"
            )
            return c
        ambiguous = re.search(r"[一二三四五六七八九十]米(以上|上)", piece) or "以上未取到样" in piece
        if ambiguous:
            c = self._base(piece, ClauseKind.UNCONFIRMED)
            c.reason = "“X米以上/上”为无界口语表述，未确认孔号与区间端点，禁止当作均无异常"
            return c

        # 2. 样号更正
        if "应为" in piece and SAMPLE_RE.search(piece):
            c = self._base(piece, ClauseKind.RENUMBER)
            ids = SAMPLE_RE.findall(piece)
            c.old_sample_id, c.sample_id = ids[0], ids[1]
            if "同袋" in piece:
                m_ev = re.search(r"，([^，。；]+?)证实同袋", piece)
                raw = m_ev.group(1) if m_ev else ""
                c.evidence = [x for x in re.split(r"与|及|和", raw) if x]
                if not c.evidence and "封签" in piece and "接收单" in piece:
                    c.evidence = ["封签", "接收单"]
                c.note = "同袋样本"
            c.extra["depth_unchanged"] = "深度不变" in piece
            if "不改" in piece:
                c.extra["unchanged_ids"] = SAMPLE_RE.findall(piece.split("不改", 1)[0])[-1:]
            return c

        # 3. 孔口基准更正：必须含新孔口标高；“K7基准不变”这类他孔说明不触发
        if "孔口应" in piece:
            c = self._base(piece, ClauseKind.DATUM_CORRECTION)
            m = re.search(r"孔口应\s*(\d+(?:\.\d+)?)", piece)
            if m:
                c.collar_elevation = float(m.group(1))
            c.extra["depth_unchanged"] = "深度" in piece and "不变" in piece
            c.extra["other_hole_unchanged"] = "K7基准不变" in self.text
            c.reason = "原抄错" if "原抄错" in self.text else None
            return c

        # 4. 孔口标高登记
        m = re.search(r"K\d+孔口\s*(\d+(?:\.\d+)?)", piece)
        if m:
            c = self._base(piece, ClauseKind.COLLAR)
            c.collar_elevation = float(m.group(1))
            return c

        # 5. 未取到样 / 取样筒空：点状未回收事件，不是无此层
        if "取样筒空" in piece or "未取到样" in piece:
            c = self._base(piece, ClauseKind.EMPTY_SAMPLER)
            d = _point_depth(piece)
            if d is not None:
                c.point_depth = d
                c.depth = DepthInterval(d, d)
            else:
                c.kind = ClauseKind.UNPARSED
                c.reason = "未取到样缺少可确认深度，不补造深度"
            return c

        # 6. 见水 / 观察时段内未见水
        if "见水" in piece or "未见水" in piece:
            kind = ClauseKind.WATER_NOT_SEEN if "未见水" in piece else ClauseKind.WATER_SEEN
            c = self._base(piece, kind)
            d = _point_depth(piece)
            if d is not None:
                c.point_depth = d
                c.depth = DepthInterval(d, d)
            m = re.search(r"观察至\s*([^，。；]+)", piece)
            if m:
                c.observed_until = m.group(1)
            return c

        # 7. 续尺：“继续两米见灰黏土”，区间挂接交给台账（同孔上次进尺）
        m = re.search(r"继续\s*([一二两三四五六\d][\d.]*)\s*" + METER_UNITS + r"?(?:见(.+))?$", piece)
        if m:
            c = self._base(piece, ClauseKind.CONTINUE)
            c.amount = _to_number(m.group(1))
            c.layer = m.group(2).strip() if m.group(2) else None
            return c

        # 8. 样品登记（含混合样跨层）
        if SAMPLE_RE.search(piece) and ("来自" in piece or "补样" in piece):
            kind = ClauseKind.SUPPLEMENT_SAMPLE if "补样" in piece else ClauseKind.SAMPLE_REGISTER
            c = self._base(piece, kind)
            c.sample_id = _find_sample(piece)
            c.depth = _depth_interval(piece)
            if "混合样" in piece:
                c.mixed = True
                m2 = re.search(r"跨(.+?)层", piece)
                if m2:
                    c.crossed_layers = [ch + "层" for ch in m2.group(1) if ch in "软硬"]
            m2 = re.search(r"有(.+?层?)[，。；]?$", piece)
            if kind is ClauseKind.SUPPLEMENT_SAMPLE and m2:
                layer_name = m2.group(1)
                c.layer = layer_name if layer_name.endswith("层") else layer_name + "层"
            # 仅保留岩性名称，去掉试验与排他性说明
            if c.layer:
                lm = re.search(r"([一-龥]*软[一-龥]*层?|[一-龥]*硬[一-龥]*层?|灰黏土层?)", c.layer)
                if lm:
                    name = lm.group(1)
                    c.layer = name if name.endswith("层") else name + "层"
            if "不是" in piece and "重复" in piece:
                c.distinct_from = SAMPLE_RE.findall(piece.split("不是", 1)[1])
            return c

        # 9. 实验结果
        if "无侧限抗压" in piece or "含水率" in piece or "试验" in piece:
            c = self._base(piece, ClauseKind.LAB_TEST)
            c.sample_id = _find_sample(piece) or self.last_sample
            if c.sample_id is None:
                c.kind = ClauseKind.UNPARSED
                c.reason = "实验条款缺少可归属样号，按规则不凭邻号猜测"
                return c
            if "无侧限抗压" in piece:
                c.test_type = "unconfined_compressive_strength"
                m2 = re.search(r"(\d+(?:\.\d+)?)\s*(MPa|kPa|Pa)", piece)
                if m2:
                    c.test_value, c.test_unit = float(m2.group(1)), m2.group(2)
            elif "含水率" in piece:
                c.test_type = "moisture_content"
            else:
                m2 = re.search(r"(\d+(?:\.\d+)?)\s*(MPa|kPa|Pa)", piece)
                if m2:
                    c.test_type = "unconfined_compressive_strength"
                    c.test_value, c.test_unit = float(m2.group(1)), m2.group(2)
                elif "未分层" in piece:
                    # 续句，如“混合样未分层试验”：类型与样号继承同记录前一实验句
                    prev = next((x for x in reversed(self._prior_lab_tests()) if x.sample_id == c.sample_id), None)
                    c.test_type = prev.test_type if prev else "unconfined_compressive_strength"
                    c.merge_into = prev.clause_id if prev else None
                else:
                    c.test_type = "unconfined_compressive_strength"
                c.note = "独立试验" if "独立试验" in piece else None
            c.layered = not ("未分层" in piece)
            if "未分层" in piece:
                c.note = "混合样未分层试验"
            if "只测" in piece:
                c.note = "只测该项"
            if "不是" in piece and "重复" in piece:
                c.distinct_from = SAMPLE_RE.findall(piece.split("不是", 1)[1])
            self.lab_tests.append(c)
            return c

        # 10. 设计选用的长句在 parse() 中已走 _parse_design_statement；
        #     此处保留兜底，处理被逗号切开后的独立片段
        if piece.startswith("拟用"):
            return self._parse_design_statement(piece)[0]
        if "缺样区" in piece and "假设" in piece:
            c = self._base(piece, ClauseKind.ASSUMPTION)
            c.description = piece
            c.zone = _find_zone(piece) or self.last_zone
            return c
        if "须停待补查" in piece or "停待" in piece:
            c = self._base(piece, ClauseKind.TRIGGER)
            c.trigger = piece
            c.zone = _find_zone(piece)
            return c
        if "未签认" in piece:
            c = self._base(piece, ClauseKind.NOTE)
            c.note = "未签认（草案）"
            return c

        # 11. 施工开挖登记
        if "已挖至" in piece:
            c = self._base(piece, ClauseKind.CONSTRUCTION_EXCAVATION)
            c.zone = _find_zone(piece)
            m2 = re.search(r"已挖至\s*(\d+(?:\.\d+)?)", piece)
            if m2:
                c.elevation = float(m2.group(1))
            return c
        if "待复测" in piece or "以下未挖" in piece:
            c = self._base(piece, ClauseKind.NOTE)
            c.note = piece
            return c
        if piece.startswith("观察至"):
            c = self._base(piece, ClauseKind.NOTE)
            m2 = re.search(r"观察至\s*([^，。；]+)", piece)
            c.observed_until = m2.group(1) if m2 else None
            c.note = piece
            return c

        # 12. 分区采用（已签认类设计说明）
        if "采用" in piece and _find_zone(piece):
            c = self._base(piece, ClauseKind.ZONE_SELECTION)
            c.zone = _find_zone(piece)
            c.sample_id = _find_sample(piece)
            if "不引用" in piece:
                c.exclusions = SAMPLE_RE.findall(piece.split("不引用", 1)[1])
            c.extra["location_note"] = piece if "位置不在" in piece else None
            return c

        # 13. 口传/转述送样：不绑定登记样号
        if "送实验室" in piece or "送样" in piece:
            c = self._base(piece, ClauseKind.SUBMISSION_CLAIM)
            m2 = re.search(r"([一二三四五六七八九十]+)号样", piece)
            c.extra["oral_sample_no"] = m2.group(1) if m2 else None
            c.note = "转述送样，未与样品登记绑定，禁止凭邻号猜同袋"
            if "不是" in piece:
                c.extra["scope_note"] = piece
            return c

        # 14. 层位界 / 见层
        m = re.search(r"深\s*(\d+(?:\.\d+)?)\s*" + METER_UNITS + r"?\s*入(.+)$", piece)
        if m:
            c = self._base(piece, ClauseKind.LAYER_ENTRY)
            d = float(m.group(1))
            c.point_depth, c.depth = d, DepthInterval(d, d)
            c.layer = m.group(2).strip()
            return c
        if ("见" in piece or "入" in piece) and _point_depth(piece) is not None:
            c = self._base(piece, ClauseKind.LAYER_ENTRY)
            d = _point_depth(piece)
            c.point_depth, c.depth = d, DepthInterval(d, d)
            m2 = re.search(r"(?:见|入)(.+)$", piece)
            c.layer = m2.group(1).strip() if m2 else None
            return c

        # 15. 一般附注
        if piece:
            c = self._base(piece, ClauseKind.NOTE)
            c.note = piece
            return c
        c = self._base(piece, ClauseKind.UNPARSED)
        c.reason = "无匹配模式"
        return c


def parse_record(record: dict) -> ParseResult:
    """解析资料包中的一条原文记录。"""
    return _ClauseParser(record["id"], record["text"]).parse()


def parse_case(records: list[dict]) -> dict[str, ParseResult]:
    return {r["id"]: parse_record(r) for r in records}
