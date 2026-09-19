# -*- coding: utf-8 -*-
"""Python 字面量补提取。

Ren'Py 官方提取只能看到对白/菜单语句和 _() 包裹的字面量；数据驱动型游戏
（init python 里 add_action("...")、Location("...")、动态拼接的提示文本等）
大量显示文本不在其列。本模块扫描反编译后的 .rpy 源码，挑出"像玩家可读文本"
的字面量，写入 tl/<语言>/zz_ng_pystrings.rpy 补充 strings 骨架。

关键保证：骨架只追加新条目，绝不改写已有条目，因此重复执行不会动任何
已翻译内容，也不会重复计费。
"""
import json
import os
import re

from . import ipatch
from .util import esc_rpy, is_display_text, unesc_rpy, work_dir

# 字面量紧跟这些关键字参数名时是代码参数而非显示文本
_KWARG_CODE = re.compile(
    r"\b(?:label|id|tag|tags|condition|context|new_context|image|file|filename|"
    r"voice|keysym|alternate|search|layer|predict|auto|update|function)\s*=\s*$")

# 单次扫描同时吃掉双/单引号字面量，避免撇号被误当成单引号字符串边界
_ANY_STR = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'')
_OLD = re.compile(r'^\s*old "((?:[^"\\]|\\.)*)"\s*$')
_NEW = re.compile(r'^\s*new "((?:[^"\\]|\\.)*)"\s*$')
# 对白语句/续行：who "text" 或以引号开头的续行（拼接长台词）。
# 这些文本已由官方 translate 块覆盖（或其片段根本不该单独翻译）。
_SAY_LINE = re.compile(r'^\s*[A-Za-z_][A-Za-z0-9_]*\s+["\']')
_QUOTE_LINE = re.compile(r'^\s*["\']')
# 例外：notify 是常见的自定义显示语句，走 strings 表；return 可能返回显示文本
_EXTRACT_LINE = re.compile(r'^\s*(?:notify|return)\s+["\']')
# 界面显示语句关键字开头：这些行上的引号字面量是界面文本而非对白
_SCREEN_STMT = re.compile(r"^(?:textbutton|text|caption|hovered|unhovered|action|tooltip|alt|activate)\b")
# 属性/变量赋值行：行内字符串是运行期动态显示的数据（self.orgasm_text = "..."、
# stats = {'sos': "Sense of Self", ...}），即使文本与某句对白相同也要进 strings 表
_ASSIGN_LINE = re.compile(r"^\s*[\w.]+\s*=[^=]")
# "^ 表达式"字符串内部的引号片段（游戏引擎运行期 eval 的拼接模板）
_EXPR_FRAG = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")

_HEADER = "# RenpyTranslatorNG Python 字面量补提取（工具生成；重复执行只追加新条目）\n"

