# -*- coding: utf-8 -*-
"""合成 Ren'Py 游戏的提取模拟器（测试基础设施）。

真实提取（core.extract.extract）靠向游戏注入钩子、启动游戏进程完成；合成游戏
没有引擎和 exe，本模块作为引擎替身，产物与真实引擎逐行对齐：

- dump（ng_extract.json）：{标识符: {filename, lineno, nodes}}，与运行期钩子同构。
  标识符公式复刻引擎 Restructurer.create_translate：<label>_<md5(say代码+"\\r\\n")[:8]>，
  冲突时追加 "_1" 后缀；who 为说话人变量（字符串字面量说话人带引号）、旁白为空串；
  what 为原文（未求值，含 {标签}、[变量] 与转义）。每条 say 自成一个翻译块。
- tl 骨架：复刻 renpy.translation.generation（write_translates / write_strings /
  open_tl_file）：新文件以 BOM + "# TODO: Translation updated at <时间>" 开头；
  每个翻译块为 "# <file>:<line>" + "translate <lang> <id>:" + 空行 +
  [全部注释行][全部代码行]（代码行经 empty_filter 为空串）+ 空行；
  strings 块 old/new 成对（new 为空），每个源文件一个块。时间戳固定，产物可复现。
- 菜单字幕与 _() 文本走 strings 机制（现代引擎行为，菜单节点不进 dump）；
  再次提取时跳过已有译文的语言块与 strings 条目（引擎增量语义），因此
  重复提取幂等：dump 不变、已填译文不动。
- common.rpy 生成直接复用 core.extract.gen_common_tl（与真实提取相同）。

支持的语法面 = 随仓库分发的合成游戏所用的形态：label、say（变量说话人/旁白/
字符串字面量说话人/三引号多行）、menu（字幕 + if 条件 + 条目体内的 say）、
jump/return/$/show 等单行语句、define/default、init python 与 python 块
（只扫 _()）、translate <语言> 块（None 以外一律跳过，含 ipatch 伪语言）。
"""
import ast
import hashlib
import json
import os
import re

from core import extract as _real_extract
from core.util import unesc_rpy

# 引擎 open_tl_file 的固定时间戳（真机取当前时间；测试要可复现，固定即可）
_TODO_STAMP = "2026-09-20 12:00"

_SAY_VAR = re.compile(r'^([A-Za-z_]\w*)\s+("((?:[^"\\]|\\.)*)")\s*$')
_SAY_STR = re.compile(r'^("((?:[^"\\]|\\.)*)")\s+("((?:[^"\\]|\\.)*)")\s*$')
_SAY_NARR = re.compile(r'^("((?:[^"\\]|\\.)*)")\s*$')
_SAY_TQ = re.compile(r'^(?:([A-Za-z_]\w*)\s+)?"""(.*)$')
_MENU_ITEM = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:if\b[^:]*)?:\s*$')
_LABEL = re.compile(r'^label\s+([A-Za-z_]\w*)\s*:\s*$')
_XL_HDR = re.compile(r'^translate\s+(\S+)\s+(\S+)\s*:\s*$')
_PY_HDR = re.compile(r'^init\s+(?:-?\d+\s+)?(?:python|hide)\s*:\s*$'
                     r'|^python\s*:\s*$')
_DUNDER = re.compile(r'\b_\(\s*"((?:[^"\\]|\\.)*)"\s*\)')


def encode_say_string(s):
    """复刻 renpy.translation.encode_say_string：say 语句字符串的编码方式。"""
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = re.sub(r"(?<= ) ", "\\ ", s)
    return '"' + s + '"'


def quote_unicode(s):
    """复刻 renpy.translation.quote_unicode：strings 块 old/new 的转义方式。"""
    for a, b in (("\\", "\\\\"), ('"', '\\"'), ("\a", "\\a"), ("\b", "\\b"),
                 ("\f", "\\f"), ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"),
                 ("\v", "\\v")):
        s = s.replace(a, b)
    return s


def unesc(s):
    """脚本字符串字面量内容（引号已剥）-> 实际文本。"""
    return ast.literal_eval('"%s"' % s)


