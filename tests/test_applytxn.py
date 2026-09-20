# -*- coding: utf-8 -*-
"""工单 07 验收测试:事务式应用与恢复(core.applytxn)。

「应用到游戏」= 暂存生成 → 快速结构检查 → 统一替换 → 应用清单/恢复点。
断言全部落在外部行为(游戏目录的文件状态、清单/恢复点内容、任务摘要):

- 暂存、检查、替换各阶段注入失败:游戏目录保持应用前状态(无半应用);
- 崩溃在提交点之前(有事务日志、无应用清单):下次使用先回滚;
- 每次应用生成应用清单与恢复点,可查看差异并逐次还原;
- 应用后人名与关系词显示层、字体、语言入口随译文一并生效(无隐藏例外);
- 停用汉化只撤销工具管理的文件,玩家的文件与汉化项目资产原样保留;
- 应用返回可验证的任务摘要(成功、跳过、待检查数量)。

游戏目录树用 sha256 快照对比;game/fonts_ng_backup 是工具保留的本地字体
安全网(应用清单注明保留),对比时排除。
"""
import os
import shutil

import pytest

from core import applytxn, extract, pipeline, registry, tlgen, util
from tests import syntheng

GAMES_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "synthgames")
FONT_BYTES = b"CJK-TEST-FONT-BYTES-0123456789"


def make_project(tmp_path, monkeypatch):
    """登记一个合成游戏项目并完成提取;返回 (project, 任务清单)。"""
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(extract, "extract", syntheng.extract_for_test)
    game = os.path.join(str(tmp_path), "game")
    shutil.copytree(os.path.join(GAMES_SRC, "synth_basic"), game)
    reg = registry.Registry()
    try:
        pid = reg.create_project("Synth Basic", game)["id"]
    finally:
        reg.close()
    font = os.path.join(str(tmp_path), "cjk.ttf")
    with open(font, "wb") as f:
        f.write(FONT_BYTES)
    cfg = {"language": "chinese", "engine": {"context_lines": 2},
           "apply_font": True, "font_path": font, "apply_lang_entry": True,
           "translate_strings": True}
    p = pipeline.Project(cfg, game, pid)
    p.extract_tl(log=lambda m: None)
    dump = util.read_json(os.path.join(p.work, "dump.json"))
    jobs, _tl = tlgen.build_jobs(game, "chinese", dump, include_strings=True,
                                 context_lines=2)
    p.ensure_store(jobs=jobs, log=lambda m: None)
    return p, jobs


def write_assets(p, name_cn="艾琳"):
    util.write_json(os.path.join(p.work, "relations.json"), [
        {"name": "Eileen", "name_cn": name_cn, "gender": "f", "relation": "friend"}])
    util.write_json(os.path.join(p.work, "relation_words.json"),
                    [{"en": "sister", "cn": "姐姐"}])


def inject(p, jobs, text="【译】"):
    for j in jobs:
        if j.get("old"):
            p.store().set_human_translation(j["key"], text + j["old"])


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def tree(p):
    """游戏目录树(排除工具保留的字体本地安全网目录)。"""
    return {k: v for k, v in applytxn.snapshot_tree(p.game_base).items()
            if not k.startswith("game/fonts_ng_backup/")}


def script_path(p):
    return os.path.join(p.game_base, "game", "tl", "chinese", "script.rpy")


# ---------- 应用:统一生效 + 清单/恢复点 + 任务摘要 ----------

