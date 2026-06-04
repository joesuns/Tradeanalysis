---
name: engineering-protocol
description: Use when starting any code-modification task in this project — when the user asks to edit, fix, add, refactor, or implement code; when touching backend/, tests/, or docs/; or when a task involves multiple steps spanning analysis, planning, and implementation
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

### ⑤ Closing Checklist
After implementation:
- [ ] Every user requirement addressed? (Recheck ①)
- [ ] `pytest tests/ -v` passes?
- [ ] CLAUDE.md updated if commands/architecture/notes changed?
- [ ] Related specs/plans updated?
- [ ] If new function/class/endpoint added, checked all registries (orchestrator list, schema DDL, indexes, views, export_wide)?

## Mandatory Mechanisms

### TodoWrite First
Any code task → first action is TodoWrite with standard structure:
1. 全量审计
2. 方案撰写 → 等审批
3. 实施计划 → 等审批
4. 代码修改
5. 测试验证
6. 文档更新
7. 收尾核对

### Self-Check 5 Questions
Before every code suggestion, silently answer:
1. 用户提了几条要求？全回了吗？
2. 我有没有曲意逢迎？
3. 审批拿到了吗？
4. 受影响文件列全了吗？
5. 三条铁律满足了吗？证据列全了吗？有不了解的模块吗？

## Project-Specific Triggers

These patterns in this codebase require the full audit step:
- **Modifying a Calculator** → check orchestrator CALCULATORS list, schema DDL, indexes, views, export_wide
- **Modifying CLI** → update CLAUDE.md 常用命令 section
- **Modifying data flow** → trace ODS→DWD→DWS→ADS full chain
- **Adding a fetch** → verify all 3 DWD tables rebuilt in rebuild_all_dwd()

## User Interrupts

| Phrase | Meaning |
|--------|---------|
| "停，你漏了第 X 条" | I missed a user requirement |
| "停，方案我还没同意" | I skipped the approval step |

## Quick Card

> 三条铁律 → ① Parse all → ② Be honest → ③ Audit fully → ④ Await approval → ⑤ Close properly
> TodoWrite first. Self-check 5. Evidence before conclusion.
