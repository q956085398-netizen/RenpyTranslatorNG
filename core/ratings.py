# -*- coding: utf-8 -*-
"""游戏评分：按游戏名联网查评分，并带上评分人数。

两个源都无需登录，先 F95 命中就收工，没命中再用 dikgames 兜底：

1. F95Zone（5 星制）——官方「latest_alpha」JSON 接口按名字搜索拿到帖子号，
   帖子页的 JSON-LD `aggregateRating` 给出精确的 ratingValue / ratingCount
   （搜索结果里的 rating 只有两位小数、没有人数，帖子页才有人数）。
2. dikgames（10 分制）——站内搜索拿帖子地址，帖子页两套评分都在：
   「站点评分」gp-rating-score（带 Great / Fair 这类评语）与
   「用户平均分」gp-average-rating-score + 投票人数 gp-total-votes。

匹配从严：搜索词按「整串 → 去掉虚词 → 逐级砍头/砍尾」降级尝试（F95 的检索对
多词串很挑剔，「No More Money」要退到「More Money」才搜得到），候选标题必须是
查询词的完全一致（归一化后）、「接了下半句」的扩写、或「查询词的开头部分」，
都不满足宁可不配，也不乱配（见 match_rank）。
"""
import html as _html
import re
import time

import requests

from . import net
from .config import Config
from .coverfetch import key, normalize_name, search_dikgames_posts

_F95_API = "https://f95zone.to/sam/latest_alpha/latest_data.php"
_F95_THREAD = "https://f95zone.to/threads/%d/"
_F95_INTERVAL = 1.2  # 秒：F95 限流很紧（连发就 429），两次请求之间至少隔这么久
_F95_429_WAIT = 5.0  # 秒：被限流后退避一下再试一次，再不行就整源冷却、改用 dikgames

# F95 帖子页 JSON-LD 里的聚合评分（顺序固定：ratingValue, bestRating, ratingCount）
_F95_AGG = re.compile(
    r'"aggregateRating".{0,200}?"ratingValue"\s*:\s*"([\d.]+)".{0,200}?'
    r'"bestRating"\s*:\s*"(\d+)".{0,200}?"ratingCount"\s*:\s*"(\d+)"', re.S)

# dikgames 帖子页：站点评分 + 评语 / 用户平均分 / 投票人数
_DIK_SITE = re.compile(
    r'id="gp-review-results-rating".{0,400}?<div class="gp-rating-score">\s*([\d.]+)\s*</div>'
    r'(?:.{0,200}?<span class="gp-rating-text">([^<]*)</span>)?', re.S)
_DIK_USER = re.compile(r'<div class="gp-average-rating-score[^"]*">\s*([\d.]+)\s*</div>')
_DIK_VOTES = re.compile(
    r'<div class="gp-total-votes[^"]*">\s*<span[^>]*>\s*([\d,]+)\s*</span>')


def _tokens(s):
    return [t for t in re.split(r"[^0-9a-z]+", (s or "").lower()) if t]


# F95 检索吃不下虚词：带 a/and/with 的多词串整串查不到，去掉虚词才行
_FUNCTION_WORDS = {"a", "an", "and", "the", "of", "with", "in", "on", "at",
                   "to", "for", "or", "by", "is", "no", "my", "your"}


def _run_at(seq, sub):
    """sub 是否为 seq 中一段连续的词，返回起始下标或 None。"""
    for i in range(len(seq) - len(sub) + 1):
        if seq[i:i + len(sub)] == sub:
            return i
    return None


# 下载包常见的发布尾巴：只有这些词算「版本尾巴」，其余多出来的词都当另一个游戏
_QUALIFIER_WORDS = {"pc", "win", "windows", "mac", "linux", "full", "final", "se",
                    "hd", "remaster", "remastered", "remake", "special", "complete",
                    "definitive", "extended", "supporter", "market", "prologue",
                    "demo", "build", "edition", "ver", "version", "alpha", "beta",
                    "game", "the"}
