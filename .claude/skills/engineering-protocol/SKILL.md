---
name: engineering-protocol
description: Use when starting any code-modification task in this project — when the user asks to edit, fix, add, refactor, or implement code; when touching backend/, tests/, or docs/; when optimizing calc/DWD/rebuild performance; when choosing incremental vs full pipeline paths; or when a task involves balancing data-pipeline efficiency with data quality
---

# Engineering Protocol

## 三条铁律 — Three Foundations

所有建议必须建立在三个基础上。缺一条 = 不合格，不可给出建议。

### 1. 对项目的充分了解
不了解 → 先调研 → 再说话。
- 涉及代码：`grep` 全项目相关数值/模式，列出完整清单
- 涉及模块：通读所有相关源文件，确认依赖链
- **不确定的模块：直接说"这里我不确定，需要调研"，禁止跳过、猜测或假设**

### 2. 坚实准确的专业知识
- 结论必须能追溯到 spec / CLAUDE.md / 代码逻辑 / 学术公式
- 不确定的判断标注置信度和前提假设
- 禁止以确定语气陈述未经验证的推测

### 3. 客观真实的论据
- 数值建议：结论前先列出全量证据（所有相关源文件中的硬编码数值）
- 看到列表不全 → 结论不可信
- 看到列表完整 → 结论基于事实

### 违反后果
发现自己在三条任一缺位 → 立即停止建议，回到调研。

## Tradeanalysis 计算约束

本项目是数据分析管道：**效率与分析需求同等重要**，但冲突时有明确优先级。

### 冲突裁决
**数据质量与分析需求 > 性能。** 性能优化须证明「范围更小且结果等价」；无法证明时，不做优化或先补测试/spec/等价性用例。

### DWD / Calc 路径决策树
制定方案前走此树；细节见 [reference.md](reference.md) 与 CLAUDE.md。

```
要改 DWD / calc？
├─ 有 stale 子集、仅新 bar、同日复跑？ → rebuild_dwd_for_stale / APPEND / SKIP / 幂等闸门
├─ 仅算法或 spec_version 变？ → 按指标窄窗 FULL + 更新 spec_version
├─ 首次建库 / repair-weekly / 除权未走增量 / 用户明确要求？ → rebuild_all_dwd(con, ts_codes) 或运维 FULL
└─ 否则 → 禁止全市场全量；方案须写「为何 incremental 不够」
```

### 三条反模式（禁止）
1. **习惯性全库 rebuild 或全市场 FULL** — 日常默认增量；全量须根因 + 范围说明
2. **无新 bar 仍全窗重算** — 有 state/fingerprint 时优先 SKIP/APPEND
3. **为省时间跳过门禁** — warmup(G1/G2/G3)、强签名 FULL、OHLC/adj 护栏、append/FULL 等价性、B4/golden 不可省略

## 5-Step Protocol

Every code-related response MUST follow these 5 steps. Do NOT skip.

### ① Full Parse
List ALL user requirements as numbered items. If unclear, ask. Never silently skip.

### ② Honest Judgment
If the user's approach has a problem: "我不同意，原因是 X" + alternative. Professional honesty > pleasing.

### ③ Full Audit
Before proposing: list ALL affected files/modules/data-flows. Partial audit = wrong audit.

### ④ Await Approval
No code edits until user says "好"/"可以"/"同意". No exceptions, not even "obvious fixes."
**例外：** 纯评审/问答/设计讨论 — 走 ①②（及必要的 ③），不写代码，不等审批。

### ⑤ Closing Checklist
After implementation:
- [ ] Every user requirement addressed? (Recheck ①)
- [ ] 方案用了最小必要计算范围（非习惯性全量）？
- [ ] 性能改动仍满足数据质量门禁（测试/spec/等价性）？
- [ ] `pytest tests/ -v` passes?
- [ ] CLAUDE.md updated if commands/architecture/notes changed?
- [ ] Related specs/plans updated?
- [ ] If new function/class/endpoint added, checked all registries (orchestrator list, schema DDL, indexes, views, export_wide)?

## Mandatory Mechanisms

### TodoWrite
**改代码任务：** 调研确认范围后 TodoWrite（非盲目先建 todo）。结构：
1. 全量审计
2. 方案撰写 → 等审批
3. 实施计划 → 等审批
4. 代码修改
5. 测试验证
6. 文档更新
7. 收尾核对

**纯问答/评审：** 可跳过 TodoWrite。

### Self-Check（每次给方案前 silently）
1. 用户提了几条要求？全回了吗？
2. 我有没有曲意逢迎？
3. 审批拿到了吗？（改代码任务）
4. 受影响文件列全了吗？
5. 三条铁律满足了吗？证据列全了吗？有不了解的模块吗？
6. 方案是否最小必要范围？若提议全量，是否已说明根因且 incremental 不够？

## Project-Specific Triggers

These patterns in this codebase require the full audit step:
- **Modifying a Calculator** → check orchestrator CALCULATORS list, schema DDL, indexes, views, export_wide
- **Modifying CLI** → update CLAUDE.md 常用命令 section
- **Modifying data flow** → trace ODS→DWD→DWS→ADS full chain
- **Adding a fetch** → 确认 stale 子集是否需 `rebuild_dwd_for_stale`；仅首次建库/运维 repair 才用 `rebuild_all_dwd(con, ts_codes)`
- **Performance / rebuild / calc routing** → 走上方决策树；禁止默认全库 rebuild

## User Interrupts

| Phrase | Meaning |
|--------|---------|
| "停，你漏了第 X 条" | I missed a user requirement |
| "停，方案我还没同意" | I skipped the approval step |
| "停，别全库 rebuild" | 立即收窄范围，按决策树重写方案 |

## Quick Card

> 铁律 → 5步 → **质量>速度** → **最小范围计算** → 审批后改码
> 决策树：stale/APPEND/SKIP 优先；全量须根因。反模式： habit 全量 / 无新 bar 全窗 / 跳门禁
> TodoWrite 在调研后；纯问答可跳过。Self-check 6 条。Evidence before conclusion.

## Additional resources
- 开关、SLA、等价性要求：[reference.md](reference.md)
