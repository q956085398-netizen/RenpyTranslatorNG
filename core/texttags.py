# -*- coding: utf-8 -*-
"""Ren'Py 文本标签 / [变量] 插值的静态校验与笔误修复。

引擎渲染对 {…} 只认 {tag} 与 {tag=value} 两种写法——
renpy/text/text.py 的 Layout.segment 里是 `tag, _, value = text.partition("=")`，
写成 {image:x} 时 tag 会变成整串 "image:x"，落到最后的 else 分支抛
"Unknown text tag ..."（整段对白直接崩）。

而引擎自带的 lint（renpy.text.extras.check_text_tags）会把 ':' 也当参数分隔符
剥掉，所以 {image:x} 能通过 lint、只在运行时炸。本模块补上这个盲点：

    fix(s)        把能确定的笔误修好：{image:x} -> {image=x}
    problems(s)   修不掉、运行时会崩的写法（空列表 = 安全）
    fix_interps(a,b)  译文里被改写的 [变量] 按原文还原（大小写/下划线），还原不了的原样报出
    tag_diff(a,b) 译文相对原文缺失/新增的标签与 [变量]，供人工复核

引擎确实用冒号的两处例外：`axis:<轴>`（{axis:x=1}，轴名在冒号后）与 {#rrggbb}。
真实事故：{image:chloe_selfie_full_6} 让 NVL 对白抛 Unknown text tag（2026-09-15）；
[player_name] 让整段对白抛 NameError（2026-09-17，模型把原文的 [PlayerName] 改了大小写）。
"""
import re
from collections import Counter

# 引擎 segment() 的 tag 分支（含 get_displayables 处理的 image）；
# 与 renpy/text/extras.py::text_tags 的差别是补上了 vert/horiz（引擎有分支、lint 表里没有）
KNOWN_TAGS = frozenset("""
    a alpha alt art b color cps done fast font horiz i image instance k noalt nw
    outlinecolor p plain rb rt s shader size space u vert vspace w
    """.split())

# 必须用 "=" 传参的标签——写成冒号一定是笔误，可安全改写（axis 例外，引擎就要冒号）
ARG_TAGS = frozenset("""
    a alpha color cps font image instance k outlinecolor shader size space u vspace
    """.split())

_COLON = re.compile(r"\{\s*(/?)\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*([^{}]*)\}")
_INTERP = re.compile(r"(?<!\[)\[([^\[\]]+)\]")


def _iter_tags(s):
    """按引擎的扫描方式逐个取出 {...} 的内容（'{{' 是字面量大括号，跳过）。"""
    i = 0
    while True:
        i = s.find("{", i)
        if i == -1:
            return
        if s.startswith("{{", i):
            i += 2
            continue
        j = s.find("}", i + 1)
        if j == -1:
            return
        yield s[i + 1:j]
        i = j + 1


def problems(s):
    """返回会在运行时抛异常的标签写法（空列表表示安全）。"""
    out = []
    for raw in _iter_tags(s):
        body = raw[1:] if raw.startswith("/") else raw
        if body.startswith("#"):            # {#rrggbb} 颜色简写
            continue
        name = body.split("=", 1)[0]
        if ":" in name:
            if name.startswith("axis:") and "=" in body:
                continue                    # {axis:x=1}：引擎的轴标签就是冒号形式
            out.append("用 ':' 传参（引擎只认 '='，会抛 Unknown text tag）：{%s}" % raw)
            continue
        if name and name not in KNOWN_TAGS:
            out.append("未知标签：{%s}" % raw)
    return out


def fix(s):
    """把 {tag:value} 改写成 {tag=value}。

    只改 ARG_TAGS 里的标签，且不动闭标签——未知标签留在原处交给 problems() 报出来，
    不猜。{axis:x=1} 与 {#rrggbb} 不在改写范围。
    """
    if not s or "{" not in s:
        return s

    def rep(m):
        slash, name, val = m.group(1), m.group(2), m.group(3)
        if slash or name not in ARG_TAGS:
            return m.group(0)
        return "{%s=%s}" % (name, val.strip())

    return _COLON.sub(rep, s)


def tags(s):
    """标签多重集（含闭标签；参数不同的同名标签算不同条目）。

    比较前先抹掉标签内空白（{ i } == {i}），避免把格式差异当成标签差异。
    """
    out = Counter()
    for raw in _iter_tags(s or ""):
        if raw.startswith("#"):
            continue
        out["{%s}" % re.sub(r"\s+", "", raw)] += 1
    return out


def interps(s):
    """[变量] 多重集（引擎里 '[' 的转义是 '[['）。"""
    return Counter(_INTERP.findall(s or ""))


_INTERP_SPLIT = re.compile(r"^([^!:]*)(.*)$", re.S)


def _split_interp(name):
    """[var!t] / [var:.2f] 拆成 (变量名, 后缀)；后缀是转换符，改写时原样保留。"""
    m = _INTERP_SPLIT.match(name or "")
    return (m.group(1).strip(), m.group(2)) if m else (name or "", "")


def _interp_key(name):
    """比对用的归一化键：小写 + 去下划线（PlayerName == player_name == PLAYER_NAME）。"""
    return _split_interp(name)[0].lower().replace("_", "")


def fix_interps(old, new):
    """把译文里被改写的 [变量] 还原成原文的写法，返回 (修好的译文, 改不掉的变量)。

    Ren'Py 的 [name] 插值按 Python 标识符从 store 取值（renpy/substitutions.py：
    SIMPLE_NAME 不匹配或不在 scope 里就走 py_eval），大小写与下划线都算数——原文
    [PlayerName] 被译成 [player_name] 就是运行时 NameError，整段对白直接崩
    （2026-09-17 的事故：The Home of Pleasure 全游戏 198 处，走到哪句崩哪句）。
    模型偶尔会把变量名"规范化"（大小写、加下划线，甚至换成提示词里见过的名字），
    这里按原文逐字符还原：只改名字，[var!t] 的转换后缀保留。

    归一化后也对不上的不猜，原样返回在第二个值里——译文新增原文没有的 [变量]
    和未知 {tag} 一样是运行时必崩的写法，由调用方决定拒用还是人工处理。
    """
    old_tokens = list(interps(old).elements())
    src = {}
    for tok in old_tokens:
        src.setdefault(_interp_key(tok), set()).add(tok)
    fixed = new or ""
    unresolved = []
    for tok in sorted(set(interps(new).elements())):
        if tok in old_tokens:
            continue                        # 原文就是这个写法，保留
        cands = src.get(_interp_key(tok)) or set()
        if len(cands) == 1:
            want = _split_interp(cands.pop())[0] + _split_interp(tok)[1]
            fixed = fixed.replace("[%s]" % tok, "[%s]" % want)
        else:
            unresolved.append(tok)          # 原文没有这个变量：不猜
    return fixed, unresolved


def tag_diff(old, new):
    """比较原文与译文的标签/[变量]：返回 (缺失, 新增)，空元组为空表示一致。

    译文重排是允许的，所以只比较多重集；顺序差异不报。
    """
    missing, added = [], []
    for name, cnt in (tags(old) - tags(new)).items():
        missing.extend([name] * cnt)
    for name, cnt in (tags(new) - tags(old)).items():
        added.extend([name] * cnt)
    for name, cnt in (interps(old) - interps(new)).items():
        missing.extend(["[%s]" % name] * cnt)
    for name, cnt in (interps(new) - interps(old)).items():
        added.extend(["[%s]" % name] * cnt)
    return missing, added