_QUALIFIER_RE = re.compile(r"^[sve]\d+$")   # s3 / v0.4 / e2 这类季号版本号


def _is_qualifier(tok):
    return tok in _QUALIFIER_WORDS or bool(_QUALIFIER_RE.match(tok))


def match_rank(query, title):
    """查询词与候选标题的贴合度：0 完全一致 / 1 标题是查询词的扩写
    / 2 标题是查询词的开头、多出来的都是版本尾巴，None 不匹配。

    1 要求查询词本身有两个以上词、且不短于 6 个字符——单词查询的扩写
    几乎都是另一个游戏（Imperial 撞上 Imperial Chronicles）；2 只认「开头 + 版本尾巴」，
    既不会从中段截出 No More Money 这种怪匹配，也不会把 Doors Part 2 当成 Doors。"""
    qk, tk = key(query), key(title)
    if not qk or not tk:
        return None
    if qk == tk:
        return 0
    qt, tt = _tokens(query), _tokens(title)
    if len(qt) < 2 or len(qk) < 6:
        return None
    if len(tt) > len(qt):
        return 1 if _run_at(tt, qt) is not None else None
    if len(tt) < len(qt) and len(tk) >= 5:
        return 2 if _run_at(qt, tt) == 0 and all(
            _is_qualifier(t) for t in qt[len(tt):]) else None
    return None


def _queries(query, limit=8):
    """搜索词降级序列：长串优先，同级里虚词版优先（最多 limit 个）。

    搜索词逐级砍头/砍尾，并额外生成一份去掉虚词的版本（Shut Up And Dance Special
    → Shut Up Dance Special → Shut Up Dance）；截短后仍只认与游戏名贴合的结果，
    不会因为放宽搜索词而配到别的游戏。"""
    toks = query.split()
    kept = [t for t in toks if t.lower() not in _FUNCTION_WORDS]
    cands = []

    def gen(seq, pri):
        for i in range(len(seq)):
            for j in range(len(seq), i, -1):
                cands.append((-(j - i), pri, " ".join(seq[i:j])))

    gen(toks, 0)
    if kept and kept != toks:
        gen(kept, -1)
    out = []
    for _, _, q in sorted(cands):
        if q not in out:
            out.append(q)
    return out[:limit]


def _best(cands, query):
    """cands: [(标题, 附加信息...)]，返回贴合度最好的一项（并列时取搜索结果靠前者）。"""
    scored = [(match_rank(query, c[0]), c) for c in cands]
    scored = [(r, c) for r, c in scored if r is not None]
    if not scored:
        return None
    scored.sort(key=lambda rc: rc[0])
    return scored[0][1]


# ---------------- F95Zone ----------------

_last_call = [0.0]


def f95_headers(cookie=None):
    """F95 请求头：配了登录 Cookie 就带上。

    未登录的匿名请求有每小时次数上限（接口直接返回 429：「Anonymous users have a
    limited amount of requests per hour」），登录 Cookie 能把这条限制放开。"""
    if cookie is None:
        cookie = Config().get("f95_cookie") or ""
    headers = dict(net.BROWSER_HEADERS)
    if cookie.strip():
        headers["Cookie"] = cookie.strip()
    return headers


def _get_f95(url, params=None):
    """带节流的 F95 请求：两次请求之间留间隔，被限流（429）时退避重试一次。

    重试还不行说明限流窗口没过去：给 f95 打上冷却（后续游戏直接跳过、改用 dikgames），
    并把异常抛给上层，本游戏剩下的搜索词变体也不再多花时间。"""
    r = None
    for attempt in range(2):
        wait = _F95_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        r = net.request("GET", url, headers=f95_headers(),
                        timeout=net.SEARCH_TIMEOUT, params=params)
        if r.status_code != 429:
            net.cooldown_clear("f95")
            return r
        if attempt == 0:
            time.sleep(_F95_429_WAIT)
    net.cooldown_start("f95")
    r.raise_for_status()
    return r


