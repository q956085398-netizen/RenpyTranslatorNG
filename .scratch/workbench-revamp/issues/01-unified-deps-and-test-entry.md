# 01: 统一依赖与测试入口

**What to build:** 任何人在新机器或干净虚拟环境中,按文档一条命令安装全部依赖并运行完整测试套件,不依赖开发者本机的游戏路径或手工预装。包含:声明并锁定运行与测试依赖;建立统一测试命令;现有全部测试纳入该入口;初始化 Git 仓库并提交当前状态,为后续持续集成与公开发布打底。

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] 干净环境中按文档一条命令完成安装
- [x] 同一(或第二条)命令运行全部现有测试并给出统一退出码
- [x] 测试不引用任何本机绝对路径或真实游戏目录
- [x] 依赖有锁定策略,重复安装得到相同版本
- [x] 目录成为 Git 仓库并有初始提交,忽略规则覆盖生成物与本机数据(work/、config.json 中的密钥等)

## Answer

现状打底在初始提交 `23c317b` 已具备大部分;本票补齐的差距与验证:

- **测试引用本机绝对路径(主要差距)**:`tests/test_ipatch.py` Part 1 硬编码了 7 个
  `《本机路径已脱敏》` 真实游戏路径。移入 gitignored 的
  `tests/private_regression_cases.json`(`{"名称": "路径"}`),被跟踪代码不再含任何本机
  绝对路径(`git grep` 验证);开发者本机已创建该清单,`NG_PRIVATE_REGRESSION=1` 私有回归
  工作流不变(实测:存在样本正常解析、缺失样本逐项跳过)。
- **清单防崩与诚实失败**:`json.load` 加 JSONDecodeError 防护——默认模式清单损坏只跳过、
  退出码 0;显式 `NG_PRIVATE_REGRESSION=1` 时清单损坏按失败处理(退出码 1),避免"回归
  通过"假象。两种模式实测验证,清单文件测后逐字节还原。
- **传递依赖锁定**:新增 `requirements-dev.lock`(pip freeze 约束文件),一键命令升级为
  `pip install -r requirements-dev.txt -c requirements-dev.lock && pytest`。干净 venv 实测:
  安装后 `pip freeze` 与 lock 文件逐行一致,pytest 5 passed、退出码 0。
- **忽略规则**:`.gitignore` 增补 `.pytest_cache/` 与 `tests/private_regression_cases.json`。
  原有 `work/`、`covers/`、`config.json`(密钥)、`library.json`、`data/` 覆盖不变。
- **文档**:README 安装/测试章节与 `tests/conftest.py` 同步更新;词汇遵循 CONTEXT.md
  "回归样本"(避免 _Avoid_ 词条)。

审查(code-review 双轴)发现中不采纳的一项:`RenpyTranslatorNG.lnk` 被指嵌入本机路径——
实测其二进制仅含 `C:\Windows\pyw.exe` 系统公共路径,不含用户目录,保留。

遗留(本票范围外):初始提交 `23c317b` 的历史快照中仍含旧的本机路径(修复只改当前树)。
仓库目前仅本地单提交、无远程;公开发布前可由用户决定是否压平/重写历史一次性清除。
