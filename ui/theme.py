# -*- coding: utf-8 -*-
"""深色 / 浅色双主题。

深色模式参考现代桌面工具的流行设计：近黑蓝底、悬浮圆角侧栏、
半透明卡片 + 细发丝描边 + 柔和强调色光晕；浅色模式保留磨砂玻璃质感，
两者共用同一张 QSS 模板，仅替换色表。

Python 侧需要取色的地方（底板光斑、卡片阴影、富文本颜色、状态灯等）
统一走 token() / 辅助函数，保证自绘部分与样式表同步切换。
"""

from PySide6.QtGui import QColor

# ---------------- 色表 ----------------

DARK = {
    "BG": "#0b0e14",                 # 近黑蓝底
    "DIALOG": "#141924",
    "SIDE_BG": "#111621",            # 悬浮侧栏面板
    "SIDE_BORDER": "rgba(255,255,255,0.07)",
    "HAIR": "rgba(255,255,255,0.08)",
    "CARD": "rgba(255,255,255,0.035)",
    "CARD_BORDER": "rgba(255,255,255,0.075)",
    "TEXT": "#e7eaf3",
    "TEXT_DIM": "#8d96ab",
    "ACCENT": "#9182ff",
    "ACCENT_HOVER": "#a497ff",
    "ACCENT_SOFT": "rgba(145,130,255,0.14)",
    "GRAD1": "#7c5cff",
    "GRAD2": "#9a6bff",
    "GRAD1_H": "#8f74ff",
    "GRAD2_H": "#a97dff",
    "PRIMARY_DISABLED": "rgba(145,130,255,0.32)",
    "DANGER": "#ff7b81",
    "STATE_OK": "#3ddc8f",
    "STATE_BUSY": "#ffc857",
    "STATE_ERR": "#ff6b6e",
    "CHIP_BG": "rgba(255,255,255,0.05)",
    "CHIP_BORDER": "rgba(255,255,255,0.10)",
    "CHIP_TEXT": "#9aa3b8",
    "GCARD": "rgba(255,255,255,0.04)",
    "GCARD_BORDER": "rgba(255,255,255,0.09)",
    "GCARD_HOVER": "rgba(255,255,255,0.07)",
    "NAV_TEXT": "#9aa3b8",
    "NAV_HOVER": "rgba(255,255,255,0.06)",
    "NAV_CHECKED_BORDER": "rgba(145,130,255,0.38)",
    "BTN_BG": "rgba(255,255,255,0.055)",
    "BTN_BORDER": "rgba(255,255,255,0.12)",
    "BTN_TEXT": "#e7eaf3",
    "BTN_HOVER_BG": "rgba(255,255,255,0.09)",
    "BTN_DISABLED_TEXT": "rgba(231,234,243,0.32)",
    "BTN_DISABLED_BG": "rgba(255,255,255,0.03)",
    "BTN_DISABLED_BORDER": "rgba(255,255,255,0.07)",
    "INPUT_BG": "rgba(255,255,255,0.055)",
    "INPUT_BORDER": "rgba(255,255,255,0.12)",
    "INPUT_FOCUS_BG": "rgba(255,255,255,0.085)",
    "EDIT_BG": "#1a2030",            # 表格内联编辑器：必须不透明，见 QSS 里的说明
    "INPUT_DISABLED_TEXT": "rgba(231,234,243,0.32)",
    "INPUT_DISABLED_BG": "rgba(255,255,255,0.03)",
    "INPUT_DISABLED_BORDER": "rgba(255,255,255,0.06)",
    "SELECT_BG": "#7c5cff",
    "POPUP_BG": "#1a2030",
    "POPUP_BORDER": "rgba(255,255,255,0.12)",
    "TABLE_BG": "rgba(255,255,255,0.025)",
    "TABLE_ALT": "rgba(255,255,255,0.02)",
    "TABLE_BORDER": "rgba(255,255,255,0.08)",
    "TABLE_TEXT": "#dfe3ee",
    "TABLE_GRID": "rgba(255,255,255,0.06)",
    "HEADER_BG": "rgba(255,255,255,0.04)",
    "HEADER_TEXT": "#9aa3b8",
    "HEADER_BORDER": "rgba(255,255,255,0.09)",
    "PROG_BG": "rgba(255,255,255,0.06)",
    "PROG_TEXT": "#e7eaf3",
    "CONSOLE_BG": "#0d1119",
    "CONSOLE_BORDER": "rgba(255,255,255,0.08)",
    "CONSOLE_TEXT": "#c9d2e8",
    "CHECK_BORDER": "rgba(255,255,255,0.28)",
    "CHECK_BG": "rgba(255,255,255,0.06)",
    "STAR_ON": "#ffc53d",
    "STAR_OFF": "#39415a",
    "SCROLL_HANDLE": "rgba(255,255,255,0.16)",
    "MENU_BG": "#1a2030",
    "MENU_BORDER": "rgba(255,255,255,0.12)",
    "MENU_TEXT": "#e7eaf3",
    "TOOLTIP_BG": "rgba(28,34,48,0.97)",
    "TOOLTIP_BORDER": "rgba(255,255,255,0.14)",
    "TOOLTIP_TEXT": "#e8eaf2",
    "STATUSBAR_TEXT": "#8d96ab",
    "STATUS_BG": "rgba(255,255,255,0.045)",
    "STATUS_BORDER": "rgba(255,255,255,0.08)",
    "ICONBTN_BG": "rgba(255,255,255,0.055)",
    "ICONBTN_BORDER": "rgba(255,255,255,0.12)",
    "ICONBTN_HOVER_BG": "rgba(255,255,255,0.09)",
}

