# 06: 单项目单写入任务协调器

**What to build:** 提取、翻译、迁移、检查、应用全部作为项目任务登记到协调器;同一汉化项目同一时间只允许一个修改核心数据的项目任务,其他项目与游戏库不受影响。翻译过程按记录事务提交,不再整份覆盖缓存文件;任务运行期间用户仍可浏览并进行非冲突编辑,冲突编辑进入待检查在任务结束后安全合并。

**Blocked by:** 05(项目数据库:出现位置与译文记录)

**Status:** resolved

- [x] 所有修改核心数据的后台操作都经协调器登记,无绕行直写路径
- [x] 翻译运行时编辑其他记录,两边数据都不丢失(并发测试)
- [x] 模拟中断进程后,已提交批次保持一致,未提交内容不产生半写状态
- [x] 冲突编辑形成带说明的待检查项,任务结束后可安全合并
- [x] 其他汉化项目与游戏库操作不被运行中的项目任务锁住

## Answer

实现（ADR-0003、ADR-0004；测试 `tests/test_coordinator.py` 13 项 + 合成 e2e 新增工单 06 段）：

- **协调器**：`core/coordinator.py` —— 项目任务登记落在项目库（project.db）的
  `tasks` 表上（ADR-0004：项目任务属于项目状态，表结构由项目库建库脚本统一创建，
  读写经同一连接入口，项目库写路径纪律不破）。`begin(kind)` 在单个
  `BEGIN IMMEDIATE` 事务里完成"检查活跃任务 + 插入任务行"，同项目的多线程乃至
  两个应用实例之间原子互斥（8 线程并发 begin 恰好 1 成功）；冲突抛 `TaskBusy`。
  任务行带心跳（TaskHandle 内 daemon 线程每 10s 刷新）；进程中断后按"进程已死
  （Windows OpenProcess+WaitForSingleObject）或心跳过期（不可判定时的回退）"
  判定 interrupted 并允许新任务接管。上下文管理器退出自动收尾（异常记 failed
  带摘要），界面长流程可显式 `finish(state, summary)`。
- **翻译按记录事务提交**：`translator.translate_jobs` 改收 `seed`（断点续翻与
  同文去重的种子，由 pipeline 从项目库取）与 `commit`（每批结果在标签修复、
  变量还原后立即调用，项目库一次一小事务），删除整份镜像文件的节流重写与
  `work_path` 读写。种子同文复用（reused）与收尾去重展开（expanded）同样走
  commit——进程中断后已提交批次完整一致、未提交批次无半写，断点续翻不重发
  已提交内容（真实 TerminateProcess 子进程中断回归验证）。
- **冲突编辑**：`record_model_results` 返回 `conflicts`（人工确认的当前译文被
  任务结果命中的出现位置）——人工译文保持当前不动，任务结果进候选译文并带
  说明（"项目任务《翻译》运行期间与人工编辑冲突，请比较后采用或忽略"，相同
  候选已存在时补写说明）；`adopt_candidate` 即"任务结束后安全合并"的采用入口。
  pipeline 汇总冲突条数给出明确日志；任务收尾状态记入任务行（done/stopped/
  failed + 摘要）。
- **pipeline**：`translate`/`retranslate_missing` 共用 `_translate_into_store`
  （提交+冲突汇总），`_prepare_translation_run` 在开跑前整份刷新一次派生镜像
  （此后任务期间不再写镜像，结束时再刷新一次对账）——派生镜像与项目库的
  滞后窗口收敛到"上次刷新"，库始终是可信来源。
- **UI**：`@project_task(kind)` 显式标记全部会改核心数据的后台步骤（全流程/
  准备/提取/检查/翻译/回填/修复/应用/统计），`_run` 启动前主线程登记（TaskBusy
  → 界面拒绝，绝不两任务并行写核心数据），`_on_done` 落终态；`_clear_cache`
  与运行中任务互斥（is_busy 拒绝）。游戏库任务（扫描/刷新/封面/评分）走独立
  Worker 槽位 `_run_lib`，不经协调器、不再被项目任务禁用——与运行中的项目任务
  并行无碍。
- **WAL**：项目库连接启用 `journal_mode=WAL` + `busy_timeout=10s`——任务运行
  期间的浏览与非冲突编辑（译文修改页 `set_human_translation` 直连项目库）与
  任务批量提交读写并行，互不阻塞。

code-review（双轴）处理：新代码中"缓存文件"措辞统一为派生镜像/项目库（_Avoid_
词汇）；`_PROJECT_TASK_KINDS` 方法名映射改为 `@project_task` 显式标记（改名/
新增步骤漏登记即绕行直写路径的隐患）；`record_model_result` 单条入口收敛到
批量路径（同一保护与冲突语义）；相同候选已存在时补写冲突说明（"带说明"不依赖
候选恰好是首条）；`_reclaim_stale` 对 pid 复用残留行（started_at 早于本进程
启动）按陈旧回收，不再永久阻塞；`_run` worker 启动失败时任务行立即收尾；
`_running_fn` 拆分为项目/游戏库两槽位；pipeline 翻译/重译重复执行段提取为
`_translate_into_store`；冲突计数去重；e2e stub6 停止挪入 finally。

说明与遗留：
- **迁移类任务的写入方在 04 号票**（旧数据迁移登记为项目任务时用 kind="迁移"）；
  协调器 kind 是开放标签，不预设枚举。
- **检查中心的完整形态属阶段 2**：冲突待检查项目前的呈现面是候选译文 + 冲突
  说明 + 任务摘要/日志，汇入统一检查中心（故事 23）时直接消费 `candidates` 与
  tasks 表，不需要数据层改动。
- 项目库连接入口目前仍是 `ProjectStore._conn`（core 包内协作，模块 docstring
  已声明纪律）；如后续有包外读取方需要任务表，再提公开连接入口。
