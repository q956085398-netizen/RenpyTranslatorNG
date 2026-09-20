# -*- coding: utf-8 -*-
"""根据提取结果构建翻译任务清单，并把译文回填进 tl 文件。"""
import os
import re

from .util import esc_rpy, unesc_rpy

_HDR = re.compile(r'^translate (\S+) (\S+):\s*$')
_CAPTION = re.compile(r'^(\s*)"((?:[^"\\]|\\.)*)"\s*:\s*$')
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _iter_blocks(lines):
    """yield (header_line_idx, header, [body_idx...])  header 为 ('strings', '-') 或 (lang, id)。"""
    i, n = 0, len(lines)
    while i < n:
        m = _HDR.match(lines[i])
        if m:
            lang, bid = m.group(1), m.group(2)
            j = i + 1
            body = []
            while j < n:
                if _HDR.match(lines[j]):
                    break
                body.append(j)
                j += 1
            yield (lang, bid), body
            i = j
        else:
            i += 1


def _live_body(lines, body):
    return [k for k in body if lines[k].strip() and not lines[k].strip().startswith("#")]


class TlFile:
    def __init__(self, path):
        self.path = path
        with open(path, "r", encoding="utf-8") as f:
            self.lines = f.read().split("\n")

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines))

    def _replace_first_string(self, idx, text):
        m = _STR.search(self.lines[idx])
        if not m:
            return False
        self.lines[idx] = self.lines[idx][:m.start()] + '"' + esc_rpy(text) + '"' + self.lines[idx][m.end():]
        return True

    def _replace_nth_string(self, idx, n, text):
        """替换该行第 n 个（0 起）双引号字符串。"""
        matches = list(_STR.finditer(self.lines[idx]))
        if n >= len(matches):
            return False
        m = matches[n]
        self.lines[idx] = self.lines[idx][:m.start()] + '"' + esc_rpy(text) + '"' + self.lines[idx][m.end():]
        return True


def build_jobs(game_base, language, dump, include_strings=True, context_lines=2):
    """返回 (jobs, tl_files)。jobs 为待翻译条目列表。"""
    gamedir = os.path.join(game_base, "game")
    tl_dir = os.path.join(gamedir, "tl", language)
    if not os.path.isdir(tl_dir):
        raise RuntimeError("tl 目录不存在: %s（请先执行提取）" % tl_dir)

    # 对白上下文：按源文件+行号排序
    seq = {}
    for ident, e in dump.items():
        for node in e.get("nodes", []):
            if node["type"] == "say":
                seq.setdefault(e["filename"], []).append((e["lineno"], node["what"]))
    ctx_map = {}
    for fn, items in seq.items():
        items.sort()
        whats = [w for _, w in items]
        for i, (ln, w) in enumerate(items):
            pre = [x for x in whats[max(0, i - context_lines):i]]
            nxt = [x for x in whats[i + 1:i + 1 + context_lines]]
            ctx_map[(fn, ln)] = (pre, nxt)

    jobs = []
    tl_files = []
    for root, _dirs, files in os.walk(tl_dir):
        for fname in sorted(files):
            if not fname.endswith(".rpy"):
                continue
            path = os.path.join(root, fname)
            tf = TlFile(path)
            rel = os.path.relpath(path, tl_dir)
            had_content = False
            for (lang, bid), body in _iter_blocks(tf.lines):
                live = _live_body(tf.lines, body)
                if bid == "strings":
                    if not include_strings:
                        continue
                    k = 0
                    while k < len(live) - 1:
                        if tf.lines[live[k]].strip().startswith("old "):
                            old = unesc_rpy(_STR.search(tf.lines[live[k]]).group(1))
                            jobs.append({"key": "S:" + old, "kind": "string", "file": rel,
                                         "old": old, "who": "", "ctx": ([], [])})
                            had_content = True
                            k += 2
                        else:
                            k += 1
                    continue
                entry = dump.get(bid)
                if not entry:
                    continue
                had_content = True
                say_i = 0
                for node in entry.get("nodes", []):
                    if node["type"] == "say":
                        # 找到本块内下一个含引号的活动行
                        for k in live:
                            if _STR.search(tf.lines[k]):
                                old = node["what"]
                                pre, nxt = ctx_map.get((entry["filename"], entry["lineno"]), ([], []))
                                jobs.append({"key": ("%s:s%d" % (bid, say_i)) if say_i else bid,
                                             "kind": "say", "file": rel, "old": old,
                                             "who": node.get("who", ""), "ctx": (pre, nxt),
                                             "src_file": entry["filename"],
                                             "src_line": entry["lineno"]})
                                say_i += 1
                                live = live[live.index(k) + 1:]
                                break
                        continue
                    # 菜单节点不生成任务：现代引擎（7.4–8.x）里菜单字幕经 strings
                    # 机制翻译（dump 菜单节点只是 translate None 块的附属信息），
                    # 字幕的出现位置标识就是 strings 任务 key（"S:<原文>"）。
                    # 旧实现按节点重计序号（bid:c0/c1...），同一块中的多个菜单节点
                    # 序号互相冲突，且该形态在真实引擎下不可达、回填从不处理。
            if had_content:
                tl_files.append(path)
    return jobs, tl_files