LIGHT = {
    "BG": "#eef1f9",
    "DIALOG": "#f3f5fb",
    "SIDE_BG": "rgba(255,255,255,0.55)",
    "SIDE_BORDER": "rgba(255,255,255,0.75)",
    "HAIR": "rgba(110,120,150,0.22)",
    "CARD": "rgba(255,255,255,0.66)",
    "CARD_BORDER": "rgba(255,255,255,0.85)",
    "TEXT": "#22263c",
    "TEXT_DIM": "#68718a",
    "ACCENT": "#7c5cff",
    "ACCENT_HOVER": "#8f74ff",
    "ACCENT_SOFT": "rgba(124,92,255,0.11)",
    "GRAD1": "#7c5cff",
    "GRAD2": "#9a6bff",
    "GRAD1_H": "#8f74ff",
    "GRAD2_H": "#a97dff",
    "PRIMARY_DISABLED": "#8f86c6",
    "DANGER": "#e5484d",
    "STATE_OK": "#149a54",
    "STATE_BUSY": "#c77f00",
    "STATE_ERR": "#d33c41",
    "CHIP_BG": "rgba(255,255,255,0.6)",
    "CHIP_BORDER": "rgba(255,255,255,0.9)",
    "CHIP_TEXT": "#59627a",
    "GCARD": "rgba(255,255,255,0.8)",
    "GCARD_BORDER": "rgba(255,255,255,0.95)",
    "GCARD_HOVER": "rgba(255,255,255,0.96)",
    "NAV_TEXT": "#59627a",
    "NAV_HOVER": "rgba(124,92,255,0.10)",
    "NAV_CHECKED_BORDER": "rgba(124,92,255,0.35)",
    "BTN_BG": "rgba(255,255,255,0.78)",
    "BTN_BORDER": "rgba(96,106,138,0.32)",
    "BTN_TEXT": "#1d2136",
    "BTN_HOVER_BG": "#ffffff",
    "BTN_DISABLED_TEXT": "#6f7890",
    "BTN_DISABLED_BG": "rgba(255,255,255,0.62)",
    "BTN_DISABLED_BORDER": "rgba(96,106,138,0.26)",
    "INPUT_BG": "rgba(255,255,255,0.78)",
    "INPUT_BORDER": "rgba(110,120,150,0.22)",
    "INPUT_FOCUS_BG": "#ffffff",
    "EDIT_BG": "#ffffff",            # 表格内联编辑器：必须不透明，见 QSS 里的说明
    "INPUT_DISABLED_TEXT": "#6f7890",
    "INPUT_DISABLED_BG": "rgba(255,255,255,0.5)",
    "INPUT_DISABLED_BORDER": "rgba(96,106,138,0.2)",
    "SELECT_BG": "#7c5cff",
    "POPUP_BG": "rgba(252,253,255,0.99)",
    "POPUP_BORDER": "rgba(110,120,150,0.22)",
    "TABLE_BG": "rgba(255,255,255,0.6)",
    "TABLE_ALT": "rgba(124,92,255,0.028)",
    "TABLE_BORDER": "rgba(255,255,255,0.85)",
    "TABLE_TEXT": "#1d2136",
    "TABLE_GRID": "rgba(96,106,138,0.18)",
    "HEADER_BG": "rgba(255,255,255,0.55)",
    "HEADER_TEXT": "#59627a",
    "HEADER_BORDER": "rgba(96,106,138,0.2)",
    "PROG_BG": "rgba(255,255,255,0.7)",
    "PROG_TEXT": "#22263c",
    "CONSOLE_BG": "rgba(255,255,255,0.88)",
    "CONSOLE_BORDER": "rgba(110,120,150,0.25)",
    "CONSOLE_TEXT": "#333a55",
    "CHECK_BORDER": "rgba(96,106,138,0.4)",
    "CHECK_BG": "rgba(255,255,255,0.8)",
    "STAR_ON": "#f0a500",
    "STAR_OFF": "#c6cbd9",
    "SCROLL_HANDLE": "rgba(110,120,150,0.30)",
    "MENU_BG": "rgba(252,253,255,0.99)",
    "MENU_BORDER": "rgba(110,120,150,0.22)",
    "MENU_TEXT": "#22263c",
    "TOOLTIP_BG": "rgba(30,33,48,0.95)",
    "TOOLTIP_BORDER": "rgba(255,255,255,0.16)",
    "TOOLTIP_TEXT": "#e8eaf2",
    "STATUSBAR_TEXT": "#68718a",
    "STATUS_BG": "rgba(255,255,255,0.5)",
    "STATUS_BORDER": "rgba(255,255,255,0.8)",
    "ICONBTN_BG": "rgba(255,255,255,0.7)",
    "ICONBTN_BORDER": "rgba(96,106,138,0.28)",
    "ICONBTN_HOVER_BG": "#ffffff",
}

