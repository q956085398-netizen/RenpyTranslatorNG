# -*- coding: utf-8 -*-
"""工单 09 界面逻辑测试：译文编辑对话框的自动保存（离屏，无显示环境）。

编辑页按文本出现位置加载与保存（pipeline.editor_rows × EditTranslationDialog）。
这里只测对话框的写路径行为——外部可观察结果是项目库里的译文记录：

- 输入停顿后自动保存为项目草稿（人工来源 + 确认位），关窗前再补一次落库；
- 本会话内的连续编辑对当前译文原位改写，不把每个输入停顿堆成版本历史
  （待检查计数不被自动保存污染）；
- 没有改动就关窗不写库。
"""
import os

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core import project_store, util
from ui.main import EditTranslationDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_app = QApplication.instance() or QApplication([])


def make_row(key, source, trans="", same_source=1, **extra):
    row = {"key": key, "kind": "say", "who": "e", "file": "script.rpy",
           "src_file": "script.rpy", "source": source, "anchor": "",
           "trans": trans, "origin": "model" if trans else "",
           "confirmed": False, "candidates": 0, "suggestions": 0,
           "same_source": same_source}
    row.update(extra)
    return row


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    s = project_store.ProjectStore("editor-dlg-test")
    s.sync_occurrences([
        {"occurrence_id": "lbl_a", "kind": "say", "source_text": "Hello.",
         "trans_source": "Hello.", "who": "e", "source_file": "script.rpy",
         "fingerprint": project_store.fingerprint("say", "script.rpy", "e", "Hello.")},
        {"occurrence_id": "lbl_b", "kind": "say", "source_text": "Hello.",
         "trans_source": "Hello.", "who": "m", "source_file": "script.rpy",
         "fingerprint": project_store.fingerprint("say", "script.rpy", "m", "Hello.")},
    ])
    return s


def test_typing_autosaves_as_human_draft(store):
    store.record_model_result("lbl_a", "模型译文。")
    saved = []
    dlg = EditTranslationDialog(store, make_row("lbl_a", "Hello.", "模型译文。"),
                                on_saved=lambda ks: saved.extend(ks))
    dlg.ed_trans.setPlainText("人工定稿。")
    QTest.qWait(dlg.SAVE_DEBOUNCE_MS + 400)      # 停顿 -> 自动落库
    cur = store.get_current("lbl_a")
    assert cur["text"] == "人工定稿。" and cur["source"] == "human" and cur["confirmed"]
    assert saved == ["lbl_a"]
    dlg.accept()
    assert dlg.saved_keys == ["lbl_a"]


def test_session_edits_rewrite_in_place_without_version_churn(store):
    store.record_model_result("lbl_a", "模型译文。")
    dlg = EditTranslationDialog(store, make_row("lbl_a", "Hello.", "模型译文。"))
    for text in ("人工", "人工定", "人工定稿。"):
        dlg.ed_trans.setPlainText(text)
        QTest.qWait(dlg.SAVE_DEBOUNCE_MS + 400)
    dlg.accept()
    # 每个停顿都落库了最终文本，但只有第一次接管产生一条旧版本候选
    assert store.get_current("lbl_a")["text"] == "人工定稿。"
    assert [c["text"] for c in store.candidates("lbl_a")] == ["模型译文。"]


def test_close_flushes_pending_draft(store):
    dlg = EditTranslationDialog(store, make_row("lbl_a", "Hello.", ""))
    dlg.ed_trans.setPlainText("关窗前没等到停顿的输入。")
    dlg.reject()                                  # Esc / 关闭：先落库再退出
    cur = store.get_current("lbl_a")
    assert cur["text"] == "关窗前没等到停顿的输入。"
    assert cur["source"] == "human" and cur["confirmed"]


def test_close_without_edits_writes_nothing(store):
    store.record_model_result("lbl_a", "模型译文。")
    dlg = EditTranslationDialog(store, make_row("lbl_a", "Hello.", "模型译文。"))
    dlg.accept()
    assert dlg.saved_keys == []
    assert store.get_current("lbl_a")["text"] == "模型译文。"
    assert store.get_current("lbl_a")["source"] == "model"
