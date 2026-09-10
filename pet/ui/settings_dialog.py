"""
设置对话框 —— 首次启动引导 + 桌宠右键菜单设置。
所有项实时写入 config.json 并缓存（无需重启）。
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from ..config import load_config, save_config


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, first_run: bool = False):
        super().__init__(parent)
        self.setWindowTitle("小鲸鱼设置" + ("（首次启动）" if first_run else ""))
        self.setMinimumWidth(420)
        self.cfg = load_config()

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        # --- API Key ---
        self.api_key = QLineEdit(self.cfg.get("api_key", ""))
        self.api_key.setPlaceholderText("sk-... 必需：拉余额 + 中转转发")
        self.api_key.setEchoMode(QLineEdit.Password)
        if first_run:
            self.api_key.setFocus()
        form.addRow("DeepSeek API Key", self.api_key)

        self.api_base = QLineEdit(self.cfg.get("api_base", "https://api.deepseek.com"))
        self.api_base.setPlaceholderText("https://api.deepseek.com")
        form.addRow("API 地址", self.api_base)

        # --- 中转 ---
        proxy_row = QHBoxLayout()
        self.proxy_enabled = QCheckBox("启用")
        self.proxy_enabled.setChecked(bool(self.cfg.get("proxy_enabled", True)))
        self.proxy_host = QLineEdit(self.cfg.get("proxy_host", "127.0.0.1"))
        self.proxy_host.setFixedWidth(110)
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(int(self.cfg.get("proxy_port", 11434)))
        proxy_row.addWidget(self.proxy_enabled)
        proxy_row.addWidget(QLabel("地址"))
        proxy_row.addWidget(self.proxy_host)
        proxy_row.addWidget(QLabel("端口"))
        proxy_row.addWidget(self.proxy_port)
        proxy_row.addStretch(1)
        form.addRow("API 中转", proxy_row)

        # --- 外观 ---
        scale_row = QHBoxLayout()
        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(6, 25)
        self.scale_slider.setValue(int(round(float(self.cfg.get("scale", 1.5)) * 10)))
        self.scale_val = QLabel(f"{self.cfg.get('scale', 1.5):.1f}×")
        self.scale_val.setFixedWidth(40)
        self.scale_slider.valueChanged.connect(
            lambda v: self.scale_val.setText(f"{v / 10:.1f}×"))
        scale_row.addWidget(self.scale_slider)
        scale_row.addWidget(self.scale_val)
        form.addRow("大小", scale_row)

        self.sound_set = QComboBox()
        self.sound_set.addItem("小黄鸭", "duck")
        self.sound_set.addItem("音效1", "fx1")
        idx = self.sound_set.findData(self.cfg.get("sound_set", "duck"))
        self.sound_set.setCurrentIndex(max(0, idx))
        form.addRow("音效", self.sound_set)

        vol_row = QHBoxLayout()
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(int(round(float(self.cfg.get("volume", 0.9)) * 100)))
        self.vol_val = QLabel(f"{self.vol_slider.value()}%")
        self.vol_val.setFixedWidth(44)
        self.vol_slider.valueChanged.connect(lambda v: self.vol_val.setText(f"{v}%"))
        vol_row.addWidget(self.vol_slider)
        vol_row.addWidget(self.vol_val)
        form.addRow("音量", vol_row)

        self.usage_mode = QComboBox()
        self.usage_mode.addItem("小鲸鱼记账（推荐）", "ledger")
        self.usage_mode.addItem("实时·令牌", "token")
        idx = self.usage_mode.findData(self.cfg.get("usage_mode", "ledger"))
        self.usage_mode.setCurrentIndex(max(0, idx))
        form.addRow("今日已用", self.usage_mode)

        self.platform_token = QLineEdit(self.cfg.get("platform_token", ""))
        self.platform_token.setPlaceholderText("可选：平台会话令牌（Bearer eyJ...）")
        self.platform_token.setEchoMode(QLineEdit.Password)
        form.addRow("平台令牌(可选)", self.platform_token)

        self.bubble_on = QCheckBox("余额/台词气泡")
        self.bubble_on.setChecked(bool(self.cfg.get("bubble_on", True)))
        form.addRow("气泡", self.bubble_on)

        self.turn_cost_on = QCheckBox("每轮对话后显示消耗金额")
        self.turn_cost_on.setChecked(bool(self.cfg.get("turn_cost_on", True)))
        self.turn_cost_close = QSpinBox()
        self.turn_cost_close.setRange(0, 3600)
        self.turn_cost_close.setSuffix(" 秒")
        self.turn_cost_close.setValue(int(
            self.cfg.get("turn_cost_close_ms", 5000) / 1000))
        row = QHBoxLayout()
        row.addWidget(self.turn_cost_on)
        row.addWidget(QLabel("自动关闭"))
        row.addWidget(self.turn_cost_close)
        form.addRow("每轮消耗", row)

        self.peak_mode = QComboBox()
        self.peak_mode.addItem("默认", "default")
        self.peak_mode.addItem("梁文峰谷", "liangwen")
        self.peak_mode.addItem("!?强强?!", "qiangqiang")
        idx = self.peak_mode.findData(self.cfg.get("peak_mode", "default"))
        self.peak_mode.setCurrentIndex(max(0, idx))
        form.addRow("峰谷文案", self.peak_mode)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if first_run:
            buttons.button(QDialogButtonBox.Cancel).setText("暂时跳过")

        lay = QVBoxLayout(self)
        if first_run:
            tip = QLabel("首次启动：请填入 DeepSeek API Key（必填）。\n"
                         "之后使用任意 OpenAI 兼容客户端时，把 base_url 指向本地中转即可统计消耗。")
            tip.setWordWrap(True)
            lay.addWidget(tip)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def accept(self):
        self.cfg["api_key"] = self.api_key.text().strip()
        self.cfg["api_base"] = self.api_base.text().strip() or "https://api.deepseek.com"
        self.cfg["proxy_enabled"] = self.proxy_enabled.isChecked()
        self.cfg["proxy_host"] = self.proxy_host.text().strip() or "127.0.0.1"
        self.cfg["proxy_port"] = self.proxy_port.value()
        self.cfg["scale"] = self.scale_slider.value() / 10.0
        self.cfg["sound_set"] = self.sound_set.currentData()
        self.cfg["volume"] = self.vol_slider.value() / 100.0
        self.cfg["usage_mode"] = self.usage_mode.currentData()
        self.cfg["platform_token"] = self.platform_token.text().strip()
        self.cfg["bubble_on"] = self.bubble_on.isChecked()
        self.cfg["turn_cost_on"] = self.turn_cost_on.isChecked()
        self.cfg["turn_cost_close_ms"] = self.turn_cost_close.value() * 1000
        self.cfg["peak_mode"] = self.peak_mode.currentData()
        save_config(self.cfg)
        super().accept()

    @staticmethod
    def read_plain() -> dict:
        return load_config()

    @staticmethod
    def dump(cfg: dict) -> str:
        return json.dumps(cfg, ensure_ascii=False, indent=2)
