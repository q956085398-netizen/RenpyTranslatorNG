# -*- coding: utf-8 -*-
"""工单 08 验收测试:外部 tl 变更检测(core.externaltl + pipeline 接入)。

断言全部落在外部行为(游戏目录的文件状态、项目库译文记录、应用清单留痕):

- 基线随每次应用自动保存;无外部变更时应用不被打扰(确认通道不被调用);
- 游戏 tl 被外部修改后,无确认通道的应用被阻止(绝不静默覆盖);
- 逐项导入:外部译文成为人工来源的当前译文,受人工译文保护(模型结果只进候选);
- 显式放弃:本次应用覆盖外部值,应用清单留痕(检测/导入/放弃计数);
- 恢复点还原与停用汉化是工具自身动作,不误报为外部译文变更;
- 工具侧的草稿修改(项目库改了还没应用)不是外部变更,不打扰。
"""
import json
import os

import pytest

from core import applytxn, externaltl
from tests.test_applytxn import inject, make_project, read, script_path, tree, write_assets

LOG = lambda m: None            # noqa: E731


def applied(jobs, text):
    """按 old 原文找任务 key;返回 (key, 应用后应写入的译文)。"""
    j = next(j for j in jobs if j["old"] == text)
    return j["key"], "【译】" + text


def external_edit(p, was, now):
    """在汉化工作台之外改写游戏 tl 里的一句译文(模拟外部编辑器)。

    只改代码行(非注释行):未译行的原文同时出现在注释锚点和代码行里,
    改注释会破坏位置锚点而不是改译文。
    """
    sp = script_path(p)
    out = []
    for ln in read(sp).split("\n"):
        if not ln.strip().startswith("#") and '"%s"' % was in ln:
            ln = ln.replace('"%s"' % was, '"%s"' % now, 1)
        out.append(ln)
    assert any(now in ln for ln in out), "游戏 tl 里找不到要改的译文行"
    with open(sp, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def fill_untranslated(p, anchor, text):
    """外部给未译行补译文:提取骨架的代码行是空串(原文在注释锚点里),
    找到锚点注释行后把下一个代码行的空串换成译文。"""
    sp = script_path(p)
    lines = read(sp).split("\n")
    for i, ln in enumerate(lines):
        if ln.strip().startswith("#") and '"%s"' % anchor in ln:
            for j in range(i + 1, min(i + 6, len(lines))):
                s = lines[j].strip()
                if s and not s.startswith("#"):
                    assert '""' in lines[j], "骨架代码行不是空串: %r" % lines[j]
                    lines[j] = lines[j].replace('""', '"%s"' % text, 1)
                    break
            else:
                raise AssertionError("锚点注释后没有代码行")
            break
    else:
        raise AssertionError("找不到锚点注释行: %s" % anchor)
    with open(sp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def import_all(message):
    """确认通道:全部可导入项选择导入,其余放弃。"""
    payload = json.loads(message.split(":", 1)[1])
    ids = [i["id"] for i in payload["items"] if i["importable"]]
    return json.dumps({"import": ids,
                       "discard": [i["id"] for i in payload["items"]
                                   if i["id"] not in ids]})


def discard_all(message):
    payload = json.loads(message.split(":", 1)[1])
    return json.dumps({"import": [], "discard": [i["id"] for i in payload["items"]]})


def never(message):
    raise AssertionError("无外部变更时不应该弹出确认(不打扰)")


def rebuild_jobs(p):
    from core import tlgen, util
    dump = util.read_json(os.path.join(p.work, "dump.json"))
    jobs, _tl = tlgen.build_jobs(p.game_base, p.language, dump,
                                 include_strings=True, context_lines=2)
    return jobs


# ---------- 基线与不打扰 ----------

def test_apply_saves_baseline_and_next_apply_is_quiet(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)

    base = externaltl.load_baseline(p.work)
    assert base["language"] == "chinese"
    entry = base["files"]["script.rpy"]
    assert entry["sha256"] and entry["keys"]
    key, text = applied(jobs, "Welcome to the Synth Vale.")
    assert entry["keys"][key]["text"] == text      # 基线记录工具实际写入的译文
    assert entry["keys"][key]["anchor"] == "Welcome to the Synth Vale."

    # 再次应用:没有外部变更,确认通道绝不被调用
    s = p.apply_txn(log=LOG, confirm=never)
    assert s["filled"] > 0


def test_tool_side_draft_is_not_external_change(tmp_path, monkeypatch):
    """用户在工具里改了项目库还没应用:这是项目草稿,不是外部译文变更。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    key, _text = applied(jobs, "The vale is quiet tonight.")
    p.store().set_human_translation(key, "【草稿】还没应用的新译法。")

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs)["items"]
    assert items == []
    s = p.apply_txn(log=LOG, confirm=never)     # 不打扰,直接应用草稿
    assert "【草稿】" in read(script_path(p))
    assert s["external"] == {"detected": 0, "imported": 0, "discarded": 0}


def test_unapplied_draft_before_first_apply_is_not_external_change(tmp_path, monkeypatch):
    """首次应用前(无基线)编辑页保存了草稿:游戏 tl 停留在翻译步骤写入的旧值上
    (旧值仍在项目库版本历史里),不误报为外部译文变更——工单 09 的编辑→应用
    路径,检测参照补上"工具记录过的历史文本"(known_texts)。"""
    from core import pipeline, tlgen
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    # 模拟翻译步骤的工具写入:把项目库当前译文回填进游戏 tl(尚未应用 -> 无基线)
    text_map, key_map = pipeline._expand_translations(jobs, p.store().currents())
    tl_files = sorted({os.path.join(p.game_base, "game", "tl", p.language, j["file"])
                       for j in jobs if j.get("file")})
    tlgen.fill_translations(tl_files, text_map, key_map)

    key, old_text = applied(jobs, "The vale is quiet tonight.")
    p.store().set_human_translation(key, "【编辑页草稿】夜很静。")   # 未应用的项目草稿
    assert old_text in read(script_path(p))                        # tl 还停在旧值

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs,
                                      known_texts=p.store().history_texts())["items"]
    assert items == []
    s = p.apply_txn(log=LOG, confirm=never)      # 首次应用不被自己的草稿打扰
    assert "【编辑页草稿】夜很静。" in read(script_path(p))
    assert s["external"] == {"detected": 0, "imported": 0, "discarded": 0}


# ---------- 检测与阻止直接覆盖 ----------

def test_external_edit_is_detected_and_blocks_apply(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    key, text = applied(jobs, "Welcome to the Synth Vale.")
    external_edit(p, text, "【手改】欢迎来到合成之谷。")

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs)["items"]
    assert len(items) == 1
    it = items[0]
    assert it["key"] == key and it["kind"] == "modified" and it["importable"]
    assert it["baseline"] == text
    assert it["current"] == "【手改】欢迎来到合成之谷。"
    assert it["anchor"] == "Welcome to the Synth Vale."

    # 没有确认通道(脚本/无界面调用):阻止直接覆盖
    with pytest.raises(externaltl.ExternalChangesError):
        p.apply_txn(log=LOG)
    assert "【手改】" in read(script_path(p))     # 游戏目录未被工具改动


def test_external_addition_to_untranslated_line(tmp_path, monkeypatch):
    """外部给未译行补了译文:基线记录的是英文原文,检测为新增。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    for j in jobs:
        if j.get("old") and j["old"] != "The vale is quiet tonight.":
            p.store().set_human_translation(j["key"], "【译】" + j["old"])
    p.apply_txn(log=LOG)      # The vale is quiet tonight. 未译,tl 里是英文原文
    external_edit(p, "The vale is quiet tonight.", "【外部补译】夜很静。")

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs)["items"]
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "added" and it["importable"]
    assert it["baseline"] == "The vale is quiet tonight."   # 未译状态(原文)
    assert it["current"] == "【外部补译】夜很静。"


def test_removed_translation_pair_reported_not_importable(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    key, text = applied(jobs, "The road is long, [player_name].")
    # 外部删掉该位置的整对行:块内结构是[全部注释行][全部代码行],把注释行与
    # 代码行各删一条,其余位置配对不受影响
    sp = script_path(p)
    out = []
    for ln in read(sp).split("\n"):
        s = ln.strip()
        if s.startswith("#") and '"The road is long, [player_name]."' in ln:
            continue          # 注释行(原文锚点)
        if not s.startswith("#") and '"%s"' % text in ln:
            continue          # 代码行(译文)
        out.append(ln)
    with open(sp, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs)["items"]
    it = next(i for i in items if i["key"] == key)
    assert it["kind"] == "removed" and not it["importable"]
    assert it["current"] is None and it["baseline"] == text


def test_untracked_tl_file_reported_discard_only(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    hand = os.path.join(p.game_base, "game", "tl", "chinese", "handmade.rpy")
    with open(hand, "w", encoding="utf-8") as f:
        f.write("translate chinese handmade_block:\n    # e \"Hand line.\"\n"
                '    e "手写的一行。"\n')

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs)["items"]
    it = next(i for i in items if i["file"] == "handmade.rpy")
    assert it["kind"] == "untracked" and not it["importable"]

    s = p.apply_txn(log=LOG, confirm=discard_all)
    assert not os.path.exists(hand)               # 按旧文件移除
    entry = next(e for e in applytxn.load_manifest(
        p.work, s["apply_id"])["files"] if e["path"].endswith("handmade.rpy"))
    assert entry["action"] == "removed"           # 字节进恢复点(工单 07 语义)


# ---------- 导入:人工来源 + 人工译文保护 ----------

def test_import_marks_human_and_is_protected(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    key, text = applied(jobs, "Welcome to the Synth Vale.")
    external_edit(p, text, "【手改】欢迎来到合成之谷。")

    s = p.apply_txn(log=LOG, confirm=import_all)
    assert s["external"] == {"detected": 1, "imported": 1, "discarded": 0}

    cur = p.store().get_current(key)
    assert cur["text"] == "【手改】欢迎来到合成之谷。"
    assert cur["source"] == "human" and cur["confirmed"]

    # 应用后游戏里就是外部译文(导入后回填同值,游戏目录无感变化)
    assert "【手改】欢迎来到合成之谷。" in read(script_path(p))
    # 人工译文保护(ADR-0003):模型重译只进候选,当前译文不动
    p.store().record_model_result(key, "【模型重译】合成之谷欢迎你。")
    cur = p.store().get_current(key)
    assert cur["text"] == "【手改】欢迎来到合成之谷。" and cur["confirmed"]
    assert any(c["text"] == "【模型重译】合成之谷欢迎你。" for c in p.store().candidates(key))

    # 应用清单留痕:计数 + 逐项明细(哪条被导入)
    m = applytxn.load_manifest(p.work, s["apply_id"])["external_changes"]
    assert m["detected"] == 1 and m["imported"] == 1 and m["discarded"] == 0
    assert m["items"] == [{"key": key, "file": "script.rpy", "kind": "modified",
                           "action": "imported"}]

    # 基线已随本次应用更新:再次应用不再打扰
    p.apply_txn(log=LOG, confirm=never)


def test_abort_apply_leaves_game_untouched(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    s1 = p.apply_txn(log=LOG)
    key, text = applied(jobs, "Welcome to the Synth Vale.")
    external_edit(p, text, "【手改】欢迎来到合成之谷。")

    s = p.apply_txn(log=LOG, confirm=lambda m: "abort")
    assert s.get("stopped")
    assert "【手改】" in read(script_path(p))          # 游戏目录保持现状
    assert p.store().get_current(key)["text"] == text  # 项目库也没动
    assert len(applytxn.list_applies(p.work)) == 1     # 没有新应用


# ---------- 显式放弃:覆盖 + 留痕 ----------

def test_discard_overwrites_and_traces_in_manifest(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    key, text = applied(jobs, "Welcome to the Synth Vale.")
    external_edit(p, text, "【手改】欢迎来到合成之谷。")

    s = p.apply_txn(log=LOG, confirm=discard_all)
    assert s["external"] == {"detected": 1, "imported": 0, "discarded": 1}
    content = read(script_path(p))
    assert "【译】Welcome to the Synth Vale." in content      # 工具译文生效
    assert "【手改】" not in content                          # 外部值被明确放弃后覆盖
    assert p.store().get_current(key)["text"] == text         # 项目库不受外部值影响
    m = applytxn.load_manifest(p.work, s["apply_id"])["external_changes"]
    assert m["discarded"] == 1 and m["items"][0]["action"] == "discarded"

    # 放弃并应用后基线更新:外部值不再是待处理变更
    assert externaltl.detect_changes(p.game_base, p.work, p.language,
                                     rebuild_jobs(p))["items"] == []


# ---------- 工具自身动作不误报 ----------

def test_restore_point_result_is_not_external_change(tmp_path, monkeypatch):
    """恢复点还原把游戏目录改回应用前:这是工具动作,靠应用清单对账不误报。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    s = p.apply_txn(log=LOG)

    # 直接走 applytxn.restore(不经过基线刷新包装):检测靠清单前后哈希对账
    applytxn.restore(p.work, p.game_base, s["apply_id"], log=LOG)
    jobs2 = rebuild_jobs(p)
    assert externaltl.detect_changes(p.game_base, p.work, p.language,
                                     jobs2)["items"] == []
    p.apply_txn(log=LOG, confirm=never)      # 再次应用也不打扰


def test_deactivate_refreshes_baseline(tmp_path, monkeypatch):
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)

    msgs = []
    p.deactivate(log=msgs.append)
    assert externaltl.detect_changes(p.game_base, p.work, p.language,
                                     rebuild_jobs(p))["items"] == []
    p.apply_txn(log=LOG, confirm=never)      # 停用后再应用,从零打扰


# ---------- 首次应用前与边界形态 ----------

def test_first_apply_detects_preexisting_translations(tmp_path, monkeypatch):
    """从未应用过(无基线)时以原文锚点为参照:游戏自带的既有译文也能被发现。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    key, _text = applied(jobs, "The vale is quiet tonight.")
    for j in jobs:                       # 只入库,不应用——模拟首次应用之前;
        if j.get("old") and j["key"] != key:   # 留一句未译,tl 里还是英文原文
            p.store().set_human_translation(j["key"], "【译】" + j["old"])
    fill_untranslated(p, "The vale is quiet tonight.", "【民间译文】夜很静。")

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs)["items"]
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "added" and it["importable"]
    assert it["baseline"] == "The vale is quiet tonight."

    s = p.apply_txn(log=LOG, confirm=import_all)
    cur = p.store().get_current(key)
    assert cur["text"] == "【民间译文】夜很静。" and cur["source"] == "human"
    assert "【民间译文】夜很静。" in read(script_path(p))
    assert s["external"]["imported"] == 1


def test_structural_shift_is_not_importable(tmp_path, monkeypatch):
    """外部改写注释锚点会让位置对不上号:整个文件只提供放弃,不提供逐项导入。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    sp = script_path(p)
    content = read(sp)
    assert '# e "Welcome to the Synth Vale."' in content
    with open(sp, "w", encoding="utf-8") as f:
        f.write(content.replace('# e "Welcome to the Synth Vale."',
                                '# e "Welcome (edited) to the Synth Vale."', 1))

    items = externaltl.detect_changes(p.game_base, p.work, p.language, jobs)["items"]
    script_items = [i for i in items if i["file"] == "script.rpy"]
    assert script_items and all(i["kind"] == "structure" and not i["importable"]
                                for i in script_items)
    s = p.apply_txn(log=LOG, confirm=discard_all)
    assert s["external"]["discarded"] >= 1
    assert "【译】Welcome to the Synth Vale." in read(script_path(p))


def test_import_with_bad_variable_gets_warning_note(tmp_path, monkeypatch):
    """外部译文含原文没有的 [变量]:可导入(存为人工译文),但确认通道里
    预先说明本次应用仍会拒用该行,不让"导入后显示英文"变成无解释的意外。"""
    p, jobs = make_project(tmp_path, monkeypatch)
    write_assets(p)
    inject(p, jobs)
    p.apply_txn(log=LOG)
    key, text = applied(jobs, "Welcome to the Synth Vale.")
    external_edit(p, text, "【手改】欢迎来到[不存在]的合成之谷。")

    seen = {}

    def confirm(message):
        payload = json.loads(message.split(":", 1)[1])
        seen["items"] = payload["items"]
        return import_all(message)

    s = p.apply_txn(log=LOG, confirm=confirm)
    it = next(i for i in seen["items"] if i["key"] == key)
    assert it["importable"] and "[变量]" in it["note"]
    cur = p.store().get_current(key)
    assert cur["text"] == "【手改】欢迎来到[不存在]的合成之谷。" and cur["confirmed"]
    # 运行时必崩的译文绝不进游戏(工单 07 不变量):该行显示英文
    assert "[不存在]" not in read(script_path(p))
    assert s["external"]["imported"] == 1
