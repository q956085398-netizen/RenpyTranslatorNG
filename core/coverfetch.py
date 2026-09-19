# -*- coding: utf-8 -*-
"""网络封面：游戏文件夹里没有封面时，按游戏名联网搜索同名游戏并下载封面。

按顺序尝试多个封面源，先命中先用：
1. VNDB（api.vndb.org，开放 JSON API、免密钥）——欧美视觉小说收录率高，封面为竖版盒图；
2. dikgames（dikgames.com，WordPress 站点、无需登录，国内可直连）——同类游戏覆盖广，
   封面为横幅图（帖子标题形如「Game Name [v1.0] [Dev]」，方括号剥掉后再参与匹配）。
F95Zone 的图是横幅位图，卡片是竖版，所以只把它当评分源（见 ratings.py）不当封面源。

匹配从严：标题与游戏名「归一化后完全一致」优先，其次互为包含；都不满足宁可不配。
网络健壮性：每条请求系统代理失败自动切直连（再失败切回来），成功路径会被记住；
某个源整体网络故障时冷却 10 分钟，期间直接用其余源，不再反复浪费时间。
"""
import hashlib
import html as _html
import os
import re
from urllib.parse import quote_plus

import requests

from . import net
from .library import COVER_DIR

_VNDB_API = "https://api.vndb.org/kana/vn"

# 发布包常见后缀，与版本号一起从搜索词里剔除
_STOP_TOKENS = {"pc", "win", "final", "supporter", "market", "prologue",
                "demo", "build", "edition", "alpha", "beta"}


def normalize_name(name):
    """把 exe 名/目录名整理成自然搜索词：驼峰拆词、下划线/点转空格、去版本号尾巴。"""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")   # yV → y V
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)          # AFa → A Fa
    s = re.sub(r"[-_.]+", " ", s)
    toks = [t for t in s.split()
            if t and not re.fullmatch(r"v?\d[\w.]*", t, re.I)
            and t.lower() not in _STOP_TOKENS]
    return " ".join(toks)


def key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def variants(query):
    """搜索词变体：全词优先，搜不到时逐个去掉尾部词条（至少保留 2 个，至多 3 次）。"""
    toks = query.split()
    out = []
    while toks and len(out) < 3:
        out.append(" ".join(toks))
        if len(toks) <= 2:
            break
        toks = toks[:-1]
    return out


# ---------------- 各封面源 ----------------

def search_vndb(query):
    """VNDB 搜索，返回 (匹配名, 别名, url, 显示标题) 列表（只保留带封面的条目）。"""
    r = net.request("POST", _VNDB_API, headers=net.API_HEADERS, timeout=net.SEARCH_TIMEOUT,
                    json_body={"filters": ["search", "=", query],
                               "fields": "title, alttitle, image.url",
                               "results": 10})
    r.raise_for_status()
    out = []
    for it in r.json().get("results", []):
        img = it.get("image") or {}
        title, alt = it.get("title", ""), it.get("alttitle") or ""
        if img.get("url"):
            out.append((title, alt, img["url"], title))
    return out


_DIK_ITEM = re.compile(
    r'<a href="(https://dikgames\.com/[^"]+)"[^>]*?title="([^"]+)"[^>]*?>\s*'
    r'<img[^>]+?src="([^"]+)"', re.S)
_DIK_OK_EXT = (".jpg", ".jpeg", ".png", ".webp")


def search_dikgames_posts(query):
    """dikgames 搜索：抓搜索结果卡片，返回 (去版本尾的帖子名, 帖子地址, 缩略图, 原始标题)。"""
    r = net.request("GET", "https://dikgames.com/?s=" + quote_plus(query),
                    headers=net.BROWSER_HEADERS, timeout=net.SEARCH_TIMEOUT)
    r.raise_for_status()
    out, seen = [], set()
    for url, title, img in _DIK_ITEM.findall(r.text):
        display = _html.unescape(title).strip()
        name = re.sub(r"\[[^\]]*\]", "", display).strip()      # 去 [v1.0] [Dev] 尾巴
        img = re.sub(r"-\d+x\d+(?=\.[A-Za-z]{3,4}$)", "", _html.unescape(img))
        if img.startswith("//"):
            img = "https:" + img
        if not name or not img.lower().endswith(_DIK_OK_EXT) or name in seen:
            continue
        seen.add(name)
        out.append((name, url, img, display))
    return out


