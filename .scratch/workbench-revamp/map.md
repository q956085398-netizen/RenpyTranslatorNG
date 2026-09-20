# Map: workbench-revamp

规格:`spec.md`(Status: ready-for-agent)
范围:阶段 0(安全网)+ 阶段 1(可靠性基线)。阶段 2-4(UI 重组、视觉系统、发布能力)在 10 号票验收后另行切片,本图不含。

## Tickets

| # | Title | Status | Blocked by |
|----|-------|--------|------------|
| 01 | 统一依赖与测试入口 | resolved | — |
| 02 | 合成 Ren'Py 回归游戏 | resolved | 01 |
| 03 | 项目注册库与稳定项目身份 | resolved | 01 |
| 04 | 旧数据自动迁移 | ready-for-agent | 03 |
| 05 | 项目数据库:出现位置与译文记录 | resolved | 02, 03 |
| 06 | 单项目单写入任务协调器 | resolved | 05 |
| 07 | 事务式应用与恢复 | ready-for-agent | 05, 06 |
| 08 | 外部 tl 变更检测 | ready-for-agent | 07 |
| 09 | 译文编辑页接入项目库 | ready-for-agent | 05, 06 |
| 10 | 可靠性验收与回归全量接入 | ready-for-agent | 04, 08, 09 |

并行提示:01 完成后 02 与 03 可并行;05 完成后 06 → (07 ∥ 09) 可并行;07 → 08;10 汇聚全部。

## Context pointers

- 领域词汇与边界:根目录 `CONTEXT.md`
- 架构决策:`docs/adr/0001`–`0005`
- 完整计划与验收标准:`docs/PRODUCT-IMPROVEMENT-PLAN.md`
- 合成回归夹具与引擎替身:`tests/fixtures/synthgames/`、`tests/syntheng.py`、
  `tests/stubserver.py`（05–09 的测试直接复用,菜单字幕走 strings 语义见 02 号票 Answer）

## Log

- 2026-09-20:由 to-tickets 从 spec.md 生成 01–10,经用户批准发布。
- 2026-09-20:01 已解决(统一依赖与测试入口):私有回归样本路径移出被跟踪代码、
  requirements-dev.lock 传递依赖锁定、.gitignore 增补;答案见 `issues/01-….md` 的 Answer。
  02 与 03 现在可并行。
- 2026-09-20:02 已解决(合成 Ren'Py 回归游戏):synth_basic/synth_ipatch 两个合成游戏、
  引擎替身 syntheng、本地 stub 服务连接、57 条断言的端到端回归;顺带修复 gen_common_tl
  重写 common.rpy 与 test_ipatch work_dir 污染两个既有缺陷;答案见
  `issues/02-….md` 的 Answer。05 现在只等 03。
- 2026-09-20:03 已解决(项目注册库与稳定项目身份):core/registry.py 应用级 SQLite 注册库
  (稳定 UUID + 游戏安装关联 + 唯一索引兜底)、util.store_dir 按身份存项目资产
  (work/<游戏名> 推导入口已删除)、UI 经注册库登记/重新定位(用户确认,绝不按名合并);
  15 项注册库测试 + 合成回归同名双路径隔离段;答案见 `issues/03-….md` 的 Answer。
  04 与 05 现在可并行。
- 2026-09-20:05 已解决(项目数据库:出现位置与译文记录):core/project_store.py
  每项目一个 SQLite 项目库(出现位置统一稳定标识 + 译文记录来源/确认位/候选版本/
  迁移建议)、菜单 key 生成与回填一致(死的 caption 任务路径删除,菜单字幕统一
  strings 语义)、人工译文不被模型/清缓存/补丁失效覆盖、重译请求按 key 生效、
  旧 translations.json 一次性导入;28 项存储测试 + 2 项菜单 key 测试 + e2e
  工单 05 段;答案见 `issues/05-….md` 的 Answer。06 现在解锁,07/09 只等 05(已解)。
- 2026-09-20:06 已解决(单项目单写入任务协调器):core/coordinator.py 项目任务
  协调器(项目库 tasks 表 + BEGIN IMMEDIATE 原子登记 + 心跳/进程死活判定的陈旧
  接管,跨线程跨进程互斥)、翻译按记录事务逐批提交(translate_jobs 改 seed+commit,
  删除整份覆盖镜像文件;真实 TerminateProcess 中断回归:已提交批次一致、无半写、
  断点续跑不重发)、冲突编辑进带说明的候选译文(adopt_candidate 即任务后合并
  入口)、游戏库任务独立 Worker 槽位不被项目任务锁住;13 项协调器测试 + e2e
  工单 06 段;答案见 `issues/06-….md` 的 Answer。07 与 09 现在可并行,08 等 07。