def test_apply_writes_all_assets_with_manifest(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    virgin = tree(p)

    s = p.apply_txn(log=lambda m: None)
    gamedir = os.path.join(p.game_base, "game")

    # 译文回填进游戏 tl
    assert 'e "【译】Welcome to the Synth Vale."' in read(script_path(p))
    # 人名与关系词显示层、字体、语言入口一并生效(无隐藏例外)
    helpers = read(os.path.join(gamedir, "zz_ng_text.rpy"))
    assert "'Eileen': '艾琳'" in helpers and "'sister': '姐姐'" in helpers
    assert 'Language("chinese")' in read(os.path.join(gamedir, "zz_ng_language.rpy"))
    assert read(os.path.join(gamedir, "fonts", "myfont.ttf")) == FONT_BYTES.decode("latin-1")
    assert os.path.isfile(os.path.join(gamedir, "fonts", "cjk.ttf"))
    # 物理替换前有游戏内备份
    bak = os.path.join(gamedir, "fonts_ng_backup", "fonts", "myfont.ttf")
    assert "PLACEHOLDER" in read(bak)
    assert tree(p) != virgin

    # 任务摘要可验证:成功(回填条数)、跳过(未译)、待检查数量
    n_jobs = sum(1 for j in jobs if j.get("old"))
    assert s["filled"] == n_jobs and s["untranslated"] == 0
    assert s["dropped"] == 0 and s["suggestions"] == 0
    assert s["fonts_replaced"] == 1
    assert s["files"]["lang_entry"] == 1 and s["files"]["text_helpers"] == 1
    assert s["files"]["font"] == 3          # tl 字体脚本 + 中文字体资产 + 物理替换的游戏字体
    assert s["files"]["tl"] >= 3            # script/common/补充骨架

    # 应用清单与恢复点
    applies = applytxn.list_applies(p.work)
    assert len(applies) == 1 and applies[0]["apply_id"] == s["apply_id"]
    owners = {e["owner"] for e in applies[0]["files"]}
    assert {"tl", "font", "lang_entry", "text_helpers"} <= owners
    entry = next(e for e in applies[0]["files"]
                 if e["path"] == "game/tl/chinese/script.rpy")
    assert entry["action"] == "modified"
    assert entry["before"] and entry["after"] and entry["before"] != entry["after"]
    backup = os.path.join(s["restore_point"], "backup", "game", "tl", "chinese",
                          "script.rpy")
    assert os.path.isfile(backup)      # 恢复点保存替换前字节
    # 暂存区是临时产物,应用结束即清理
    assert not os.path.exists(os.path.join(p.work, "apply_staging"))


def test_summary_reports_pending_checks(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    key = next(j["key"] for j in jobs if j["old"] == "The vale is quiet tonight.")
    p.store().set_human_translation(key, "夜晚很安静[nope]。")   # 原文没有的变量

    s = p.apply_txn(log=lambda m: None)
    assert s["dropped"] == 1 and s["untranslated"] == 1
    # 拒用的行回填英文原文(运行时必崩的译文绝不进游戏)
    assert "夜晚很安静[nope]。" not in read(script_path(p))
    assert '"The vale is quiet tonight."' in read(script_path(p))


# ---------- 故障注入:任何阶段失败,游戏目录保持应用前状态 ----------

def test_staging_failure_keeps_game_untouched(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.cfg["font_path"] = os.path.join(str(tmp_path), "missing.ttf")
    before = tree(p)

    with pytest.raises(applytxn.ApplyError):
        p.apply_txn(log=lambda m: None)
    assert tree(p) == before
    assert applytxn.list_applies(p.work) == []


def test_check_failure_keeps_game_untouched(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    before = tree(p)
    orig = applytxn._fill_staged

    def broken(targets, text_map, key_map):
        n = orig(targets, text_map, key_map)
        with open(targets[0], "a", encoding="utf-8") as f:
            # 无控制字符,但多出一行 -> 骨架结构被破坏,必须拦在替换之前
            f.write('\n    "多出来的一行"\n')
        return n

    monkeypatch.setattr(applytxn, "_fill_staged", broken)
    with pytest.raises(applytxn.ApplyCheckError):
        p.apply_txn(log=lambda m: None)
    assert tree(p) == before
    assert applytxn.list_applies(p.work) == []


def test_replace_failure_rolls_back(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    before = tree(p)
    orig = applytxn._install_file
    state = {"n": 0}

    def flaky(game_base, entry):
        state["n"] += 1
        if state["n"] == 3:
            raise OSError("注入:替换第 3 个文件时磁盘故障")
        return orig(game_base, entry)

    monkeypatch.setattr(applytxn, "_install_file", flaky)
    with pytest.raises(OSError):
        p.apply_txn(log=lambda m: None)
    assert tree(p) == before
    assert applytxn.list_applies(p.work) == []


def test_replace_failure_after_fonts_rolls_back_cleanly(tmp_path, monkeypatch):
    """故障点落在字体替换之后:连本次新建的游戏内字体备份目录也一并清掉。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    before = applytxn.snapshot_tree(p.game_base)   # 全量:失败的尝试不留任何痕迹
    orig = applytxn.fontpatch.replace_fonts_in_place

    def boom(gamedir, font_path, refs=None):
        orig(gamedir, font_path, refs)
        raise OSError("注入:字体替换完成后故障")

    monkeypatch.setattr(applytxn.fontpatch, "replace_fonts_in_place", boom)
    with pytest.raises(OSError):
        p.apply_txn(log=lambda m: None)
    assert applytxn.snapshot_tree(p.game_base) == before
    assert applytxn.list_applies(p.work) == []


def test_crash_before_manifest_is_recovered(tmp_path, monkeypatch):
    """提交点(应用清单)之前进程被杀:下次使用先按事务日志回滚。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    virgin = applytxn.snapshot_tree(p.game_base)
    real = util.write_json

    def fail_manifest(path, data):
        if os.path.basename(path) == applytxn.MANIFEST_NAME:
            raise OSError("注入:写应用清单前进程被杀死")
        return real(path, data)

    util.write_json = fail_manifest
    try:
        with pytest.raises(OSError):
            p.apply_txn(log=lambda m: None)
    finally:
        util.write_json = real
    # 替换已发生但未提交:没有应用清单
    assert applytxn.list_applies(p.work) == []
    assert applytxn.snapshot_tree(p.game_base) != virgin

    # 停用触发崩溃恢复:游戏目录完整回到应用前(含字体与其游戏内备份目录),
    # 然后新的应用照常成功
    msgs = []
    applytxn.deactivate(p.work, p.game_base, p.cfg, p.language, log=msgs.append)
    assert applytxn.snapshot_tree(p.game_base) == virgin
    assert any("中断" in m for m in msgs)
    s = p.apply_txn(log=lambda m: None)
    assert applytxn.list_applies(p.work)[0]["apply_id"] == s["apply_id"]
    assert '【译】' in read(script_path(p))


def test_stop_before_replace_touches_nothing(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    before = tree(p)
    s = p.apply_txn(log=lambda m: None, should_stop=lambda: True)
    assert s == {"stopped": True}
    assert tree(p) == before


# ---------- 恢复点:差异查看与逐次还原 ----------

def test_restore_returns_game_to_pre_apply_state(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    virgin = tree(p)
    s1 = p.apply_txn(log=lambda m: None)
    applied1 = tree(p)

    # 改一条译文 + 换人名译法再应用:显示层跟着刷新(无隐藏例外)
    write_assets(p, name_cn="艾琳二号")
    p.store().set_human_translation("S:Leave", "【改】离开。")
    s2 = p.apply_txn(log=lambda m: None)
    assert s2["apply_id"] != s1["apply_id"]
    content = read(script_path(p))
    assert 'new "【改】离开。"' in content
    assert "'Eileen': '艾琳二号'" in read(
        os.path.join(p.game_base, "game", "zz_ng_text.rpy"))
    applied2 = tree(p)
    assert applied2 != applied1

    # 差异报告:每个文件的动作、归属、前后哈希与当前状态
    report = applytxn.diff(p.work, p.game_base, s2["apply_id"])
    entry = next(r for r in report["files"]
                 if r["path"] == "game/tl/chinese/script.rpy")
    assert entry["action"] == "modified" and entry["owner"] == "tl"
    assert entry["backup"] and os.path.isfile(entry["backup"])
    assert entry["current_is_after"] is True

    # 还原最新一次:回到应用 2 之前(= 应用 1 之后);再还原应用 1:回到未应用
    applytxn.restore(p.work, p.game_base, s2["apply_id"], log=lambda m: None)
    assert tree(p) == applied1
    applytxn.restore(p.work, p.game_base, s1["apply_id"], log=lambda m: None)
    assert tree(p) == virgin
    # 恢复点保留,可反复查看差异
    assert len(applytxn.list_applies(p.work)) == 2


def test_apply_removes_stale_tl_files(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    # 旧版本提取留下的孤儿文件:块标识已不在当前提取结果里(字符串块会被
    # build_jobs 识别为受管内容,只有这种死标签文件才算旧文件)
    stale = os.path.join(p.game_base, "game", "tl", "chinese", "stale_old.rpy")
    with open(stale, "w", encoding="utf-8") as f:
        f.write("# 旧版本提取留下的孤儿文件\ntranslate chinese lbl_deadbeef:\n"
                '# e "An old line."\n    e "An old line."\n')

    s = p.apply_txn(log=lambda m: None)
    assert not os.path.exists(stale)
    entry = next(e for e in applytxn.list_applies(p.work)[0]["files"]
                 if e["path"] == "game/tl/chinese/stale_old.rpy")
    assert entry["action"] == "removed" and entry["owner"] == "tl"
    # 旧文件字节进恢复点,可还原
    backup = os.path.join(s["restore_point"], "backup", "game", "tl", "chinese",
                          "stale_old.rpy")
    assert os.path.isfile(backup)


# ---------- 停用汉化:只撤销工具管理的文件 ----------

def test_deactivate_removes_only_tool_files(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    # 玩家自己的文件:停用绝不触碰
    player = os.path.join(p.game_base, "game", "mymod.rpy")
    with open(player, "w", encoding="utf-8") as f:
        f.write("# player mod\n")
    # 提取阶段写的运行时过滤器:uipatch 改写脚本的运行时依赖,停用时保留
    dyntrans = os.path.join(p.game_base, "game", "zz_ng_dyntrans.rpy")
    assert os.path.isfile(dyntrans)

    before = tree(p)
    p.apply_txn(log=lambda m: None)
    p.apply_txn(log=lambda m: None)      # 两次应用:停用要撤销全部
    assert tree(p) != before

    s = applytxn.deactivate(p.work, p.game_base, p.cfg, p.language,
                            log=lambda m: None)
    # 回到应用前(提取骨架与运行时过滤器保留、玩家文件不动;物理替换的字体
    # 从游戏内备份还原,备份目录随之消费掉)
    assert tree(p) == before
    assert os.path.isfile(dyntrans)
    assert s["files"] >= s["restored_files"] > 0

    # 汉化项目及其资产保留:项目库译文、恢复点清单都在,再次应用即恢复
    assert p.store().currents()
    assert len(applytxn.list_applies(p.work)) == 2
    p.apply_txn(log=lambda m: None)
    assert '【译】' in read(script_path(p))
    assert os.path.isfile(os.path.join(p.game_base, "game", "zz_ng_text.rpy"))