def search_f95(query):
    """F95 搜索：返回 [(标题, 帖子号, 五星分)]。"""
    r = _get_f95(_F95_API, params={"cmd": "list", "search": query, "cat": "games"})
    try:
        data = (r.json().get("msg") or {}).get("data") or []
    except ValueError:
        # 抽到 HTML 说明被风控挡了（不是没搜到）：按源故障处理，冷却后改用 dikgames
        net.cooldown_start("f95")
        raise requests.HTTPError("F95 返回的不是 JSON，可能触发风控")
    out = []
    for it in data:
        tid, title = it.get("thread_id"), (it.get("title") or "").strip()
        if tid and title:
            out.append((title, int(tid), it.get("rating") or 0))
    return out


def parse_f95_html(html):
    """F95 帖子页 → (分数, 满分, 人数)；页面没带聚合评分时返回 None。"""
    m = _F95_AGG.search(html)
    if not m:
        return None
    return float(m.group(1)), int(m.group(2)), int(m.group(3))


def f95_rating(thread_id):
    """帖子页的精确评分：返回 (分数, 满分, 人数)；页面没给聚合评分时返回 None。"""
    return parse_f95_html(_get_f95(_F95_THREAD % thread_id).text)


def fetch_f95(name):
    """按游戏名查 F95 五星评分，返回评分字典；没配到返回 None。"""
    query = normalize_name(name)
    if not query:
        return None
    for q in _queries(query):
        if net.cooldown_active("f95"):
            break              # 已被限流，本游戏不必再换搜索词硬试
        hit = _best(search_f95(q), query)
        if not hit:
            continue
        title, tid, api_score = hit
        try:
            agg = f95_rating(tid)
        except requests.RequestException:
            raise
        except Exception:
            agg = None
        out = {"src": "f95", "title": title, "url": _F95_THREAD % tid,
               "max": 5, "ts": int(time.time())}
        if agg:
            out.update(score=round(agg[0], 2), count=agg[2])
        else:                       # 帖子页没读到聚合评分时退回接口里的分数
            out.update(score=round(float(api_score), 2), count=None)
        return out
    return None


# ---------------- dikgames ----------------

def parse_dikgames_html(html):
    """dikgames 帖子页 → (站点评分, 评语, 用户均分, 投票人数)，读不到的都是 None。"""
    site = _DIK_SITE.search(html)
    user = _DIK_USER.search(html)
    votes = _DIK_VOTES.search(html)
    return (float(site.group(1)) if site else None,
            _html.unescape(site.group(2) or "").strip() if site else "",
            float(user.group(1)) if user else None,
            int(votes.group(1).replace(",", "")) if votes else None)


def dikgames_rating(url):
    """dikgames 帖子页的两套评分：返回 (站点评分, 评语, 用户均分, 投票人数)。"""
    r = net.request("GET", url, headers=net.BROWSER_HEADERS, timeout=net.SEARCH_TIMEOUT)
    r.raise_for_status()
    return parse_dikgames_html(r.text)


def fetch_dikgames(name):
    """按游戏名查 dikgames 评分，返回评分字典；没配到返回 None。"""
    query = normalize_name(name)
    if not query:
        return None
    for q in _queries(query, limit=6):
        hit = _best([(n, u) for n, u, _img, _d in search_dikgames_posts(q)], query)
        net.cooldown_clear("dikgames")
        if not hit:
            continue
        title, url = hit
        try:
            site, label, user, votes = dikgames_rating(url)
        except requests.RequestException:
            raise
        except Exception:
            return None
        if site is None and user is None:
            return None
        return {"src": "dikgames", "title": title, "url": url, "max": 10,
                "site_score": site, "site_label": label,
                "user_score": user, "count": votes, "ts": int(time.time())}
    return None


