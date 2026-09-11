"""
Fluent 图标小工具 —— 用系统图标字体渲染菜单/按钮上的图标。

Win11 的 “Segoe Fluent Icons” 优先，其次 Win10 的 “Segoe MDL2 Assets”；
两者都没有（非 Windows / 精简系统 / 离屏平台）时 `glyph_icon()` 返回空图标，
调用方自己用 QPainter 画个兜底图形即可（见 `pet/ui/menu.py`）。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

# ---- 图标码点（两个字体共用同一批私有区字形）----
ICON_SETTINGS = "\ue713"
ICON_STATS = "\ue9d2"
ICON_DICT = "\ue82d"
ICON_REFRESH = "\ue72c"
ICON_BUBBLE = "\ue8bd"        # 圆形对话框（气泡）
ICON_SHOW = "\ue7b3"          # 眼睛（显示）
ICON_HIDE = "\ued1a"          # 眼睛带斜线（隐藏）
ICON_SWAP = "\ue8cb"          # 上下箭头（换一条）
ICON_CLOSE = "\ue8bb"         # 叉（关闭/收起）
ICON_QUIT = "\ue7e8"
ICON_CHEVRON = "\ue76c"
ICON_CHECK = "\ue73e"
ICON_ADD = "\ue710"           # 加号
ICON_DELETE = "\ue74d"        # 垃圾桶
ICON_UP = "\ue74a"            # 上移
ICON_DOWN = "\ue74b"          # 下移
ICON_EDIT = "\ue70f"          # 铅笔
ICON_SAVE = "\ue74e"          # 保存
ICON_FOLDER = "\ue8b7"        # 文件夹
ICON_UNDO = "\ue7a7"          # 撤销/重置

_ICON_FAMILY: str | None = None


def icon_family() -> str:
    """Win11 的 Fluent Icons 优先，其次 Win10 的 MDL2 Assets；都没有 → 空串。"""
    global _ICON_FAMILY
    if _ICON_FAMILY is None:
        try:
            families = set(QFontDatabase.families())
        except Exception:
            return ""                 # 还没建 QApplication：这次别把空结果缓存下来
        _ICON_FAMILY = next(
            (f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), "")
    return _ICON_FAMILY


def glyph_icon(ch: str, size: int = 16, color: str = "#4b4f57") -> QIcon:
    """把图标字体里的一个字形渲染成 QIcon（字号 ~16，跟菜单项/按钮对齐）。

    图标字体不可用时返回空 QIcon —— 调用方仍在，只是没有图标。
    """
    if not ch or not icon_family():
        return QIcon()
    dpr = 1.0
    scr = QApplication.primaryScreen()
    if scr is not None:
        dpr = max(1.0, float(scr.devicePixelRatio()))
    px = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.TextAntialiasing)
    f = QFont(icon_family())
    f.setPixelSize(max(8, int(round(size * dpr))))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRectF(0, 0, px.width(), px.height()), int(Qt.AlignCenter), ch)
    p.end()
    px.setDevicePixelRatio(dpr)
    return QIcon(px)
