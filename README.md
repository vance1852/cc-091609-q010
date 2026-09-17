# 药用植物采收窗口决策平台

黄芩基地的地块级采收决策：把物候观察、投入品施用（含安全间隔）、抽样检测、
逐时降雨预报和采收作业合并为**可解释、可追溯、按地块**的 可采 / 等待 / 暂停 结论，
而不是给所有田块一个统一答案。

## 核心规则

- **双时态**：每条观察同时保存 `occurred_at`（发生时间）与 `recorded_at`（录入时间）。
  任何时点的评估只采用 `recorded_at ≤ as_of` 的资料，可以复算"当时知道什么"。
- **决策只追加**：`HarvestDecision` 版本链 (`version`, `supersedes_id`) 永不改写。
  迟到记录可以暂停未开始的作业、撤回未领取的放行，但已执行的决定原样保留，
  只追加隔离/复检警示（`Advisory`）。
- **质量否决不可绕过**：安全间隔未结束、检测缺失/不合格/过期、投入品未登记 → `paused`；
  降雨预报更新只能**缩短**建议窗口，不能覆盖任何质量阻断。
- **检测范围不自动扩展**：抽样结果只对 `parcel_ids` 列明地块且在 `valid_until` 内有效，
  混合样 (`mixed_sample`) 不会把效力扩展到未列明地块。
- **并发变更需重新确认**：现场领取作业后锁定决策版本；之后任何影响结论的新资料
  （新预报、补录、检测状态变化）都会把作业挂为 `awaiting_reconfirm`，质量重新确认
  通过并产生新版本后才能开工。

状态语义：`ready`（可采，可质量放行）/ `wait`（物候、沥水或天气窗口约束）/
`paused`（质量阻断，不得放行）。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `farm/contracts.py` | 既有契约：双时态 `FieldObservation`、`SampleResult`、版本化 `HarvestDecision` |
| `farm/models.py` | 地块、种源、投入品、预报、作业单、理由/证据、领域异常 |
| `farm/ledger.py` | append-only 双时态台账与历史查询 |
| `farm/engine.py` | 纯函数地块评估：状态、建议窗口、阻断理由、证据版本 |
| `farm/service.py` | 工作流：提案 → 放行 → 领取 → 并发同步/撤回 → 重新确认 → 完工 |
| `farm/views.py` | 地块视图与可读报告（含完整决策版本链与数据版本解释） |
| `farm/loader.py` | 夹具 JSON 加载（精简格式 + 扩展字段） |
| `farm/demo.py` | 完整时间线回放：放行 → 迟到补录撤回 → 质量否决 → 雨停后重新确认 |

## 使用

```bash
python3 -m compileall farm                 # 契约编译检查
python3 -m unittest discover -s tests      # 20 个行为/集成测试
python3 -m farm.cli view                   # 按地块查看当前状态与理由
python3 -m farm.cli view field-a \
    --as-of 2026-09-15T12:00:00+08:00      # 历史时点回放（迟到记录尚不可见）
python3 -m farm.demo                       # 回放放行被迟到记录撤回的完整过程
```

## 夹具说明

`fixtures/harvest_window.json` 在原有三个观察（field-a 物候、**09-10 施用却在
09-16 08:30 补录**、field-b 突发暴雨 42mm）基础上，补充了：

- 种源与三个地块（field-a / field-b / field-c）；
- 投入品登记（168h 安全间隔）；
- 单地块样（仅 field-a）与混合样（仅覆盖 field-a、field-b）；
- 两版逐时降雨预报（第二版显示 09-17 18:00 雨带）；
- field-a 的已领取作业单。

直接 `view` 可见：field-a 的放行 v2 被补录记录撤回为 v3（paused），作业单挂起
等待重新确认，且 v2 放行记录完整保留；field-c 虽物候成熟，因不在任何检测样品
范围内而暂停；field-b 处于暴雨沥水等待。