# 运行时过滤器：变量对白/动态菜单等运行期拼接的文本也查 strings 译文表。
# 没有它，补提取的条目只有在显示端被 _() 包住的那些才会生效。
_RUNTIME_RPY = '''\
# RenpyTranslatorNG 动态文本翻译（工具生成）
# 1) _ng_t(s)：安全查 strings 译文表，非字符串/查不到译文时原样返回；
#    uipatch 对表达式/变量的显示包装使用它，绝无副作用。
#    v2：查不到时依次尝试——去首尾空白、剥 {color=} 标签、逐行、
#    大小写变体、尾缀短语、首词组合（"Examine X" → 查看X），
#    覆盖 .upper()/.format()/字符串拼接等运行期变形；
#    v3：结果按原文记忆化，回退级联每个新字符串只算一次。
# 2) say_menu_text_filter：让变量对白/动态菜单等运行期文本也过译文表。
# 3) renpy.input 提示词过译文表。ipatch 类补丁（init 1 整个换掉 renpy.input、
#    丢弃外部传入的提示词）必须包在比它们更内层的位置：init 0 先把引擎的
#    renpy.input 包一层，补丁随后捕获到的"原函数"就是这层包装——补丁给出的
#    新提示词（如 "(default is Sister)."，配合 ipatch 模块收进 strings 表）
#    到达引擎前同样被翻译；提示词已是中文时查不到表、原样返回，绝不二次翻译。
init -999 python:
    import re as _ng_re

    def _ng_lookup(s):
        try:
            t = __(s)
            if t != s:
                return t
            # 词对齐匹配会丢掉拼接片段原本的前后空格（词条常写作 " to "），
            # 补/去空格各再试一次
            for alt in (s + " ", " " + s, " " + s + " "):
                t = __(alt)
                if t != alt:
                    return t
        except Exception:
            pass
        return None

    def _ng_try_case(s):
        for cand in (s.title(), s.capitalize(), s.lower(), s.upper()):
            if cand and cand != s:
                t = _ng_lookup(cand)
                if t is not None:
                    return t
        return None

    # 记忆化：对白/界面文本高度重复，回退级联只需对每个新字符串跑一次，
    # 之后都是字典命中（shift+R 重载会重跑 init，缓存随之重建）
    _NG_T_CACHE = {}

    def _ng_t(s):
        if not isinstance(s, str) or not s:
            return s
        r = _NG_T_CACHE.get(s)
        if r is None:
            r = _NG_T_CACHE[s] = _ng_t_core(s)
        return r

    def _ng_t_core(s):
        try:
            t = _ng_lookup(s)
            if t is not None:
                return t
            core = s.strip()
            if not core:
                return s
            if core != s:
                t = _ng_t(core)
                if t != core:
                    return s[:len(s) - len(s.lstrip())] + t + s[len(s.rstrip()):]
            m = _ng_re.match(r'^(\\{color=[^}]*\\})(.+)(\\{/color\\})$', core, _ng_re.S)
            if m:
                t = _ng_t(m.group(2))
                if t != m.group(2):
                    return m.group(1) + t + m.group(3)
            if "\\n" in s:
                return "\\n".join([_ng_t(line) for line in s.split("\\n")])
            t = _ng_try_case(core)
            if t is not None:
                return t
            for suf in _NG_SUFFIXES:
                if core.endswith(suf) and len(core) > len(suf):
                    ts = _ng_lookup(suf) or _ng_try_case(suf)
                    if ts is not None:
                        head = core[:-len(suf)]
                        th = _ng_t(head)
                        if th != head:
                            return th + ts
            # 词对齐的前/后缀长短语扫描：运行期拼接句（'Call ' + name + ' the Intro Wife'）
            # 整句查不到时，按词边界用表中长短语试配（最长优先，≤6 词），
            # 命中则递归翻译剩余部分；无词条命中时原样返回，绝无副作用
            words = core.split(" ")
            if len(words) > 1:
                limit = min(6, len(words) - 1)
                for i in range(limit, 0, -1):
                    suf = " ".join(words[-i:])
                    for cand in (suf, " " + suf):
                        ts = _ng_lookup(cand) or _ng_try_case(cand)
                        if ts is not None:
                            head = core[:len(core) - len(cand)]
                            th = _ng_t(head)
                            if th != head:
                                th_r = th.rstrip(" ")
                                if th_r and ord(th_r[-1]) > 0x2E7F:
                                    ts = ts.lstrip(" ")
                                    th = th_r
                                return _ng_re.sub(" {2,}", " ", th + ts)
                for i in range(limit, 0, -1):
                    pre = " ".join(words[:i])
                    for cand in (pre + " ", pre):
                        th = _ng_lookup(cand) or _ng_try_case(cand)
                        if th is not None:
                            rest = core[len(cand):]
                            tr = _ng_t(rest)
                            if tr != rest:
                                sep = "" if (tr and (ord(tr[0]) > 0x2E7F or tr[0] == " ")) else " "
                                return _ng_re.sub(" {2,}", " ", th + sep + tr)
            i = core.find(" ")
            if i > 0:
                head, rest = core[:i], core[i + 1:]
                th = _ng_lookup(head) or _ng_try_case(head)
                if th is not None:
                    tr = _ng_t(rest)
                    if tr != rest:
                        sep = "" if th and ord(th[-1]) > 0x2E7F else " "
                        return th + sep + tr
            return s
        except Exception:
            return s

    _NG_SUFFIXES = (" on whom?",)

# init 0：早于 ipatch 补丁（init 1）捕获 renpy.input，让补丁内部调用的
# "原函数"就是这层翻译包装（补丁替换出的新提示词也能被翻译）
init 0 python:
    _ng_input0 = renpy.input

    def _ng_input_prompt(prompt, *args, **kwargs):
        try:
            if isinstance(prompt, str) and prompt:
                p = _ng_t(prompt)
                if p:
                    prompt = p
        except Exception:
            pass
        return _ng_input0(prompt, *args, **kwargs)

    renpy.input = _ng_input_prompt

init 999 python:
    _ng_prev_filter = config.say_menu_text_filter

    def _ng_dyn_filter(s):
        try:
            if isinstance(s, str) and s:
                s = __(s)
        except Exception:
            pass
        if _ng_prev_filter is not None:
            try:
                s = _ng_prev_filter(s)
            except Exception:
                pass
        return s

    config.say_menu_text_filter = _ng_dyn_filter

    # init 0 那层的兜底：若补丁在 init >=999 捕获/替换 renpy.input，这里再包一层；
    # 提示词已被 _() 或内层翻译成中文时查不到表、原样返回，绝不二次翻译
    _ng_prev_input = renpy.input

    def _ng_input(prompt, *args, **kwargs):
        try:
            if isinstance(prompt, str) and prompt:
                p = _ng_t(prompt)
                if p:
                    prompt = p
        except Exception:
            pass
        return _ng_prev_input(prompt, *args, **kwargs)

    renpy.input = _ng_input
'''


