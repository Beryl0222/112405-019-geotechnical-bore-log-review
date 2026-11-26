"""地质钻探层位证据台账（合成演练，不用于真实设计）。

能力边界：
- 录入中文钻进叙述、取样登记、实验报告、设计选用说明；
- 把层位实测事实、未回收/观察受限、未确认主张与保守假设分账；
- 区间确认、参数选用（外推前提）、补样推翻后的重核；
- 施工交接区间与基准/样号更正的联合影响面；
- 全部结论可回查到原文编号与原句。
"""
from .ledger import Ledger
from .pipeline import PipelineResult, run_pipeline
from .reports import build_report, render_markdown

__all__ = ["Ledger", "run_pipeline", "PipelineResult", "build_report", "render_markdown"]
