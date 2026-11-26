# 地质钻探日志的层位归属与风险边界

项目保存用于本地业务联调的合成原始文本、对象登记及已有办理记录，并提供从录入、区间确认、
参数选用到施工交接的可复用处理能力。文字包含尚未确认的主张，不能直接当作业务结论。
资料口径见 [资料口径](docs/materials.md)，业务规则与能力边界见 [能力与规则口径](docs/capabilities.md)。

`data/case.json` 是一份可追加原文的资料包；`docs` 记录输入含义，`tests` 检查资料完整性与业务规则。
**全部为合成演练内容，不用于真实设计。**

## 能力概览

`geotech_ledger/` 是无第三方依赖的标准库 Python 包（Python 3.11+）：

- `parser.py`：中文钻进叙述/取样/实验/设计说明的规则解析，省略句只接同记录孔号，歧义句隔离；
- `ingest.py`：解析条款落为事件，续尺挂同孔锚点、更正证据校验、未确认不补造；
- `ledger.py`：只追加事件溯源台账、孔口基准版本、深度↔标高换算、快照与重放；
- `geology.py`：层位事实确认、实测覆盖与缺样区分析（缺样不是无异常）；
- `parameters.py`：参数适用性按区间/试验类型/设计部位判定，外推前提、假设触发与补样重核；
- `construction.py`：可作业/停待补查/已完成交接区间、更正联合影响面、已完施工不可移动；
- `trace.py` / `reports.py` / `cli.py`：证据链回查、JSON 与 Markdown 交接报告、命令行入口。

## 使用

```sh
# 全量处理
python3 -I run_demo.py data/case.json --outdir out

# 导出事件流（只追加 JSONL，可重放）
python3 -I run_demo.py data/case.json --outdir out --events out/events.jsonl

# 回查实测、假设与待查依据
python3 -I run_demo.py data/case.json --trace-sample S60
python3 -I run_demo.py data/case.json --trace-depth K6 6.4

# 运行全部测试
python3 -I -m unittest discover -s tests -v
```

输出：`out/ledger.json`（台账快照）、`out/report.json`（结构化交接报告）、
`out/handoff.md`（中文交接报告）、`out/events.jsonl`（事件流）。

## 核心口径

- 深度为孔口向下米数，标高按各孔孔口基准推算；基准更正不改深度，只改推算标高。
- “继续两米”接同孔上次确认进尺，不接邻孔或页码。
- 岩芯未取到不是无此层；未见水只覆盖观察时段；样号更正须同袋证据，不能凭邻号猜同袋。
- 混合样强度不覆盖所有深度；缺样区的保守假设必须标明触发条件，补样推翻前提后选用与施工范围重核。
- 已完施工记录不随更改变动，只追加当前基准等效值。
