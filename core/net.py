# -*- coding: utf-8 -*-
"""联网小工具：系统代理 ↔ 直连自动切换的请求，以及数据源的故障冷却。

各站点在国内环境下的可达性差异很大（VNDB / dikgames / F95 各自走不同的路），
所以每条请求先按最近成功的路径发，失败就换另一条，两条都失败才向上抛错。
某个源整体网络故障时进入冷却期，期间直接用其余源，不再反复浪费时间。
"""
import time

import requests

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
API_HEADERS = {"User-Agent": "RenpyTranslatorNG/1.0 (game library metadata fetcher)"}
SEARCH_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 90
SOURCE_COOLDOWN = 600.0  # 秒：某源网络故障后的冷却期

# 系统代理（注册表/环境变量）与直连两条路；sticky 记住最近成功的那条
_MODES = (("proxy", None), ("direct", {"http": None, "https": None}))
_sticky = None
_cooldown = {}  # 源名 -> 冷却到期时间（monotonic）


def request(method, url, *, headers, timeout, json_body=None, params=None):
    """带 failover 的请求：代理↔直连各试 2 轮；某条路成功后优先走它。"""
    global _sticky
    modes = list(_MODES)
    if _sticky:
        modes.sort(key=lambda m: 0 if m[0] == _sticky else 1)
    last = None
    for rnd in range(2):
        for mode, proxies in modes:
            try:
                r = requests.request(method, url, headers=headers, timeout=timeout,
                                     json=json_body, params=params, proxies=proxies)
                _sticky = mode
                return r
            except requests.RequestException as e:
                last = e
                time.sleep(0.5 * (rnd + 1))
    raise last


def cooldown_active(src):
    """该源是否处于故障冷却期。"""
    return time.monotonic() < _cooldown.get(src, 0)


def cooldown_clear(src):
    """该源刚刚有请求成功，恢复正常使用。"""
    _cooldown.pop(src, None)


def cooldown_start(src):
    _cooldown[src] = time.monotonic() + SOURCE_COOLDOWN