STATE = {"dark": True}

# ---------------- QSS 模板 ----------------

QSS_TEMPLATE = """
* { font-family: 'Microsoft YaHei UI','Segoe UI',sans-serif; outline: none; }
QMainWindow, QWidget#root { background: %(BG)s; }
QDialog { background: %(DIALOG)s; }
QLabel { color: %(TEXT)s; background: transparent; }
QLabel#dim { color: %(TEXT_DIM)s; }
QLabel#h1 { font-size: 19px; font-weight: 700; }
QLabel#h2 { font-size: 14px; font-weight: 600; }
QLabel#accent { color: %(ACCENT)s; font-weight: 700; }
QLabel#sec { font-size: 14px; font-weight: 700; color: %(TEXT)s; padding: 4px 2px; }
QLabel#empty { color: %(TEXT_DIM)s; font-size: 13px; padding: 40px; }
QLabel#brand { font-size: 15px; font-weight: 800; color: %(TEXT)s; }
QLabel#brandsub { font-size: 11px; color: %(TEXT_DIM)s; }
QLabel#pagetitle { font-size: 17px; font-weight: 700; color: %(TEXT)s; }

/* 卡片：半透明面板 + 细描边 */
QWidget#card {
    background: %(CARD)s; border: 1px solid %(CARD_BORDER)s; border-radius: 16px;
}

/* 悬浮圆角侧栏 / 顶部条 / 侧栏底部状态卡 */
QWidget#side {
    background: %(SIDE_BG)s; border: 1px solid %(SIDE_BORDER)s; border-radius: 16px;
}
QWidget#topbar { background: transparent; border: none; }
QWidget#sidestatus {
    background: %(STATUS_BG)s; border: 1px solid %(STATUS_BORDER)s; border-radius: 12px;
}
QLabel#chip {
    background: %(CHIP_BG)s; border: 1px solid %(CHIP_BORDER)s;
    border-radius: 13px; padding: 5px 14px; color: %(CHIP_TEXT)s; font-size: 12px;
}

/* 游戏库卡片 */
QFrame#gcard {
    background: %(GCARD)s; border: 1px solid %(GCARD_BORDER)s; border-radius: 14px;
}
QFrame#gcard:hover { border: 1px solid %(ACCENT)s; background: %(GCARD_HOVER)s; }
QLabel#gname { font-size: 12px; font-weight: 600; color: %(TEXT)s; }
QLabel#grate { font-size: 11px; color: %(TEXT_DIM)s; }
QLabel#badge_ok { background: rgba(46,196,120,0.94); color: white; border-radius: 9px;
    padding: 2px 10px; font-size: 11px; font-weight: 600; }
QLabel#badge_no { background: rgba(245,158,66,0.94); color: white; border-radius: 9px;
    padding: 2px 10px; font-size: 11px; font-weight: 600; }
QLabel#badge_missing { background: rgba(122,130,152,0.94); color: white; border-radius: 9px;
    padding: 2px 10px; font-size: 11px; font-weight: 600; }
/* 有新版本：紫色（与绿色的「已翻译」、橙色的「未翻译」区分开） */
QLabel#badge_update { background: rgba(124,92,255,0.95); color: white; border-radius: 9px;
    padding: 2px 10px; font-size: 11px; font-weight: 700; }

/* 侧边导航：胶囊选中态（描边 + 染色底），分组间以细分隔线区隔 */
QPushButton#nav {
    background: transparent; color: %(NAV_TEXT)s; border: 1px solid transparent;
    border-radius: 12px; padding: 11px 14px; font-size: 14px; text-align: left;
}
QPushButton#nav:hover { background: %(NAV_HOVER)s; color: %(TEXT)s; }
QPushButton#nav:checked {
    background: %(ACCENT_SOFT)s; border: 1px solid %(NAV_CHECKED_BORDER)s;
    color: %(ACCENT)s; font-weight: 700;
}
QFrame#navdiv { background: %(HAIR)s; border: none; }

/* 顶部圆形图标按钮（主题切换） */
QPushButton#iconbtn {
    background: %(ICONBTN_BG)s; color: %(TEXT)s; border: 1px solid %(ICONBTN_BORDER)s;
    border-radius: 19px; padding: 0; font-size: 15px;
}
QPushButton#iconbtn:hover { border-color: %(ACCENT)s; background: %(ICONBTN_HOVER_BG)s; }

QPushButton {
    background: %(BTN_BG)s; color: %(BTN_TEXT)s; border: 1px solid %(BTN_BORDER)s;
    border-radius: 10px; padding: 8px 16px; font-size: 13px;
}
QPushButton:hover { border-color: %(ACCENT)s; background: %(BTN_HOVER_BG)s; }
QPushButton:pressed { background: %(ACCENT_SOFT)s; }
QPushButton:disabled {
    color: %(BTN_DISABLED_TEXT)s; background: %(BTN_DISABLED_BG)s;
    border-color: %(BTN_DISABLED_BORDER)s;
}
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 %(GRAD1)s, stop:1 %(GRAD2)s);
    border: none; color: white; font-weight: 600; padding: 10px 22px;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 %(GRAD1_H)s, stop:1 %(GRAD2_H)s);
}
QPushButton#primary:pressed { background: %(GRAD1)s; }
QPushButton#primary:disabled { background: %(PRIMARY_DISABLED)s; color: rgba(255,255,255,0.96); }
QPushButton#danger { color: %(DANGER)s; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: %(INPUT_BG)s; color: %(TEXT)s; border: 1px solid %(INPUT_BORDER)s;
    border-radius: 10px; padding: 7px 10px; font-size: 13px; selection-background-color: %(SELECT_BG)s;
}
QLineEdit:focus, QComboBox:focus { border-color: %(ACCENT)s; background: %(INPUT_FOCUS_BG)s; }

/* 表格内联编辑器：它是盖在单元格上的子控件，背景必须完全不透明——
   沿用半透明的 INPUT_BG 会让单元格原文和选中底色透上来，和编辑内容叠成一团。
   同时去掉大圆角与厚内边距：单元格本就只有一行高，撑不下它们。 */
QTableWidget QLineEdit, QTableView QLineEdit,
QTableWidget QTextEdit, QTableView QTextEdit {
    background: %(EDIT_BG)s; color: %(TEXT)s; border: 1px solid %(ACCENT)s;
    border-radius: 0; padding: 0 4px; margin: 0; font-size: 13px;
    selection-background-color: %(SELECT_BG)s; selection-color: white;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: %(INPUT_DISABLED_TEXT)s; background: %(INPUT_DISABLED_BG)s;
    border-color: %(INPUT_DISABLED_BORDER)s;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow {
    image: none; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid %(TEXT_DIM)s; margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: %(POPUP_BG)s; color: %(TEXT)s; border: 1px solid %(POPUP_BORDER)s;
    border-radius: 10px; padding: 4px; selection-background-color: %(ACCENT)s; selection-color: white;
}

QTableWidget {
    background: %(TABLE_BG)s; color: %(TABLE_TEXT)s; border: 1px solid %(TABLE_BORDER)s;
    border-radius: 12px; gridline-color: %(TABLE_GRID)s; font-size: 13px;
    alternate-background-color: %(TABLE_ALT)s;
}
QTableWidget::item { padding: 4px 8px; }
QTableWidget::item:selected { background: %(ACCENT)s; color: white; }
QHeaderView::section {
    background: %(HEADER_BG)s; color: %(HEADER_TEXT)s; border: none;
    border-bottom: 1px solid %(HEADER_BORDER)s;
    padding: 7px 8px; font-size: 12px; font-weight: 600;
}

QProgressBar {
    background: %(PROG_BG)s; border: none;
    border-radius: 8px; height: 14px; text-align: center; color: %(PROG_TEXT)s; font-size: 11px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %(GRAD1)s, stop:1 %(GRAD2)s);
    border-radius: 7px;
}

/* 日志 / 提示词控制台：两种模式下都用深色玻璃 */
QPlainTextEdit, QTextEdit {
    background: %(CONSOLE_BG)s; color: %(CONSOLE_TEXT)s; border: 1px solid %(CONSOLE_BORDER)s;
    border-radius: 12px; padding: 8px;
    font-family: 'Cascadia Mono','Consolas',monospace; font-size: 12px;
    selection-background-color: %(ACCENT)s;
}

QCheckBox { color: %(TEXT)s; spacing: 8px; background: transparent; }
QCheckBox:disabled { color: %(TEXT_DIM)s; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 5px;
    border: 1px solid %(CHECK_BORDER)s; background: %(CHECK_BG)s; }
QCheckBox::indicator:checked { background: %(ACCENT)s; border-color: %(ACCENT)s; image: url(none); }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: %(SCROLL_HANDLE)s; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: %(ACCENT)s; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: %(SCROLL_HANDLE)s; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QStatusBar { background: transparent; color: %(STATUSBAR_TEXT)s; }
QToolTip {
    background: %(TOOLTIP_BG)s; color: %(TOOLTIP_TEXT)s;
    border: 1px solid %(TOOLTIP_BORDER)s; border-radius: 8px; padding: 6px;
}

QMenu {
    background: %(MENU_BG)s; border: 1px solid %(MENU_BORDER)s; border-radius: 10px; padding: 6px;
}
QMenu::item { color: %(MENU_TEXT)s; padding: 7px 24px; border-radius: 7px; font-size: 13px; }
QMenu::item:selected { background: %(ACCENT)s; color: white; }
QMenu::separator { height: 1px; background: %(HAIR)s; margin: 5px 8px; }

QMessageBox { background: %(DIALOG)s; }
QMessageBox QLabel { background: transparent; color: %(TEXT)s; font-size: 13px; }
QMessageBox QPushButton { min-width: 80px; }
"""

