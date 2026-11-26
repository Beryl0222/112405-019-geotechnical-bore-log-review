"""交接报告：把台账渲染为设计/施工可回查的中文 Markdown 与 JSON。"""
from __future__ import annotations

from typing import Any

from .model import WorkKind
from .pipeline import PipelineResult, geology_summary
from .trace import trace_zone


def build_report(result: PipelineResult, case: dict) -> dict[str, Any]:
    ledger = result.ledger
    geology = geology_summary(ledger, case)
    zones = {}
    from .parameters import build_zone_specs

    for spec in build_zone_specs(case["objects"]):
        zones[spec.zone_id] = trace_zone(ledger, spec.zone_id, spec)
    return {
        "case_id": ledger.case_id,
        "snapshot": result.to_dict(),
        "geology": geology,
        "zones": zones,
        "correction_impact": result.impact,
        "pending_investigations": result.pending,
        "disclaimer": "全部为合成演练资料与规则推导，不用于真实设计。",
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 钻探层位证据交接报告（{report['case_id']}）\n")
    add("> " + report["disclaimer"] + "\n")

    add("## 1. 孔口基准与更正")
    for hid, hole in report["snapshot"]["holes"].items():
        history = " → ".join(
            f"{h['version']}:{h['collar_elevation']}m" for h in hole["collar_history"]
        )
        add(f"- {hid}：孔口基准 {history}（当前 {hole['datum_version']}，深度不随基准改写）")
    add("")

    add("## 2. 层位事实与证据覆盖")
    for zid, z in report["geology"].items():
        add(f"### 分区 {zid}")
        for hid, h in z["holes"].items():
            add(f"#### 孔 {hid}")
            if not h["layers"]:
                add("- 无定位层位界记录（未知不等于无异常）")
            for seg in h["layers"]:
                located = "层位界已定位" if seg["contact_located"] else "区间内见层、界位未定位"
                add(f"- 深度 {seg['depth']['start_m']}-{seg['depth']['end_m']}m"
                    f"（标高 {seg['elevation']['low_m']}-{seg['elevation']['high_m']}m，"
                    f"当前基准）：{'/'.join(seg['layers'])}，{located}")
            cov = h["coverage"]
            add(f"- 实测覆盖：{_iv(cov['measured_intervals'])}")
            for gap in cov["gaps"]:
                add(f"  - 【{_gap_label(gap['status'])}】{gap['depth']['start_m']}-{gap['depth']['end_m']}m：{gap['reason']}")
    add("")

    add("## 3. 样品与实验（含重编号）")
    for s in report["snapshot"]["samples"]:
        alias = f"（旧编号 {', '.join(s['aliases'])}，同袋证据：{', '.join(s['identity_evidence'])}）" if s["aliases"] else ""
        mixed = "，混合样跨 " + "/".join(s["crossed_layers"]) if s["mixed"] else ""
        add(f"- {s['sample_id']}{alias}：{s['hole']} 深 {s['depth']['start_m']}-{s['depth']['end_m']}m{mixed}")
        for t in s["tests"]:
            val = f"{t['value']} {t['unit']}" if t["value"] is not None else "无数值"
            scope = "，未分层、仅限混合区间" if not t["layered"] else ""
            add(f"  - {t['test_label']}：{val}{scope}（{t['sample_id']}）")
    add("")

    add("## 4. 参数选用与外推前提")
    for sel in report["snapshot"]["selections"]:
        add(f"### {sel['selection_id']}（{sel['zone']}，状态：{sel['state']}）")
        val = f"{sel['value']} {sel['unit']}" if sel["value"] is not None else "无可用实测值"
        add(f"- 采用值：{val}；引用样：{', '.join(sel['cited_sample_ids']) or '无'}")
        if sel["evidence_elevation"]:
            add(f"- 证据标高区间：{sel['evidence_elevation']['low_m']}-{sel['evidence_elevation']['high_m']}m")
        for e in sel["extrapolation"]:
            add(f"- 外推前提：{_humanize(e)}")
        for a in sel["assumption_ids"]:
            asm = next(x for x in report["snapshot"]["assumptions"] if x["assumption_id"] == a)
            mark = "有效假设" if asm["valid"] else "已被补样推翻"
            add(f"- 假设 {a}（{mark}）：{asm['description']}；触发：{asm['trigger']}")
        for t in sel["triggers"]:
            add(f"- 补查触发：{t}")
        for n in sel["review_notes"]:
            add(f"- 重核意见：{_humanize(n)}")
    add("")

    add("## 5. 施工交接区间")
    kind_label = {WorkKind.WORKABLE.value: "可作业", WorkKind.STOP_PENDING.value: "停待补查",
                  WorkKind.COMPLETED.value: "已完成（记录不移动）"}
    for h in report["snapshot"]["handoffs"]:
        elev = h["elevation"]
        add(f"- 【{kind_label[h['kind']]}】{h['zone']} {elev['low_m']}-{elev['high_m']}m"
            f"（等效深度 {h['depth']['start_m']}-{h['depth']['end_m']}m）")
        for r in h["reasons"]:
            add(f"  - 依据：{_humanize(r)}")
        for c in h["conditions"]:
            add(f"  - 监测条件：{c}")
    add("")
    add("### 已完施工登记（不可变）")
    for e in report["snapshot"]["excavations"]:
        add(f"- {e['entry_id']} {e['zone']}：原记录标高 {e['recorded_elevation_m']}m"
            f"（{e['datum_version']} 基准，等效深度 {e['recorded_equivalent_depth_m']}m）；"
            f"当前基准等效深度 {e['current_equivalent_depth_m']}m")
    add("")

    add("## 6. 更正联合影响面")
    impact = report["correction_impact"]
    add(f"- 受影响孔：{', '.join(impact['affected_holes']) or '无'}")
    add(f"- 样号映射：{', '.join(impact['affected_sample_mappings']) or '无'}")
    add(f"- 受影响分区：{', '.join(impact['affected_zones']) or '无'}")
    for s in impact["selections_to_recheck"]:
        add(f"  - 重核 {s['selection_id']}（{s['state']}）：{'；'.join(s['reason'])}")
    for c in impact["rejected_corrections"]:
        add(f"  - 已拒绝更正 {c['correction_id']}：{c['reason']}")
    add("")

    add("## 7. 待查清单（实测/假设/未知分账）")
    for item in report["pending_investigations"]:
        add(f"- [{item['type']}] " + _pending_line(item))
    add("")
    return "\n".join(lines)


def _gap_label(status: str) -> str:
    return {
        "unsampled": "缺样",
        "not_recovered": "未回收",
        "limited": "观察受限",
        "unknown": "未知",
    }.get(status, status)


def _humanize(text: str) -> str:
    return text.replace("unconfined_compressive_strength", "无侧限抗压强度")


def _iv(intervals: list[dict]) -> str:
    if not intervals:
        return "无"
    return "；".join(f"{i['start_m']}-{i['end_m']}m" for i in intervals)


def _pending_line(item: dict) -> str:
    if item["type"] == "unconfirmed_claim":
        return f"{item['record_id']} “{item['quote']}” —— {item['reason']}"
    if item["type"] == "not_recovered":
        depth = item["depth"]
        depth_text = f"{depth['start_m']}-{depth['end_m']}m" if depth else "深度未确认"
        return f"{item['obs_id']} {item['hole']} 深度 {depth_text} 未回收，需补查"
    if item["type"] == "assumption":
        return f"{item['assumption_id']} 有效={item['valid']}，触发：{item['trigger']}"
    if item["type"].startswith("selection_"):
        return f"{item['selection_id']}：{_humanize('；'.join(item.get('review_notes', ())) or item['type'])}"
    if item["type"] == "submission_claim":
        return f"{item['obs_id']} 转述送样未绑定登记，禁止凭邻号猜同袋"
    if item["type"] == "rejected_correction":
        return f"{item['correction_id']}：{item['reason']}"
    return str(item)
