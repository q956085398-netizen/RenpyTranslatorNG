# -*- coding: utf-8 -*-
"""RenpyTranslatorNG 主界面。"""
import difflib
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QThread, Signal, QRegularExpression, QEvent, QTimer
from PySide6.QtGui import (QBrush, QColor, QIcon, QLinearGradient, QPainter,
                           QPixmap, QRegularExpressionValidator, QRadialGradient,
                           QTextDocument)
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSplitter, QStackedWidget, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget)

from core.config import Config
from core.pipeline import Project, editor_row_state
from core import applytxn
from core import coordinator
from core import fontpatch
from core import library as gamelib
from core import migration
from core import relations as rel
from core import registry as projreg
from core import texttags
from core import updates as upd
from core.translator import DEFAULT_SYSTEM_PROMPT, fix_rpy_tags
from core.util import read_json, write_json

from . import theme
from .library import LibraryPage

_HL_SPAN = '<span style="background:#6e5aef;color:#ffffff;">%s</span>'

# 接口方案保存/还原的参数范围：换接口时这些都要跟着走，不必重新调一遍
PROFILE_KEYS = ("base_url", "api_key", "model", "temperature", "presence_penalty",
                "thinking", "concurrency", "batch_size", "context_lines", "timeout",
                "max_retry", "max_tokens", "max_prompt_tokens", "system_prompt")


def project_task(kind):
    """标记一个会修改当前汉化项目核心数据的后台步骤（_op_*）。

    被 _run 识别后先经项目任务协调器登记（core.coordinator：同一项目同一时间
    只允许一个此类任务）才启动；kind 用于任务摘要与冲突说明。漏标新步骤比
    漏登记更危险——统一在方法定义处显式声明。"""
    def deco(fn):
        fn._project_task_kind = kind
        return fn
    return deco


def apply_summary_msg(s):
    """应用事务器摘要 -> 界面/任务行消息(core.applytxn 统一格式 + 恢复点号)。"""
    if s.get("stopped"):
        return "已在应用前暂停（游戏目录未改动）"
    return "%s；恢复点 %s" % (applytxn.summary_message(s), s["apply_id"])


def _hl(text, kw):
    """转义 HTML 并把关键词高亮（英文不区分大小写）。"""
    text = text or ""
    if not kw:
        return html.escape(text)
    out, idx = [], 0
    for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
        out.append(html.escape(text[idx:m.start()]))
        out.append(_HL_SPAN % html.escape(m.group(0)))
        idx = m.end()
    out.append(html.escape(text[idx:]))
    return "".join(out)


def _ci_replace(text, kw, repl):
    """大小写不敏感地把 text 中的 kw 替换为 repl。"""
    if not kw:
        return text
    out, idx = [], 0
    for m in re.finditer(re.escape(kw), text or "", re.IGNORECASE):
        out.append(text[idx:m.start()])
        out.append(repl)
        idx = m.end()
    out.append(text[idx:])
    return "".join(out)


class RichTextDelegate(QStyledItemDelegate):
    """让 QTableWidget 单元格显示 HTML（用于关键词高亮）。"""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        if not text:
            return
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(4)
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        doc.setDefaultStyleSheet(
            "body { color: %s; }" % ("#ffffff" if sel else theme.token("TEXT")))
        doc.setHtml("<body>%s</body>" % text)
        painter.save()
        painter.setClipRect(opt.rect)
        painter.translate(opt.rect.topLeft())
        doc.setTextWidth(opt.rect.width())
        doc.drawContents(painter)
        painter.restore()


class NumEdit(QLineEdit):
    """数字填写框：替代 SpinBox——没有步进箭头、不响应滚轮，鼠标划过不会再误改数值。
    对外提供与 QSpinBox/QDoubleSpinBox 兼容的 setValue()/value() 接口。"""

    def __init__(self, kind=float, lo=0.0, hi=999999.0, default=0, tip=""):
        super().__init__()
        self.kind, self.lo, self.hi, self.default = kind, float(lo), float(hi), default
        pattern = r"-?\d*" if kind is int else r"-?\d*\.?\d*"
        self.setValidator(QRegularExpressionValidator(QRegularExpression(pattern)))
        self.setMaximumWidth(110)
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        if tip:
            self.setToolTip(tip)
        self.setValue(default)

    def setValue(self, v):
        v = max(self.lo, min(self.hi, float(v or 0)))
        self.setText(str(int(round(v))) if self.kind is int else "%g" % v)

    def value(self):
        s = self.text().strip()
        try:
            v = self.kind(s) if s else self.default
        except ValueError:
            v = self.default
        v = max(self.lo, min(self.hi, v))
        return int(round(v)) if self.kind is int else v


class FetchThread(QThread):
    """轻量后台任务（如拉取模型列表），不占用主流程 Worker。"""
    sig_done = Signal(bool, object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.sig_done.emit(True, self.fn())
        except Exception as e:
            self.sig_done.emit(False, str(e))


class Worker(QThread):
    sig_log = Signal(str)
    sig_prog = Signal(int, int)
    sig_done = Signal(bool, str)
    sig_confirm = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self._stop = False
        self._confirm_evt = None
        self._confirm_result = None

    def stop(self):
        self._stop = True

    def ask_confirm(self, message):
        """工作线程调用：向主线程请求用户确认并阻塞等待。"""
        import threading
        self._confirm_evt = threading.Event()
        self._confirm_result = None
        self.sig_confirm.emit(message)
        self._confirm_evt.wait()
        return self._confirm_result

    def answer_confirm(self, result):
        self._confirm_result = result
        if self._confirm_evt:
            self._confirm_evt.set()

    def run(self):
        try:
            msg = self.fn(self.sig_log.emit, lambda c, t: self.sig_prog.emit(c, t),
                          lambda: self._stop, self.ask_confirm)
            self.sig_done.emit(True, str(msg))
        except Exception as e:
            import traceback
            self.sig_log.emit(traceback.format_exc())
            self.sig_done.emit(False, str(e))


def _scroll_page(page):
    """把内容较高的页面套进滚动区，避免布局最小高度顶死窗口（无法缩小高度）。

    注意透明规则必须用「子代选择器」精确圈定滚动内容页——无选择器的裸声明
    会级联到全部后代，把日志/提示词控制台的深色背景也一并杀掉。"""
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.Shape.NoFrame)
    sc.setStyleSheet(
        "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
    sc.viewport().setAutoFillBackground(False)
    sc.setWidget(page)
    return sc


class GlassRoot(QWidget):
    """底板：纯色基底 + 几团低饱和柔光色斑，半透明卡片叠在上面形成层次
    （比纯色背景更有层次，代价为零——纯绘制；深浅色两套参数在 theme 里）。"""

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), theme.root_bg())
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        for cx, cy, k, color in theme.root_blobs():
            g = QRadialGradient(w * cx, h * cy, k * max(w, h))
            g.setColorAt(0.0, color)
            g.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
            p.setBrush(QBrush(g))
            p.drawRect(self.rect())
        p.end()


class RelationsDialog(QDialog):
    """第④步的确认框：把关系草表摊在眼前，当场核对、当场改，再决定用不用它。

    以前这里只报「生成了 N 个角色」，用户无法当场判断这张表对不对，只能中止流程、
    切到「③ 人物关系」页看完再重跑。现在表格内嵌且可直接编辑，改动在退出时写回
    relations.json——发现译名/称谓错了就地修正，不必打断流程。

    exec() 之后读 action：use / skip / abort（Esc 等同 abort）。save_path 为 None
    （拿不到工作目录）时只展示不落盘，save_error 里回传失败原因。
    """

    COLS = (("speaker", "说话人代号", 130), ("name", "人名", 130), ("name_cn", "中文名", 100),
            ("gender", "性别", 55), ("relation", "与主角关系(含长幼)", 220), ("note", "备注/语气", 200))

    def __init__(self, chars, save_path=None, parent=None):
        super().__init__(parent)
        self.action = "abort"
        self.save_path = save_path
        self.save_error = ""
        self._saved = False
        self._chars = [c for c in chars if isinstance(c, dict)]   # 模型偶发返回脏结构时只显示能显示的行
        self._table = None
        self.setWindowTitle("请确认人物关系")
        scr = QApplication.primaryScreen()
        g = scr.availableGeometry() if scr else None
        self.resize(min(1120, int(g.width() * 0.85)) if g else 1120,
                    min(700, int(g.height() * 0.85)) if g else 700)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        head = QLabel(
            "AI 从台词推断出 <b>%d</b> 个角色，下表就是接下来翻译要用的关系表。请核对称谓长幼"
            "（姐姐/妹妹、哥哥/弟弟 译错会让全篇称呼错位），发现不对直接双击单元格修改。"
            % len(self._chars))
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setWordWrap(True)
        v.addWidget(head)

        if self._chars:
            self._table = self._build_table(self._chars)
            v.addWidget(self._table, 1)
        else:
            v.addWidget(QLabel("（关系表为空，未提取到角色）", objectName="dim"), 1)

        tip = QLabel("双击单元格即可修改，改动在关闭本窗口时保存到关系表（与「③ 人物关系」页是同一张表）。"
                     "不用关系表也能翻译，只是称谓一致性差一些；选「先去审核」则暂停流程并把关系页翻到眼前。")
        tip.setObjectName("dim")
        tip.setWordWrap(True)
        v.addWidget(tip)

        row = QHBoxLayout()
        b_use = QPushButton("使用关系表，继续翻译")
        b_use.setObjectName("primary")
        b_use.setDefault(True)
        b_skip = QPushButton("本次不用关系表，直接翻译")
        b_abort = QPushButton("先去审核（暂停流程）")
        # 中止键放最左、主操作放最右：两者隔开，避免误点把流程停掉
        row.addWidget(b_abort)
        row.addStretch(1)
        row.addWidget(b_skip)
        row.addWidget(b_use)
        v.addLayout(row)
        for b, act in ((b_use, "use"), (b_skip, "skip"), (b_abort, "abort")):
            b.clicked.connect(lambda _=False, a=act: self._choose(a))

    def _choose(self, action):
        self.action = action
        self.accept()

    def done(self, result):
        """不管从哪个出口离开（三个按钮、Esc、右上角关闭），都先把表里的改动落盘。"""
        self._save()
        super().done(result)

    def _rows(self):
        """把表格读回关系表结构：以原条目为底，只覆盖六个可编辑字段，AI 多给的键不丢。"""
        if self._table is None:
            return list(self._chars)
        rows = []
        for r in range(self._table.rowCount()):
            c = dict(self._chars[r]) if r < len(self._chars) else {}
            for i, (key, _title, _w) in enumerate(self.COLS):
                item = self._table.item(r, i)
                c[key] = (item.text() if item is not None else "").strip()
            if c.get("speaker"):
                rows.append(c)
        return rows

    def _save(self):
        if self._saved or not self.save_path:
            return
        self._saved = True
        rows = self._rows()
        if rows == self._chars:
            return                      # 一个字没动，就不碰盘上的文件
        try:
            write_json(self.save_path, rows)
        except Exception as e:
            self.save_error = str(e)    # 交给调用方记进日志，不拦住确认流程

    def _build_table(self, chars):
        t = QTableWidget(len(chars), len(self.COLS))
        t.setHorizontalHeaderLabels([c[1] for c in self.COLS])
        t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                          | QAbstractItemView.EditTrigger.EditKeyPressed
                          | QAbstractItemView.EditTrigger.AnyKeyPressed)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        t.setWordWrap(True)
        t.verticalHeader().setVisible(False)
        hdr = t.horizontalHeader()
        for i, (_key, _title, w) in enumerate(self.COLS):
            t.setColumnWidth(i, w)
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        # 备注列最长、且逐行换行撑高行距：让它吃掉剩余宽度，行高才不至于失控
        hdr.setSectionResizeMode(len(self.COLS) - 1, QHeaderView.ResizeMode.Stretch)
        for r, c in enumerate(chars):
            for i, (key, _title, _w) in enumerate(self.COLS):
                t.setItem(r, i, QTableWidgetItem(str(c.get(key) or "")))
        t.resizeRowsToContents()
        return t


