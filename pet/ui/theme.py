"""
小鲸鱼 UI 主题 —— W11 风格「微倒角 + 半透明磨砂玻璃」窗口与控件的公共实现。

`GlassDialog`：无边框 + 自绘圆角卡片 + DWM 亚克力模糊 + 自绘标题栏（可拖动、关闭）。
设置 / 账单 / 台词词典等页面都继承它，风格与右键菜单保持一致。

- 亚克力不可用（非 Windows / 远程桌面 / `WHALE_PET_NO_ACRYLIC=1`）时自动降级为
  “几乎不透明”的圆角卡片，保证可读；
- 控件外观沿用 Qt 的 windows11 原生样式（本来就是 Fluent 风），这里只补齐
  在透明卡片上需要微调的部分（表格/列表/滚动条/提示文字）。
"""
from __future__ import annotations

import ctypes
import os
import sys

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QImage, QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QSizeGrip,
                               QToolButton, QVBoxLayout)

from .icons import ICON_CLOSE, glyph_icon

# ---- 卡片外观（想整体调色/调透明度改这几个）----
RADIUS = 8.0                  # 边缘倒角：**必须和 DWM 的系统圆角一致**（约 8），
                              # 画大了会在角落被 DWM 裁掉一截边框
CARD_RGB = (243, 244, 246)    # 卡片底色：中性灰白（Win11 亚克力面板观感）
CARD_ALPHA_ACRYLIC = 132      # 有亚克力时的卡片透明度（对话框字多，比菜单稍实一点）
CARD_ALPHA_PLAIN = 240        # 无亚克力时的降级透明度
BORDER_RGBA = (0, 0, 0, 16)   # 1px 细描边
TOP_HIGHLIGHT = (255, 255, 255, 120)
SEP_RGBA = (0, 0, 0, 12)      # 标题栏下的细分隔线
ACRYLIC_TINT = 0xB4F6F6F6     # DWM 亚克力底色（ABGR：alpha 0xB4 + 中性浅灰）
TEXT_COLOR = "#1f2329"
HINT_COLOR = "#6b7280"
ACCENT = "#3b6fd4"

TITLE_H = 36                  # 自绘标题栏高度（拖动区，Win11 标题栏比菜单行略矮）
TITLE_ICON = 20               # 标题左侧的小鲸鱼图标尺寸


# ================= 亚克力毛玻璃（Windows 原生，失败自动降级）=================
class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_uint),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int)]


class _WcaData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int),
                ("Data", ctypes.POINTER(_AccentPolicy)),
                ("SizeOfData", ctypes.c_size_t)]


_WCA_ACCENT_POLICY = 19
_ACCENT_ENABLE_BLURBEHIND = 3
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

# Win11 的窗口圆角偏好：亚克力会把整个窗口矩形铺满（连透明的角落也填上），
# 所以圆角不能只靠“画一个圆角透明卡片”，得让 DWM 把窗口本身裁成圆角。
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2

# DWM 圆角不可用（Win10 等）时是否仍要开亚克力：默认**不开**——
# 宁愿要正确的圆角（半透明卡片），也不要方角的毛玻璃。
ACRYLIC_WITHOUT_DWM_ROUND = False


def round_window_corners(hwnd: int, preference: int = _DWMWCP_ROUND) -> bool:
    """让 DWM 把窗口（连带亚克力）裁成系统圆角；不支持时返回 False。

    Win11 才能用；系统圆角约 8px，和 Win11 飞窗一致（自绘的 RADIUS 要对齐它，
    否则边框弧线会在角落被 DWM 裁掉一截）。
    """
    try:
        v = ctypes.c_int(int(preference))
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(hwnd), _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(v),
            ctypes.sizeof(v))
        return hr == 0
    except Exception:
        return False


