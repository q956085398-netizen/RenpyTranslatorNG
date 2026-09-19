# -*- coding: utf-8 -*-
"""ipatch 模块测试：真实补丁文件解析 + 合成管线端到端。"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import ipatch, tlgen, pystrings  # noqa: E402
from core import pipeline as pl  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, name, detail))
    if not cond:
        failures.append(name)


# ---------- Part 1: 真实补丁解析 ----------
# 本部分引用开发者本机的真实项目回归样本（受版权保护，不入库）：样本路径只存放在
# gitignored 的 tests/private_regression_cases.json（{"名称": "路径"}，名称仅作分组展示，
# 如 rtl 是文件名不含 ipatch、靠内容特征识别的字典型关系补丁），仓库内不出现任何本机绝对路径。
# 默认跳过；开发者建好该文件并设 NG_PRIVATE_REGRESSION=1 后才运行。
print("=== Part 1: 真实 ipatch 文件解析 ===")
CASES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "private_regression_cases.json")
CASES = {}
_manifest_error = None
if os.path.isfile(CASES_FILE):
    try:
        with open(CASES_FILE, encoding="utf-8") as f:
            CASES = json.load(f)
    except ValueError as e:
        _manifest_error = "JSON 解析失败: %s" % e
    if not _manifest_error and not isinstance(CASES, dict):
        _manifest_error = "应为 {名称: 路径} 的 JSON 对象"
    if _manifest_error:
        CASES = {}


def have(name):
    """样本游戏可能已被删/换版本：文件不在就跳过该组断言，不让整套测试挂掉。"""
    if not os.path.isfile(CASES[name]):
        print("[SKIP] %s 样本不存在: %s" % (name, CASES[name]))
        return False
    return True


PRIVATE = os.environ.get("NG_PRIVATE_REGRESSION") == "1"
if not PRIVATE:
    print("[SKIP] 真实项目回归需要 NG_PRIVATE_REGRESSION=1 且存在 tests/private_regression_cases.json")
elif _manifest_error:
    # 开发者显式要求跑回归而清单损坏：必须报失败，不能静默跳过造成"回归通过"假象
    check("回归样本清单可解析", False, _manifest_error)
elif not CASES:
    print("[SKIP] 未找到 tests/private_regression_cases.json（本机私有回归样本清单，不入库）")
parsed = {}
for name, p in (CASES.items() if PRIVATE else []):
    if not have(name):
        continue
    pairs, nodes, vars_, who, _extra, text_map = ipatch._parse_file(p)
    parsed[name] = (pairs, nodes, vars_, who, text_map)
    print("--- %s: pairs=%d nodes=%d vars=%d who=%d text_map=%d"
          % (name, len(pairs), len(nodes), len(vars_), len(who), len(text_map)))
    for o, n in pairs[:2]:
        print("    pair: %r -> %r" % (o[:60], n[:60]))
    for k in list(nodes)[:2]:
        print("    node: %s -> %r" % (k, nodes[k][:60]))
    if vars_:
        print("    vars:", dict(list(vars_.items())[:5]))
    if who:
        print("    who:", who)
    if text_map:
        for k in list(text_map)[:2]:
            print("    map: %r -> %r" % (k[:60], text_map[k][:60]))

if PRIVATE and "universal" in parsed:
    p, n, v, w, _tm = parsed["universal"]
    check("universal pairs>40", len(p) > 40, "got %d" % len(p))
    check("universal Arthur->[MC]", ("Arthur", "[MC]") in p)
    check("universal no nodes", not n)

if PRIVATE and "another" in parsed:
    p, n, v, w, _tm = parsed["another"]
    check("another nodes>10", len(n) > 10, "got %d" % len(n))
    check("another strings pairs>5", len(p) > 5, "got %d" % len(p))
    check("another who jo=Mom", w.get("jo") == "Mom", repr(w))
    has_jo_day = any("quest_jo_day" in k for k in n)
    check("another node id sample", has_jo_day)

if PRIVATE and "dp" in parsed:
    p, n, v, w, _tm = parsed["dp"]
    check("dp pairs>8", len(p) > 8, "got %d" % len(p))
    check("dp tenant->son", ("tenant", "son") in p)

if PRIVATE and "crossworlds" in parsed:
    p, n, v, w, _tm = parsed["crossworlds"]
    check("cw vars Landlady=Mother", v.get("Landlady") == "Mother", repr(dict(list(v.items())[:4])))
    check("cw roommatef=sister", v.get("roommatef") == "sister")
    check("cw no pairs", len(p) == 0, "got %d" % len(p))

if PRIVATE and "lat" in parsed:
    check("lat pairs>3", len(parsed["lat"][0]) > 3, "got %d" % len(parsed["lat"][0]))

if PRIVATE and "sd" in parsed:
    sd_pairs, sd_nodes, sd_vars, sd_who, sd_extra, _sd_tm = ipatch._parse_file(CASES["sd"])
    print("sd pairs=%d extra=%d" % (len(sd_pairs), len(sd_extra)))
    check("sd input prompts extracted", len(sd_extra) >= 14, "got %d" % len(sd_extra))
    check("sd prompt sample", "(default is Sister)." in sd_extra, repr(sd_extra[:3]))
    check("sd default values", "Sister" in sd_extra and "Mom" in sd_extra)

# --- 字典型（say 猴子补丁）补丁 ---
if PRIVATE and "rtl" in parsed:
    rtl_pairs, rtl_nodes, rtl_vars, rtl_who, rtl_extra, rtl_map = ipatch._parse_file(CASES["rtl"])
    check("rtl 711 条整句映射", len(rtl_map) >= 700, "got %d" % len(rtl_map))
    check("rtl no replace pairs", not rtl_pairs, "got %d" % len(rtl_pairs))
    check("rtl map Grace->mom",
          rtl_map.get("I missed you too, Grace. I promise to visit more often.")
          == "I missed you too, Mom. I promise to visit more often.", "")
    check("rtl 短句不被长句污染",
          rtl_map.get("Chloe?") == "Sis?" and "Is everything okay, Chloe?" in rtl_map
          and rtl_map["Is everything okay, Chloe?"].startswith("Is everything okay"),
          repr(rtl_map.get("Chloe?")))
    check("rtl is_patch_file", ipatch.is_patch_file(CASES["rtl"]))
    check("rtl 普通剧情文件不误判",
          not ipatch.is_patch_file(os.path.join(os.path.dirname(CASES["rtl"]),
                                                "Z_KoGa3Screens.rpy")))

    ov_map = ipatch.Overlay(["patch.rpy"], [], {}, {}, {}, None, rtl_map)
    # 含短键 "Chloe?" 但不是字典条目的句子：精确匹配下必须原样保留
    probe = "Well, Chloe? Anything?"
    check("probe 不是字典条目", probe not in rtl_map)
    check("map 精确匹配生效", ov_map.apply("Chloe?", say=True) == "Sis?",
          repr(ov_map.apply("Chloe?", say=True)))
    check("map 子串不误伤（长句走自己的映射）",
          ov_map.apply("Is everything okay, Chloe?", say=True) == "Is everything okay sis?",
          repr(ov_map.apply("Is everything okay, Chloe?", say=True)))
    check("map 子串不误伤（非条目句子原样）", ov_map.apply(probe, say=True) == probe,
          repr(ov_map.apply(probe, say=True)))
    check("map 不管菜单项（补丁没钩菜单）", ov_map.apply("Chloe?") == "Chloe?")
    check("map 参与指纹",
          ov_map.sig != ipatch.Overlay(["patch.rpy"], [], {}, {}, {}).sig)

# apply 顺序语义：演变文本上的链式替换（纯合成用例，不依赖真实样本）
ov = ipatch.Overlay(["t"], [("a b", "c"), ("c", "d")], {}, {}, {})
check("apply sequential", ov.apply("a b") == "d", repr(ov.apply("a b")))
ov2 = ipatch.Overlay(["t"], [("Diana", "your mom")], {}, {}, {})
check("apply basic", ov2.apply("I trust Diana this time") == "I trust your mom this time")

# ---------- Part 2: 合成管线端到端 ----------
print("=== Part 2: 合成管线端到端 ===")
tmp = tempfile.mkdtemp(prefix="ng_ipatch_test_")
game = os.path.join(tmp, "ipatch_test_game")
gamedir = os.path.join(game, "game")
os.makedirs(os.path.join(gamedir, "tl", "chinese"))

with open(os.path.join(gamedir, "mom_ipatch.rpy"), "w", encoding="utf-8") as f:
    f.write('init 999 python:\n'
            '    def rep(text):\n'
            '        text = text.replace("Diana", "your mom")\n'
            '        text = text.replace("Landlady day", "Mom day")\n'
            '        return text\n'
            '    config.say_menu_text_filter = rep\n')
with open(os.path.join(gamedir, "other.rpy"), "w", encoding="utf-8") as f:
    f.write('label start:\n'
            '    e "I trust Diana this time"\n'
            '    $ quest_title = "Landlady day"\n')
# 名字带 patch 的普通剧情文件：没有台词钩子特征，不能被当补丁排除
with open(os.path.join(gamedir, "storypatch.rpy"), "w", encoding="utf-8") as f:
    f.write('label part2:\n'
            '    e "Just an ordinary line"\n')
with open(os.path.join(gamedir, "tl", "chinese", "test.rpy"), "w", encoding="utf-8") as f:
    f.write('translate chinese labels_start_abc123:\n'
            '    # e "I trust Diana this time"\n'
            '    e "I trust Diana this time"\n'
            '\n'
            'translate chinese strings:\n'
            '\n'
            '    old "Landlady day"\n'
            '    new "Landlady day"\n')

# ipatch 模块的 work_dir 指到 tmp
ipatch.work_dir = lambda gb: os.path.join(tmp, "work")

dump = {"labels_start_abc123": {"filename": "other.rpy", "lineno": 2,
                                "nodes": [{"type": "say", "who": "e",
                                           "what": "I trust Diana this time"}]}}

jobs, tl_files = tlgen.build_jobs(game, "chinese", dump, include_strings=True, context_lines=1)
check("jobs built", len(jobs) == 2, "got %d" % len(jobs))

ov = ipatch.build_overlay(game, log=print)
check("overlay built", ov is not None)
if ov:
    cnt = ipatch.overlay_jobs(jobs, ov)
    check("overlay patched 2 jobs", cnt == 2, "got %d" % cnt)
    say = next(j for j in jobs if j["kind"] == "say")
    st = next(j for j in jobs if j["kind"] == "string")
    check("say old=patched", say["old"] == "I trust your mom this time", repr(say["old"]))
    check("say orig kept", say.get("orig") == "I trust Diana this time", repr(say.get("orig")))
    check("string old=patched", st["old"] == "Mom day", repr(st["old"]))
    check("string orig kept", st.get("orig") == "Landlady day", repr(st.get("orig")))

    # 模拟翻译缓存：译文 = "译(" + 翻译源 + ")"
    trans = {j["key"]: "译(%s)" % j["old"] for j in jobs}
    text_map, key_map = pl._expand_translations(jobs, trans)
    check("text_map keyed by orig",
          text_map.get("I trust Diana this time") == "译(I trust your mom this time)",
          repr(text_map))
    check("string map by orig", text_map.get("Landlady day") == "译(Mom day)")
    check("key map 逐条可查", key_map.get("labels_start_abc123") == "译(I trust your mom this time)"
          and key_map.get("S:Landlady day") == "译(Mom day)", repr(key_map))

    n = tlgen.fill_translations(tl_files, text_map)
    check("fill replaced 2", n == 2, "got %d" % n)
    with open(tl_files[0], encoding="utf-8") as f:
        out = f.read()
    check("say line = patched translation", 'e "译(I trust your mom this time)"' in out)
    check("strings old unchanged", 'old "Landlady day"' in out)
    check("strings new = patched translation", 'new "译(Mom day)"' in out)

    # 缓存失效：首次（无 sig）应丢弃打过补丁的 key
    trans_path = os.path.join(tmp, "work", "translations.json")
    os.makedirs(os.path.dirname(trans_path), exist_ok=True)
    json.dump(trans, open(trans_path, "w", encoding="utf-8"), ensure_ascii=False)
    dropped = ipatch.drop_stale_cache(jobs, ov, trans_path, log=print)
    left = json.load(open(trans_path, encoding="utf-8"))
    check("stale dropped", dropped and len(left) == 0, "left=%d" % len(left))
    # 第二次：sig 已存，不再丢弃
    json.dump(trans, open(trans_path, "w", encoding="utf-8"), ensure_ascii=False)
    dropped2 = ipatch.drop_stale_cache(jobs, ov, trans_path, log=print)
    left2 = json.load(open(trans_path, encoding="utf-8"))
    check("second run keeps cache", not dropped2 and len(left2) == 2, "left=%d" % len(left2))

# pystrings 扫描必须跳过补丁文件，但名字带 patch 的普通脚本要照常扫描
check("pystrings skips patch, keeps storypatch",
      sorted(os.path.basename(x) for x in pystrings._iter_source_files(gamedir))
      == ["other.rpy", "storypatch.rpy"],
      repr(sorted(os.path.basename(x) for x in pystrings._iter_source_files(gamedir))))
check("storypatch 不误判为补丁",
      not ipatch.is_patch_file(os.path.join(gamedir, "storypatch.rpy")))

# write_skeleton 接收补丁自有文本（输入提示词）并并入骨架；运行时文件含 input 包装
added = pystrings.write_skeleton(game, "chinese", extra=["(default is Sister).", "(default is Sister)."])
skel = open(os.path.join(gamedir, "tl", "chinese", "zz_ng_pystrings.rpy"), encoding="utf-8").read()
check("extra merged once", added >= 1 and skel.count('old "(default is Sister)."') == 1)
rt = open(os.path.join(gamedir, "zz_ng_dyntrans.rpy"), encoding="utf-8").read()
check("runtime input wrappers", "_ng_input_prompt" in rt and "_ng_input" in rt)

# apply_dump 副本
d2 = ov.apply_dump(dump)
check("dump patched copy",
      d2["labels_start_abc123"]["nodes"][0]["what"] == "I trust your mom this time"
      and dump["labels_start_abc123"]["nodes"][0]["what"] == "I trust Diana this time")

# ---------- 跳过名单：人工排除按文本匹配误伤其它说话人的条目 ----------
os.makedirs(os.path.join(tmp, "work"), exist_ok=True)
json.dump(["labels_start_abc123"], open(os.path.join(tmp, "work", "ipatch_skip.json"),
                                        "w", encoding="utf-8"), ensure_ascii=False)
ov3 = ipatch.build_overlay(game, log=print)
check("skip overlay built", ov3 is not None and ov3.skip_keys == {"labels_start_abc123"})
check("skip 不影响整体指纹（避免全量重译）", ov3.sig == ov.sig)
jobs3, _tf3 = tlgen.build_jobs(game, "chinese", dump, include_strings=True, context_lines=1)
ipatch.overlay_jobs(jobs3, ov3)
say3 = next(j for j in jobs3 if j["kind"] == "say")
st3 = next(j for j in jobs3 if j["kind"] == "string")
check("跳过名单内的台词不被补丁改写", not say3.get("orig") and say3["old"] == "I trust Diana this time",
      repr(say3))
check("跳过名单不影响其它条目", st3.get("orig") == "Landlady day" and st3["old"] == "Mom day",
      repr(st3))
d3 = ov3.apply_dump(dump)
check("跳过名单内取样文本也不改写",
      d3["labels_start_abc123"]["nodes"][0]["what"] == "I trust Diana this time")
os.remove(os.path.join(tmp, "work", "ipatch_skip.json"))

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(os.path.join(ROOT, "work", "ipatch_test_game"), ignore_errors=True)

print()
if failures:
    print("FAILED: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("ALL TESTS PASSED")


def test_all():
    """pytest 收集入口：上面的模块级脚本执行完毕后在此汇总结果。"""
    assert not failures, failures
