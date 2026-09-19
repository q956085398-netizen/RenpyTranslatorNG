# -*- coding: utf-8 -*-
"""ratings 模块离线测试：名字匹配规则 + 站点页面解析 + 卡片显示文本。

不联网，只测纯函数。要覆盖的关键事实：
  - 匹配必须从严：扩写只认「查询词有多个词」，单词查询的扩写几乎都是别的游戏
    （Imperial 会撞上一堆 Imperial xxx）；
  - 查询词比标题长时只认「标题是查询词的开头」，否则 No More Money 会被
    从中间截出 More Money 之类的怪匹配；
  - F95 帖子页的评分在 JSON-LD 的 aggregateRating 里（搜索结果接口给不出人数）；
  - dikgames 有两套 10 分制评分：站点评分（带 Great/Fair 这类评语）与用户均分（带人数）。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import ratings  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, name, detail))
    if not cond:
        failures.append(name)


print("=== 名字匹配 ===")
check("完全一致（忽略大小写/标点）", ratings.match_rank("No More Money", "No More Money") == 0)
check("驼峰名归一化后一致", ratings.match_rank("Photo Hunt", "PhotoHunt") == 0)
check("标题是查询词的扩写", ratings.match_rank(
    "Sinful Summer", "Sinful Summer: A Tale of Forbidden Love") == 1)
check("扩写不认单词查询", ratings.match_rank("Imperial", "Imperial Chronicles") is None)
check("扩写要求整词对齐", ratings.match_rank("Early Love", "Early Lovers") is None)
check("标题是查询词的开头（版本尾巴）", ratings.match_rank(
    "Shut Up And Dance Special", "Shut Up and Dance") == 2)
check("季号也算版本尾巴", ratings.match_rank("Her Desire s3", "Her Desire") == 2)
check("标题只是查询词的中间段则不认", ratings.match_rank("No More Money", "More Money") is None)
check("续作不当作同一部", ratings.match_rank("Doors Part 2", "Doors") is None)
check("短词不参与模糊匹配", ratings.match_rank("Arya", "Ikaryaku") is None)
check("完全不相干", ratings.match_rank("Ripples", "Desert Stalker") is None)

print("=== 搜索词降级序列 ===")
q1 = ratings._queries("No More Money")
check("优先整串", q1[0] == "No More Money", str(q1))
check("包含去掉虚词的形态", "More Money" in q1, str(q1))
q2 = ratings._queries("Shut Up And Dance Special")
check("去虚词版整串在前", q2.index("Shut Up Dance") < 8, str(q2))
check("序列不过长", len(ratings._queries("A B C D E F G")) <= 8)

print("=== F95 登录 Cookie ===")
check("不配 Cookie 时不发该请求头", "Cookie" not in ratings.f95_headers(""))
check("配了 Cookie 就带上", ratings.f95_headers("xf_user=abc").get("Cookie") == "xf_user=abc")
check("Cookie 前后空白会被去掉",
      ratings.f95_headers("  xf_user=abc  ").get("Cookie") == "xf_user=abc")

print("=== F95 解析 ===")
F95_HTML = """<script type="application/ld+json">
{"@context":"http://schema.org/","@type":"Book","name":"VN - Ren&#039;Py - Eternum",
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.8",
                    "bestRating":"5","ratingCount":"1341"}}</script>"""
check("读到分数/满分/人数", ratings.parse_f95_html(F95_HTML) == (4.8, 5, 1341))
check("没有聚合评分返回 None", ratings.parse_f95_html("<html>no rating</html>") is None)

print("=== dikgames 解析 ===")
DIK_HTML = """
<div class="gp-rating-box-average-rating">
    <div class="gp-average-rating-score gp-rating-color-4">
        8.8
    </div>
    <div class="gp-average-rating-info">
        <div class="gp-average-rating-text">Average User Rating</div>
        <div class="gp-total-votes gp-current-rating">
            <span class="gp-count">1,234</span>  votes
        </div>
    </div>
</div>
<div id="gp-review-results-rating" class=" gp-rating-color-4">
    <div class="gp-site-rating">
        <div class="gp-rating-score">
            8.5
        </div>
        <span class="gp-rating-text">Great</span>
    </div>
</div>"""
site, label, user, votes = ratings.parse_dikgames_html(DIK_HTML)
check("站点评分 + 评语", (site, label) == (8.5, "Great"), "%s %s" % (site, label))
check("用户均分", user == 8.8)
check("投票人数带千位分隔", votes == 1234)

print("=== 卡片显示 ===")
f95 = {"src": "f95", "score": 4.72, "max": 5, "count": 1341, "title": "Ripples"}
dik = {"src": "dikgames", "site_score": 8.5, "site_label": "Great",
       "user_score": 8.8, "count": 220, "max": 10, "title": "Ripples"}
check("五星制折算成 5 星", abs(ratings.stars(f95) - 4.72) < 1e-6)
check("十分制折算成 5 星", abs(ratings.stars(dik) - 4.4) < 1e-6)
check("没有评分时 0 星", ratings.stars(None) == 0 and ratings.stars({}) == 0)
check("人数过万折算", ratings.format_count(28354) == "2.8 万", ratings.format_count(28354))
check("千位分隔", ratings.format_count(1341) == "1,341")
check("主文本用各源自己的进制", ratings.headline(f95).startswith("4.7")
      and ratings.headline(dik).startswith("8.8"),
      "%s | %s" % (ratings.headline(f95), ratings.headline(dik)))
check("提示里带来源与人数", "F95Zone" in ratings.summary(f95)
      and "1,341 人评分" in ratings.summary(f95), ratings.summary(f95))
check("dikgames 提示两套评分都在", "站点评分 8.5/10（Great）" in ratings.summary(dik)
      and "用户均分 8.8/10" in ratings.summary(dik), ratings.summary(dik))

print("=== 过期判定 ===")
now = int(time.time())
check("没抓过算过期", ratings.is_stale(None) and ratings.is_stale({}))
check("刚抓的不算过期", not ratings.is_stale({"src": "f95", "ts": now}))
check("超过 30 天算过期", ratings.is_stale({"src": "f95", "ts": now - 31 * 86400}))
check("兜底源的评分几天后就重试（好升级成 F95）",
      ratings.is_stale({"src": "dikgames", "ts": now - 4 * 86400})
      and not ratings.is_stale({"src": "dikgames", "ts": now - 2 * 86400}))

print()
if failures:
    print("FAILED: %d 项 -> %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL PASS")


def test_all():
    """pytest 收集入口：上面的模块级脚本执行完毕后在此汇总结果。"""
    assert not failures, failures
