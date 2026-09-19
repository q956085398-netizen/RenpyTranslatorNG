# -*- coding: utf-8 -*-
"""给游戏中未加 _() 的屏幕显示文本补上翻译包装。
很多游戏的自定义界面写的是 textbutton "Play"（无 _()），引擎不视其为可翻译字符串。
本模块在反编译出的 .rpy 上做外科手术，覆盖四类形态：
1. 屏幕语句字面量：textbutton "Play"（官方提取可收录）
2. renpy.input/renpy.notify 的提示参数
3. 属性链表达式：text a.name / textbutton i.caption: / text message line_leading 5
   ——数据驱动型游戏把 add_action("...") 等普通 Python 字符串存在对象里再显示，
   官方提取完全看不到，包装显示点后即可在运行时查 strings 译文表
4. 运行时拼接："{} is : {}".format(x, y) 与 "a" + var + "b" 形态的显示拼接，
   对模板、简单参数和拼接片段分别包包装，让组成片段各自查表

字符串字面量用 _() 包装（官方提取靠它识别）；表达式/变量用 _ng_t() 包装
（由 zz_ng_dyntrans.rpy 定义，非字符串或查不到译文时原样返回，绝无副作用）。
原文件备份到工作目录。
"""
import os
import re
import shutil

from . import ipatch
from .util import _EXT, is_display_text, unesc_rpy, work_dir

# 表达式包装函数名（由 zz_ng_dyntrans.rpy 提供，init -999 定义）
EXPR_WRAP = "_ng_t"

# 行首空白 + 显示控件关键字 + 紧随的裸字符串字面量
_DISPLAY = re.compile(r'^(\s*(?:textbutton|text|label)\s+)"((?:[^"\\]|\\.)*)"')
# 同上，单引号字面量变体（有些游戏 screens 用 'xxx' 写按钮文字）
_DISPLAY1 = re.compile(r"^(\s*(?:textbutton|text|label)\s+)'((?:[^'\\]|\\.)*)'")
# renpy.input("提示") / renpy.notify("提示") 的首个裸字符串参数
_INPUT = re.compile(r'(renpy\.(?:input|notify)\(\s*)"((?:[^"\\]|\\.)*)"')
_INPUT1 = re.compile(r"(renpy\.(?:input|notify)\(\s*)'((?:[^'\\]|\\.)*)'")
# show text "..."
_SHOW_TEXT = re.compile(r'^(\s*show\s+text\s+)"((?:[^"\\]|\\.)*)"')
# show screen notify("提示")
_NOTIFY_SCREEN = re.compile(r'^(\s*show\s+screen\s+notify\(\s*)"((?:[^"\\]|\\.)*)"')
# 屏幕语句的属性链表达式：text a.name / textbutton i.caption: / text message line_leading 5
# 不含 label：`label xxx:` 是脚本标签语法，会被误包
_ATTR_EXPR = re.compile(
    r'^(\s*(?:text|textbutton|caption)\s+)'
    r'([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?:\(\))?)'
    r'(\s*:|\s+[A-Za-z_][A-Za-z0-9_]*\b.*|\s*)$')
# "模板".format( 的模板部分（单双引号各自配对，禁止跨字面量贪婪跨越）
_FMT_HEAD = re.compile(r'"((?:[^"\\]|\\.)*)"\s*\.format\(|\'((?:[^\'\\]|\\.)*)\'\s*\.format\(')
_WORD = re.compile(r"[A-Za-z]{2,}")
_INTERP_ONLY = re.compile(r'^\[[^\]]*\]$')

