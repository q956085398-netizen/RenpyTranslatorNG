# -*- coding: utf-8 -*-
"""工单 05 验收测试：项目数据库（文本出现位置与译文记录）。

项目库是每个汉化项目一个 SQLite 数据库（ADR-0004），保存文本出现位置（对白、
菜单、strings 使用统一且稳定的标识）、译文记录、人工确认状态、候选译文版本与
来源信息。这里的断言全部落在对外行为上：

- 出现位置标识稳定：再次同步相同提取结果不产生任何变化（幂等）；
- 源文本或结构指纹变化后，旧译文降为迁移建议而不是当前译文；
- 相同英文原文的不同出现位置可保存不同译文（独立编辑/翻译/回填的基础）；
- 人工确认的译文不被模型结果覆盖，新结果只进入候选译文（ADR-0003）；
- 译文记录带来源（模型/人工/迁移/导入）与结构指纹。
"""
import os

import pytest

from core import project_store, util


@pytest.fixture
def store(tmp_path, monkeypatch):
    """每个测试一个独立项目库（数据库文件落在该测试的临时项目资产目录）。

    ProjectStore 每次操作使用短连接，不持有常驻句柄，无需显式关闭。"""
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    return project_store.ProjectStore("11111111-1111-1111-1111-111111111111")


def rec(oid, kind="say", text="Hello.", who="e", file="script.rpy",
        trans_source=None):
    """构造一条出现位置记录（与 pipeline._jobs 同步时的字段同构）。"""
    return {"occurrence_id": oid, "kind": kind, "source_text": text,
            "trans_source": trans_source or text, "who": who,
            "source_file": file,
            "fingerprint": project_store.fingerprint(kind, file, who, text)}


SAY_A = "lbl_12345678"       # 单节点对白块（引擎标识符即任务 key）
SAY_B = "lbl_87654321:s1"    # 多节点对白块的第 2 个节点
STR_A = "S:Leave"


# ---------- 项目库落位：按项目身份，一项目一库 ----------

def test_db_lives_in_project_store_dir_by_identity(store, tmp_path):
    expect = os.path.join(str(tmp_path), "work",
                          "11111111-1111-1111-1111-111111111111", "project.db")
    assert store.path == expect
    store.stats()  # 数据库文件在首次使用时创建（未用过的项目不落文件）
    assert os.path.isfile(expect)


def test_stores_of_different_projects_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    sa = project_store.ProjectStore("aaaaaaaa-1111-1111-1111-111111111111")
    sb = project_store.ProjectStore("bbbbbbbb-2222-2222-2222-222222222222")
    sa.sync_occurrences([rec(SAY_A)])
    sa.record_model_result(SAY_A, "A 项目的译文")
    assert sb.currents() == {}
    assert os.path.dirname(sa.path) != os.path.dirname(sb.path)


def test_store_persists_across_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    db_dir = os.path.join(str(tmp_path), "work", "p1")
    s1 = project_store.ProjectStore("p1")
    s1.sync_occurrences([rec(SAY_A)])
    s1.record_model_result(SAY_A, "你好。")
    s2 = project_store.ProjectStore("p1")
    assert s2.currents() == {SAY_A: "你好。"}
    assert os.path.isfile(os.path.join(db_dir, "project.db"))


# ---------- 出现位置同步：稳定、幂等、指纹变化降级 ----------

def test_sync_inserts_occurrences_and_is_idempotent(store):
    r1 = store.sync_occurrences([rec(SAY_A), rec(SAY_B), rec(STR_A, kind="string")])
    assert r1["added"] == 3 and r1["changed"] == 0 and r1["removed"] == 0
    # 再次同步相同提取结果：出现位置标识保持稳定，不产生任何变化
    r2 = store.sync_occurrences([rec(SAY_A), rec(SAY_B), rec(STR_A, kind="string")])
    assert r2 == {"added": 0, "changed": 0, "removed": 0, "unchanged": 3,
                  "demoted": 0}
    assert r2["demoted"] == 0