def say_code(who, what):
    """复刻 Say.get_code()（无属性/参数的简单形态）：注释与代码行的锚点。"""
    parts = []
    if who:
        parts.append(who)
    parts.append(encode_say_string(what))
    return " ".join(parts)


def say_identifier(label, code, used):
    """复刻 Restructurer.create_translate 的标识符公式（含冲突后缀）。"""
    md5 = hashlib.md5()
    md5.update((code + "\r\n").encode("utf-8"))
    digest = md5.hexdigest()[:8]
    base = (label.replace(".", "_") + "_" + digest) if label else digest
    identifier, i = base, 0
    while identifier in used:
        i += 1
        identifier = "%s_%d" % (base, i)
    used.add(identifier)
    return identifier


class _Parser(object):
    """逐行解析一个游戏目录，收集 dump（say 节点）与 strings 来源。"""

    def __init__(self, gamedir):
        self.gamedir = gamedir
        self.dump = {}          # 标识符 -> entry（引擎 default_translates 语义）
        self._used_ids = set()  # 已分配标识符（冲突后缀判定）
        self.menu_captions = []  # [{file, lineno, caption}]
        self.call_strings = []   # [{file, lineno, text}]  _() 文本

    # ---- 语句收集 ----

    def _add_say(self, fname, lineno, who, what, label):
        code = say_code(who, what)
        identifier = say_identifier(label, code, self._used_ids)
        self.dump[identifier] = {
            "filename": fname,
            "lineno": lineno,
            "nodes": [{"type": "say", "who": who, "what": what}],
        }

    def _scan_menu_body(self, fname, lines, i, end, body_indent, label):
        """菜单条目体内缩进更深的语句：只关心 say（其余行跳过）。"""
        while i < end:
            raw = lines[i].rstrip("\r")
            stripped = raw.strip()
            if not stripped:
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= body_indent:
                break
            got, nxt = self._match_say(fname, lines, i, end, stripped, label)
            i = nxt if got else i + 1
        return i

    def _parse_menu(self, fname, lines, i, end, menu_indent, label):
        """menu 块：收集字幕（与 if 条件无关）；条目体内的 say 照常提取。"""
        captions = []
        i += 1
        while i < end:
            raw = lines[i].rstrip("\r")
            stripped = raw.strip()
            if not stripped:
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= menu_indent:
                break
            m = _MENU_ITEM.match(stripped)
            if m:
                captions.append(unesc(m.group(1)))
                self.menu_captions.append(
                    {"file": fname, "lineno": i + 1, "caption": unesc(m.group(1))})
                i += 1
                i = self._scan_menu_body(fname, lines, i, end, indent, label)
                continue
            i += 1
        return captions, i

    def _read_triple(self, fname, lines, i, end, stripped):
        """三引号对白：返回 (who, what, 结束下标)。"""
        m = _SAY_TQ.match(stripped)
        who = m.group(1) or ""
        body = [m.group(2)]
        j = i
        closed = '"""' in m.group(2)
        while not closed:
            j += 1
            if j >= end:
                raise RuntimeError("三引号字符串未闭合: %s:%d" % (fname, i + 1))
            seg = lines[j].rstrip("\r")
            if '"""' in seg:
                closed = True
                body.append(seg.split('"""')[0])
            else:
                body.append(seg.strip())
        return who, "\n".join(body), j + 1

    def _match_say(self, fname, lines, i, end, stripped, label):
        """单行 say 三形态 + 三引号。命中返回 (True, 下一行下标)。"""
        if '"""' in stripped:
            who, what, nxt = self._read_triple(fname, lines, i, end, stripped)
            self._add_say(fname, i + 1, who, what, label)
            return True, nxt
        m = _SAY_VAR.match(stripped)
        if m:
            self._add_say(fname, i + 1, m.group(1), unesc(m.group(3)), label)
            return True, i + 1
        m = _SAY_STR.match(stripped)
        if m:
            self._add_say(fname, i + 1, m.group(1), unesc(m.group(4)), label)
            return True, i + 1
        m = _SAY_NARR.match(stripped)
        if m:
            self._add_say(fname, i + 1, "", unesc(m.group(2)), label)
            return True, i + 1
        return False, i + 1

    def _parse_block(self, fname, lines, i, end, base_indent, label,
                     explicit_id=None):
        """解析缩进 > base_indent 的语句区，返回结束下标。

        explicit_id 非 None 时处于 translate None 块内：say/menu 并入该标识符。"""
        while i < end:
            raw = lines[i].rstrip("\r")
            stripped = raw.strip()
            if not stripped:
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= base_indent:
                break
            if stripped == "menu:" or stripped.startswith("menu:"):
                captions, i = self._parse_menu(fname, lines, i, end, indent, label)
                if explicit_id is not None:
                    entry = self.dump.setdefault(explicit_id, {
                        "filename": fname, "lineno": i + 1, "nodes": []})
                    entry["nodes"].append({"type": "menu", "captions": captions})
                continue
            got, nxt = self._match_say(fname, lines, i, end, stripped, label)
            i = nxt if got else i + 1
        return i

    # ---- 文件与目录 ----

    def parse_file(self, path):
        fname = os.path.relpath(path, self.gamedir).replace("\\", "/")
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().split("\n")
        n = len(lines)
        i = 0
        label = None
        while i < n:
            raw = lines[i].rstrip("\r")
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            m = _LABEL.match(stripped)
            if m and indent == 0:
                label = m.group(1)
                i = self._parse_block(fname, lines, i + 1, n, 0, label)
                continue
            m = _XL_HDR.match(stripped)
            if m:
                lang, bid = m.group(1), m.group(2)
                j = self._block_end(lines, i + 1, n, indent)
                if lang.lower() == "none":
                    # 作者自带的默认语言翻译块（进 dump，现代引擎不为其生成骨架）
                    self._parse_block(fname, lines, i + 1, j, indent, label,
                                      explicit_id=bid.replace(".", "_"))
                i = j
                continue
            m = _PY_HDR.match(stripped)
            if m:
                j = self._block_end(lines, i + 1, n, indent)
                for k in range(i + 1, j):
                    ln = lines[k].rstrip("\r")
                    for mm in _DUNDER.finditer(ln):
                        self.call_strings.append(
                            {"file": fname, "lineno": k + 1,
                             "text": unesc(mm.group(1))})
                i = j
                continue
            # define/default/$/jump/return/show 等单行语句：引擎不提取
            i += 1

    @staticmethod
    def _block_end(lines, i, end, base_indent):
        """块结束下标：缩进 > base_indent 的连续内容（空行跳过）。"""
        while i < end:
            raw = lines[i].rstrip("\r")
            stripped = raw.strip()
            if stripped and len(raw) - len(raw.lstrip(" ")) <= base_indent:
                break
            i += 1
        return i

    def parse_game(self):
        for root, dirs, files in os.walk(self.gamedir):
            dirs[:] = [d for d in dirs if d not in ("tl", "cache", "saves",
                                                    "fonts_ng_backup")]
            for f in sorted(files):
                if not f.endswith(".rpy") or f.startswith("zz_ng"):
                    continue
                self.parse_file(os.path.join(root, f))
        return self

    # ---- strings 条目（引擎 scanstrings 语义） ----

    def strings_entries(self):
        """菜单字幕（additional_strings）+ _() 文本，全局按 (优先级, 文件, 行号)
        排序去重；优先级 script.rpy=5 其余=100（0–299 区间全部收录）。
        返回 {file: [(lineno, text), ...]}。"""
        entries = [{"file": m["file"], "lineno": m["lineno"], "text": m["caption"]}
                   for m in self.menu_captions]
        entries.extend(self.call_strings)
        for e in entries:
            e["priority"] = 5 if os.path.basename(e["file"]) == "script.rpy" else 100
        entries.sort(key=lambda e: (e["priority"], e["file"], e["lineno"]))
        seen = set()
        byfile = {}
        for e in entries:
            if e["text"] in seen:
                continue
            seen.add(e["text"])
            byfile.setdefault(e["file"], []).append((e["lineno"], e["text"]))
        return byfile


