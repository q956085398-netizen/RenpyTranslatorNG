# -*- coding: utf-8 -*-
"""ipatch 台词补丁覆盖。

F95 社区流传的 *_ipatch.rpy 是不改 rpyc 的"运行期台词补丁"（关系改写/台词还原），
常见五种机制：
1) 给 config.say_menu_text_filter 赋一个函数，内部一串 text.replace("旧","新")
   （UNIVERSAL_ipatch / Golden_Mean 等）；
2) 猴子补丁 renpy.translation.StringTranslator.translate，同样一串 replace
   （Double_Perception / Love_and_Tem / StrongDesire 等）；
3) define config.language = "ipatch" 伪语言 + translate ipatch <标识符>: 整句重写、
   translate ipatch strings: old/new 对（AnotherChance）——该机制在本工具的中文
   语言环境下不生效，但其内容代表补丁作者的最终文本，正好拿来当翻译源；
4) config.label_overrides 重定向 + $ 变量改写（Crossworlds 的 Landlady→Mother）；
5) 猴子补丁 renpy.exports.say + 整句字典 {"原文": "补丁后"}（Reclaiming the Lost
   的关系补丁一类）：字典按**整句精确匹配**，条目之间大量互为子串，只能精确查表，
   绝不能并进 replace 链。

本模块把上述内容解析成 有序替换对 / 节点整句覆盖 / 变量值覆盖 / 整句映射，在构建
翻译任务时把"翻译源"换成打完补丁的文本再送 AI，译文仍按原文写回 tl：
- 对白/菜单块的运行期查表按节点标识符进行、与文本内容无关，所以 tl 里写打补丁
  之后的句子不会破坏查表；
- strings 块按 old 文本查表，因此 tl 里的 old 一律保持原文，补丁只影响翻译源。
原 dump 与 tl 骨架一律不动。补丁文件自身必须从一切文本提取扫描中排除。
"""
import ast
import hashlib
import json
import os
import re

from .util import read_json, unesc_rpy, work_dir, write_json

# .replace("旧", "新")：双/单引号 Python 字面量各一组（单行形态）
_PAIR = re.compile(
    r'\.\s*replace\s*\(\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
    r'\s*,\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')\s*\)')
# translate <伪语言> <标识符>:
_XL_HDR = re.compile(r'^translate\s+(\S+)\s+(\S+)\s*:\s*$')
_XL_OLD = re.compile(r'^\s*old\s+(".*"|e?u?\'.*\')\s*$')
_XL_NEW = re.compile(r'^\s*new\s+(".*"|e?u?\'.*\')\s*$')
# 行内最后一个双引号字符串（伪语言块里的 say 语句取台词）
_QSTR = re.compile(r'"((?:[^"\\]|\\.)*)"')
# 变量改写：$ Var = "值" / default Var = "值" / X.default_name = u"值"
_VAR_SET = re.compile(r'^\s*(?:\$|default)\s+([A-Za-z_]\w*)\s*=\s*'
                      r'(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')\s*$')
_DEFAULT_NAME = re.compile(
    r'^\s*([A-Za-z_]\w*)\.default_name\s*=\s*'
    r'(?:[uUbBrR]?)("((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')\s*$')
# 补丁里的输入提示词：renpy.input("...") / old_input_function("...") 等
# （StrongDesire 一类补丁整个换掉 renpy.input，提示词只存在于补丁文件里）
_INPUT_CALL = re.compile(
    r'\b[A-Za-z_][\w.]*[iI]nput[\w.]*\(\s*("(?:[^"\\]|\\.)*")')
# 输入默认值：return rv.strip() or "Sister"（运行期存进变量、经插值显示）
_INPUT_DEFAULT = re.compile(r'\.strip\(\)\s+or\s+("(?:[^"\\]|\\.)*")')
# 字典型补丁：replacements = { "原文": "补丁后", ... }（给 renpy.exports.say 打猴子补丁）
_DICT_HEAD = re.compile(r'^[ \t]*([A-Za-z_]\w*)[ \t]*=[ \t]*\{', re.M)
# 台词钩子特征：未按 ipatch 命名的补丁（社区关系补丁常叫 patch.rpy）靠这个识别
_PATCH_SIG = re.compile(
    r'renpy\.exports\.say\s*=|say_menu_text_filter\s*=|'
    r'renpy\.translation\.StringTranslator|\.default_name\s*=|'
    r'\.replace\s*\(\s*["\']')