def set_acrylic_window(hwnd: int, tint_abgr: int = ACRYLIC_TINT) -> bool:
    """给窗口开 DWM 亚克力模糊（失败自动回落普通模糊）。返回是否成功。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        fn = getattr(ctypes.windll.user32, "SetWindowCompositionAttribute", None)
        if fn is None:
            return False
        for state in (_ACCENT_ENABLE_ACRYLICBLURBEHIND, _ACCENT_ENABLE_BLURBEHIND):
            accent = _AccentPolicy(AccentState=state, AccentFlags=2,
                                   GradientColor=tint_abgr, AnimationId=0)
            data = _WcaData(Attribute=_WCA_ACCENT_POLICY,
                            Data=ctypes.pointer(accent),
                            SizeOfData=ctypes.sizeof(accent))
            if fn(int(hwnd), ctypes.byref(data)):
                return True
        return False
    except Exception:
        return False


def acrylic_allowed() -> bool:
    """Windows 且没被环境变量关掉时才尝试亚克力。"""
    if not sys.platform.startswith("win"):
        return False
    return not os.environ.get("WHALE_PET_NO_ACRYLIC")


_BOX_CACHE: dict = {}


def _content_box(img: QImage, thresh: int = 8, key: str = "") -> QRect | None:
    """非透明内容的包围盒（隔点扫描，结果按素材路径缓存）。"""
    if key and key in _BOX_CACHE:
        return _BOX_CACHE[key]
    x0, y0, x1, y1 = img.width(), img.height(), -1, -1
    step = max(1, min(img.width(), img.height()) // 256)
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            if img.pixelColor(x, y).alpha() > thresh:
                x0, y0 = min(x0, x), min(y0, y)
                x1, y1 = max(x1, x), max(y1, y)
    if x1 < 0:
        return None
    box = QRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    if key:
        _BOX_CACHE[key] = box
    return box


def _png_icon(path: str, size: int = TITLE_ICON):
    """标题栏小图标：裁掉透明边，并按设备像素比渲染。

    素材（assets/DSniang1.png）是 610×610、内容偏左偏上且带透明边：
    直接 `scaled(20, 20)` 会又小又偏心，而且 20px 的位图在 1.5× 屏上会被二次放大 → 糊。
    这里按 `size*dpr` 物理像素渲染再标回 DPR，屏幕上是原生的清晰度。
    """
    img = QImage(path)
    if img.isNull():
        return None
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        stamp = 0
    box = _content_box(img, key=f"{path}:{stamp}")
    if box is not None:
        img = img.copy(box)
    dpr = 1.0
    scr = QApplication.primaryScreen()
    if scr is not None:
        dpr = max(1.0, float(scr.devicePixelRatio()))
    px = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    px.fill(Qt.transparent)
    inner = img.scaled(px.width(), px.height(), Qt.KeepAspectRatio,
                       Qt.SmoothTransformation)
    # 素材自带一层极淡的半透明底（原图几乎整幅 alpha>0）：缩到 20px 后会变成一小块灰底，
    # 这里在小图上把 alpha 很低的像素清成完全透明（20~30px 的图，开销可忽略）。
    inner = inner.convertToFormat(QImage.Format_ARGB32)
    for y in range(inner.height()):
        for x in range(inner.width()):
            a = inner.pixelColor(x, y).alpha()
            if 0 < a < 48:
                inner.setPixelColor(x, y, QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.drawImage(QPointF((px.width() - inner.width()) / 2.0,
                        (px.height() - inner.height()) / 2.0), inner)
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


# 控件微调：让原生 windows11 控件在透明卡片上看着协调
DIALOG_QSS = f"""
QLabel {{ color: {TEXT_COLOR}; }}
QLabel#hint {{ color: {HINT_COLOR}; }}
QLabel#glassTitle {{
    font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 12px;             /* Win11 标题栏：12px 常规字重，不是加粗大字 */
    font-weight: 400;
    color: {TEXT_COLOR};
}}
QToolButton#glassClose {{ border: none; border-radius: 6px; background: transparent; }}
QToolButton#glassClose:hover {{ background: rgba(224, 67, 63, 0.14); }}
QToolButton#glassClose:pressed {{ background: rgba(224, 67, 63, 0.24); }}
QListWidget, QTableWidget, QTreeWidget {{
    background: rgba(255, 255, 255, 0.55);
    border: 1px solid rgba(0, 0, 0, 0.10);
    border-radius: 8px;
}}
QListWidget::item {{ padding: 5px 8px; border-radius: 6px; }}
QListWidget::item:hover {{ background: rgba(0, 0, 0, 0.045); }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: rgba(59, 111, 212, 0.18);
    color: {TEXT_COLOR};
}}
QHeaderView::section {{
    background: rgba(255, 255, 255, 0.62);
    border: none;
    border-bottom: 1px solid rgba(0, 0, 0, 0.08);
    padding: 6px 8px;
    color: {TEXT_COLOR};
}}
QTableCornerButton::section {{ background: transparent; border: none; }}
QPlainTextEdit, QTextEdit {{
    background: rgba(255, 255, 255, 0.72);
    border: 1px solid rgba(0, 0, 0, 0.10);
    border-radius: 8px;
    padding: 6px;
    color: {TEXT_COLOR};
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(0, 0, 0, 0.18); border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(0, 0, 0, 0.30); }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(0, 0, 0, 0.18); border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: rgba(0, 0, 0, 0.30); }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
/* Win11 主按钮：强调色实心 —— 只有显式标了 objectName("primary") 的才染蓝
   （设置里的 OK、词典页的「保存」；Close 之类的次要按钮保持原生） */