# .format 简单参数：变量属性链 / 无嵌套调用 / 下标 / 字面量
_ARG_VAR = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*$')
_ARG_CALL = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*\([^()]*\)$')
_ARG_SUB = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*(?:\[[^\[\]]*\])+$')
_ARG_LIT = re.compile(r'^([\'"])((?:\\.|.)*?)\1$', re.S)
# 拼接行里的简单变量片段（允许 stats[stat] 形态的下标）
_SUB = r"(?:\[[^\[\]]*\])*"
_CONCAT_VAR = re.compile(r'(?<=\+)\s*([A-Za-z_][A-Za-z0-9_.]*' + _SUB + r')\s*(?=\+)')
_CONCAT_VAR_END = re.compile(r'\+\s*([A-Za-z_][A-Za-z0-9_.]*' + _SUB + r')\s*$')
_CONCAT_VAR_HEAD = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\+')
_CONCAT_VAR_PRE = re.compile(r'(?<![A-Za-z0-9_.+])([A-Za-z_][A-Za-z0-9_.]*' + _SUB + r')\s*\+(?!\+)')
_LIT_ANY = re.compile(r'(["\'])((?:\\.|.)*?)\1')
_PY_KW = {"if", "else", "elif", "and", "or", "in", "is", "not", "for", "while", "return", "None", "True", "False"}

# —— 第 5 类：运行期组合拼接的漏包片段 ——
# 数据驱动游戏常在 Python 里把「文字片段 + 变量 + 颜色标签」拼成一整句再显示，
# 只包变量不够，拼进来的英文短语片段也要各自查表。以下规则全部幂等：
# 替换产物不再匹配原模式。
_CMP_RULES = [
    # 自定义 notify 语句的字面量参数：notify 'Day has ended' → notify _ng_t('Day has ended')
    (re.compile(r"^(\s*notify\s+)'((?:[^'\\]|\\.)*)'(\s*)$"), r"\1_ng_t('\2')\3"),
    # 播报组合头："Your {color=" + color + "}" → _ng_t("Your ") + "{color=" + color + "}"
    (re.compile(r'"Your \{color=" \+ (?:_ng_t\(color\)|color) \+ "\}"'),
     '_ng_t("Your ") + "{color=" + color + "}"'),
    # 同上，他人属性：self.name + "'s {color=" + ...
    (re.compile(r'self\.name \+ "\'s \{color=" \+ (?:_ng_t\(color\)|color) \+ "\}"'),
     '_ng_t(self.name) + _ng_t("\'s ") + "{color=" + color + "}"'),
    # （旧版这里有一条包装 stats[stat] 的规则，曾把 self.stats[stat] 数值属性也包上
    #   导致 "can't assign to function call"，已改为只包裸 stats[stat]：那是 script 里
    #   的"显示名"字典，格式化进界面文本需要翻译；接收者前缀/赋值/比较/del 一律排除）
    (re.compile(r'(?<!_ng_t\()(?<![\w.])(?<!del )stats\[stat\](?![\w.])'
                r'(?!\s*(?:==|!=|<=|>=|<<=|>>=|[-+*/%+]?=))'), '_ng_t(stats[stat])'),
    # 被错误切断的模板片段（历史补丁产物）与未包片段
    (re.compile(r'_?\("\{/color\} has been raised"\)|"\{/color\} has been raised"'),
     '"{/color}" + _ng_t(" has been raised")'),
    (re.compile(r'_?\("\{/color\} has been decreased"\)|"\{/color\} has been decreased"'),
     '"{/color}" + _ng_t(" has been decreased")'),
    # 数值增量片段
    (re.compile(r'" by \{color=" \+ (?:_ng_t\(color\)|color) \+ "\}"'),
     '_ng_t(" by ") + "{color=" + color + "}"'),
    # 无变化变体：self.name + "'s {color=#00F}"（固定色值）
    (re.compile(r'self\.name \+ "(\'s \{color=#[0-9A-Fa-f]{3,6}\})"'),
     '_ng_t(self.name) + _ng_t("\\1")'),
    # 原因括号片段
    (re.compile(r'" \(\{\}\)"\.format\('), '_ng_t(" ({})").format('),
    # 检视面板标题行赋值：sblock = "..." → sblock = _ng_t("...")
    (re.compile(r'(\bsblock = )"((?:[^"\\]|\\.)*)"'), r'\1_ng_t("\2")'),
    # 状态等级带色返回：... + text + "{/color}" → + _ng_t(text) + ...
    (re.compile(r'(\{color=" \+ color \+ "\}" \+ )text( \+ "\{/color\}")'), r'\1_ng_t(text)\2'),
    # 日历资源条目：星期名先翻译再大写，工具提示文字包装
    (re.compile(r'\.format\(days\[day\]\.upper\(\)\)'), '.format(_ng_t(days[day].upper()))'),
    (re.compile(r"\('week', 'CALENDAR'"), "('week', _ng_t('CALENDAR')"),
    (re.compile(r"\('money', 'MONEY'"), "('money', _ng_t('MONEY')"),
    (re.compile(r"\('energy', \"ENERGY\""), "('energy', _ng_t(\"ENERGY\")"),
]