class ApplyPointsDialog(QDialog):
    """恢复点管理（工单 07）：查看每次应用的差异、还原到应用前、一键停用汉化。"""

    ACTION_LABELS = {"added": "新增", "modified": "修改", "removed": "移除"}
    OWNER_LABELS = {"tl": "译文", "font": "字体", "lang_entry": "语言入口",
                    "text_helpers": "人名与关系词显示层", "legacy_cleanup": "旧版遗留"}

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.setWindowTitle("恢复点 / 停用汉化")
        self.resize(900, 600)
        self.main = main
        p = main._project()
        self.work, self.game_base = p.work, p.game_base
        self.applies = applytxn.list_applies(self.work)

        lv = QVBoxLayout(self)
        hint = QLabel("每次「应用到游戏」生成一个恢复点（应用清单 + 替换前字节）。"
                      "还原建议从最新开始连续撤销；停用汉化会按全部清单依次撤销并清理"
                      "工具文件——汉化项目与译文资产不受影响，再次应用即恢复。")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        lv.addWidget(hint)

        split = QSplitter()
        self.lst = QListWidget()
        if not self.applies:
            self.lst.addItem("尚无应用记录（执行一次「应用到游戏」后生成）")
        for m in self.applies:
            s = m.get("summary") or {}
            self.lst.addItem("应用 %s   回填 %s 条 · 文件 %d 个%s" % (
                m["apply_id"], s.get("filled", "?"), len(m.get("files") or []),
                "（最新）" if m is self.applies[0] else ""))
        self.lst.currentRowChanged.connect(self._load_files)
        split.addWidget(self.lst)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["文件（相对游戏目录）", "动作", "归属"])
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl.setColumnWidth(1, 70)
        self.tbl.setColumnWidth(2, 160)
        self.tbl.itemSelectionChanged.connect(self._show_diff)
        rv.addWidget(self.tbl, 2)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setPlaceholderText("选中一个文件查看差异（应用前 ←→ 当前）")
        rv.addWidget(self.diff, 3)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 640])
        lv.addWidget(split, 1)

        row = QHBoxLayout()
        btn_restore = QPushButton("⤺ 还原到此应用之前")
        btn_restore.setObjectName("primary")
        btn_restore.clicked.connect(self._restore)
        btn_off = QPushButton("⏹ 停用汉化")
        btn_off.clicked.connect(self._deactivate)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        row.addWidget(btn_restore)
        row.addWidget(btn_off)
        row.addStretch(1)
        row.addWidget(btn_close)
        lv.addLayout(row)
        if self.applies:
            self.lst.setCurrentRow(0)

    def _current(self):
        i = self.lst.currentRow()
        if 0 <= i < len(self.applies):
            return self.applies[i]
        return None

    def _load_files(self, row):
        self.tbl.setRowCount(0)
        self.diff.clear()
        m = self._current()
        if not m:
            return
        for e in m.get("files") or []:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            self.tbl.setItem(r, 0, QTableWidgetItem(e["path"]))
            self.tbl.setItem(r, 1, QTableWidgetItem(
                self.ACTION_LABELS.get(e["action"], e["action"])))
            self.tbl.setItem(r, 2, QTableWidgetItem(
                self.OWNER_LABELS.get(e["owner"], e["owner"])))

    def _show_diff(self):
        self.diff.clear()
        m = self._current()
        rows = self.tbl.selectionModel().selectedRows()
        if not m or not rows:
            return
        e = (m.get("files") or [])[rows[0].row()]
        try:
            report = applytxn.diff(self.work, self.game_base, m["apply_id"])
            row = next(r for r in report["files"] if r["path"] == e["path"])
        except Exception:
            self.diff.setPlainText("差异信息读取失败：%s" % e["path"])
            return
        parts = ["%s（%s / %s）" % (e["path"],
                                   self.ACTION_LABELS.get(e["action"], e["action"]),
                                   self.OWNER_LABELS.get(e["owner"], e["owner"]))]
        if row.get("backup"):
            before = self._read_text(row["backup"])
            if row.get("current_is_after"):
                # 行级差异:应用前(恢复点备份) vs 当前(即该次应用后)
                diff = list(difflib.unified_diff(
                    before.splitlines(), self._read_text(row["current"]).splitlines(),
                    fromfile="应用前", tofile="应用后", lineterm=""))
                parts.append(self._head("差异（- 应用前 / + 应用后）"))
                parts.append("\n".join(diff) if diff else "（两个版本内容相同）")
            else:
                parts.append(self._head("应用前（恢复点备份）"))
                parts.append(before)
        else:
            parts.append(self._head("应用前：该文件是本次应用新增，此前不存在"))
        if not row.get("current"):
            parts.append("（该文件当前不在游戏目录中——已被还原、停用或移除）")
        elif not row.get("current_is_after") and not row.get("backup"):
            parts.append(self._head("当前游戏目录"))
            parts.append(self._read_text(row["current"]))
        self.diff.setPlainText("\n".join(parts))

    @staticmethod
    def _head(t):
        return "—— %s ——\n" % t

    @staticmethod
    def _read_text(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            return "（无法读取：%s）" % e

    def _restore(self):
        m = self._current()
        if not m:
            return
        ret = QMessageBox.question(
            self, "还原恢复点",
            "把游戏目录还原到应用 %s 之前？\n该次应用（以及其后未再撤销的应用成果）"
            "会被覆盖；恢复点本身保留。" % m["apply_id"],
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.accept()
        self.main._run(self.main._restore_op(m["apply_id"]))

    def _deactivate(self):
        ret = QMessageBox.question(
            self, "停用汉化",
            "停用当前游戏安装上的汉化？\n只撤销工具管理的文件（tl 译文、字体、语言入口、"
            "显示层脚本）；\n汉化项目、译文、恢复点全部保留，再次「应用到游戏」即恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.accept()
        self.main._run(self.main._op_deactivate)


class ExternalChangesDialog(QDialog):
    """外部译文变更处理（工单 08、ADR-0005）：应用前逐项比较、导入或明确放弃。

    导入 = 外部译文登记为人工来源的当前译文（受人工译文保护）；
    放弃 = 明确允许本次应用覆盖（应用清单留痕）；
    取消 = 中止应用，游戏目录保持现状。存在未逐项处理的变更时不能继续。
    """

    KIND_LABELS = {"modified": "改写译文", "added": "补了译文", "removed": "删了译文行",
                   "unmanaged": "不在任务清单的位置", "removed_file": "删了整个文件",
                   "untracked": "工具未管理的新文件",
                   "structure": "行结构被改动（位置配对不可靠）"}

    def __init__(self, payload, parent=None):
        super().__init__(parent)
        self.setWindowTitle("检测到外部译文变更")
        self.resize(1020, 640)
        self.items = payload.get("items") or []

        lv = QVBoxLayout(self)
        hint = QLabel("游戏 tl 文件在汉化工作台之外被修改（外部译文变更）。"
                      "勾选「导入」的条目会登记为人工来源的当前译文（受人工译文保护，"
                      "模型重译只进候选）；不勾选即明确放弃，本次应用将覆盖它"
                      "（应用清单留痕，可从恢复点找回）。取消则中止应用，游戏目录保持现状。")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        lv.addWidget(hint)

        self.tbl = QTableWidget(len(self.items), 6)
        self.tbl.setHorizontalHeaderLabels(
            ["导入", "文件（相对 tl 目录）", "原文", "工具译文（上次应用）",
             "游戏目录（外部修改）", "变更"])
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tbl.setColumnWidth(0, 46)
        self.tbl.setColumnWidth(1, 170)
        self.tbl.setColumnWidth(5, 120)
        self._boxes = []
        for r, it in enumerate(self.items):
            box = QCheckBox()
            box.setEnabled(bool(it.get("importable")))
            if not it.get("importable"):
                box.setToolTip("该变更没有可导入的译文（删除行/未管理位置/整文件），"
                               "只能放弃（应用后按工具状态覆盖）或取消")
            box.setChecked(bool(it.get("importable")))
            self._boxes.append(box)
            self.tbl.setCellWidget(r, 0, box)
            kind = self.KIND_LABELS.get(it.get("kind"), it.get("kind"))
            if it.get("note"):
                kind = "⚠ " + kind
            for col, val in ((1, it.get("file")), (2, it.get("anchor")),
                             (3, it.get("baseline")), (4, it.get("current")),
                             (5, kind)):
                cell = QTableWidgetItem(val if isinstance(val, str) else
                                        ("（不存在）" if col in (3, 4) else "—"))
                tip = val if isinstance(val, str) else ""
                if col == 5 and it.get("note"):
                    tip = (tip + "\n" if tip else "") + it["note"]
                cell.setToolTip(tip)
                self.tbl.setItem(r, col, cell)
        self.tbl.itemSelectionChanged.connect(self._show_diff)
        lv.addWidget(self.tbl, 3)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setPlaceholderText("选中一个条目查看比较（工具译文 ←→ 游戏目录外部修改）")
        lv.addWidget(self.diff, 2)

        row = QHBoxLayout()
        btn_all = QPushButton("全部导入")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none = QPushButton("全部放弃")
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_go = QPushButton("继续应用")
        btn_go.setObjectName("primary")
        btn_go.clicked.connect(self.accept)
        btn_cancel = QPushButton("取消（中止应用）")
        btn_cancel.clicked.connect(self.reject)
        for b in (btn_all, btn_none):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(btn_go)
        row.addWidget(btn_cancel)
        lv.addLayout(row)
        if self.items:
            self.tbl.selectRow(0)

    def _set_all(self, on):
        for box, it in zip(self._boxes, self.items):
            box.setChecked(on and it.get("importable"))

    def decisions(self):
        """逐项处理结果：{"import": [条目 id…], "discard": [条目 id…]}。"""
        imports = [it["id"] for box, it in zip(self._boxes, self.items)
                   if box.isEnabled() and box.isChecked()]
        return {"import": imports,
                "discard": [it["id"] for it in self.items
                            if it["id"] not in imports]}

    def _show_diff(self):
        self.diff.clear()
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return
        it = self.items[rows[0].row()]
        parts = ["%s（%s）｜出现位置：%s" % (
            it.get("file"), self.KIND_LABELS.get(it.get("kind"), it.get("kind")),
            it.get("key") or "—")]
        if it.get("kind") in ("removed_file", "untracked", "structure"):
            parts.append(it.get("note") or "文件级变更，没有可逐条比较的译文。")
            self.diff.setPlainText("\n".join(parts))
            return
        before = it.get("baseline")
        after = it.get("current")
        diff = list(difflib.unified_diff(
            (before or "").splitlines(), (after or "").splitlines(),
            fromfile="工具译文（上次应用）", tofile="游戏目录（外部修改）", lineterm=""))
        parts.append("—— 差异（- 工具译文 / + 外部修改）——")
        parts.append("\n".join(diff) if diff else "（两个版本内容相同）")
        self.diff.setPlainText("\n".join(parts))


class EditTranslationDialog(QDialog):
    """单条译文编辑（工单 09）：“修改此处”与“替换全部相同原文”是两个明确动作。

    - 修改即时自动保存为项目草稿（输入停顿 0.7s 落库，关窗时立即补存——
      切页、关窗、崩溃最多丢最后一次停顿内的输入），界面显示保存状态——
      保存草稿与应用到游戏是两个语义不同的动作，这里只做前者；
    - 译文只写当前出现位置：相同原文的其他位置互不影响；要统一译法用
      「替换全部 N 处相同原文」——显式批量操作，执行前展示影响范围；
    - 候选译文与迁移建议并排比较：采用（成为人工确认的当前译文）或忽略
      （删除这条待比较结果）。翻译任务运行期间与人工编辑冲突的结果、
      重译请求的结果都在这里处理；
    - 请求重译：下次翻译时单独重发这个出现位置（人工定稿保持回填，新结果
      作为候选到达，不会覆盖）。

    本会话内的连续编辑对当前译文原位改写（不把每个停顿都堆成版本历史），
    仅当当前译文仍是本会话上一次写入的内容时才原位改写——中途有其他写入方
    （如翻译任务）接管时退回标准人工译文入口，正确降级保留旧版本。
    """

    SAVE_DEBOUNCE_MS = 700
    SOURCE_LABELS = {"model": "模型", "human": "人工", "migration": "迁移", "import": "导入"}

    def __init__(self, store, row, siblings=None, on_saved=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.row = row                    # 编辑页数据行（pipeline.editor_rows 的元素）
        self.siblings = siblings or [row]  # 相同翻译源的全部出现位置（含本行）
        self.on_saved = on_saved          # 自动保存回执：主界面刷新受影响行与保存状态
        self.saved_keys = []              # 本对话框写过的出现位置（主界面据此刷镜像）
        self._claimed = False             # 本会话是否已把当前译文接管为人工
        self._loaded = row["trans"]       # 已落库的文本（与文本框比对判脏）
        self._dirty = False
        self.setWindowTitle("修改译文（出现位置：%s）" % row["key"])
        self.resize(780, 620)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        meta = ["%s · 说话人 %s" % ("对白" if row["kind"] == "say" else "界面文本",
                                    row["who"].strip('"') or "旁白"),
                "文件 %s" % row["file"]]
        if row["src_file"]:
            meta.append("脚本 %s" % row["src_file"])
        if row["same_source"] > 1:
            meta.append("相同原文共 %d 处（互不影响）" % row["same_source"])
        lb_meta = QLabel("　|　".join(meta))
        lb_meta.setObjectName("dim")
        lb_meta.setWordWrap(True)
        v.addWidget(lb_meta)

        v.addWidget(QLabel("原文："))
        self.ed_src = QPlainTextEdit()
        self.ed_src.setReadOnly(True)
        self.ed_src.setPlainText(row["source"])
        self.ed_src.setMaximumHeight(92)
        v.addWidget(self.ed_src)
        if row["anchor"] and row["anchor"] != row["source"]:
            lb_patch = QLabel("（这句被 ipatch 补丁改写过，补丁前：%s —— 译文按补丁后文本翻译）"
                              % row["anchor"])
            lb_patch.setObjectName("dim")
            lb_patch.setWordWrap(True)
            v.addWidget(lb_patch)

        v.addWidget(QLabel("译文（自动保存为项目草稿）："))
        self.ed_trans = QPlainTextEdit()
        self.ed_trans.setPlainText(row["trans"])
        v.addWidget(self.ed_trans, 3)
        self.lb_check = QLabel("")
        self.lb_check.setTextFormat(Qt.TextFormat.RichText)
        self.lb_check.setWordWrap(True)
        v.addWidget(self.lb_check)
        self.ed_trans.textChanged.connect(self._on_text_changed)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self.SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._flush_save)

        self.lb_save = QLabel("修改会自动保存为项目草稿（未应用到游戏）")
        self.lb_save.setObjectName("dim")
        v.addWidget(self.lb_save)

        row_btns = QHBoxLayout()
        btn_retrans = QPushButton("↻ 请求重译")
        btn_retrans.setToolTip(
            "对这个出现位置发起重译请求：下次执行第 5 步（或「重译未译句子」）时单独重新请求它。\n"
            "人工确认的译文保持当前并继续回填，新结果作为候选译文到达（在下面的待检查区比较）；\n"
            "未确认的模型译文会被新结果直接更新。")
        btn_retrans.clicked.connect(self._request_retranslate)
        row_btns.addWidget(btn_retrans)
        row_btns.addStretch(1)
        if row["same_source"] > 1:
            btn_all = QPushButton("替换全部 %d 处相同原文…" % row["same_source"])
            btn_all.setToolTip("把这份译文应用到相同原文的全部出现位置（显式批量操作，先展示影响范围）")
            btn_all.clicked.connect(self._replace_all)
            row_btns.addWidget(btn_all)
        btn_close = QPushButton("关闭")
        btn_close.setDefault(True)
        btn_close.clicked.connect(self.accept)
        row_btns.addWidget(btn_close)
        v.addLayout(row_btns)
        self._pending_box = None
        self._build_pending()

    # ---------- 自动保存（项目草稿） ----------

    def _on_text_changed(self):
        if self.ed_trans.toPlainText() == self._loaded:
            self._dirty = False
            self._save_timer.stop()
            return
        self._dirty = True
        self.lb_save.setText("正在输入…停顿后自动保存为项目草稿")
        self._revalidate()
        self._save_timer.start()

    def _flush_save(self):
        """把文本框当前内容落库（停顿触发 / 关窗触发）。"""
        if not self._dirty:
            return
        text = self.ed_trans.toPlainText()
        key = self.row["key"]
        # 会话首次写入（含打开前就存在的人工译文）走标准入口，旧值降为候选
        # 保留；此后本会话的连续编辑才原位改写（不堆版本历史）
        if not (self._claimed
                and self.store.rewrite_human_if_current(key, self._loaded, text)):
            self.store.set_human_translation(key, text)
        self._note_saved(text)

    def _note_saved(self, text):
        """落库后的会话状态收尾（自动保存与采用候选共用）。"""
        self._claimed = True
        self._loaded = text
        self._dirty = False
        if self.row["key"] not in self.saved_keys:
            self.saved_keys.append(self.row["key"])
        self._mark_saved()
        if self.on_saved is not None:
            self.on_saved([self.row["key"]])

    def _mark_saved(self):
        self.lb_save.setText("✓ 已自动保存为项目草稿 · %s（未应用到游戏；点「应用修改到游戏」生效）"
                             % time.strftime("%H:%M:%S"))

    def done(self, result):
        """任何出口（关闭按钮、Esc、右上角）都先把未落库的输入保存成草稿。"""
        self._save_timer.stop()
        self._flush_save()
        super().done(result)

    def _revalidate(self):
        """与回填同一套校验：提前发现"应用时会被拒用、该行显示英文"的译文。"""
        text = self.ed_trans.toPlainText()
        t = fix_rpy_tags(text)
        _t, bad = texttags.fix_interps(self.row["source"], t)
        if bad:
            msg = "⚠ 译文包含原文没有的 %s：应用时会拒用这一行、显示英文" % bad
        elif t.count("{}") != self.row["source"].count("{}"):
            msg = "⚠ 译文与原文的 {} 占位符数量不一致：应用时会拒用这一行、显示英文"
        else:
            self.lb_check.setText("")
            return
        self.lb_check.setText('<span style="color:%s;">%s</span>'
                              % (theme.token("STATE_ERR"), msg))

    # ---------- 待检查区：候选译文 / 迁移建议 ----------

    def _pending_items(self):
        return self.store.candidates(self.row["key"]) + \
            self.store.suggestions(self.row["key"])

    def _build_pending(self):
        if self._pending_box is not None:
            self.layout().removeWidget(self._pending_box)
            self._pending_box.deleteLater()
            self._pending_box = None
        items = self._pending_items()
        if not items:
            return
        box = QGroupBox("待检查（比较后采用，或忽略）")
        bv = QVBoxLayout(box)
        bv.setContentsMargins(10, 8, 10, 8)
        bv.setSpacing(8)
        for it in items:
            bv.addWidget(self._pending_item(it))
        self._pending_box = box
        # 插在保存状态标签之前（按钮行始终垫底）
        self.layout().insertWidget(self.layout().indexOf(self.lb_save), box)

    def _pending_item(self, it):
        """一条候选/迁移建议：译文 + 来源说明 + 采用/忽略。"""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        fv = QVBoxLayout(frame)
        fv.setContentsMargins(8, 6, 8, 6)
        fv.setSpacing(2)
        head = "%s译文 · %s" % ("迁移建议（旧译文，出现位置结构已变化）"
                               if it["status"] == "suggestion" else "候选译文",
                               self.SOURCE_LABELS.get(it["source"], it["source"]))
        if it.get("updated_at"):
            head += " · %s" % time.strftime("%m-%d %H:%M", time.localtime(it["updated_at"]))
        lb_head = QLabel(head)
        lb_head.setObjectName("dim")
        fv.addWidget(lb_head)
        lb_text = QLabel(it["text"] or "（空）")
        lb_text.setWordWrap(True)
        lb_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fv.addWidget(lb_text)
        if it.get("note"):
            lb_note = QLabel(it["note"])
            lb_note.setObjectName("dim")
            lb_note.setWordWrap(True)
            fv.addWidget(lb_note)
        row = QHBoxLayout()
        row.addStretch(1)
        btn_adopt = QPushButton("✔ 采用")
        btn_adopt.setToolTip("把这条结果采用为当前译文（人工确认，受人工译文保护）")
        btn_adopt.clicked.connect(lambda _=False, x=it: self._adopt(x))
        btn_drop = QPushButton("✕ 忽略")
        btn_drop.setToolTip("删除这条待比较结果（当前译文不受影响）")
        btn_drop.clicked.connect(lambda _=False, x=it: self._dismiss(x))
        row.addWidget(btn_adopt)
        row.addWidget(btn_drop)
        fv.addLayout(row)
        return frame

    def _adopt(self, item):
        self._save_timer.stop()
        self._dirty = False            # 文本框即将换成采用结果，不再保存旧输入
        self.store.adopt_candidate(self.row["key"], item["id"])
        self._note_saved(item["text"])  # 先更新会话状态，setPlainText 才不会被当成新输入
        self.ed_trans.setPlainText(item["text"])
        self._build_pending()

    def _dismiss(self, item):
        self.store.dismiss_candidate(self.row["key"], item["id"])
        self._build_pending()
        if self.on_saved is not None:
            self.on_saved([self.row["key"]])

    # ---------- 显式批量操作 / 重译请求 ----------

    def _replace_all(self):
        """替换全部相同原文：先展示影响范围，确认后才写入。"""
        self._flush_save()
        text = self.ed_trans.toPlainText()
        others = [r for r in self.siblings if r["key"] != self.row["key"]]
        differ = [r for r in others if (r["trans"] or "") != text]
        preview = "\n".join(
            "· %s（%s）：%s" % (r["who"].strip('"') or "旁白", r["file"],
                              (r["trans"] or "（未翻译）")[:46])
            for r in others[:8])
        if len(others) > 8:
            preview += "\n…其余 %d 处" % (len(others) - 8)
        box = QMessageBox(self)
        box.setWindowTitle("替换全部相同原文")
        box.setText("把这份译文应用到相同原文的全部 %d 个出现位置？\n"
                    "其中 %d 处的当前译文与这份不同（将被改写）。\n\n%s\n\n"
                    "替换立即写入项目草稿（登记为人工译文），点「应用修改到游戏」后生效。"
                    % (len(others) + 1, len(differ), preview))
        adopt_all = box.addButton("替换全部", QMessageBox.ButtonRole.YesRole)
        box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        box.exec()
        if box.clickedButton() is not adopt_all:
            return
        keys = []
        for r in self.siblings:
            self.store.set_human_translation(r["key"], text)
            if r["key"] not in keys:
                keys.append(r["key"])
        self.saved_keys = keys
        if self.on_saved is not None:
            self.on_saved(keys)
        self.accept()

    def _request_retranslate(self):
        ret = QMessageBox.question(
            self, "请求重译",
            "对这个出现位置发起重译请求？\n\n下次执行第 5 步（或「重译未译句子」）时会单独重新请求它：\n"
            "· 人工确认的译文保持当前并继续回填，新结果作为候选译文到达（待检查）；\n"
            "· 未确认的模型译文会被新结果直接更新。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.store.request_retranslation(self.row["key"])
        self.lb_save.setText("✓ 已登记重译请求 · %s（下次翻译时单独重发）"
                             % time.strftime("%H:%M:%S"))


class MainWindow(QMainWindow):
    sig_est = Signal(str)  # 工作线程里安全更新“统计工作量”标签

    def __init__(self):
        super().__init__()
        self.cfg = Config()
        self.worker = None
        self._lib_worker = None
        self._task_handle = None
        self._running_fn = None
        self._lib_running_fn = None
        self._fetch_threads = []
        self._edit_rows = None       # 编辑页数据：每个文本出现位置一行（pipeline.editor_rows）
        self._edit_shown = []        # 当前表格里实际显示的行（_edit_rows 的引用子集）
        self._edit_store = None
        self._edit_kw = ""
        self.setWindowTitle("RenpyTranslatorNG — Ren'Py 游戏汉化工具")
        self.resize(1180, 780)
        self.setMinimumSize(720, 520)   # 允许缩到比单页布局更小（内容页可滚动）
        self._build()
        self._load_fields()
        self.sig_est.connect(self.lb_estimate.setText)
        self._apply_theme()
        self._side_state("ok", "就绪")
        # 模型框是可编辑下拉框：默认只有右侧小箭头能弹出列表，点输入区只落光标。
        # 装上事件过滤器后，点击框体任意位置即弹出模型列表供直接选取。
        self._popup_combos = (self.cmb_model, self.cmb_scan_model)
        for c in self._popup_combos:
            c.installEventFilter(self)
            c.lineEdit().installEventFilter(self)
        # 游戏库版本检查：窗口显示后再动手（不拖慢启动），后台静默跑，不打扰使用。
        # 评分不做自动检查（点「⭐ 更新评分」手动查），版本这边只补查过期的。
        if self.cfg.get("auto_check_updates", True):
            QTimer.singleShot(1500, self.lib_page.auto_check)
        # 旧数据自动迁移：启动后扫描旧版 work/<游戏目录名>/ 数据，预览确认后导入
        QTimer.singleShot(600, self._offer_legacy_migration)

    # ---------- 布局 ----------
    PAGE_TITLES = {"flow": "流程", "glossary": "词汇表", "chars": "人物关系",
                   "editor": "译文修改", "settings": "设置", "library": "游戏库"}

    def _build(self):
        root = GlassRoot(objectName="root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        # 侧边栏：悬浮圆角面板 = 品牌 + 导航 + 底部状态卡
        side = QWidget(objectName="side")
        side.setFixedWidth(200)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 18, 14, 14)
        sv.setSpacing(4)
        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        icon_lb = QLabel()
        icon_lb.setFixedSize(38, 38)
        icon_lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "resources", "icon.png")
        if os.path.isfile(icon_path):
            pm = QPixmap(icon_path)
            if not pm.isNull():
                icon_lb.setPixmap(pm.scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
        logo_row.addWidget(icon_lb)
        brand_v = QVBoxLayout()
        brand_v.setSpacing(0)
        brand_v.addWidget(QLabel("RenpyTranslator", objectName="brand"))
        brand_v.addWidget(QLabel("NG · Ren'Py 8 汉化", objectName="brandsub"))
        logo_row.addLayout(brand_v, 1)
        sv.addLayout(logo_row)
        sv.addSpacing(16)
        self.nav_btns = []
        for key, text in [("flow", "① 流程"), ("glossary", "② 词汇表"),
                          ("chars", "③ 人物关系"), ("editor", "④ 译文修改"),
                          ("settings", "⚙ 设置"), ("library", "🎮 游戏库")]:
            if key == "settings":
                # 翻译管线（①~④）与工具页之间加一组细分隔线 + 留白
                sv.addSpacing(8)
                div = QFrame(objectName="navdiv")
                div.setFixedHeight(1)
                sv.addWidget(div)
                sv.addSpacing(8)
            b = QPushButton(text, objectName="nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._goto(k))
            sv.addWidget(b)
            self.nav_btns.append((key, b))
        sv.addStretch(1)

        # 底部状态卡（状态灯 + 应用名，参考现代桌面工具的侧栏状态区）
        status = QWidget(objectName="sidestatus")
        stv = QVBoxLayout(status)
        stv.setContentsMargins(12, 10, 12, 10)
        stv.setSpacing(2)
        self.lb_state = QLabel()
        self.lb_state.setTextFormat(Qt.TextFormat.RichText)
        stv.addWidget(self.lb_state)
        stv.addWidget(QLabel("RenpyTranslatorNG", objectName="dim"))
        stv.addWidget(QLabel("NG · " + self.cfg.get("language", "chinese"), objectName="dim"))
        sv.addWidget(status)
        outer.addWidget(side)

        # 右侧：顶部常驻工具条（页面标题 + 主题切换，不随页面切换消失）+ 页面栈
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        top = QWidget(objectName="topbar")
        th = QHBoxLayout(top)
        th.setContentsMargins(8, 2, 8, 10)
        self.lb_page = QLabel("", objectName="pagetitle")
        th.addWidget(self.lb_page)
        th.addStretch(1)
        self.btn_theme = QPushButton(objectName="iconbtn")
        self.btn_theme.setFixedSize(38, 38)
        self.btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_theme.clicked.connect(self._toggle_theme)
        th.addWidget(self.btn_theme)
        rv.addWidget(top)

        # 页面栈（顺序必须与 nav_btns 一致）
        self.stack = QStackedWidget()
        rv.addWidget(self.stack, 1)
        self.stack.addWidget(_scroll_page(self._page_flow()))
        self.stack.addWidget(self._page_glossary())
        self.stack.addWidget(self._page_chars())
        self.stack.addWidget(self._page_editor())
        self.stack.addWidget(_scroll_page(self._page_settings()))
        self.lib_page = LibraryPage(self)
        self.stack.addWidget(self.lib_page)
        outer.addWidget(right, 1)
        self._goto("flow")

    def _card(self, title=None):
        card = QWidget(objectName="card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(10)
        if title:
            t = QLabel(title)
            t.setObjectName("h2")
            v.addWidget(t)
        return card, v

    def _page_flow(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(22, 18, 22, 18)
        v.setSpacing(14)

        card, cv = self._card("游戏")
        row = QHBoxLayout()
        self.ed_game = QLineEdit(placeholderText="选择游戏根目录（含 game/ 与游戏 exe）")
        btn_launch = QPushButton("▶ 启动游戏")
        btn_launch.setToolTip("直接运行游戏根目录下的 exe，无需手动切换文件夹")
        btn_launch.clicked.connect(self._launch_game)
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._pick_game)
        row.addWidget(self.ed_game, 1)
        row.addWidget(btn)
        row.addWidget(btn_launch)
        cv.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("翻译语言目录名"))
        self.ed_lang = QLineEdit()
        self.ed_lang.setFixedWidth(120)
        row2.addWidget(self.ed_lang)
        row2.addStretch(1)
        self.btn_estimate = QPushButton("统计工作量")
        self.btn_estimate.clicked.connect(lambda: self._run(self._op_estimate))
        row2.addWidget(self.btn_estimate)
        self.btn_clearcache = QPushButton("清空译文缓存")
        self.btn_clearcache.setToolTip("删除本地译文进度，下次执行第5步将全文重新翻译\n（修正了人物关系表/词汇表后，需要重译时使用）")
        self.btn_clearcache.clicked.connect(self._clear_cache)
        row2.addWidget(self.btn_clearcache)
        self.lb_estimate = QLabel("")
        self.lb_estimate.setMinimumWidth(0)
        self.lb_estimate.setWordWrap(True)
        self.lb_estimate.setObjectName("dim")
        row2.addWidget(self.lb_estimate)
        cv.addLayout(row2)
        v.addWidget(card)

        card, cv = self._card("流程步骤（可单独执行，按顺序）")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.steps = []
        for i, (key, text, tip) in enumerate([
            ("unpack", "1. 解包资源", "解开 .rpa 打包的脚本"),
            ("decompile", "2. 反编译脚本", "rpyc → rpy（只处理 game/ 目录）"),
            ("extract", "3. 提取+生成tl", "运行一次游戏提取对白并生成翻译骨架"),
            ("scan", "4. AI关系草表", "用模型分析角色关系，生成后请在③页审核"),
            ("translate", "5. 翻译并回填", "调用 API 翻译全部文本并写入 tl\n自动续翻：已译过的行直接跳过，可随时暂停后接着翻"),
            ("finish", "6. 应用到游戏", "事务式统一应用：回填译文 + 中文字体 + 语言入口 + 角色中文名\n暂存生成→结构检查→统一替换；失败自动回滚，生成恢复点"),
        ]):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setMinimumHeight(64)
            b.clicked.connect(lambda _=False, k=key: self._run(getattr(self, "_op_" + k)))
            grid.addWidget(b, i // 3, i % 3)
            self.steps.append(b)
        cv.addLayout(grid)
        run_all = QPushButton("▶  一键全流程（1→6）")
        self.btn_runall = run_all
        run_all.setObjectName("primary")
        run_all.setMinimumHeight(48)
        run_all.clicked.connect(self._run_all)
        cv.addWidget(run_all)
        row_util = QHBoxLayout()
        self.btn_fill = QPushButton("⤵ 应用到游戏（试玩模式）")
        self.btn_fill.setToolTip(
            "不调用 API、不花钱：把当前已翻译的译文写入 tl，并统一应用字体、语言入口、"
            "人名与关系词显示层。\n事务式：暂存生成 → 结构检查 → 统一替换，任何阶段失败"
            "游戏目录保持应用前状态；\n每次应用生成恢复点，可随时还原或停用汉化。\n"
            "未翻译的行在游戏里显示英文原文——先试玩一部分，再决定是否继续翻译。")
        self.btn_fill.clicked.connect(lambda: self._run(self._op_fill))
        self.btn_retrans = QPushButton("↻ 重译未译句子")
        self.btn_retrans.setToolTip(
            "修复翻译失败：只把缓存里没有译文的句子重新发给 AI（已译内容绝不重复请求，最省钱），\n"
            "每句至多附前后各 1 句上下文；完成后自动回填。反复执行可逐步清零失败句。")
        self.btn_retrans.clicked.connect(lambda: self._run(self._op_retrans))
        self.btn_applypts = QPushButton("🧯 恢复点 / 停用汉化")
        self.btn_applypts.setToolTip(
            "查看每次应用的恢复点（哪些文件被谁改过、差异预览），可还原到某次应用之前，\n"
            "或一键停用汉化——只撤销工具管理的文件，汉化项目与译文资产全部保留。")
        self.btn_applypts.clicked.connect(self._open_apply_points)
        row_util.addWidget(self.btn_fill)
        row_util.addWidget(self.btn_retrans)
        row_util.addWidget(self.btn_applypts)
        row_util.addStretch(1)
        cv.addLayout(row_util)
        v.addWidget(card, 0)

        card, cv = self._card("进度 / 日志")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("")   # 数值显示在下方 lb_prog；内嵌文字在高进度时会落在紫色块上看不清
        cv.addWidget(self.progress)
        prow = QHBoxLayout()
        self.lb_prog = QLabel("空闲")
        self.lb_prog.setMinimumWidth(0)
        self.lb_prog.setObjectName("dim")
        prow.addWidget(self.lb_prog, 1)
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip("暂停当前任务。翻译进度实时保存，正在运行的请求返回后即停；\n之后可再执行第 5 步从剩余部分继续，不会重复翻译")
        self.btn_pause.clicked.connect(self._pause_worker)
        prow.addWidget(self.btn_pause)
        cv.addLayout(prow)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        cv.addWidget(self.log, 1)
        v.addWidget(card, 1)
        return page

    def _page_glossary(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(22, 18, 22, 18)
        card, cv = self._card("本地词汇表（强制约束译名，如人名/地名/特殊术语）")
        hint = QLabel("翻译时命中原文的词条会注入提示词；行内留空“译文”的行不生效。")
        hint.setObjectName("dim")
        cv.addWidget(hint)
        self.tbl_gloss = QTableWidget(0, 2)
        self.tbl_gloss.setHorizontalHeaderLabels(["原文 (English)", "译文 (中文)"])
        self.tbl_gloss.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        cv.addWidget(self.tbl_gloss, 1)
        row = QHBoxLayout()
        add = QPushButton("＋ 添加")
        add.clicked.connect(self._gloss_add)
        dele = QPushButton("－ 删除选中")
        dele.clicked.connect(self._gloss_del)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self._gloss_save)
        row.addWidget(add)
        row.addWidget(dele)
        row.addStretch(1)
        row.addWidget(save)
        cv.addLayout(row)
        v.addWidget(card, 1)
        return page

    def _page_chars(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(22, 18, 22, 18)
        v.setSpacing(14)
        card, cv = self._card("人物关系表（决定 姐姐/妹妹/哥哥/弟弟 等称谓的翻译一致性）")
        row = QHBoxLayout()
        self.btn_scan = QPushButton("🤖 用 AI 生成关系草表")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(lambda: self._run(self._op_scan))
        row.addWidget(self.btn_scan)
        self.lb_stats = QLabel("")
        self.lb_stats.setMinimumWidth(0)
        self.lb_stats.setObjectName("dim")
        row.addWidget(self.lb_stats, 1)
        cv.addLayout(row)
        self.tbl_chars = QTableWidget(0, 6)
        self.tbl_chars.setHorizontalHeaderLabels(["说话人代号", "人名", "中文名", "性别", "与主角关系(含长幼)", "备注/语气"])
        self.tbl_chars.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_chars.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        cv.addWidget(self.tbl_chars, 1)
        row2 = QHBoxLayout()
        add = QPushButton("＋ 添加")
        add.clicked.connect(self._char_add)
        dele = QPushButton("－ 删除选中")
        dele.clicked.connect(self._char_del)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self._char_save)
        row2.addWidget(add)
        row2.addWidget(dele)
        row2.addStretch(1)
        row2.addWidget(save)
        cv.addLayout(row2)
        v.addWidget(card, 3)

        card, cv = self._card("关系词显示表（游戏让玩家自填关系词时，台词里显示成什么）")
        hint = QLabel(
            "开场要求玩家输入关系词的游戏（如“那你是她的……”→ brother），台词里的 "
            "[relation2] 会插值出这个英文词，译文管不到，只能在这里定：想让台词显示“哥哥”"
            "就加一条 brother → 哥哥（长幼按本游戏剧本里的人物年龄填）。\n"
            "复数自动加「们」（[relation1]s → 哥哥们），所有格自动加「的」（[relation1]'s → 哥哥的）；"
            "玩家若在游戏里直接输入中文（如 哥哥），以他当场的输入为准。\n"
            "同一个英文词被两处提问问到、而玩家两次答得不一样时（如“苏菲是你的…”和“格蕾丝是苏菲的…”"
            "英文都是 sister），这句回答分不清该用在哪，按本表显示。\n"
            "保存后执行第 6 步（或「译文修改」页的「应用修改到游戏」）生效。")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        cv.addWidget(hint)
        self.tbl_words = QTableWidget(0, 2)
        self.tbl_words.setHorizontalHeaderLabels(["英文关系词", "中文显示"])
        self.tbl_words.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        cv.addWidget(self.tbl_words, 1)
        row3 = QHBoxLayout()
        wadd = QPushButton("＋ 添加")
        wadd.clicked.connect(self._word_add)
        wcommon = QPushButton("填入常用关系词")
        wcommon.setToolTip("把常用关系词（sister/brother/mother/daughter…）连默认中文一起列出来，"
                           "再把长幼改成本游戏的实际情况（如 brother → 弟弟）")
        wcommon.clicked.connect(self._word_common)
        wdel = QPushButton("－ 删除选中")
        wdel.clicked.connect(self._word_del)
        wsave = QPushButton("保存")
        wsave.setObjectName("primary")
        wsave.clicked.connect(self._word_save)
        row3.addWidget(wadd)
        row3.addWidget(wcommon)
        row3.addWidget(wdel)
        row3.addStretch(1)
        row3.addWidget(wsave)
        cv.addLayout(row3)
        v.addWidget(card, 1)
        return page

    def _page_editor(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(22, 18, 22, 18)
        card, cv = self._card("译文修改（按文本出现位置逐条校对）")
        hint = QLabel(
            "主列表的每一行是一个文本出现位置：同一句英文在不同人物或分支下可以有各自译文，"
            "互不影响。双击一行修改：编辑自动保存为项目草稿（未应用到游戏）；相同原文要统一译法时，"
            "在修改对话框里用「替换全部相同原文」（执行前展示影响范围）。下面填替换词可按关键词"
            "批量替换命中的译文。点「应用修改到游戏」才写入游戏目录。")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        cv.addWidget(hint)
        row = QHBoxLayout()
        self.ed_search = QLineEdit(placeholderText="关键词，如：哥哥、Guard、某人名…")
        self.cmb_scope = QComboBox()
        self.cmb_scope.addItems(["搜译文", "搜原文", "两者都搜"])
        self.cmb_status = QComboBox()
        self.cmb_status.addItems(["全部状态", "未翻译", "已有译文", "待检查", "人工确认"])
        self.cmb_status.setToolTip(
            "待检查 = 有候选译文（重译结果、翻译任务与人工编辑的冲突）或迁移建议"
            "（源文本/结构变化后降级的旧译文），双击打开比较后采用或忽略")
        self.cmb_status.currentIndexChanged.connect(lambda _: self._edit_search())
        self.btn_search = QPushButton("🔍 搜索")
        self.btn_search.setObjectName("primary")
        self.btn_search.clicked.connect(self._edit_search)
        self.ed_search.returnPressed.connect(self._edit_search)
        row.addWidget(self.ed_search, 1)
        row.addWidget(self.cmb_scope)
        row.addWidget(self.cmb_status)
        row.addWidget(self.btn_search)
        cv.addLayout(row)
        self.lb_search = QLabel("请先在①页选择游戏目录")
        self.lb_search.setWordWrap(True)
        self.lb_search.setObjectName("dim")
        cv.addWidget(self.lb_search)
        self.tbl_edit = QTableWidget(0, 4)
        self.tbl_edit.setHorizontalHeaderLabels(
            ["状态", "说话人", "原文 (English)", "译文（双击修改）"])
        self.tbl_edit.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_edit.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_edit.setColumnWidth(0, 84)
        self.tbl_edit.setColumnWidth(1, 96)
        self.tbl_edit.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_edit.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_edit.setWordWrap(True)
        self.tbl_edit.setItemDelegate(RichTextDelegate(self.tbl_edit))
        self.tbl_edit.verticalHeader().setDefaultSectionSize(26)
        self.tbl_edit.doubleClicked.connect(self._edit_dblclick)
        cv.addWidget(self.tbl_edit, 1)
        self.lb_save = QLabel("编辑会自动保存为项目草稿；「应用到游戏」才会修改游戏目录")
        self.lb_save.setObjectName("dim")
        self.lb_save.setWordWrap(True)
        cv.addWidget(self.lb_save)
        row2 = QHBoxLayout()
        self.ed_replace = QLineEdit(placeholderText="替换为…")
        btn_replace = QPushButton("一键替换全部")
        btn_replace.setToolTip("把所有译文中出现的关键词（上面搜索框里的词）替换为这里填的词\n"
                               "显式批量操作：执行前展示影响范围（多少处译文、多少处出现）")
        btn_replace.clicked.connect(self._edit_replace_all)
        self.btn_edit_fix = QPushButton("🔧 修复标签错乱")
        self.btn_edit_fix.setToolTip("修复缓存译文里 {/i} 被当成 {i}、{ i } 带空格等标签问题。\n"
                                     "若结果为 0 条说明缓存本来就没问题——游戏里仍有 /i 乱码时，"
                                     "直接点「应用修改到游戏」重新回填即可修复 tl 文件。")
        self.btn_edit_fix.clicked.connect(lambda: self._run(self._op_repair))
        self.btn_edit_apply = QPushButton("⤵ 应用修改到游戏")
        self.btn_edit_apply.setObjectName("primary")
        self.btn_edit_apply.setToolTip("把（修改后的）译文重新回填进 tl 并应用字体/语言入口，不调用 API。\n"
                                       "任务进度显示在窗口底部状态栏，详细日志在「① 流程」页；大游戏需要几十秒")
        self.btn_edit_apply.clicked.connect(lambda: self._run(self._op_fill))
        row2.addWidget(QLabel("替换："))
        row2.addWidget(self.ed_replace, 1)
        row2.addWidget(btn_replace)
        row2.addStretch(1)
        row2.addWidget(self.btn_edit_fix)
        row2.addWidget(self.btn_edit_apply)
        cv.addLayout(row2)
        v.addWidget(card, 1)
        return page

    # ---------- 译文修改（工单 09：按文本出现位置加载与保存） ----------
    def _edit_reload(self):
        """(重新)载入编辑页数据：每个文本出现位置一行。

        任务清单总是从当前提取结果重建（pipeline.editor_rows）——重新提取后
        编辑页立即使用最新任务清单，编辑的都是游戏里仍存在的出现位置。"""
        self._edit_rows = None
        self._edit_shown = []
        self._edit_kw = ""
        try:
            p = self._project()
        except Exception as e:
            self.lb_search.setText(str(e))
            return False
        try:
            rows = p.editor_rows()
        except Exception as e:
            self.lb_search.setText("加载失败：%s（先在①页执行「3. 提取+生成tl」）" % e)
            return False
        self._edit_rows = rows
        self._edit_store = p.store()
        done = sum(1 for r in rows if r["trans"])
        review = sum(1 for r in rows if r["candidates"] or r["suggestions"])
        self.lb_search.setText("已载入 %d 个出现位置：已译 %d、未译 %d、待检查 %d。"
                               "输入关键词或选状态筛选"
                               % (len(rows), done, len(rows) - done, review))
        return True

    def _edit_state_info(self, r):
        """行状态：(文本, 主题色 token 或 None, 工具提示)。"""
        tips = []
        if r["candidates"]:
            tips.append("候选译文 %d 条（重译结果 / 翻译任务与人工编辑的冲突 / 被替换的旧版本）"
                        % r["candidates"])
        if r["suggestions"]:
            tips.append("迁移建议 %d 条（源文本或出现位置结构已变化，确认后可采用）"
                        % r["suggestions"])
        if tips:
            lead = ("当前为人工定稿、保持不动；" if r["trans"] and r["confirmed"] else "")
            return "待检查", "STATE_BUSY", lead + "；".join(tips) + "。双击打开比较后采用或忽略"
        if not r["trans"]:
            return "未翻译", None, "尚无译文（应用后该行显示英文原文）"
        if r["confirmed"]:
            return "人工", "STATE_OK", "人工译文：不会被重新翻译、重新提取或项目迁移自动覆盖"
        return "模型", None, "模型译文：未经人工确认"

    def _edit_row_tip(self, r):
        tip = "%s\n位置：%s" % (r["file"], r["key"])
        if r["src_file"]:
            tip += "\n脚本：%s" % r["src_file"]
        if r["anchor"] and r["anchor"] != r["source"]:
            tip += "\n补丁改写前：%s" % r["anchor"]
        if r["same_source"] > 1:
            tip += "\n相同原文共 %d 处（互不影响；统一译法用修改对话框的「替换全部」）" % r["same_source"]
        return tip

    def _edit_fill_row(self, i, r):
        """把一行数据画进表格第 i 行（搜索结果构建与保存后局部刷新共用）。"""
        state, color, state_tip = self._edit_state_info(r)
        who = r["who"].strip('"')
        if r["kind"] == "string":
            who = "—"                       # 界面文本没有说话人
        cells = [(state, state_tip), (who or "(旁白)", None),
                 (r["source"], None), (r["trans"] or "（未翻译）", None)]
        kw = self._edit_kw
        row_tip = self._edit_row_tip(r)
        for col, (txt, tip) in enumerate(cells):
            it = QTableWidgetItem()
            if col == 0 and color:
                # RichTextDelegate 自绘单元格文本，颜色必须写进 HTML 才生效
                it.setText('<span style="color:%s;">%s</span>'
                           % (theme.token(color), html.escape(txt)))
            else:
                it.setText(_hl(txt, kw) if (kw and col) else html.escape(txt))
            it.setData(Qt.ItemDataRole.UserRole, txt)
            it.setToolTip(tip or row_tip)
            self.tbl_edit.setItem(i, col, it)
        self.tbl_edit.setRowHeight(i, 26)

    def _edit_search(self):
        if self._edit_rows is None and not self._edit_reload():
            return
        kw = self.ed_search.text().strip()
        self._edit_kw = kw
        scope = self.cmb_scope.currentIndex()          # 0译文 1原文 2两者
        status = self.cmb_status.currentIndex()        # 0全部 1未翻译 2已有译文 3待检查 4人工确认
        rows = []
        for r in self._edit_rows:
            if status == 1 and r["trans"]:
                continue
            if status == 2 and not r["trans"]:
                continue
            if status == 3 and not (r["candidates"] or r["suggestions"]):
                continue
            if status == 4 and not r["confirmed"]:
                continue
            if not kw:
                rows.append(r)
                continue
            in_t = kw.lower() in (r["trans"] or "").lower()
            in_o = kw.lower() in r["source"].lower()
            if (scope == 0 and in_t) or (scope == 1 and in_o) or (scope == 2 and (in_t or in_o)):
                rows.append(r)
        cap = 800
        shown = rows[:cap]
        self._edit_shown = shown
        self.tbl_edit.setRowCount(0)
        for i, r in enumerate(shown):
            self.tbl_edit.insertRow(i)
            self._edit_fill_row(i, r)
        more = "（仅显示前 %d 条）" % cap if len(rows) > cap else ""
        self.lb_search.setText("命中 %d 个出现位置%s" % (len(rows), more))

    def _edit_dblclick(self, index):
        if self._edit_rows is None or self._edit_store is None:
            return
        if not (0 <= index.row() < len(self._edit_shown)):
            return
        row = self._edit_shown[index.row()]
        siblings = [r for r in self._edit_rows if r["source"] == row["source"]]
        dlg = EditTranslationDialog(self._edit_store, row, siblings,
                                    on_saved=self._edit_autosaved, parent=self)
        dlg.exec()

    def _edit_autosaved(self, keys):
        """编辑对话框写动作的回执：刷新受影响行与保存状态（不整页重建）。

        状态更新覆盖全部已载入行（含被筛选/截断而不可见的——批量替换会写
        到它们），表格只重画当前可见的那些。"""
        if self._edit_store is None:
            return
        keyset = set(keys)
        revs = self._edit_store.review_counts()
        shown_at = {}
        for i, r in enumerate(self._edit_shown):
            shown_at[r["key"]] = i
        for r in self._edit_rows:
            if r["key"] not in keyset:
                continue
            r.update(editor_row_state(self._edit_store.get_current(r["key"]),
                                      revs.get(r["key"])))
            i = shown_at.get(r["key"])
            if i is not None:
                self._edit_fill_row(i, r)
        self._edit_saved_at()

    def _edit_saved_at(self):
        self.lb_save.setText("✓ 已保存为项目草稿 · %s（未应用到游戏；点「应用修改到游戏」生效）"
                             % time.strftime("%H:%M:%S"))

    def _edit_replace_all(self):
        """按关键词批量替换：显式批量操作，执行前展示影响范围。"""
        if self._edit_rows is None and not self._edit_reload():
            return
        kw = self.ed_search.text().strip()
        repl = self.ed_replace.text()
        if not kw:
            QMessageBox.information(self, "替换", "请先在搜索框填写要替换的关键词")
            return
        hits = [r for r in self._edit_rows if kw.lower() in (r["trans"] or "").lower()]
        if not hits:
            QMessageBox.information(self, "替换", "没有译文包含关键词「%s」" % kw)
            return
        total = sum((r["trans"] or "").lower().count(kw.lower()) for r in hits)
        human = sum(1 for r in hits if r["confirmed"])
        box = QMessageBox(self)
        box.setWindowTitle("确认批量替换")
        box.setText("将把 %d 个出现位置的译文中的「%s」（共 %d 处）全部替换为「%s」。\n"
                    "其中人工确认的译文 %d 条（替换后登记为人工译文，仍受保护）。\n"
                    "替换立即写入项目草稿，之后点「应用修改到游戏」回填生效。"
                    % (len(hits), kw, total, repl, human))
        box.addButton("替换", QMessageBox.ButtonRole.YesRole)
        box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        if box.exec() != 0:
            return
        for r in hits:
            self._edit_store.set_human_translation(r["key"], _ci_replace(r["trans"], kw, repl))
        self._edit_autosaved([r["key"] for r in hits])
        self._toast("已替换 %d 条译文并保存为项目草稿" % len(hits))

    def _edit_fix_tags(self):
        """（已改为后台任务 _op_repair；此方法保留给老入口）"""
        self._run(self._op_repair)

    def _page_settings(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(22, 18, 22, 18)
        card, cv = self._card("翻译引擎（OpenAI 兼容协议：DeepSeek / OpenAI / Ollama / LM Studio…）")
        prow = QHBoxLayout()
        prow.addWidget(QLabel("已存接口"))
        self.cmb_profile = QComboBox()
        self.cmb_profile.setToolTip("选中后自动填入该方案保存的 接口地址 / API Key / 模型，\n"
                                    "以及温度、并发、批次、单次回复上限、输入预算、System Prompt 等全部参数，\n"
                                    "在 DeepSeek 官方、OpenRouter、本地 LM Studio 之间切换时不必反复调整")
        self.cmb_profile.activated.connect(self._profile_selected)
        self.btn_profile_save = QPushButton("存为方案")
        self.btn_profile_save.setToolTip("把当前填写的接口和全部参数保存为一个方案（同名覆盖更新）")
        self.btn_profile_save.clicked.connect(self._profile_save)
        self.btn_profile_del = QPushButton("删除")
        self.btn_profile_del.setToolTip("删除当前选中的接口方案")
        self.btn_profile_del.clicked.connect(self._profile_del)
        prow.addWidget(self.cmb_profile, 1)
        prow.addWidget(self.btn_profile_save)
        prow.addWidget(self.btn_profile_del)
        cv.addLayout(prow)
        form = QFormLayout()
        self.ed_base = QLineEdit()
        self.ed_key = QLineEdit()
        self.ed_key.setEchoMode(QLineEdit.Password)
        self.cmb_model = QComboBox()
        self.cmb_model.setEditable(True)
        self.btn_fetch = QPushButton("⟳ 拉取模型")
        self.btn_fetch.setToolTip("按上面填写的接口地址和 API Key 拉取可用模型列表，不必手动填写模型名")
        self.btn_fetch.clicked.connect(lambda: self._fetch_models("engine"))
        self.btn_verify = QPushButton("✓")
        self.btn_verify.setFixedWidth(40)
        self.btn_verify.setToolTip("校验当前填写的这一个模型名：是否存在/可用（不拉全表）。\n"
                                   "OpenRouter 等支持时还会显示上下文长度与价格（免费模型标“免费”）")
        self.btn_verify.clicked.connect(lambda: self._verify_model("engine"))
        rmodel = QHBoxLayout()
        rmodel.setSpacing(6)
        rmodel.addWidget(self.cmb_model, 1)
        rmodel.addWidget(self.btn_verify)
        rmodel.addWidget(self.btn_fetch)
        self.sp_temp = NumEdit(float, 0, 2, 0.6, tip="采样温度：越高越发散，翻译建议 0.5~1.0（直接键入数字）")
        self.sp_pen = NumEdit(float, 0, 2, 0.0, tip="重复惩罚（presence_penalty）。本地量化模型（如 Qwen GGUF）建议 1.5 防止复读；\n用云端 API 时保持 0 即可")
        self.sp_conc = NumEdit(int, 1, 32, 8, tip="同时进行的请求数（直接键入数字）")
        self.sp_batch = NumEdit(int, 1, 60, 12, tip="每次请求翻译的行数（直接键入数字）")
        self.sp_timeout = NumEdit(int, 10, 7200, 240,
                                  tip="单个请求的最长等待秒数，超时即断开并重试（直接键入数字）。\n"
                                      "在线 API：60~240 一般足够；本地大模型（27B 以上）或大批次建议 600~900，\n"
                                      "否则生成到一半被掐断，整批 JSON 解析失败只能重翻")
        self.sp_ctx = NumEdit(int, 0, 6, 2, tip="每行携带的前后文行数（直接键入数字）")
        self.sp_maxtok = NumEdit(int, 0, 32768, 0,
                                 tip="单次回复的输出上限（max_tokens）。0 = 不发送该参数。\n"
                                     "在线 API：保持 0 即可（各家默认值够用，也兼容只认 "
                                     "max_completion_tokens 的新模型）。\n"
                                     "本地模型：必须设置（建议 1024~2048）。不设时模型可能一直复读写到\n"
                                     "上下文窗口用满，推理服务会直接报 500 “Context size has been exceeded”\n"
                                     "并掐断同批所有并发请求——整轮翻译就是这样卡死的。\n"
                                     "经验值：每行译文约 80~140 token，即 ≈ 每请求行数 × 150。")
        self.sp_prompt = NumEdit(int, 0, 200000, 0,
                                 tip="单个请求的输入预算（估算 token）。0 = 不限制。\n"
                                     "超过预算的批次会自动对半拆小后再发，避免撞上模型上下文。\n"
                                     "本地模型建议填「模型上下文长度的一半左右」：例如 LM Studio 里\n"
                                     "上下文设为 16k，这里填 6000~8000，并给「单次回复上限」留出空间。\n"
                                     "注意：输入预算 + 单次回复上限 必须小于模型的上下文长度。\n"
                                     "在线 API 保持 0 即可（128k 上下文用不到）。")
        self.cmb_think = QComboBox()
        self.cmb_think.addItem("跟随服务商默认（不发送参数）", "auto")
        self.cmb_think.addItem("关闭思考（省 token，翻译推荐）", "off")
        self.cmb_think.addItem("开启 · 低强度", "low")
        self.cmb_think.addItem("开启 · 高强度", "high")
        self.cmb_think.setToolTip(
            "DeepSeek V4 系列（v4-flash / v4-pro）默认开启思考模式且强度为 high，\n"
            "思考内容按输出 token 计费——翻译花费暴涨多半就是它。\n"
            "· 关闭思考：发送 thinking={type:disabled}，翻译不需要推理，关掉可大幅省钱；\n"
            "· 开启：按官方格式发送 thinking + reasoning_effort（适合需要推理的场景）；\n"
            "· 注意：思考模式下 temperature / 惩罚参数会被 DeepSeek 忽略（官方文档说明）；\n"
            "· 跟随默认：不发送该参数，兼容 OpenAI / Ollama 等不认识此字段的服务商。")
        form.addRow("接口地址", self.ed_base)
        form.addRow("API Key", self.ed_key)
        form.addRow("模型", rmodel)
        form.addRow("思考模式", self.cmb_think)
        form.addRow("温度", self.sp_temp)
        form.addRow("重复惩罚", self.sp_pen)
        form.addRow("并发数", self.sp_conc)
        form.addRow("每请求行数", self.sp_batch)
        form.addRow("单次回复上限", self.sp_maxtok)
        form.addRow("输入预算(token)", self.sp_prompt)
        form.addRow("超时(秒)", self.sp_timeout)
        form.addRow("上下文行数", self.sp_ctx)
        cv.addLayout(form)
        row = QHBoxLayout()
        test = QPushButton("测试连接")
        test.clicked.connect(self._test_engine)
        row.addWidget(test)
        self.lb_engine = QLabel("")
        self.lb_engine.setMinimumWidth(0)
        self.lb_engine.setWordWrap(True)
        self.lb_engine.setObjectName("dim")
        row.addWidget(self.lb_engine, 1)
        cv.addLayout(row)
        v.addWidget(card)

        card, cv = self._card("AI 角色提示词（System Prompt，可自定义翻译风格）")
        hint = QLabel("占位符 {lang} 会替换成目标语言名；人名对照 / 词汇表 / 人物关系约束仍自动附加在提示词之后。"
                      "修改并保存后对新翻译的行生效；输出 JSON 的格式约定不可删除。")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        cv.addWidget(hint)
        self.ed_prompt = QPlainTextEdit()
        self.ed_prompt.setMinimumHeight(180)
        cv.addWidget(self.ed_prompt, 1)
        rowp = QHBoxLayout()
        breset = QPushButton("恢复默认")
        breset.clicked.connect(self._reset_prompt)
        rowp.addWidget(breset)
        rowp.addStretch(1)
        cv.addLayout(rowp)
        v.addWidget(card, 1)

        card, cv = self._card("关系扫描模型")
        self.cmb_scan = QComboBox()
        self.cmb_scan.addItems(["与翻译引擎相同", "自定义端点（可用于本地 Ollama 等）"])
        self.cmb_scan.currentIndexChanged.connect(lambda _: self._sync_scan_ui())
        self.ed_scan_base = QLineEdit()
        self.ed_scan_key = QLineEdit()
        self.ed_scan_key.setEchoMode(QLineEdit.Password)
        self.cmb_scan_model = QComboBox()
        self.cmb_scan_model.setEditable(True)
        self.btn_scan_fetch = QPushButton("⟳ 拉取模型")
        self.btn_scan_fetch.setToolTip("按上面填写的自定义端点拉取可用模型列表")
        self.btn_scan_fetch.clicked.connect(lambda: self._fetch_models("scan"))
        self.btn_scan_verify = QPushButton("✓")
        self.btn_scan_verify.setFixedWidth(40)
        self.btn_scan_verify.setToolTip("校验当前填写的这一个模型名是否可用（不拉全表）")
        self.btn_scan_verify.clicked.connect(lambda: self._verify_model("scan"))
        rsmodel = QHBoxLayout()
        rsmodel.setSpacing(6)
        rsmodel.addWidget(self.cmb_scan_model, 1)
        rsmodel.addWidget(self.btn_scan_verify)
        rsmodel.addWidget(self.btn_scan_fetch)
        self.sp_sample = NumEdit(int, 5, 200, 40, tip="每个角色抽取多少句台词用于关系分析（直接键入数字）")
        f2 = QFormLayout()
        f2.addRow("使用", self.cmb_scan)
        f2.addRow("接口地址", self.ed_scan_base)
        f2.addRow("API Key", self.ed_scan_key)
        f2.addRow("模型", rsmodel)
        f2.addRow("每角色抽样句数", self.sp_sample)
        cv.addLayout(f2)
        v.addWidget(card)

        card, cv = self._card("字体与杂项")
        f3 = QFormLayout()
        rowf = QHBoxLayout()
        self.ed_font = QLineEdit()
        bf = QPushButton("浏览…")
        bf.clicked.connect(self._pick_font)
        bf.setToolTip("选择中文字体后,到「① 流程」页执行「⤵ 应用到游戏」生效\n"
                      "(统一应用:字体与译文、显示层、语言入口一并事务式生效)")
        rowf.addWidget(self.ed_font, 1)
        rowf.addWidget(bf)
        f3.addRow("中文字体", rowf)
        self.ed_f95cookie = QLineEdit()
        self.ed_f95cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_f95cookie.setPlaceholderText(
            "可选：登录 f95zone.to 后的 Cookie（xf_user=…），用于游戏库查询评分")
        self.ed_f95cookie.setToolTip(
            "F95Zone 对未登录请求有每小时次数上限（查询评分会撞到 429）；\n"
            "填上自己账号的登录 Cookie 可放开这条限制。\n\n"
            "获取方法：浏览器登录 f95zone.to → F12 打开开发者工具 → Network（网络）→\n"
            "刷新页面 → 点任意请求 → Request Headers 里的 Cookie 一行，整行复制粘贴到这里。\n"
            "只保存在本机 config.json，仅用于请求 f95zone.to。")
        f3.addRow("F95 登录 Cookie", self.ed_f95cookie)
        self.chk_font = QCheckBox("应用字体覆盖（写入 tl/<语言>/zz_ng_font.rpy）")
        self.chk_lang = QCheckBox("添加游戏内 中/EN 切换按钮（zz_ng_language.rpy）")
        self.chk_strings = QCheckBox("同时翻译 UI 字符串（按钮/菜单等）")
        self.chk_autoupd = QCheckBox("启动时后台静默检查游戏库更新（不锁界面、不弹窗）")
        self.chk_autoupd.setToolTip(
            "打开程序后自动在后台比对本机游戏与线上最新版本，全程不锁界面、不挡操作，\n"
            "有新版才在封面左上角标「可更新」，没有就什么都不显示。\n"
            "只补查 12 小时内没查过的游戏（刚查过的跳过，所以不会每次启动都全查一遍）；\n"
            "查不到版本号的游戏不做任何提示。关掉后仍可点「🎮 游戏库 → 🔍 检查更新」手动查。")
        f3.addRow(self.chk_font)
        f3.addRow(self.chk_lang)
        f3.addRow(self.chk_strings)
        f3.addRow(self.chk_autoupd)
        cv.addLayout(f3)
        save = QPushButton("保存设置")
        save.setObjectName("primary")
        save.clicked.connect(self._save_settings)
        cv.addWidget(save)
        v.addWidget(card)
        card, cv = self._card("数据维护")
        rowm = QHBoxLayout()
        lblm = QLabel("从最近一次旧数据迁移备份还原旧项目数据、游戏库记录与应用配置"
                      "（迁移前自动创建，覆盖现有同名文件，不影响汉化项目及其项目资产）")
        lblm.setWordWrap(True)
        btn_restore = QPushButton("从迁移备份恢复…")
        btn_restore.clicked.connect(self._restore_migration_backup)
        rowm.addWidget(lblm, 1)
        rowm.addWidget(btn_restore)
        cv.addLayout(rowm)
        v.addWidget(card)
        v.addStretch(1)
        return page

    # ---------- 导航 / 通用 ----------
    def eventFilter(self, obj, ev):
        """可编辑模型框：点击框体/输入区任意位置都弹出选项列表（Esc 关闭后仍可手动键入）。"""
        if ev.type() == QEvent.Type.MouseButtonPress:
            combo = obj if isinstance(obj, QComboBox) else obj.parent()
            if combo in self._popup_combos and combo.count() and not combo.view().isVisible():
                QTimer.singleShot(0, combo.showPopup)   # 延迟一拍，避免和光标定位冲突
        return super().eventFilter(obj, ev)

    def _goto(self, key):
        idx = [k for k, _ in self.nav_btns].index(key)
        self.stack.setCurrentIndex(idx)
        for k, b in self.nav_btns:
            b.setChecked(k == key)
        self.lb_page.setText(self.PAGE_TITLES.get(key, ""))
        if key == "glossary":
            self._gloss_load()
        if key == "chars":
            self._char_load()
            self._word_load()
        if key == "editor":
            self._edit_reload()
        if key == "library":
            self.lib_page.reload()

    # ---------- 主题 / 侧栏状态 ----------
    def _apply_theme(self):
        """按配置应用深/浅色主题（QSS 重刷会带动全部控件重新着色）。"""
        dark = self.cfg.get("theme", "dark") != "light"
        theme.set_dark(dark)
        QApplication.instance().setStyleSheet(theme.build_qss())
        self.btn_theme.setText("☀" if dark else "🌙")
        self.btn_theme.setToolTip("当前为深色模式，点击切换到浅色"
                                  if dark else "当前为浅色模式，点击切换到深色")
        self._apply_titlebar()
        self.update()   # 让 GlassRoot 按新主题重绘底板与光斑

    def _apply_titlebar(self):
        """Windows 11：让系统标题栏颜色跟随主题（其他平台/失败时静默跳过）。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            dark = ctypes.c_int(1 if theme.is_dark() else 0)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)      # 深色模式标志
            cap = ctypes.c_uint(theme.titlebar_color())
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)       # 标题栏底色
            txt = ctypes.c_uint(theme.titlebar_text_color())
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)       # 标题栏文字色
        except Exception:
            pass

    def _toggle_theme(self):
        self.cfg["theme"] = "light" if theme.is_dark() else "dark"
        self.cfg.save()   # 记住选择，下次启动沿用
        self._apply_theme()
        self._toast("已切换到%s模式（右上角可随时切回）" % ("浅色" if theme.is_dark() else "深色"))

    def _side_state(self, kind, text):
        """侧栏底部状态灯：kind = ok / busy / err。"""
        color = theme.token({"ok": "STATE_OK", "busy": "STATE_BUSY", "err": "STATE_ERR"}[kind])
        self.lb_state.setText('<span style="color:%s;">●</span>&nbsp; %s' % (color, text))

    def _pick_game(self):
        d = QFileDialog.getExistingDirectory(self, "选择游戏根目录")
        if d:
            self._set_game(d)

    def _pick_font(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择字体", "C:/Windows/Fonts", "字体 (*.ttf *.ttc *.otf)")
        if f:
            self.ed_font.setText(f)

    def _set_game(self, path):
        """选择/切换游戏目录：校验、登记或定位其汉化项目（须在主线程调用）。

        登记成功才写入界面与配置；版本号经 updates.local_version 一并记录到注册库。"""
        path = os.path.normpath(path.strip())
        if not os.path.isdir(os.path.join(path, "game")):
            QMessageBox.warning(self, "无效目录",
                                "所选目录不是 Ren'Py 游戏根目录（需包含 game/ 子目录）：\n%s" % path)
            return False
        try:
            proj, action = self._resolve_project(path)
        except Exception as e:
            QMessageBox.warning(self, "项目登记失败", str(e))
            return False
        self.ed_game.setText(path)
        self.cfg["last_game"] = path
        self.cfg.save()
        if action == "relocated":
            self._toast("已重新定位汉化项目《%s》：沿用其全部项目资产" % proj["name"])
        elif action == "created":
            self._toast("已登记汉化项目《%s》" % proj["name"])
        return True

    def _resolve_project(self, base, prompt=True):
        """打开/登记 base 对应的汉化项目，返回 (项目, 动作)。

        动作 = existing（已登记）/ relocated（用户确认重新定位）/ created（新登记）。
        注册库是项目身份的唯一入口：项目资产目录由项目身份派生，不再按目录名推导。
        游戏目录被移动或改名后，旧项目会出现在"当前安装丢失"名单里——请用户确认
        当前目录就是它的游戏安装（绝不按名称自动合并），确认后重新定位到同一项目。
        所有项目任务启动前都在主线程调用本方法，工作线程内不再发生登记分支。
        """
        reg = projreg.Registry()
        try:
            cur = reg.find_by_path(base)
            if cur is not None:
                return cur, "existing"
            if prompt and self.thread() is QThread.currentThread():
                stale = reg.stale_projects()
                if stale:
                    picked = self._ask_relocate(stale, base)
                    if picked is not None:
                        return (reg.relocate(picked["id"], base,
                                             version=self._install_version(base)),
                                "relocated")
            return (reg.create_project(gamelib.derive_name(base), base,
                                       version=self._install_version(base)),
                    "created")
        finally:
            reg.close()

    def _install_version(self, base):
        """游戏安装的本地版本（log.txt / options.rpy / 目录名），判不出则留空。"""
        try:
            return upd.local_version(base, gamelib.derive_name(base)) or None
        except Exception:
            return None

    def _ask_relocate(self, stale, base):
        """重新定位确认：返回被确认的旧汉化项目；用户否认/取消时返回 None（登记新项目）。"""
        if len(stale) == 1:
            p = stale[0]
            ret = QMessageBox.question(
                self, "重新定位汉化项目",
                "汉化项目《%s》原来的游戏目录已不存在：\n%s\n\n"
                "当前选择的目录是否就是它的新位置？\n\n"
                "选「Yes」沿用该项目的提取结果、词汇表和译文；\n"
                "选「No」把当前目录登记为新汉化项目。" % (p["name"], p["path"]),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            return p if ret == QMessageBox.StandardButton.Yes else None
        labels = ["《%s》  原目录：%s" % (p["name"], p["path"]) for p in stale]
        item, ok = QInputDialog.getItem(
            self, "重新定位汉化项目",
            "以下汉化项目原来的游戏目录已不存在。\n"
            "当前目录是否属于其中某个项目？\n\n当前目录：%s\n\n"
            "选择项目则沿用其全部资产；点「Cancel」则登记为新项目。" % base,
            labels, 0, False)
        if not ok:
            return None
        return stale[labels.index(item)]

    # ---------- 旧数据自动迁移（工单 04：升级首次启动扫描旧版数据） ----------

    def _offer_legacy_migration(self):
        """窗口显示后后台扫描旧版数据；有可迁移内容时弹出预览供确认。

        扫描只读不改；已有任务在运行时本次不弹（下次启动还会再扫描）。"""
        if self.worker and self.worker.isRunning():
            return
        t = FetchThread(migration.scan)
        self._fetch_threads.append(t)
        t.sig_done.connect(self._on_legacy_scan)
        t.start()

    def _on_legacy_scan(self, ok, info):
        if not ok or not isinstance(info, dict):
            return
        pending = [p for p in info.get("projects", [])
                   if not p["done"] and p["match"]["status"] in ("new", "merge")]
        if not pending:
            return   # 没有可迁移的项目：已迁移/暂不可迁移的静默跳过，不打扰启动
        lines = []
        for p in info["projects"]:
            if p["done"]:
                continue
            m = p["match"]
            if m["status"] in ("new", "merge"):
                n_rev = p["counts"]["translations"] - p["plan"]["translations_importable"]
                lines.append("• %s：译文 %d 条（可导入 %d 条，%d 条待检查）"
                             % (p["name"], p["counts"]["translations"],
                                p["plan"]["translations_importable"], n_rev))
            elif m["status"] == "missing":
                lines.append("• %s：找不到对应的游戏安装，暂不迁移（数据保留在原目录）"
                             % p["name"])
            else:
                lines.append("• %s：同名游戏安装有多个，无法自动确定，暂不迁移" % p["name"])
        n_review = sum(len(p["review_items"]) for p in info["projects"]) \
            + len(info.get("review_items", []))
        msg = ("检测到旧版数据，可以迁移到新版项目结构（按项目身份存放，译文进入"
               "项目数据库）：\n\n%s\n\n游戏库记录与应用配置保持原样继续使用；"
               "迁移前会自动创建备份，可随时恢复。"
               % "\n".join(lines))
        if n_review:
            msg += "\n另有 %d 条待检查项（迁移后在「① 流程」页的任务摘要里逐条列出）。" % n_review
        ret = QMessageBox.question(
            self, "旧数据迁移", msg + "\n\n现在开始迁移吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._goto("flow")
        self._run(lambda log, prog, should_stop, ask: self._do_legacy_migration(log))

    def _do_legacy_migration(self, log):
        """在后台执行迁移并输出任务摘要（备份位置、对账结果、待检查项清单）。"""
        report = migration.apply(log=log)
        p, t = report["projects"], report["translations"]
        if not report["backup_dir"]:
            return "没有需要迁移的旧数据"
        log("迁移备份：%s（可在 设置 → 数据维护 从备份恢复）" % report["backup_dir"])
        log("任务摘要：项目 新建 %d / 并入 %d / 跳过 %d；译文 导入 %d / 保留 %d / 待检查 %d"
            % (p["created"], p["merged"], p["skipped"], t["imported"], t["kept"], t["review"]))
        for item in report["review_items"]:
            log("待检查：%s — %s（来源：%s）"
                % (item["key"], item["detail"], item["source"]))
        if not report["reconciled"]:
            log("⚠ 译文对账不平：共 %d 条，与导入/保留/待检查之和不一致，请人工核查" % t["total"])
        return ("迁移完成：新建 %d 个项目、并入 %d 个；导入译文 %d 条，待检查 %d 条"
                "（失败可从备份恢复）" % (p["created"], p["merged"], t["imported"], t["review"]))

    def _restore_migration_backup(self):
        ret = QMessageBox.question(
            self, "恢复迁移备份",
            "把最近一次迁移备份里的文件复制回原位（覆盖现有同名文件）：\n"
            "旧项目数据（work/ 下按游戏安装目录名存放的旧目录）、\n"
            "library.json 与 config.json。\n"
            "不会删除备份之外的新文件，汉化项目与其项目资产不受影响。\n\n"
            "确定恢复吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._goto("flow")
        self._run(lambda log, prog, should_stop, ask: self._do_restore_backup(log))

    def _do_restore_backup(self, log):
        n = migration.restore_backup()
        if not n:
            return "没有可用的迁移备份"
        log("已从最近一次迁移备份还原 %d 个文件" % n)
        return ("恢复完成：共还原 %d 个文件（如需重新迁移，"
                "删除对应旧目录里的 _migrated.json 标记后重启即可）" % n)

    def _project(self):
        base = self.ed_game.text().strip()
        if not base or not os.path.isdir(os.path.join(base, "game")):
            raise RuntimeError("请先选择有效的游戏根目录（需包含 game/ 子目录）")
        self._collect()
        proj, _action = self._resolve_project(base)
        return Project(self.cfg, base, proj["id"])

    # 会修改当前汉化项目核心数据的后台步骤用 @project_task(kind) 显式标记
    # （见模块头 project_task 定义），不再用方法名映射——改名/新增步骤漏登记
    # 就是绕行直写路径。

    def _run(self, fn):
        if getattr(fn, "__self__", None) is self.lib_page:
            return self._run_lib(fn)
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self, "忙碌", "有任务正在运行（进度见窗口底部状态栏，详细日志在「① 流程」页），\n请等待完成后再操作。")
            return
        self._collect()
        self.cfg.save()
        base = self.ed_game.text().strip()
        if base and os.path.isdir(os.path.join(base, "game")):
            # 项目任务的登记与"重新定位"确认一律在主线程完成（含目录移动后
            # 直接运行步骤的情形），工作线程内只按已登记的项目身份执行
            try:
                self._resolve_project(base)
            except Exception as e:
                QMessageBox.warning(self, "项目登记失败", str(e))
                return
            kind = getattr(fn, "_project_task_kind", None)
            if kind:
                # 同一汉化项目同一时间只允许一个修改核心数据的项目任务；
                # 拒绝在界面层给出，而不是让两个任务同时写核心数据
                try:
                    self._task_handle = coordinator.TaskCoordinator(
                        self._project().store()).begin(kind)
                except coordinator.TaskBusy as e:
                    QMessageBox.information(self, "任务进行中", str(e))
                    return
                except Exception as e:
                    QMessageBox.warning(self, "任务登记失败", str(e))
                    return
        try:
            self.worker = Worker(fn)
            self.worker.sig_log.connect(self._on_log)
            self.worker.sig_prog.connect(self._on_prog)
            self.worker.sig_done.connect(self._on_done)
            self.worker.sig_confirm.connect(self._on_confirm)
            # 日志与进度同步到状态栏：在其他页面（如④译文修改）发起任务也能看到反馈
            self.worker.sig_log.connect(lambda m: self.statusBar().showMessage(str(m), 8000))
            for b in self.steps + [self.btn_estimate, self.btn_scan, self.btn_fill,
                                   self.btn_retrans, self.btn_runall,
                                   self.btn_edit_apply, self.btn_edit_fix]:
                b.setEnabled(False)
            self.btn_pause.setEnabled(True)
            self.btn_pause.setText("⏸ 暂停")
            self._side_state("busy", "任务运行中")
            self._running_fn = fn
            self.statusBar().showMessage("任务开始…", 0)
            self.worker.start()
        except BaseException:
            # 启动失败时任务行必须收尾，否则它带着新鲜心跳阻塞本项目直到进程退出
            task, self._task_handle = self._task_handle, None
            if task is not None:
                task.finish("failed", "任务启动失败")
            raise

    def _run_lib(self, fn):
        """游戏库自己的任务（扫描/刷新）走独立槽位：不占项目任务的 Worker，
        也不经项目任务协调器（游戏库记录不是汉化项目的项目资产），与运行中的
        项目任务互不阻塞。"""
        if self._lib_worker and self._lib_worker.isRunning():
            QMessageBox.information(
                self, "忙碌", "游戏库任务正在运行，请等待完成后再操作。")
            return
        self._lib_worker = Worker(fn)
        self._lib_worker.sig_log.connect(self._on_log)
        self._lib_worker.sig_prog.connect(self._on_prog)
        self._lib_worker.sig_done.connect(self._on_done)
        self._lib_worker.sig_log.connect(lambda m: self.statusBar().showMessage(str(m), 8000))
        self.lib_page.set_busy(True)
        self.lib_page.pause_update_check(True)   # 版本检查让路：不抢网络、不抢写库
        self._lib_running_fn = fn
        self.statusBar().showMessage("游戏库任务开始…", 0)
        self._lib_worker.start()

    def _on_log(self, msg):
        self.log.appendPlainText(str(msg))

    def _on_prog(self, cur, total):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(cur)
        msg = "%d / %d（%.1f%%）" % (cur, total, 100.0 * cur / max(1, total))
        self.lb_prog.setText(msg)
        self.statusBar().showMessage(msg, 0)

    def _on_confirm(self, message):
        if message.startswith("EXTTL:"):
            # 外部译文变更（工单 08）：逐项比较后导入（人工来源）或明确放弃；
            # 取消对话框 = 中止应用，游戏目录保持现状
            try:
                payload = json.loads(message.split(":", 1)[1])
            except ValueError:
                payload = {"items": []}
            dlg = ExternalChangesDialog(payload, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                res = json.dumps(dlg.decisions(), ensure_ascii=False)
            else:
                res = "abort"
        elif message.startswith("RELATIONS:"):
            try:
                chars = json.loads(message.split(":", 1)[1])
            except ValueError:
                chars = []
            if not isinstance(chars, list):
                chars = []
            save_path = None
            try:
                save_path = os.path.join(self._project().work, "relations.json")
            except Exception:
                pass    # 取不到路径就只展示：对话框里的修改改不动盘上的表
            dlg = RelationsDialog(chars, save_path, self)
            dlg.exec()
            res = dlg.action
            if dlg.save_error:
                self.log.appendPlainText(
                    "⚠ 关系表保存失败：%s\n   本次翻译仍按原关系表进行（改动只在对话框里，没有落盘）"
                    % dlg.save_error)
            if res == "abort":
                # 既然选择先去审核，就把关系页直接翻到眼前；
                # 翻页失败也不能抛出去——工作线程还在等答复，漏掉会一直卡住
                try:
                    self._goto("chars")
                except Exception:
                    pass
        else:
            res = "use" if QMessageBox.question(self, "确认", message) == QMessageBox.StandardButton.Yes else "abort"
        self.worker.answer_confirm(res)

    def _on_done(self, ok, msg):
        if self.sender() is self._lib_worker and self._lib_worker is not None:
            return self._on_lib_done(ok, msg)
        # 项目任务收尾：协调器登记的任务行落到终态（摘要给任务历史/诊断用）
        paused = bool(self.worker and self.worker._stop)
        task, self._task_handle = self._task_handle, None
        if task is not None:
            task.finish("done" if ok and not paused
                        else ("stopped" if ok else "failed"), str(msg))
        for b in self.steps + [self.btn_estimate, self.btn_scan, self.btn_fill,
                               self.btn_retrans, self.btn_runall,
                               self.btn_edit_apply, self.btn_edit_fix]:
            b.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ 暂停")
        if not ok:
            self.lb_prog.setText("❌ 出错")
            self.statusBar().showMessage("❌ 出错：%s" % msg[:120], 10000)
            self._side_state("err", "任务出错")
            QMessageBox.warning(self, "任务失败", msg[:800])
        elif paused:
            self.lb_prog.setText("⏸ 已暂停：进度已保存，可随时继续")
            self.statusBar().showMessage("⏸ 已暂停，进度已保存；继续翻译请再执行第 5 步（自动续翻）", 8000)
            self._side_state("busy", "已暂停")
        else:
            self.lb_prog.setText("✅ 完成")
            self.statusBar().showMessage("✅ %s" % msg, 10000)
            self._side_state("ok", "就绪")
        # 编辑页数据可能已被任务改写（翻译/回填/修复/重新提取），在用则刷新；
        # 按当前搜索框与状态筛选重建可见行，不让表格停留在任务前的旧数据
        if self._edit_rows is not None:
            try:
                if self._edit_reload():
                    self._edit_search()
            except Exception:
                pass

    def _on_lib_done(self, ok, msg):
        """游戏库任务收尾：与项目任务互不影响，各自恢复各自的界面状态。"""
        self.lib_page.set_busy(False)
        self.lib_page.pause_update_check(False)  # 版本检查接着上次继续
        # 游戏库自己的任务（扫描/刷新）结束后重建卡片墙
        if getattr(self._lib_running_fn, "__self__", None) is self.lib_page:
            self.lib_page.reload()
        if not ok:
            self.statusBar().showMessage("❌ 出错：%s" % str(msg)[:120], 10000)
            QMessageBox.warning(self, "任务失败", str(msg)[:800])
        else:
            self.statusBar().showMessage("✅ %s" % msg, 10000)

    def _pause_worker(self):
        if not (self.worker and self.worker.isRunning()):
            return
        self.worker.stop()
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("正在停止…")
        self.log.appendPlainText("⏸ 已请求暂停：等待进行中的请求返回后保存进度（已完成的部分不会丢失）")

    def _run_all(self):
        self._run(self._op_all)

    # ---------- 各操作 ----------
    def _launch_game(self):
        root = self.ed_game.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "错误", "请先选择有效的游戏根目录")
            return
        exe, total = gamelib.find_game_exe(root)
        if not exe:
            QMessageBox.warning(self, "错误", "游戏根目录下没有找到可执行文件（.exe）")
            return
        if total > 1:
            self._toast("发现多个 exe，已启动主程序：%s" % os.path.basename(exe))
        try:
            subprocess.Popen([exe], cwd=root, close_fds=True)
        except OSError as e:
            QMessageBox.warning(self, "启动失败", str(e))

    def _clear_cache(self):
        try:
            p = self._project()
        except Exception as e:
            QMessageBox.warning(self, "错误", str(e))
            return
        # 清空译文记录会改核心数据：与运行中的项目任务互斥（前台动作，不登记）
        if coordinator.TaskCoordinator(p.store()).is_busy():
            self._toast("有项目任务正在运行，请等它结束后再清空译文缓存")
            return
        store = p.store()
        if not store.currents():
            self._toast("当前游戏没有译文记录")
            return
        ret = QMessageBox.question(
            self,
            "确认清空译文缓存",
            "即将清空项目数据库里全部未确认的译文记录。\n"
            "人工确认过的译文会保留，不会被清空。\n"
            "被清掉的译文先备份到项目资产目录（cleared_currents.*.json），可找回。\n"
            "重跑第5步会重新翻译未确认的内容。\n\n"
            "确定要继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        kept = store.clear_currents()
        if kept:
            self._toast("译文记录已清空（已备份）；%d 条人工确认的译文已保留，重跑第5步即重译其余内容" % kept)
        else:
            self._toast("译文记录已清空（已备份），重跑第5步即全文重译")

    def _open_apply_points(self):
        """恢复点 / 停用汉化（工单 07）：需要已登记的项目与游戏目录。"""
        try:
            p = self._project()
        except Exception as e:
            QMessageBox.warning(self, "错误", str(e))
            return
        if not os.path.isdir(os.path.join(p.game_base, "game")):
            QMessageBox.warning(self, "错误", "游戏目录不可用：%s" % p.game_base)
            return
        # 与运行中的项目任务互斥：还原/停用与应用都改游戏目录，不并行
        if coordinator.TaskCoordinator(p.store()).is_busy():
            self._toast("有项目任务正在运行，请等它结束后再操作恢复点")
            return
        ApplyPointsDialog(self, self).exec()

    @project_task("全流程")
    def _op_all(self, log, prog, stop, confirm=None):
        p = self._project()
        log("① 解包：%s" % p.unpack(log, None, stop))
        if stop():
            return "已在步骤①后暂停（可随时继续）"
        log("② 反编译：\n%s" % p.decompile(log, stop))
        if stop():
            return "已在步骤②后暂停（可随时继续）"
        n = p.extract_tl(log, stop)
        log("③ 提取完成：%d 个对白块" % n)
        if stop():
            return "已在步骤③后暂停（可随时继续）"
        use_relations = True
        try:
            chars = p.scan_relations(log, stop)
            log("④ 关系草表：%d 个角色" % len(chars))
            if confirm and chars:
                # 整张表随消息一起送过去：确认框要原样摆给用户核对，不能只报个数
                res = confirm("RELATIONS:" + json.dumps(chars, ensure_ascii=False))
                if res == "abort":
                    raise RuntimeError("已中止：请到「③ 人物关系」页审核修改后，手动执行第 5、6 步")
                if res == "skip":
                    use_relations = False
        except RuntimeError:
            raise
        except Exception as e:
            log("④ 关系草表跳过：%s" % e)
        if stop():
            return "已在步骤④后暂停（可随时继续）"
        rel_path = os.path.join(p.work, "relations.json")
        rel_bak = rel_path + ".skip"
        if not use_relations:
            os.replace(rel_path, rel_bak)
        try:
            p.translate(log, prog, stop)
        finally:
            if not use_relations and os.path.isfile(rel_bak):
                os.replace(rel_bak, rel_path)
        paused = stop()
        s = p.apply_txn(log, confirm=confirm)
        if s.get("stopped"):
            return ("已在外部译文变更处理时中止：游戏目录未改动；"
                    "处理完变更后请手动执行第 6 步应用")
        if paused:
            return "已暂停：已译部分已应用（可先试玩）；继续翻译请再执行第 5 步（自动续翻）"
        return "全流程完成"

    @project_task("准备")
    def _op_unpack(self, log, prog, stop, confirm=None):
        return self._project().unpack(log, prog, stop)

    @project_task("准备")
    def _op_decompile(self, log, prog, stop, confirm=None):
        return self._project().decompile(log, stop)

    @project_task("提取")
    def _op_extract(self, log, prog, stop, confirm=None):
        n = self._project().extract_tl(log, stop)
        return "提取完成：%d 个对白块" % n

    @project_task("检查")
    def _op_scan(self, log, prog, stop, confirm=None):
        p = self._project()
        chars = p.scan_relations(log, stop)
        self._char_load()
        self._word_load()
        return "关系草表生成完成：%d 个角色，请审核" % len(chars)

    @project_task("翻译")
    def _op_translate(self, log, prog, stop, confirm=None):
        n, missing = self._project().translate(log, prog, stop)
        return "回填 %d 条，未译 %d 条" % (n, missing)

    @project_task("回填")
    def _op_fill(self, log, prog, stop, confirm=None):
        s = self._project().apply_txn(log, confirm=confirm)
        return apply_summary_msg(s)

    @project_task("翻译")
    def _op_retrans(self, log, prog, stop, confirm=None):
        n, missing = self._project().retranslate_missing(log, prog, stop)
        if n == 0 and missing == 0:
            return "没有未译内容，无需重译"
        return "重译并回填 %d 条，剩余未译 %d 条" % (n, missing)

    @project_task("修复")
    def _op_repair(self, log, prog, stop, confirm=None):
        n = self._project().repair_cache(log)
        if n:
            return "已修复 %d 条标签错乱；请点「应用修改到游戏」回填生效" % n
        log("缓存没有需要修复的标签。")
        log("若游戏里仍看到 /i 之类乱码：乱码在 tl 文件里而不在缓存，"
            "直接点「⤵ 应用修改到游戏」重新回填即可修复，不需要重新翻译。")
        return "缓存无需修复；游戏内乱码请点「应用修改到游戏」重新回填"

    @project_task("应用")
    def _op_finish(self, log, prog, stop, confirm=None):
        s = self._project().apply_txn(log, confirm=confirm)
        return apply_summary_msg(s)

    @project_task("应用")
    def _op_deactivate(self, log, prog, stop, confirm=None):
        """停用汉化:只撤销工具管理的文件,汉化项目与全部资产保留。"""
        p = self._project()
        s = p.deactivate(log)
        return ("汉化已停用（撤销/清理 %d 个工具管理的文件）；译文与恢复点都在，"
                "再次「应用到游戏」即恢复汉化" % s["files"])

    def _restore_op(self, apply_id):
        """还原到某次应用之前（恢复点对话框触发）。"""
        @project_task("应用")
        def op(log, prog, stop, confirm=None):
            p = self._project()
            s = p.restore_apply(apply_id, log)
            return "已还原到应用 %s 之前（%d 个文件；恢复点保留）" % (
                apply_id, s["files"])
        return op

    @project_task("统计")
    def _op_estimate(self, log, prog, stop, confirm=None):
        est = self._project().estimate(log)
        msg = "共 %d 行 / 已译 %d / 待译 %d（约 %d 字符）" % (
            est["lines"], est["done"], est["left"], est["chars"])
        log("工作量：" + msg + "；全部翻完预计约 %d tokens" % est["tokens_est"])
        self.sig_est.emit(msg)
        return "统计完成"

    # ---------- 词汇表 ----------
    def _gloss_path(self):
        return os.path.join(self._project().work, "glossary.json")

    def _gloss_load(self):
        try:
            data = read_json(self._gloss_path(), []) or []
        except Exception:
            data = []
        self.tbl_gloss.setRowCount(0)
        for g in data:
            self._gloss_append(g.get("src", ""), g.get("dst", ""))

    def _gloss_append(self, src, dst):
        r = self.tbl_gloss.rowCount()
        self.tbl_gloss.insertRow(r)
        self.tbl_gloss.setItem(r, 0, QTableWidgetItem(src))
        self.tbl_gloss.setItem(r, 1, QTableWidgetItem(dst))

    def _gloss_add(self):
        self._gloss_append("", "")

    def _gloss_del(self):
        rows = sorted({i.row() for i in self.tbl_gloss.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl_gloss.removeRow(r)

    def _gloss_save(self):
        data = []
        for r in range(self.tbl_gloss.rowCount()):
            s = (self.tbl_gloss.item(r, 0).text() if self.tbl_gloss.item(r, 0) else "").strip()
            d = (self.tbl_gloss.item(r, 1).text() if self.tbl_gloss.item(r, 1) else "").strip()
            if s and d:
                data.append({"src": s, "dst": d})
        write_json(self._gloss_path(), data)
        self._toast("词汇表已保存（%d 条）" % len(data))

    # ---------- 人物关系 ----------
    def _char_path(self):
        return os.path.join(self._project().work, "relations.json")

    def _char_load(self):
        try:
            data = read_json(self._char_path(), []) or []
        except Exception:
            data = []
        self.tbl_chars.setRowCount(0)
        for c in data:
            self._char_append(c)
        try:
            p = self._project()
            dump = p.load_dump()
            stats = rel.speaker_stats(dump, 12)
            if stats:
                self.lb_stats.setText("  |  说话人统计：" + "，".join("%s(%d句)" % (w or "旁白", n) for w, n in stats[:8]))
        except Exception:
            pass

    def _char_append(self, c):
        r = self.tbl_chars.rowCount()
        self.tbl_chars.insertRow(r)
        for col, key in enumerate(["speaker", "name", "name_cn", "gender", "relation", "note"]):
            self.tbl_chars.setItem(r, col, QTableWidgetItem(str(c.get(key, ""))))

    def _char_add(self):
        self._char_append({"speaker": "", "name": "", "gender": "", "relation": "", "note": ""})

    def _char_del(self):
        rows = sorted({i.row() for i in self.tbl_chars.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl_chars.removeRow(r)

    def _char_save(self):
        data = []
        for r in range(self.tbl_chars.rowCount()):
            c = {}
            for col, key in enumerate(["speaker", "name", "name_cn", "gender", "relation", "note"]):
                c[key] = (self.tbl_chars.item(r, col).text() if self.tbl_chars.item(r, col) else "").strip()
            if c["speaker"]:
                data.append(c)
        write_json(self._char_path(), data)
        self._toast("人物关系表已保存（%d 个角色）" % len(data))

    # ---------- 关系词显示表 ----------
    def _word_path(self):
        return os.path.join(self._project().work, "relation_words.json")

    def _word_load(self):
        try:
            data = read_json(self._word_path(), []) or []
        except Exception:
            data = []
        self.tbl_words.setRowCount(0)
        for w in data:
            if isinstance(w, dict):
                self._word_append(w.get("en", ""), w.get("cn", ""))

    def _word_append(self, en, cn):
        r = self.tbl_words.rowCount()
        self.tbl_words.insertRow(r)
        self.tbl_words.setItem(r, 0, QTableWidgetItem(str(en)))
        self.tbl_words.setItem(r, 1, QTableWidgetItem(str(cn)))

    def _word_add(self):
        self._word_append("", "")

    def _word_common(self):
        """一键列出常用关系词：默认中文取自内置表，改长幼即可（brother → 弟弟）。"""
        have = set()
        for r in range(self.tbl_words.rowCount()):
            it = self.tbl_words.item(r, 0)
            if it and it.text().strip():
                have.add(it.text().strip().lower())
        added = 0
        for en, cn in fontpatch.COMMON_RELATION_WORDS.items():
            if en not in have:
                self._word_append(en, cn)
                added += 1
        if added:
            self._toast("已列出 %d 个常用关系词；按本游戏的长幼改中文即可（如 brother → 哥哥）" % added)
        else:
            self._toast("常用关系词都已列出")

    def _word_del(self):
        rows = sorted({i.row() for i in self.tbl_words.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl_words.removeRow(r)

    def _word_save(self):
        data = []
        for r in range(self.tbl_words.rowCount()):
            en = (self.tbl_words.item(r, 0).text() if self.tbl_words.item(r, 0) else "").strip()
            cn = (self.tbl_words.item(r, 1).text() if self.tbl_words.item(r, 1) else "").strip()
            if en and cn:
                data.append({"en": en, "cn": cn})
        write_json(self._word_path(), data)
        self._toast("关系词显示表已保存（%d 条）；执行第 6 步或「应用修改到游戏」后生效" % len(data))

    # ---------- 设置 ----------
    def _profiles(self):
        return self.cfg.setdefault("profiles", [])

    def _reload_profiles(self, sel=0):
        """刷新方案下拉框；sel 为要选中的列表项下标（0 = 空项）。"""
        self.cmb_profile.blockSignals(True)
        self.cmb_profile.clear()
        self.cmb_profile.addItem("— 选择已保存的接口 —")
        for p in self._profiles():
            self.cmb_profile.addItem(p.get("name") or "未命名")
        self.cmb_profile.setCurrentIndex(max(0, min(sel, self.cmb_profile.count() - 1)))
        self.cmb_profile.blockSignals(False)

    def _profile_selected(self, idx):
        ps = self._profiles()
        if idx <= 0 or idx - 1 >= len(ps):
            return
        p = ps[idx - 1]
        e = self.cfg["engine"]
        # 方案里有的键全部覆盖（含温度/并发/批次/上限/提示词等），方案里没有的键保持不动
        for k in PROFILE_KEYS:
            if k in p:
                e[k] = p[k]
        self._fill_engine_fields(e)
        self.cfg.save()
        self._toast("已填入方案「%s」：接口与全部参数" % (p.get("name") or ""))

    def _profile_save(self):
        base = self.ed_base.text().strip()
        if not base:
            self._toast("请先填写接口地址，再保存为方案")
            return
        host = re.sub(r"^https?://", "", base).split("/")[0].strip() or "方案"
        name, ok = QInputDialog.getText(self, "保存接口方案",
                                        "方案名称（同名会覆盖更新）：", text=host)
        if not ok:
            return
        name = name.strip() or host
        self._collect()   # 先把界面上的参数写回配置，再整份存进方案
        entry = {"name": name}
        entry.update({k: self.cfg["engine"].get(k) for k in PROFILE_KEYS
                      if k in self.cfg["engine"]})
        ps = self._profiles()
        pos = next((i for i, p in enumerate(ps) if p.get("name") == name), None)
        if pos is None:
            ps.append(entry)
            pos = len(ps) - 1
        else:
            ps[pos] = entry
        self.cfg.save()
        self._reload_profiles(pos + 1)
        self._toast("已保存方案「%s」（含接口、模型、温度/并发/批次/上限等全部参数）" % name)

    def _profile_del(self):
        idx = self.cmb_profile.currentIndex()
        ps = self._profiles()
        if idx <= 0 or idx - 1 >= len(ps):
            self._toast("请先在下拉框选中要删除的方案")
            return
        name = ps[idx - 1].get("name") or ""
        del ps[idx - 1]
        self.cfg.save()
        self._reload_profiles()
        self._toast("已删除方案「%s」" % name)

    def _sync_scan_ui(self):
        custom = self.cmb_scan.currentIndex() == 1
        for w in (self.ed_scan_base, self.ed_scan_key, self.cmb_scan_model,
                  self.btn_scan_fetch, self.btn_scan_verify):
            w.setEnabled(custom)

    def _fill_engine_fields(self, e):
        """把 engine 配置填进设置页控件（切方案时也用这个，保证界面上看到的就是实际生效的）。"""
        self.ed_base.setText(e.get("base_url", ""))
        self.ed_key.setText(e.get("api_key", ""))
        self.cmb_model.setCurrentText(e.get("model", ""))
        self.sp_temp.setValue(float(e.get("temperature", 0.6)))
        self.sp_pen.setValue(float(e.get("presence_penalty", 0) or 0))
        self.sp_conc.setValue(int(e.get("concurrency", 8)))
        self.sp_batch.setValue(int(e.get("batch_size", 12)))
        self.sp_maxtok.setValue(int(e.get("max_tokens", 0) or 0))
        self.sp_prompt.setValue(int(e.get("max_prompt_tokens", 0) or 0))
        self.sp_timeout.setValue(int(e.get("timeout", 240)))
        self.sp_ctx.setValue(int(e.get("context_lines", 2)))
        self.cmb_think.setCurrentIndex(max(0, self.cmb_think.findData(e.get("thinking", "auto"))))
        self.ed_prompt.setPlainText(e.get("system_prompt", "") or DEFAULT_SYSTEM_PROMPT)

    def _load_fields(self):
        e = self.cfg["engine"]
        self._fill_engine_fields(e)
        match = next((i + 1 for i, p in enumerate(self._profiles())
                      if p.get("base_url", "") == e.get("base_url", "")), 0)
        self._reload_profiles(match)
        s = self.cfg.get("scan", {})
        self.cmb_scan.setCurrentIndex(1 if s.get("use") == "custom" else 0)
        self.ed_scan_base.setText(s.get("base_url", ""))
        self.ed_scan_key.setText(s.get("api_key", ""))
        self.cmb_scan_model.setCurrentText(s.get("model", ""))
        self.sp_sample.setValue(int(s.get("sample_per_char", 40)))
        self.ed_lang.setText(self.cfg.get("language", "chinese"))
        self.ed_font.setText(self.cfg.get("font_path", ""))
        self.ed_f95cookie.setText(self.cfg.get("f95_cookie", ""))
        self.chk_font.setChecked(self.cfg.get("apply_font", True))
        self.chk_lang.setChecked(self.cfg.get("apply_lang_entry", True))
        self.chk_strings.setChecked(self.cfg.get("translate_strings", True))
        self.chk_autoupd.setChecked(self.cfg.get("auto_check_updates", True))
        self.ed_game.setText(self.cfg.get("last_game", ""))
        self._restore_last_project()
        self._sync_scan_ui()

    def _restore_last_project(self):
        """启动恢复上次的游戏目录：只查找已登记的汉化项目并记录打开时间。

        不登记、不弹窗：目录未登记（如升级首次启动）或目录已移动/改名时，
        留待用户主动选择目录——届时才会发生登记或"重新定位"确认，
        避免启动时静默创建汉化项目或打断启动流程。"""
        last = self.ed_game.text().strip()
        if not last or not os.path.isdir(os.path.join(last, "game")):
            return
        reg = projreg.Registry()
        try:
            cur = reg.find_by_path(last)
            if cur is not None:
                reg.touch(cur["id"])
        except Exception:
            pass
        finally:
            reg.close()

    def _collect(self):
        e = self.cfg["engine"]
        e.update({"base_url": self.ed_base.text().strip(), "api_key": self.ed_key.text().strip(),
                  "model": self.cmb_model.currentText().strip(), "temperature": self.sp_temp.value(),
                  "presence_penalty": self.sp_pen.value(),
                  "concurrency": self.sp_conc.value(), "batch_size": self.sp_batch.value(),
                  "timeout": self.sp_timeout.value(),
                  "max_tokens": self.sp_maxtok.value(),
                  "max_prompt_tokens": self.sp_prompt.value(),
                  "context_lines": self.sp_ctx.value(),
                  "thinking": self.cmb_think.currentData() or "auto",
                  "system_prompt": self.ed_prompt.toPlainText()})
        self.cfg["scan"] = {"use": "custom" if self.cmb_scan.currentIndex() == 1 else "same",
                            "base_url": self.ed_scan_base.text().strip(),
                            "api_key": self.ed_scan_key.text().strip(),
                            "model": self.cmb_scan_model.currentText().strip(),
                            "sample_per_char": self.sp_sample.value()}
        self.cfg["language"] = self.ed_lang.text().strip() or "chinese"
        self.cfg["font_path"] = self.ed_font.text().strip()
        self.cfg["f95_cookie"] = self.ed_f95cookie.text().strip()
        self.cfg["apply_font"] = self.chk_font.isChecked()
        self.cfg["apply_lang_entry"] = self.chk_lang.isChecked()
        self.cfg["translate_strings"] = self.chk_strings.isChecked()
        self.cfg["auto_check_updates"] = self.chk_autoupd.isChecked()
        self.cfg["last_game"] = self.ed_game.text().strip()

    def _save_settings(self):
        self._collect()
        self.cfg.save()
        self._toast("设置已保存")

    def _test_engine(self):
        from core.translator import test_engine
        self._collect()
        e = dict(self.cfg["engine"])
        e["timeout"] = 30
        try:
            r = test_engine(e)
            self.lb_engine.setText("✅ 连接成功：%s" % r)
        except Exception as ex:
            self.lb_engine.setText("❌ 失败：%s" % ex)

    def _fetch_models(self, which):
        """按当前填写的接口地址/Key 拉取模型列表，填入对应下拉框。"""
        from core.translator import list_models
        if which == "engine":
            cfg = {"base_url": self.ed_base.text().strip(), "api_key": self.ed_key.text().strip()}
            combo, btn = self.cmb_model, self.btn_fetch
        else:
            cfg = {"base_url": self.ed_scan_base.text().strip(), "api_key": self.ed_scan_key.text().strip()}
            combo, btn = self.cmb_scan_model, self.btn_scan_fetch
        if not cfg["base_url"]:
            self._toast("请先填写接口地址")
            return
        cfg["timeout"] = 20
        btn.setEnabled(False)
        btn.setText("拉取中…")
        t = FetchThread(lambda: list_models(cfg))

        def on_done(ok, res):
            btn.setEnabled(True)
            btn.setText("⟳ 拉取模型")
            if not ok:
                msg = "❌ 拉取模型失败：%s" % res
                if which == "engine":
                    self.lb_engine.setText(msg)
                else:
                    self._toast(msg)
                return
            cur = combo.currentText().strip()
            combo.clear()
            if "openrouter" in cfg["base_url"].lower():
                # OpenRouter：免费模型（:free 后缀）排前面，方便挑选
                res = sorted(res, key=lambda m: not m.endswith(":free"))
            combo.addItems(res)
            combo.setCurrentText(cur if cur in res else (res[0] if res else ""))
            msg = "✅ 获取到 %d 个模型%s" % (
                len(res), "（列表前段为免费 :free 模型）" if "openrouter" in cfg["base_url"].lower() else "")
            if which == "engine":
                self.lb_engine.setText(msg)
            else:
                self._toast(msg)

        t.sig_done.connect(on_done)
        self._fetch_threads.append(t)  # 持引用防回收
        t.start()

    def _verify_model(self, which):
        """校验手动填写的单个模型（不拉全表）。"""
        from core.translator import verify_model
        if which == "engine":
            cfg = {"base_url": self.ed_base.text().strip(), "api_key": self.ed_key.text().strip()}
            combo, btn = self.cmb_model, self.btn_verify
        else:
            cfg = {"base_url": self.ed_scan_base.text().strip(), "api_key": self.ed_scan_key.text().strip()}
            combo, btn = self.cmb_scan_model, self.btn_scan_verify
        mid = combo.currentText().strip()
        if not mid:
            self._toast("请先在模型框里填写要校验的模型名")
            return
        if not cfg["base_url"]:
            self._toast("请先填写接口地址")
            return
        cfg.update({"timeout": 30, "temperature": 0.1})
        btn.setEnabled(False)
        btn.setText("…")
        t = FetchThread(lambda: verify_model(cfg, mid))

        def on_done(ok, res):
            btn.setEnabled(True)
            btn.setText("✓")
            if not ok:
                msg = "❌ 校验失败：%s" % res
            elif res.get("via") == "models":
                info = res.get("info") or {}
                parts = ["✅ 模型存在：%s" % (info.get("name") or mid)]
                cl = info.get("context_length")
                if cl:
                    parts.append("上下文 %s" % ("%.0fk" % (cl / 1000) if cl >= 1000 else cl))
                pr = info.get("pricing") or {}
                try:
                    p_in = float(pr.get("prompt") or 0) * 1e6
                    p_out = float(pr.get("completion") or 0) * 1e6
                    if p_in == 0 and p_out == 0:
                        parts.append("免费")
                    else:
                        parts.append("$%.2f / $%.2f 每百万token(入/出)" % (p_in, p_out))
                except (TypeError, ValueError):
                    pass
                msg = "；".join(parts)
            else:
                msg = "✅ 模型可用（对话实测通过）：%s" % (res.get("reply") or "")
            if which == "engine":
                self.lb_engine.setText(msg)
            else:
                self._toast(msg)

        t.sig_done.connect(on_done)
        self._fetch_threads.append(t)
        t.start()

    def _reset_prompt(self):
        self.ed_prompt.setPlainText(DEFAULT_SYSTEM_PROMPT)
        self._toast("已恢复内置默认提示词（点“保存设置”生效）")

    def _toast(self, msg):
        self.statusBar().showMessage(msg, 4000)

    def closeEvent(self, ev):
        """关窗口时先让后台版本检查收尾（已查到的结果早都存过库了）。"""
        try:
            self.lib_page.stop_update_check()
        except Exception:
            pass
        super().closeEvent(ev)


def _app_icon():
    """应用图标：resources/icon.ico（多尺寸，标题栏/任务栏自动取合适大小）。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("icon.ico", "icon.png"):
        p = os.path.join(base, "resources", name)
        if os.path.isfile(p):
            icon = QIcon(p)
            if not icon.isNull():
                return icon
    return None


def run():
    app = QApplication(sys.argv)
    theme.set_dark(Config().get("theme", "dark") != "light")   # 启动即应用上次的主题选择
    app.setStyleSheet(theme.build_qss())
    icon = _app_icon()
    if icon:
        app.setWindowIcon(icon)
    # 以 pythonw 运行时没有控制台，未捕获异常会无声无息地退出——改为弹窗展示
    sys.excepthook = lambda t, v, tb: QMessageBox.critical(
        None, "程序错误",
        "".join(traceback.format_exception(t, v, tb))[-2000:])
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
