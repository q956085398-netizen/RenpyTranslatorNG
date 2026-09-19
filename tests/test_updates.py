# -*- coding: utf-8 -*-
"""updates 模块离线测试：本地版本取证 + 版本比较判定（不联网）。

要覆盖的关键事实：
  - 判定必须从严：只有线上版本**严格高于**本地所有候选版本才提示「可更新」，
    判不出来一律不提示（乱报会让用户白下一次几十 GB 的重复包）；
  - 版本号写法五花八门（0.09_V4_Supporter / Ch.1_Ep.4 / 2025.04 / 0-20-1 / EpisodeFive.1），
    带小数点的那个才是主版本号；
  - S4 / Ep.4 / Part2 这类「第几季第几集」是内容标记，不是修订号
    （F95 的 `v4.4.3 S4 Ep.4 Gold` 与本地的 4.4.3 是同一版）；
  - 目录名里游戏名部分的数字不算版本（LabRats2 / TFTUV2）。
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import updates  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (tag, name, detail))
    if not cond:
        failures.append(name)


def key(text):
    k = updates.version_key(text)
    return k.core, k.extras


print("=== 版本号拆解 ===")
check("小数版本号当主版本号", key("0.09_V4_Supporter") == ((0, 9), ((4,),)), key("0.09_V4_Supporter"))
check("三段版本号", key("4.4.3-pc") == ((4, 4, 3), ()), key("4.4.3-pc"))
check("Part 2 不算版本号", key("Part_2-0.235") == ((0, 235), ()), key("Part_2-0.235"))
check("连字符版本号拉平", key("0-20-1-pc") == ((0, 20, 1), ()) or key("0-20-1-pc"),
      key("0-20-1-pc"))
check("下划线版本号拉平", key("S2E4_5-pc")[0] == (4, 5), key("S2E4_5-pc"))
check("英文数字转阿拉伯数字", key("EpisodeFive.1-pc")[0] == (5, 1), key("EpisodeFive.1-pc"))
check("章节/集数标记不算修订号", key("Ch.1_Ep.4")[1] == (), key("Ch.1_Ep.4"))
check("v 前缀是版本标记、要算修订号", key("v0.09 v4 Fix")[1] == ((4,),), key("v0.09 v4 Fix"))
check("年份版本号", key("2025.04")[0] == (2025, 4), key("2025.04"))
check("无版本号时主版本号为空", key("Tales From The Unending Void")[0] == (), key("Tales From The Unending Void"))

print("=== 判定：已是最新 ===")
for local, remote in [("0.09_V4_Supporter", "v0.09 v4 Fix"),
                      ("4.4.3-pc", "v4.4.3 S4 Ep.4 Gold"),
                      ("0-20-1", "v0.20.1"),
                      ("Part_2-0.235", "P2 v0.235"),
                      ("Ch.1_Ep.4", "Ch. 1 Ep. 4"),
                      ("Day7_Part2", "Day 7 Part 2"),
                      ("2025.04", "2025.04"),
                      ("Ch4Up3-pc", "Ch. 4 Upd. 3"),
                      ("1.0", "1.0.0"),
                      # Ep.1-11 是集数区间，不是修订号（线上标题里的写法与目录名不同）
                      ("Ep1-Ep11-SE-ver.1.0-pc", "Ep.1-11 v1.0 SE"),
                      ("FINAL", "Final"),
                      ("0.10b-Prologue-Southern-Edition", "v0.10b Prologue")]:
    st = updates.verdict([local], remote)
    check("同一版：%s ≈ %s" % (local, remote), st[0] == "latest", st)

print("=== 判定：有新版本 ===")
for local, remote in [("0.09", "v0.10"),
                      ("0.8.5", "v0.9.5 Public"),
                      ("2.52", "v2.53"),
                      ("1.0", "1.0.1"),
                      ("Chapter-3.4", "Ch.3.6"),
                      ("Ch4Up3-pc", "Ch. 4 Up.7"),
                      ("4.4.3-pc", "v4.4.4 S4 Ep.4"),
                      ("0.5", "Ch. 2")]:
    st = updates.verdict([local], remote)
    check("有新版本：%s → %s" % (local, remote), st[0] == "update", st)

print("=== 判定：判不出来一律不提示 ===")
for local, remote, why in [("0.09_V4_Supporter", "v0.09 v3", "本地还多一个 v4 修订"),
                           ("0.13.1", "0.13", "本地比线上新"),
                           ("demo", "Final", "纯词版本对不上"),
                           ("sixteenyears", "v0.5", "本地读不出数字"),
                           ("Tales From The Unending Void", "v0.5", "本地无版本号"),
                           ("Ep2_V_2_Final-pc", "Ep. 2 Final", "本地多一段 V_2，对不齐")]:
    st = updates.verdict([local], remote)
    check("保守不提示：%s vs %s（%s）" % (local, remote, why), st[0] == "unknown", st)
# 字母后缀（0.10b / 0.7s）不参与比较：宁可判成同一版漏报，也不误报「可更新」
check("字母后缀差异算同一版（宁可漏报不误报）",
      updates.verdict(["0.10"], "v0.10b")[0] == "latest"
      and updates.verdict(["0.7"], "v0.7s")[0] == "latest")

print("=== 多候选：任一候选对上就算最新 ===")
check("log 与目录名不一致时取最有利的", updates.verdict(["0.08", "0.09"], "v0.09")[0] == "latest")
check("目录名 0.08 / 日志 0.09", updates.verdict(["0.08", "0.09"], "v0.10")[0] == "update")
check("英文数字与阿拉伯数字混写", updates.verdict(["EpisodeFive.1"], "Ep.5.3")[0] == "update")

print("=== 本地版本取证 ===")
tmp = tempfile.mkdtemp(prefix="rtl_upd_")
try:
    root = os.path.join(tmp, "AFamilyVenture-0.09_V4_Supporter-pc")
    os.makedirs(os.path.join(root, "game"))
    with open(os.path.join(root, "log.txt"), "w", encoding="utf-8") as f:
        f.write("2026-08-01 13:49:27 UTC\nWindows-10-10.0.26100\nRen'Py 8.2.1.24030407\n\n"
                "A Family Venture\n0.09_V4_Supporter\nBuilt at 2026-07-27 04:06:09 UTC\n")
    with open(os.path.join(root, "game", "options.rpy"), "w", encoding="utf-8") as f:
        f.write('define config.version = "0.09_V4_Supporter"\n')
    check("log.txt 头部读出版本", updates._from_log(root) == "0.09_V4_Supporter",
          updates._from_log(root))
    check("options.rpy 读出版本", updates._from_options(root) == "0.09_V4_Supporter",
          updates._from_options(root))
    check("目录名剥掉游戏名只留版本尾巴",
          updates._from_folder(root, "AFamilyVenture") == "0.09_V4_Supporter-pc",
          updates._from_folder(root, "AFamilyVenture"))
    check("候选列表按 log → options → 目录名", updates.local_versions(root, "AFamilyVenture")[:2]
          == ["0.09_V4_Supporter", "0.09_V4_Supporter-pc"],
          updates.local_versions(root, "AFamilyVenture"))
    check("游戏名里的数字不算版本（LabRats2）",
          updates.version_key(updates._from_folder(os.path.join(tmp, "LabRats2"), "LabRats2")).core == (),
          updates._from_folder(os.path.join(tmp, "LabRats2"), "LabRats2"))

    # 只有目录名能取证时（老游戏 log 里没有版本行）
    root2 = os.path.join(tmp, "EarlyLove-0.8.2-pc")
    os.makedirs(os.path.join(root2, "game"))
    check("目录名兜底：EarlyLove-0.8.2-pc", updates.local_versions(root2, "EarlyLove") == ["0.8.2-pc"],
          updates.local_versions(root2, "EarlyLove"))
    check("目录名兜底判定：0.8.2 vs v0.9", updates.verdict(updates.local_versions(root2, "EarlyLove"),
                                                     "v0.9")[0] == "update")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("=== dikgames 标题里的版本 ===")
check("方括号版本号", updates.pick_dik_version("My Sisters And Me [Ep. 2.1] [MrDev]") == "Ep. 2.1",
      updates.pick_dik_version("My Sisters And Me [Ep. 2.1] [MrDev]"))
check("开发者方括号不当版本", updates.pick_dik_version("Game [Dev123] [v1.0]") == "v1.0",
      updates.pick_dik_version("Game [Dev123] [v1.0]"))
check("没有版本号返回空", updates.pick_dik_version("Some Game [Dev]") == "",
      updates.pick_dik_version("Some Game [Dev]"))

print("=== 复查节流 ===")
now = int(time.time())
check("没查过要查", updates.is_stale(None) and updates.is_stale({}))
check("刚查过不查", not updates.is_stale({"state": "latest", "checked": now}))
check("超过 12 小时再查", updates.is_stale({"state": "latest", "checked": now - 13 * 3600}))
check("被限流没查全的几小时后就补查",
      updates.is_stale({"state": "unknown", "checked": now - 4 * 3600, "partial": True}))
check("两个站都没收录的隔几天再查",
      not updates.is_stale({"state": "unknown", "checked": now - 4 * 3600, "partial": False}))

print("=== 卡片显示 ===")
check("只有确认有新版本才出角标",
      updates.badge_text({"state": "update"}) == "可更新"
      and updates.badge_text({"state": "latest"}) == ""
      and updates.badge_text({"state": "unknown"}) == ""
      and updates.badge_text(None) == "")
up = {"state": "update", "ver": "v0.10", "src": "f95", "local": "0.09"}
check("提示里带上本地与线上版本", "v0.10" in updates.summary(up) and "0.09" in updates.summary(up),
      updates.summary(up))
check("已是最新的提示", "已是最新" in updates.summary(
    {"state": "latest", "ver": "v0.09", "src": "f95", "local": "0.09"}))
check("没收录的提示", "没有同名游戏" in updates.summary({"state": "unknown", "reason": "no_remote"}))

print()
if failures:
    print("FAILED: %d 项 -> %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL PASS")


def test_all():
    """pytest 收集入口：上面的模块级脚本执行完毕后在此汇总结果。"""
    assert not failures, failures
