# -*- coding: utf-8 -*-
"""游戏库页面：封面卡片墙（图鉴式），支持扫描磁盘 / 手动添加 / 启动 / 换封面 / 查评分。"""
import math
import os
import subprocess
import time

import requests
from PySide6.QtCore import QDir, QRect, QRectF, QSize, Qt, QThread, Signal, QUrl
from PySide6.QtGui import (QBrush, QColor, QCursor, QDesktopServices, QFont,
                           QImage, QLinearGradient, QPainter, QPainterPath,
                           QPixmap)
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QLayout,
    QMenu, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from core import coverfetch
from core import library as gamelib
from core import ratings
from core import updates
from core.library import Library

from . import theme

CARD_W, CARD_H, COVER_W, COVER_H = 186, 296, 170, 220
_RADIUS = 14
STAR_H, STAR_GAP = 11, 1  # 评分星星的直径与间距

# 占位封面的渐变配色（按游戏名散列取色，保证同名游戏每次颜色一致）
_PALETTES = (
    ("#7c5cff", "#4636b8"), ("#00b8a9", "#0a6e8a"), ("#ff6b9a", "#b23a6b"),
    ("#f5a623", "#c25e00"), ("#2ecc71", "#137a4b"), ("#5d9dff", "#2a4fc0"),
    ("#e05c8a", "#7a3cc0"), ("#38bdf8", "#1d4ed8"),
)


def _key(path):
    """游戏路径的比较键（大小写/斜杠差异不影响匹配）。"""
    return os.path.normcase(os.path.normpath(path or ""))