def _apply_cmp_rules(line):
    changed = False
    for pat, rep in _CMP_RULES:
        new = pat.sub(rep, line)
        if new != line:
            line = new
            changed = True
    return line, changed


def _should_wrap(s):
    if not s.strip():
        return False
    if _INTERP_ONLY.match(s):
        return False
    if not _WORD.search(s):
        return False
    return True


def _wrap_line(line):
    """对一行做包装，返回 (新行, 是否修改)。"""
    if "_(" in line or EXPR_WRAP + "(" in line:
        return line, False
    if line.lstrip().startswith("#"):
        return line, False
    m = _SHOW_TEXT.match(line) or _DISPLAY.match(line) or _DISPLAY1.match(line) or _NOTIFY_SCREEN.match(line)
    if m and _should_wrap(m.group(2)):
        return '%s_("%s")%s' % (m.group(1), m.group(2), line[m.end():]), True
    m = _INPUT.search(line) or _INPUT1.search(line)
    if m and _should_wrap(m.group(2)):
        return line[:m.start()] + m.group(1) + '_("%s")' % m.group(2) + line[m.end():], True
    m = _ATTR_EXPR.match(line)
    if m:
        return "%s%s(%s)%s" % (m.group(1), EXPR_WRAP, m.group(2), m.group(3) or ""), True
    for fn in (_wrap_format, _wrap_concat):
        new, ok = fn(line)
        if ok:
            return new, True
    return line, False


def _tpl_ok(tpl):
    """含 {} 的模板可否作为翻译模板：排除路径/代码形态，防止图片路径被翻掉。"""
    if _EXT.search(tpl) or "/" in tpl or "\\" in tpl:
        return False
    return _WORD.search(tpl) is not None


# 代码用模板的占位符：{} / {0} / {name}
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
# 占位符剥离后只剩字母/数字/下划线（含空串）→ 标识符形态
_IDENT_REST = re.compile(r"^[A-Za-z0-9_]*$")


def _is_code_template(tpl):
    """"{}_{}_{}"/"{}_{}_fallback" 这类占位符间以下划线相连、其余部分只有
    标识符字符的模板，是拼 key/label 用的代码模板而非显示文本
    （显示模板必有空格或标点；裸 "{}" 的参数常是显示人名，不能误判）。"""
    rest = _PLACEHOLDER.sub("", tpl)
    return "_" in rest and _IDENT_REST.match(rest) is not None


# 路径模板："gui/button/{}.png" 这类。format 的参数是文件名组成（money/energy 等），
# 包装后查表会变成 "gui/button/钱.png"，资源直接加载失败
_PATH_TEMPLATE = re.compile(
    r'''["'][^"']*/[^"']*\.(?:png|jpg|jpeg|webp|webm|gif|bmp|mp4|ogg|opus|mp3|wav|ttf|otf)["']''',
    re.I)


