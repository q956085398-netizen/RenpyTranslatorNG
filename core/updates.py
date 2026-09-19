# -*- coding: utf-8 -*-
"""游戏版本检查：本地装的是哪版 vs 线上最新是哪版（F95Zone 优先、dikgames 兜底）。

本地版本从三处取证，任何一处能读出来就参与比较：
1. `log.txt` 头部——Ren'Py 8.2+ 启动时会把 `config.name` / `config.version` 写进去
   （形如「A Family Venture」+「0.09_V4_Supporter」）；
2. `game/options.rpy` 的 `define config.version = "..."`（游戏被本工具反编译过就有）；
3. 游戏目录名——下载包名几乎都带版本尾巴（`AFamilyVenture-0.09_V4_Supporter-pc`），
   取目录名里**去掉游戏名之后**的剩余部分，免得把游戏名里的数字（LabRats2、TFTUV2）
   当成版本号。

线上版本：F95Zone 的检索接口直接给 `version` 字段（如 `v0.09 v4 Fix`），
没收录的退回 dikgames（搜索结果标题里的 `[v1.0]` 方括号）；两者都不需要登录。

**判定从严**：版本串的写法千奇百怪（`Ch.1_Ep.4`、`Ep2.1`、`2025.04`、`update_1`），
所以只有「线上版本严格高于本地所有候选版本」才判为有新版本，其余一律不提示——
宁可漏报（用户自己点开卡片看），也不能乱报（白下一遍几十 GB 的重复包）。
因此只比数字：主版本号（带小数点的那个，`Ch.3.6` / `v0.9.5`）+ 其余修订号，
而 `S4` / `Ep.4` / `Part2` 这类「第几季第几集」的内容标记不算修订号
（否则 `v4.4.3 S4 Ep.4 Gold` 会被误判成比本地的 `4.4.3` 新）；
`Supporter` / `Fix` / `market` / `Beta` 这类词只在两边都是纯词版本时用于判等。
"""
import os
import re
import time

import requests

from . import net
from . import ratings
from .coverfetch import key, normalize_name

# 版本号里的英文数字（EpisodeFive.1 / Part Two）
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
              "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
              "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
              "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
              "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

# 数字：优先整块吃下带小数点的版本号（0.09 / 1.0.1），否则单个整数
_NUM_RE = re.compile(r"\d+(?:\.\d+)+|\d+")
_WORD_NUM_RE = re.compile(r"\b(%s)\b" % "|".join(_NUM_WORDS))
_SPLIT_RE = re.compile(r"[^0-9a-z]+")


class Version:
    """版本串的归一化形态：core 主版本号 / extras 其余数字 / words 词集（只用于判等）。"""

    __slots__ = ("raw", "core", "extras", "words")

    def __init__(self, raw, core, extras, words):
        self.raw, self.core, self.extras, self.words = raw, core, extras, words

    def __repr__(self):
        return "Version(%r, core=%s, extras=%s)" % (self.raw, self.core, self.extras)


def _prep(text):
    """版本串归一化：驼峰拆词 + 英文数字转阿拉伯数字 + 分隔符拉平。"""
    s = (text or "").strip()
    if not s:
        return ""
    # 驼峰拆词（EpisodeFive → Episode Five）后再映射英文数字，才认得出 Five.1 是 5.1
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)     # V0.27 → V 0.27（版本标记与数字分开）
    low = s.lower()
    low = _WORD_NUM_RE.sub(lambda m: str(_NUM_WORDS[m.group(1)]), low)
    # 「0-20-1」「S2E4_5」这种用连字符/下划线写的版本号拉平成点号；字符串里已经有
    # 小数点时不拉，免得把 Part_2-0.235 里的「2」和版本号粘成一个数
    if "." not in low:
        low = re.sub(r"(?<=\d)[-_](?=\d)", ".", low)
    return low


# 内容标记：S4 / Ep.4 / Ch.3 / Part2 / Day7 后面跟着的数字是「第几季第几集」，
# 不是版本修订号，不能拿去和线上版本比大小（v0.09 v4 里的 v 才是版本标记，不在此列）
_MARKER_WORDS = {"s", "e", "ep", "episode", "ch", "chap", "chapter", "part", "pt",
                 "p", "day", "d", "act", "book", "vol", "volume", "season",
                 "round", "r"}


def _marker_before(low, start):
    """数字前面紧挨着的那个词（跳过空格、分隔符，以及 Ep.1-11 这种区间里的前一个数字），
    用于识别内容标记。"""
    i = start - 1
    while i >= 0 and (low[i].isdigit() or low[i].isspace() or low[i] in "-_."):
        i -= 1
    j = i
    while j >= 0 and low[j].isalpha():
        j -= 1
    return low[j + 1:i + 1]


