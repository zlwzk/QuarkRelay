"""设计系统：配色、字号与全局样式表。

两种主题共用一套 token，换主题只换 token，不在控件里写死颜色。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtCore import Qt


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    surface: str
    surface_alt: str
    elevated: str
    border: str
    border_soft: str
    text: str
    text_dim: str
    text_faint: str
    primary: str
    primary_hover: str
    primary_soft: str
    accent: str
    success: str
    warning: str
    danger: str
    shadow: str


DARK = Palette(
    name="dark",
    bg="#080D1A",
    surface="#0F1626",
    surface_alt="#141D31",
    elevated="#1A2439",
    border="#24304A",
    border_soft="#1B2439",
    text="#E9EFFA",
    text_dim="#93A3C2",
    text_faint="#63739A",
    primary="#3B82F6",
    primary_hover="#5B9BFF",
    primary_soft="#1D2B47",
    accent="#22D3EE",
    success="#34D399",
    warning="#FBBF24",
    danger="#F87171",
    shadow="rgba(0,0,0,0.45)",
)

LIGHT = Palette(
    name="light",
    bg="#F3F6FC",
    surface="#FFFFFF",
    surface_alt="#F7F9FE",
    elevated="#FFFFFF",
    border="#DCE4F2",
    border_soft="#E8EEF9",
    text="#101828",
    text_dim="#5A6B87",
    text_faint="#8A99B5",
    primary="#2563EB",
    primary_hover="#1D4ED8",
    primary_soft="#E4EDFF",
    accent="#0891B2",
    success="#059669",
    warning="#D97706",
    danger="#DC2626",
    shadow="rgba(15,23,42,0.10)",
)

PALETTES = {"dark": DARK, "light": LIGHT}

RADIUS = 12
FONT_FAMILY_CANDIDATES = ("Microsoft YaHei UI", "微软雅黑", "Segoe UI", "PingFang SC", "Arial")


def pick_font(size: int = 10, bold: bool = False) -> QFont:
    families = set(QFontDatabase.families())
    family = next((f for f in FONT_FAMILY_CANDIDATES if f in families), "Sans Serif")
    font = QFont(family, size)
    font.setBold(bold)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return font


def app_icon(size: int = 256) -> QIcon:
    """程序图标：深色圆角底 + 蓝色中转箭头。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setBrush(QColor("#0F1626"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, size - 4, size - 4, size * 0.24, size * 0.24)

    unit = size / 100.0
    painter.setBrush(QColor("#3B82F6"))
    painter.drawRoundedRect(22 * unit, 26 * unit, 56 * unit, 14 * unit, 7 * unit, 7 * unit)
    painter.setBrush(QColor("#22D3EE"))
    painter.drawRoundedRect(22 * unit, 60 * unit, 56 * unit, 14 * unit, 7 * unit, 7 * unit)
    painter.setBrush(QColor("#E9EFFA"))
    painter.drawRoundedRect(44 * unit, 42 * unit, 12 * unit, 16 * unit, 5 * unit, 5 * unit)
    painter.end()
    return QIcon(pixmap)


def build_qss(palette: Palette) -> str:
    p = palette
    return f"""
* {{
    font-family: "Microsoft YaHei UI", "微软雅黑", "Segoe UI", sans-serif;
    outline: none;
}}
QWidget {{
    color: {p.text};
    font-size: 13px;
}}
QMainWindow, QDialog, #Root {{
    background: {p.bg};
}}

/* ---------------------------------------------------------------- 侧边栏 */
#Sidebar {{
    background: {p.surface};
    border-right: 1px solid {p.border_soft};
}}
#BrandName {{
    font-size: 17px;
    font-weight: 700;
    color: {p.text};
    letter-spacing: 1px;
}}
#BrandSub {{
    font-size: 11px;
    color: {p.text_faint};
    letter-spacing: 2px;
}}
#NavButton {{
    background: transparent;
    border: none;
    border-radius: 10px;
    padding: 10px 14px;
    text-align: left;
    color: {p.text_dim};
    font-size: 13.5px;
}}
#NavButton:hover {{
    background: {p.surface_alt};
    color: {p.text};
}}
#NavButton:checked {{
    background: {p.primary_soft};
    color: {p.text};
    font-weight: 600;
}}
#NavButton:checked:hover {{
    background: {p.primary_soft};
}}
#NavBadge {{
    background: {p.primary};
    color: #FFFFFF;
    border-radius: 9px;
    padding: 1px 7px;
    font-size: 11px;
    font-weight: 700;
}}

/* ------------------------------------------------------------------ 卡片 */
#Card {{
    background: {p.surface};
    border: 1px solid {p.border_soft};
    border-radius: {RADIUS}px;
}}
#CardTight {{
    background: {p.surface_alt};
    border: 1px solid {p.border_soft};
    border-radius: 10px;
}}
#PageTitle {{
    font-size: 22px;
    font-weight: 700;
}}
#PageSubtitle {{
    font-size: 12.5px;
    color: {p.text_dim};
}}
#CardTitle {{
    font-size: 14.5px;
    font-weight: 600;
}}
#SectionTitle {{
    font-size: 12px;
    font-weight: 700;
    color: {p.text_faint};
    letter-spacing: 1px;
}}
#Muted {{
    color: {p.text_dim};
    font-size: 12px;
}}
#Faint {{
    color: {p.text_faint};
    font-size: 11.5px;
}}
#Percent {{
    color: {p.primary};
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 12.5px;
    font-weight: 700;
}}
#StepBadge {{
    background: {p.primary_soft};
    color: {p.primary_hover};
    border-radius: 9px;
    min-width: 18px;
    max-width: 18px;
    min-height: 18px;
    max-height: 18px;
    font-size: 11px;
    font-weight: 700;
}}
#LinkText {{
    color: {p.accent};
    font-size: 12.5px;
}}
#StatValue {{
    font-size: 19px;
    font-weight: 700;
}}
#StatLabel {{
    color: {p.text_faint};
    font-size: 11.5px;
}}

/* ------------------------------------------------------------------ 按钮 */
QPushButton {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 9px;
    padding: 8px 16px;
    color: {p.text};
}}
QPushButton:hover {{
    background: {p.elevated};
    border-color: {p.primary};
}}
QPushButton:pressed {{
    background: {p.primary_soft};
}}
QPushButton:disabled {{
    color: {p.text_faint};
    background: {p.surface_alt};
    border-color: {p.border_soft};
}}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {p.primary}, stop:1 {p.accent});
    border: none;
    color: #FFFFFF;
    font-weight: 600;
    padding: 9px 20px;
}}
QPushButton#Primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {p.primary_hover}, stop:1 {p.accent});
}}
QPushButton#Primary:disabled {{
    background: {p.primary_soft};
    color: {p.text_faint};
}}
QPushButton#Ghost {{
    background: transparent;
    border: 1px solid {p.border};
    color: {p.text_dim};
    padding: 6px 12px;
}}
QPushButton#Ghost:hover {{
    color: {p.text};
    border-color: {p.primary};
    background: {p.surface_alt};
}}
QPushButton#Danger {{
    background: transparent;
    border: 1px solid {p.danger};
    color: {p.danger};
    padding: 6px 12px;
}}
QPushButton#Danger:hover {{
    background: {p.danger};
    color: #FFFFFF;
}}
QPushButton#LinkBtn {{
    background: transparent;
    border: none;
    color: {p.primary_hover};
    padding: 2px 6px;
    text-decoration: underline;
}}
QPushButton#LinkBtn:hover {{
    color: {p.accent};
}}
QPushButton#IconBtn {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 5px 9px;
    color: {p.text_dim};
}}
QPushButton#IconBtn:hover {{
    background: {p.surface_alt};
    color: {p.text};
}}
QPushButton#Seg {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    color: {p.text_dim};
    padding: 8px 18px;
}}
QPushButton#Seg:hover {{
    background: {p.elevated};
    color: {p.text};
}}
QPushButton#Seg:checked {{
    background: {p.primary_soft};
    border-color: {p.primary};
    color: {p.text};
    font-weight: 600;
}}

/* ------------------------------------------------------------ 输入类控件 */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 9px;
    padding: 7px 10px;
    selection-background-color: {p.primary};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {p.primary};
    background: {p.elevated};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{
    color: {p.text_faint};
}}
QLineEdit[echoMode="2"] {{
    lineedit-password-character: 9679;
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_dim};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {p.elevated};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {p.primary_soft};
    selection-color: {p.text};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    width: 16px;
    border: none;
    background: transparent;
}}
QSpinBox::up-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {p.text_dim};
}}
QSpinBox::down-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_dim};
}}

/* -------------------------------------------------------------- 勾选/进度 */
QCheckBox, QRadioButton {{
    spacing: 8px;
    color: {p.text};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1.5px solid {p.border};
    border-radius: 5px;
    background: {p.surface_alt};
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.primary};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p.primary};
    border-color: {p.primary};
    image: none;
}}
QProgressBar {{
    background: {p.surface_alt};
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {p.primary}, stop:1 {p.accent});
}}

/* ------------------------------------------------------------------ 列表 */
QListWidget, QTableWidget, QTreeWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: 9px;
    padding: 8px 10px;
    margin: 2px 0px;
}}
QListWidget::item:selected {{
    background: {p.primary_soft};
    color: {p.text};
}}
QListWidget::item:hover {{
    background: {p.surface_alt};
}}
QHeaderView::section {{
    background: {p.surface_alt};
    color: {p.text_dim};
    border: none;
    border-bottom: 1px solid {p.border_soft};
    padding: 7px 8px;
    font-weight: 600;
}}
QTableWidget::item {{
    padding: 6px 8px;
    border-bottom: 1px solid {p.border_soft};
}}
QTableWidget::item:selected {{
    background: {p.primary_soft};
    color: {p.text};
}}

/* ------------------------------------------------------------ 滚动条/分隔 */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 5px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.text_faint};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.border};
    border-radius: 5px;
    min-width: 32px;
}}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
    border: none;
    height: 0px;
    width: 0px;
}}
QSplitter::handle {{
    background: {p.border_soft};
}}

/* -------------------------------------------------------------- 其它控件 */
QTabWidget::pane {{
    border: 1px solid {p.border_soft};
    border-radius: 10px;
    background: {p.surface};
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    padding: 8px 16px;
    border-radius: 8px;
    margin-right: 4px;
}}
QTabBar::tab:selected {{
    background: {p.primary_soft};
    color: {p.text};
    font-weight: 600;
}}
QTabBar::tab:hover {{
    color: {p.text};
}}
QToolTip {{
    background: {p.elevated};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 5px 8px;
}}
QMenu {{
    background: {p.elevated};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 18px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {p.primary_soft};
}}
QStatusBar {{
    background: {p.surface};
    border-top: 1px solid {p.border_soft};
    color: {p.text_dim};
}}
QStatusBar::item {{
    border: none;
}}
#LogView {{
    background: {p.bg};
    border: 1px solid {p.border_soft};
    border-radius: 10px;
    font-family: "Cascadia Mono", "Consolas", "Menlo", monospace;
    font-size: 12px;
    color: {p.text_dim};
}}
#ToastFrame {{
    background: {p.elevated};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
#Dot {{
    border-radius: 5px;
    min-width: 10px;
    max-width: 10px;
    min-height: 10px;
    max-height: 10px;
}}
#Divider {{
    background: {p.border_soft};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}
"""


def apply_theme(app, name: str) -> Palette:
    palette = PALETTES.get(name, DARK)
    app.setStyleSheet(build_qss(palette))
    app.setFont(pick_font(10))
    return palette