def test_sync_records_kind_who_source_and_fingerprint(store):
    store.sync_occurrences([rec(SAY_A), rec(STR_A, kind="string", who="")])
    occ = {o["occurrence_id"]: o for o in store.occurrences()}
    assert occ[SAY_A]["kind"] == "say" and occ[SAY_A]["who"] == "e"
    assert occ[STR_A]["kind"] == "string"
    assert occ[SAY_A]["source_text"] == "Hello."
    assert occ[SAY_A]["fingerprint"] == project_store.fingerprint(
        "say", "script.rpy", "e", "Hello.")


def test_source_text_change_demotes_current_to_suggestion(store):
    store.sync_occurrences([rec(SAY_A, text="Hello.")])
    store.record_model_result(SAY_A, "你好。")
    # 游戏更新：同一标识符下源文本变了（作者改写台词）
    store.sync_occurrences([rec(SAY_A, text="Hello there.")])
    assert store.currents() == {}
    sug = store.suggestions()
    assert len(sug) == 1
    assert sug[0]["occurrence_id"] == SAY_A
    assert sug[0]["text"] == "你好。"
    assert sug[0]["source_text"] == "Hello."          # 译文所针对的旧源文本
    assert "源文本" in (sug[0]["note"] or "")


def test_fingerprint_change_demotes_current_to_suggestion(store):
    store.sync_occurrences([rec(SAY_A, file="script.rpy")])
    store.record_model_result(SAY_A, "你好。")
    # 结构变化：同一台词出现在另一个文件（标识符不变、文本不变）
    moved = rec(SAY_A, file="chapter2.rpy")
    assert moved["fingerprint"] != rec(SAY_A, file="script.rpy")["fingerprint"]
    rep = store.sync_occurrences([moved])
    assert rep["changed"] == 1 and rep["demoted"] == 1
    assert store.currents() == {}
    assert len(store.suggestions()) == 1


def test_occurrence_missing_from_extraction_demotes_and_keeps_history(store):
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.record_model_result(SAY_A, "你好。")
    rep = store.sync_occurrences([rec(SAY_B)])   # SAY_A 从提取结果中消失
    assert rep["removed"] == 1 and rep["demoted"] == 1
    assert store.currents() == {}
    assert len(store.suggestions()) == 1
    # 幂等：再次同步不重复降级、不重复产生迁移建议
    rep2 = store.sync_occurrences([rec(SAY_B)])
    assert rep2["removed"] == 0 and rep2["demoted"] == 0
    assert len(store.suggestions()) == 1
    # 出现位置记录保留（历史可追溯），标记为不在当前提取中
    occ = {o["occurrence_id"]: o for o in store.occurrences()}
    assert occ[SAY_A]["active"] is False
    assert occ[SAY_B]["active"] is True


def test_reappearing_occurrence_is_reactivated_without_auto_restore(store):
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "你好。")
    store.sync_occurrences([rec(SAY_B)])          # 消失 -> 降级
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])  # 回来了（游戏回滚）
    occ = {o["occurrence_id"]: o for o in store.occurrences()}
    assert occ[SAY_A]["active"] is True
    # 降级过的译文不会自动恢复为当前译文，仍需用户确认
    assert store.currents() == {}
    assert len(store.suggestions()) == 1


def test_placeholder_occurrence_syncs_without_falsifying_report(store):
    """request_retranslation 对未同步位置登记的占位记录：真实提取到来时补全，
    不计为结构变化（对账数字不失真），也不触发降级。"""
    store.request_retranslation(SAY_A)
    rep = store.sync_occurrences([rec(SAY_A)])
    assert rep == {"added": 0, "changed": 0, "removed": 0, "unchanged": 1,
                   "demoted": 0}
    occ = {o["occurrence_id"]: o for o in store.occurrences()}
    assert occ[SAY_A]["source_text"] == "Hello."
    assert occ[SAY_A]["fingerprint"] == rec(SAY_A)["fingerprint"]
    # 再次同步照常幂等
    rep2 = store.sync_occurrences([rec(SAY_A)])
    assert rep2["unchanged"] == 1 and rep2["changed"] == 0