def looks_like_patch(path):
    """文件名不含 ipatch 时，按内容特征判断是否台词补丁。"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return bool(_PATCH_SIG.search(f.read()))
    except OSError:
        return False


def is_patch_file(path):
    """该文件是否算台词补丁——补丁文件必须排除在一切文本提取扫描之外。"""
    name = os.path.basename(path).lower()
    if name.startswith("zz_ng"):
        return False
    if name.endswith(".rpy"):
        if "ipatch" in name:
            return True
        return "patch" in name and looks_like_patch(path)
    return "ipatch" in name


def discover(game_base):
    """找出游戏目录里的补丁脚本：文件名含 ipatch，或名为 *patch* 且带台词钩子特征。"""
    gamedir = os.path.join(game_base, "game")
    found, missing_src = [], []
    if not os.path.isdir(gamedir):
        return found, missing_src
    for root, dirs, files in os.walk(gamedir):
        dirs[:] = [d for d in dirs if d not in ("tl", "saves", "cache", "fonts_ng_backup")]
        for f in sorted(files):
            low = f.lower()
            if f.startswith("zz_ng"):
                continue
            p = os.path.join(root, f)
            if f.endswith(".rpy"):
                if is_patch_file(p):
                    found.append(p)
            elif f.endswith(".rpyc") and "ipatch" in low and not os.path.isfile(p[:-1]):
                missing_src.append(p)
    return found, missing_src


def _py_literal(tok):
    """Python 字符串字面量（含引号）-> 值；失败返回 None。"""
    try:
        v = ast.literal_eval(tok)
        return v if isinstance(v, str) else None
    except Exception:
        return None


def _scan_dict_literal(text, start):
    """从 text[start]（必须指向 '{'）扫出配平的字典字面量，返回字面量文本或 None。
    字符串内部的 {}（文本标签 {i}{/i} 等）与 # 注释不参与配平。"""
    depth = 0
    quote = None
    i = start
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            j = text.find("\n", i)
            i = len(text) - 1 if j < 0 else j
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def _scan_text_map(src):
    """提取"整句精确映射"字典：{"原文": "补丁后", ...}（say 猴子补丁型关系补丁）。

    这类补丁运行期是 `if what in replacements` 的整句精确查表，翻译源覆盖必须
    用同样的精确匹配语义——字典条目之间大量互为子串（"Chloe?" 是
    "Is everything okay, Chloe?" 的子串），绝不能并进 replace 链。"""
    out = {}
    for m in _DICT_HEAD.finditer(src):
        name = m.group(1)
        # 只认真的被当查表用的字典：出现 in <name> / <name>[ / <name>.get(
        if not (re.search(r'\bin\s+%s\b' % re.escape(name), src)
                or re.search(r'\b%s\s*\[|%s\.get\s*\(' % (re.escape(name), re.escape(name)), src)):
            continue
        lit = _scan_dict_literal(src, src.index("{", m.start()))
        if not lit:
            continue
        try:
            d = ast.literal_eval(lit)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(k, str) and isinstance(v, str) and k and k != v:
                out.setdefault(k, v)
    return out


