# -*- coding: utf-8 -*-
"""RenpyTranslatorNG 主界面。"""
import html
import json
import os
import re
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QThread, Signal, QRegularExpression, QEvent, QTimer
from PySide6.QtGui import (QBrush, QColor, QIcon, QLinearGradient, QPainter,
                           QPixmap, QRegularExpressionValidator, QRadialGradient,
                           QTextDocument)
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.config import Config
from core.pipeline import Project
from core import fontpatch
from core import library as gamelib
from core import relations as rel
from core import registry as projreg
from core import updates as upd
from core.translator import DEFAULT_SYSTEM_PROMPT
from core.util import read_json, write_json

from . import theme
from .library import LibraryPage

_HL_SPAN = '<span style="background:#6e5aef;color:#ffffff;">%s</span>'

# 接口方案保存/还原的参数范围：换接口时这些都要跟着走，不必重新调一遍
PROFILE_KEYS = ("base_url", "api_key", "model", "temperature", "presence_penalty",
                "thinking", "concurrency", "batch_size", "context_lines", "timeout",
                "max_retry", "max_tokens", "max_prompt_tokens", "system_prompt")


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


class MainWindow(QMainWindow):
    sig_est = Signal(str)  # 工作线程里安全更新“统计工作量”标签

    def __init__(self):
        super().__init__()
        self.cfg = Config()
        self.worker = None
        self._running_fn = None
        self._fetch_threads = []
        self._edit_groups = None
        self._edit_path = ""
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
            ("finish", "6. 字体+语言+角色名", "中文字体全覆盖 + 设置内语言切换 + 角色中文名"),
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
        self.btn_fill = QPushButton("⤵ 回填已译部分（试玩模式）")
        self.btn_fill.setToolTip(
            "不调用 API、不花钱：把当前已翻译的译文写入 tl 并应用字体/语言入口。\n"
            "未翻译的行在游戏里显示英文原文——先试玩一部分，再决定是否继续翻译。")
        self.btn_fill.clicked.connect(lambda: self._run(self._op_fill))
        self.btn_retrans = QPushButton("↻ 重译未译句子")
        self.btn_retrans.setToolTip(
            "修复翻译失败：只把缓存里没有译文的句子重新发给 AI（已译内容绝不重复请求，最省钱），\n"
            "每句至多附前后各 1 句上下文；完成后自动回填。反复执行可逐步清零失败句。")
        self.btn_retrans.clicked.connect(lambda: self._run(self._op_retrans))
        row_util.addWidget(self.btn_fill)
        row_util.addWidget(self.btn_retrans)
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
        card, cv = self._card("译文修改（修正译名/称谓不一致——不必重新翻译）")
        hint = QLabel("搜索译文中的关键词（如 哥哥/弟弟/某人名），结果会高亮显示；双击“译文”列可单独修改一条，"
                      "或在下面填替换词后一键替换全部。修改立即写入缓存，点「应用修改到游戏」回填生效。")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        cv.addWidget(hint)
        row = QHBoxLayout()
        self.ed_search = QLineEdit(placeholderText="关键词，如：哥哥、Guard、某人名…")
        self.cmb_scope = QComboBox()
        self.cmb_scope.addItems(["搜译文", "搜原文", "两者都搜"])
        self.btn_search = QPushButton("🔍 搜索")
        self.btn_search.setObjectName("primary")
        self.btn_search.clicked.connect(self._edit_search)
        self.ed_search.returnPressed.connect(self._edit_search)
        row.addWidget(self.ed_search, 1)
        row.addWidget(self.cmb_scope)
        row.addWidget(self.btn_search)
        cv.addLayout(row)
        self.lb_search = QLabel("请先在①页选择游戏目录")
        self.lb_search.setWordWrap(True)
        self.lb_search.setObjectName("dim")
        cv.addWidget(self.lb_search)
        self.tbl_edit = QTableWidget(0, 3)
        self.tbl_edit.setHorizontalHeaderLabels(["说话人", "原文 (English)", "译文（双击修改）"])
        self.tbl_edit.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_edit.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_edit.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_edit.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_edit.setWordWrap(True)
        self.tbl_edit.setItemDelegate(RichTextDelegate(self.tbl_edit))
        self.tbl_edit.verticalHeader().setDefaultSectionSize(26)
        self.tbl_edit.doubleClicked.connect(self._edit_dblclick)
        cv.addWidget(self.tbl_edit, 1)
        row2 = QHBoxLayout()
        self.ed_replace = QLineEdit(placeholderText="替换为…")
        btn_replace = QPushButton("一键替换全部")
        btn_replace.setToolTip("把所有译文中出现的关键词（上面搜索框里的词）替换为这里填的词")
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

    # ---------- 译文修改 ----------
    def _edit_reload(self):
        """(重新)载入当前游戏的 jobs + 译文缓存，按唯一文本分组。"""
        self._edit_groups = None
        self._edit_kw = ""
        try:
            p = self._project()
        except Exception as e:
            self.lb_search.setText(str(e))
            return False
        jobs = read_json(os.path.join(p.work, "jobs.json"), None)
        if not jobs:
            try:
                jobs, _ = p._jobs(lambda s: None)
            except Exception as e:
                self.lb_search.setText("加载失败：%s（先执行提取和翻译）" % e)
                return False
        trans = read_json(os.path.join(p.work, "translations.json"), {}) or {}
        groups, bytext = [], {}
        for j in jobs:
            if not j.get("old"):
                continue
            g = bytext.get(j["old"])
            if g is None:
                g = {"old": j["old"], "who": j.get("who", ""), "file": j.get("file", ""),
                     "keys": [], "trans": ""}
                bytext[j["old"]] = g
                groups.append(g)
            g["keys"].append(j["key"])
        for g in groups:
            for k in g["keys"]:
                t = trans.get(k)
                if t:
                    g["trans"] = t
                    break
        self._edit_groups = groups
        self._edit_path = os.path.join(p.work, "translations.json")
        done = sum(1 for g in groups if g["trans"])
        self.lb_search.setText("已载入：%d 条唯一文本，其中已译 %d 条。输入关键词开始搜索" % (len(groups), done))
        return True

    def _edit_search(self):
        if self._edit_groups is None and not self._edit_reload():
            return
        kw = self.ed_search.text().strip()
        self._edit_kw = kw
        scope = self.cmb_scope.currentIndex()  # 0译文 1原文 2两者
        rows = []
        for g in self._edit_groups:
            if not kw:
                rows.append(g)
                continue
            in_t = kw.lower() in (g["trans"] or "").lower()
            in_o = kw.lower() in g["old"].lower()
            if (scope == 0 and in_t) or (scope == 1 and in_o) or (scope == 2 and (in_t or in_o)):
                rows.append(g)
        cap = 800
        shown = rows[:cap]
        self.tbl_edit.setRowCount(0)
        for g in shown:
            r = self.tbl_edit.rowCount()
            self.tbl_edit.insertRow(r)
            who = g["who"] or ""
            if who.startswith('"') and who.endswith('"'):
                who = who[1:-1]
            items = [who or "(旁白)", g["old"], g["trans"] or "（未翻译）"]
            for col, txt in enumerate(items):
                it = QTableWidgetItem()
                it.setText(_hl(txt, kw) if kw else html.escape(txt))
                it.setData(Qt.ItemDataRole.UserRole, txt)
                it.setToolTip("%s\n%s" % (g["file"], g["keys"][0] if g["keys"] else ""))
                self.tbl_edit.setItem(r, col, it)
            self.tbl_edit.setRowHeight(r, 26)
        more = "（仅显示前 %d 条）" % cap if len(rows) > cap else ""
        self.lb_search.setText("命中 %d 条%s" % (len(rows), more))

    def _edit_dblclick(self, index):
        if index.column() != 2 or self._edit_groups is None:
            return
        r = index.row()
        old = self.tbl_edit.item(r, 1).data(Qt.ItemDataRole.UserRole)
        cur = self.tbl_edit.item(r, 2).data(Qt.ItemDataRole.UserRole)
        if cur == "（未翻译）":
            cur = ""
        txt, ok = QInputDialog.getMultiLineText(self, "修改译文", "原文：\n" + old[:500], cur)
        if not ok:
            return
        # 行号 -> 分组：行按搜索结果顺序生成，重新按原文找组
        g = next((x for x in self._edit_groups if x["old"] == old), None)
        if not g:
            return
        g["trans"] = txt
        self._edit_persist([g])
        item = self.tbl_edit.item(r, 2)
        item.setText(_hl(txt, self._edit_kw) if self._edit_kw else html.escape(txt))
        item.setData(Qt.ItemDataRole.UserRole, txt)
        self._toast("已保存并写入缓存")

    def _edit_replace_all(self):
        if self._edit_groups is None and not self._edit_reload():
            return
        kw = self.ed_search.text().strip()
        repl = self.ed_replace.text()
        if not kw:
            QMessageBox.information(self, "替换", "请先在搜索框填写要替换的关键词")
            return
        hits = [g for g in self._edit_groups if kw.lower() in (g["trans"] or "").lower()]
        if not hits:
            QMessageBox.information(self, "替换", "没有译文包含关键词「%s」" % kw)
            return
        total = sum((g["trans"] or "").lower().count(kw.lower()) for g in hits)
        box = QMessageBox(self)
        box.setWindowTitle("确认替换")
        box.setText("将把 %d 条译文中的「%s」（共 %d 处）全部替换为「%s」。\n替换会立即写入缓存，"
                    "之后点「应用修改到游戏」回填生效。" % (len(hits), kw, total, repl))
        box.addButton("替换", QMessageBox.YesRole)
        box.addButton("取消", QMessageBox.NoRole)
        if box.exec() != 0:
            return
        for g in hits:
            g["trans"] = _ci_replace(g["trans"], kw, repl)
        self._edit_persist(hits)
        self._edit_search()
        self._toast("已替换 %d 条译文并写入缓存" % len(hits))

    def _edit_fix_tags(self):
        """（已改为后台任务 _op_repair；此方法保留给老入口）"""
        self._run(self._op_repair)

    def _edit_persist(self, groups):
        """把分组译文写回 translations.json（该组所有 key 同步，保证续翻不再重译）。"""
        trans = read_json(self._edit_path, {}) or {}
        for g in groups:
            for k in g["keys"]:
                trans[k] = g["trans"]
        write_json(self._edit_path, trans)

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
        bapply = QPushButton("仅应用字体")
        bapply.setToolTip("把上面选的字体立即应用到所选游戏，无需重新提取/翻译")
        bapply.clicked.connect(lambda: self._run(self._op_font_only))
        rowf.addWidget(self.ed_font, 1)
        rowf.addWidget(bf)
        rowf.addWidget(bapply)
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

    def _project(self):
        base = self.ed_game.text().strip()
        if not base or not os.path.isdir(os.path.join(base, "game")):
            raise RuntimeError("请先选择有效的游戏根目录（需包含 game/ 子目录）")
        self._collect()
        proj, _action = self._resolve_project(base)
        return Project(self.cfg, base, proj["id"])

    def _run(self, fn):
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
        self.lib_page.set_busy(True)
        self.lib_page.pause_update_check(True)   # 版本检查让路：不抢网络、不抢写库
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸ 暂停")
        self._side_state("busy", "任务运行中")
        self._running_fn = fn
        self.statusBar().showMessage("任务开始…", 0)
        self.worker.start()

    def _on_log(self, msg):
        self.log.appendPlainText(str(msg))

    def _on_prog(self, cur, total):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(cur)
        msg = "%d / %d（%.1f%%）" % (cur, total, 100.0 * cur / max(1, total))
        self.lb_prog.setText(msg)
        self.statusBar().showMessage(msg, 0)

    def _on_confirm(self, message):
        if message.startswith("RELATIONS:"):
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
        for b in self.steps + [self.btn_estimate, self.btn_scan, self.btn_fill,
                               self.btn_retrans, self.btn_runall,
                               self.btn_edit_apply, self.btn_edit_fix]:
            b.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ 暂停")
        self.lib_page.set_busy(False)
        # 游戏库自己的任务（扫描/刷新）结束后重建卡片墙
        if getattr(self._running_fn, "__self__", None) is self.lib_page:
            self.lib_page.reload()
        self.lib_page.pause_update_check(False)  # 版本检查接着上次继续
        paused = bool(self.worker and self.worker._stop)
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
        # 编辑页数据可能已被任务改写（回填/修复），在用则刷新
        if self._edit_groups is not None:
            try:
                self._edit_reload()
            except Exception:
                pass

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
        path = os.path.join(p.work, "translations.json")
        if not os.path.isfile(path):
            self._toast("当前游戏没有译文缓存")
            return
        ret = QMessageBox.question(
            self,
            "确认清空译文缓存",
            "即将删除全部本地译文进度（translations.json）。\n"
            "已翻译的内容将全部丢失，重跑第5步会全文重新翻译。\n\n"
            "确定要继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        os.replace(path, path + ".bak")
        self._toast("译文缓存已清空（备份为 translations.json.bak），重跑第5步即全文重译")

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
        p.apply_font(log)
        p.apply_lang_entry(log)
        if use_relations:
            p.apply_names(log)
        if paused:
            return "已暂停：已译部分已回填 tl，可先试玩；继续翻译请再执行第 5 步（自动续翻）"
        return "全流程完成"

    def _op_unpack(self, log, prog, stop, confirm=None):
        return self._project().unpack(log, prog, stop)

    def _op_decompile(self, log, prog, stop, confirm=None):
        return self._project().decompile(log, stop)

    def _op_extract(self, log, prog, stop, confirm=None):
        n = self._project().extract_tl(log, stop)
        return "提取完成：%d 个对白块" % n

    def _op_scan(self, log, prog, stop, confirm=None):
        p = self._project()
        chars = p.scan_relations(log, stop)
        self._char_load()
        self._word_load()
        return "关系草表生成完成：%d 个角色，请审核" % len(chars)

    def _op_translate(self, log, prog, stop, confirm=None):
        n, missing = self._project().translate(log, prog, stop)
        return "回填 %d 条，未译 %d 条" % (n, missing)

    def _op_fill(self, log, prog, stop, confirm=None):
        n, missing = self._project().fill_partial(log)
        return "已回填 %d 条译文（未译 %d 条显示英文），可以试玩了" % (n, missing)

    def _op_retrans(self, log, prog, stop, confirm=None):
        n, missing = self._project().retranslate_missing(log, prog, stop)
        if n == 0 and missing == 0:
            return "没有未译内容，无需重译"
        return "重译并回填 %d 条，剩余未译 %d 条" % (n, missing)

    def _op_repair(self, log, prog, stop, confirm=None):
        n = self._project().repair_cache(log)
        if n:
            return "已修复 %d 条标签错乱；请点「应用修改到游戏」回填生效" % n
        log("缓存没有需要修复的标签。")
        log("若游戏里仍看到 /i 之类乱码：乱码在 tl 文件里而不在缓存，"
            "直接点「⤵ 应用修改到游戏」重新回填即可修复，不需要重新翻译。")
        return "缓存无需修复；游戏内乱码请点「应用修改到游戏」重新回填"

    def _op_finish(self, log, prog, stop, confirm=None):
        p = self._project()
        p.apply_font(log)
        p.apply_lang_entry(log)
        p.apply_names(log)
        return "字体、语言入口、人名映射、输入助手已应用"

    def _op_font_only(self, log, prog, stop, confirm=None):
        self._project().apply_font(log)
        return "字体已应用（无需重新翻译）"

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