def _ago(ts):
    """时间戳 → 「3 小时前」这类相对时间（版本信息是手动查的，得能看出新不新）。"""
    try:
        sec = max(0, int(time.time() - float(ts or 0)))
    except (TypeError, ValueError):
        return ""
    if sec < 90:
        return "刚刚"
    if sec < 3600:
        return "%d 分钟前" % (sec // 60)
    if sec < 86400:
        return "%d 小时前" % (sec // 3600)
    return "%d 天前" % (sec // 86400)


class FlowLayout(QLayout):
    """流式布局：卡片按可用宽度自动换行（窗口拉宽/缩窄都会重排）。"""

    def __init__(self, parent=None, spacing=14):
        super().__init__(parent)
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do_layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            next_x = x + hint.width() + self.spacing()
            if next_x - self.spacing() > rect.right() + 1 and line_h > 0:
                x = rect.x()
                y += line_h + self.spacing()
                next_x = x + hint.width() + self.spacing()
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


def _rounded(src, w, h, radius, dpr=1.0):
    """把图片缩放裁剪成圆角卡片（居中填充裁剪，等价于 CSS object-fit: cover）。"""
    tw, th = int(w * dpr), int(h * dpr)
    scaled = src.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    sx = max(0, (scaled.width() - tw) // 2)
    sy = max(0, (scaled.height() - th) // 2)
    out = QPixmap(tw, th)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, tw, th, radius * dpr, radius * dpr)
    p.setClipPath(path)
    p.drawPixmap(-sx, -sy, scaled)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


def _placeholder(name, w, h, dpr=1.0):
    """没有封面图时生成的占位封面：柔光渐变 + 游戏名首字（同色系、不突兀）。"""
    tw, th = int(w * dpr), int(h * dpr)
    pm = QPixmap(tw, th)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c1, c2 = _PALETTES[sum(map(ord, (name or "?"))) % len(_PALETTES)]
    g = QLinearGradient(0, 0, tw, th)
    g.setColorAt(0.0, QColor(c1))
    g.setColorAt(1.0, QColor(c2))
    p.setBrush(QBrush(g))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, tw, th, _RADIUS * dpr, _RADIUS * dpr)
    # 顶部柔光提亮一点层次
    hi = QLinearGradient(0, 0, 0, th * 0.6)
    hi.setColorAt(0.0, QColor(255, 255, 255, 46))
    hi.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(hi))
    p.drawRoundedRect(0, 0, tw, th, _RADIUS * dpr, _RADIUS * dpr)
    ch = (name or "🎮").strip()[:1] or "🎮"
    f = QFont("Segoe UI", int(th * 0.16), QFont.Weight.Bold)
    p.setFont(f)
    p.setPen(QColor(255, 255, 255, 120))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, ch.upper())
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def _star_path(cx, cy, r):
    """五角星路径（外顶点半径 r，内顶点 0.42r）。"""
    path = QPainterPath()
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.42
        x, y = cx + rad * math.cos(ang), cy + rad * math.sin(ang)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


class StarBar(QWidget):
    """五星评分条：按分数填充整星 / 半星，颜色跟随主题。"""

    def __init__(self, score, parent=None):
        super().__init__(parent)
        self.score = max(0.0, min(5.0, float(score or 0)))
        self.setFixedSize(5 * STAR_H + 4 * STAR_GAP, STAR_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r, cy = STAR_H / 2.0, STAR_H / 2.0
        on, off = QColor(theme.token("STAR_ON")), QColor(theme.token("STAR_OFF"))
        for i in range(5):
            cx = r + i * (STAR_H + STAR_GAP)
            star = _star_path(cx, cy, r - 0.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(off)
            p.drawPath(star)
            fill = max(0.0, min(1.0, self.score - i))
            if fill <= 0:
                continue
            p.save()
            p.setClipRect(QRectF(cx - r, 0, 2 * r * fill, STAR_H))
            p.setBrush(on)
            p.drawPath(star)
            p.restore()
        p.end()


def cover_pixmap(entry, w, h, dpr=1.0):
    """条目封面：优先用户/自动发现的封面图，其次占位渐变图。"""
    path = entry.get("cover") or ""
    if path and os.path.isfile(path):
        img = QImage(path)
        if not img.isNull():
            return _rounded(QPixmap.fromImage(img), w, h, _RADIUS, dpr)
    return _placeholder(entry.get("name") or "", w, h, dpr)


class GameCard(QFrame):
    """单个游戏卡片：圆角封面 + 状态角标 + 游戏名；点击弹出操作菜单。

    角标：右上角是汉化状态（已翻译/未翻译/缺失），左上角只在**确认有新版本**时
    才出现「可更新」——没有新版本时那里什么都不画。"""
    clicked = Signal()

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("gcard")
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 10)
        v.setSpacing(6)

        self.cover = QLabel(objectName="gcover")
        self.cover.setFixedSize(COVER_W, COVER_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dpr = int(self.devicePixelRatioF() or 1)
        self.cover.setPixmap(cover_pixmap(entry, COVER_W, COVER_H, dpr))
        missing = not os.path.isdir(entry.get("path", ""))
        badge = QLabel("缺失" if missing else
                       ("已翻译" if entry.get("translated") else "未翻译"))
        badge.setObjectName("badge_missing" if missing else
                            ("badge_ok" if entry.get("translated") else "badge_no"))
        badge.setParent(self.cover)
        badge.adjustSize()
        badge.move(COVER_W - badge.width() - 8, 8)
        badge.show()
        badge.raise_()
        v.addWidget(self.cover)

        self.badge_up = QLabel("", objectName="badge_update")
        self.badge_up.setParent(self.cover)
        self.badge_up.hide()
        self._missing = missing
        self.set_update(entry.get("update") or {})

        name = entry.get("name") or os.path.basename(entry.get("path", "")) or "未命名"
        self.lb_name = QLabel(name, objectName="gname")
        self.lb_name.setWordWrap(True)
        self.lb_name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.lb_name.setFixedHeight(30)
        v.addWidget(self.lb_name)

        rate = entry.get("ratings") or {}
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        row.addStretch(1)
        row.addWidget(StarBar(ratings.stars(rate)))
        self.lb_rate = QLabel(ratings.headline(rate) or "暂无评分", objectName="grate")
        row.addWidget(self.lb_rate)
        row.addStretch(1)
        v.addLayout(row)
        self._tooltip()

    def _tooltip(self):
        name = self.entry.get("name") or ""
        tip = "%s\n%s" % (name, self.entry.get("path", ""))
        info = ratings.summary(self.entry.get("ratings") or {})
        if info:
            tip += "\n\n⭐ %s" % info
        upd = updates.summary(self.entry.get("update") or {})
        if upd:
            tip += "\n\n%s" % upd
        self.setToolTip(tip)
        self.cover.setToolTip(tip)

    def set_update(self, info):
        """刷新「可更新」角标（检查过程中每到一条结果就调一次）。"""
        self.entry["update"] = info or {}
        text = "" if self._missing else updates.badge_text(info)
        self.badge_up.setText(text)
        self.badge_up.setVisible(bool(text))
        if text:
            self.badge_up.adjustSize()
            self.badge_up.move(8, 8)          # 左上角，与右上角的汉化角标互不遮挡
            self.badge_up.raise_()
        self._tooltip()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class UpdateChecker(QThread):
    """后台版本检查线程：逐个问 F95Zone / dikgames，不占主 Worker、不锁界面。

    用户发起正式任务（扫描/刷新/评分/翻译）时主窗口会调 pause()——本线程让出
    网络与 library.json 的写入权，等任务结束再继续，两边不抢 F95 的每小时额度。"""

    sig_log = Signal(str)
    sig_prog = Signal(int, int)
    sig_result = Signal(dict)     # 条目（含 update 字段）
    sig_done = Signal(str)

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.entries = entries
        self._stop = False
        self._pause = False

    def stop(self):
        self._stop = True

    def pause(self, on=True):
        self._pause = bool(on)

    def run(self):
        try:
            msg = updates.run_check(self.entries, log=self.sig_log.emit,
                                    prog=lambda c, t: self.sig_prog.emit(c, t),
                                    should_stop=lambda: self._stop,
                                    on_result=self.sig_result.emit,
                                    pause=lambda: self._pause)
            self.sig_done.emit(msg)
        except Exception as e:
            import traceback
            self.sig_log.emit(traceback.format_exc())
            self.sig_done.emit("检查更新出错：%s" % e)


class LibraryPage(QWidget):
    """游戏库页：顶栏操作 + 搜索筛选 + 已翻译/未翻译两个分区。"""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._kw = ""
        self._filter = 0  # 0 全部 / 1 已翻译 / 2 未翻译 / 3 可更新
        self._pending_roots = []
        self._pending_cover = None
        self._pending_rating = None
        self._cards = {}          # path key -> GameCard（版本检查时要就地刷角标）
        self._checker = None      # 后台版本检查线程
        self._check_results = {}
        self._build()
        self.reload()

    # ---------- 构建 ----------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 14)
        v.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("游戏库", objectName="h1")
        head.addWidget(title)
        self.lb_show = QLabel("", objectName="dim")
        head.addWidget(self.lb_show, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch(1)

        self.btn_add = QPushButton("＋ 添加游戏")
        self.btn_add.setToolTip("手动选择一个游戏根目录（含 game/ 子目录）加入库")
        self.btn_add.clicked.connect(self._add_manual)
        self.btn_scan_dir = QPushButton("📁 扫描文件夹")
        self.btn_scan_dir.setToolTip("递归扫描所选文件夹（最多 5 层），自动发现其中的 Ren'Py 游戏，"
                                     "并在游戏根目录找封面、联网补齐缺失封面")
        self.btn_scan_dir.clicked.connect(self._scan_folder)
        self.btn_scan_drive = QPushButton("💽 扫描整盘 ▾")
        self.btn_scan_drive.setToolTip("选择一个磁盘/分区整体扫描（大硬盘可能需要几分钟，可随时暂停）")
        self.btn_scan_drive.clicked.connect(self._scan_drive_menu)
        self.btn_ratings = QPushButton("⭐ 更新评分")
        self.btn_ratings.setToolTip("联网查询库里游戏的评分与评分人数：F95Zone（5 星制）优先，"
                                    "没收录的换 dikgames（10 分制，站点评分 + 用户均分）。\n"
                                    "已抓过的跳过（超过 30 天才重抓）；想刷新某一个是点卡片 → "
                                    "「🔄 刷新评分」")
        self.btn_ratings.clicked.connect(self._fetch_ratings)
        self.btn_update = QPushButton("🔍 检查更新")
        self.btn_update.setToolTip("联网查每个游戏有没有新版本（F95Zone 优先，dikgames 兜底），\n"
                                   "比对本机装的那版：确认有新版才在封面左上角标「可更新」。\n"
                                   "启动时已在后台静默查过一遍（设置页可关掉），这里是没查全时手动补查；\n"
                                   "12 小时内查过的会跳过，想全部重查会再问一次；\n"
                                   "只查某一个是点卡片 → 「🔍 检查更新」")
        self.btn_update.clicked.connect(self._check_updates)
        self.btn_rescan = QPushButton("⟳ 重新扫描")
        self.btn_rescan.setObjectName("primary")
        self.btn_rescan.setToolTip("刷新库中所有游戏的汉化状态，重新扫描之前扫过的目录，"
                                   "并联网补齐缺失封面（VNDB 同名搜索）")
        self.btn_rescan.clicked.connect(self._rescan)
        for b in (self.btn_add, self.btn_scan_dir, self.btn_scan_drive,
                  self.btn_ratings, self.btn_update, self.btn_rescan):
            head.addWidget(b)
        v.addLayout(head)

        frow = QHBoxLayout()
        self.ed_search = QLineEdit(placeholderText="按游戏名称搜索…")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._refilter)
        self.cmb_filter = QComboBox()
        self.cmb_filter.addItems(["全部状态", "✅ 已翻译", "🌐 未翻译", "🔄 可更新"])
        self.cmb_filter.currentIndexChanged.connect(self._refilter)
        frow.addWidget(self.ed_search, 1)
        frow.addWidget(self.cmb_filter)
        v.addLayout(frow)

        chips = QHBoxLayout()
        self.chip_all = QLabel("", objectName="chip")
        self.chip_ok = QLabel("", objectName="chip")
        self.chip_no = QLabel("", objectName="chip")
        self.chip_up = QLabel("", objectName="chip")
        for c in (self.chip_all, self.chip_ok, self.chip_no, self.chip_up):
            chips.addWidget(c)
        chips.addStretch(1)
        # 把「上次检查时间」摆在明面上，免得角标过期了看不出来
        self.lb_verinfo = QLabel("", objectName="dim")
        self.lb_verinfo.setToolTip("每次启动后会自动在后台静默刷新一遍版本信息（只补查 12 小时内\n"
                                   "没查过的游戏，设置页可关掉），也可点「🔍 检查更新」手动补查。\n"
                                   "评分不同：只在点「⭐ 更新评分」时联网。")
        chips.addWidget(self.lb_verinfo)
        v.addLayout(chips)

        self.body = QWidget()
        bv = QVBoxLayout(self.body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(16)
        self.lb_empty = QLabel(
            "库里还没有游戏。\n点右上角「＋ 添加游戏」手动加入，或用「📁 扫描文件夹 / 💽 扫描整盘」自动发现 Ren'Py 游戏。",
            objectName="empty")
        self.lb_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bv.addWidget(self.lb_empty)
        self._sections = {}
        for key, label in (("ok", "✅ 已翻译"), ("no", "🌐 未翻译")):
            sec = QLabel(label, objectName="sec")
            sec.hide()
            bv.addWidget(sec)
            holder = QWidget()
            flow = FlowLayout(holder, spacing=14)
            bv.addWidget(holder)
            self._sections[key] = (sec, holder, flow)
        bv.addStretch(1)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        sc.viewport().setAutoFillBackground(False)
        sc.setWidget(self.body)
        v.addWidget(sc, 1)

    # ---------- 数据 / 渲染 ----------
    def reload(self):
        """从 library.json 重建整页卡片（任务结束后也会自动调用）。"""
        lib = Library()
        entries = sorted(
            lib.games,
            key=lambda g: (bool(g.get("translated")),
                           not os.path.isdir(g.get("path", "")),
                           (g.get("name") or "").lower()))
        self._entries = entries
        ok = sum(1 for g in entries if g.get("translated") and os.path.isdir(g["path"]))
        no = len(entries) - ok
        self.chip_all.setText("📚 共 %d 款" % len(entries))
        self.chip_ok.setText("✅ 已翻译 %d" % ok)
        self.chip_no.setText("🌐 未翻译 %d" % no)
        self._update_chips()
        self._render()

    def _n_updates(self):
        """确认有新版本的游戏数（只算目录还在的）。"""
        return sum(1 for g in self._entries
                   if (g.get("update") or {}).get("state") == "update"
                   and os.path.isdir(g.get("path", "")))

    def _update_chips(self):
        n = self._n_updates()
        self.chip_up.setText("🔄 可更新 %d" % n)
        self.chip_up.setVisible(n > 0)
        ts = max([(g.get("update") or {}).get("checked") or 0 for g in self._entries] or [0])
        self.lb_verinfo.setText("版本信息：%s检查" % _ago(ts) if ts else "版本信息：未检查")

    def _render(self):
        kw = self._kw.lower()
        shown = []
        for g in self._entries:
            up = (g.get("update") or {}).get("state") == "update"
            if self._filter == 1 and not g.get("translated"):
                continue
            if self._filter == 2 and g.get("translated"):
                continue
            if self._filter == 3 and not up:
                continue
            if kw and kw not in (g.get("name") or "").lower() \
                    and kw not in (g.get("path") or "").lower():
                continue
            shown.append(g)
        self.lb_show.setText("显示 %d / %d" % (len(shown), len(self._entries)))

        # 清空旧卡片（takeAt 归还布局项）
        for key in self._sections:
            flow = self._sections[key][2]
            while flow.count():
                it = flow.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
        self._cards = {}
        for g in shown:
            key = "ok" if g.get("translated") else "no"
            card = GameCard(g)
            card.clicked.connect(lambda entry=g: self._card_menu(entry))
            self._sections[key][2].addWidget(card)
            self._cards[_key(g.get("path", ""))] = card
        has_any = bool(self._entries)
        self.lb_empty.setVisible(not has_any)
        for key, (sec, holder, flow) in self._sections.items():
            sec.setVisible(flow.count() > 0)
            holder.setVisible(flow.count() > 0)

    def _refilter(self):
        self._kw = self.ed_search.text().strip()
        self._filter = self.cmb_filter.currentIndex()
        self._render()

    # ---------- 操作 ----------
    def set_busy(self, busy):
        """有后台任务运行时锁定库操作，避免并发写 library.json。"""
        for b in (self.btn_add, self.btn_scan_dir, self.btn_scan_drive,
                  self.btn_ratings, self.btn_update, self.btn_rescan):
            b.setEnabled(not busy)

    def _add_manual(self):
        d = QFileDialog.getExistingDirectory(self, "选择游戏根目录（含 game/ 与启动 exe）")
        if not d:
            return
        if not gamelib.looks_like_game(d):
            QMessageBox.warning(self, "不是游戏目录",
                                "该目录看起来不是 Ren'Py 游戏（缺少 game/ 子目录）：\n%s" % d)
            return
        lib = Library()
        lib.add(d, "manual", self.win.cfg.get("language", "chinese"))
        lib.save()
        self.reload()
        self.win._toast("已添加：%s" % gamelib.derive_name(d))

    def _scan_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择要扫描的文件夹")
        if not d:
            return
        self._start_scan([d])

    def _scan_drive_menu(self):
        m = QMenu(self)
        for drv in QDir.drives():
            act = m.addAction(drv.absoluteFilePath())
            act.setData(drv.absoluteFilePath())
        chosen = m.exec(self.btn_scan_drive.mapToGlobal(
            self.btn_scan_drive.rect().bottomLeft()))
        if not chosen:
            return
        root = str(chosen.data())
        ret = QMessageBox.question(
            self, "扫描整盘",
            "将递归扫描整个磁盘 %s（最多 5 层目录），大硬盘可能需要几分钟。\n扫描可随时暂停，确定继续吗？" % root)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._start_scan([root])

    def _rescan(self):
        self.win._run(self._op_rescan)

    def _fetch_ratings(self):
        self.win._run(self._op_ratings)

    def _start_scan(self, roots):
        self._pending_roots = roots
        self.win._run(self._op_scan)

    # ---------- 版本检查（后台，不占主 Worker） ----------
    def auto_check(self):
        """启动时静默查一遍：只补查 12 小时内没查过的，全程不打扰用户。"""
        entries = [g for g in Library().games if os.path.isdir(g.get("path", ""))]
        if not entries:
            return
        if not any(updates.is_stale(g.get("update")) for g in entries):
            return                       # 都是刚查过的，这次启动无需联网
        self._start_check()

    def _check_updates(self):
        """「🔍 检查更新」按钮：先查没查过的；全查过了再问是否整库重查。"""
        lib = Library()
        todo = [g for g in lib.games if os.path.isdir(g.get("path", ""))
                and updates.is_stale(g.get("update"))]
        if not todo:
            ret = QMessageBox.question(
                self, "检查更新",
                "库里 %d 个游戏的版本都是最近查过的。\n\n要全部重新联网检查一遍吗？"
                "（每个游戏一次请求，比较慢）" % len(lib.games))
            if ret != QMessageBox.StandardButton.Yes:
                self.win._toast("版本检查：都是刚查过的，跳过")
                return
            todo = [g for g in lib.games if os.path.isdir(g.get("path", ""))]
        if not todo:
            self.win._toast("库里还没有游戏")
            return
        self._start_check(entries=todo)

    def _start_check(self, entries=None, paths=None, force=False):
        """启动后台版本检查线程：用户可以照常翻页、翻译、做别的事。"""
        if self._checker and self._checker.isRunning():
            self.win._toast("版本检查正在进行中…")
            return
        if entries is None:
            lib = Library()
            keys = {_key(p) for p in (paths or [])}
            entries = []
            for g in lib.games:
                if not os.path.isdir(g.get("path", "")):
                    continue
                if keys and _key(g.get("path", "")) not in keys:
                    continue
                if not force and not updates.is_stale(g.get("update")):
                    continue
                entries.append(g)
        if not entries:
            self.win._toast("没有需要检查的游戏")
            return
        self._check_results = {}
        self._checker = UpdateChecker(entries, self)
        self._checker.sig_log.connect(self._on_check_log)
        self._checker.sig_prog.connect(self._on_check_prog)
        self._checker.sig_result.connect(self._on_update_result)
        self._checker.sig_done.connect(self._on_check_done)
        self.btn_update.setEnabled(False)
        self.btn_update.setText("🔍 检查中…")
        self._on_check_prog(0, len(entries))
        self._checker.start()

    def _on_check_log(self, msg):
        self.win.statusBar().showMessage(str(msg), 6000)

    def _on_check_prog(self, cur, total):
        if self.win.worker and self.win.worker.isRunning():
            return                       # 用户任务优先，状态卡归它用
        self.win._side_state("busy", "检查更新 %d/%d" % (cur, total))
        self.btn_update.setText("🔍 检查中 %d/%d" % (cur, total))

    def _on_update_result(self, entry):
        """一条结果到达：就地刷角标 + 立刻存库（读-改-写，保住别的任务刚写的字段）。"""
        info = entry.get("update") or {}
        path = entry.get("path", "")
        k = _key(path)
        self._check_results[k] = info
        for g in self._entries:           # 当前页面的条目同步一份（筛选/计数用）
            if _key(g.get("path", "")) == k:
                g["update"] = info
                break
        lib = Library()
        g = lib.find(path)
        if g is not None:
            g["update"] = info
            lib.save()
        card = self._cards.get(k)
        if card is not None:
            card.set_update(info)
        self._update_chips()

    def _on_check_done(self, msg):
        checker, self._checker = self._checker, None
        if checker is not None:
            checker.deleteLater()
        self.btn_update.setEnabled(True)
        self.btn_update.setText("🔍 检查更新")
        # 收尾再合并一次：检查期间若被别的任务整份重写过 library.json，这里补回去
        if self._check_results:
            lib = Library()
            for g in lib.games:
                info = self._check_results.get(_key(g.get("path", "")))
                if info:
                    g["update"] = info
            lib.save()
            self._check_results = {}
        n_up = self._n_updates()
        for g in self._entries:            # 角标与最终结果对齐
            card = self._cards.get(_key(g.get("path", "")))
            if card is not None:
                card.set_update(g.get("update") or {})
        self._update_chips()
        if not (self.win.worker and self.win.worker.isRunning()):
            self.win._side_state("busy" if n_up else "ok",
                                 "%d 个游戏可更新" % n_up if n_up else "就绪")
        if n_up:
            self.win._toast("🔄 %d 个游戏有新版本（封面左上角标了「可更新」，"
                            "可筛选「🔄 可更新」查看）" % n_up)
        else:
            self.win._toast("版本检查：%s" % msg)

    def pause_update_check(self, on):
        """用户发起正式任务时让出网络与写库权，任务结束再继续。"""
        if self._checker and self._checker.isRunning():
            self._checker.pause(on)

    def stop_update_check(self):
        """关窗口时收尾：让检查线程尽快退出（半路停下的结果下次接着查）。"""
        if self._checker and self._checker.isRunning():
            self._checker.stop()
            self._checker.wait(3000)

    # ---------- 卡片菜单 ----------
    def _card_menu(self, entry):
        path = entry.get("path", "")
        up = entry.get("update") or {}
        m = QMenu(self)
        a_launch = m.addAction("▶  启动游戏")
        a_cover = m.addAction("🖼  更换封面")
        a_webcover = m.addAction("🌐  网络找封面")
        a_rating = m.addAction("🔄  刷新评分" if entry.get("ratings") else "⭐  获取评分")
        a_check = m.addAction("🔍  检查更新")
        a_page = m.addAction("⬆  打开更新页") if up.get("url") else None
        a_open = m.addAction("📂  打开游戏目录")
        a_setcur = m.addAction("🎯  设为当前项目")
        m.addSeparator()
        a_del = m.addAction("🗑  从库中移除")
        chosen = m.exec(QCursor.pos())
        if chosen is a_launch:
            self._launch(entry)
        elif chosen is a_cover:
            self._change_cover(entry)
        elif chosen is a_webcover:
            self._pending_cover = entry
            self.win._run(self._op_fetch_cover)
        elif chosen is a_rating:
            self._pending_rating = entry
            self.win._run(self._op_fetch_rating)
        elif chosen is a_check:
            self._start_check(paths=[path], force=True)
        elif a_page is not None and chosen is a_page:
            QDesktopServices.openUrl(QUrl(up.get("url")))
        elif chosen is a_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif chosen is a_setcur:
            if self.win._set_game(path):
                self.win._goto("flow")
        elif chosen is a_del:
            ret = QMessageBox.question(self, "从库中移除",
                                       "只从游戏库移除记录，不动游戏文件：\n%s" % path)
            if ret == QMessageBox.StandardButton.Yes:
                lib = Library()
                lib.remove(path)
                lib.save()
                self.reload()

    def _launch(self, entry):
        path = entry.get("path", "")
        if not os.path.isdir(path):
            QMessageBox.warning(self, "目录不存在", "游戏目录已不存在：\n%s" % path)
            return
        exe, total = gamelib.find_game_exe(path)
        if not exe:
            QMessageBox.warning(self, "错误", "游戏根目录下没有找到可执行文件（.exe）")
            return
        if total > 1:
            self.win._toast("发现多个 exe，已启动主程序：%s" % os.path.basename(exe))
        try:
            subprocess.Popen([exe], cwd=path, close_fds=True)
        except OSError as e:
            QMessageBox.warning(self, "启动失败", str(e))

    def _change_cover(self, entry):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.ico)")
        if not f:
            return
        lib = Library()
        lib.set_cover(entry["path"], f)
        lib.save()
        self.reload()
        self.win._toast("封面已更新：%s" % (entry.get("name") or ""))

    # ---------- 后台任务（Worker 兼容签名） ----------
    def _fetch_missing_covers(self, lib, log, prog, stop):
        """给缺封面的游戏联网补图（VNDB 同名搜索），返回补齐数量。"""
        missing = lib.games_missing_cover()
        if not missing:
            log("🖼 所有游戏都有封面")
            return 0
        log("🖼 %d 个游戏缺封面，开始联网查找（VNDB，可暂停）…" % len(missing))
        got = 0
        for i, g in enumerate(missing):
            if stop():
                log("⏸ 已暂停：剩余封面下次扫描/刷新时继续")
                break
            prog(i, len(missing))
            name = g.get("name") or os.path.basename(g.get("path", "")) or ""
            try:
                path, title = coverfetch.fetch_cover(name, log=log)
            except requests.RequestException as e:
                log("⚠ 网络不可用，跳过剩余封面查找（%s）" % e)
                break
            if path:
                g["cover"] = path
                got += 1
                log("✔ %s ← %s" % (name, title))
        prog(len(missing), len(missing))
        log("🖼 封面补齐 %d / %d（其余未配到同名游戏，可点卡片手动换封面）" % (got, len(missing)))
        return got

    def _op_scan(self, log, prog, stop, confirm=None):
        roots = list(self._pending_roots)
        lib = Library()
        log("🔍 开始扫描：%s" % "；".join(roots))
        found = gamelib.scan_paths(roots, log=log, should_stop=stop)
        if stop():
            log("⏸ 扫描已停止，已发现的游戏仍会入库")
        n_new = 0
        for f in found:
            if lib.find(f) is None:
                n_new += 1
            lib.add(f, "scan", self.win.cfg.get("language", "chinese"))
        lib.add_scan_roots(roots)
        lib.refresh(self.win.cfg.get("language", "chinese"))
        n_cover = self._fetch_missing_covers(lib, log, prog, stop)
        lib.save()
        msg = "扫描完成：发现 %d 个游戏，新入库 %d 个，封面补齐 %d 个" % (len(found), n_new, n_cover)
        log(msg)
        return msg

    def _op_rescan(self, log, prog, stop, confirm=None):
        lib = Library()
        lang = self.win.cfg.get("language", "chinese")
        log("⟳ 刷新库中 %d 个游戏的汉化状态…" % len(lib.games))
        changed = lib.refresh(lang)
        log("状态变化 %d 项" % changed)
        n_new = 0
        found = []
        roots = [r for r in lib.scan_roots if os.path.isdir(r)]
        if stop():
            roots = []
        if roots:
            log("重新扫描目录：%s" % "；".join(roots))
            found = gamelib.scan_paths(roots, log=log, should_stop=stop)
            for f in found:
                if lib.find(f) is None:
                    n_new += 1
                lib.add(f, "scan", lang)
        n_cover = self._fetch_missing_covers(lib, log, prog, stop)
        lib.save()
        msg = "刷新完成：状态变化 %d 项，新入库 %d 个，封面补齐 %d 个" % (changed, n_new, n_cover)
        log(msg)
        return msg

    def _op_ratings(self, log, prog, stop, confirm=None):
        """给库里还没有评分、或评分该重抓的游戏联网查一遍（见 ratings.is_stale）。"""
        lib = Library()
        todo = [g for g in lib.games
                if ratings.is_stale(g.get("ratings")) and os.path.isdir(g.get("path", ""))]
        if not todo:
            retry = "库里 %d 个游戏都有评分了。\n\n要重新抓取全部吗？（每个游戏 2 次联网请求，比较慢）" \
                    % len(lib.games)
            if not (confirm and confirm(retry) == "use"):
                log("⭐ 评分都是最新的，跳过（点卡片 → 「🔄 刷新评分」可单独重抓）")
                return "评分已是最新"
            todo = [g for g in lib.games if os.path.isdir(g.get("path", ""))]
        log("⭐ 查询 %d 个游戏的评分（F95Zone 优先，未收录换 dikgames，可暂停）…" % len(todo))
        got = 0
        for i, g in enumerate(todo):
            if stop():
                log("⏸ 已暂停：剩余 %d 个下次再查" % (len(todo) - i))
                break
            prog(i, len(todo))
            name = g.get("name") or os.path.basename(g.get("path", "")) or ""
            try:
                found = ratings.fetch_rating(name, log=log)
            except requests.RequestException as e:
                log("⚠ 网络不可用，跳过剩余评分查询（%s）" % e)
                break
            if found:
                g["ratings"] = found
                got += 1
                log("⭐ %s ← %s" % (name, ratings.summary(found)))
            else:
                log("✖ %s：两个站都没找到同名游戏，可稍后再试" % name)
            lib.save()
        prog(len(todo), len(todo))
        msg = "评分完成：%d / %d 个游戏拿到评分" % (got, len(todo))
        log(msg)
        return msg

    def _op_fetch_rating(self, log, prog, stop, confirm=None):
        entry = self._pending_rating
        name = entry.get("name") or os.path.basename(entry.get("path", "")) or ""
        log("⭐ 正在联网查询评分：%s" % name)
        prog(0, 1)
        try:
            found = ratings.fetch_rating(name, log=log)
        except requests.RequestException as e:
            log("⚠ 网络不可用：%s" % e)
            prog(1, 1)
            return "网络不可用，稍后再试：%s" % name
        prog(1, 1)
        lib = Library()
        g = lib.find(entry.get("path", "")) or entry
        if not found:
            log("未找到与「%s」同名的游戏（F95Zone / dikgames 都查过了）" % name)
            return "两个站都没收录：%s" % name
        g["ratings"] = found
        lib.save()
        log("✔ %s" % ratings.summary(found))
        return "评分已更新：%s ← %s" % (name, ratings.headline(found))

    def _op_fetch_cover(self, log, prog, stop, confirm=None):
        entry = self._pending_cover
        name = entry.get("name") or os.path.basename(entry.get("path", "")) or ""
        log("🌐 正在联网查找封面：%s" % name)
        prog(0, 1)
        try:
            path, title = coverfetch.fetch_cover(name, log=log)
        except requests.RequestException as e:
            log("⚠ 网络不可用：%s" % e)
            prog(1, 1)
            return "网络不可用，稍后再试：%s" % name
        prog(1, 1)
        if not path:
            log("未找到与「%s」同名且带封面的游戏，可继续用「更换封面」手动指定" % name)
            return "未找到同名游戏的封面：%s" % name
        lib = Library()
        g = lib.find(entry.get("path", ""))
        if g is None:
            g = entry
        g["cover"] = path
        lib.save()
        log("✔ 封面来源：%s" % title)
        return "封面已更新：%s ← %s" % (name, title)
