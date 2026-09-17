# 药用植物采收窗口决策平台

按 **地块** 合并物候观察、投入品施用、抽样检测、逐时降雨预报与采收作业，
输出「可采 / 等待 / 暂停」的可解释决策，而不是给所有田块一个统一答案。

## 双时态模型

每条事实同时保存两个时间：

| 时间 | 含义 |
| --- | --- |
| `occurred_at` / `sampled_at` / `issued_at` | 田间事实实际发生时间 |
| `recorded_at` | 系统录入（获知）时间，迟到记录可以晚于发生时间 |

评估时刻 `t` 只能看到 `recorded_at <= t` 的资料。迟到的施用记录可以暂停
**尚未开始** 的作业，却不能抹去已经执行（放行/开工）的决定——撤回是追加
一条 `supersedes` 指向旧版的新 `PAUSED` 决策，旧决策永久保留在审计链中。

## 决策门禁（按优先级）

1. **质量否决 → `paused`**
   - 投入品安全间隔（PHI）未结束：按 `occurred_at + phi_hours` 计算解禁时刻，
     迟到补录的记录同样生效；
   - 检测结果只代表 `parcel_ids` 精确列出的地块，且只在
     `sampled_at … valid_until` 内有效；**混合样不自动扩展范围**，
     `result_code != pass` 不放行。
2. **并发变更 → `paused`**：作业单领取后所依据的数据版本被新事实超过，
   作业单自动暂停，须由质量/现场按新版本 `reconfirm`；`started`/`completed`
   的作业不受迟到记录影响。
3. **农艺条件 → `wait` / `ready`**：物候成熟、突发暴雨后 24h 脱水；
   逐时降雨预报只能 **收缩** 建议窗口（推过雨带、截断到雨带前），
   不能推翻质量否决。窗口在当前时刻开启且上述条件满足才 `ready`。

技术员（`WindowProposal`）提出窗口，质量人员（`release`/`withdraw`）放行或
撤回；每个决策记录 `basis_version`（与该地块相关的可见事实条数）与
`observation_ids`，可逐条解释。

## 目录

```
farm/contracts.py   领域契约：观察、施用、抽样、预报、提案、决策、作业单
farm/loader.py      fixtures JSON -> 领域对象
farm/engine.py      HarvestLedger：双时态录入、门禁评估、版本化决策与回放
farm/__main__.py    命令行报告：地块状态、版本依据、撤回审计链、双时态对照
fixtures/harvest_window.json
                    field-a 补录施用撤回放行；field-b 暴雨；field-c 正常可采
tests/test_engine.py 规则测试（17 项）
```

## 运行

```bash
python -m compileall farm        # 契约编译检查
python3 -m unittest tests.test_engine -v
python3 -m farm                  # 回放夹具并打印决策报告
```

## 夹具故事线

- `field-a`：09-15 质量放行、现场领取；当晚预报更新触发暂停，09-16 09:10
  现场重新确认恢复；**09:40 补录 09-10 的多菌灵施用（PHI 336h）**，作业单
  再次暂停，09:45 质量撤回原放行——原放行与撤回均保留，须等到 09-24 16:00。
- `field-b`：凌晨突发暴雨（58mm），雨后需脱水 24h，最新预报又把窗口截到
  09-18 雨带之前 → 等待。
- `field-c`：无雨、检测在有效期内；混合样 `smp-mix-01` 虽然合格但不覆盖
  本地块，凭专属样 `smp-c-01` 放行 → 可采。