def test_sync_refreshes_trans_source_when_patch_content_changes(store):
    """ipatch 补丁内容变化只改翻译源文本（trans_source），锚点与指纹不变：
    静默刷新，不计结构变化、不降级。"""
    store.sync_occurrences([rec(SAY_A, text="锚点原文。",
                                trans_source="补丁后文本一")])
    store.record_model_result(SAY_A, "译文。")
    rep = store.sync_occurrences([rec(SAY_A, text="锚点原文。",
                                      trans_source="补丁后文本二")])
    assert rep == {"added": 0, "changed": 0, "removed": 0, "unchanged": 1,
                   "demoted": 0}
    occ = {o["occurrence_id"]: o for o in store.occurrences()}
    assert occ[SAY_A]["trans_source"] == "补丁后文本二"
    assert store.currents() == {SAY_A: "译文。"}


# ---------- 译文记录：来源、人工状态、候选版本 ----------

def test_model_result_becomes_current_with_provenance(store):
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "你好。")
    cur = store.get_current(SAY_A)
    assert cur["text"] == "你好。"
    assert cur["source"] == "model" and cur["confirmed"] is False
    assert cur["source_text"] == "Hello."             # 译文所针对的源文本
    assert cur["fingerprint"] == rec(SAY_A)["fingerprint"]


def test_model_result_replaces_unconfirmed_current_keeps_old_as_candidate(store):
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "第一版。")
    store.record_model_result(SAY_A, "第二版。")
    assert store.currents() == {SAY_A: "第二版。"}
    cands = store.candidates(SAY_A)
    assert [c["text"] for c in cands] == ["第一版。"]
    assert cands[0]["source"] == "model"


def test_model_result_never_overwrites_confirmed_human_translation(store):
    store.sync_occurrences([rec(SAY_A)])
    store.set_human_translation(SAY_A, "人工定稿。")
    store.record_model_result(SAY_A, "模型新译。")
    # 当前译文保持人工定稿；模型结果只进入候选译文
    cur = store.get_current(SAY_A)
    assert cur["text"] == "人工定稿。" and cur["confirmed"] is True
    assert [c["text"] for c in store.candidates(SAY_A)] == ["模型新译。"]


def test_same_model_result_does_not_duplicate_candidates(store):
    store.sync_occurrences([rec(SAY_A)])
    store.set_human_translation(SAY_A, "人工定稿。")
    store.record_model_result(SAY_A, "模型新译。")
    store.record_model_result(SAY_A, "模型新译。")
    assert len(store.candidates(SAY_A)) == 1


def test_human_translation_replaces_and_confirms(store):
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "模型初译。")
    store.set_human_translation(SAY_A, "人工修改。")
    cur = store.get_current(SAY_A)
    assert cur["text"] == "人工修改。" and cur["source"] == "human"
    assert cur["confirmed"] is True
    assert [c["text"] for c in store.candidates(SAY_A)] == ["模型初译。"]


def test_confirm_marks_current_as_human_confirmed(store):
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "模型初译。")
    store.confirm(SAY_A)
    cur = store.get_current(SAY_A)
    assert cur["confirmed"] is True and cur["source"] == "model"  # 来源如实保留
    store.record_model_result(SAY_A, "重译结果。")
    assert store.get_current(SAY_A)["text"] == "模型初译。"
    assert [c["text"] for c in store.candidates(SAY_A)] == ["重译结果。"]


