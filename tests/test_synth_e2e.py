# -*- coding: utf-8 -*-
"""工单 02 端到端回归：合成 Ren'Py 游戏作为固定输入走完整流水线。

提取（引擎替身 tests/syntheng，产物与真实引擎逐行对齐）→ 生成任务 →
stub 服务连接翻译（本地 HTTP，零费用零网络）→ 回填 → 再次提取（幂等）→
应用到游戏（字体/语言入口/人名与关系词显示层）。

覆盖形态与对应断言（工单 02）：普通对白与菜单、同一原文的不同分支、同一 label
中的多个菜单节点、动态变量对白、init python 文本、多行字符串、复杂表达式
（转义/标签/插值）、字符串字面量说话人、ipatch 各种覆盖形式、多主角与玩家
自填关系词、引擎 common 字符串（含 developer 文件过滤）。

工单 06 补充：项目任务协调器（同项目互斥、他项目无锁、任务行收尾）与翻译
运行时的并发编辑（人工编辑与模型结果两边都不丢，冲突进带说明的候选译文）。

测试全程使用临时目录（app_dir 重定向：注册库与项目资产目录都落在临时目录），不触碰真实 work/ 数据。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import applytxn, coordinator, extract, ipatch, pipeline, project_store, registry, tlgen, util  # noqa: E402
from tests import syntheng, stubserver  # noqa: E402

failures = []

REAL_WORK = os.path.join(util.app_dir(), "work")
REAL_WORK_BEFORE = sorted(os.listdir(REAL_WORK)) if os.path.isdir(REAL_WORK) else None
REAL_APP_DIR = util.app_dir
TMP = tempfile.mkdtemp(prefix="ng_synth_e2e_")
GAMES_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "synthgames")


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, name, detail))
    if not cond:
        failures.append(name)


def copy_game(name):
    dst = os.path.join(TMP, "games", name)
    shutil.copytree(os.path.join(GAMES_SRC, name), dst)
    return dst


def make_cfg(base_url, font_path):
    return {
        "language": "chinese",
        "engine": {"base_url": base_url, "api_key": "stub-key", "model": "stub-model",
                   "temperature": 0.0, "concurrency": 2, "batch_size": 4,
                   "context_lines": 2, "timeout": 30, "max_retry": 1,
                   "max_tokens": 0, "max_prompt_tokens": 0, "thinking": "auto",
                   "system_prompt": ""},
        "apply_font": True,
        "font_path": font_path,
        "apply_lang_entry": True,
        "translate_strings": True,
    }


def register(game, name):
    """把合成游戏登记为汉化项目（注册库随 app_dir 重定向到临时目录）。"""
    reg = registry.Registry()
    try:
        return reg.create_project(name, game)
    finally:
        reg.close()


def make_project(cfg, game, name):
    return pipeline.Project(cfg, game, register(game, name)["id"])


def jobs_by_key(game, dump_path):
    dump = util.read_json(dump_path)
    jobs, _files = tlgen.build_jobs(game, "chinese", dump, include_strings=True,
                                   context_lines=2)
    return {j["key"]: j for j in jobs}


def batch6(items):
    """stub 请求条目里的任务 key 列表（工单 06 并发窗口断言用）。"""
    return [it["i"] for it in items if isinstance(it, dict) and it.get("i")]


def tl_dir_of(game):
    return os.path.join(game, "game", "tl", "chinese")


def snapshot_tl(game):
    """tl/<语言>/ 全部文件的文本快照（幂等断言用）。"""
    out = {}
    for root, _dirs, files in os.walk(tl_dir_of(game)):
        for f in files:
            fp = os.path.join(root, f)
            out[os.path.relpath(fp, tl_dir_of(game))] = read(fp)
    return out


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# =====================================================================
# synth_basic：语法形态全覆盖 + 端到端链路
# =====================================================================
stub = stubserver.StubTranslator(table={
    "Welcome to the Synth Vale.": "欢迎来到合成之谷。",
    "You are [player_name], aren't you?": "你是[player_name]，对吧？",
    "The vale is quiet tonight.": "今夜山谷很安静。",
    "I'm {i}so{/i} ready for this.": "我{i}已经{/i}准备好了。",
    "Ask about the vale": "打听山谷的事",
    "Leave": "离开",
    "Look closer": "凑近看看",
    "Tell me about this place.": "跟我讲讲这个地方。",
    "It is an old shrine.": "那是一座古老的神社。",
    "Progress: [progress]%% {color=#ccffcc}and counting{/color}.": "进度：[progress]%% {color=#ccffcc}仍在继续{/color}。",
    "Leaving already?": "这就走了？",
    "The road is long, [player_name].": "路还长着呢，[player_name]。",
    "The river remembers.": "河水记得。",
    "This blessing spans\nseveral quiet lines.": "这份祝福横跨\n数行静谧。",
    "Halt. Who goes there?": "站住。来者何人？",
    "Escaped \"quotes\" and a [[bracket] stay literal.": "转义的\"引号\"和[[方括号]保持原样。",
    "So you are my [relation1] now.": "那么你现在是我的[relation1]了。",
    "Family is family.": "家人就是家人。",
    "The Synth Vale": "合成之谷",
    "Skip": "跳过",
    "Self-Voicing": "自动朗读",
    "What is Sofia to you?": "Sofia 对你来说是什么？",
}).start()

FONT_PATH = os.path.join(TMP, "ng_test_font.ttf")
with open(FONT_PATH, "wb") as f:
    f.write(b"NG-TEST-FONT-BYTES")

_orig_extract = extract.extract
extract.extract = syntheng.extract_for_test  # 引擎替身：合成游戏无进程可注入
util.app_dir = lambda: TMP  # 注册库与项目资产目录随之指向临时目录
try:
    game = copy_game("synth_basic")
    cfg = make_cfg(stub.base_url, FONT_PATH)
    p = make_project(cfg, game, "synth_basic")

    check("项目数据目录按项目身份存放（不再由目录名推导）",
          os.path.dirname(p.work) == os.path.join(TMP, "work")
          and os.path.basename(p.work) == p.project_id
          and "synth_basic" not in p.work,
          p.work)

    # ---------- 提取（引擎替身 + 补提取 + 补丁解析，走真实 extract_tl） ----------
    p.extract_tl(log=lambda m: None)
    dump_path = os.path.join(p.work, "dump.json")
    dump = util.read_json(dump_path)
    check("提取得到对白标识符", len(dump) == 16, "got %d" % len(dump))
    who_set = {n.get("who") for e in dump.values() for n in e.get("nodes", [])}
    check("多主角说话人齐全（含字符串字面量说话人）",
          {"e", "m", "", '"Guard"'} <= who_set, repr(sorted(map(str, who_set))))
    pystrings_path = os.path.join(game, "game", "tl", "chinese", "zz_ng_pystrings.rpy")
    check("Python 字面量补提取（init python 文本）",
          os.path.isfile(pystrings_path)
          and 'old "Grand Opening Soon"' in read(pystrings_path))
    check("运行时过滤器已写入",
          os.path.isfile(os.path.join(game, "game", "zz_ng_dyntrans.rpy")))

    # ---------- 生成任务：每类形态的断言 ----------
    jobs = jobs_by_key(game, dump_path)
    say_keys = [k for k, j in jobs.items() if j["kind"] == "say"]
    str_keys = [k for k, j in jobs.items() if j["kind"] == "string"]

    check("普通对白成为任务", "Welcome to the Synth Vale." not in str_keys
          and any(j["old"] == "Welcome to the Synth Vale." for j in jobs.values()))
    check("菜单字幕走 strings 任务（现代引擎路径）",
          "S:Ask about the vale" in jobs and jobs["S:Ask about the vale"]["kind"] == "string")
    check("引擎 common 字符串纳入任务",
          "S:Skip" in jobs and "S:Self-Voicing" in jobs)
    check("developer 字符串被过滤",
          "S:Console" not in jobs and "S:Skip" in jobs)

    branch = [k for k, j in jobs.items()
              if j["kind"] == "say" and j["old"] == "The river remembers."]
    check("同一原文的不同分支 -> 两个稳定任务 key", len(branch) == 2
          and branch[0].split("_")[0] != branch[1].split("_")[0], repr(branch))

    check("同一 label 中的多个菜单节点字幕齐全",
          {"S:Ask about the vale", "S:Leave", "S:Look closer"} <= set(str_keys))
    check("跨菜单重复字幕合并为单一 strings 条目（现语义：同文同译）",
          sum(1 for j in jobs.values() if j["old"] == "Leave") == 1)

    dyn = next((j for j in jobs.values()
                if j["old"] == "You are [player_name], aren't you?"), None)
    check("动态变量对白带插值变量", dyn is not None and "[player_name]" in dyn["old"])

    ml = next((j for j in jobs.values()
               if j["old"] == "This blessing spans\nseveral quiet lines."), None)
    check("多行字符串作为一条任务", ml is not None)

    guard = next((j for j in jobs.values()
                  if j["old"] == "Halt. Who goes there?"), None)
    check("字符串字面量说话人任务", guard is not None and guard["who"] == '"Guard"')

    # ---------- stub 服务连接翻译 + 回填 ----------
    rel_path = os.path.join(p.work, "relations.json")
    util.write_json(rel_path, [
        {"name": "Eileen", "name_cn": "艾琳", "gender": "f", "relation": "friend"},
        {"name": "Marisol", "name_cn": "玛丽索", "gender": "f", "relation": "friend"},
    ])
    util.write_json(os.path.join(p.work, "relation_words.json"),
                    [{"en": "sister", "cn": "姐姐"}])

    n, missing = p.translate(log=lambda m: None)
    check("stub 服务连接被真实调用", stub.request_count >= 1,
          "requests=%d" % stub.request_count)
    check("全部任务翻译完成（剩余 0）", missing == 0, "missing=%d" % missing)

    trans = util.read_json(os.path.join(p.work, "translations.json"))
    river_keys = branch
    check("两个分支的 key 都有自己的译文缓存",
          all(k in trans for k in river_keys), repr(sorted(trans)[:5]))
    check("分支同文沿用同一译文（现语义：同文同译）",
          trans[river_keys[0]] == trans[river_keys[1]] == "河水记得。")

    tl_main = os.path.join(game, "game", "tl", "chinese", "script.rpy")
    content = read(tl_main)
    check("对白回填：译文写入代码行", 'e "欢迎来到合成之谷。"' in content)
    check("对白回填：注释锚点保持原文", '# e "Welcome to the Synth Vale."' in content)
    check("变量对白回填保留插值", 'e "你是[player_name]，对吧？"' in content)
    check("文本标签在译文中保留", 'e "我{i}已经{/i}准备好了。"' in content)
    check("旁白行回填", '"今夜山谷很安静。"' in content)
    check("字符串说话人行回填到台词串", '"Guard" "站住。来者何人？"' in content)
    check("多行字符串回填（换行转义写回）",
          "这份祝福横跨\\n数行静谧。" in content)
    check("转义与 [[ 字面量回填", "和[[方括号]保持原样。" in content)
    check("%% 与 {color} 标签回填",
          "进度：[progress]%% {color=#ccffcc}仍在继续{/color}。" in content)
    check("菜单字幕回填 old/new 对（重复字幕）",
          'old "Leave"\n    new "离开"' in content)
    check("菜单字幕回填 old/new 对（条件字幕）",
          'old "Ask about the vale"\n    new "打听山谷的事"' in content)
    check("关系词回显行回填并保留插值",
          'e "那么你现在是我的[relation1]了。"' in content)
    check("分支两处各自回填", content.count('e "河水记得。"') == 2,
          "count=%d" % content.count('e "河水记得。"'))
    check("init python _() 文本回填", 'new "合成之谷"' in content)

    common_path = os.path.join(game, "game", "tl", "chinese", "common.rpy")
    common = read(common_path)
    check("引擎 common 字符串回填", 'new "跳过"' in common and 'new "自动朗读"' in common
          and "Console" not in common)
    pystrings_out = read(pystrings_path)
    check("补提取条目回填", 'new "【Grand Opening Soon】"' in pystrings_out
          and 'new "Sofia 对你来说是什么？"' in pystrings_out)

    # 结构完整性：注释行与代码行一一对应（块尾附带的下一块 "# file:line"
    # 位置头注释是引擎格式的一部分，不计入），无残留空 new
    loc_head = re.compile(r"^# \S+:\d+$")
    for path in (tl_main, common_path):
        tf = tlgen.TlFile(path)
        for (lang, bid), body in tlgen._iter_blocks(tf.lines):
            comments = [tf.lines[k].strip() for k in body
                        if tf.lines[k].strip().startswith("#")
                        and not loc_head.match(tf.lines[k].strip())]
            lives = [k for k in body if tf.lines[k].strip()
                     and not tf.lines[k].strip().startswith("#")]
            if bid == "strings":
                check("strings 块 old/new 成对（%s）" % bid, len(lives) % 2 == 0)
            else:
                check("对白块注释/代码一一对应（%s）" % bid,
                      len(comments) == len(lives), "%d vs %d" % (len(comments), len(lives)))
    check("无残留空译文", 'new ""' not in content and 'new ""' not in common)

    # ---------- 应用到游戏：字体 / 语言入口 / 人名与关系词显示层 ----------
    p.apply_font(log=lambda m: None)
    check("字体文件复制进游戏",
          os.path.isfile(os.path.join(game, "game", "fonts", "ng_test_font.ttf")))
    check("游戏引用字体被物理替换",
          read(os.path.join(game, "game", "fonts", "myfont.ttf")) == "NG-TEST-FONT-BYTES")
    backup = os.path.join(game, "game", "fonts_ng_backup", "fonts", "myfont.ttf")
    check("替换前有字体备份", os.path.isfile(backup)
          and "PLACEHOLDER" in read(backup))
    check("tl 字体覆盖脚本写入",
          'gui.text_font = "fonts/ng_test_font.ttf"'
          in read(os.path.join(game, "game", "tl", "chinese", "zz_ng_font.rpy")))

    p.apply_lang_entry(log=lambda m: None)
    lang_entry = read(os.path.join(game, "game", "zz_ng_language.rpy"))
    check("语言入口写入", 'Language("chinese")' in lang_entry)

    p.apply_names(log=lambda m: None)
    text_helpers = read(os.path.join(game, "game", "zz_ng_text.rpy"))
    check("人名显示映射（真实人名 -> 译名）",
          "'Eileen': '艾琳'" in text_helpers and "'Marisol': '玛丽索'" in text_helpers)
    check("龙套名走通用词典", "'Guard': '卫兵'" in text_helpers)
    check("关系词显示表写入", "'sister': '姐姐'" in text_helpers)

    # ---------- 再次提取（应用之后）：幂等 + 断点续翻 ----------
    snap = snapshot_tl(game)
    dump_before = read(dump_path)
    p.extract_tl(log=lambda m: None)
    check("再次提取：tl 骨架不覆盖已填译文与应用产物",
          snap == snapshot_tl(game))
    check("再次提取：dump 标识符稳定", read(dump_path) == dump_before)
    jobs2 = jobs_by_key(game, dump_path)
    check("再次提取：任务 key 完全一致", sorted(jobs2) == sorted(jobs))
    est = p.estimate(log=lambda m: None)
    check("再次提取后无需重复翻译（断点续翻）",
          est["lines"] == est["done"] and est["left"] == 0,
          "done=%d/%d" % (est["done"], est["lines"]))

    # ---------- 不触碰真实 work/ ----------
    REAL_WORK_AFTER = (sorted(os.listdir(REAL_WORK))
                       if os.path.isdir(REAL_WORK) else None)
    check("真实 work/ 目录未被触碰", REAL_WORK_BEFORE == REAL_WORK_AFTER)
except Exception as e:  # noqa: BLE001 —— 任何环节失败都要记录而不是中断其余断言输出
    import traceback
    check("synth_basic 端到端流程无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# synth_ipatch：ipatch 各种覆盖形式走完整流水线
# =====================================================================
try:
    stub2 = stubserver.StubTranslator(table={
        "I really trust your mom this time.": "这一次我真的相信你妈。",
        "I missed you, Mom.": "我也想你，妈妈。",
        "Sis?": "姐？",
        "Welcome to the manor.": "欢迎来到庄园。",
        "Mom day": "妈咪日",
        "your mom": "你妈",
        "Just an ordinary line.": "只是普通的一行。",
    }).start()
    game2 = copy_game("synth_ipatch")
    cfg2 = make_cfg(stub2.base_url, "")
    p2 = make_project(cfg2, game2, "synth_ipatch")

    p2.extract_tl(log=lambda m: None)
    dump2_path = os.path.join(p2.work, "dump.json")
    check("ipatch 解析结果落盘",
          os.path.isfile(os.path.join(p2.work, "ipatch.json")))

    ov = ipatch.build_overlay(game2, p2.work, log=None)
    check("五种补丁机制全部解析",
          ov is not None and len(ov.pairs) >= 4 and len(ov.text_map) == 2
          and len(ov.nodes) == 1 and ov.vars.get("Landlady") == "Mother"
          and ov.who.get("d") == "Mom", ov.summary() if ov else "None")
    check("补丁自有输入文本并入补提取骨架",
          'old "(default is Sister)."' in read(os.path.join(
              game2, "game", "tl", "chinese", "zz_ng_pystrings.rpy"))
          and 'old "Sister"' in read(os.path.join(
              game2, "game", "tl", "chinese", "zz_ng_pystrings.rpy")))

    jobs2 = jobs_by_key(game2, dump2_path)
    ipatch.overlay_jobs(list(jobs2.values()), ov)
    say_trust = next(j for j in jobs2.values()
                     if j.get("orig") == "I trust Diana this time.")
    check("伪语言块整句改写作为翻译源（原文进 orig）",
          say_trust["old"] == "I really trust your mom this time.")
    say_map = next(j for j in jobs2.values()
                   if j.get("orig") == "I missed you, Kate.")
    check("整句字典补丁精确改写", say_map["old"] == "I missed you, Mom.")
    say_est = next(j for j in jobs2.values()
                   if j.get("orig") == "Welcome to the estate.")
    check("替换链补丁改写翻译源", say_est["old"] == "Welcome to the manor.")
    str_day = jobs2.get("S:Landlady day")
    check("strings 条目同样被补丁改写（old 保原文）",
          str_day is not None and str_day.get("orig") == "Landlady day"
          and str_day["old"] == "Mom day")
    plain = next(j for j in jobs2.values()
                 if j["old"] == "Just an ordinary line.")
    check("名字带 patch 的普通剧情不被误判", not plain.get("orig"))

    n2, missing2 = p2.translate(log=lambda m: None)
    check("补丁覆盖后全部翻译完成", missing2 == 0, "missing=%d" % missing2)

    content2 = read(os.path.join(game2, "game", "tl", "chinese", "script.rpy"))
    py2 = read(os.path.join(game2, "game", "tl", "chinese", "zz_ng_pystrings.rpy"))
    check("整句改写：tl 锚点保持原文、译文来自补丁后文本",
          '# d "I trust Diana this time."' in content2
          and "这一次我真的相信你妈。" in content2)
    check("字典补丁：译文按补丁后整句回填",
          '# d "I missed you, Kate."' in content2 and "我也想你，妈妈。" in content2)
    check("替换链补丁：译文按补丁后文本回填",
          '# d "Welcome to the estate."' in content2 and "欢迎来到庄园。" in content2)
    check("字符串说话人 + 字典补丁回填",
          '# k "Chloe?"' in content2 and 'k "姐？"' in content2)
    check("strings 补丁条目：old 保原文、new 用补丁后文本的译文",
          'old "Landlady day"\n    new "妈咪日"' in py2)
    check("人名字符串同样被补丁改写并回填",
          'old "Diana"\n    new "你妈"' in py2)
    story_out = read(os.path.join(game2, "game", "tl", "chinese", "storypatch.rpy"))
    check("storypatch 台词照常回填", "只是普通的一行。" in story_out)
    stub2.stop()
except Exception as e:  # noqa: BLE001
    import traceback
    check("synth_ipatch 端到端流程无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# 工单 03：同名游戏、不同路径 —— 项目身份隔离（串数据为 0）
# =====================================================================
try:
    reg = registry.Registry()   # app_dir 已重定向：注册库落在临时目录
    gameA = os.path.join(TMP, "packA", "My Game")
    gameB = os.path.join(TMP, "packB", "My Game")
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), gameA)
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), gameB)
    pa_id = reg.create_project("My Game", gameA)["id"]
    pb_id = reg.create_project("My Game", gameB)["id"]
    check("同名游戏两个安装登记为两个互不相干的项目", pa_id != pb_id
          and reg.find_by_path(gameA)["id"] == pa_id
          and reg.find_by_path(gameB)["id"] == pb_id)
    reg.close()

    cfgA = make_cfg(stub.base_url, "")
    cfgB = make_cfg(stub.base_url, "")
    cfgB["apply_font"] = False
    cfgB["apply_lang_entry"] = False
    pa = pipeline.Project(cfgA, gameA, pa_id)
    pb = pipeline.Project(cfgB, gameB, pb_id)

    pa.extract_tl(log=lambda m: None)
    pb.extract_tl(log=lambda m: None)
    check("同名游戏的两个项目数据目录完全独立",
          pa.work != pb.work
          and os.path.dirname(pa.work) == os.path.dirname(pb.work) == os.path.join(TMP, "work")
          and "My Game" not in pa.work and "My Game" not in pb.work,
          "%s | %s" % (pa.work, pb.work))

    # A 端写入词汇表和译文标记；B 端必须什么都看不到
    util.write_json(os.path.join(pa.work, "glossary.json"),
                    [{"en": "vale", "cn": "幽谷（A 项目专用）"}])
    util.write_json(os.path.join(pa.work, "translations.json"),
                    {"k_marker_A": "A 项目的译文"})
    check("B 项目库看不到 A 的词汇表与译文",
          not os.path.isfile(os.path.join(pb.work, "glossary.json"))
          and not os.path.isfile(os.path.join(pb.work, "translations.json")))
    check("B 的提取结果在自己的项目库里",
          os.path.isfile(os.path.join(pb.work, "dump.json")))

    # B 端按自己的译文回填同一英文（不同项目、不同译法），A 端分毫不动
    dumpB = util.read_json(os.path.join(pb.work, "dump.json"))
    jobsB, _tlb = tlgen.build_jobs(gameB, "chinese", dumpB, include_strings=True,
                                   context_lines=2)
    riverB = [j["key"] for j in jobsB if j["kind"] == "say"
              and j["old"] == "The river remembers."]
    check("两个项目对同一源文本得到结构一致的任务清单", len(riverB) == 2, repr(riverB))
    util.write_json(os.path.join(pb.work, "translations.json"),
                    {k: "河流记得（B 项目专用）。" for k in riverB})
    pb.apply_txn(log=lambda m: None)

    contentA = read(os.path.join(gameA, "game", "tl", "chinese", "script.rpy"))
    contentB = read(os.path.join(gameB, "game", "tl", "chinese", "script.rpy"))
    check("B 端回填的是 B 自己的译文", "河流记得（B 项目专用）。" in contentB)
    check("A 的游戏与项目库不受 B 影响",
          "河流记得（B 项目专用）。" not in contentA
          and util.read_json(os.path.join(pa.work, "translations.json"))
          == {"k_marker_A": "A 项目的译文"}
          and util.read_json(os.path.join(pa.work, "glossary.json"))
          == [{"en": "vale", "cn": "幽谷（A 项目专用）"}])
except Exception as e:  # noqa: BLE001
    import traceback
    check("同名游戏隔离回归无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# 工单 05：项目数据库 —— 出现位置稳定、独立分支、人工保护、迁移建议
# =====================================================================
try:
    stub5 = stubserver.StubTranslator(table={
        "The river remembers.": "河水记得。",
    }).start()
    game5 = os.path.join(TMP, "games", "synth_basic_store")
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game5)
    cfg5 = make_cfg(stub5.base_url, "")
    p5 = make_project(cfg5, game5, "synth_basic_store")

    p5.extract_tl(log=lambda m: None)
    dump5_path = os.path.join(p5.work, "dump.json")
    p5.translate(log=lambda m: None)
    check("项目库随翻译建立（project.db 在项目资产目录）",
          os.path.isfile(os.path.join(p5.work, "project.db")))
    store5 = p5.store()
    jobs5 = jobs_by_key(game5, dump5_path)
    expect_n = sum(1 for j in jobs5.values() if j.get("old"))
    st5 = store5.stats()
    check("全部出现位置同步入库且都有当前译文记录（可对账）",
          st5["occurrences"] == expect_n and st5["currents"] == expect_n, st5)

    river5 = sorted(k for k, j in jobs5.items()
                    if j["kind"] == "say" and j["old"] == "The river remembers.")
    check("同一英文原文的两个分支出现位置各自入库", len(river5) == 2)
    check("译文记录带来源与结构指纹/源文本快照",
          all(store5.get_current(k)["source"] == "model"
              and store5.get_current(k)["fingerprint"]
              and store5.get_current(k)["source_text"] for k in river5))

    # 出现位置标识稳定：相同提取结果再次同步，零变化（幂等）
    recs5 = [project_store.record_from_job(j) for j in jobs5.values() if j.get("old")]
    rep5 = store5.sync_occurrences(recs5)
    check("再次同步相同提取结果幂等（出现位置标识稳定）",
          rep5["added"] == 0 and rep5["changed"] == 0 and rep5["removed"] == 0
          and rep5["demoted"] == 0, rep5)

    # ---------- 独立分支 + 人工保护：一支人工定稿，重译结果只进候选 ----------
    k_fixed, k_redo = river5
    store5.set_human_translation(k_fixed, "河水记得（定稿）。")
    store5.request_retranslation(k_fixed)
    store5.request_retranslation(k_redo)
    stub5.table["The river remembers."] = "河水的新译本。"
    p5.translate(log=lambda m: None)
    cur_fixed = store5.get_current(k_fixed)
    check("人工定稿的译文在重新翻译后保持为当前译文",
          cur_fixed["text"] == "河水记得（定稿）。" and cur_fixed["confirmed"] is True
          and cur_fixed["source"] == "human")
    check("人工译文的重译结果进入候选译文（被替换的旧译文也保留为候选版本）",
          "河水的新译本。" in [c["text"] for c in store5.candidates(k_fixed)]
          and store5.get_current(k_fixed)["text"] == "河水记得（定稿）。")
    cur_redo = store5.get_current(k_redo)
    check("未确认译文的重译按新结果更新（来源仍为模型）",
          cur_redo["text"] == "河水的新译本。" and cur_redo["source"] == "model")
    check("重译请求标记已清除", store5.retranslation_requested() == [])
    content5 = read(os.path.join(game5, "game", "tl", "chinese", "script.rpy"))
    check("同一英文的两个分支独立回填各自的译文",
          content5.count('e "河水记得（定稿）。"') == 1
          and content5.count('e "河水的新译本。"') == 1,
          "定稿=%d 新译=%d" % (content5.count('e "河水记得（定稿）。"'),
                              content5.count('e "河水的新译本。"')))

    # ---------- 源文本变化：旧译文降为迁移建议，标识保持稳定 ----------
    fam_key = next(k for k, j in jobs5.items() if j["old"] == "Family is family.")
    fam_text = store5.get_current(fam_key)["text"]
    script5 = os.path.join(game5, "game", "script.rpy")
    with open(script5, "r", encoding="utf-8") as f:
        src5 = f.read()
    with open(script5, "w", encoding="utf-8") as f:
        f.write(src5.replace('m "Family is family."', 'm "Family is everything."'))
    p5.extract_tl(log=lambda m: None)
    p5.estimate(log=lambda m: None)  # 触发任务构建 -> 出现位置同步
    check("源文本变化后旧出现位置不再持有当前译文",
          store5.get_current(fam_key) is None)
    sug = next(s for s in store5.suggestions() if s["occurrence_id"] == fam_key)
    check("旧译文降为迁移建议，带旧文本快照与来源说明",
          sug["text"] == fam_text and sug["source_text"] == "Family is family."
          and (sug["note"] or "").strip() != "", sug["note"])
    jobs5b = jobs_by_key(game5, dump5_path)
    fam_new = [k for k, j in jobs5b.items() if j["old"] == "Family is everything."]
    check("改写后的文本成为新出现位置（尚无当前译文）",
          len(fam_new) == 1 and store5.get_current(fam_new[0]) is None)
    river_after = sorted(k for k, j in jobs5b.items()
                         if j["kind"] == "say" and j["old"] == "The river remembers.")
    check("未受影响的出现位置标识在再次提取后保持稳定",
          river_after == river5
          and store5.get_current(k_fixed)["text"] == "河水记得（定稿）。")
    stub5.stop()
except Exception as e:  # noqa: BLE001 —— 任何环节失败都要记录而不是中断其余断言输出
    import traceback
    check("工单 05 项目库回归无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# 工单 06：单项目单写入任务协调器 —— 登记互斥、他项目无锁、并发编辑不丢
# =====================================================================
try:
    game6 = os.path.join(TMP, "games", "synth_basic_coord")
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game6)
    p6 = make_project(make_cfg(stub.base_url, ""), game6, "synth_basic_coord")
    p6.extract_tl(log=lambda m: None)
    coord6 = coordinator.TaskCoordinator(p6.store())

    with coord6.begin("翻译"):
        try:
            coord6.begin("提取")
            check("同一项目第二个核心数据任务被拒绝", False, "登记竟然成功")
        except coordinator.TaskBusy:
            check("同一项目第二个核心数据任务被拒绝", True)
        # 其他汉化项目是独立项目库：本项目任务运行期间照常登记与收尾
        game6b = os.path.join(TMP, "games", "synth_basic_coord_b")
        shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game6b)
        p6b = make_project(make_cfg(stub.base_url, ""), game6b, "synth_basic_coord_b")
        p6b.extract_tl(log=lambda m: None)
        with coordinator.TaskCoordinator(p6b.store()).begin("翻译"):
            pass
        check("另一项目在本项目任务运行期间照常登记任务",
              coordinator.TaskCoordinator(p6b.store()).history()[0]["state"] == "done")

    with coord6.begin("翻译"):
        p6.translate(log=lambda m: None)
    hist6 = coord6.history()
    check("翻译任务经协调器登记并正常收尾（带任务摘要）",
          hist6[0]["kind"] == "翻译" and hist6[0]["state"] == "done", hist6[0])

    # ---------- 翻译运行时的并发编辑：两边数据都不丢失 ----------
    class _Gated(stubserver.StubTranslator):
        """第 2 个请求挂起直到放行：制造确定性的并发编辑窗口。"""

        def __init__(self, table=None):
            super().__init__(table)
            self.gate = threading.Event()
            self.reached = threading.Event()

        def _gate(self, n):
            if n >= 2:
                self.reached.set()
                self.gate.wait(timeout=120)

    stub6 = _Gated().start()
    try:
        game6c = os.path.join(TMP, "games", "synth_basic_coord_edit")
        shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game6c)
        cfg6c = make_cfg(stub6.base_url, "")
        cfg6c["engine"]["concurrency"] = 1   # 批次串行：第 2 批请求到达 ⇒ 第 1 批已提交
        p6c = make_project(cfg6c, game6c, "synth_basic_coord_edit")
        p6c.extract_tl(log=lambda m: None)
        coord6c = coordinator.TaskCoordinator(p6c.store())
        err6 = []

        def _translate6():
            try:
                with coord6c.begin("翻译"):
                    p6c.translate(log=lambda m: None)
            except Exception as e:  # noqa: BLE001
                err6.append(e)

        th6 = threading.Thread(target=_translate6)
        th6.start()
        check("并发窗口到达（第 2 批在途，第 1 批已提交）", stub6.reached.wait(60))
        store6c = p6c.store()
        first6 = batch6(stub6.requests[0])
        later6 = batch6(stub6.requests[1])
        _missing6 = [k for k in first6 if store6c.get_current(k) is None]
        check("前一批请求已按记录事务提交（第 1 批当前译文在库）",
              not _missing6, "first=%s missing=%s" % (first6, _missing6))
        conflict6 = later6[0]      # 任务即将翻译的记录：与其冲突
        store6c.set_human_translation(conflict6, "任务期间的人工编辑。")
        stub6.gate.set()
        th6.join(120)
        check("翻译任务并发回归无异常", not err6 and not th6.is_alive(), repr(err6))
        cur6 = store6c.get_current(conflict6)
        check("任务运行期间的人工编辑保持为当前译文",
              cur6 is not None and cur6["text"] == "任务期间的人工编辑。" and cur6["confirmed"])
        cand6 = [c for c in store6c.candidates(conflict6) if c["text"] != cur6["text"]]
        check("任务结果与人工编辑冲突时进入带说明的候选译文（任务结束后可比较采用）",
              len(cand6) == 1 and "冲突" in (cand6[0]["note"] or ""),
              cand6 and cand6[0]["note"])
        st6c = store6c.stats()
        check("并发编辑与翻译结果合并不丢失（全部出现位置都有当前译文）",
              st6c["currents"] == st6c["occurrences"], st6c)
        check("并发翻译任务经协调器正常收尾", coord6c.history()[0]["state"] == "done")
    finally:
        stub6.stop()
except Exception as e:  # noqa: BLE001
    import traceback
    check("工单 06 协调器回归无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# 工单 07:事务式应用与恢复 —— 暂存/检查/替换故障注入、恢复点、停用汉化
# =====================================================================
try:
    game7 = os.path.join(TMP, "games", "synth_basic_apply")
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game7)
    cfg7 = make_cfg(stub.base_url, FONT_PATH)
    p7 = make_project(cfg7, game7, "synth_basic_apply")
    p7.extract_tl(log=lambda m: None)
    p7.translate(log=lambda m: None)   # stub 服务连接翻译(项目库提交 + 回填)

    # 玩家自己的文件在基线之前就存在:停用绝不触碰
    with open(os.path.join(game7, "game", "player_mod.rpy"), "w",
              encoding="utf-8") as f:
        f.write("# 玩家自己的文件\n")
    tree7 = {k: v for k, v in applytxn.snapshot_tree(game7).items()
             if not k.startswith("game/fonts_ng_backup/")}   # 应用前基线

    s7 = p7.apply_txn(log=lambda m: None)
    g7 = os.path.join(game7, "game")
    check("应用事务器:任务摘要可验证(成功/跳过/待检查)",
          s7["filled"] > 0 and s7["untranslated"] == 0 and s7["dropped"] == 0
          and s7["files_total"] > 0 and s7["fonts_replaced"] == 1, s7["filled"])
    check("应用事务器:译文+显示层+字体+语言入口一并生效",
          "欢迎来到合成之谷。" in read(os.path.join(g7, "tl", "chinese", "script.rpy"))
          and os.path.isfile(os.path.join(g7, "zz_ng_text.rpy"))
          and read(os.path.join(g7, "fonts", "myfont.ttf")) == "NG-TEST-FONT-BYTES"
          and 'Language("chinese")' in read(os.path.join(g7, "zz_ng_language.rpy")))
    m7 = applytxn.list_applies(p7.work)
    check("应用事务器:应用清单与恢复点生成(带归属与前后哈希)",
          len(m7) == 1 and m7[0]["apply_id"] == s7["apply_id"]
          and {"tl", "font", "lang_entry", "text_helpers"}
          <= {e["owner"] for e in m7[0]["files"]}
          and all(e.get("after") for e in m7[0]["files"]
                  if e["action"] != "removed"))

    # 二次应用:改人名译法后显示层随译文刷新(无隐藏例外)
    util.write_json(os.path.join(p7.work, "relations.json"), [
        {"name": "Eileen", "name_cn": "小艾", "gender": "f", "relation": "friend"},
        {"name": "Marisol", "name_cn": "玛丽索", "gender": "f", "relation": "friend"}])
    s7b = p7.apply_txn(log=lambda m: None)
    text7 = read(os.path.join(g7, "zz_ng_text.rpy"))
    check("二次应用刷新人名与关系词显示层(不存在隐藏例外)",
          "'Eileen': '小艾'" in text7 and "'艾琳'" not in text7
          and len(applytxn.list_applies(p7.work)) == 2,
          s7b["apply_id"])
    applied7 = {k: v for k, v in applytxn.snapshot_tree(game7).items()
                if not k.startswith("game/fonts_ng_backup/")}

    # ---- 故障注入:暂存/替换阶段失败,游戏目录保持应用前状态 ----
    cfg7["font_path"] = os.path.join(TMP, "missing.ttf")
    try:
        p7.apply_txn(log=lambda m: None)
        check("暂存阶段失败:应用中止", False, "竟然成功")
    except applytxn.ApplyError:
        check("暂存阶段失败:应用中止且游戏目录零改动",
              {k: v for k, v in applytxn.snapshot_tree(game7).items()
               if not k.startswith("game/fonts_ng_backup/")} == applied7)
    cfg7["font_path"] = FONT_PATH

    orig_install = applytxn._install_file
    calls = {"n": 0}

    def flaky_install(game_base, entry):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("e2e 注入:替换第 3 个文件时故障")
        return orig_install(game_base, entry)

    applytxn._install_file = flaky_install
    try:
        p7.apply_txn(log=lambda m: None)
        check("替换阶段失败:回滚", False, "竟然成功")
    except OSError:
        check("替换阶段失败:游戏目录回滚到应用前状态(无半应用)",
              {k: v for k, v in applytxn.snapshot_tree(game7).items()
               if not k.startswith("game/fonts_ng_backup/")} == applied7)
    finally:
        applytxn._install_file = orig_install
    check("失败的应用不产生应用清单", len(applytxn.list_applies(p7.work)) == 2)

    # ---- 停用汉化:只撤销工具管理的文件,项目资产保留 ----
    # 期望 = 应用前基线(含玩家文件与提取写的运行时过滤器——后者是 uipatch
    # 改写脚本的运行时依赖,停用时保留)
    expect7 = dict(tree7)
    r7 = applytxn.deactivate(p7.work, game7, cfg7, "chinese", log=lambda m: None)
    after7 = {k: v for k, v in applytxn.snapshot_tree(game7).items()
              if not k.startswith("game/fonts_ng_backup/")}
    check("停用汉化:游戏目录回到应用前(玩家文件不动、原字体还原、工具文件清理)",
          after7 == expect7 and os.path.isfile(os.path.join(g7, "player_mod.rpy"))
          and read(os.path.join(g7, "fonts", "myfont.ttf")) != "NG-TEST-FONT-BYTES",
          r7)
    check("停用汉化:汉化项目及其资产保留(译文在库、恢复点在)",
          p7.store().currents() and len(applytxn.list_applies(p7.work)) == 2)
    s7c = p7.apply_txn(log=lambda m: None)
    check("停用后再次应用即恢复汉化",
          "欢迎来到合成之谷。" in read(os.path.join(g7, "tl", "chinese", "script.rpy"))
          and s7c["apply_id"] not in (s7["apply_id"], s7b["apply_id"]))
except Exception as e:  # noqa: BLE001
    import traceback
    check("工单 07 应用事务回归无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# 工单 08:外部 tl 变更检测 —— 基线自动保存、应用前检测、逐项导入/放弃留痕
# =====================================================================
try:
    from core import externaltl

    game8 = os.path.join(TMP, "games", "synth_basic_exttl")
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game8)
    cfg8 = make_cfg(stub.base_url, FONT_PATH)
    p8 = make_project(cfg8, game8, "synth_basic_exttl")
    p8.extract_tl(log=lambda m: None)
    p8.translate(log=lambda m: None)   # stub 服务连接翻译(项目库提交 + 回填)
    s8 = p8.apply_txn(log=lambda m: None)
    jobs8 = jobs_by_key(game8, os.path.join(p8.work, "dump.json"))
    tl8 = os.path.join(game8, "game", "tl", "chinese", "script.rpy")

    def exttl_confirm(mode):
        """确认通道替身:按模式返回逐项决策(import/discard)或中止。"""
        def confirm(message):
            assert message.startswith("EXTTL:"), "外部变更确认协议走 EXTTL 通道"
            payload = json.loads(message.split(":", 1)[1])
            ids = [i["id"] for i in payload["items"]]
            if mode == "abort":
                return "abort"
            imports = [i["id"] for i in payload["items"]
                       if i["importable"]] if mode == "import" else []
            return json.dumps({"import": imports,
                               "discard": [i for i in ids if i not in imports]})
        return confirm

    def never_confirm(message):
        raise AssertionError("无外部变更时不应弹出确认")

    def game_tl_edit(was, now):
        """在汉化工作台之外改写游戏 tl(只动非注释的代码行)。"""
        out = []
        hit = False
        for ln in read(tl8).split("\n"):
            if not ln.strip().startswith("#") and '"%s"' % was in ln:
                ln, hit = ln.replace('"%s"' % was, '"%s"' % now, 1), True
            out.append(ln)
        assert hit, "外部改写没有命中译文行"
        with open(tl8, "w", encoding="utf-8") as f:
            f.write("\n".join(out))

    check("外部变更:基线随首次应用自动保存",
          externaltl.load_baseline(p8.work) is not None
          and "script.rpy" in externaltl.load_baseline(p8.work)["files"],
          s8["apply_id"])
    # 无外部变更:再次应用不被打扰(确认通道一旦被调用即断言失败)
    s8b = p8.apply_txn(log=lambda m: None, confirm=never_confirm)
    check("外部变更:无变更不打扰,应用照常完成",
          s8b["filled"] > 0 and s8b["external"] == {"detected": 0, "imported": 0,
                                                    "discarded": 0})

    # ---- 外部改写一句已回填译文:阻止直接覆盖 → 逐项导入(人工来源+保护) ----
    key8 = next(k for k, j in jobs8.items()
                if j["old"] == "Welcome to the Synth Vale.")
    game_tl_edit("欢迎来到合成之谷。", "【手改】欢迎来到合成之谷。")
    try:
        p8.apply_txn(log=lambda m: None)
        check("外部变更:无确认通道的应用被阻止", False, "竟然成功")
    except externaltl.ExternalChangesError:
        check("外部变更:无确认通道的应用被阻止(绝不静默覆盖)",
              "【手改】" in read(tl8))
    s8c = p8.apply_txn(log=lambda m: None, confirm=exttl_confirm("import"))
    cur8 = p8.store().get_current(key8)
    check("外部变更:逐项导入登记为人工来源的当前译文",
          s8c["external"] == {"detected": 1, "imported": 1, "discarded": 0}
          and cur8["text"] == "【手改】欢迎来到合成之谷。"
          and cur8["source"] == "human" and cur8["confirmed"])
    p8.store().record_model_result(key8, "【模型重译】外来译文。")
    cur8 = p8.store().get_current(key8)
    check("外部变更:导入的译文受人工译文保护(模型重译只进候选)",
          cur8["text"] == "【手改】欢迎来到合成之谷。"
          and any(c["text"] == "【模型重译】外来译文。"
                  for c in p8.store().candidates(key8)))
    ec8 = applytxn.load_manifest(p8.work, s8c["apply_id"]).get("external_changes")
    check("外部变更:导入与放弃在应用清单留痕",
          ec8 is not None and ec8["detected"] == 1 and ec8["imported"] == 1
          and ec8["discarded"] == 0
          and [i["action"] for i in ec8.get("items", [])] == ["imported"])
    p8.apply_txn(log=lambda m: None, confirm=never_confirm)   # 基线已随应用更新

    # ---- 外部再改一句:明确放弃(覆盖 + 留痕)与取消(中止,游戏不动) ----
    game_tl_edit("这就走了？", "【手改】这就走了？")
    s8d = p8.apply_txn(log=lambda m: None, confirm=exttl_confirm("discard"))
    check("外部变更:明确放弃后本次应用覆盖外部值并留痕",
          s8d["external"] == {"detected": 1, "imported": 0, "discarded": 1}
          and '"【手改】这就走了？"' not in read(tl8)
          and '"这就走了？"' in read(tl8)
          and applytxn.load_manifest(p8.work, s8d["apply_id"])
          ["external_changes"]["discarded"] == 1)
    game_tl_edit("路还长着呢，[player_name]。", "【手改】长路漫漫。")
    s8e = p8.apply_txn(log=lambda m: None, confirm=exttl_confirm("abort"))
    check("外部变更:取消处理即中止应用,游戏目录保持现状",
          s8e.get("stopped") and "【手改】长路漫漫。" in read(tl8)
          and applytxn.load_manifest(p8.work, s8d["apply_id"])["apply_id"]
          == applytxn.list_applies(p8.work)[0]["apply_id"])
    p8.apply_txn(log=lambda m: None, confirm=exttl_confirm("import"))   # 收尾:导入

    # ---- 工具自身动作不误报:恢复点还原后再次应用不打扰 ----
    p8.restore_apply(s8["apply_id"], log=lambda m: None)
    s8f = p8.apply_txn(log=lambda m: None, confirm=never_confirm)
    check("外部变更:恢复点还原是工具动作,不误报为外部变更",
          s8f["filled"] > 0 and s8f["external"]["detected"] == 0, s8f["apply_id"])
except Exception as e:  # noqa: BLE001
    import traceback
    check("工单 08 外部变更回归无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# 工单 09:译文编辑页接入项目库 —— 出现位置行、独立编辑、批量替换、最新任务清单
# =====================================================================
try:
    game9 = os.path.join(TMP, "games", "synth_basic_editor")
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game9)
    p9 = make_project(make_cfg(stub.base_url, FONT_PATH), game9, "synth_basic_editor")
    p9.extract_tl(log=lambda m: None)
    p9.translate(log=lambda m: None)
    store9 = p9.store()
    tl9 = os.path.join(game9, "game", "tl", "chinese", "script.rpy")

    # ---------- 主列表：一个文本出现位置一行，带状态 ----------
    rows9 = p9.editor_rows()
    jobs9 = jobs_by_key(game9, os.path.join(p9.work, "dump.json"))
    check("编辑页按出现位置一行一条（与任务清单对账）",
          len(rows9) == sum(1 for j in jobs9.values() if j.get("old")),
          "%d rows" % len(rows9))
    river9 = [r for r in rows9 if r["source"] == "The river remembers."]
    check("同一原文的两个出现位置各占一行，same_source 标记影响范围",
          len(river9) == 2 and all(r["same_source"] == 2 for r in river9))
    check("已译行的状态字段（模型来源、未确认）",
          all(r["trans"] == "河水记得。" and r["origin"] == "model"
              and not r["confirmed"] for r in river9))
    check("strings 行同表展示（说话人为空、kind=string）",
          next(r for r in rows9 if r["key"] == "S:Leave")["who"] == "")

    # ---------- 修改此处：只写当前出现位置，另一处不受影响 ----------
    store9.set_human_translation(river9[0]["key"], "河水记得（编辑页定稿）。")
    rows9 = p9.editor_rows()
    river9 = [r for r in rows9 if r["source"] == "The river remembers."]
    edited9 = next(r for r in river9 if r["trans"] == "河水记得（编辑页定稿）。")
    other9 = next(r for r in river9 if r["key"] != edited9["key"])
    check("修改此处只写当前出现位置（另一分支保持原译文）",
          other9["trans"] == "河水记得。" and edited9["origin"] == "human"
          and edited9["confirmed"])
    check("保存草稿不修改游戏目录（应用到游戏才写入）",
          "河水记得（编辑页定稿）。" not in read(tl9))
    p9.apply_txn(log=lambda m: None)
    content9 = read(tl9)
    check("两处独立回填各自的译文",
          content9.count('e "河水记得（编辑页定稿）。"') == 1
          and content9.count('e "河水记得。"') == 1)

    # ---------- 替换全部相同原文：显式批量（影响范围 = same_source 分组） ----------
    for r in [x for x in rows9 if x["source"] == "The river remembers."]:
        store9.set_human_translation(r["key"], "河水记得（统一译法）。")
    p9.apply_txn(log=lambda m: None)
    check("替换全部相同原文后两处一并更新",
          read(tl9).count('e "河水记得（统一译法）。"') == 2)

    # ---------- 冲突/重译结果进待检查，编辑页比较后采用或忽略 ----------
    k9 = next(r["key"] for r in rows9 if r["source"] == "Welcome to the Synth Vale.")
    store9.set_human_translation(k9, "欢迎来到合成之谷（人工定稿）。")
    store9.record_model_result(k9, "【重译】合成谷欢迎你。")
    row9 = next(r for r in p9.editor_rows() if r["key"] == k9)
    check("人工译文与任务结果冲突：当前保持人工定稿，结果进候选（行标记待检查）",
          row9["trans"] == "欢迎来到合成之谷（人工定稿）。" and row9["confirmed"]
          and row9["candidates"] == 2,
          "candidates=%d（重译结果 + 被替换的旧模型版本）" % row9["candidates"])
    cand9 = next(c for c in store9.candidates(k9)
                 if c["text"] == "【重译】合成谷欢迎你。")
    store9.adopt_candidate(k9, cand9["id"])
    cur9 = store9.get_current(k9)
    row9 = next(r for r in p9.editor_rows() if r["key"] == k9)
    check("采用候选后成为人工确认的当前译文，待检查清除一个",
          cur9["text"] == "【重译】合成谷欢迎你。" and cur9["confirmed"]
          and row9["candidates"] == 2,
          "candidates=%d（被替换的人工定稿降为候选 + 旧模型版本）" % row9["candidates"])
    old9 = next(c for c in store9.candidates(k9)
                if c["text"] == "欢迎来到合成之谷（人工定稿）。")
    store9.dismiss_candidate(k9, old9["id"])
    check("忽略已比较过的候选（当前译文不受影响）",
          next(r for r in p9.editor_rows() if r["key"] == k9)["candidates"] == 1
          and store9.get_current(k9)["text"] == "【重译】合成谷欢迎你。")

    # ---------- 重新提取后编辑页立即使用最新任务清单 ----------
    road9 = next(r for r in rows9 if r["source"] == "The road is long, [player_name].")
    script9 = os.path.join(game9, "game", "script.rpy")
    with open(script9, "r", encoding="utf-8") as f:
        src9 = f.read()
    with open(script9, "w", encoding="utf-8") as f:
        f.write(src9.replace("The road is long, [player_name].",
                             "The road is longer now, [player_name]."))
    p9.extract_tl(log=lambda m: None)
    rows9b = p9.editor_rows()
    new_road9 = next(r for r in rows9b
                     if r["source"] == "The road is longer now, [player_name].")
    check("重新提取后编辑页用最新任务清单（改写后的文本在列、未译）",
          new_road9["trans"] == "" and not new_road9["confirmed"]
          and not any(r["source"] == "The road is long, [player_name]." for r in rows9b))
    check("消失位置的旧译文降为迁移建议（待检查，不静默丢弃）",
          any(s["text"] == "路还长着呢，[player_name]。" for s in store9.suggestions()))
    # 重新提取把 tl 骨架重写为新任务清单（旧块消失、新块未译）——应用前的
    # 外部变更检测按基线对账把这处差异摆给用户决定（工单 08 语义）；这里
    # 明确放弃（应用按当前任务清单重新回填）
    def discard_all9(message):
        assert message.startswith("EXTTL:"), "外部变更确认协议走 EXTTL 通道"
        payload9 = json.loads(message.split(":", 1)[1])
        assert all(not i["importable"] for i in payload9["items"]), \
            "骨架重写产生的差异只应是不可导入的结构性条目"
        return json.dumps({"import": [],
                           "discard": [i["id"] for i in payload9["items"]]})

    # 在最新清单上编辑并应用：一定写进游戏——不再"保存成功但游戏没有变化"
    store9.set_human_translation(new_road9["key"], "长路漫漫，改写过的版本。")
    p9.apply_txn(log=lambda m: None, confirm=discard_all9)
    check("最新清单上的编辑经应用落到游戏 tl",
          "长路漫漫，改写过的版本。" in read(tl9))

    # ---------- 项目任务运行期间：浏览与非冲突草稿编辑照常 ----------
    with coordinator.TaskCoordinator(store9).begin("翻译"):
        rows_busy9 = p9.editor_rows()
        store9.set_human_translation(other9["key"], "任务运行期间的人工编辑。")
    check("任务运行期间编辑页照常加载（最新任务清单 + 项目库并行读）",
          len(rows_busy9) == len(rows9b))
    check("任务运行期间的非冲突草稿编辑成功落库（协调器不冻结浏览与编辑）",
          store9.get_current(other9["key"])["text"] == "任务运行期间的人工编辑。"
          and coordinator.TaskCoordinator(store9).history()[0]["state"] == "done")
except Exception as e:  # noqa: BLE001
    import traceback
    check("工单 09 编辑页回归无异常", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

# =====================================================================
# 清理（无论成败都恢复补丁与目录）
# =====================================================================
try:
    stub.stop()
finally:
    extract.extract = _orig_extract
    util.app_dir = REAL_APP_DIR
    shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print("FAILED: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("ALL TESTS PASSED")


def test_all():
    """pytest 收集入口：上面的模块级脚本执行完毕后在此汇总结果。"""
    assert not failures, failures
