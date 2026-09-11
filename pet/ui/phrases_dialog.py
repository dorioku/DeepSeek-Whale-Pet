"""
台词词典编辑页 —— 像设置一样打开一个**页面**，而不是直接把 JSON 丢进文本编辑器。

- 左边选台词组（权重 / 类型 / 条数一览），右边改组属性与台词；
- 保存即写回 `phrases.json`（原子写）；桌宠下次抽台词时自动重载，无需重启；
- 想直接改 JSON 也行：页面里保留「用文本编辑器打开」；
- 词典文件写坏时也进得来：顶部/底部会显示原因，并提供「恢复内置默认」。

实现要点：界面永远只是「当前正在编辑的组 / 备选」的视图，切换选择或做结构操作前
先把界面写回模型（`_sync_model` / `_sync_alt`），这样来回切换不会丢编辑。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from ..config import phrases_path
from ..phrases import open_dict_file
from .icons import (ICON_ADD, ICON_DELETE, ICON_DOWN, ICON_FOLDER, ICON_UP, glyph_icon)
from .phrases_model import (PEAK_COLS, STYLE_LABELS, TYPE_LABELS, Group, Line,
                            PhraseDoc)
from .theme import HINT_COLOR, GlassDialog

_ASSETS = Path(__file__).resolve().parents[2] / "assets"


def _tool_btn(icon: str, text: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setToolTip(tip)
    ic = glyph_icon(icon, 14)
    if ic.isNull():
        b.setText(text)
    else:
        b.setIcon(ic)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedSize(30, 26)
    return b


class PhrasesDialog(GlassDialog):
    """台词词典编辑页。"""

    def __init__(self, parent: QWidget | None = None, path=None):
        super().__init__(parent, "台词词典 · 小鲸鱼",
                         icon_path=str(_ASSETS / "DSniang1.png"), resizable=True)
        self.resize(940, 680)
        self.setMinimumSize(740, 500)
        self._loading = False
        self._row = -1          # 当前编辑的组下标
        self._alt = -1          # 当前编辑的备选下标
        self.path = Path(path) if path else phrases_path()
        self.doc = PhraseDoc(self.path)
        self.doc.load()
        self._snap = ""
        self._build_ui()
        self._load_all(select=0)
        self._set_path_label()
        self._snap = self._snapshot()
        if self.doc.error:
            self._warn(f"词典读取失败，已加载内置默认：{self.doc.error}")

    # ================= 界面搭建 =================
    def _build_ui(self):
        # --- 顶部：文件位置 + 工具 ---
        self.path_label = QLabel()
        self.path_label.setObjectName("hint")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        btn_raw = QPushButton("用文本编辑器打开…")
        btn_raw.setToolTip("直接编辑 phrases.json（高级用法）；改完回来点「重新加载」")
        btn_raw.setAutoDefault(False)
        btn_raw.clicked.connect(self._open_raw)
        btn_reload = QPushButton("重新加载")
        btn_reload.setToolTip("丢弃未保存的改动，重新读盘")
        btn_reload.setAutoDefault(False)
        btn_reload.clicked.connect(self._reload)
        top = QHBoxLayout()
        top.addWidget(QLabel("词典文件"))
        top.addWidget(self.path_label, 1)
        top.addWidget(btn_raw)
        top.addWidget(btn_reload)

        # --- 左：台词组 ---
        self.group_list = QListWidget()
        self.group_list.setMinimumWidth(230)
        self.group_list.currentRowChanged.connect(self._on_group_row)
        b_new = _tool_btn(ICON_ADD, "＋", "新建一组")
        b_dup = _tool_btn(ICON_FOLDER, "⧉", "复制这一组")
        b_del = _tool_btn(ICON_DELETE, "－", "删除这一组")
        b_up = _tool_btn(ICON_UP, "↑", "上移（只是列表顺序，不影响抽取概率）")
        b_down = _tool_btn(ICON_DOWN, "↓", "下移（只是列表顺序，不影响抽取概率）")
        b_new.clicked.connect(self._add_group)
        b_dup.clicked.connect(self._dup_group)
        b_del.clicked.connect(self._del_group)
        b_up.clicked.connect(lambda: self._move_group(-1))
        b_down.clicked.connect(lambda: self._move_group(1))
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(QLabel("台词组（每次按权重随机挑一组）"))
        left.addWidget(self.group_list, 1)
        row = QHBoxLayout()
        for b in (b_new, b_dup, b_del, b_up, b_down):
            row.addWidget(b)
        row.addStretch(1)
        left.addLayout(row)

        # --- 右：组属性 ---
        self.weight = QDoubleSpinBox()
        self.weight.setRange(0.1, 99999.0)
        self.weight.setDecimals(2)
        self.weight.setToolTip("相对权重：越大越常出现（默认 1）")
        self.gtype = QComboBox()
        for val, label in TYPE_LABELS:
            self.gtype.addItem(label, val)
        self.gtype.currentIndexChanged.connect(self._on_type_combo)
        self.gstyle = QComboBox()
        for val, label in STYLE_LABELS:
            self.gstyle.addItem(label, val)
        self.gwrap = QCheckBox("长句自动换行")
        self.gsfx = QLineEdit()
        self.gsfx.setPlaceholderText("可选：音效名（如 ciallo）")
        self.gsfx.setToolTip("显示这组台词时播放同名音效：先找 config 同目录 sounds/，"
                             "再找内置 assets/（支持 wav/mp3/aac 等）")
        self.gon = QCheckBox("启用这一组")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow("权重", self.weight)
        form.addRow("类型", self.gtype)
        self.lbl_style = QLabel("样式")
        form.addRow(self.lbl_style, self.gstyle)
        form.addRow("", self.gwrap)
        form.addRow("音效", self.gsfx)
        form.addRow("", self.gon)
        for w in (self.gstyle, self.gwrap, self.gsfx, self.gon):
            self._watch_meta(w)
        self.weight.valueChanged.connect(lambda *_: self._on_edit())
        self.gsfx.textEdited.connect(lambda *_: self._on_edit())
        self.gwrap.toggled.connect(lambda *_: self._on_edit())
        self.gon.toggled.connect(lambda *_: self._on_edit())

        # 峰谷文案表
        self.peak_table = QTableWidget(0, len(PEAK_COLS))
        self.peak_table.setHorizontalHeaderLabels(list(PEAK_COLS))
        self.peak_table.verticalHeader().setVisible(False)
        self.peak_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for c in range(1, len(PEAK_COLS)):
            self.peak_table.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch)
        self.peak_table.itemChanged.connect(lambda *_: self._on_edit())
        b_padd = _tool_btn(ICON_ADD, "＋", "新增一套峰谷文案")
        b_pdel = _tool_btn(ICON_DELETE, "－", "删除选中的文案")
        b_padd.clicked.connect(self._add_peak_mode)
        b_pdel.clicked.connect(self._del_peak_mode)
        peak_head = QHBoxLayout()
        peak_head.addWidget(QLabel("峰谷文案（模式名对应设置里的「峰谷文案」选项）"))
        peak_head.addStretch(1)
        peak_head.addWidget(b_padd)
        peak_head.addWidget(b_pdel)
        self.peak_box = QWidget()
        pb = QVBoxLayout(self.peak_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(6)
        pb.addLayout(peak_head)
        pb.addWidget(self.peak_table, 1)

        # 备选台词 + 行表格
        self.alt_list = QListWidget()
        self.alt_list.currentRowChanged.connect(self._on_alt_row)
        b_aadd = _tool_btn(ICON_ADD, "＋", "新增一条备选台词（每次随机挑一条）")
        b_adel = _tool_btn(ICON_DELETE, "－", "删除选中的备选")
        b_aadd.clicked.connect(self._add_alt)
        b_adel.clicked.connect(self._del_alt)
        alt_row = QHBoxLayout()
        alt_row.addWidget(QLabel("备选台词"))
        alt_row.addStretch(1)
        alt_row.addWidget(b_aadd)
        alt_row.addWidget(b_adel)

        self.line_table = QTableWidget(0, 4)
        self.line_table.setHorizontalHeaderLabels(["文案", "样式", "颜色（可空）", "换行"])
        self.line_table.verticalHeader().setVisible(False)
        self.line_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.line_table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.line_table.itemChanged.connect(lambda *_: self._on_edit())
        b_ladd = _tool_btn(ICON_ADD, "＋", "在这条备选里加一行（多行 = 一次显示多行）")
        b_ldel = _tool_btn(ICON_DELETE, "－", "删除选中的行")
        b_ladd.clicked.connect(self._add_line)
        b_ldel.clicked.connect(self._del_line)
        line_row = QHBoxLayout()
        line_row.addWidget(QLabel("这一条备选的行（多行 = 一次显示多行）"))
        line_row.addStretch(1)
        line_row.addWidget(b_ladd)
        line_row.addWidget(b_ldel)

        self.lines_box = QWidget()
        lb = QVBoxLayout(self.lines_box)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.setSpacing(6)
        lb.addLayout(alt_row)
        lb.addWidget(self.alt_list, 1)
        lb.addLayout(line_row)
        lb.addWidget(self.line_table, 2)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addLayout(form)
        right.addWidget(self.peak_box, 1)
        right.addWidget(self.lines_box, 2)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        mid.addLayout(left, 0)
        mid.addLayout(right, 1)

        # --- 底部 ---
        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        btn_default = QPushButton("恢复内置默认")
        btn_default.setToolTip("用内置默认词典覆盖当前文件（会先问一次）")
        btn_default.setAutoDefault(False)
        btn_default.clicked.connect(self._restore_default)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("primary")      # Win11 主按钮配色
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._save)
        btn_close = QPushButton("关闭")
        btn_close.setAutoDefault(False)
        btn_close.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(btn_default)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_save)
        bottom.addWidget(btn_close)

        self.body.addLayout(top)
        self.body.addLayout(mid, 1)
        self.body.addLayout(bottom)

    # ================= 小工具 =================
    def _watch_meta(self, w):
        """组属性里的下拉/勾选：改动后写回模型并刷新左侧摘要。"""
        if isinstance(w, QComboBox):
            w.currentIndexChanged.connect(lambda *_: self._on_edit())
        elif isinstance(w, QCheckBox):
            w.toggled.connect(lambda *_: self._on_edit())

    def _set_path_label(self):
        p = str(self.path)
        self.path_label.setText(p if len(p) <= 66 else "…" + p[-65:])
        self.path_label.setToolTip(p)

    def _snapshot(self) -> str:
        return json.dumps(self.doc.to_data(), ensure_ascii=False, sort_keys=True)

    def _dirty(self) -> bool:
        return self._snapshot() != self._snap

    def _status(self, text: str, *, warn: bool = False, ok: bool = False):
        color = "#c0392b" if warn else ("#1f7a3f" if ok else HINT_COLOR)
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)

    def _warn(self, text: str, ok: bool = False):
        self._status(text, warn=not ok, ok=ok)

    def _cur(self) -> Group | None:
        return self.doc.groups[self._row] if 0 <= self._row < len(self.doc.groups) else None

    # ================= 载入（模型 → 界面）=================
    def _load_all(self, select: int = 0):
        self._row, self._alt = -1, -1
        self._loading = True
        self.group_list.clear()
        for g in self.doc.groups:
            self.group_list.addItem(g.summary())
        self._loading = False
        if self.doc.groups:
            i = max(0, min(select, len(self.doc.groups) - 1))
            self.group_list.setCurrentRow(i)
            self._show_group(i)
        else:
            self._show_group(-1)

    def _show_group(self, i: int):
        """把第 i 组载入右侧编辑器（不写回模型）。"""
        self._row, self._alt = i, -1
        g = self._cur()
        self._loading = True
        for w in (self.weight, self.gtype, self.gstyle, self.gwrap, self.gsfx, self.gon):
            w.setEnabled(g is not None)
        if g is not None:
            self.weight.setValue(float(g.weight))
            self.gtype.setCurrentIndex(max(0, self.gtype.findData(g.type)))
            self.gstyle.setCurrentIndex(max(0, self.gstyle.findData(g.style)))
            self.gwrap.setChecked(bool(g.wrap))
            self.gsfx.setText(g.sfx)
            self.gon.setChecked(bool(g.enabled))
        self._loading = False
        self._apply_visibility(g)

    def _apply_visibility(self, g: Group | None):
        kind = g.type if g is not None else "lines"
        is_peak, is_gif = kind == "peak", kind == "gif"
        self.peak_box.setVisible(g is not None and is_peak)
        self.lines_box.setVisible(g is not None and not (is_peak or is_gif))
        for w in (self.gstyle, self.lbl_style, self.gwrap):
            w.setVisible(not (is_peak or is_gif))
        if g is None:
            return
        if is_peak:
            self._load_peak()
        elif not is_gif:
            self._show_alt(0)

    def _show_alt(self, i: int):
        """把第 i 条备选载入行表格（不写回模型）。"""
        g = self._cur()
        self._alt = -1
        self._loading = True
        self.alt_list.clear()
        if g is not None:
            for alt in g.alts:
                text = " / ".join(ln.text for ln in alt if ln.text.strip()) or "（空）"
                self.alt_list.addItem(text[:60])
        self._loading = False
        if g is not None and g.alts:
            i = max(0, min(i, len(g.alts) - 1))
            self.alt_list.setCurrentRow(i)
            self._alt = i
        self._load_lines()

    def _load_lines(self):
        g = self._cur()
        rows = g.alts[self._alt] if (g is not None and 0 <= self._alt < len(g.alts)) else []
        t = self.line_table
        self._loading = True
        t.setRowCount(len(rows))
        for r, ln in enumerate(rows):
            t.setItem(r, 0, QTableWidgetItem(ln.text))
            combo = QComboBox()
            for val, label in STYLE_LABELS:
                combo.addItem(label, val)
            combo.setCurrentIndex(max(0, combo.findData(ln.style)))
            combo.currentIndexChanged.connect(lambda *_: self._on_edit())
            t.setCellWidget(r, 1, combo)
            t.setItem(r, 2, QTableWidgetItem(ln.color))
            it = QTableWidgetItem()
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            it.setCheckState(Qt.CheckState.Checked if ln.wrap else Qt.CheckState.Unchecked)
            t.setItem(r, 3, it)
        self._loading = False

    def _load_peak(self):
        g = self._cur()
        if g is None:
            return
        modes = g.peak_modes()
        t = self.peak_table
        self._loading = True
        t.setRowCount(len(modes))
        for r, mode in enumerate(modes):
            d = g.peak_row(mode)
            t.setItem(r, 0, QTableWidgetItem(mode))
            title = d.get("title", "")
            t.setItem(r, 1, QTableWidgetItem("" if title is None else str(title)))
            t.setItem(r, 2, QTableWidgetItem(str(d.get("off", "") or "")))
            t.setItem(r, 3, QTableWidgetItem(str(d.get("peak", "") or "")))
        self._loading = False

    # ================= 写回（界面 → 模型）=================
    def _sync_model(self):
        """把右侧编辑器写回当前组（含当前备选的行）。"""
        g = self._cur()
        if g is None:
            return
        self._sync_alt(g)
        g.weight = float(self.weight.value())
        g.type = self.gtype.currentData() or "lines"
        g.style = self.gstyle.currentData() or "A"
        g.wrap = bool(self.gwrap.isChecked())
        g.sfx = self.gsfx.text().strip()
        g.enabled = bool(self.gon.isChecked())
        if g.type == "peak":
            self._sync_peak(g)

    def _sync_alt(self, g: Group):
        if g.type != "lines" or not (0 <= self._alt < len(g.alts)):
            return
        old_rows = g.alts[self._alt]
        rows: list[Line] = []
        for r in range(self.line_table.rowCount()):
            item = self.line_table.item(r, 0)
            text = item.text() if item is not None else ""
            combo = self.line_table.cellWidget(r, 1)
            style = (combo.currentData() if isinstance(combo, QComboBox) else None) or g.style
            c_item = self.line_table.item(r, 2)
            color = c_item.text().strip() if c_item is not None else ""
            w_item = self.line_table.item(r, 3)
            wrap = w_item is not None and w_item.checkState() == Qt.CheckState.Checked
            old = old_rows[r] if r < len(old_rows) else None
            rows.append(Line(text, style, color, wrap,
                             sfx=(old.sfx if old else ""),
                             as_str=(old.as_str if old else True),
                             extra=(dict(old.extra) if old else {})))
        g.alts[self._alt] = rows

    def _sync_peak(self, g: Group):
        modes = g.peak_modes()
        for r in range(min(self.peak_table.rowCount(), len(modes))):
            mode_item = self.peak_table.item(r, 0)
            new_mode = mode_item.text().strip() if mode_item is not None else modes[r]
            d = g.peak_row(modes[r])
            if new_mode and new_mode != modes[r]:
                del g.texts[modes[r]]
                g.texts[new_mode] = d
                modes[r] = new_mode
            for col, key in ((1, "title"), (2, "off"), (3, "peak")):
                it = self.peak_table.item(r, col)
                txt = it.text() if it is not None else ""
                if key == "title" and txt == "" and d.get("title") is None:
                    continue      # 空着 = 保持 None（None 表示「用内置标题」，
                                  # "" 表示「不显示标题行」，两者不能混）
                d[key] = txt

    # ================= 编辑回调 =================
    def _on_edit(self, *_a):
        if self._loading:
            return
        self._sync_model()
        self._refresh_summary()
        self._status("有未保存的改动（点右下角「保存」）")

    def _on_group_row(self, row: int):
        if self._loading or row < 0 or row == self._row:
            return
        self._sync_model()                     # 先写回上一组
        self._refresh_summary(self._row)
        self._show_group(row)

    def _on_alt_row(self, row: int):
        if self._loading or row < 0 or row == self._alt:
            return
        g = self._cur()
        if g is not None:
            self._sync_alt(g)
        self._alt = row
        self._load_lines()

    def _on_type_combo(self, *_a):
        if self._loading:
            return
        self._sync_model()
        g = self._cur()
        if g is not None:
            if g.type == "peak" and not g.texts:
                g.texts["default"] = {"title": "当前时间段为:",
                                      "off": "空闲时段", "peak": "高峰时段"}
            if g.type == "lines" and not g.alts:
                g.alts.append([Line("新台词", g.style, wrap=g.wrap)])
                g.alt_forms.append(False)
        self._apply_visibility(g)
        self._refresh_summary()
        self._status("有未保存的改动（点右下角「保存」）")

    def _refresh_summary(self, i: int | None = None):
        i = self._row if i is None else i
        if not (0 <= i < len(self.doc.groups) and i < self.group_list.count()):
            return
        self._loading = True
        self.group_list.item(i).setText(self.doc.groups[i].summary())
        self._loading = False

    # ================= 结构操作 =================
    def _add_group(self):
        self._sync_model()
        self.doc.groups.append(Group(weight=1.0, style="A", wrap=True,
                                     alts=[[Line("新台词", "A", wrap=True)]]))
        self._load_all(select=len(self.doc.groups) - 1)
        self._status("已新建一组（记得保存）")

    def _dup_group(self):
        self._sync_model()
        i = self._row
        if not (0 <= i < len(self.doc.groups)):
            return
        self.doc.groups.insert(i + 1, Group.from_raw(self.doc.groups[i].to_raw()))
        self._load_all(select=i + 1)
        self._status("已复制这一组（记得保存）")

    def _del_group(self):
        self._sync_model()
        i = self._row
        g = self._cur()
        if g is None:
            return
        if QMessageBox.question(self, "删除台词组",
                                f"确定删除这一组吗？\n\n{g.summary()}") != QMessageBox.Yes:
            return
        self.doc.groups.pop(i)
        self._load_all(select=max(0, i - 1))
        self._status("已删除（记得保存）")

    def _move_group(self, delta: int):
        self._sync_model()
        i, j = self._row, self._row + delta
        if not (0 <= i < len(self.doc.groups) and 0 <= j < len(self.doc.groups)):
            return
        self.doc.groups[i], self.doc.groups[j] = self.doc.groups[j], self.doc.groups[i]
        self._load_all(select=j)

    def _add_alt(self):
        g = self._cur()
        if g is None:
            return
        self._sync_model()
        g.alts.append([Line("新台词", g.style, wrap=g.wrap)])
        g.alt_forms.append(False)          # 单行备选写成「一整句」，文件更清爽
        self._show_alt(len(g.alts) - 1)
        self._refresh_summary()
        self._status("已加一条备选（记得保存）")

    def _del_alt(self):
        g = self._cur()
        i = self._alt
        if g is None or not (0 <= i < len(g.alts)):
            return
        g.alts.pop(i)
        if i < len(g.alt_forms):
            g.alt_forms.pop(i)
        self._show_alt(max(0, i - 1))
        self._refresh_summary()
        self._status("已删除一条备选（记得保存）")

    def _add_line(self):
        g = self._cur()
        if g is None or not (0 <= self._alt < len(g.alts)):
            return
        self._sync_model()
        g.alts[self._alt].append(Line("新的一行", g.style, wrap=g.wrap))
        self._load_lines()
        self.line_table.setCurrentCell(len(g.alts[self._alt]) - 1, 0)
        self._refresh_summary()
        self._status("已加一行（记得保存）")

    def _del_line(self):
        g = self._cur()
        if g is None or not (0 <= self._alt < len(g.alts)):
            return
        self._sync_model()
        r = self.line_table.currentRow()
        if len(g.alts[self._alt]) <= 1:
            self._warn("一条备选至少要有一行；想删掉整条请用「－」删备选")
            return
        if 0 <= r < len(g.alts[self._alt]):
            g.alts[self._alt].pop(r)
        self._load_lines()
        self._refresh_summary()
        self._status("已删除一行（记得保存）")

    def _add_peak_mode(self):
        g = self._cur()
        if g is None:
            return
        self._sync_model()
        n = 1
        while f"mode{n}" in g.texts:
            n += 1
        g.texts[f"mode{n}"] = {"title": "当前时间段为:", "off": "空闲时段", "peak": "高峰时段"}
        self._load_peak()
        self._status("已加一套峰谷文案（记得保存）")

    def _del_peak_mode(self):
        g = self._cur()
        if g is None or self.peak_table.currentRow() < 0:
            return
        self._sync_model()
        modes = g.peak_modes()
        r = self.peak_table.currentRow()
        if len(modes) <= 1:
            self._warn("至少保留一套峰谷文案")
            return
        if 0 <= r < len(modes):
            g.texts.pop(modes[r], None)
        self._load_peak()
        self._status("已删除一套文案（记得保存）")

    # ================= 文件操作 =================
    def _save(self):
        self._sync_model()
        self._refresh_summary()
        err = self.doc.save()
        if err:
            self._warn(f"保存失败：{err}")
            return
        self._snap = self._snapshot()
        self._status("已保存 · 桌宠下次抽台词就用新词典（无需重启）", ok=True)

    def _reload(self):
        if self._dirty() and QMessageBox.question(
                self, "重新加载", "有未保存的改动，确定丢弃并重新读盘吗？") != QMessageBox.Yes:
            return
        self.doc = PhraseDoc(self.path)
        self.doc.load()
        self._load_all(select=0)
        self._set_path_label()
        self._snap = self._snapshot()
        if self.doc.error:
            self._warn(f"词典读取失败，已加载内置默认：{self.doc.error}")
        else:
            self._status("已重新读盘")

    def _restore_default(self):
        if QMessageBox.question(
                self, "恢复内置默认",
                "用内置默认词典覆盖词典文件？\n（现在的内容会被替换掉）") != QMessageBox.Yes:
            return
        self.doc.restore_default()
        self._load_all(select=0)
        self._snap = self._snapshot()
        self._status("已恢复内置默认词典", ok=True)

    def _open_raw(self):
        """用系统默认程序打开 JSON（保留给习惯直接编辑的人）。"""
        try:
            open_dict_file(self, self.path)
        except Exception as e:                       # pragma: no cover - 兜底
            self._warn(f"打开失败：{e}")
        else:
            self._status("已在文本编辑器里打开；改完保存后回到这里点「重新加载」")

    # ================= 关闭 =================
    def reject(self):
        if self._dirty():
            box = QMessageBox(self)
            box.setWindowTitle("还没保存")
            box.setText("有未保存的改动，要保存吗？")
            box.setIcon(QMessageBox.Question)
            box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard
                                   | QMessageBox.Cancel)
            box.setDefaultButton(QMessageBox.Save)
            r = box.exec()
            if r == QMessageBox.Save:
                self._save()
                if self._dirty():                    # 校验没过就别关
                    return
            elif r == QMessageBox.Cancel:
                return
        super().reject()