def test_adopt_candidate_promotes_it_to_current(store):
    store.sync_occurrences([rec(SAY_A)])
    store.set_human_translation(SAY_A, "人工定稿。")
    store.record_model_result(SAY_A, "模型新译。")
    cand = store.candidates(SAY_A)[0]
    store.adopt_candidate(SAY_A, cand["id"])
    cur = store.get_current(SAY_A)
    assert cur["text"] == "模型新译。" and cur["confirmed"] is True
    assert [c["text"] for c in store.candidates(SAY_A)] == ["人工定稿。"]


def test_adopt_suggestion_after_user_confirmation(store):
    """迁移建议必须经用户确认才能重新成为当前译文（采用入口存在且生效）。"""
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "旧译文。")
    store.sync_occurrences([rec(SAY_A, text="新文本。")])  # 源文本变化 -> 迁移建议
    sug = store.suggestions()[0]
    store.adopt_candidate(SAY_A, sug["id"])
    cur = store.get_current(SAY_A)
    assert cur["text"] == "旧译文。" and cur["confirmed"] is True
    assert store.suggestions() == []
    # 采用过的记录不再是迁移建议，重复采用同一 id 会被拒绝
    with pytest.raises(ValueError):
        store.adopt_candidate(SAY_A, sug["id"])


# ---------- 相同原文、不同出现位置：互相独立 ----------

def test_same_text_at_different_occurrences_holds_different_translations(store):
    """相同英文原文的两个分支出现位置：独立编辑、独立记录。"""
    store.sync_occurrences([rec("river_a", text="The river remembers."),
                            rec("river_b", text="The river remembers.")])
    store.set_human_translation("river_a", "河水记得（上游）。")
    store.record_model_result("river_b", "河水记忆犹新。")
    assert store.currents() == {"river_a": "河水记得（上游）。",
                                "river_b": "河水记忆犹新。"}
    # 保护彼此独立：对 A 的重译请求不影响 B
    store.request_retranslation("river_a")
    assert store.get_current("river_b")["text"] == "河水记忆犹新。"


# ---------- 重译请求：人工译文保留，新结果进候选 ----------

def test_retranslation_request_on_confirmed_keeps_current_open_for_candidate(store):
    store.sync_occurrences([rec(SAY_A)])
    store.set_human_translation(SAY_A, "人工定稿。")
    store.request_retranslation(SAY_A)
    # 请求重译不立即改动当前译文（当前译文仍参与回填）
    assert store.get_current(SAY_A)["text"] == "人工定稿。"
    flagged = store.retranslation_requested()
    assert flagged == [SAY_A]
    # 模型结果到达：只进候选，人工定稿保持不动，请求标记清除
    store.record_model_result(SAY_A, "模型重译。")
    assert store.get_current(SAY_A)["text"] == "人工定稿。"
    assert [c["text"] for c in store.candidates(SAY_A)] == ["模型重译。"]
    assert store.retranslation_requested() == []


def test_retranslation_request_on_unconfirmed_allows_replacement(store):
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "第一版。")
    store.request_retranslation(SAY_A)
    store.record_model_result(SAY_A, "第二版。")
    assert store.currents() == {SAY_A: "第二版。"}
    assert [c["text"] for c in store.candidates(SAY_A)] == ["第一版。"]
    assert store.retranslation_requested() == []


# ---------- 缓存维护：丢弃与清空都绕不开人工保护 ----------

def test_drop_currents_removes_unconfirmed_but_spares_confirmed(store):
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.record_model_result(SAY_A, "模型 A。")
    store.set_human_translation(SAY_B, "人工 B。")
    dropped, protected = store.drop_currents([SAY_A, SAY_B])
    assert dropped == [SAY_A] and protected == [SAY_B]
    assert store.currents() == {SAY_B: "人工 B。"}
    # 被丢弃的未确认译文保留为候选版本，不静默消失
    assert [c["text"] for c in store.candidates(SAY_A)] == ["模型 A。"]