def version_key(text):
    """版本串 → Version。数字一律取成整数元组（0.09 → (0,9)，v4 → (4,)），
    带小数点的那个优先当主版本号（`Sophia…Part_2-0.235` 取 0.235 而不是 2），
    其余数字里剔掉内容标记（S4 / Ep.4 / Part2），只留真正的修订号。"""
    low = _prep(text)
    if not low:
        return Version(text or "", (), (), set())
    matches = list(_NUM_RE.finditer(low))
    core_idx = next((i for i, m in enumerate(matches) if "." in m.group(0)), None)
    if core_idx is None:
        core_idx = 0 if matches else -1

    def as_tuple(m):
        return tuple(int(p) for p in m.group(0).split("."))

    core = as_tuple(matches[core_idx]) if core_idx >= 0 else ()
    extras = tuple(as_tuple(m) for i, m in enumerate(matches)
                   if i != core_idx and _marker_before(low, m.start()) not in _MARKER_WORDS)
    words = {w for w in _SPLIT_RE.split(low) if w and not w.isdigit()}
    return Version(text or "", core, extras, words)


def _cmp(a, b):
    """版本比较（-1 更小 / 0 相同 / 1 更大）：主版本号优先，相同再比其余数字，
    位数不同时短的补 0（1.0 == 1.0.0；1.0 < 1.0.1）。"""
    # core 是整数元组（补 0），extras 是整数元组的元组（补 (0,)）
    for x, y, pad in ((a.core, b.core, 0), (a.extras, b.extras, (0,))):
        n = max(len(x), len(y))
        if not n:
            continue
        xp = x + (pad,) * (n - len(x))
        yp = y + (pad,) * (n - len(y))
        if xp != yp:
            return -1 if xp < yp else 1
    return 0


def _same(a, b):
    """两边都读得出数字时比数字；都是纯词版本（FINAL / demo）时比词集。"""
    if a.core and b.core:
        return _cmp(a, b) == 0
    if not a.core and not b.core:
        return bool(a.words or b.words) and a.words == b.words
    return False


def verdict(local_list, remote):
    """本地候选版本列表 + 线上版本 → (状态, 用于展示的本地版本, 原因)。

    状态：update = 线上确认更新 / latest = 已是最新 / unknown = 判不出来（不提示）。
    """
    r = version_key(remote)
    cands = [(s, version_key(s)) for s in (local_list or []) if (s or "").strip()]
    if not (r.core or r.words):
        return "unknown", "", "no_remote"
    for s, k in cands:
        if (k.core or k.words) and _same(k, r):
            return "latest", s, "same"
    ranked = [(s, k) for s, k in cands if k.core]
    if not ranked:
        return "unknown", (cands[0][0] if cands else ""), "no_local_version"
    if not r.core:
        return "unknown", ranked[0][0], "unclear"
    if all(_cmp(k, r) < 0 for _, k in ranked):
        best = max(ranked, key=lambda sk: sk[1].core + sk[1].extras)[0]
        return "update", best, "remote_newer"
    if any(_cmp(k, r) > 0 for _, k in ranked):
        return "unknown", ranked[0][0], "local_ahead"
    return "unknown", ranked[0][0], "unclear"


# ---------------- 本地版本取证 ----------------

_OPT_VERSION = re.compile(r"""config\.version\s*=\s*["']([^"']*)["']""")


def _from_log(root):
    """log.txt 头部：日期 / 平台 / Ren'Py 版本，之后可能是 游戏名 / 版本 / Built at。"""
    path = os.path.join(root, "log.txt")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(800)
    except OSError:
        return ""
    lines = [l.strip() for l in head.splitlines()[:12]]
    for i, line in enumerate(lines):
        if line.startswith("Built at") and i:
            return lines[i - 1]
    return ""


def _from_options(root):
    """game/options.rpy 的 config.version（游戏被本工具反编译过就一定有）。"""
    path = os.path.join(root, "game", "options.rpy")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(200000)
    except OSError:
        return ""
    m = _OPT_VERSION.search(text)
    return m.group(1).strip() if m else ""


def _tail_after_name(folder, name):
    """去掉目录名开头与游戏名对应的部分，剩下的才是版本尾巴。

    按「只留字母数字」的归一化形式对齐（Angel's Love / A_Soldiers_Struggle 这类
    写法差异不会影响对齐），对不上就原样返回（宁可多留几个数字，也不硬切）。"""
    kn = key(name)
    if not kn:
        return folder
    acc, idx = "", None
    for i, ch in enumerate(folder):
        if not ch.isalnum():
            continue
        acc += ch.lower()
        if not kn.startswith(acc):
            return folder
        if len(acc) == len(kn):
            idx = i + 1
            break
    if idx is None:
        return folder
    return folder[idx:]


