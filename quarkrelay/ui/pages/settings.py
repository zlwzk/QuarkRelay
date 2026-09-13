"""设置页：默认目录、命名模板、分享策略、并发限速、界面与数据。"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
)

from ...core.naming import render
from ...core.quark import EXPIRED_TYPES
from ...paths import APP_DIR
from ..widgets import Card, Toast
from . import Page

TEMPLATE_PRESETS = [
    ("{name} {link}", "名字 + 链接"),
    ("{name}", "只要名字"),
    ("{link}", "只要链接"),
    ("{name} {link} 提取码：{code}", "名字 + 链接 + 提取码"),
    ("{index}. {name}\\n{link}", "带序号，链接换行"),
]


class SettingsPage(Page):
    title = "设置"
    subtitle = "所有设置即时生效并写入本机配置文件"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        cfg = services.config
        body = self.scrollable()

        # ------------------------------------------------------------ 夸克
        quark_card = Card("夸克网盘")
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("默认转存目录")
        label.setFixedWidth(120)
        label.setObjectName("Muted")
        row.addWidget(label)
        self.default_dir = QLineEdit(str(cfg.get("quark.default_dir", "夸克中转站")))
        self.default_dir.editingFinished.connect(
            lambda: cfg.set("quark.default_dir", self.default_dir.text().strip() or "夸克中转站")
        )
        row.addWidget(self.default_dir, 1)
        quark_card.add_layout(row)

        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("分享有效期")
        label.setFixedWidth(120)
        label.setObjectName("Muted")
        row.addWidget(label)
        self.expired = QComboBox()
        self.expired.addItems(list(EXPIRED_TYPES.keys()))
        current = int(cfg.get("quark.share_expired_type", 1) or 1)
        for name, value in EXPIRED_TYPES.items():
            if value == current:
                self.expired.setCurrentText(name)
        self.expired.currentTextChanged.connect(
            lambda text: cfg.set("quark.share_expired_type", EXPIRED_TYPES.get(text, 1))
        )
        row.addWidget(self.expired)
        label2 = QLabel("分享方式")
        label2.setObjectName("Muted")
        row.addWidget(label2)
        self.url_type = QComboBox()
        self.url_type.addItems(["私密（带提取码）", "公开（无提取码）"])
        self.url_type.setCurrentIndex(1 if int(cfg.get("quark.share_url_type", 2) or 2) == 1 else 0)
        self.url_type.currentIndexChanged.connect(
            lambda index: cfg.set("quark.share_url_type", 1 if index == 1 else 2)
        )
        row.addWidget(self.url_type)
        row.addStretch(1)
        quark_card.add_layout(row)
        body.addWidget(quark_card)

        # ------------------------------------------------------------ 命名
        naming_card = Card(
            "命名模板",
            "生成结果的文本格式。可用变量：{name} 名字、{link} 链接、{code} 提取码、{index} 序号、{date} 日期。",
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        self.template = QLineEdit(str(cfg.get("quark.naming_template", "{name} {link}")))
        self.template.textChanged.connect(self._on_template)
        row.addWidget(self.template, 1)
        self.preset = QComboBox()
        self.preset.addItems([f"{label}（{value}）" for value, label in TEMPLATE_PRESETS])
        self.preset.currentIndexChanged.connect(self._apply_preset)
        row.addWidget(self.preset)
        naming_card.add_layout(row)
        self.preview = QLabel("")
        self.preview.setObjectName("LinkText")
        self.preview.setWordWrap(True)
        naming_card.add(self.preview)
        body.addWidget(naming_card)

        # ------------------------------------------------------------ 运行
        run_card = Card("运行与网络")
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("并发任务数")
        label.setFixedWidth(120)
        label.setObjectName("Muted")
        row.addWidget(label)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 6)
        self.parallel.setValue(int(cfg.get("app.max_parallel", 2) or 2))
        self.parallel.valueChanged.connect(self.services.apply_parallelism)
        row.addWidget(self.parallel)
        label2 = QLabel("限速（KB/s，0 为不限）")
        label2.setObjectName("Muted")
        row.addWidget(label2)
        self.speed = QSpinBox()
        self.speed.setRange(0, 1024 * 100)
        self.speed.setSingleStep(256)
        self.speed.setValue(int(cfg.get("app.speed_limit_kbps", 0) or 0))
        self.speed.valueChanged.connect(lambda value: cfg.set("app.speed_limit_kbps", value))
        row.addWidget(self.speed)
        row.addStretch(1)
        run_card.add_layout(row)

        self.watch_clipboard = QCheckBox("监听剪贴板，复制到夸克链接后自动填充")
        self.watch_clipboard.setChecked(bool(cfg.get("app.watch_clipboard", True)))
        self.watch_clipboard.stateChanged.connect(
            lambda: self.services.set_clipboard_watch(self.watch_clipboard.isChecked())
        )
        run_card.add(self.watch_clipboard)

        self.copy_after = QCheckBox("生成分享后自动把「名字 + 链接」复制到剪贴板")
        self.copy_after.setChecked(bool(cfg.get("app.copy_after_share", True)))
        self.copy_after.stateChanged.connect(
            lambda: cfg.set("app.copy_after_share", self.copy_after.isChecked())
        )
        run_card.add(self.copy_after)

        self.notify = QCheckBox("任务完成后弹出提示")
        self.notify.setChecked(bool(cfg.get("app.notify_on_finish", True)))
        self.notify.stateChanged.connect(lambda: cfg.set("app.notify_on_finish", self.notify.isChecked()))
        run_card.add(self.notify)
        body.addWidget(run_card)

        # ------------------------------------------------------------ 界面
        ui_card = Card("界面")
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("主题")
        label.setFixedWidth(120)
        label.setObjectName("Muted")
        row.addWidget(label)
        self.theme = QComboBox()
        self.theme.addItems(["深色", "浅色"])
        self.theme.setCurrentIndex(0 if cfg.get("app.theme", "dark") == "dark" else 1)
        self.theme.currentIndexChanged.connect(self._on_theme)
        row.addWidget(self.theme)
        row.addStretch(1)
        ui_card.add_layout(row)

        self.tray = QCheckBox("关闭窗口时最小化到系统托盘（而不是退出）")
        self.tray.setChecked(bool(cfg.get("app.close_to_tray", True)))
        self.tray.stateChanged.connect(lambda: cfg.set("app.close_to_tray", self.tray.isChecked()))
        ui_card.add(self.tray)
        body.addWidget(ui_card)

        # ------------------------------------------------------------ 数据
        data_card = Card("数据与凭据", f"所有数据都在：{APP_DIR}")
        data_row = QHBoxLayout()
        data_row.setSpacing(8)
        open_dir = QPushButton("打开数据目录")
        open_dir.setObjectName("Ghost")
        open_dir.clicked.connect(self._open_dir)
        data_row.addWidget(open_dir)
        clear_hist = QPushButton("清空历史记录")
        clear_hist.setObjectName("Ghost")
        clear_hist.clicked.connect(self._clear_history)
        data_row.addWidget(clear_hist)
        clear_cred = QPushButton("清除全部登录凭据")
        clear_cred.setObjectName("Danger")
        clear_cred.clicked.connect(self._clear_credentials)
        data_row.addWidget(clear_cred)
        data_row.addStretch(1)
        data_card.add_layout(data_row)
        body.addWidget(data_card)

        self._on_template(self.template.text())

    # ------------------------------------------------------------------ 事件
    def _on_template(self, text: str) -> None:
        self.services.config.set("quark.naming_template", text.strip() or "{name} {link}")
        self.preview.setText(
            "预览：" + render(text, name="示例电影合集", link="https://pan.quark.cn/s/abcd1234", code="k9x2")
        )

    def _apply_preset(self, index: int) -> None:
        if 0 <= index < len(TEMPLATE_PRESETS):
            value = TEMPLATE_PRESETS[index][0].replace("\\n", "\n")
            self.template.setText(value)

    def _on_theme(self, index: int) -> None:
        name = "dark" if index == 0 else "light"
        self.services.config.set("app.theme", name)
        window = self.window()
        if hasattr(window, "apply_theme"):
            window.apply_theme(name)

    def _open_dir(self) -> None:
        try:
            os.startfile(APP_DIR)  # noqa: S606 - Windows 专用
        except OSError as exc:
            Toast.show_message(self, f"打不开目录：{exc}", "error")

    def _clear_history(self) -> None:
        self.services.history.clear()
        self.services.history_changed.emit()
        Toast.show_message(self, "历史记录已清空", "info")

    def _clear_credentials(self) -> None:
        self.services.config.clear_credentials()
        self.services.quark = self.services._make_quark()
        self.services.baidu = self.services._make_baidu()
        self.services.quark_changed.emit()
        self.services.baidu_changed.emit()
        Toast.show_message(self, "已清除本机保存的登录凭据", "success")
