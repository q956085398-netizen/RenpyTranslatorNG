# 02: 合成 Ren'Py 回归游戏

**What to build:** 一组随仓库分发的合成 Ren'Py 游戏,作为最高测试接缝的固定输入,覆盖真实踩坑过的全部语法形态:普通对白与菜单、同一原文的不同分支、同一块中的多个菜单节点、动态变量对白、init python 文本、多行字符串与复杂表达式、标签与插值变量、ipatch 各种覆盖形式、多主角与玩家自填关系词。提取 → 生成任务 → 模拟翻译(stub 服务连接,零费用零网络)→ 回填 → 再次提取 → 应用验证可以端到端自动执行。

**Blocked by:** 01(统一依赖与测试入口)

**Status:** resolved

- [x] 合成游戏随仓库分发,不包含任何受版权保护的第三方内容
- [x] 覆盖上述全部语法形态,每类形态有对应断言
- [x] 端到端链路(提取→任务→stub 翻译→回填→再提取→应用)可在测试中自动执行
- [x] 测试使用本地 stub 服务连接,不产生网络请求与 API 费用
- [x] 测试用临时目录,不触碰真实 work/ 数据

## Answer

新增 `tests/fixtures/synthgames/`（两个合成游戏，全部原创文本）、`tests/syntheng.py`（提取模拟器）、`tests/stubserver.py`（本地 stub 服务连接）、`tests/test_synth_e2e.py`（端到端回归，57 条断言）。

- **synth_basic**：普通对白与菜单、同一 label 中的多个菜单节点（含跨菜单重复字幕）、同一原文不同分支、动态变量对白、`_()` 与 Python 字面量文本、三引号多行字符串、转义引号/`[[`/`%%`/`{color}`、字符串字面量说话人、多主角、`renpy.input` 关系词与 `[relation1]` 回显；另带 `tl/None/common.rpym`（引擎 UI 字符串，MIT 许可并注明出处）驱动 common.rpy 生成与 developer 文件过滤。
- **synth_ipatch**：五种 ipatch 机制（say_menu_text_filter 替换链、StringTranslator 猴子补丁、translate ipatch 伪语言块、变量改写+说话人改名、renpy.exports.say 整句字典）+ 补丁自有输入文本 + `storypatch.rpy` 误判守卫。
- **syntheng（引擎替身）**：逐行复刻 `renpy.translation.generation` 与运行期钩子的产物格式——标识符公式 `<label>_<md5(say代码+"\r\n")[:8]>`、BOM+TODO 头、`# file:line` 位置注释、[全部注释行][全部代码行] 结构、strings old/new 对、再提取跳过已有译文（幂等，时间戳固定）。它替代 `extract.extract` 成为提取边界替身：**真实引擎钩子路径不在本套件覆盖内**，继续由本机私有回归（`NG_PRIVATE_REGRESSION`）负责——合成游戏没有引擎与 exe，这是本票的既定妥协，后续真实引擎行为变化由私有回归兜底。
- **stub 服务连接**：127.0.0.1 随机端口的 OpenAI 兼容 HTTP 替身，走完整 requests→协议解析→批次对齐→缓存链路；测试注入固定译表+确定性兜底，零外部网络与费用。
- **菜单路径的现实修正**：现代引擎（7.4–8.x）中菜单字幕经 strings 机制翻译，dump 里只有 say 节点（真实 dump.json 样本证实）；tlgen 的 caption 任务路径（dump 菜单节点）在真实引擎下不可达（Translate 块含 Menu 会使引擎生成器崩溃），故"同一块中的多个菜单节点"按现实以同 label 多菜单字幕条目覆盖。05 号票修菜单 key 时以此语义为准。
- **顺带修复的两个既有缺陷**（e2e 直接暴露）：`core/extract.py` gen_common_tl 的 rpym 路线在 common.rpy 已存在时仍重写、清空已回填译文（现在尊重 skip_strings 提前返回，与函数头注释及路线三守卫一致）；`tests/test_ipatch.py` 模块级替换 `ipatch.work_dir` 从不恢复、污染后续测试模块（现在保存并恢复）。
- 词汇备注：测试与产品代码沿用"译文缓存"一词，CONTEXT.md《译文记录》_Avoid_ 含"缓存条目"——属仓库级既有用法，留给后续词汇统一处理。

code-review（Standards + Spec 双轴）发现的问题已全部处理：docstring 同步、按规格顺序"应用→再次提取"、补关系词回显断言、菜单回填断言拆强、common.rpym 注明引擎字符串出处、tl 快照提取 helper。
