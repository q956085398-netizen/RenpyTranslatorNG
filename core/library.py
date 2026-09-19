# -*- coding: utf-8 -*-
"""游戏库：发现本机 Ren'Py 游戏、判定汉化状态、管理封面与启动器。

判定规则：游戏根目录含 game/ 子目录，且（根目录有 exe/py 启动器，
或有 renpy//lib/ 引擎目录，或 game/ 里有 .rpa/.rpyc 脚本）。
汉化状态：game/tl/ 下存在名字含 Chinese/中文 的语言目录即视为已翻译。
"""
import hashlib
import os
import re
import shutil

from .util import app_dir, read_json, write_json

LIB_PATH = os.path.join(app_dir(), "library.json")
COVER_DIR = os.path.join(app_dir(), "covers")

# 全盘扫描时跳过的系统/噪音目录（大小写不敏感）
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "windows.old",
    "appdata", "programdata", "common files", "perflogs",
    "intel", "amd", "nvidia", "drivers", "driver", "temp", "tmp",
    "node_modules", "__pycache__", ".git", ".svn",
}
# 已识别为游戏后不再深入的最高频目录（省时间；游戏内再嵌套游戏几乎不存在）
GAME_SUBDIRS = {"game", "renpy", "lib"}

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico")
COVER_NAMES = ("cover", "poster", "banner", "title", "icon", "logo")


def looks_like_game(path):
    """目录是否像一个 Ren'Py 游戏根目录。"""
    game_dir = os.path.join(path, "game")
    if not os.path.isdir(game_dir):
        return False
    try:
        root_names = os.listdir(path)
        game_names = os.listdir(game_dir)
    except OSError:
        return False
    lows = {n.lower() for n in root_names}
    has_launcher = any(n.lower().endswith((".exe", ".py")) for n in root_names)
    has_engine = "renpy" in lows or "lib" in lows
    has_script = any(n.lower().endswith((".rpa", ".rpyc", ".rpy")) for n in game_names)
    return has_launcher or has_engine or has_script


def detect_translated(path, language="chinese"):
    """game/tl/ 下是否存在中文语言目录（Chinese/中文/简中/繁中/汉化）。"""
    tl = os.path.join(path, "game", "tl")
    try:
        names = os.listdir(tl)
    except OSError:
        return False
    zh_keys = ("chinese", "中文", "简中", "繁中", "汉化")
    lang = (language or "").strip().lower()
    for n in names:
        if not os.path.isdir(os.path.join(tl, n)):
            continue
        low = n.lower()
        if (lang and low == lang) or any(k in low for k in zh_keys):
            return True
    return False


def find_game_exe(root):
    """挑选游戏主程序：优先与根目录 .py 同名的 exe，排除 -32 后缀，再按体积取大者。
    返回 (exe完整路径, 候选exe总数)；没有 exe 时返回 (None, 0)。"""
    skip = ("unins", "crash", "dotnet", "redist", "dxsetup", "vcredist")
    try:
        files = os.listdir(root)
    except OSError:
        return None, 0
    exes = [f for f in files
            if f.lower().endswith(".exe") and not any(s in f.lower() for s in skip)]
    if not exes:
        return None, 0
    pys = {os.path.splitext(f)[0] for f in files if f.lower().endswith(".py")}

    def rank(f):
        stem = f[:-4]
        return (0 if stem in pys else 1,
                0 if not stem.lower().endswith(("-32", "-32bit")) else 1,
                -os.path.getsize(os.path.join(root, f)))

    exes.sort(key=rank)
    return os.path.join(root, exes[0]), len(exes)


def derive_name(path):
    """游戏显示名：优先取主程序名（去掉 -32 之类后缀），否则用目录名。"""
    exe, _ = find_game_exe(path)
    stem = os.path.splitext(os.path.basename(exe))[0] if exe else ""
    stem = re.sub(r"(-32bit|-32|_32|win|_x64)$", "", stem, flags=re.I)
    return stem or os.path.basename(os.path.normpath(path))


def _cover_score(stem):
    """封面候选打分：整名命中 COVER_NAMES 得 0-5，以封面词开头/结尾
    （cover1、game_cover 这类变体）得 10+；favicon 之类排除。分小者优先。"""
    if "favicon" in stem:
        return None
    for i, w in enumerate(COVER_NAMES):
        if stem == w:
            return i
    for i, w in enumerate(COVER_NAMES):
        if stem.startswith(w) or stem.endswith(w):
            return 10 + i
    return None