def _parse_file(path):
    """解析单个补丁文件，返回 (pairs, nodes, vars, who, extra, text_map)。

    pairs: 有序 [(旧, 新)]——replace 链按出现顺序合并（运行期就是逐条顺序替换）；
    nodes: {块标识符: 台词}——translate 伪语言块整句重写（只取单 say 行的块）；
    vars:  {变量: 值}；who: {说话人变量: 显示名}（X.default_name）；
    extra: 补丁自己引入的玩家可见文本（输入提示词、输入默认值），需要收进
           strings 表翻译——它们只在补丁的运行期钩子里出现，游戏源码里没有；
    text_map: {原文: 补丁后}——整句精确映射字典（say 猴子补丁型关系补丁），
           应用时按精确匹配，不参与 replace 链。"""
    pairs, nodes, vars_, who, extra, text_map = [], {}, {}, {}, [], {}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return pairs, nodes, vars_, who, extra, text_map
    lines = text.split("\n")
    text_map = _scan_text_map(text)

    block_lang = None   # 当前 translate 块的伪语言名（None=不在块内）
    block_id = None
    block_says = []     # 块内疑似 say 行（取最后一个字符串为台词）
    pending_old = None  # strings 块里待配对的 old

    def flush_block():
        nonlocal block_says, pending_old
        if block_lang and block_id and block_id != "strings" and len(block_says) == 1:
            text = block_says[0]
            if text and text not in nodes:
                nodes[block_id] = text
        block_says = []
        pending_old = None

    for ln in lines:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _XL_HDR.match(ln)
        if m:
            flush_block()
            block_lang, block_id = m.group(1), m.group(2)
            # 常见真语言名（chinese/english/...）是正经翻译文件，不属于补丁
            if block_lang.lower() in ("chinese", "english", "schinese", "tchinese",
                                      "japanese", "korean", "russian", "spanish",
                                      "french", "german", "portuguese", "italian",
                                      "polish", "brazilian", "ukrainian", "turkish"):
                block_lang = None
            continue
        if block_id:
            if _XL_HDR.match(ln) or (stripped and not ln[0].isspace() and not stripped.startswith(("translate",))):
                flush_block()
                block_lang = None
                block_id = None
            elif _XL_OLD.match(ln):
                # strings 块：old/new 对（renpy 转义）
                om = _XL_OLD.match(ln)
                v = om.group(1)
                pending_old = unesc_rpy(v[1:-1]) if v[:1] == '"' else None
                continue
            elif _XL_NEW.match(ln) and pending_old is not None:
                nm = _XL_NEW.match(ln)
                v = nm.group(1)
                new = unesc_rpy(v[1:-1]) if v[:1] == '"' else None
                if pending_old and new and new != pending_old:
                    pairs.append((pending_old, new))
                pending_old = None
                continue
            elif _QSTR.search(ln):
                # 伪语言块里的 say 语句：取行内最后一个字符串为台词（renpy 反转义）
                block_says.append(unesc_rpy(_QSTR.findall(ln)[-1]))
                continue
        if block_id:
            continue
        for m in _PAIR.finditer(ln):
            old, new = _py_literal(m.group(1)), _py_literal(m.group(2))
            if old and new is not None and old != new:
                pairs.append((old, new))
        for m in (_INPUT_CALL, _INPUT_DEFAULT):
            for q in m.finditer(ln):
                val = unesc_rpy(q.group(1)[1:-1])
                if val and val not in extra:
                    extra.append(val)
        m = _VAR_SET.match(ln)
        if m:
            val = m.group(2) if m.group(2) is not None else m.group(3)
            if val and m.group(1) not in vars_:
                vars_[m.group(1)] = val
            continue
        m = _DEFAULT_NAME.match(ln)
        if m:
            val = m.group(3) if m.group(3) is not None else m.group(4)
            name = re.sub(r"^Character_", "", m.group(1), flags=re.I).lower()
            if val and name and name not in who:
                who[name] = val
    flush_block()
    return pairs, nodes, vars_, who, extra, text_map