QPushButton#primary {{
    background-color: {ACCENT};
    color: #ffffff;
    border: 1px solid rgba(0, 0, 0, 0.08);
    border-radius: 5px;
    padding: 5px 16px;
}}
QPushButton#primary:hover {{ background-color: #3262c0; }}
QPushButton#primary:pressed {{ background-color: #2a53a4; }}
QPushButton#primary:disabled {{ background-color: rgba(0, 0, 0, 0.12); color: #9fa4ab; }}
"""


class GlassDialog(QDialog):
    """W11 风格对话框基类：圆角卡片 + 磨砂玻璃 + 自绘标题栏。

    子类把内容加进 `self.body`（QVBoxLayout）即可；标题栏自带标题与关闭按钮，
    按住标题栏可拖动窗口。
    """

    def __init__(self, parent=None, title: str = "", *, icon_path: str | None = None,
                 radius: float = RADIUS, resizable: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)                       # 任务栏/无障碍仍保留标题
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._acrylic = False
        self._acrylic_hwnd = 0
        self._rounded = False
        self._placed = False
        self._radius = float(radius)
        self._drag_off: QPoint | None = None

        # ---- 标题栏（不套子 QWidget：QLabel 不吃鼠标事件，按下去就是拖动窗口）----
        self.title_label = QLabel(title or self.windowTitle())
        self.title_label.setObjectName("glassTitle")
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 9, 8, 0)
        bar.setSpacing(9)
        if icon_path:
            pm = _png_icon(icon_path)
            if pm is not None:
                ico = QLabel()
                ico.setPixmap(pm)
                ico.setFixedSize(TITLE_ICON, TITLE_ICON)
                bar.addWidget(ico)
        bar.addWidget(self.title_label)
        bar.addStretch(1)
        self.close_btn = QToolButton()
        self.close_btn.setObjectName("glassClose")
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFixedSize(30, 24)
        ic = glyph_icon(ICON_CLOSE, 13, "#3a3f47")
        if ic.isNull():
            self.close_btn.setText("✕")
        else:
            self.close_btn.setIcon(ic)
        self.close_btn.setToolTip("关闭")
        self.close_btn.clicked.connect(self.reject)
        bar.addWidget(self.close_btn)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(16, 8, 16, 14)
        self.body.setSpacing(10)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(bar)
        root.addSpacing(TITLE_H - 21)
        root.addLayout(self.body, 1)
        if resizable:
            grip_row = QHBoxLayout()
            grip_row.setContentsMargins(0, 0, 4, 4)
            grip_row.addStretch(1)
            grip = QSizeGrip(self)
            grip.setFixedSize(14, 14)
            grip_row.addWidget(grip, 0, Qt.AlignRight | Qt.AlignBottom)
            root.addLayout(grip_row)

        self.setStyleSheet(self.styleSheet() + DIALOG_QSS)

    # ---- 磨砂玻璃 ----
    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._placed:
            self._placed = True
            QTimer.singleShot(0, self._center_on_parent)
        # 每次显示都确认一遍：Qt 可能重建原生窗口（句柄变了亚克力就没了）
        QTimer.singleShot(0, self._apply_acrylic)

    def _center_on_parent(self):
        """无边框窗口自己居中：优先父窗口中心，并保证不超出可用区域。"""
        parent = self.parentWidget()
        scr = (parent.screen() if parent is not None else None) or QApplication.primaryScreen()
        if scr is None:
            return
        area = scr.availableGeometry()
        if parent is not None:
            pg = parent.window().frameGeometry()
            x = pg.center().x() - self.width() // 2
            y = pg.center().y() - self.height() // 2
        else:
            x = area.center().x() - self.width() // 2
            y = area.center().y() - self.height() // 2
        x = max(area.left() + 8, min(int(x), area.right() - self.width() - 8))
        y = max(area.top() + 8, min(int(y), area.bottom() - self.height() - 8))
        self.move(int(x), int(y))

    def _apply_acrylic(self):
        if not self.isVisible() or not acrylic_allowed():
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        if (self._acrylic and hwnd == self._acrylic_hwnd):
            return                        # 同一个窗口已经开过，不重复调用
        # 圆角交给 DWM（亚克力会把窗口矩形铺满，自己画的透明角会被它填掉）
        self._rounded = round_window_corners(hwnd)
        try:
            if self._rounded or ACRYLIC_WITHOUT_DWM_ROUND:
                self._acrylic = set_acrylic_window(hwnd, ACRYLIC_TINT)
            else:
                self._acrylic = False     # 保不住圆角就别开亚克力
        except Exception:
            self._acrylic = False
        self._acrylic_hwnd = hwnd if (self._acrylic or self._rounded) else 0
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, self._radius, self._radius)
        alpha = CARD_ALPHA_ACRYLIC if self._acrylic else CARD_ALPHA_PLAIN
        p.fillPath(path, QColor(*CARD_RGB, alpha))
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(QColor(*TOP_HIGHLIGHT), 1.0))
        p.drawLine(r.left() + self._radius, r.top() + 0.5,
                   r.right() - self._radius, r.top() + 0.5)
        p.setPen(QPen(QColor(*SEP_RGBA), 1.0))
        y = r.top() + TITLE_H + 0.5
        p.drawLine(r.left() + 1.0, y, r.right() - 1.0, y)
        p.restore()
        p.setPen(QPen(QColor(*BORDER_RGBA), 1.0))
        p.drawPath(path)
        p.end()

    # ---- 按住标题栏拖动 ----
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and ev.position().y() <= TITLE_H:
            self._drag_off = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_off is not None and (ev.buttons() & Qt.LeftButton):
            self.move(ev.globalPosition().toPoint() - self._drag_off)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_off = None
        super().mouseReleaseEvent(ev)
