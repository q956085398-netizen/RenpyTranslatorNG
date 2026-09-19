# Map: workbench-revamp

规格:`spec.md`(Status: ready-for-agent)
范围:阶段 0(安全网)+ 阶段 1(可靠性基线)。阶段 2-4(UI 重组、视觉系统、发布能力)在 10 号票验收后另行切片,本图不含。

## Tickets

| # | Title | Status | Blocked by |
|----|-------|--------|------------|
| 01 | 统一依赖与测试入口 | ready-for-agent | — |
| 02 | 合成 Ren'Py 回归游戏 | ready-for-agent | 01 |
| 03 | 项目注册库与稳定项目身份 | ready-for-agent | 01 |
| 04 | 旧数据自动迁移 | ready-for-agent | 03 |
| 05 | 项目数据库:出现位置与译文记录 | ready-for-agent | 02, 03 |
| 06 | 单项目单写入任务协调器 | ready-for-agent | 05 |
| 07 | 事务式应用与恢复 | ready-for-agent | 05, 06 |
| 08 | 外部 tl 变更检测 | ready-for-agent | 07 |
| 09 | 译文编辑页接入项目库 | ready-for-agent | 05, 06 |
| 10 | 可靠性验收与回归全量接入 | ready-for-agent | 04, 08, 09 |

并行提示:01 完成后 02 与 03 可并行;05 完成后 06 → (07 ∥ 09) 可并行;07 → 08;10 汇聚全部。

## Context pointers

- 领域词汇与边界:根目录 `CONTEXT.md`
- 架构决策:`docs/adr/0001`–`0005`
- 完整计划与验收标准:`docs/PRODUCT-IMPROVEMENT-PLAN.md`

## Log

- 2026-09-20:由 to-tickets 从 spec.md 生成 01–10,经用户批准发布。
