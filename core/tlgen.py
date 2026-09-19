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
                say_i = cap_i = 0
                for node in entry.get("nodes", []):
                    if node["type"] == "say":
                        # 找到本块内下一个含引号的活动行
                        for k in live:
                            if _STR.search(tf.lines[k]):
                                old = node["what"]
                                pre, nxt = ctx_map.get((entry["filename"], entry["lineno"]), ([], []))
                                jobs.append({"key": ("%s:s%d" % (bid, say_i)) if say_i else bid,
                                             "kind": "say", "file": rel, "old": old,
                                             "who": node.get("who", ""), "ctx": (pre, nxt)})
                                say_i += 1
                                live = live[live.index(k) + 1:]
                                break
                        continue
                    if node["type"] == "menu":
                        for ci, cap in enumerate(node.get("captions", [])):
                            if not cap:
                                continue
                            jobs.append({"key": "%s:c%d" % (bid, ci), "kind": "caption", "file": rel,
                                         "old": cap, "who": "", "ctx": ([], [])})
            if had_content:
                tl_files.append(path)
    return jobs, tl_files


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
