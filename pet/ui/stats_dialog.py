"""
账单统计对话框 —— 最近 N 天每日消耗 + 按模型汇总。

数据全部来自 Ledger（ledger.json）：
  - 每日金额 = 账本的“今日已用”口径（记账模式为余额差值，令牌模式为平台用量）；
  - 按模型 = 本地中转每轮统计（令牌模式下以平台按模型数据为准，本地增量叠加）；
  - 历史长期保留（730 天），今天的数据实时计算。
从桌宠右键菜单 / 托盘菜单「账单…」打开。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QWidget,
)

from .theme import GlassDialog

_ASSETS = Path(__file__).resolve().parents[2] / "assets"

PERIODS = [("今天", 1), ("最近 7 天", 7), ("最近 30 天", 30), ("最近 90 天", 90)]


def _fmt_tokens(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


class StatsDialog(GlassDialog):
    """账单：按模型汇总表 + 每日明细表。"""

    def __init__(self, parent: QWidget | None = None, ledger=None):
        super().__init__(parent, "小鲸鱼账单",
                         icon_path=str(_ASSETS / "DSniang1.png"), resizable=True)
        self.resize(640, 680)      # 14 天以内基本不用滚动
        self.setMinimumSize(520, 420)
        self.ledger = ledger

        top = QHBoxLayout()
        top.addWidget(QLabel("统计范围"))
        self.period = QComboBox()
        for name, days in PERIODS:
            self.period.addItem(name, days)
        self.period.setCurrentIndex(1)          # 默认最近 7 天
        self.period.currentIndexChanged.connect(self.reload)
        top.addWidget(self.period)
        top.addStretch(1)
        self.summary = QLabel("—")
        top.addWidget(self.summary)

        self.model_table = self._make_table(["模型", "金额", "占比", "Token", "轮次"])
        self.day_table = self._make_table(["日期", "金额", "Token", "轮次", "主要模型"])

        tip = QLabel("金额为“今日已用”口径（记账=余额差值，令牌=平台用量）；"
                     "按模型为本地中转统计（令牌模式以平台按模型数据为准）。")
        tip.setWordWrap(True)
        tip.setObjectName("hint")

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        lay = self.body
        lay.addLayout(top)
        lay.addWidget(QLabel("按模型"))
        lay.addWidget(self.model_table, 1)
        lay.addWidget(QLabel("按天"))
        lay.addWidget(self.day_table, 1)
        lay.addWidget(tip)
        lay.addWidget(buttons)
        self.reload()

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionMode(QTableWidget.NoSelection)
        t.setAlternatingRowColors(True)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        return t

    @staticmethod
    def _cell(text: str, right: bool = False, bold: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if right:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if bold:
            f = item.font()
            f.setBold(True)
            item.setFont(f)
        return item

    @staticmethod
    def _fill(table: QTableWidget, rows: list[list]) -> None:
        """按行填充：每行 [文本, ...]，文本含 (内容, 右对齐, 加粗) 元组或纯字符串。"""
        table.setRowCount(0)
        for row in rows:
            r = table.rowCount()
            table.insertRow(r)
            for c, cell in enumerate(row):
                if isinstance(cell, tuple):
                    text, right, bold = (list(cell) + [False, False])[:3]
                else:
                    text, right, bold = str(cell), False, False
                table.setItem(r, c, StatsDialog._cell(str(text), bool(right), bool(bold)))

    def reload(self):
        """重新计算当前范围的汇总与明细（先对比一次缓存文件）。"""
        led = self.ledger
        if led is None:
            return
        try:
            led.sync_from_disk()          # 账单也以磁盘缓存为准
        except Exception:
            pass
        days = int(self.period.currentData() or 7)
        series = led.daily_series(days)
        models = led.model_totals(days)
        total = sum(float(d["usage"]) for d in series)
        tokens = sum(int(d["tokens"]) for d in series)
        turns = sum(int(d["turns"]) for d in series)
        self.summary.setText(f"合计 ¥ {total:.2f} · {_fmt_tokens(tokens)} token · "
                             f"{turns} 轮 · 日均 ¥ {total / max(1, days):.2f}")

        # ---- 按模型（金额降序；占比按金额）----
        rows = []
        for name, rec in models.items():
            cost = float(rec.get("cost") or 0.0)
            pct = (cost / total * 100.0) if total > 0 else 0.0
            rows.append([
                name,
                (f"¥ {cost:.2f}", True, False),
                (f"{pct:.1f}%", True, False),
                (_fmt_tokens(rec.get("tokens")), True, False),
                (str(int(rec.get("turns") or 0)) if rec.get("turns")
                 else "—", True, False),
            ])
        self._fill(self.model_table, rows)

        # ---- 按天（升序，今天在最后并加粗）----
        rows = []
        for day in series:
            main = "—"
            if day["models"]:
                top = max(day["models"].items(),
                          key=lambda kv: float(kv[1].get("cost") or 0.0))
                if float(top[1].get("cost") or 0.0) > 0 or top[1].get("tokens"):
                    main = top[0]
            date = day["date"][5:]
            rows.append([
                (date + ("（今天）" if day["is_today"] else ""), False, day["is_today"]),
                (f"¥ {float(day['usage']):.2f}", True, day["is_today"]),
                (_fmt_tokens(day["tokens"]), True, False),
                (str(int(day["turns"])) if day["turns"] else "—", True, False),
                main,
            ])
        self._fill(self.day_table, rows)