def _existing_state(tl_dir, language):
    """已有 tl 产物：语言块标识符集合 + strings old 集合（引擎增量提取语义）。"""
    ids, olds = set(), set()
    if not os.path.isdir(tl_dir):
        return ids, olds
    hdr = re.compile(r"^translate\s+%s\s+(\S+)\s*:" % re.escape(language))
    old_re = re.compile(r'^\s*old\s+"((?:[^"\\]|\\.)*)"')
    for root, _dirs, files in os.walk(tl_dir):
        for f in sorted(files):
            if not f.endswith(".rpy"):
                continue
            with open(os.path.join(root, f), "r", encoding="utf-8",
                      errors="replace") as fh:
                for ln in fh:
                    line = ln.rstrip("\r\n")
                    m = hdr.match(line)
                    if m:
                        ids.add(m.group(1))
                        continue
                    m = old_re.match(line)
                    if m:
                        olds.add(unesc_rpy(m.group(1)))
    return ids, olds


def _write_skeleton(gamedir, language, game):
    """生成/增量维护 tl/<语言>/ 骨架，与 write_translates/write_strings 逐行对齐。"""
    tl_dir = os.path.join(gamedir, "tl", language)
    known_ids, known_olds = _existing_state(tl_dir, language)

    per_file = {}
    for identifier, entry in game.dump.items():
        if identifier in known_ids or not entry.get("nodes"):
            continue
        per_file.setdefault(entry["filename"], []).append((identifier, entry))

    strings_by_file = game.strings_entries()
    pending_strings = {
        f: [(ln, t) for ln, t in items if t not in known_olds]
        for f, items in strings_by_file.items()}
    pending_strings = {f: v for f, v in pending_strings.items() if v}
    if not per_file and not pending_strings:
        return tl_dir

    for fname in sorted(set(per_file) | set(pending_strings)):
        dst = os.path.join(tl_dir, *fname.split("/"))
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        out = []
        if not os.path.isfile(dst):
            out.append("\ufeff")
            out.append("# TODO: Translation updated at %s\n" % _TODO_STAMP)
            out.append("\n")
        for identifier, entry in per_file.get(fname, []):
            out.append("# %s:%d\n" % (entry["filename"], entry["lineno"]))
            out.append("translate %s %s:\n" % (language, identifier))
            out.append("\n")
            for node in entry["nodes"]:
                if node["type"] == "say":
                    out.append("    # %s\n" % say_code(node.get("who", ""),
                                                       node["what"]))
            for node in entry["nodes"]:
                if node["type"] == "say":
                    out.append("    %s\n" % say_code(node.get("who", ""), ""))
            out.append("\n")
        if pending_strings.get(fname):
            out.append("translate %s strings:\n" % language)
            out.append("\n")
            for lineno, text in pending_strings[fname]:
                out.append("    # %s:%d\n" % (fname, lineno))
                out.append('    old "%s"\n' % quote_unicode(text))
                out.append('    new ""\n')
                out.append("\n")
        with open(dst, "a", encoding="utf-8") as f:
            f.write("".join(out))
    return tl_dir


def extract_for_test(game_base, language, timeout=180, log=print, should_stop=None):
    """extract.extract 的引擎替身：产物格式与真实提取一致，返回值同构。

    - 写 game/ng_extract.json（运行期钩子的 dump，全量重写）；
    - 生成/增量维护 game/tl/<语言>/ 骨架（跳过已有标识符与 strings 条目）；
    - 调用与真实提取相同的 gen_common_tl 生成 common.rpy。"""
    gamedir = os.path.join(game_base, "game")
    game = _Parser(gamedir).parse_game()

    json_path = os.path.join(gamedir, "ng_extract.json")
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(game.dump, ensure_ascii=False))

    tl_dir = _write_skeleton(gamedir, language, game)
    _real_extract.gen_common_tl(game_base, language, log)
    if log:
        log("[syntheng] 模拟引擎提取：%d 个对白标识符 -> %s" % (len(game.dump), tl_dir))
    return len(game.dump), tl_dir, json_path
