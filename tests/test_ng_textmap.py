# -*- coding: utf-8 -*-
"""zz_ng_text.rpy 显示层文本映射测试：关系词回显 / 复数与所有格 / 玩家自填关系词。

要覆盖的关键事实（都是 The Home of Pleasure 那类"开场让玩家自填关系词"的游戏
真机上踩出来的）：
  - 关系词是运行期插值出来的（[relation2] → brother），译文管不到，只能在显示层替换；
  - 词边界不能用 \\b：汉字在 Python 正则里也算 \\w，"你是我的brother" 里
    brother 前后都不是 \\b，整句英文词会原样漏出去（玩家看到"你是我们brother"）；
  - 台词里大量 "[relation1]s" / "[relation1]'s"，复数要出"姐姐们"、所有格要出"姐姐的"；
  - 同一个英文词的长幼由玩家的选择决定：本游戏关系词表（工具里配置）与
    玩家在游戏里直接输入的中文都要能定住它。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import fontpatch  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, name, detail))
    if not cond:
        failures.append(name)


def build(names=None, words=None, lang="chinese", show=None, guess=None, stale=False):
    """按 apply_text_helpers 的方式展开模板，执行 init 999 块，返回运行期命名空间。

    只做纯逻辑验证：renpy/persistent/config 全用桩，不碰真游戏。
    stale=True 模拟"玩家记忆是旧关系词表下写下的"（改过表之后旧记忆必须作废）。
    """
    content = (fontpatch.TEXT_HELPERS_TEMPLATE
               .replace("__NG_LANG__", lang)
               .replace("__NG_NAMES__", repr(names or {}))
               .replace("__NG_SHOW__", repr(fontpatch.COMMON_RELATION_WORDS))
               .replace("__NG_WORDS__", repr(words_dict(words))))
    block, inside = [], False
    for ln in content.split("\n"):
        if ln.startswith("init 999 python:"):
            inside = True
            continue
        if inside:
            if ln and not ln.startswith(" "):
                break
            if ln.strip():
                block.append(ln[4:] if ln.startswith("    ") else ln)
    code = compile("\n".join(block), "zz_ng_text.rpy", "exec")

    class NS(object):
        pass

    def run(prime=None):
        box = {"v": ""}
        renpy = NS()
        renpy.input = lambda *a, **k: box["v"]
        renpy.game = NS()
        renpy.game.preferences = NS()
        renpy.game.preferences.language = lang
        persistent = NS()
        if prime:
            prime(persistent)
        ns = {"renpy": renpy, "persistent": persistent, "config": NS()}
        sys.modules.pop("_ng_text_runtime", None)  # 运行期状态在模块里，每个用例从零开始
        exec(code, ns)
        ns["renpy"] = renpy
        ns["persistent"] = persistent
        ns["box"] = box
        return ns

    sig = run()["_NG_WORDS_SIG"]   # 先跑一次拿到当前关系词表的指纹（相当于上次启动留下的）

    def prime(p):
        p.ng_words_sig = "[]" if stale else sig
        if show is not None:
            p.ng_input_show = dict(show)
        if guess is not None:
            p.ng_input_guess = dict(guess)

    return run(prime)


def words_dict(words):
    return fontpatch.relation_words_map(words)


def typ(ns, value, prompt=""):
    """模拟玩家在 renpy.input 里敲了 value，返回游戏逻辑实际拿到的值。"""
    ns["box"]["v"] = value
    return ns["renpy"].input(prompt)


# ---------- Part 1: 关系词表解析 ----------
print("=== Part 1: relation_words_map 只收英文词条 ===")
check("列表格式（工具存的格式）",
      words_dict([{"en": "brother", "cn": "弟弟"}]) == {"brother": "弟弟"})
check("字典格式也认", words_dict({"Sister": "姐姐"}) == {"sister": "姐姐"})
check("中文原文的词条丢弃（没有可命中的英文原文）",
      words_dict([{"en": "弟弟", "cn": "弟弟"}]) == {})
check("空译文丢弃", words_dict([{"en": "brother", "cn": "  "}]) == {})
check("英文带空格/连字符的词条保留",
      words_dict([{"en": "step father", "cn": "继父"}]) == {"step father": "继父"})
check("首条同名生效", words_dict([{"en": "brother", "cn": "弟弟"},
                                 {"en": "brother", "cn": "哥哥"}]) == {"brother": "弟弟"})

# ---------- Part 2: 紧贴中文的关系词（本次事故的根因） ----------
print("=== Part 2: 汉字紧贴英文词也要命中 ===")
ns = build()
f = ns["_ng_replace_text"]
check("你是我的brother。 -> 弟弟（默认表：哥哥；这里验证能命中）",
      f(u"你是我的brother。") == u"你是我的哥哥。", repr(f(u"你是我的brother。")))
check("行首/行尾中文夹击", f(u"我的mother做饭") == u"我的妈妈做饭")
check("标点分隔照旧", f(u"my mother") == u"my 妈妈")
check("句中英文不误伤", f(u"Say brotherhood!") == u"Say brotherhood!")
check("全角标点", f(u"（brother）") == u"（哥哥）")
ns_en = build()
ns_en["renpy"].game.preferences.language = "None"
check("非中文语言不动文本", ns_en["_ng_replace_text"](u"我的brother") == u"我的brother")

# ---------- Part 3: 复数与所有格 ----------
print("=== Part 3: [relation1]s / [relation1]'s ===")
check("复数加们：我的sisters", f(u"我的sisters") == u"我的姐姐们")
check("复数加们：你的brothers", f(u"你的brothers") == u"你的哥哥们")
check("所有格加的：sister's room", f(u"sister's room") == u"姐姐的 room")
check("复数所有格 sisters'", f(u"sisters' room") == u"姐姐们的 room")
check("已有们不重复加", f(u"我的sisters们") == u"我的姐姐们们")
check("单词复数 friends", f(u"friends") == u"朋友们")
check("名字所有格 Sophie's", build(names={"Sophie": u"苏菲"})["_ng_replace_text"](u"Sophie's room")
      == u"苏菲的 room")
check("已带 's 的短语不被拆（your mom's）",
      f(u"I'm at your mom's") == u"I'm at 你妈的")

# ---------- Part 4: 本游戏关系词表决定长幼 ----------
print("=== Part 4: 关系词表（brother → 弟弟） ===")
ns2 = build(words=[{"en": "brother", "cn": "弟弟"}, {"en": "sister", "cn": "姐姐"},
                   {"en": "mentor", "cn": "教练"}])
g = ns2["_ng_replace_text"]
check("你是我们brother。 -> 你是我们弟弟。", g(u"你是我们brother。") == u"你是我们弟弟。")
check("复数仍出弟弟们", g(u"我的brothers") == u"我的弟弟们")
check("表里没有的词走默认（mother → 妈妈）", g(u"我的mother") == u"我的妈妈")
check("表能补默认没有的词（mentor → 教练）", g(u"她是我的mentor") == g(u"她是我的教练"))

# ---------- Part 5: 玩家在游戏里自己输入 ----------
print("=== Part 5: 玩家自填关系词 ===")
ns3 = build(words=[{"en": "brother", "cn": "弟弟"}])
h = ns3["_ng_replace_text"]
boxed = ns3["_ng_input"]
check("输入中文弟弟：游戏逻辑拿到 brother", typ(ns3, u"弟弟", u"那你是她的……") == "brother")
check("输入中文弟弟：显示就用玩家的词（盖过关系词表）", h(u"你是我们brother。") == u"你是我们弟弟。")
check("输入中文妹妹：显示改用妹妹", typ(ns3, u"妹妹", u"你是她的……") == "sister"
      and h(u"你的sister") == u"你的妹妹")
check("输入中文后所有格也对", h(u"你sister的") == u"你妹妹的")
check("表里没有的中文原样交给游戏", typ(ns3, u"老妹", u"她是你的……") == u"老妹")
check("玩家名不受影响", typ(ns3, u"小明", u"你叫什么名字？") == u"小明")

ns4 = build(words=[{"en": "brother", "cn": "弟弟"}])
check("输入英文 brother：语境没线索时仍按关系词表（弟弟）",
      typ(ns4, "brother", u"That makes you her...") == "brother"
      and ns4["_ng_replace_text"](u"你是我们brother。") == u"你是我们弟弟。")
ns5 = build()
check("输入英文 younger sister：按提问语境出妹妹",
      typ(ns5, "sister", u"...your younger sister?(The default is Sister.)") == "sister"
      and ns5["_ng_replace_text"](u"你的sister") == u"你的妹妹")
check("语境推断不盖关系词表",
      build(words=[{"en": "sister", "cn": "姐姐"}],
            guess={"sister": u"妹妹"})["_ng_replace_text"](u"你的sister") == u"你的姐姐")

# ---------- Part 6: 旧存档迁移 ----------
print("=== Part 6: 旧 persistent 迁移（改过关系词表后，旧记忆不能盖住新设定）===")
# 真实场景：先按"主角是弟弟"配表并玩过，存档留下输入记忆；之后从台词发现主角其实
# 25 岁、姐妹 22 岁，把表改成 brother→哥哥、sister→妹妹——旧记忆必须整体作废。
ns6 = build(words=[{"en": "brother", "cn": u"哥哥"}, {"en": "sister", "cn": u"妹妹"}],
            show={"sister": u"姐姐", "brother": u"弟弟", "landlady": u"房东太太"},
            guess={"sister": u"姐姐"},
            stale=True)
check("改了关系词表：旧记忆作废（sister=姐姐 → 妹妹）",
      ns6["_ng_replace_text"](u"你的sister") == u"你的妹妹")
check("改了关系词表：旧记忆作废（brother=弟弟 → 哥哥）",
      ns6["_ng_replace_text"](u"你是我们brother。") == u"你是我们哥哥。")
check("新指纹已存下（下次启动不再清）",
      ns6["persistent"].ng_words_sig == ns6["_NG_WORDS_SIG"])

ns6b = build(words=[{"en": "sister", "cn": u"妹妹"}], show={"sister": u"姐姐", "brother": u"老弟"})
check("表没变：玩家自己输入过的中文保留（老弟）",
      ns6b["_ng_replace_text"](u"你是我的brother") == u"你是我的老弟")
check("表没变：等于默认值的回声条目清掉，关系词表生效（sister → 妹妹）",
      ns6b["_ng_replace_text"](u"你是我的sister") == u"你是我的妹妹")


# ---------- Part 7: 存档/回滚不能把旧映射带回来 ----------
print("=== Part 7: 读旧档不能盖掉当前关系词表 ===")
# 真机事故：旧档按旧表（sister→姐姐）玩过，存档里存下了运行期映射表；之后把表改成
# sister→妹妹 并重新生成 zz_ng_text.rpy，读旧档时 store 里的旧映射被还原回来，
# 盖住 init 按新表算好的结果——玩家看到"你把手机拿给姐姐们看。"
# 所以映射表必须放在 store 之外（模块里），存档/回滚带不走它。
ns8 = build(words=[{"en": "sister", "cn": u"妹妹"}])
stale = ns8["_ng_replace_text"]
check("改表后新档正常：sister → 妹妹",
      stale(u"你把手机拿给sister们看。") == u"你把手机拿给妹妹们看。")
# 模拟 Ren'Py 读档：把旧存档里的运行期变量原样塞回 store
import re as _re  # noqa: E402
ns8["_NG_MAP"] = {"sister": u"姐姐", "brother": u"哥哥"}
ns8["_NG_RE"] = _re.compile(u"(?i)(?<![A-Za-z0-9_])(sister|brother)(?![A-Za-z0-9_])")
check("旧档回灌 _NG_MAP 后，显示仍按当前关系词表（妹妹）",
      stale(u"你把手机拿给sister们看。") == u"你把手机拿给妹妹们看。")
check("旧档回灌 _NG_RE 后，显示仍按当前关系词表",
      stale(u"你是我的sister") == u"你是我的妹妹")
check("旧档回灌后玩家当场输入的中文照样优先",
      (typ(ns8, u"姐姐", u"她是你的……") == "sister"
       and stale(u"你把手机拿给sister们看。") == u"你把手机拿给姐姐们看。"))
ns8b = build(words=[{"en": "sister", "cn": u"妹妹"}])
check("运行期映射表存在模块里、不是 store 变量（存档带不走它）",
      "_NG_MAP" not in ns8b and "_NG_RE" not in ns8b
      and ns8b["_ng_rt"]().map.get("sister") == u"妹妹")

# ---------- Part 8: 同一个英文词被两处提问答得不一样 ----------
print("=== Part 8: 同词多处提问（relation1 与 relation3 都是 sister） ===")
# 真机事故：开局两个提问的英文都是 sister——"苏菲是你的…"（玩家答妹妹，要兄妹）
# 和"格蕾丝是苏菲的…"（玩家答姐姐，剧情上确实是姐姐）。旧版按词记最后一个输入，
# 于是"你把手机拿给[relation3]们看"也成了"姐姐们"。回答矛盾时改回转关系词表。
ns9 = build(words=[{"en": "sister", "cn": u"妹妹"}, {"en": "brother", "cn": u"哥哥"}])
t9 = ns9["_ng_replace_text"]
typ(ns9, u"妹妹", u"苏菲是你的……（默认是朋友）")
check("先答妹妹：显示妹妹", t9(u"你把手机拿给sister们看。") == u"你把手机拿给妹妹们看。")
typ(ns9, u"姐姐", u"格蕾丝是苏菲的……（默认是室友）")
check("再答姐姐（同一个词）：两处回答矛盾 -> 退回关系词表（妹妹）",
      t9(u"你把手机拿给sister们看。") == u"你把手机拿给妹妹们看。")
check("矛盾时不再通吃（不会变成姐姐们）",
      t9(u"你的sister") == u"你的妹妹")
check("矛盾只影响这个词：另一个词照旧听玩家的",
      (typ(ns9, u"弟弟", u"那你是她的……") == "brother"
       and t9(u"你是我们brother") == u"你是我们弟弟"))

ns9b = build(words=[{"en": "sister", "cn": u"妹妹"}])
t9b = ns9b["_ng_replace_text"]
typ(ns9b, u"姐姐", u"苏菲是你的……")
typ(ns9b, u"姐姐", u"格蕾丝是苏菲的……")
check("两处回答一致时仍听玩家的（姐姐）", t9b(u"你把手机拿给sister们看。") == u"你把手机拿给姐姐们看。")

ns9c = build(words=[{"en": "sister", "cn": u"妹妹"}],
             show={"sister": u"姐姐"})   # 老格式（英文词 -> 中文）的旧记忆
check("老格式记忆里等于内置默认值的回声丢掉（姐姐 -> 关系词表妹妹）",
      ns9c["_ng_replace_text"](u"你的sister") == u"你的妹妹")
ns9d = build(words=[{"en": "sister", "cn": u"妹妹"}], show={"sister": u"老妹"})
check("老格式记忆里带信息的选择照原样迁移（老妹）",
      ns9d["_ng_replace_text"](u"你的sister") == u"你的老妹")

# ---------- Part 9: 生成端 ----------
print("=== Part 9: apply_text_helpers 生成的文件 ===")
tmp = tempfile.mkdtemp(prefix="ng_textmap_")
try:
    game = os.path.join(tmp, "game")
    os.makedirs(game)
    with open(os.path.join(game, "script.rpy"), "w", encoding="utf-8") as fh:
        fh.write(u'define s = Character("Sophie")\ndefine g = Character("Grace")\n'
                 u'label start:\n    s "hi"\n')
    fontpatch.apply_text_helpers(tmp, "chinese",
                                 [{"name": "Sophie", "name_cn": u"苏菲"}],
                                 log=lambda *a: None,
                                 words=[{"en": "brother", "cn": u"弟弟"}])
    rpy = os.path.join(game, "zz_ng_text.rpy")
    src = open(rpy, encoding="utf-8").read()
    check("文件已生成", os.path.isfile(rpy))
    check("关系词表写进文件", "'brother': '\\u5f1f\\u5f1f'" in repr(src)
          or u"'brother': '弟弟'" in src)
    check("人名映射写进文件", u"'Sophie': '苏菲'" in src)
    check("不留旧 .rpyc", not os.path.isfile(rpy + "c"))
    check("占位符全部替换", "__NG_" not in src)
    ns7 = build(names={"Sophie": u"苏菲"}, words=[{"en": "brother", "cn": u"弟弟"}])
    check("生成内容与模板行为一致",
          ns7["_ng_replace_text"](u"你是我们brother。") == u"你是我们弟弟。")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print("FAILED: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("ALL TESTS PASSED")


def test_all():
    """pytest 收集入口：上面的模块级脚本执行完毕后在此汇总结果。"""
    assert not failures, failures