def find_cover(path):
    """从游戏根目录找现成的封面/图标图（cover > poster > banner > title > icon > logo，
    变体命名 cover1/game_cover 优先级更低但可用；同级取字典序靠前者）。"""
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return ""
    best, best_score = "", None
    for n in names:
        low = n.lower()
        if not low.endswith(IMAGE_EXTS):
            continue
        score = _cover_score(os.path.splitext(low)[0])
        if score is not None and (best_score is None or score < best_score):
            best, best_score = os.path.join(path, n), score
    return best


def import_cover(src):
    """把用户选的图片复制进工具的 covers 目录，返回新路径（同名内容去重）。"""
    os.makedirs(COVER_DIR, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() or ".png"
    try:
        key = hashlib.md5((os.path.abspath(src) + str(os.path.getmtime(src)))
                          .encode("utf-8")).hexdigest()[:12]
    except OSError:
        key = hashlib.md5(os.path.abspath(src).encode("utf-8")).hexdigest()[:12]
    dst = os.path.join(COVER_DIR, key + ext)
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)
    return dst


def scan_paths(roots, log=print, should_stop=None, max_depth=5):
    """在给定目录/盘符列表里递归寻找 Ren'Py 游戏，返回游戏根目录列表。

    深度限制 max_depth 层；跳过系统目录；识别为一个游戏后不再深入其内部。"""
    found = []
    scanned = [0]

    def walk(d, depth):
        if should_stop is not None and should_stop():
            return
        if depth > max_depth:
            return
        try:
            entries = os.scandir(d)
        except OSError:
            return
        with entries:
            for e in entries:
                if should_stop is not None and should_stop():
                    return
                if not e.is_dir(follow_symlinks=False):
                    continue
                name = e.name.strip().lower()
                if name in SKIP_DIRS or name.startswith(("$", ".")):
                    continue
                scanned[0] += 1
                if looks_like_game(e.path):
                    found.append(e.path)
                    log("发现游戏：%s" % e.path)
                    continue
                walk(e.path, depth + 1)
                if scanned[0] and scanned[0] % 500 == 0:
                    log("已扫描 %d 个目录…" % scanned[0])

    for r in roots:
        if os.path.isdir(r):
            walk(r, 0)
    return found


class Library:
    """持久化的游戏库（library.json）：游戏条目 + 扫描根目录。"""

    def __init__(self):
        data = read_json(LIB_PATH, None) or {}
        self.scan_roots = [r for r in data.get("scan_roots", []) if isinstance(r, str)]
        self.games = [g for g in data.get("games", []) if isinstance(g, dict)]

    def save(self):
        write_json(LIB_PATH, {"scan_roots": self.scan_roots, "games": self.games})

    @staticmethod
    def _key(p):
        return os.path.normcase(os.path.normpath(p))

    def find(self, path):
        k = self._key(path)
        return next((g for g in self.games if self._key(g.get("path", "")) == k), None)

    def add(self, path, added_by="manual", language="chinese"):
        """登记/刷新一个游戏（按路径去重），返回条目。"""
        path = os.path.normpath(path)
        g = self.find(path)
        if g is None:
            g = {"path": path, "name": "", "cover": "",
                 "translated": False, "added_by": added_by}
            self.games.append(g)
        g["name"] = derive_name(path)
        g["translated"] = detect_translated(path, language)
        if not g.get("cover"):
            g["cover"] = find_cover(path)
        return g

    def remove(self, path):
        k = self._key(path)
        self.games = [g for g in self.games if self._key(g.get("path", "")) != k]

    def games_missing_cover(self):
        """没有可用封面（未设置或文件已丢失）的游戏条目。"""
        return [g for g in self.games
                if not (g.get("cover") and os.path.isfile(str(g["cover"])))]

    def set_cover(self, path, src):
        g = self.find(path)
        if not g:
            return ""
        g["cover"] = import_cover(src)
        return g["cover"]

    def set_rating(self, path, rating):
        """写入某游戏的联网评分（结构见 core/ratings.py）。"""
        g = self.find(path)
        if not g:
            return
        g["ratings"] = rating

    def refresh(self, language="chinese"):
        """刷新全部条目的名称/存在性/汉化状态，返回状态发生变化的条数。"""
        changed = 0
        for g in self.games:
            p = g.get("path", "")
            if os.path.isdir(p):
                if not g.get("cover"):
                    g["cover"] = find_cover(p)
                g["name"] = derive_name(p)
                t = detect_translated(p, language)
                if t != bool(g.get("translated")):
                    changed += 1
                g["translated"] = t
            g.setdefault("translated", False)
        return changed

    def add_scan_roots(self, roots):
        for r in roots:
            r = os.path.normpath(r)
            if r not in self.scan_roots:
                self.scan_roots.append(r)