def test_clear_currents_keeps_confirmed_human_translations(store):
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.record_model_result(SAY_A, "模型 A。")
    store.set_human_translation(SAY_B, "人工 B。")
    store.clear_currents()
    assert store.currents() == {SAY_B: "人工 B。"}


# ---------- 旧缓存导入：只补空位，绝不覆盖 ----------

def test_import_currents_fills_only_missing_occurrences(store):
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.record_model_result(SAY_A, "库里已有。")
    rep = store.import_currents({SAY_A: "文件里的旧值。", SAY_B: "文件补进来的。",
                                 "S:不在提取结果里": "垃圾条目"})
    assert store.currents() == {SAY_A: "库里已有。", SAY_B: "文件补进来的。"}
    cur = store.get_current(SAY_B)
    assert cur["source"] == "import" and cur["confirmed"] is False
    # 幂等：重复导入不产生新记录
    rep2 = store.import_currents({SAY_B: "文件补进来的。"})
    assert rep2["imported"] == 0 and len(store.candidates(SAY_B)) == 0


def test_import_does_not_resurrect_demoted_translations(store):
    """镜像文件里的旧值不得把已降级为迁移建议的译文复活成当前译文。"""
    store.sync_occurrences([rec(SAY_A)])
    store.record_model_result(SAY_A, "旧译文。")
    store.sync_occurrences([rec(SAY_A, text="新文本。")])  # 源文本变化 -> 降级
    rep = store.import_currents({SAY_A: "旧译文。"})
    assert store.currents() == {}
    assert rep["imported"] == 0


def test_import_skips_occurrences_absent_from_latest_extraction(store):
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.sync_occurrences([rec(SAY_B)])  # SAY_A 从提取结果中消失（无译文）
    rep = store.import_currents({SAY_A: "旧缓存里的值。"})
    assert rep["imported"] == 0 and store.currents() == {}


# ---------- 汇总对账 ----------

def test_stats_reports_counts_for_reconciliation(store):
    store.sync_occurrences([rec(SAY_A), rec(SAY_B), rec(STR_A, kind="string")])
    store.record_model_result(SAY_A, "A。")
    store.set_human_translation(SAY_B, "B。")
    store.record_model_result(SAY_B, "B 重译。")   # 人工 -> 候选
    st = store.stats()
    assert st["occurrences"] == 3
    assert st["currents"] == 2
    assert st["confirmed"] == 1
    assert st["candidates"] == 1
    assert st["suggestions"] == 0


# ---------- 编辑页状态读取（工单 09）：当前译文明细、待检查计数、忽略候选 ----------

def test_current_records_expose_text_source_and_confirmed_flag(store):
    """编辑页状态列需要每条当前译文的来源与确认位（currents 只给文本不够）。"""
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.record_model_result(SAY_A, "模型 A。")
    store.set_human_translation(SAY_B, "人工 B。")
    recs = store.current_records()
    assert recs[SAY_A]["text"] == "模型 A。"
    assert recs[SAY_A]["source"] == "model" and recs[SAY_A]["confirmed"] is False
    assert recs[SAY_B]["text"] == "人工 B。"
    assert recs[SAY_B]["source"] == "human" and recs[SAY_B]["confirmed"] is True


def test_review_counts_group_pending_candidates_and_suggestions(store):
    """待检查计数按出现位置分组：候选（重译/冲突/旧版本）与迁移建议分开计。"""
    store.sync_occurrences([rec(SAY_A), rec(SAY_B), rec(STR_A, kind="string")])
    store.set_human_translation(SAY_A, "人工 A。")
    store.record_model_result(SAY_A, "冲突结果。")     # 人工确认 -> 候选
    store.record_model_result(SAY_B, "模型 B。")
    store.record_model_result(SAY_B, "模型 B 新。")    # 旧当前降为候选
    store.set_human_translation(STR_A, "界面文本。")
    # 源文本变化 -> 降级为迁移建议（其余出现位置照常在场，不受影响）
    store.sync_occurrences([rec(SAY_A), rec(SAY_B),
                            rec(STR_A, kind="string", text="改了。")])
    counts = store.review_counts()
    assert counts[SAY_A] == {"candidates": 1, "suggestions": 0}
    assert counts[SAY_B] == {"candidates": 1, "suggestions": 0}
    assert counts[STR_A] == {"candidates": 0, "suggestions": 1}


