# 03: 项目注册库与稳定项目身份

**What to build:** 建立应用级 SQLite 注册库:汉化项目以稳定 UUID 为身份,记录其关联的游戏安装(路径、版本);创建汉化项目时在注册库登记,项目数据改按项目身份存放,不再以游戏目录名称推导。同一游戏在不同路径下的两个安装是两个互不串用的项目;游戏目录移动或重命名后,项目可以重新定位到同一汉化项目。现有界面流程保持可用(ADR-0001)。

**Blocked by:** 01(统一依赖与测试入口)

**Status:** resolved

- [x] 注册库落地,汉化项目拥有与目录名、绝对路径无关的稳定身份
- [x] 不同路径的同名游戏创建项目后,提取结果、词汇表、译文完全隔离(合成验证:串数据为 0)
- [x] 目录改名或移动后,项目可重新定位并继续使用原有资产
- [x] 不存在"未登记就凭目录名建目录"的旁路入口
- [x] 通过现有界面仍可完成创建与打开项目

## Answer

实现（ADR-0001、ADR-0004；测试 `tests/test_registry.py` 15 项 + 合成回归新增同名隔离段）：

- **注册库**：`core/registry.py` —— 应用级 SQLite（`registry.db`，随 `app_dir` 重定向）。
  `projects`（UUID、名称、创建/最近打开时间）+ `installs`（游戏安装：路径、版本、
  是否当前、首次登记时间；历史安装保留可追溯）。路径仅是"当前游戏安装"的属性，
  比较经 `path_key`（normcase+normpath）大小写/分隔符不敏感。部分唯一索引
  `installs(path_key) WHERE is_current=1` 在数据库层面兜底"同一路径至多是一个项目的
  当前安装"，并发登记的竞态窗口转化为明确的 ValueError。API：`create_project`、
  `find_by_path`、`relocate`、`stale_projects`、`installs`、`touch`、`ensure_project`。
- **身份式存储**：`util.work_dir(游戏目录)` 删除，改为 `util.store_dir(project_id)`
  （work/<项目身份>/）。`Project(cfg, game_base, project_id)` 强制持注册库身份，
  pystrings/ipatch/uipatch 的新参数 `data_dir` 由调用方显式传入——低层模块不再有能力
  按目录名建数据目录（旁路入口不存在）。
- **UI**：`_set_game`（选目录/游戏库"设为当前项目"）→ `_resolve_project`：已登记直接
  用；未登记且存在"当前安装已丢失"的旧项目时弹确认（单个=question，多个=列表选择），
  用户确认才 `relocate`，否认/取消则登记新项目——绝不按名称自动合并。`_run` 在主线程
  预解析，工作线程内不再有登记分支（目录移动后直接点步骤也不会静默另起新项目）。
  启动恢复上次目录只查找不登记不弹窗。版本号经 `updates.local_version` 写入注册库。
- **同名隔离回归**：test_synth_e2e 新增 packA/packB 两个同名 "My Game"：登记为两个
  项目、提取结果/词汇表/译文互不可见、B 端回填自己的译文而 A 端分毫不动（串数据 0）。

code-review（双轴）处理：删除死代码 `_project_dict`；`_add_install` 合并 create/relocate
重复 INSERT；唯一索引兜底并发 + 测试；`ensure_project` 文档改为"非交互便捷入口"；
测试改 fixture 去样板；UI 登记失败不再先写配置；词汇统一为"项目资产"（CONTEXT.md 词条）。

说明与遗留：
- **存量数据迁移不在本票**：老用户的 work/<游戏名>/ JSON 数据尚未导入注册库，升级后
  首次打开旧目录会作为新项目登记、旧数据暂不可达——这正是 04 号票（旧数据自动迁移，
  含预览/备份/幂等对账）的职责，序列上紧跟本票。
- **概念缺口记录**：目录改名/移动后的"重新定位"（re-point 同一安装）与 CONTEXT.md 的
  "项目迁移"（确认新版本安装后带入资产）是两个动作，前者暂无领域词条，按 domain.md
  要求记录给 domain-modeling 工作流。
- 路径比较不解析 8.3 短路径/符号链接：同一物理目录经不同路径形式会视为不同游戏安装
  （宁可分裂、绝不自动合并），已在 registry 模块文档注明。
