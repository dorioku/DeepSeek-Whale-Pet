"""
W11 风格弹出菜单 —— 微倒角 + 微毛玻璃 + 轻微透明 的清爽菜单。

用于小鲸鱼的右键菜单与托盘菜单；**二级菜单（子菜单）用同一个类，自动同款样式**：

    menu = WhaleMenu()
    act = menu.addAction(glyph_icon(ICON_SETTINGS), "设置…")
    sub = menu.addMenu(glyph_icon(ICON_BUBBLE), "气泡")   # 子菜单 = WhaleMenu，自动同款
    sub.addAction("…")

外观实现：
- 卡片（圆角 8px、1px 细描边、顶部细高光）由 `paintEvent` 自绘，**不裁剪**任何样式；
- 卡片底色微透明；在 Windows 上再给窗口开 DWM 亚克力（acrylic）模糊，形成「微毛玻璃」；
  老系统 / 远程桌面 / 环境变量 `WHALE_PET_NO_ACRYLIC=1` 时自动降级为纯半透明卡片；
- 菜单项 / 分隔线 / 悬停高亮走 QSS，动作、快捷键、键盘导航仍是 QMenu 原生逻辑。

图标：优先用 Win11 的 “Segoe Fluent Icons”，其次 Win10 的 “Segoe MDL2 Assets”；
两者都没有（非 Windows / 精简系统）时 `glyph_icon()` 返回空图标，菜单照常可用。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu

from .icons import (ICON_BUBBLE, ICON_CHECK, ICON_CHEVRON, ICON_CLOSE, ICON_DICT,
                    ICON_HIDE, ICON_QUIT, ICON_REFRESH, ICON_SETTINGS, ICON_SHOW,
                    ICON_STATS, ICON_SWAP, glyph_icon)   # noqa: F401（对外转发）
from .theme import (ACRYLIC_WITHOUT_DWM_ROUND, acrylic_allowed as _acrylic_allowed,
                    round_window_corners, set_acrylic_window as _set_window_acrylic)

# ---- 外观常量（改这几个就能整体调色 / 调透明度）----
RADIUS = 8.0                  # 卡片圆角：**必须与 DWM 的系统圆角一致**（约 8），
                              # 画大了会在角落被 DWM 裁掉一截边框
CARD_RGB = (243, 244, 246)    # 卡片底色：中性灰白（比纯白更像 Win11 亚克力面板）
CARD_ALPHA_ACRYLIC = 108      # 卡片透明度：再透一点，模糊的背景更明显
CARD_ALPHA_PLAIN = 236        # 无亚克力时降级透明度（几乎不透明，先保证可读）
BORDER_RGBA = (0, 0, 0, 16)   # 1px 细描边
TOP_HIGHLIGHT = (255, 255, 255, 120)   # 顶部细高光（Win11 卡片的“亮边”）
ACRYLIC_TINT = 0xC0F6F6F6     # DWM 亚克力底色（ABGR：alpha 0xC0 + 中性浅灰）
                              # —— 压在暗背景上也偏亮，就是开始菜单那种“亮而不实”
TEXT_COLOR = "#1f2329"
TEXT_DISABLED = "#9fa4ab"
ICON_FG = "#43474e"           # 图标默认色（比文字浅一点，更清爽）
ICON_HOVER_FG = "#1f2329"
ARROW_FG = "#4a4f55"          # 子菜单箭头
CHECK_FG = "#1f6feb"          # 勾选标记（勾在勾选项左侧，同 Win11）

# 图标码点 / glyph_icon 见 pet/ui/icons.py（上面已导入）

# 菜单项 / 分隔线 / 悬停高亮（卡片本身由 paintEvent 自绘）
_QSS = f"""
QMenu {{
    background: transparent;      /* 卡片由 paintEvent 自绘（圆角/描边/高光） */
    border: none;
    padding: 7px 5px;
    color: {TEXT_COLOR};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI Variable Text", "Segoe UI";
    font-size: 14px;
}}
QMenu::item {{
    min-width: 176px;
    padding: 9px 34px 9px 44px;
    margin: 1px 5px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: rgba(0, 0, 0, 0.060);
}}
QMenu::item:pressed {{
    background-color: rgba(0, 0, 0, 0.090);
}}
QMenu::item:disabled {{
    color: {TEXT_DISABLED};
}}
QMenu::separator {{
    height: 1px;
    background: rgba(0, 0, 0, 0.06);
    margin: 6px 14px;
}}
QMenu::icon {{
    padding-left: 12px;           /* 图标缩进（与 item 的 padding-left 配合） */
}}
QMenu::indicator {{
    width: 0px;                   /* 勾选标记由 paintEvent 自绘（原生指示器太旧） */
    height: 0px;
}}
QMenu::right-arrow {{
    image: none;                  /* 默认三角收起：子菜单箭头由 paintEvent 自绘 */
    width: 0px;
    height: 0px;
}}
"""

# 亚克力与图标实现见 pet/ui/theme.py / pet/ui/icons.py（上面已导入）


class WhaleMenu(QMenu):
    """W11 风格菜单：微倒角 + 微毛玻璃 + 轻微透明。

    - 子菜单：`menu.addMenu("更多")` 返回的也是 WhaleMenu（自动同款样式）；
    - 只改外观：动作、快捷键、键盘导航、`exec()` 行为与原生 QMenu 一致。
    """

    def __init__(self, parent=None, title: str = ""):
        super().__init__(parent)
        if title:
            self.setTitle(title)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setStyleSheet(_QSS)
        self._acrylic = False
        self._acrylic_hwnd = 0
        self._rounded = False

    def addMenu(self, *args):
        """子菜单一律是 WhaleMenu（同款样式）。

        支持 `addMenu("标题")` / `addMenu(icon, "标题")`；若传入的是已建好的
        QMenu 实例则原样接上（并补一份样式，保证不是裸样式）。
        """
        icon, title = None, ""
        for a in args:
            if isinstance(a, QMenu):
                if a is self or self.isAncestorOf(a):
                    return a
                style_menu(a)                   # 外部菜单：套上同款 QSS
                super().addMenu(a)
                return a
            if isinstance(a, QIcon):
                icon = a
            elif isinstance(a, str):
                title = a
        sub = WhaleMenu(self, title)
        if icon is not None and not icon.isNull():
            sub.menuAction().setIcon(icon)
        super().addMenu(sub)
        return sub

    # ---- 外观 ----
    def showEvent(self, ev):
        super().showEvent(ev)
        # 每次显示都确认一遍：Qt 可能重建原生窗口（句柄变了亚克力就没了）
        QTimer.singleShot(0, self._apply_acrylic)

    def _apply_acrylic(self):
        if not self.isVisible() or not _acrylic_allowed():
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        if self._acrylic and hwnd == self._acrylic_hwnd:
            return                       # 同一个窗口已经开过，不重复调用
        # 圆角交给 DWM：亚克力会把窗口矩形铺满，自己画的透明角会被它填掉
        self._rounded = round_window_corners(hwnd)
        try:
            if self._rounded or ACRYLIC_WITHOUT_DWM_ROUND:
                self._acrylic = _set_window_acrylic(hwnd, ACRYLIC_TINT)
            else:
                self._acrylic = False
        except Exception:
            self._acrylic = False
        self._acrylic_hwnd = hwnd if (self._acrylic or self._rounded) else 0
        self.update()

    def paintEvent(self, ev):
        self._paint_card()
        super().paintEvent(ev)
        self._paint_arrows()
        self._paint_checks()

    def _paint_card(self):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, RADIUS, RADIUS)
        alpha = CARD_ALPHA_ACRYLIC if self._acrylic else CARD_ALPHA_PLAIN
        p.fillPath(path, QColor(*CARD_RGB, alpha))
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(QColor(*TOP_HIGHLIGHT), 1.0))
        p.drawLine(QPointF(r.left() + RADIUS, r.top() + 0.5),
                   QPointF(r.right() - RADIUS, r.top() + 0.5))
        p.restore()
        p.setPen(QPen(QColor(*BORDER_RGBA), 1.0))
        p.drawPath(path)
        p.end()

    def _paint_arrows(self):
        """子菜单行的右侧箭头（原生三角太粗，换成 Fluent 细箭头）。"""
        subs = [a for a in self.actions() if a.menu() is not None]
        if not subs:
            return
        pm = glyph_icon(ICON_CHEVRON, 14, ARROW_FG).pixmap(14, 14)
        p = QPainter(self)
        if pm.isNull():
            # 图标字体不可用：自绘一个细箭头兜底（保证“有子菜单”的提示不丢）
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(ARROW_FG), 1.4))
            for a in subs:
                g = self.actionGeometry(a)
                if g.isEmpty():
                    continue
                x, y = float(g.right()) - 12.0, float(g.center().y())
                p.drawPolyline([QPointF(x, y - 3.6), QPointF(x + 3.6, y),
                                QPointF(x, y + 3.6)])
            p.end()
            return
        w = max(1.0, pm.width() / max(1.0, pm.devicePixelRatio()))
        h = max(1.0, pm.height() / max(1.0, pm.devicePixelRatio()))
        for a in subs:
            g = self.actionGeometry(a)
            if g.isEmpty():
                continue
            p.drawPixmap(QPointF(g.right() - w - 6.0,
                                 g.center().y() - h / 2.0 + 1.0), pm)
        p.end()

    def _paint_checks(self):
        """勾选项左侧的 Fluent 勾（原生指示器在 QSS 里被收成 0 宽）。"""
        marks = [a for a in self.actions() if a.isCheckable() and a.isChecked()]
        if not marks:
            return
        pm = glyph_icon(ICON_CHECK, 15, CHECK_FG).pixmap(15, 15)
        p = QPainter(self)
        if pm.isNull():
            # 图标字体不可用：自绘勾号兜底（否则勾选状态看不出来）
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(CHECK_FG), 1.6))
            for a in marks:
                g = self.actionGeometry(a)
                if g.isEmpty():
                    continue
                x, y = float(g.left()) + 14.0, float(g.center().y())
                p.drawPolyline([QPointF(x - 3.6, y), QPointF(x - 1.0, y + 2.8),
                                QPointF(x + 4.2, y - 3.2)])
            p.end()
            return
        w = max(1.0, pm.width() / max(1.0, pm.devicePixelRatio()))
        h = max(1.0, pm.height() / max(1.0, pm.devicePixelRatio()))
        for a in marks:
            g = self.actionGeometry(a)
            if g.isEmpty():
                continue
            p.drawPixmap(QPointF(g.left() + 13.0, g.center().y() - h / 2.0 + 1.0), pm)
        p.end()


def style_menu(menu: QMenu) -> QMenu:
    """给已存在的普通 QMenu 套上同款外观（透明底 + QSS）；返回原对象。

    自绘卡片（圆角/描边）只有 WhaleMenu 才有，外部菜单至少保证配色一致。
    """
    menu.setAttribute(Qt.WA_TranslucentBackground, True)
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint)
    menu.setStyleSheet(_QSS)
    return menu