def _iter_source_files(gamedir):
    for root, dirs, files in os.walk(gamedir):
        dirs[:] = [d for d in dirs if d not in ("tl", "cache", "saves", "fonts_ng_backup")]
        for f in sorted(files):
            if f.endswith(".rpy") and not f.startswith("zz_ng"):
                # 补丁文件里的 replace 链/伪语言块/整句字典是改写规则而非游戏文本，
                # 收进 strings 表会白白翻译、还可能反向污染译文
                p = os.path.join(root, f)
                if ipatch.is_patch_file(p):
                    continue
                yield p


def _skeleton_path(game_base, language):
    return os.path.join(game_base, "game", "tl", language, "zz_ng_pystrings.rpy")


def _load_existing(path):
    """读已有补充骨架，保持原顺序返回 [(old, new)]；不存在返回 []。"""
    pairs = []
    if not os.path.isfile(path):
        return pairs
    try:
        with open(path, "r", encoding="utf-8") as f:
            pending = None
            for ln in f:
                m = _OLD.match(ln)
                if m:
                    pending = unesc_rpy(m.group(1))
                    continue
                m = _NEW.match(ln)
                if m and pending is not None:
                    pairs.append((pending, unesc_rpy(m.group(1))))
                    pending = None
    except OSError:
        pass
    return pairs


_TL_OLD = re.compile(r'^\s*old (?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')', re.M)


def _collect_tl_olds(game_base, language):
    """官方/已有 tl 文件里定义过的 strings 条目（old，兼容单双引号写法）。

    Ren'Py 7.4+ 对同一 old 的第二次定义直接抛异常（启动即崩），因此这些文本
    对补充骨架是硬约束：任何放行规则都不能越过。官方条目本身就在译文表里，
    运行期 __()/say_menu_text_filter 查表照样生效，不收进骨架不损失翻译。
    """
    olds = set()
    tl_dir = os.path.join(game_base, "game", "tl", language)
    if os.path.isdir(tl_dir):
        for root, _dirs, files in os.walk(tl_dir):
            for f in files:
                # 排除补充骨架自身：它不是官方定义，否则自己的条目会被当成
                # "官方已有"全部误删；骨架内部重复由 write_skeleton 写入时去重
                if not f.endswith(".rpy") or f.startswith("zz_ng"):
                    continue
                try:
                    with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                        for m in _TL_OLD.finditer(fh.read()):
                            olds.add(unesc_rpy(m.group(1) if m.group(1) is not None
                                               else m.group(2)))
                except OSError:
                    pass
    return olds