def scan_translations(path):
    """读出一份 tl 文件当前实际生效的译文：{出现位置标识: {"text", "anchor"}}。

    与 fill_translations 严格同一套块结构与位置配对规则（同一注释/代码行配对、
    同一说话人名字判定），保证"工具写入的"与"检测读到的"按同一规则对齐——
    外部 tl 变更检测（core.externaltl，工单 08）据此与应用基线对账。
    anchor 是注释行里的原文锚点（strings 块为 old 文本），用于识别外部结构
    改动造成的配对错位；strings 块未译的空 new 行 text 为空串。
    """
    tf = TlFile(path)
    out = {}
    for (lang, bid), body in _iter_blocks(tf.lines):
        lives = _live_body(tf.lines, body)
        if bid == "strings":
            pending = None
            for k in lives:
                s = tf.lines[k].strip()
                if s.startswith("old "):
                    m = _STR.search(s)
                    pending = unesc_rpy(m.group(1)) if m else None
                elif s.startswith("new ") and pending is not None:
                    m = _STR.search(s)
                    out["S:" + pending] = {"text": unesc_rpy(m.group(1)) if m else "",
                                           "anchor": pending}
                    pending = None
            continue
        comments = [tf.lines[k].strip() for k in body
                    if tf.lines[k].strip().startswith("#")]
        for i, (c, k) in enumerate(zip(comments, lives)):
            cs, ks = _STR.findall(c), _STR.findall(tf.lines[k])
            if not cs:
                continue
            # 字符串字面量说话人（"Guard 1" "台词"）：注释与代码行的第 1 个串
            # 都是说话人名字时，锚点与译文都取第 2 个串（与回填一致）
            n = 1 if (len(cs) > 1 and len(ks) > 1 and unesc_rpy(cs[0]) == unesc_rpy(ks[0])) else 0
            if n >= len(cs):
                n = 0
            key = bid if i == 0 else "%s:s%d" % (bid, i)
            out[key] = {"text": unesc_rpy(ks[n]) if n < len(ks) else None,
                        "anchor": unesc_rpy(cs[n])}
    return out


def fill_translations(tl_files, text_map, key_map=None):
    """回填 tl 文件，返回替换条数。

    对白块：write_translates 生成结构为 [全部注释行][全部代码行]，一一对应；
    注释行里取第一个字符串作为原文锚点（who 不带引号，取到的即 what/菜单项）。
    strings 块：old/new 成对，按 old 文本查找。
    找不到译文时回填原文，保证未翻译内容显示英文而不是空白（避免菜单项消失）。

    key_map（任务 key -> 译文）优先：同一原文在不同分支可能有各自的既有译法，
    被人工排除补丁的条目也要保留自己的译文；只有 key 查不到时才退回按原文复用。
    """
    replaced = 0
    key_map = key_map or {}
    for path in tl_files:
        tf = TlFile(path)
        for (lang, bid), body in _iter_blocks(tf.lines):
            comments = [tf.lines[k].strip() for k in body if tf.lines[k].strip().startswith("#")]
            lives = [k for k in body if tf.lines[k].strip() and not tf.lines[k].strip().startswith("#")]
            if bid == "strings":
                pending = None
                for k in lives:
                    s = tf.lines[k].strip()
                    if s.startswith("old "):
                        m = _STR.search(s)
                        pending = unesc_rpy(m.group(1)) if m else None
                    elif s.startswith("new ") and pending is not None:
                        text = (key_map.get("S:" + pending)
                                or text_map.get(pending) or pending)
                        if tf._replace_first_string(k, text):
                            replaced += 1
                        pending = None
                continue
            for i, (c, k) in enumerate(zip(comments, lives)):
                cs, ks = _STR.findall(c), _STR.findall(tf.lines[k])
                if not cs:
                    continue
                # 字符串字面量说话人（"Guard 1" "台词"）：注释与代码行的第 1 个串
                # 都是说话人名字时，真正要替换的是第 2 个串（台词内容）
                n = 1 if (len(cs) > 1 and len(ks) > 1 and unesc_rpy(cs[0]) == unesc_rpy(ks[0])) else 0
                if n >= len(cs):
                    n = 0
                orig = unesc_rpy(cs[n])
                key = bid if i == 0 else "%s:s%d" % (bid, i)
                text = key_map.get(key) or text_map.get(orig) or orig
                if text and tf._replace_nth_string(k, n, text):
                    replaced += 1
        tf.save()
    return replaced