def _wrap_format(line):
    """"{} .. {}".format(x, y) 形态：模板与简单参数分别包包装。"""
    if ".format(" not in line or ('"' not in line and "'" not in line):
        return line, False
    if _PATH_TEMPLATE.search(line):
        return line, False  # 路径模板：模板与参数都是文件名组成，一律不包
    head_edit = None
    head = _FMT_HEAD.search(line)
    if head:
        tpl = head.group(1) if head.group(1) is not None else head.group(2)
        tpl = unesc_rpy(tpl)
        if _is_code_template(tpl):
            # 代码模板（拼 label/key 用）：模板与参数都不得包装，否则运行时
            # 把 "give"/"kitty" 等组成片段翻成中文，has_label 永远失配
            return line, False
        if is_display_text(tpl) or ("{}" in tpl and _tpl_ok(tpl)):
            q = '"' if head.group(1) is not None else "'"
            raw = head.group(1) if head.group(1) is not None else head.group(2)
            head_edit = (head.start(), head.end(),
                         "_(%s%s%s).format(" % (q, raw, q))
    base = line
    if head_edit:
        base = line[:head_edit[0]] + head_edit[2] + line[head_edit[1]:]
    idx = base.find(".format(")
    if idx < 0:
        return line, False
    start = idx + len(".format(")
    n = len(base)
    args = []
    cur = start
    depth = 1
    i = start
    while i < n and depth:
        c = base[i]
        if c in "\"'":
            q = c
            i += 1
            while i < n:
                if base[i] == "\\":
                    i += 2
                    continue
                if base[i] == q:
                    break
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                args.append((cur, i))
        elif c == "," and depth == 1:
            args.append((cur, i))
            cur = i + 1
        i += 1
    edits = []
    for s0, e0 in args:
        a = base[s0:e0].strip()
        if not a or a.startswith("_(") or a.startswith(EXPR_WRAP + "("):
            continue
        ok = bool(_ARG_VAR.match(a) or _ARG_CALL.match(a) or _ARG_SUB.match(a))
        if not ok:
            m = _ARG_LIT.match(a)
            if m and (is_display_text(unesc_rpy(m.group(2))) or "{}" in m.group(2)):
                ok = True
        if ok:
            # 字面量用 _()（官方提取需要）；表达式用 _ng_t()（非字符串安全）
            if ok and _ARG_LIT.match(a):
                edits.append((s0, e0, "_(%s)" % a))
            else:
                edits.append((s0, e0, "%s(%s)" % (EXPR_WRAP, a)))
    for s0, e0, rep in reversed(edits):
        base = base[:s0] + rep + base[e0:]
    return base, bool(head_edit or edits)


def _plus_adjacent(line, m):
    """字面量 match 与 + 是否相邻（跳过空格与一层配对括号）。"""
    i = m.start() - 1
    while i >= 0 and line[i] in " \t":
        i -= 1
    if i >= 0 and line[i] == "+":
        return True
    if i >= 0 and line[i] == ")":
        j = i - 1
        seen = 1
        while j >= 0 and seen:
            if line[j] == ")":
                seen += 1
            elif line[j] == "(":
                seen -= 1
            j -= 1
        if not seen:
            while j >= 0 and line[j] in " \t":
                j -= 1
            if j >= 0 and line[j] == "+":
                return True
    i = m.end()
    while i < len(line) and line[i] in " \t":
        i += 1
    if i < len(line) and line[i] == "+":
        return True
    if i < len(line) and line[i] == "(":
        j = i + 1
        seen = 1
        while j < len(line) and seen:
            if line[j] == "(":
                seen += 1
            elif line[j] == ")":
                seen -= 1
            j += 1
        while j < len(line) and line[j] in " \t":
            j += 1
        if j < len(line) and line[j] == "+":
            return True
    return False


def _lit_across_plus(line, pos, forward):
    """pos 处（跳过空格）遇到 + 时，检查该 + 的另一侧是否紧挨字符串字面量。"""
    i = pos
    if forward:
        step = 1
        n = len(line)
    else:
        step = -1
        n = -1
    while i != n and line[i] in " \t":
        i += step
    if i == n or line[i] != "+":
        return False
    i += step
    while i != n and line[i] in " \t":
        i += step
    if i == n:
        return False
    return line[i] in "\"'"