class Overlay:
    """一个游戏全部 ipatch 补丁的合并覆盖规则。"""

    def __init__(self, files, pairs, nodes, vars_, who, extra=None, text_map=None,
                 skip_keys=None):
        self.files = files
        self.skip_keys = {str(k) for k in (skip_keys or []) if k}
        self.pairs = [(o, n) for o, n in pairs if o and o != n]
        self.nodes = nodes
        self.vars = vars_
        self.who = who
        self.extra = [s for s in (extra or []) if s]
        # 整句精确映射（say 猴子补丁型的字典补丁），与 replace 链分开：
        # 字典里的短句大量是别的长句的子串，只能精确匹配
        self.text_map = {k: v for k, v in (text_map or {}).items() if k and v and k != v}
        # 注意：跳过名单**不**参与指纹——它只影响被排除的那几条（由调用方负责
        # 作废这几条的译文缓存），不该让全部补丁条目的缓存一起失效重译。
        blob = json.dumps([files, self.pairs, nodes, vars_, who, self.extra, self.text_map],
                          ensure_ascii=False, sort_keys=True)
        self.sig = hashlib.md5(blob.encode("utf-8")).hexdigest()

    @property
    def empty(self):
        return not (self.pairs or self.nodes or self.vars or self.who or self.extra
                    or self.text_map)

    def skipped(self, key):
        """该任务 key / 块标识符是否被人工排除在补丁之外。"""
        return bool(self.skip_keys) and (key in self.skip_keys
                                         or key.split(":")[0] in self.skip_keys)

    def apply(self, text, say=False):
        """按补丁语义改写文本。

        say=True 时先做精确字典查表（等价运行期 `if what in replacements`）；
        replace 链对代词性文本一律生效（补丁的文本过滤器同时作用于菜单项）。
        """
        if not isinstance(text, str):
            return text
        if say and self.text_map:
            hit = self.text_map.get(text)
            if hit is not None:
                return hit
        if not self.pairs:
            return text
        for old, new in self.pairs:
            if old in text:
                text = text.replace(old, new)
        return text

    def node_text(self, identifier):
        return self.nodes.get(identifier)

    def apply_dump(self, dump):
        """返回打了补丁的 dump 副本（关系扫描取样用；原 dump 不动）。"""
        if self.empty:
            return dump
        out = {}
        for ident, e in dump.items():
            nodes = []
            skip = self.skipped(ident)
            for i, node in enumerate(e.get("nodes", [])):
                n = dict(node)
                if skip:
                    nodes.append(n)
                    continue
                if i == 0 and ident in self.nodes and n.get("type") == "say":
                    # 伪语言块对该节点整句重写：取样文本换成补丁后的台词
                    n["what"] = self.apply(self.nodes[ident], say=True)
                elif n.get("what"):
                    n["what"] = self.apply(n["what"], say=True)
                if n.get("captions"):
                    n["captions"] = [self.apply(c) if c else c for c in n["captions"]]
                nodes.append(n)
            e2 = dict(e)
            e2["nodes"] = nodes
            out[ident] = e2
        return out

    def summary(self):
        parts = []
        if self.text_map:
            parts.append("%d 条整句映射" % len(self.text_map))
        if self.pairs:
            parts.append("%d 条文本替换" % len(self.pairs))
        if self.nodes:
            parts.append("%d 句整句改写" % len(self.nodes))
        if self.vars:
            parts.append("%d 个变量改写" % len(self.vars))
        if self.who:
            parts.append("%d 个说话人改名" % len(self.who))
        if self.extra:
            parts.append("%d 条补丁自有文本" % len(self.extra))
        if self.skip_keys:
            parts.append("人工排除 %d 条" % len(self.skip_keys))
        return "、".join(parts)


def _load_skip(game_base):
    """可选跳过名单：work/<游戏>/ipatch_skip.json 里列出的 key / 块标识符不套用补丁。

    字典型补丁按整句文本匹配、与说话人无关：补丁作者把 "[mname]…" 映射成 "son…"，
    那么女友、路人说同一句时也会被改成「儿子」。这类误伤在这里人工排除，被排除的
    条目按游戏原文翻译。文件格式：["块标识符", "块标识符:s1", ...] 或 {"keys": [...]}。"""
    data = read_json(os.path.join(work_dir(game_base), "ipatch_skip.json"), []) or []
    if isinstance(data, dict):
        data = data.get("keys", [])
    return {str(k) for k in data if k}