def _from_folder(root, name):
    """目录名里的版本尾巴（下载包名常带 -0.09_V4_Supporter-pc 这类后缀）。"""
    tail = _tail_after_name(os.path.basename(os.path.normpath(root)), name)
    return tail.strip(" -_.")


def local_versions(root, name=""):
    """本地版本候选列表（去重，顺序：log.txt → options.rpy → 目录名）。"""
    out = []
    for v in (_from_log(root), _from_options(root), _from_folder(root, name)):
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def local_version(root, name=""):
    """展示用的本地版本：取数字最大的那个候选（判不出数字时取第一个）。"""
    cands = [(v, version_key(v)) for v in local_versions(root, name)]
    if not cands:
        return ""
    ranked = [(v, k) for v, k in cands if k.core]
    if not ranked:
        return cands[0][0]
    return max(ranked, key=lambda vk: vk[1].core + vk[1].extras)[0]


# ---------------- 线上版本 ----------------

def search_f95_versions(query):
    """F95 检索：返回 [(标题, 帖子号, 线上版本, 最后更新时刻)]（只保留有帖子号的条目）。"""
    r = ratings._get_f95(ratings._F95_API,
                         params={"cmd": "list", "search": query, "cat": "games"})
    try:
        data = (r.json().get("msg") or {}).get("data") or []
    except ValueError:
        # 抽到 HTML 说明被风控挡了（不是没搜到）：按源故障处理，冷却后改用 dikgames
        net.cooldown_start("f95")
        raise requests.HTTPError("F95 返回的不是 JSON，可能触发风控")
    out = []
    for it in data:
        tid, title = it.get("thread_id"), str(it.get("title") or "").strip()
        if tid and title:
            # version 有时是数字（1.1），统一成字符串再参与比较
            out.append((title, int(tid), str(it.get("version") or "").strip(),
                        it.get("ts") or 0))
    return out


def fetch_f95_version(name):
    """F95Zone：检索接口的 version 字段（比帖子页 HTML 稳，一次请求拿到）。"""
    query = normalize_name(name)
    if not query:
        return None
    for q in ratings._queries(query, limit=3):
        if net.cooldown_active("f95"):
            break              # 已被限流，本游戏不必再换搜索词硬试
        hit = ratings._best(search_f95_versions(q), query)
        if not hit:
            continue
        title, tid, ver, ts = hit
        if not ver:
            return None        # 检索结果没带版本号，交给 dikgames 兜底
        return {"src": "f95", "title": title, "version": ver,
                "url": ratings._F95_THREAD % tid, "ts": ts}
    return None


_DIK_BRACKET = re.compile(r"\[([^\]]{0,32})\]")
_DIK_NOT_VER = re.compile(r"^(dev|by|author|patreon|subscribestar|itch|gog|steam|\W)", re.I)


def pick_dik_version(display):
    """dikgames 搜索结果标题 → 版本：取第一个像版本号的方括号（[v1.0] / [0.8.2]）。"""
    for m in _DIK_BRACKET.finditer(display or ""):
        seg = m.group(1).strip()
        if not re.search(r"\d", seg) or _DIK_NOT_VER.match(seg):
            continue
        return seg
    return ""


def fetch_dikgames_version(name):
    """dikgames：搜索结果标题里的版本号（免费、无需登录，F95 被限流时的兜底）。"""
    from .coverfetch import search_dikgames_posts
    query = normalize_name(name)
    if not query:
        return None
    for q in ratings._queries(query, limit=3):
        hit = ratings._best([(n, u, d) for n, u, _img, d in search_dikgames_posts(q)], query)
        net.cooldown_clear("dikgames")
        if not hit:
            continue
        title, url, display = hit
        ver = pick_dik_version(display)
        if not ver:
            return None
        return {"src": "dikgames", "title": title, "version": ver, "url": url, "ts": 0}
    return None


_SOURCES = (("f95", fetch_f95_version), ("dikgames", fetch_dikgames_version))


def fetch_remote_version(name, log=print):
    """按游戏名联网查线上最新版本；两个源都没收录返回 None。

    沿用评分那套源切换：某个源故障就冷却并改用另一个，两个都故障时抛
    requests.RequestException，由调用方决定是否中断整轮检查。"""
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


# ---------------- 单个游戏 / 整库检查 ----------------

STALE_HOURS = 12          # 同一游戏多久内不重复联网查（启动自动检查按这个节流）
_PARTIAL_STALE_HOURS = 3  # 上次因为源被限流/故障没查全：过几小时补一次
_UNKNOWN_STALE_HOURS = 72  # 两个站都没收录（用户自己装的冷门版本）：几天后再看一眼


