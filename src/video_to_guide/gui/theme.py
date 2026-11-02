"""界面视觉规范：颜色、字号、间距和全局样式表。

集中放在这里的目的：
1. 改一处就能让整个窗口保持一致，不用在 20 个控件上各写一遍 setStyleSheet；
2. 组件代码里只出现「这是标题」「这是卡片」这种语义，不出现 #0969da 这种字面值；
3. 换配色时只动这一个文件，业务逻辑一行不用碰。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 颜色
BG = "#f6f8fa"          # 工作区底色（浅灰，衬托白色面板）
PANEL = "#ffffff"       # 内容面板
BORDER = "#d0d7de"      # 面板与控件边框
BORDER_SOFT = "#e6eaef"
TEXT = "#1f2328"        # 正文
TEXT_DIM = "#57606a"    # 次要说明
TEXT_FAINT = "#8b949e"  # 更弱的提示
ACCENT = "#0969da"      # 主色（按钮、选中、链接）
ACCENT_HOVER = "#0860c4"
ACCENT_SOFT = "#ddf4ff" # 选中项浅底
OK = "#1a7f37"
WARN = "#9a6700"
WARN_BG = "#fff8c5"
FAIL = "#cf222e"

FONT_UI = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
FONT_MONO = '"Consolas", "Microsoft YaHei UI", monospace'

RADIUS = 8


def app_qss() -> str:
    """返回整个应用的样式表，在 QApplication 上设置一次即可。"""
    return f"""
QWidget {{
    font-family: {FONT_UI};
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog {{
    background: {BG};
}}

/* ---- 工具栏：白底 + 一条细分隔线，不要花哨 ---- */
QToolBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px 8px;
    spacing: 4px;
}}
QToolBar::separator {{
    background: {BORDER};
    width: 1px;
    margin: 6px 6px;
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 9px;
    color: {TEXT};
}}
QToolButton:hover {{
    background: {ACCENT_SOFT};
    border-color: {BORDER_SOFT};
}}
QToolButton:pressed {{
    background: #c8e8ff;
}}
QToolButton:disabled {{
    color: {TEXT_FAINT};
}}

QPushButton {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT};
}}
QPushButton:disabled {{
    color: {TEXT_FAINT};
    background: #f6f8fa;
}}
QPushButton#primary {{
    background: {ACCENT};
    color: #ffffff;
    border: none;
}}
QPushButton#primary:hover {{
    background: {ACCENT_HOVER};
}}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextBrowser, QTextEdit {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}

/* ---- 列表：选中态要一眼看出来 ---- */
QListWidget {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 6px 8px;
    border-radius: 6px;
    color: {TEXT};
}}
QListWidget::item:hover {{
    background: #f3f6f9;
}}
QListWidget::item:selected {{
    background: {ACCENT_SOFT};
    color: {TEXT};
    border: 1px solid {ACCENT};
}}

/* ---- 标签页 ---- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    background: {PANEL};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 6px 14px;
    color: {TEXT_DIM};
}}
QTabBar::tab:hover {{
    color: {TEXT};
}}
QTabBar::tab:selected {{
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
}}

/* ---- 分组框 ---- */
QGroupBox {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {TEXT};
    font-weight: 600;
}}

QCheckBox, QRadioButton {{
    spacing: 6px;
}}

/* ---- 滚动条：细一点，别抢视觉 ---- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #c6cbd1;
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #a8b0b8;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: #c6cbd1;
    border-radius: 4px;
    min-width: 28px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QSplitter::handle {{
    background: transparent;
}}
QSplitter::handle:hover {{
    background: {ACCENT_SOFT};
}}

QStatusBar {{
    background: {PANEL};
    border-top: 1px solid {BORDER};
}}
QStatusBar::item {{ border: none; }}

QMenuBar {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER_SOFT};
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
}}
QMenuBar::item:selected {{
    background: {ACCENT_SOFT};
    border-radius: 4px;
}}
QMenu {{
    background: {PANEL};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 24px 5px 20px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {ACCENT_SOFT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER_SOFT};
    margin: 4px 8px;
}}
"""


def card_qss() -> str:
    """信息卡等圆角白面板。"""
    return (f"background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:{RADIUS}px;")


def detail_html_css() -> str:
    """步骤详情用的内嵌 CSS（QTextBrowser 不认 QSS，只能写在 HTML 里）。"""
    return (
        "body{font-family:'Microsoft YaHei UI';font-size:14px;line-height:1.75;color:" + TEXT + ";"
        "margin:2px 4px}"
        "h2{font-size:18px;margin:0 0 6px;color:" + TEXT + "}"
        ".time{color:" + TEXT_DIM + ";font-size:13px;margin:0 0 12px}"
        ".warn{background:" + WARN_BG + ";border:1px solid #d4a72c;padding:8px 12px;"
        "border-radius:6px;color:#6b4e00;margin-bottom:12px}"
        ".field{margin:0 0 10px}"
        ".label{color:" + TEXT_DIM + ";font-size:13px}"
        ".ev{color:" + TEXT_DIM + ";font-size:13px;line-height:1.7}"
        "a{color:" + ACCENT + ";text-decoration:none}"
    )