def test_dismiss_candidate_removes_only_that_pending_result(store):
    """忽略 = 删除一条已查看、决定不采用的候选/迁移建议；其余版本不受影响。"""
    store.sync_occurrences([rec(SAY_A)])
    store.set_human_translation(SAY_A, "人工定稿。")
    store.record_model_result(SAY_A, "冲突结果一。")
    store.record_model_result(SAY_A, "冲突结果二。")
    cands = store.candidates(SAY_A)
    assert len(cands) == 2
    victim = next(c for c in cands if c["text"] == "冲突结果一。")
    store.dismiss_candidate(SAY_A, victim["id"])
    assert [c["text"] for c in store.candidates(SAY_A)] == ["冲突结果二。"]
    # 当前译文不受忽略影响
    assert store.get_current(SAY_A)["text"] == "人工定稿。"
    # 迁移建议同样可忽略
    store.sync_occurrences([rec(SAY_A, text="新文本。")])
    sug = store.suggestions()[0]
    store.dismiss_candidate(SAY_A, sug["id"])
    assert [c["text"] for c in store.candidates(SAY_A)] == ["冲突结果二。"]
    assert store.suggestions() == []


def test_dismiss_candidate_rejects_current_translation(store):
    """忽略入口不得作用于当前译文（删掉它会丢用户成果）。"""
    store.sync_occurrences([rec(SAY_A)])
    store.set_human_translation(SAY_A, "人工定稿。")
    cur = store.get_current(SAY_A)
    with pytest.raises(ValueError):
        store.dismiss_candidate(SAY_A, cur["id"])
    assert store.currents() == {SAY_A: "人工定稿。"}


# ---------- 编辑会话原位改写与建议的范围查询（工单 09） ----------

def test_rewrite_human_if_current_rewrites_only_claimed_session_state(store):
    """会话内连续编辑原位改写；会话外/已被人接管的内容不走这条捷径。"""
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.set_human_translation(SAY_A, "人工一。")
    assert store.rewrite_human_if_current(SAY_A, "人工一。", "人工二。") is True
    cur = store.get_current(SAY_A)
    assert cur["text"] == "人工二。" and cur["confirmed"] and cur["source"] == "human"
    assert store.candidates(SAY_A) == []          # 原位改写不堆版本历史
    # 期望文本已不是当前内容（其他写入方接管过）-> 拒绝原位改写
    assert store.rewrite_human_if_current(SAY_A, "人工一。", "人工三。") is False
    assert store.get_current(SAY_A)["text"] == "人工二。"
    # 未确认的模型译文不适用原位改写（会话首次写入必须走标准入口）
    store.record_model_result(SAY_B, "模型 B。")
    assert store.rewrite_human_if_current(SAY_B, "模型 B。", "改写。") is False
    assert store.get_current(SAY_B)["source"] == "model"


def test_suggestions_scoped_to_one_occurrence(store):
    store.sync_occurrences([rec(SAY_A), rec(SAY_B)])
    store.set_human_translation(SAY_A, "A 的旧译文。")
    store.set_human_translation(SAY_B, "B 的旧译文。")
    store.sync_occurrences([rec(SAY_A, text="A 改了。"), rec(SAY_B, text="B 改了。")])
    only_a = store.suggestions(SAY_A)
    assert [s["text"] for s in only_a] == ["A 的旧译文。"]
    assert len(store.suggestions()) == 2