def search_dikgames(query):
    """dikgames 封面候选：(匹配名, 别名, 图片地址, 显示标题)。"""
    return [(name, "", img, display)
            for name, _url, img, display in search_dikgames_posts(query)]


_SOURCES = ("vndb", "dikgames")


def _search(src, query, log):
    """单源按变体依次搜索，返回按相似度排序的 (url, 显示标题) 列表。
    只有全词搜索允许包含匹配；截断重试只认完全一致（防止去掉尾词后误配相似名的其他游戏）。"""
    if net.cooldown_active(src):
        return []
    for i, q in enumerate(variants(query)):
        if src == "vndb":
            cands = search_vndb(q)
        else:
            cands = search_dikgames(q)
        net.cooldown_clear(src)
        ranked = _rank(cands, q, loose=(i == 0))
        if ranked:
            return ranked
    return []


def _rank(cands, query, loose=True):
    """把候选按相似度排序：完全一致 > 互为包含；不沾边的一律丢弃。

    包含匹配仅在 loose=True（全词搜索）且两侧都 ≥6 字符时启用——短词包含匹配
    误配率极高（如 arya 撞上罗马音 ikaryaku）。返回去重后的 (url, 显示标题) 列表。"""
    q = key(query)
    exact, partial = [], []
    for name, alt, url, display in cands:
        for pri, t in ((0, name), (1, alt)):
            k = key(t)
            if not k:
                continue
            if k == q:
                exact.append((pri, url, display))
            elif loose and len(q) >= 6 and len(k) >= 6 and (k in q or q in k):
                partial.append((pri, url, display))
    exact.sort()
    partial.sort()
    out, seen = [], set()
    for _, url, display in exact + partial:
        if url not in seen:
            seen.add(url)
            out.append((url, display))
    return out


def download(url):
    """把封面图下载进 covers 缓存目录（按 URL 哈希命名，同图不重复下载）。"""
    os.makedirs(COVER_DIR, exist_ok=True)
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    dst = os.path.join(COVER_DIR,
                       hashlib.md5(url.encode("utf-8")).hexdigest()[:12] + ext)
    if not os.path.isfile(dst):
        r = net.request("GET", url, headers=net.BROWSER_HEADERS, timeout=net.DOWNLOAD_TIMEOUT)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if len(r.content) < 2048 or (ctype and not ctype.startswith("image/")):
            raise ValueError("响应不是有效图片（%s, %d 字节）" % (ctype or "?", len(r.content)))
        with open(dst, "wb") as f:
            f.write(r.content)
    return dst


def fetch_cover(name, log=print):
    """按游戏名联网找封面：返回 (本地封面路径, 命中标题)；没配到返回 ("", "")。

    单个源网络故障自动降级到其余源；所有源都网络故障时抛 requests.RequestException，
    由调用方决定是否继续。"""
    query = normalize_name(name)
    if not query:
        return "", ""
    net_fail = 0
    for src in _SOURCES:
        try:
            ranked = _search(src, query, log)
        except requests.RequestException as e:
            net_fail += 1
            net.cooldown_start(src)
            log("⚠ %s 网络故障（%s），冷却 10 分钟，改用其他源"
                % (src, str(e)[:100]))
            if net_fail >= len(_SOURCES):
                raise
            continue
        for url, title in ranked:
            try:
                return download(url), title
            except (requests.ConnectionError, requests.Timeout):
                raise
            except Exception as e:
                log("⚠ 封面下载失败（%s）：%s" % (title, e))
    return "", ""