def _collect_skip(game_base, language, dump_path):
    """软去重集合：对白 dump 里的台词、译文缓存里已存在的条目。

    与这些重复的文本默认不收，但赋值/notify/return 行（运行期动态显示的数据）
    仍放行进 strings 表。官方 tl 已定义的 old 不在此列——那是硬约束，
    由 _collect_tl_olds 单独处理。"""
    skip = set()
    if dump_path and os.path.isfile(dump_path):
        try:
            with open(dump_path, "r", encoding="utf-8") as f:
                dump = json.load(f)
            for e in dump.values():
                for node in e.get("nodes", []):
                    w = node.get("what")
                    if w:
                        skip.add(w)
                    # 菜单 caption 不进 skip：数据驱动游戏在 Python 里 add_choice()
                    # 构建菜单，同样的文本运行期走 strings 表，必须收录
        except Exception:
            pass
    cache = os.path.join(work_dir(game_base), "translations.json")
    if os.path.isfile(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                for k in json.load(f):
                    if k.startswith("S:"):
                        skip.add(k[2:])
        except Exception:
            pass
    return skip


def scan_strings(game_base, dump_path=None, language="chinese"):
    """扫描源码，返回按首次出现顺序去重的候选原文列表。"""
    gamedir = os.path.join(game_base, "game")
    skip = _collect_skip(game_base, language, dump_path)
    tl_olds = _collect_tl_olds(game_base, language)
    seen = set()
    out = []
    for path in _iter_source_files(gamedir):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().split("\n")
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("old ") \
                    or stripped.startswith("new "):
                continue
            # 对白行/续行整体跳过，避免台词碎片混进候选（notify/return 例外）；
            # 界面语句（textbutton 等）即使带引号字面量也不是对白，不能跳过
            say_like = (_SAY_LINE.match(line) or _QUOTE_LINE.match(line)) \
                and not _EXTRACT_LINE.match(line) and not _SCREEN_STMT.match(stripped)
            if say_like:
                continue
            for m in _ANY_STR.finditer(line):
                before = line[:m.start()]
                raw = m.group(1) if m.group(1) is not None else m.group(2)
                try:
                    val = unesc_rpy(raw)
                except Exception:
                    continue
                # "^ 表达式"字符串是游戏引擎运行期 eval 的拼接模板，本身不是显示文本，
                # 但其内部引号片段（'Give '、' to whom?'）是最终显示文本的组成部分
                if val.startswith("^"):
                    for m2 in _EXPR_FRAG.finditer(val):
                        try:
                            frag = unesc_rpy(m2.group(1) or m2.group(2))
                        except Exception:
                            continue
                        if is_display_text(frag) and frag not in seen \
                                and frag not in skip and frag not in tl_olds:
                            seen.add(frag)
                            out.append(frag)
                    continue
                # 已在任一 tl old 表 / dump / 译文缓存里的不再收（去重靠 skip 集合）。
                # 注意：_() 包装的字符串也要收（官方提取基于原始 rpyc，看不到包装）；
                # 属性/变量赋值行里的字符串是运行期动态显示的数据
                # （如 self.orgasm_text = "Aaaggghhhh!!"），即使与某句对白相同也要进 strings 表
                if _KWARG_CODE.search(before):
                    continue
                if not is_display_text(val) or val in seen:
                    continue
                # 官方 tl 已定义 old 的绝不收：重复定义会让 Ren'Py 启动即崩，
                # 且官方条目就在译文表里，运行期查表照样生效——赋值/notify 行也不放行
                if val in tl_olds:
                    continue
                # 与对白 dump/译文缓存重复的文本，仅当出现在赋值/notify/return 行
                # （运行期动态显示的数据）时仍要进 strings 表
                if val in skip and not _EXTRACT_LINE.match(line) \
                        and not _ASSIGN_LINE.match(line):
                    continue
                seen.add(val)
                out.append(val)
    return out


def _write_runtime_filter(game_base):
    """写运行时过滤器 zz_ng_dyntrans.rpy（幂等，重跑直接覆盖）。"""
    path = os.path.join(game_base, "game", "zz_ng_dyntrans.rpy")
    pyc = path + "c"
    if os.path.exists(pyc):
        try:
            os.remove(pyc)
        except OSError:
            pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(_RUNTIME_RPY)


def write_skeleton(game_base, language, dump_path=None, log=print, extra=None):
    """生成/合并补充骨架与运行时过滤器，返回本次新增条数。

    extra：ipatch 补丁自有的玩家可见文本（输入提示词等），游戏源码里没有
    对应字面量，必须由调用方显式并入骨架一起翻译。

    重写时顺带自愈：剔除骨架里与官方 tl 重复定义的 old（会让 Ren'Py
    启动即崩）及骨架自身重复的条目，因此旧版本生成的坏文件重跑即可修复。
    """
    found = scan_strings(game_base, dump_path, language)
    if extra:
        have_f = set(found)
        for s in extra:
            if s and s not in have_f:
                found.append(s)
                have_f.add(s)
    _write_runtime_filter(game_base)
    path = _skeleton_path(game_base, language)
    tl_olds = _collect_tl_olds(game_base, language)
    existing = _load_existing(path)
    merged = []
    have = set()
    purged = 0
    for old, new in existing:
        if old in tl_olds or old in have:
            purged += 1
            continue
        have.add(old)
        merged.append((old, new))
    if purged:
        log("剔除补充骨架中重复定义的 %d 条（与官方 tl 或骨架内部重复，"
            "重复 old 会导致游戏启动崩溃）" % purged)
    add = [s for s in found if s not in have]
    if not add and not purged:
        return 0
    blocks = [_HEADER, "translate %s strings:\n" % language]
    for old, new in merged:
        blocks.append('\n    old "%s"\n    new "%s"\n' % (esc_rpy(old), esc_rpy(new)))
    for old in add:
        blocks.append('\n    old "%s"\n    new ""\n' % esc_rpy(old))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(blocks))
    os.replace(tmp, path)
    pyc = path + "c"
    if os.path.exists(pyc):
        try:
            os.remove(pyc)
        except OSError:
            pass
    return len(add)
