# -*- coding: utf-8 -*-
"""texttags 模块测试：Ren'Py 文本标签的笔误修复 + 运行时崩溃写法识别。

要覆盖的关键事实（都是 2026-09-15 那次 `Unknown text tag 'image:chloe_selfie_full_6'`
崩溃里踩出来的）：
  - 引擎只认 {tag} / {tag=value}；{tag:value} 会在 segment() 里抛 Unknown text tag，
    而引擎自带的 lint 会把 ':' 当参数分隔符剥掉、看不出问题；
  - {axis:x=1} 与 {#rrggbb} 是引擎确实用冒号的两种写法，不能误修；
  - {a=image:xxx.png} 里的冒号在 "=" 之后，属超链接目标，不能动。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import texttags  # noqa: E402
from core.translator import fix_rpy_tags  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, name, detail))
    if not cond:
        failures.append(name)


# ---------- Part 1: 崩溃写法识别 ----------
print("=== Part 1: problems() 识别运行时会崩的写法 ===")
check("冒号传参被识别（真实事故那条）",
      texttags.problems("{a=image:chloe_selfie_6.png}{image:chloe_selfie_full_6}{/a}")
      == ["用 ':' 传参（引擎只认 '='，会抛 Unknown text tag）：{image:chloe_selfie_full_6}"],
      repr(texttags.problems("{a=image:chloe_selfie_6.png}{image:chloe_selfie_full_6}{/a}")))
check("正确写法无问题",
      texttags.problems("{a=image:chloe_selfie_6.png}{image=chloe_selfie_full_6}{/a}") == [])
check("未知标签被识别", texttags.problems("你好{foo}") == ["未知标签：{foo}"])
check("大小写不匹配算未知标签（引擎区分大小写）", texttags.problems("{B}粗{/B}") != [])
check("常见标签无问题",
      texttags.problems("{i}斜{/i}{b}粗{/b}{color=#ff0000}红{/color}{w}") == [])
check("{#rrggbb} 不算问题", texttags.problems("{#ff0000}红") == [])
check("{axis:x=1} 不算问题", texttags.problems("{axis:x=1}文字") == [])
check("{axis=x=1} 是错的（轴标签必须冒号）", texttags.problems("{axis=x=1}文字") != [])
check("闭标签不误报", texttags.problems("{/i}") == [])
check("{=style} 空标签名不误报", texttags.problems("{=my_style}文字") == [])
check("空文本安全", texttags.problems("") == [])


# ---------- Part 2: 笔误修复 ----------
print("=== Part 2: fix() 只修必须用 '=' 的标签 ===")
check("image 冒号被修好",
      texttags.fix("{image:chloe_selfie_full_6}") == "{image=chloe_selfie_full_6}")
check("a=image: 的超链接目标不动",
      texttags.fix("{a=image:chloe_selfie_6.png}x{/a}") == "{a=image:chloe_selfie_6.png}x{/a}")
check("color/size/font 冒号被修好",
      texttags.fix("{color:#fff}A{size:20}B{font:foo.ttf}C")
      == "{color=#fff}A{size=20}B{font=foo.ttf}C")
check("{axis:x=1} 不被改写", texttags.fix("{axis:x=1}") == "{axis:x=1}")
check("未知标签不猜、留给 problems()",
      texttags.fix("{foo:bar}") == "{foo:bar}" and texttags.problems("{foo:bar}") != [])
check("闭标签不被改写", texttags.fix("{/image:x}") == "{/image:x}")
check("{} 占位符（管线用的格式槽）不动",
      texttags.fix("血量 {} 点") == "血量 {} 点")
check("修复后不再有崩溃写法",
      texttags.problems(texttags.fix("{image:x}{color:red}")) == [])


# ---------- Part 3: 接入 fix_rpy_tags ----------
print("=== Part 3: fix_rpy_tags 已覆盖冒号笔误 ===")
check("fix_rpy_tags 修冒号传参",
      fix_rpy_tags('{a=image:chloe_selfie_6.png}{image:chloe_selfie_full_6}{/a}')
      == '{a=image:chloe_selfie_6.png}{image=chloe_selfie_full_6}{/a}')
check("fix_rpy_tags 原有能力不回归（带空格标签归一 + 闭标签配平）",
      fix_rpy_tags("{ i }强调{ /i }") == "{i}强调{/i}")
check("fix_rpy_tags 对普通文本零改动",
      fix_rpy_tags("你好，克洛伊。") == "你好，克洛伊。")
check("fix_rpy_tags 幂等",
      fix_rpy_tags(fix_rpy_tags("{image:x}")) == fix_rpy_tags("{image:x}"))


# ---------- Part 4: 标签/变量差异（译文复核用） ----------
print("=== Part 4: tag_diff 找出漏抄的标签与 [变量] ===")
check("漏抄 {image=...} 被报出来",
      texttags.tag_diff("看{a=image:x.png}{image=big}{/a}", "看{a=image:x.png}{/a}")[0]
      == ["{image=big}"])
check("漏抄 [mname] 被报出来",
      texttags.tag_diff("你好[mname]", "你好")[0] == ["[mname]"])
check("多出来的标签被报出来",
      texttags.tag_diff("普通", "{i}强调{/i}")[1] == ["{i}", "{/i}"])
check("顺序不同不算差异",
      texttags.tag_diff("{i}A{/i}和{b}B{/b}", "{b}B{/b}和{i}A{/i}") == ([], []))
check("带空格写法不算差异",
      texttags.tag_diff("{ i }强调{ /i }", "{i}强调{/i}") == ([], []))
check("一致时无差异", texttags.tag_diff("你好[i]世界", "你好[i]世界") == ([], []))

# ---------- Part 5: 变量名被改写（运行时 NameError） ----------
print("=== Part 5: fix_interps 按原文还原被改写的 [变量] ===")
check("大小写+下划线被还原（真实事故那条）",
      texttags.fix_interps("[PlayerName], we should ditch the old ball and chain.",
                           "[player_name]，咱们该甩开那个拖油瓶。")
      == ("[PlayerName]，咱们该甩开那个拖油瓶。", []))
check("多个变量一起还原",
      texttags.fix_interps("[PlayerName] 和 [b_relation1]", "[player_name] 和 [B_Relation1]")
      == ("[PlayerName] 和 [b_relation1]", []))
check("原文写法原样保留、不误改",
      texttags.fix_interps("你好[PlayerName]", "你好[PlayerName]") == ("你好[PlayerName]", []))
check("原文没有的变量不猜（模型换成了别的名字）",
      texttags.fix_interps("Wait for [mc_name] to do it.", "等[player_name]来动手。")
      == ("等[player_name]来动手。", ["player_name"]))
check("原文没有的变量不猜（模型把变量整个翻译了）",
      texttags.fix_interps("your little [b_relation1]", "你的小[妹妹]")
      == ("你的小[妹妹]", ["妹妹"]))
check("变量被整句抹掉不算新增（缺失由 tag_diff 报）",
      texttags.fix_interps("你好[mname]", "你好") == ("你好", []))
check("转换后缀保留（[var!t] / [var:>8]）",
      texttags.fix_interps("Hello [PlayerName]", "你好 [player_name!t]")
      == ("你好 [PlayerName!t]", []))
check("[[] 转义不算变量",
      texttags.fix_interps("[[PlayerName]", "[[player_name]") == ("[[player_name]", []))
check("带参数的变量名比对取冒号/叹号之前的部分",
      texttags.fix_interps("血量 [hp:.1f]", "血量 [HP:.1f]") == ("血量 [hp:.1f]", []))
check("还原后 tag_diff 不再报差异",
      texttags.tag_diff("[PlayerName] 你好", texttags.fix_interps("[PlayerName] 你好",
                                                                   "[player_name] 你好")[0])
      == ([], []))
check("没动的译文零改动",
      texttags.fix_interps("普通一句话。", "普通一句话。") == ("普通一句话。", []))

print()
if failures:
    print("FAILED: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("ALL TESTS PASSED")


def test_all():
    """pytest 收集入口：上面的模块级脚本执行完毕后在此汇总结果。"""
    assert not failures, failures