# ---------------- 对外入口 ----------------

_SOURCES = (("f95", fetch_f95), ("dikgames", fetch_dikgames))


def fetch_rating(name, log=print):
    """按游戏名联网查评分：F95 优先，未命中换 dikgames；都没有返回 None。

    两个源都网络故障时抛 requests.RequestException，由调用方决定是否继续。"""
    net_fail = 0
    for src, fn in _SOURCES:
        if net.cooldown_active(src):
            log("⚠ %s 处于故障冷却中，跳过" % src)
            net_fail += 1
            continue
        try:
            found = fn(name)
        except requests.RequestException as e:
            net_fail += 1
            net.cooldown_start(src)
            log("⚠ %s 连接失败（%s），冷却 10 分钟，改用其他源" % (src, str(e)[:100]))
            if net_fail >= len(_SOURCES):
                raise
            continue
        if found:
            return found
    return None


_STALE_DAYS = 30
_FALLBACK_STALE_DAYS = 3  # 兜底源（dikgames）抓到的评分：过几天再试一次 F95


def is_stale(rating, days=_STALE_DAYS):
    """该重新抓评分了吗：没抓过，或上次抓取已经超过 days 天。

    dikgames 是 F95 被限流/未收录时的兜底，所以它抓到的评分只留几天——
    下次批量更新时会重试 F95，命中就升级成五星制的那个。"""
    if not rating:
        return True
    if rating.get("src") != "f95":
        days = min(days, _FALLBACK_STALE_DAYS)
    try:
        ts = int(rating.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    return (time.time() - ts) > days * 86400


def summary(rating):
    """评分字典 → 卡片工具提示用的一行中文描述。"""
    if not rating:
        return ""
    src = {"f95": "F95Zone（5 星制）", "dikgames": "dikgames（10 分制）"}.get(
        rating.get("src"), rating.get("src") or "?")
    parts = [src]
    if rating.get("src") == "f95":
        parts.append("评分 %s/5" % rating.get("score"))
    else:
        if rating.get("site_score") is not None:
            parts.append("站点评分 %s/10%s" % (
                rating["site_score"],
                "（%s）" % rating["site_label"] if rating.get("site_label") else ""))
        if rating.get("user_score") is not None:
            parts.append("用户均分 %s/10" % rating["user_score"])
    if rating.get("count"):
        parts.append("%s 人评分" % format_count(rating["count"]))
    if rating.get("title"):
        parts.append("匹配：%s" % rating["title"])
    return " · ".join(parts)


def format_count(n):
    """评分人数的紧凑写法：过万折算成 1.2 万，避免卡片上撑爆。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 100000:
        return "%.0f 万" % (n / 10000.0)
    if n >= 10000:
        return "%.1f 万" % (n / 10000.0)
    return "{:,}".format(n)


def stars(rating):
    """把评分折算成 5 星制分数（0-5），没有评分返回 0；用于画星星。"""
    if not rating:
        return 0.0
    if rating.get("src") == "f95":
        score, mx = rating.get("score"), rating.get("max") or 5
    else:
        score = rating.get("user_score")
        if score is None:
            score = rating.get("site_score")
        mx = rating.get("max") or 10
    try:
        score, mx = float(score), float(mx or 5)
    except (TypeError, ValueError):
        return 0.0
    if mx <= 0 or score <= 0:
        return 0.0
    return max(0.0, min(5.0, score * 5.0 / mx))


def headline(rating):
    """卡片上显示的主评分文本（用各源自己的进制）+ 人数，例如 4.7 · 1,341。"""
    if not rating:
        return ""
    if rating.get("src") == "f95":
        score = rating.get("score")
    else:
        score = rating.get("user_score")
        if score is None:
            score = rating.get("site_score")
    if score is None:
        return ""
    txt = "%.1f" % float(score)
    if rating.get("count"):
        txt += " · %s" % format_count(rating["count"])
    return txt