def _wrap_concat(line):
    """"a" + var + "b" 形态：与 + 相邻的显示文本字面量和简单变量分别包包装。"""
    if "+" not in line or ('"' not in line and "'" not in line):
        return line, False
    # 上下文门槛：必须存在"像显示文本"的字面量参与拼接，纯代码拼接（标签名、
    # 分隔符等）不处理，避免把 has_label(...) 之类的逻辑变量包进去
    lits = [m for m in _LIT_ANY.finditer(line)
            if _plus_adjacent(line, m)
            and (is_display_text(unesc_rpy(m.group(2))) or "{}" in m.group(2))]
    if not lits:
        return line, False
    edits = []
    for m in lits:
        edits.append((m.start(), m.end(), "_(%s)" % m.group(0)))
    vars_ = set()
    for pat in (_CONCAT_VAR, _CONCAT_VAR_END, _CONCAT_VAR_HEAD, _CONCAT_VAR_PRE):
        for m in pat.finditer(line):
            v = m.group(1)
            if v in _PY_KW:
                continue
            # 变量必须与"挨着字符串字面量的 +"同侧，纯数值/代码拼接不包
            if not (_lit_across_plus(line, m.start(), False)
                    or _lit_across_plus(line, m.end(), True)):
                continue
            vars_.add((m.start(1), m.end(1), "%s(%s)" % (EXPR_WRAP, v)))
    edits.extend(vars_)
    if not edits:
        return line, False
    edits.sort(key=lambda e: e[0])
    merged = []
    for e in edits:
        if merged and e[0] < merged[-1][1]:
            continue
        merged.append(e)
    for s0, e0, rep in reversed(merged):
        line = line[:s0] + rep + line[e0:]
    return line, True


# 旧版补丁误包的两类残留：
# 1) _ng_t(self.stats[stat]) 等带接收者前缀的：数值属性，包装无效果且破坏赋值/del，还原；
#    注意裸 stats[stat] 是 script 里的"显示名"字典，包装是合法的，不能误伤
# 2) 其他 _ng_t(x) 出现在赋值号左边或比较/增量运算符旁：函数调用不能作赋值目标
_STATS_WRAP = re.compile(r'_ng_t\((([A-Za-z_]\w*\.)+stats\[stat\])\)')
_ASSIGN_TARGET = re.compile(
    r'_ng_t\(((?:[^()]|\([^()]*\))*)\)'
    r'(\s*(?:==|!=|<=|>=|<<=|>>=|[-+*/%+]?=))')
_NG_WRAP = re.compile(r'_ng_t\(((?:[^()]|\([^()]*\))*)\)')