def build_overlay(game_base, log=print, save=False):
    """扫描并解析补丁；没有补丁返回 None，解析异常不抛出。

    save=True 时把解析结果落盘到工作目录（提取阶段用；任务构建阶段频繁调用
    不落盘）。"""
    try:
        files, missing_src = discover(game_base)
        if missing_src and log:
            log("发现 ipatch 补丁但尚未反编译（请先执行第 2 步）: %s"
                % ", ".join(os.path.basename(p) for p in missing_src))
        pairs, nodes, vars_, who = [], {}, {}, {}
        extra = []
        text_map = {}
        for p in files:
            pp, nn, vv, ww, ee, tm = _parse_file(p)
            pairs.extend(pp)
            for k, v in nn.items():
                nodes.setdefault(k, v)
            for k, v in vv.items():
                vars_.setdefault(k, v)
            for k, v in ww.items():
                who.setdefault(k, v)
            for s in ee:
                if s not in extra:
                    extra.append(s)
            for k, v in tm.items():
                text_map.setdefault(k, v)
        if not files:
            return None
        ov = Overlay([os.path.basename(p) for p in files], pairs, nodes, vars_, who,
                     extra, text_map, _load_skip(game_base))
        if ov.empty:
            return None
        if log:
            log("检测到台词补丁（%s）：%s"
                % (", ".join(ov.files), ov.summary()))
        # 落盘一份供排查/查看（不影响任何流程）
        if save:
            try:
                write_json(os.path.join(work_dir(game_base), "ipatch.json"),
                           {"sig": ov.sig, "files": ov.files, "pairs": ov.pairs,
                            "nodes": ov.nodes, "vars": ov.vars, "who": ov.who,
                            "extra": ov.extra, "text_map": ov.text_map,
                            "skip_keys": sorted(ov.skip_keys)})
            except Exception:
                pass
        return ov
    except Exception:
        if log:
            log("补丁解析失败（忽略，按未打补丁的原文翻译）:\n%s"
                % _fmt_exc())
        return None


def overlay_jobs(jobs, ov):
    """把补丁覆盖注入翻译任务：改写 j["old"] 为补丁后文本，原文存进 j["orig"]。

    只有真正被补丁改动的任务才带 orig；ctx 一并替换，让 AI 看到的上下文与
    玩家看到的补丁后文本一致。返回被改动的任务数。"""
    n = 0
    for j in jobs:
        kind = j.get("kind")
        if ov.skipped(j["key"]):
            continue
        src = None
        if kind == "say" and ":" not in j["key"]:
            src = ov.nodes.get(j["key"])
        patched = ov.apply(src if src is not None else j["old"], say=(kind == "say"))
        if patched != j["old"]:
            j["orig"] = j["old"]
            j["old"] = patched
            n += 1
        ctx = j.get("ctx")
        if ctx and (ctx[0] or ctx[1]):
            pre = [ov.apply(x, say=True) for x in ctx[0]]
            nxt = [ov.apply(x, say=True) for x in ctx[1]]
            if pre != ctx[0] or nxt != ctx[1]:
                j["ctx"] = (pre, nxt)
    return n


def drop_stale_cache(jobs, ov, trans_path, log=print):
    """补丁内容变化后使受影响的译文缓存失效。

    缓存按任务 key 存译文，key 不含文本内容；补丁改动会改变翻译源文本，
    因此补丁指纹变化时，所有"源文本被补丁改过"（orig != old）的任务的缓存
    全部作废重译。源文本未被补丁改过的任务不受影响，照常断点续翻。
    返回是否发生了丢弃。"""
    if ov is None:
        # 补丁文件被移除：之前按补丁文本翻译的缓存无法逐条定位，只能提示
        sig_path = os.path.join(work_dir_from(trans_path), "ipatch_sig")
        if os.path.isfile(sig_path):
            log("注意：游戏的 ipatch 补丁已被移除。之前按补丁后文本翻译的缓存"
                "不会自动失效，如需完全按未打补丁的原文重译，请清空译文缓存后重翻")
        return False
    sig_path = os.path.join(work_dir_from(trans_path), "ipatch_sig")
    prev = None
    if os.path.isfile(sig_path):
        try:
            with open(sig_path, "r", encoding="utf-8") as f:
                prev = f.read().strip()
        except OSError:
            prev = None
    if prev == ov.sig:
        return False
    stale = {j["key"] for j in jobs if j.get("orig") and j["orig"] != j["old"]}
    if stale:
        trans = read_json(trans_path, {}) or {}
        dropped = [k for k in stale if k in trans]
        for k in dropped:
            del trans[k]
        if dropped:
            write_json(trans_path, trans)
            log("ipatch 补丁状态变化：%d 条相关译文缓存已作废，将按补丁后文本重译"
                % len(dropped))
    try:
        os.makedirs(os.path.dirname(sig_path) or ".", exist_ok=True)
        with open(sig_path, "w", encoding="utf-8") as f:
            f.write(ov.sig)
    except OSError:
        pass
    return True


def work_dir_from(trans_path):
    return os.path.dirname(os.path.abspath(trans_path))


def _fmt_exc():
    import traceback
    return traceback.format_exc()