def is_stale(info, hours=STALE_HOURS):
    """该重新联网查这个游戏了吗：没查过、没查全、或上次检查已超过时限。

    已经确认「最新」的 12 小时后复查；判不出来的分两种——上次被限流没查全的很快
    补查，两个站都没收录的隔几天再看（免得每次启动都去问一遍、白耗 F95 的每小时额度）。"""
    if not info:
        return True
    try:
        ts = int(info.get("checked") or 0)
    except (TypeError, ValueError):
        ts = 0
    if info.get("state") == "unknown":
        hours = min(hours, _PARTIAL_STALE_HOURS if info.get("partial")
                    else _UNKNOWN_STALE_HOURS)
    return (time.time() - ts) > hours * 3600


def check_game(entry, log=print):
    """检查一个库条目：返回要写进 entry["update"] 的字典（网络异常向上抛）。"""
    path = entry.get("path", "")
    name = entry.get("name") or os.path.basename(os.path.normpath(path)) or ""
    locals_ = local_versions(path, entry.get("name") or "")
    info = {"checked": int(time.time()), "local": local_version(path, entry.get("name") or "")}
    remote = fetch_remote_version(name, log=log)
    if not remote or not remote.get("version"):
        # 有源被限流/故障时这次不算查全，过几小时再补一次
        info.update(state="unknown", reason="no_remote",
                    partial=any(net.cooldown_active(s) for s, _ in _SOURCES))
        log("🔍 %s：两个站都没有收录（或没读出线上版本）" % name)
        return info
    state, used, reason = verdict(locals_, remote["version"])
    info.update(state=state, reason=reason, ver=remote["version"], src=remote["src"],
                title=remote.get("title") or "", url=remote.get("url") or "",
                ts=remote.get("ts") or 0, local=used or info.get("local", ""))
    log("%s %s：线上 %s（%s）｜本地 %s" % (
        "⬆" if state == "update" else ("✔" if state == "latest" else "·"),
        name, remote["version"], remote["src"], info.get("local") or "无"))
    return info


def run_check(entries, log=print, prog=None, should_stop=None, on_result=None, pause=None):
    """逐个检查版本；entries 是库条目（dict，就地写入 update 字段）。

    on_result(entry) 在每个游戏查完后调用（界面据此立刻存库、刷新封面角标）；
    pause() 返回 True 时挂起等待（用户正在跑别的任务时不抢网络、不抢写 library.json）；
    should_stop() 返回 True 时停下，剩下的下次继续。返回摘要文本。"""
    todo = list(entries)
    if prog:
        prog(0, len(todo))
    n_up = 0
    done = 0
    stopped = False
    for i, entry in enumerate(todo):
        while pause and pause():
            if should_stop and should_stop():
                break
            time.sleep(0.3)
        if should_stop and should_stop():
            stopped = True
            break
        if prog:
            prog(i, len(todo))
        try:
            info = check_game(entry, log=log)
        except requests.RequestException as e:
            log("⚠ 网络不可用，中断版本检查（%s）" % e)
            stopped = True
            break
        except Exception as e:                       # 单个游戏异常不拖垮整轮
            log("⚠ %s 检查失败：%s" % (entry.get("name", "?"), e))
            continue
        entry["update"] = info
        done += 1
        if info.get("state") == "update":
            n_up += 1
        if on_result:
            on_result(entry)
    if prog:
        prog(done, len(todo))
    if stopped:
        return "已中断：已查 %d 个，其中 %d 个可更新" % (done, n_up)
    if n_up:
        return "检查完成：%d 个游戏有新版本（封面左上角标了「可更新」）" % n_up
    return "检查完成：%d 个游戏都已是最新（或无法判断）" % done


def summary(info):
    """条目 → 卡片工具提示用的一行中文描述。"""
    if not info:
        return ""
    src = {"f95": "F95Zone", "dikgames": "dikgames"}.get(info.get("src"), info.get("src") or "")
    ver, local = info.get("ver") or "", info.get("local") or ""
    state, reason = info.get("state"), info.get("reason")
    if state == "update":
        return "⬆ 有新版本可用：线上 %s（%s）｜本地 %s" % (ver, src or "?", local or "未知")
    if state == "latest":
        return "✔ 已是最新：线上 %s（%s）｜本地 %s" % (ver, src or "?", local or "未知")
    if reason == "no_remote":
        return "未收录：F95Zone / dikgames 都没有同名游戏，无法比较版本"
    if reason == "local_ahead":
        return "本地版本（%s）比线上（%s）还新，不作提示" % (local or "未知", ver or "未知")
    if reason == "no_local_version":
        return "无法判断：游戏目录里读不到版本号（线上是 %s）" % (ver or "未知")
    if reason == "error":
        return "检查失败：%s" % (info.get("detail") or "网络问题")
    if ver:
        return "无法判断版本先后：线上 %s｜本地 %s" % (ver, local or "未知")
    return "还没查过版本"


def badge_text(info):
    """封面角标文字：只有确认有新版才显示，其余返回空串（不显示任何东西）。"""
    return "可更新" if (info or {}).get("state") == "update" else ""
