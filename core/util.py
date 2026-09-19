# -*- coding: utf-8 -*-
"""通用工具函数。"""
import json
import os
import re
import sys
import threading
import time
import traceback

APP_NAME = "RenpyTranslatorNG"


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def work_dir(game_base):
    """每个游戏的工作目录（存放提取结果、进度、词表等）。"""
    name = os.path.basename(os.path.normpath(game_base)) or "game"
    d = os.path.join(app_dir(), "work", re.sub(r'[\\/:*?"<>|]', "_", name))
    os.makedirs(d, exist_ok=True)
    return d


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def log_append(path, msg):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def esc_rpy(s):
    """把普通文本转成 renpy 脚本双引号字符串内容（不含两侧引号）。"""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")


def unesc_rpy(s):
    """renpy 脚本双引号字符串内容 -> 普通文本。"""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_RPY_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def parse_rpy_string_line(line):
    """从一行 rpy 代码中取出第一个双引号字符串（未转义后的文本），失败返回 None。"""
    m = _RPY_STR.search(line)
    if not m:
        return None
    return unesc_rpy(m.group(1))


_TAG_TPL = re.compile(r"\{[^{}]*\}")
_INTERP = re.compile(r"\[[^\[\]]*\]")
_WORD2 = re.compile(r"[A-Za-z]{2,}")
_SNAKE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
_CALLPAR = re.compile(r"[A-Za-z0-9_]\(")
_CONST = re.compile(r"^[A-Z][A-Z0-9_]*$")
_EXT = re.compile(r"\.(rpy|rpyc|rpym|png|jpg|jpeg|webp|webm|mp4|ogg|opus|mp3|wav|"
                  r"ttf|otf|gif|bmp|zip|txt|json|csv|webm|avi|mkv)$", re.I)
_CODE_WORDS = {"true", "false", "none", "null", "self", "default", "return"}


def is_display_text(s):
    """启发式判断 Python 字符串字面量是否为玩家可读的显示文本。
    用于补提取与显示端包装，宁缺毋滥：拿不准的一律当非文本（包装后查不到译文也
    只是原样显示，无副作用，但提取出来会白花翻译费）。"""
    if not s:
        return False
    core = _INTERP.sub("", _TAG_TPL.sub("", s))
    if not _WORD2.search(core):
        return False
    for p in ("==", ";", "://", "\\", "`"):
        if p in core:
            return False
    if _EXT.search(s):
        return False
    stripped = core.strip()
    if not stripped:
        return False
    if stripped.lower() in _CODE_WORDS:
        return False
    if "=" in stripped or "$" in stripped or "~" in stripped or "^" in stripped:
        return False
    # 纯词组+纯字母括注是可读按钮形态（Accept(Enter)），不是代码调用
    if re.match(r"^[A-Za-z][A-Za-z ]*\([A-Za-z]+\)$", stripped):
        return True
    # 蛇形命名 / 函数调用形态：标签名、条件表达式等代码内容
    if _SNAKE.search(stripped) or _CALLPAR.search(stripped):
        return False
    if _CONST.match(stripped) and "_" in stripped:
        return False
    if " " in stripped:
        return True
    # 无空格：仅接受首字母大写的单词或单词+标点（Boudoir / Yes / Author:）
    return bool(re.match(r"^[A-Z][a-z]{2,}[\s:：,.;:！!？?]*$", stripped))


def fmt_exc():
    return traceback.format_exc()