def _unwrap_path_format(line):
    """路径模板 "x/{}.png".format(_ng_t(arg))：参数是文件名组成，拆掉 _ng_t 包装。"""
    if "_ng_t(" not in line or ".format(" not in line:
        return line, False
    m = _PATH_TEMPLATE.search(line)
    if not m:
        return line, False
    idx = line.find(".format(", m.end())
    if idx < 0:
        return line, False
    start = idx + len(".format(")
    depth, i, n = 1, start, len(line)
    while i < n and depth:
        c = line[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c in "\"'":
            q = c
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == q:
                    break
                i += 1
        i += 1
    span = line[start:i]
    new_span, n = _NG_WRAP.subn(r"\1", span)
    if not n:
        return line, False
    return line[:start] + new_span + line[i:], n


# 代码模板的两种头部形态："tpl".format( 与 _("tpl").format(
_CODE_FMT_HEAD = re.compile(
    r'_\(\s*(["\'])((?:\\.|.)*?)\1\s*\)\.format\('
    r'|(["\'])((?:\\.|.)*?)\3\.format\(')


def _unwrap_code_format(line):
    """代码模板 "{}_{}_{}".format(_ng_t(a),...)：参数是 label/key 组成而非显示文本，
    拆掉参数上的 _ng_t 包装；模板被 _() 包着的（历史产物，官方提取虽未收录、
    运行时查表原样返回，但留着有被回填误翻的隐患）也一并还原。"""
    if ".format(" not in line:
        return line, False
    m = _CODE_FMT_HEAD.search(line)
    if not m:
        return line, False
    tpl = m.group(2) if m.group(2) is not None else m.group(4)
    if not _PLACEHOLDER.search(tpl) or not _is_code_template(unesc_rpy(tpl)):
        return line, False
    start = m.end()  # 匹配以 ".format(" 结尾
    depth, i, n = 1, start, len(line)
    while i < n and depth:
        c = line[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c in "\"'":
            q = c
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == q:
                    break
                i += 1
        i += 1
    edits = []
    span = line[start:i]
    new_span, k = _NG_WRAP.subn(r"\1", span)
    if k:
        edits.append((start, i, new_span))
    if m.group(2) is not None:
        # _("tpl").format( → "tpl".format(（保留原引号与原文内容）
        q, raw = m.group(1), m.group(2)
        edits.append((m.start(), m.start() + 2, ""))
        close = line.find(")", m.start())
        if close >= 0 and close < start:
            edits.append((close, close + 1, ""))
    if not edits:
        return line, False
    for s0, e0, rep in sorted(edits, reverse=True):
        line = line[:s0] + rep + line[e0:]
    return line, len(edits)


def repair_wrapped_targets(game_base, log=print):
    """修复旧版补丁把 _ng_t() 包到数值/赋值目标/路径参数上的错误，返回修复处数。"""
    gamedir = os.path.join(game_base, "game")
    fixed = 0
    for root, dirs, files in os.walk(gamedir):
        dirs[:] = [d for d in dirs
                   if d not in ("fonts_ng_backup", "cache", "saves", "uipatch_backup")]
        for f in files:
            if not f.endswith(".rpy"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            new, n = _STATS_WRAP.subn(r"\1", text)
            new, n2 = _ASSIGN_TARGET.subn(r"\1\2", new)
            n += n2
            if "_ng_t(" in new or "_(" in new:
                lines = new.split("\n")
                for i, ln in enumerate(lines):
                    lines[i], k = _unwrap_path_format(ln)
                    n += k
                    lines[i], k = _unwrap_code_format(lines[i])
                    n += k
                new = "\n".join(lines)
            if n:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(new)
                fixed += n
                log("修复赋值目标误包: %s（%d 处）" % (os.path.relpath(path, gamedir), n))
    if fixed:
        log("共修复 %d 处 _ng_t 误包的赋值目标" % fixed)
    return fixed


def patch_game(game_base, log=print):
    """返回 (修改文件数, 包装处数)。"""
    repair_wrapped_targets(game_base, log)
    gamedir = os.path.join(game_base, "game")
    backup_root = os.path.join(work_dir(game_base), "uipatch_backup")
    patched_files = wrapped = 0
    for root, dirs, files in os.walk(gamedir):
        dirs[:] = [d for d in dirs if d not in ("tl", "fonts_ng_backup", "fonts", "cache", "saves")]
        for f in files:
            if not f.endswith(".rpy") or f.startswith("zz_ng"):
                continue
            path = os.path.join(root, f)
            # 补丁文件里的字符串是改写规则（原文/补丁后文本），包 _() 会改变补丁语义
            if ipatch.is_patch_file(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    lines = fh.read().split("\n")
            except OSError:
                continue
            changed = False
            for i, line in enumerate(lines):
                lines[i], c = _wrap_line(line)
                lines[i], c2 = _apply_cmp_rules(lines[i])
                c = c or c2
                changed = changed or c
                if c:
                    wrapped += 1
            if changed:
                rel = os.path.relpath(path, gamedir)
                bak = os.path.join(backup_root, rel)
                if not os.path.isfile(bak):
                    os.makedirs(os.path.dirname(bak), exist_ok=True)
                    shutil.copy2(path, bak)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
                patched_files += 1
                log("UI补翻译包装: %s" % rel)
    if patched_files:
        log("共 %d 个文件、%d 处文本已加 _() 包装（原文件备份于 %s）"
            % (patched_files, wrapped, backup_root))
    else:
        log("未发现需要补包装的界面文本")
    return patched_files, wrapped