# ---------------- 自绘辅助（底板光斑 / 阴影） ----------------

_BLOBS_DARK = (
    (0.10, 0.06, 0.55, QColor(124, 92, 255, 52)),
    (0.92, 0.12, 0.50, QColor(0, 186, 170, 34)),
    (0.90, 0.95, 0.55, QColor(255, 110, 150, 26)),
    (0.18, 0.98, 0.55, QColor(93, 157, 255, 30)),
)
_BLOBS_LIGHT = (
    (0.10, 0.04, 0.55, QColor(124, 92, 255, 40)),
    (0.92, 0.10, 0.50, QColor(0, 186, 170, 32)),
    (0.88, 0.94, 0.60, QColor(255, 110, 150, 28)),
    (0.22, 0.96, 0.55, QColor(93, 157, 255, 28)),
)


def set_dark(dark):
    STATE["dark"] = bool(dark)


def is_dark():
    return STATE["dark"]


def tokens():
    return DARK if STATE["dark"] else LIGHT


def token(name):
    return tokens()[name]


def build_qss():
    return QSS_TEMPLATE % tokens()


def root_bg():
    """GlassRoot 底色。"""
    return QColor(token("BG"))


def root_blobs():
    """GlassRoot 柔光色斑：(cx, cy, 半径系数, QColor)。"""
    return _BLOBS_DARK if STATE["dark"] else _BLOBS_LIGHT


def _colorref(hexstr):
    """#RRGGBB → Windows COLORREF (0x00BBGGRR)。"""
    h = hexstr.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def titlebar_color():
    """系统标题栏底色（跟随窗口底色）。"""
    return _colorref(token("BG"))


def titlebar_text_color():
    """系统标题栏文字颜色。"""
    return _colorref(token("TEXT"))
