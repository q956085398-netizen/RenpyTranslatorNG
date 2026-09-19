# -*- coding: utf-8 -*-
"""工单 05 验收测试：菜单任务 key 的生成与回填一致，多菜单节点不再冲突。

现代引擎（7.4–8.x）中菜单字幕经 strings 机制翻译（工单 02 的 Answer 已确认），
菜单字幕的出现位置标识就是 strings 任务 key（"S:<原文>"）。本组测试守住：

- dump 里的菜单节点不再生成按序号编号的 caption 任务（同一块中的多个菜单节点
  曾因序号从零重计而互相冲突，且该任务形态在真实引擎下不可达、回填也从不处理）；
- 生成与回填使用同一套 key：strings 任务 key 能精确回填 old/new 对；
- 任务 key 全局唯一（同一原文的多处出现合并为同一条 strings 任务）。
"""
import os

from core import tlgen


def make_game(tmp_path, tl_text, dump):
    """搭一个最小的 <game_base>/game/tl/<语言>/ 结构供 build_jobs / fill_translations 用，
    返回 game_base。"""
    tl_dir = tmp_path / "game" / "tl" / "chinese"
    tl_dir.mkdir(parents=True)
    (tl_dir / "script.rpy").write_text(tl_text, encoding="utf-8")
    return str(tmp_path)


def test_dump_menu_nodes_never_produce_caption_keys(tmp_path):
    """同一块中的两个菜单节点：旧实现按节点重计序号（bid:c0/c1...）必然冲突；
    菜单字幕只经 strings 语义成任务，key 不含序号、全局唯一。"""
    dump = {
        "shop_1a2b3c4d": {"filename": "script.rpy", "lineno": 10, "nodes": [
            {"type": "say", "who": "e", "what": "Where to?"},
            {"type": "menu", "captions": ["Start", "Leave"]},
            {"type": "menu", "captions": ["Look closer", "Hurry"]},
        ]},
    }
    tl = ("# script.rpy:10\n"
          "translate chinese shop_1a2b3c4d:\n"
          "\n"
          '    # e "Where to?"\n'
          '    e ""\n'
          "\n"
          "translate chinese strings:\n"
          "\n"
          '    old "Start"\n'
          '    new ""\n'
          "\n"
          '    old "Leave"\n'
          '    new ""\n'
          "\n"
          '    old "Look closer"\n'
          '    new ""\n'
          "\n"
          '    old "Hurry"\n'
          '    new ""\n'
          "\n")
    game = make_game(tmp_path, tl, dump)
    jobs, _files = tlgen.build_jobs(game, "chinese", dump)

    keys = [j["key"] for j in jobs]
    assert len(keys) == len(set(keys)), keys          # 任务 key 全局唯一
    assert not any(":c" in k for k in keys), keys     # 不再有按序号编号的 caption 任务
    by_key = {j["key"]: j for j in jobs}
    # 菜单字幕以 strings 语义成任务：对白块节点不再生成 caption 任务
    assert by_key["S:Start"]["kind"] == "string"
    assert by_key["S:Look closer"]["kind"] == "string"
    assert sum(1 for j in jobs if j["old"] == "Start") == 1


def test_strings_key_generation_matches_backfill(tmp_path):
    """生成与回填同一套 key：按 strings 任务 key 的译文精确回填 old/new 对，
    相同原文不同出现位置各有译法时互不串写。"""
    dump = {
        "north_11112222": {"filename": "script.rpy", "lineno": 1, "nodes": [
            {"type": "say", "who": "e", "what": "The gate opens."}]},
        "south_33334444": {"filename": "script.rpy", "lineno": 5, "nodes": [
            {"type": "say", "who": "e", "what": "The gate opens."}]},
    }
    tl = ("# script.rpy:1\n"
          "translate chinese north_11112222:\n"
          "\n"
          '    # e "The gate opens."\n'
          '    e ""\n'
          "\n"
          "# script.rpy:5\n"
          "translate chinese south_33334444:\n"
          "\n"
          '    # e "The gate opens."\n'
          '    e ""\n'
          "\n"
          "translate chinese strings:\n"
          "\n"
          '    old "Open"\n'
          '    new ""\n'
          "\n")
    game = make_game(tmp_path, tl, dump)
    jobs, tl_files = tlgen.build_jobs(game, "chinese", dump)
    by_key = {j["key"]: j for j in jobs}
    assert set(by_key) == {"north_11112222", "south_33334444", "S:Open"}

    # 同一英文原文的两个分支 + 一条 strings：各自独立译法，回填各归各位
    key_map = {"north_11112222": "北线的门开了。", "south_33334444": "南边的门开。",
               "S:Open": "开门"}
    text_map = {}
    replaced = tlgen.fill_translations(tl_files, text_map, key_map)
    assert replaced == 3
    content = (tmp_path / "game" / "tl" / "chinese" / "script.rpy").read_text(
        encoding="utf-8")
    assert 'e "北线的门开了。"' in content
    assert 'e "南边的门开。"' in content
    assert 'new "开门"' in content
