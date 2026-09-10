"""
桌宠主窗口 —— 透明无边框置顶小鲸鱼。

移植 DeepSeek-Balance-Whale-Widget 的页面挂件（widget.js）到 PySide6：
- 气泡（SVG 几何）+ 三行文字（余额 / 今日已用 / 状态）
- 拖拽 + 四分之一吸附 + 左吸附水平镜像（文字反向保持可读）
- 按压 Q 弹（底边中心 origin）+ 音效（QMediaPlayer，可降级静音）
- 自适应轮询：基础 60s；既没中转调用、余额也没变化则每次 +100%（翻倍，上限 30 分钟）；
  有中转调用或余额与上次不一致就重置回 60s（点击可手动刷新）
- 余额变化时数字滚动动画 + 弹气泡
- 随机台词（加权 6 组，含 gif 动图），点击切换、5s 自动收起
- 每轮对话消耗泡泡（来自中转服务的 usage 统计，跨线程 signal）
- 每日账本：首次观测缓存当日资金初始值，每轮消耗立刻叠加，轮询同步后给 ±偏离值；
  按模型记录每日消耗并长期保留（账单对话框看最近 7 天 / 30 天）
- 预警：余额低于阈值 / 今日已用超过阈值时弹气泡 + 托盘通知
- 托盘 + 右键菜单（设置 / 账单 / 刷新 / 显示隐藏 / 退出）
"""
from __future__ import annotations

import itertools
import math
import os
import random
import re
import time
from pathlib import Path

from PySide6.QtCore import (
    QByteArray, QEasingCurve, QPoint, QRect, QRectF, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QMovie, QPainter, QPixmap,
)
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon, QWidget

from .balance import Ledger, fetch_balance, fetch_platform_usage, today_peak_now
from .config import ledger_path, legacy_ledger_paths, load_config, save_config

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    _HAS_AUDIO = True
except Exception:  # pragma: no cover
    _HAS_AUDIO = False

ASSETS = Path(__file__).resolve().parent.parent / "assets"

MIN_SCALE, MAX_SCALE = 0.6, 2.5
REFRESH_MS = 60_000            # 基础轮询间隔（有活动时保持这个节奏）
REFRESH_MAX_MS = 30 * 60_000   # 空闲降速的上限（30 分钟）
REFRESH_BACKOFF = 2.0          # 每次“无中转调用且余额没变”的轮询，间隔 +100%（翻倍）
ANIM_MS = 700
BUBBLE_MS = 5_000
ALERT_BUBBLE_MS = 8_000       # 预警气泡停留时长（比普通气泡久一点）
CLICK_SQ = 9

# 气泡出场/退场动画（尾巴先冒 → 主体回弹展开；收起时整体缩回、消散）
BUBBLE_IN_MS = 300            # 出现时间轴（透明度/分层进度，线性驱动）
BUBBLE_OUT_MS = 280           # 收起时长（缩回 + 消散）
POP_IN_MS = 360               # 出现时的缩放回弹（OutBack，轻微过冲）
TAIL_LEAD = 0.38              # 尾巴在全时间轴的前 38% 冒完（收起时最后 38% 才消失）
BODY_LAG = 0.34               # 主体从 34% 开始展开（收起时先缩）
POP_LOAD = 0.80               # 初始 / 收起末尾的气泡尺寸比例
TEXT_RISE = 14.0              # 文字淡入上浮距离（viewBox 单位）
COST_ROLL_MS = 320            # 连轮金额滚动时长
BUBBLE_COOLDOWN_MS = 550      # 气泡完全收起后的冷却：期间不再自动生成新气泡（防连续闪泡）

# 音效节流（防连点叠音）：实测音效时长 104~264ms
SOUND_MIN_GAP_MS = 90         # 两声之间最短间隔，更密的请求直接丢弃
SOUND_CLICK_COOLDOWN_MS = 300 # 连点冷却：一次按下起算，期间的重复点击不出声
SOUND_SWITCH_FADE_MS = 25     # 切换音效时旧声的淡出时长（交叉渡入，避免硬切爆音）

COLOR_TEXT = QColor("#536ba9")
COLOR_HINT = QColor("#9fb0d9")
COLOR_RED = QColor("#e0433f")
COLOR_GREEN = QColor("#2fa24c")

# CSS 数值字重 -> QFont.Weight 枚举（PySide6 不接受裸整数）
_WEIGHT_MAP = {
    400: QFont.Weight.Normal,
    500: QFont.Weight.Medium,
    600: QFont.Weight.DemiBold,
    700: QFont.Weight.Bold,
    800: QFont.Weight.ExtraBold,
    900: QFont.Weight.Black,
}


def _weight(n) -> QFont.Weight:
    return _WEIGHT_MAP.get(int(n), QFont.Weight.Normal)


# ===== 气泡文字排版参数（viewBox 1026×700 坐标系）=====
# 气泡内缘椭圆：轮廓圆心 (454,247)、半径 373×232，描边 18 各占一半
TEXT_CX_R, TEXT_CY_R = 454.0 / 1026.0, 0.38   # 文字块中心（相对气泡宽/高）
ELL_CY, ELL_RX, ELL_RY = 247.0, 364.0, 223.0   # 内缘椭圆中心 y / 半径
TEXT_PAD = 12.0            # 文字与内缘之间的安全间距
WRAP_W = 560.0             # 换行宽度（同原版 .dshwv-wrap 的 max-width）
MIN_WRAP_W = 448.0         # 换行宽度下限（防止超长文本越换越窄）
MIN_SHRINK = 0.75          # 单行超宽时字号最多缩到的比例
LINE_H = 1.2               # 多行行距（同原版 .dshwv-wrap line-height:1.2）
# 行首禁则：这些标点/空白不应出现在行首（换行时改为悬挂在上一行末尾）
_NO_LINE_START = ' \u3000，。、！？；：）】》」』〉·…—～%!?,.;:)]}"”’»'
# 行尾禁则：这些开引号/开括号不应出现在行尾
_NO_LINE_END = '（【《「『〈([{“‘«'
# 断行单元：西文单词/数字整体不拆、连续点号整体不拆，其余逐字断行
_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-+._:/]*|\.{2,}|[^A-Za-z0-9]")


_FONT_DIRS = [
    r"C:\Windows\Fonts\msyh.ttc",   # 微软雅黑（推荐）
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf", # 黑体
    r"C:\Windows\Fonts\simsun.ttc", # 宋体
]


def ensure_fonts() -> bool:
    """确保中文字体可用。

    部分 Windows 环境（精简/远程会话）下 Qt 无法枚举系统字体
    （QFontDatabase.families() 为空），直接 addApplicationFont 加载
    系统字体文件绕过。返回是否成功。
    """
    try:
        from PySide6.QtGui import QFontDatabase
        fams = QFontDatabase.families()
        if any("YaHei" in f or "SimHei" in f or "SimSun" in f for f in fams):
            return True
        for path in _FONT_DIRS:
            if not os.path.exists(path):
                continue
            fid = QFontDatabase.addApplicationFont(path)
            if fid >= 0 and QFontDatabase.applicationFontFamilies(fid):
                return True
    except Exception:
        pass
    return False


class Animator:
    """基于 QTimer 的补间动画（16ms 帧）。"""

    def __init__(self, on_update=None, on_finish=None, duration=300,
                 easing=QEasingCurve.OutCubic):
        self._timer = QTimer()
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.on_update = on_update
        self.on_finish = on_finish
        self.duration = duration
        self.easing = easing
        self.value = 0.0
        self._from = 0.0
        self._to = 0.0
        self._elapsed = 0

    def start(self, from_v, to_v, duration=None, easing=None):
        if duration is not None:
            self.duration = duration
        if easing is not None:
            self.easing = easing
        self._from, self._to = float(from_v), float(to_v)
        self._elapsed = 0
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _tick(self):
        self._elapsed += 16
        t = min(1.0, self._elapsed / max(1, self.duration))
        fn = QEasingCurve(self.easing)
        self.value = self._from + (self._to - self._from) * fn.valueForProgress(t)
        if self.on_update:
            self.on_update(self.value)
        if t >= 1.0:
            self._timer.stop()
            if self.on_finish:
                self.on_finish()


def _clamp01(v: float) -> float:
    return 0.0 if v <= 0.0 else (1.0 if v >= 1.0 else float(v))


def _smoothstep(v: float) -> float:
    """0→1 的缓入缓出（用于把线性时间轴映射成柔和的透明度曲线）。"""
    v = _clamp01(v)
    return v * v * (3.0 - 2.0 * v)


def _pop_curve(t: float) -> float:
    """气泡展开曲线：缓入缓出 + 中后段轻微过冲（「啵」地弹开的手感）。"""
    t = _clamp01(t)
    return _smoothstep(t) + 0.5 * math.sin(math.pi * t) * (t ** 1.5)


# 气泡几何：主体（大泡 + 嘴）与尾巴（两个小圆）拆开渲染，方便分层出场
# 出现时小尾巴先冒出来，主体随后「啵」地弹开；收起时主体先缩回、尾巴最后消失。
_BUBBLE_SVG_HEAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1026 700">'
    '<path fill="#FFFFFF" stroke="#203170" stroke-width="18" '
    'stroke-linejoin="round" stroke-linecap="round" '
    'd="M 827 248 A 373 232 0 1 0 81 246 A 373 232 0 0 0 301 465 '
    'A 57 32 10 0 0 413 484 A 373 232 0 0 0 827 248 Z"/></svg>'
)
_BUBBLE_SVG_TAIL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1026 700">'
    '<ellipse cx="352" cy="561" rx="37.5" ry="26" fill="#FFFFFF" '
    'stroke="#203170" stroke-width="18"/>'
    '<ellipse cx="442" cy="646" rx="24.5" ry="18" fill="#FFFFFF" '
    'stroke="#203170" stroke-width="18"/></svg>'
)


def _render_svg(svg: str) -> QPixmap:
    """用原版 SVG 几何预渲染（白色填充 + 深蓝描边）。"""
    try:
        from PySide6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pm = QPixmap(1026, 700)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        return pm
    except Exception:
        return QPixmap()


def _svg_bubble() -> tuple[QPixmap, QPixmap]:
    """返回 (主体, 尾巴) 两张气泡图。"""
    return _render_svg(_BUBBLE_SVG_HEAD), _render_svg(_BUBBLE_SVG_TAIL)


class WhaleWidget(QWidget):
    balance_updated = Signal(dict)
    turn_used = Signal(str, dict, float, int)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("DeepSeek 小鲸鱼")
        ensure_fonts()  # 某些环境 Qt 枚举不到系统字体，需显式加载
        self.cfg = load_config()

        # ----- 配置状态 -----
        self.scale = float(self.cfg.get("scale", 1.5))
        self.use_token_mode = self.cfg.get("usage_mode") == "token"
        self.peak_mode = self.cfg.get("peak_mode", "default")
        self.bubble_on = bool(self.cfg.get("bubble_on", True))
        self.turn_cost_on = bool(self.cfg.get("turn_cost_on", True))
        self.turn_cost_close_ms = float(self.cfg.get("turn_cost_close_ms", 5000))
        self.volume = float(self.cfg.get("volume", 0.9))
        self.sound_set = self.cfg.get("sound_set", "duck")
        # 预警：余额低于 / 今日已用超过阈值时提醒（开关关闭或阈值 <= 0 则不提醒）
        self.alert_balance_on = bool(self.cfg.get("alert_balance_on", True))
        self.alert_balance = float(self.cfg.get("alert_balance", 10.0))
        self.alert_daily_on = bool(self.cfg.get("alert_daily_on", True))
        self.alert_daily = float(self.cfg.get("alert_daily", 10.0))
        self._bal_alert_armed = True   # 跌破阈值只提醒一次，回到阈值上方再重新武装

        self.state = {
            "h": self.cfg.get("pos", {}).get("h") or "right",
            "v": self.cfg.get("pos", {}).get("v") or "bottom",
            "hOff": float(self.cfg.get("pos", {}).get("hOff", 0)),
            "vOff": float(self.cfg.get("pos", {}).get("vOff", 0)),
            "left": None, "top": None,
        }

        # ----- 余额状态 -----
        self.balance = None
        self.currency = "CNY"
        # 账本 = 磁盘上的共享缓存：启动即读取（不必等第一次轮询），
        # 旧位置的账本（项目目录 / 旧版 usage.json）一次性并入；
        # 之后每次轮询都会对比文件、合并外部改动（多实例也不会互相覆盖）。
        self.ledger = Ledger(ledger_path(), migrate_from=legacy_ledger_paths())
        self.today_usage = self.ledger.today_usage
        self.usage_dev = self.ledger.deviation
        self.status = "loading"
        self.message = ""
        self.shown = None
        self.busy = False

        # ----- 素材 -----
        self.img = QPixmap(str(ASSETS / "DSniang1.png"))
        if self.img.isNull():
            self.img = QPixmap(str(ASSETS / "DSniang02.png"))
        self.bubble_pm, self.bubble_tail_pm = _svg_bubble()
        self.movie = None
        self.gif_ok = False
        self._load_gif()

        # ----- 动画 -----
        self.bubble_t = 0.0           # 气泡总时间轴（分层进度 / 透明度）
        self.pop_t = 0.0              # 缩放时间轴（0=收拢，1=展开；出场带弹性映射）
        self._pop_in = True           # True=出场（走弹性曲线），False=退场（线性回缩）
        self.text_t = 0.0
        self.press_x = 1.0
        self.press_y = 1.0
        self.anim_bubble = Animator(self._set_bubble_t, duration=BUBBLE_IN_MS,
                                    easing=QEasingCurve.Linear)
        self.anim_pop = Animator(self._set_pop, duration=POP_IN_MS,
                                 easing=QEasingCurve.Linear)
        self.anim_text = Animator(self._set_text_t, duration=160,
                                  easing=QEasingCurve.OutCubic)
        self.anim_press = Animator(self._set_press, duration=220,
                                   easing=QEasingCurve.OutBack)
        self.anim_amount = Animator(self._set_amount, duration=ANIM_MS,
                                    easing=QEasingCurve.OutCubic)
        self._cost_value = 0.0        # 目标金额（上一轮消耗）
        self._cost_shown = 0.0        # 正在显示的金额（连轮更新时从旧值滚到新值）
        self.anim_cost = Animator(self._set_cost_shown, duration=COST_ROLL_MS,
                                  easing=QEasingCurve.OutCubic)
        self._token_value = 0         # 目标 token 数（上一轮总量）
        self._token_shown = 0.0       # 正在显示的 token 数（同样平滑滚动）
        self.anim_token = Animator(self._set_token_shown, duration=COST_ROLL_MS,
                                   easing=QEasingCurve.OutCubic)

        # ----- 气泡/消耗 -----
        self.bubble_shown = False
        self.bubble_random = False
        self.random_lines = None
        self._restore_lines = False   # 收起后待恢复余额内容（等文字透明时再切，避免闪）
        self._cost_lines = False      # 仍按“消耗”内容绘制（收起淡出期间保持）
        self._alert_lines = False     # 仍按“预警”内容绘制（同上）
        self.cost_bubble = False
        # 冷却截止（time.monotonic 口径）：气泡完全收起后的这段时间内不生成新气泡
        self._bubble_cool_until = 0.0
        self.bubble_timer = QTimer(self)
        self.bubble_timer.setSingleShot(True)
        self.bubble_timer.timeout.connect(self.hide_bubble)
        self.cost_timer = QTimer(self)
        self.cost_timer.setSingleShot(True)
        self.cost_timer.timeout.connect(self.hide_cost_bubble)
        # 内容切换（随机台词等）淡出淡入
        self._swap_timer = QTimer(self)
        self._swap_timer.setSingleShot(True)
        self._swap_timer.timeout.connect(self._swap_apply)
        self._swap_apply_fn = None
        # 音量渐变定时器（保存引用防 GC）
        self._fade_timers = []
        self._fade_done = set()
        self._text_delay = QTimer(self)
        self._text_delay.setSingleShot(True)
        self._text_delay.timeout.connect(self._text_goal)
        # 气泡完全收起后自动恢复余额内容（此刻切换不可见，避免 _alert_lines/_cost_lines
        # 残留——残留会让之后的点击、余额变动再也弹不出气泡）
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_after_hide)

        # ----- 音效 -----
        self._init_audio()

        # ----- 拖拽 -----
        self._drag = None

        # ----- 定时刷新（自适应节奏，见 _schedule_refresh）-----
        self.refresh_ms = REFRESH_MS       # 当前轮询间隔
        self._turn_since_poll = False      # 上次轮询之后有没有中转调用
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)   # 每次刷新完成后重新排期
        self.refresh_timer.timeout.connect(lambda: self.refresh_balance(False))
        self._schedule_refresh(True)

        # ----- 信号 -----
        self.balance_updated.connect(self._on_balance)
        self.turn_used.connect(self._on_turn_used)

        # ----- 几何 -----
        self._apply_scale_geometry()
        self._restore_position()

    # ================= 尺寸 / 位置 =================
    def base_px(self) -> int:
        """clamp(122, min(250, 0.28*min(sw,sh))*scale, 625)。"""
        scr = QApplication.primaryScreen()
        sw, sh = 1280, 800
        if scr:
            geo = scr.availableGeometry()
            sw, sh = geo.width(), geo.height()
        raw = min(250.0, min(sw, sh) * 0.28) * self.scale
        return int(max(122.0, min(625.0, raw)))

    def _apply_scale_geometry(self):
        b = self.base_px()
        self.setFixedSize(b, b)

    def _screen(self):
        scr = QApplication.primaryScreen()
        return scr.availableGeometry() if scr else None

    def _restore_position(self):
        b = self.base_px()
        geo = self._screen()
        if not geo:
            return
        vw, vh = geo.width(), geo.height()
        pos = self.cfg.get("pos", {})
        h, v = self.state["h"], self.state["v"]
        if h == "right":
            left = vw - b - self.state["hOff"]
        elif h == "left":
            left = self.state["hOff"]
        elif isinstance(pos.get("x"), (int, float)):
            left = float(pos["x"])
        else:
            left = vw - b
        if v == "bottom":
            top = vh - b - self.state["vOff"]
        elif v == "top":
            top = self.state["vOff"]
        elif isinstance(pos.get("y"), (int, float)):
            top = float(pos["y"])
        else:
            top = vh - b
        left = max(0, min(left, vw - b))
        top = max(0, min(top, vh - b))
        self.state["left"], self.state["top"] = left, top
        self.move(int(left), int(top))

    def _save_pos(self):
        b = self.base_px()
        geo = self._screen()
        if not geo:
            return
        vw, vh = geo.width(), geo.height()
        x, y = self.x(), self.y()
        left_dist, right_dist = x, vw - x - b
        top_dist, bottom_dist = y, vh - y - b
        h = "left" if left_dist <= right_dist else "right"
        v = "top" if top_dist <= bottom_dist else "bottom"
        self.state["h"], self.state["v"] = h, v
        self.state["hOff"] = round(min(left_dist, right_dist))
        self.state["vOff"] = round(min(top_dist, bottom_dist))
        self.cfg.setdefault("pos", {})
        self.cfg["pos"].update({
            "h": h, "v": v,
            "hOff": self.state["hOff"], "vOff": self.state["vOff"],
            "x": x, "y": y,
        })
        save_config(self.cfg)

    def _settle(self):
        b = self.base_px()
        geo = self._screen()
        if not geo:
            return
        vw, vh = geo.width(), geo.height()
        h, v = self.state["h"], self.state["v"]
        if h == "right":
            left = vw - b - self.state["hOff"]
        elif h == "left":
            left = self.state["hOff"]
        else:
            cur = self.state["left"] if self.state["left"] is not None else vw - b
            left = max(0, min(cur, vw - b))
        if v == "bottom":
            top = vh - b - self.state["vOff"]
        elif v == "top":
            top = self.state["vOff"]
        else:
            cur = self.state["top"] if self.state["top"] is not None else vh - b
            top = max(0, min(cur, vh - b))
        left = max(0, min(left, vw - b))
        top = max(0, min(top, vh - b))
        self.state["left"], self.state["top"] = left, top
        self.move(int(left), int(top))

    # ================= 绘制 =================
    def paintEvent(self, _ev):
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        flipped = self.state["h"] == "left"
        # 整体：水平镜像（中线）+ 按压缩放（底边中心 origin）
        p.save()
        p.translate(w / 2, h)
        p.scale((-1.0 if flipped else 1.0) * self.press_x, self.press_y)
        p.translate(-w / 2, -h)

        # 气泡：尾巴与主体分层出场（尾巴先冒 → 主体回弹展开；收起时反向）
        if self.bubble_t > 0.01:
            bw = w
            bh = w * 700 / 1026
            tail_a = _smoothstep(self.bubble_t / TAIL_LEAD)
            body_a = _smoothstep((self.bubble_t - BODY_LAG) / (1.0 - BODY_LAG))
            p.save()
            # 以尾根（贴近鲸鱼那侧）为锚点缩放：气泡像从鲸鱼头顶吹出来 / 缩回去
            sc = self._pop_scale()
            ax, ay = bw * 0.40, bh * 0.95
            p.translate(ax, ay)
            p.scale(sc, sc)
            p.translate(-ax, -ay)
            if body_a > 0.01:
                p.setOpacity(body_a)
                if not self.bubble_pm.isNull():
                    p.drawPixmap(QRect(0, 0, int(bw), int(bh)), self.bubble_pm)
                if self._gif_visible() and self.movie:
                    pm = self.movie.currentPixmap()
                    if not pm.isNull():
                        size = min(bw * 0.55, bh * 0.57)
                        gx, gy = bw * 0.4425, bh * 0.38
                        p.drawPixmap(QRect(int(gx - size / 2), int(gy - size / 2),
                                           int(size), int(size)), pm)
            if not self.bubble_tail_pm.isNull() and tail_a > 0.01:
                p.setOpacity(tail_a)
                p.drawPixmap(QRect(0, 0, int(bw), int(bh)), self.bubble_tail_pm)
            p.restore()

        # 文字
        self._draw_text(p, w, h)

        # 鲸鱼
        iw = w * 0.5945
        p.drawPixmap(QRect(int(w - iw), int(h - iw), int(iw), int(iw)), self.img)
        p.restore()
        p.end()

    def _draw_text(self, p, w, h):
        if self.text_t <= 0.01:
            return
        base = w / 1026.0
        # 文字相对气泡画布定位（气泡高 = w*700/1026，top 38% 相对气泡）
        bh = w * 700 / 1026
        tx, ty = w * TEXT_CX_R, bh * TEXT_CY_R
        p.save()
        p.setOpacity(self.text_t)
        if self.state["h"] == "left":
            p.translate(tx, ty)
            p.scale(-1, 1)
            p.translate(-tx, -ty)

        if self._cost_lines:
            labels = [("上一轮对话消耗:", COLOR_TEXT, 66, 600, False),
                      (self._fmt_amount(self._cost_shown, "CNY"), COLOR_RED, 128, 800, False)]
            tokens_line = self._fmt_tokens(self._token_shown)
            if tokens_line:
                labels.append((tokens_line, COLOR_HINT, 56, 400, False))
        elif self.bubble_random and isinstance(self.random_lines, list):
            labels = self._style_lines(self.random_lines)
        elif self.bubble_random and isinstance(self.random_lines, dict) and self.random_lines.get("gif"):
            labels = []
        else:
            labels = [
                ("DeepSeek 余额", COLOR_TEXT, 66, 600, False),
                (self._fmt_amount(self._amount_value(), self.currency), COLOR_TEXT, 128, 800, False),
                (self._hint_text(), COLOR_HINT, 56, 400, False),
            ]

        if labels:
            # 排版结果缓存：同一内容/尺寸下每帧重排代价无谓
            key = (w, tuple((t, c.name(), s, wt, wr)
                            for t, c, s, wt, wr in labels))
            cache = getattr(self, "_text_rows_cache", None)
            if cache is not None and cache[0] == key:
                rows = cache[1]
            else:
                rows = self._layout_rows(labels, base, ty)
                self._text_rows_cache = (key, rows)
            # 整块文字按真实高度垂直居中（长文本换行后仍居中，不再下坠）
            # 随透明度淡入上浮（出现时从下方浮起、消失时下沉），更有「冒出来」的感觉
            y = (ty + (_clamp01(1.0 - self.text_t) * TEXT_RISE * base)
                 - sum(r["h"] for r in rows) / 2.0)
            for r in rows:
                p.setFont(r["font"])
                p.setPen(r["color"])
                multi = len(r["lines"]) > 1
                for ln in r["lines"]:
                    lh = r["lh"] if multi else r["h"]
                    # 以 tx 为中心水平居中（drawText(QPoint,…) 是左对齐）
                    p.drawText(QRectF(tx - r["maxw"] / 2.0, y, r["maxw"], lh),
                               int(Qt.AlignHCenter | Qt.AlignVCenter), ln)
                    y += lh
        p.restore()

    # ---- 文字排版：换行 / 缩放 / 堆叠 ----
    def _layout_rows(self, labels, base, ty):
        """把三行标签排版成可绘制的行（含换行后的真实行高与位置）。

        原实现按“单行高度”估算整块高度，并对 w 标记的行只给 2 倍行高的绘制
        矩形：长文本换行后会整体下坠、第三行还会被矩形裁掉。这里改为
        ——先按气泡内缘椭圆的可用宽度换行，再按换行后的真实高度垂直居中。
        """
        rows = []
        for txt, color, size, weight, wrap in labels:
            f = QFont("Microsoft YaHei")
            f.setPixelSize(max(6, int(size * base)))
            f.setWeight(_weight(weight))
            rows.append({
                "txt": txt, "color": color, "font0": f, "font": f,
                "fm": QFontMetrics(f), "wrap": bool(wrap),
                "size": float(size) * base,
                "lh": float(size) * base * LINE_H,
                "h": float(size) * base * 1.15,
                "maxw": math.inf, "top": ty, "lines": [txt],
            })
        total = None
        for _ in range(4):
            # 换行 → 行块变高 → 可用宽度变窄，迭代到行块高度稳定（宽度只收窄）
            y = ty - sum(r["h"] for r in rows) / 2.0
            for r in rows:
                r["top"] = y
                limit = self._row_max_width(r, base)
                maxw = min(WRAP_W * base, limit) if r["wrap"] else limit
                r["maxw"] = min(r["maxw"], maxw)
                self._fit_row(r, base)
                y += r["h"]
            now = sum(r["h"] for r in rows)
            if now == total:
                break
            total = now
        return rows

    def _row_max_width(self, r, base):
        """该行可用宽度（px）：取行块上下边缘在气泡内缘椭圆内更窄的一侧。"""
        inset = max(0.0, (r["lh"] - 0.9 * r["size"]) / 2.0)   # 行距里的空白
        dy1 = r["top"] + inset - ELL_CY * base
        dy2 = r["top"] + r["h"] - inset - ELL_CY * base
        hw = min(self._ellipse_half_width(dy1, base),
                 self._ellipse_half_width(dy2, base))
        hw = max(hw - TEXT_PAD * base, MIN_WRAP_W * base / 2.0)
        return 2.0 * hw

    @staticmethod
    def _ellipse_half_width(dy, base):
        """气泡内缘椭圆在距中心 dy（px）处的半宽（px）。"""
        ry = ELL_RY * base
        if ry <= 0 or abs(dy) >= ry:
            return 0.0
        return ELL_RX * base * math.sqrt(1.0 - (dy / ry) ** 2)

    def _fit_row(self, r, base):
        """按 r['maxw'] 定该行的字号/换行，更新 font、lines、lh、h。"""
        txt, maxw, size = r["txt"], r["maxw"], r["size"]
        f0 = r["font0"]
        w0 = QFontMetrics(f0).horizontalAdvance(txt)
        k = 1.0
        if not r["wrap"] and w0 > maxw:
            k = max(MIN_SHRINK, maxw / max(1.0, w0))         # 单行超宽：优先缩字号
        max_h = 2.0 * (ELL_RY - 2.0 * TEXT_PAD) * base       # 单行块可用高度
        for attempt in range(4):
            f = QFont(f0)
            f.setPixelSize(max(6, int(round(f0.pixelSize() * k))))
            fm = QFontMetrics(f)
            w = fm.horizontalAdvance(txt)
            # 字号取整后仍超宽：只要还能缩就继续缩（缩到下限才改换行）
            if not r["wrap"] and w > maxw and k > MIN_SHRINK and attempt < 3:
                k = max(MIN_SHRINK, k * maxw / w)
                continue
            lines = self._wrap_lines(txt, fm, maxw) if (r["wrap"] or w > maxw) else [txt]
            lh = size * k * LINE_H
            # 行数太多导致整块超出气泡：再缩一档字号重排
            if (len(lines) > 1 and lh * len(lines) > max_h
                    and k > MIN_SHRINK * 0.6 and attempt < 3):
                k *= max(0.6, max_h / (lh * len(lines)))
                continue
            break
        r["font"], r["fm"] = f, fm
        r["lines"] = lines
        r["lh"] = lh
        # 多行按行距累加真实高度；单行保持原来的 1.15 倍槽高不变
        r["h"] = lh * len(lines) if len(lines) > 1 else size * 1.15

    def _wrap_lines(self, txt, fm, maxw):
        """断行：中文逐字、西文单词不拆，避开行首/行尾禁则，并使各行长度均匀。

        先贪心得到最少行数，再在行数不变的前提下穷举断点，取“最长行最短”
        的那组（避免贪心把「了！」这种尾字挤成孤行）。
        """
        if maxw <= 1 or not txt:
            return [txt]
        units, hard = [], False
        for u in _UNIT_RE.findall(txt):
            if len(u) > 1 and fm.horizontalAdvance(u) > maxw:
                parts = self._hard_break(u, fm, maxw)   # 超长单词强制拆
                hard = hard or len(parts) > 1
                units.extend(parts)
            else:
                units.append(u)
        widths = [float(fm.horizontalAdvance(u)) for u in units]
        starts = self._greedy_starts(units, widths, maxw)
        if not hard and len(starts) > 2:
            even = self._balanced_starts(units, widths, maxw, len(starts) - 1)
            if even:
                starts = even
        return ["".join(units[a:b]) for a, b in zip(starts, starts[1:])]

    @staticmethod
    def _greedy_starts(units, widths, maxw):
        """贪心断行，返回各行起始单元下标（末位为单元总数）。"""
        n = len(units)
        cum = [0.0]
        for w in widths:
            cum.append(cum[-1] + w)
        starts, cur = [0], 0
        for j in range(1, n):
            if cum[j] - cum[cur] + widths[j] <= maxw:
                continue                          # 再放一个单元也不超宽
            if units[j][0] in _NO_LINE_START:
                continue                          # 行首禁则：标点悬挂在行末
            if units[j - 1][-1] in _NO_LINE_END and j - cur > 1:
                starts.append(j - 1)              # 行尾禁则：开引号/开括号带到下一行
                cur = j - 1
            else:
                starts.append(j)
                cur = j
        starts.append(n)
        return starts

    @staticmethod
    def _balanced_starts(units, widths, maxw, n_lines):
        """穷举断点，取“最长行最短”的分行（行数不变，宽度上限可含标点悬挂）。"""
        n = len(units)
        if n < 3 or not 2 <= n_lines <= 4 or n > 24:
            return None
        cum = [0.0]
        for w in widths:
            cum.append(cum[-1] + w)

        def hang(b):
            """第 b 个单元作为行末时允许的悬挂宽度（标点可以冒出一点）。"""
            return widths[b] if units[b][0] in _NO_LINE_START else 0.0

        def ok_break(b):
            """允许在第 b 个单元前断行（行首/行尾禁则）。"""
            return (units[b][0] not in _NO_LINE_START
                    and units[b - 1][-1] not in _NO_LINE_END)

        best, best_key = None, None
        for cuts in itertools.combinations(range(1, n), n_lines - 1):
            if not all(ok_break(b) for b in cuts):
                continue
            bounds = (0,) + cuts + (n,)
            ws = [cum[b] - cum[a] for a, b in zip(bounds, bounds[1:])]
            if any(w > maxw + hang(b - 1) for w, b in zip(ws, bounds[1:])):
                continue
            key = (max(ws), -ws[0])      # 最长行最短；并列时首行更长
            if best_key is None or key < best_key:
                best, best_key = bounds, key
        return list(best) if best else None

    @staticmethod
    def _hard_break(unit, fm, maxw):
        """超长西文串按宽度强制切分。"""
        parts, s = [], unit
        while len(s) > 1 and fm.horizontalAdvance(s) > maxw:
            n = 1
            while n < len(s) and fm.horizontalAdvance(s[:n + 1]) <= maxw:
                n += 1
            parts.append(s[:n])
            s = s[n:]
        parts.append(s)
        return parts

    def _style_lines(self, lines):
        out = []
        for ln in lines or []:
            if not ln:
                continue
            st = ln.get("s", "A")
            if st == "B":
                out.append((ln.get("t", ""), COLOR_TEXT, 128, 800, ln.get("w", False)))
            elif st == "P":
                c = COLOR_RED if "e0433f" in str(ln.get("c", "")).lower() else COLOR_GREEN
                out.append((ln.get("t", ""), c, 104, 800, False))
            elif st == "C":
                out.append((ln.get("t", ""), COLOR_HINT, 56, 400, False))
            else:
                out.append((ln.get("t", ""), COLOR_TEXT, 66, 600, ln.get("w", False)))
        return out

    # ================= 内容 =================
    def _fmt_amount(self, v, currency):
        try:
            num = float(v)
        except (TypeError, ValueError):
            return "--"
        if not math.isfinite(num):
            return "--"
        if currency == "CNY":
            return "¥ " + f"{num:.2f}"
        return f"{num:.2f} {currency}"

    def _amount_value(self):
        return self.shown if self.shown is not None else self.balance

    @staticmethod
    def _fmt_tokens(v) -> str:
        """本轮 token 用量（单位 K）：如 12.3K tokens；无数据时返回空串。"""
        try:
            n = float(v)
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(n) or n <= 0:
            return ""
        return f"{n / 1000.0:.1f}K tokens"

    def _fmt_deviation(self) -> str:
        """轮询同步后的偏离值：+0.12 / -0.04。

        偏离值 = 余额算出的今日已用 - 本地每轮统计。没统计到任何一轮
        （比如没走小鲸鱼中转）时不显示，避免把整天消耗都当成“偏离”。
        """
        d = float(self.usage_dev or 0.0)
        if not math.isfinite(d) or abs(d) < 0.005:
            return ""
        try:
            if self.ledger.turn_cost <= 0:
                return ""
        except Exception:
            pass
        return f" ({'+' if d > 0 else '-'}{abs(d):.2f})"

    def _today_text(self) -> str:
        """“今日已用 ¥ x.xx (+偏离值)”。"""
        today = "--" if self.today_usage is None else \
            self._fmt_amount(self.today_usage, self.currency)
        return "今日已用 " + today + self._fmt_deviation()

    def _hint_text(self):
        if self.status == "error":
            return (self.message or "获取失败 · 点击重试")[:14]
        if self.balance is None:
            # 余额还没拉到：先把缓存里的“今日已用”显示出来（启动即读缓存）
            return self._today_text() if self.today_usage is not None else "加载中…"
        return self._today_text()

    # ================= 余额 =================
    def _schedule_refresh(self, active: bool | None = None):
        """排下一次轮询：True → 回到基础间隔；False → 空闲降速 +100%；None → 不变。

        自适应规则（少发无意义的请求）：
        - 上一次轮询既没有中转调用、余额也和上次一致 → 间隔翻倍
          （60s → 120s → 240s …，最长 REFRESH_MAX_MS）；
        - 有中转调用（说明正在用）或余额与上次不一致（说明有消耗） → 重置回 60s。
        """
        if active is True:
            self.refresh_ms = REFRESH_MS
        elif active is False:
            self.refresh_ms = min(REFRESH_MAX_MS,
                                  int(self.refresh_ms * REFRESH_BACKOFF))
        if self.refresh_timer.isActive():
            self.refresh_timer.stop()
        self.refresh_timer.start(int(self.refresh_ms))

    def refresh_balance(self, manual):
        if self.busy:
            self._schedule_refresh()          # 上一轮还没回来：保持节奏，稍后再试
            return
        if not self.cfg.get("api_key"):
            self.status = "error"
            self.message = "未配置 API Key"
            self._schedule_refresh()
            self.update()
            return
        self.busy = True
        if manual or self.balance is None:
            self.status = "loading"
            self.update()
        import threading
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        payload = {"ok": False, "transient": True, "error": "刷新异常"}
        try:
            api_key = self.cfg.get("api_key", "")
            api_base = self.cfg.get("api_base", "https://api.deepseek.com")
            payload = fetch_balance(api_key, api_base)
            if payload.get("ok"):
                # 每日账本：首次观测即缓存当日资金初始值；轮询同步偏离值
                led = self.ledger
                led.record(float(payload["totalBalance"]), str(payload["currency"]))
                payload["usageMode"] = "ledger"
                if self.use_token_mode and self.cfg.get("platform_token"):
                    u = fetch_platform_usage(self.cfg["platform_token"])
                    if u.get("amount") is not None:
                        led.sync_external(u["amount"], u.get("models"))
                        payload["usageMode"] = "token"
                payload["todayUsage"] = led.today_usage
                payload["todayDeviation"] = led.deviation
                payload["isPeak"] = today_peak_now()
        except Exception as e:
            # 兜底：异常也要回到 GUI 线程，否则轮询不再排期
            payload = {"ok": False, "transient": True, "error": f"刷新异常: {e}"}
        finally:
            self.busy = False
        self.balance_updated.emit(payload)

    def _on_balance(self, payload):
        if not payload.get("ok"):
            if payload.get("transient") and self.balance is not None:
                self.status = "ok"
                self.message = ""
            else:
                self.status = "error"
                self.message = str(payload.get("error") or "获取失败")
            self._schedule_refresh()          # 失败：保持当前节奏
            self.update()
            return
        nb = float(payload["totalBalance"])
        nc = str(payload.get("currency") or "CNY")
        changed = self.balance is not None and (nb != self.balance or nc != self.currency)
        currency_changed = self.currency is not None and nc != self.currency
        # 自适应轮询：有中转调用或余额与上次不一致 → 重置；两者都没有 → 空闲降速
        active = bool(changed or self._turn_since_poll)
        self._turn_since_poll = False
        self._schedule_refresh(active)
        self.balance = nb
        self.currency = nc
        self.message = ""
        self.today_usage = payload.get("todayUsage")
        dev = payload.get("todayDeviation")
        self.usage_dev = float(dev) if isinstance(dev, (int, float)) else 0.0
        self.status = "ok"
        self._check_daily_alert()
        self._check_balance_alert()
        if (changed and not currency_changed
                and not (self.cost_bubble or self._alert_lines)):
            self.show_bubble()
            self._animate_amount(self._amount_value(), nb)
        else:
            self.shown = nb
            self.update()

    def _animate_amount(self, from_v, to_v):
        if self.cost_bubble:
            return
        try:
            f0 = float(from_v) if from_v is not None else float(to_v)
        except (TypeError, ValueError):
            f0 = float(to_v)
        self.anim_amount.start(f0, float(to_v))

    def _set_amount(self, v):
        self.shown = v
        self.update()

    # ================= 气泡 =================
    def _bubble_exists(self) -> bool:
        """气泡是否“存在”：显示中，或还在弹出/收起的动画里（存在时只延长，不重播）。"""
        return self.bubble_shown or self.bubble_t > 0.01

    def show_bubble(self, force=False):
        if not self.bubble_on or self.cost_bubble:
            return
        exists = self._bubble_exists()
        # 冷却：刚收起的一小段时间内不再自动生成气泡（避免“消失→立刻又冒出来”）
        if not exists and not force and time.monotonic() < self._bubble_cool_until:
            return
        if self.bubble_timer.isActive():
            self.bubble_timer.stop()
        self._restore_timer.stop()
        self._bubble_cool_until = 0.0
        self.bubble_shown = True
        # 切回余额内容：文字已全透明时可以直接切（预警/台词/计费内容一并清掉）；
        # 若还在淡出（快速连点重开），直接切会看到“余额闪一下”，
        # 于是推迟到 _text_goal（那时已透明）。
        if self.text_t <= 0.01:
            self._restore_balance_lines()
        else:
            self._restore_lines = True
        # 已经显示着就别从 0 重播（否则余额刷新时气泡会闪一下），只把停留时间延长；
        # 只有新气泡 / 收起中重开才重新安排文字出现（正在播的延迟不重置）
        self._pop_open()
        if not exists or not self._text_delay.isActive():
            self._text_delay.start(420)   # 文字等气泡形状展开得差不多再出现
        self.bubble_timer.start(BUBBLE_MS)

    def _pop_open(self):
        """气泡出现：从头弹出（尾巴先冒 → 主体回弹展开），已显示则补到完全展开。"""
        if self.bubble_t < 0.05:
            self._pop_in = True
            self.anim_bubble.start(0.0, 1.0, BUBBLE_IN_MS, QEasingCurve.Linear)
            self.anim_pop.start(0.0, 1.0, POP_IN_MS, QEasingCurve.Linear)
        else:
            if self.bubble_t < 1.0:
                self.anim_bubble.start(self.bubble_t, 1.0, 160, QEasingCurve.OutCubic)
            if self.pop_t < 1.0:
                self._pop_in = True
                self.anim_pop.start(self.pop_t, 1.0, 240, QEasingCurve.Linear)

    def _pop_close(self):
        """气泡收起：缩回（透明度先慢后快地消散，尾巴最后消失）。"""
        self._pop_in = False
        self.anim_bubble.start(self.bubble_t, 0.0, BUBBLE_OUT_MS, QEasingCurve.InCubic)
        self.anim_pop.start(self.pop_t, 0.0, BUBBLE_OUT_MS, QEasingCurve.InOutSine)

    def _restore_balance_lines(self):
        """恢复成余额内容（只在文字全透明时调用，否则会看到切换过程）。"""
        self._restore_lines = False
        self._cost_lines = False
        self._alert_lines = False
        self.bubble_random = False
        self.random_lines = None
        self._stop_gif()

    def _restore_after_hide(self):
        """气泡已完全收起：落地待恢复的余额内容（清掉预警/计费/台词残留标记）。"""
        if self._restore_lines and not self.bubble_shown and self.text_t <= 0.01:
            self._restore_balance_lines()

    def _text_goal(self):
        # 当前显示的是不是别的文字（计费/预警/台词）——恢复后内容会变
        had_other = self._cost_lines or self._alert_lines or self.bubble_random
        if self._restore_lines:
            if had_other and self.text_t > 0.5:
                # 别的文字还完整显示着（如台词被余额刷新打断）：先淡出再切，别硬切
                self._swap_bubble_content(self._restore_balance_lines)
                return
            # 文字已（快）透明：直接恢复余额内容不会闪
            self._restore_balance_lines()
        # 内容被切回余额、或文字还没显示完 → 淡入；已完整显示的余额内容不重播淡入
        if (self.bubble_shown and not self.cost_bubble
                and (had_other or self.text_t < 0.99)):
            self.anim_text.start(0.0, 1.0, 230, QEasingCurve.OutCubic)

    def hide_bubble(self):
        self.bubble_timer.stop()
        self._text_delay.stop()
        self._swap_timer.stop()          # 取消未生效的内容切换
        self._swap_apply_fn = None
        if self.cost_bubble:
            self.cost_bubble = False
            self.cost_timer.stop()
        self.bubble_shown = False
        # 保留当前台词（不清 random_lines）：让气泡带着原文字自然淡出。
        # 否则收起瞬间文字会先变回余额内容，看起来就是“收起到一半闪一下”。
        self._restore_lines = True
        self._pop_close()
        self.anim_text.start(self.text_t, 0.0, 170, QEasingCurve.InCubic)
        # 淡出收尾后再恢复内容：此刻画面看不见，不会闪
        self._restore_timer.start(BUBBLE_OUT_MS + 80)
        # 收起动画 + 冷却：这段时间内不再生成新气泡（短时间不会连续冒泡）
        self._bubble_cool_until = (time.monotonic()
                                   + (BUBBLE_OUT_MS + BUBBLE_COOLDOWN_MS) / 1000.0)
        self._stop_gif()

    def _set_bubble_t(self, v):
        self.bubble_t = v
        self.update()

    def _set_pop(self, v):
        self.pop_t = v
        self.update()

    def _pop_scale(self) -> float:
        """当前气泡缩放：出场沿弹性曲线（带轻微过冲），收起时线性回缩。"""
        t = _clamp01(self.pop_t)
        if self._pop_in:
            t = _pop_curve(t)
        return POP_LOAD + (1.0 - POP_LOAD) * t

    def _set_text_t(self, v):
        self.text_t = v
        self.update()

    def _set_press(self, v):
        self.press_x = 1.0 + 0.05 * v
        self.press_y = 1.0 - 0.12 * v
        self.update()

    # ================= 随机台词 =================
    def _pick_random(self):
        groups = [
            (45, self._group_peak),
            (7, lambda: [{"t": random.choice(["好模型... ↓", "好女孩...↓"]), "s": "B"}]),
            (7, lambda: [{"t": random.choice([
                "不知道用户有什么用，先赶走吧~", "我...我...我也要挣钱吗？",
                "我去吃饭啦，测完叫我", "压力一只蓝色大肥鱼？！",
                "DeepSleep...", "坏了...用户彻底怒了！"]), "s": "A", "w": True}]),
            (10, lambda: {"gif": True}),
            (3, lambda: [{"t": random.choice([
                "你目录里的dsh是什么...大烧货吗...?",
                "恭喜你实现token自由！token全跑了！",
                "真当我是便宜货啊..."]), "s": "A", "w": True}]),
            (1, lambda: [{"t": "哦鲸鲸... ", "s": "B"}]),
        ]
        total = sum(g[0] for g in groups)
        r = random.random() * total
        for weight, fn in groups:
            r -= weight
            if r < 0:
                return fn()
        return groups[-1][1]()

    def _group_peak(self):
        peak = today_peak_now()
        off_text, peak_text = "空闲时段", "高峰时段"
        if self.peak_mode == "liangwen":
            off_text, peak_text = "梁文谷", "梁文峰"
        elif self.peak_mode == "qiangqiang":
            off_text, peak_text = "!?谷谷?!", "!?峰峰?!"
        return [
            {"t": "当前时间段为:", "s": "A"},
            {"t": peak_text if peak else off_text, "s": "P",
             "c": "#e0433f" if peak else "#2fa24c"},
            {"t": self._today_text(), "s": "C"},
        ]

    # ================= gif =================
    def _load_gif(self):
        g = ASSETS / "rua.gif"
        if not g.exists():
            return
        self.movie = QMovie(str(g))
        if self.movie.isValid():
            self.movie.frameChanged.connect(lambda _f: self.update())
            self.gif_ok = True

    def _gif_visible(self):
        # 收起时仍要画（随气泡淡出）；只有“重开气泡但台词还没恢复”这段窗口不画，
        # 否则重开瞬间会闪出上一帧动图。
        return bool(self.gif_ok and self.movie
                    and not (self._restore_lines and self.bubble_shown)
                    and self.bubble_random
                    and isinstance(self.random_lines, dict)
                    and self.random_lines.get("gif"))

    def _stop_gif(self):
        if self.movie and self.movie.state() == QMovie.Running:
            self.movie.stop()

    # ================= 消耗泡泡 =================
    def _on_turn_used(self, model, usage, cost, tokens):
        # 有中转调用 = 正在使用：轮询节奏重置回基础间隔（空闲降速的逆操作），
        # 避免“刚开始用却要等下一次慢轮询”的观感
        self._turn_since_poll = True
        if self.refresh_ms > REFRESH_MS and self.refresh_timer.isActive():
            self._schedule_refresh(True)
        # 每轮消耗立刻叠到“今日已用”并按模型记账（不等轮询；同步时再给 ±偏离值）
        try:
            self.today_usage = self.ledger.add_turn_cost(float(cost), model, tokens)
            self.update()
        except Exception:
            pass
        if self.turn_cost_on:
            self.show_cost_bubble(cost, tokens)
        self._check_daily_alert()

    def _apply_cost_lines(self):
        """把气泡内容切到“上一轮消耗”（可作为 _swap_bubble_content 的回调）。"""
        self._cost_lines = True
        self._alert_lines = False
        self.bubble_random = False
        self.random_lines = None
        self._stop_gif()

    def show_cost_bubble(self, amount, tokens=0, force=False):
        if not self.bubble_on or not self.turn_cost_on:
            return
        exists = self._bubble_exists()
        # 冷却：气泡刚收起的一小段时间内不再自动生成（防“刚消失又冒出来”）
        if not exists and not force and time.monotonic() < self._bubble_cool_until:
            return
        self.cost_bubble = True
        self.cost_timer.stop()
        self.bubble_timer.stop()         # 普通气泡的自动关闭计时不再适用
        self._text_delay.stop()
        self._bubble_cool_until = 0.0
        self._cost_value = float(amount)
        try:
            self._token_value = max(0, int(tokens or 0))
        except (TypeError, ValueError):
            self._token_value = 0
        self.bubble_shown = True
        self._restore_lines = False      # 内容由消耗泡泡接管，无需再恢复
        if self._cost_lines:
            # 连轮触发：气泡已在显示计费内容 → 金额/token 从当前值滚到新值（不硬切），
            # 气泡/文字只补到完全显示并延长停留，不重播任何生成动画。
            self._swap_timer.stop()
            self._swap_apply_fn = None
            self._roll_cost()
            self._pop_open()
            if self.text_t < 0.99:
                self.anim_text.start(self.text_t if self.text_t > 0.01 else 0.0,
                                     1.0, 200, QEasingCurve.OutCubic)
        else:
            self._cost_shown = self._cost_value    # 首次显示消耗：直接显示目标值
            self._token_shown = float(self._token_value)
            self.anim_cost.stop()
            self.anim_token.stop()
            if not exists:
                # 气泡不在：正常弹出
                self._apply_cost_lines()
                self._pop_open()
                self.anim_text.start(0.0, 1.0, 220, QEasingCurve.OutCubic)
            elif self.bubble_t < 0.999 or self.pop_t < 0.999:
                # 与轮询气泡同时触发（气泡还在出场动画里）：直接改文本、把气泡补到
                # 完全展开，不重播生成动画（否则看起来就是“又快速生成了一个气泡”）
                self._apply_cost_lines()
                self._pop_open()
                if self.text_t < 0.99:
                    self.anim_text.start(self.text_t if self.text_t > 0.01 else 0.0,
                                         1.0, 200, QEasingCurve.OutCubic)
            else:
                # 稳定显示中：形状不动，内容先淡出再切（更柔和）
                self._pop_open()
                self._swap_bubble_content(self._apply_cost_lines)
        if self.turn_cost_close_ms > 0:
            self.cost_timer.start(int(self.turn_cost_close_ms))
        self.update()

    def _roll_cost(self):
        """连轮切换：金额与 token 都从正在显示的值平滑滚到新值（差得越多滚得越久）。"""
        self._roll_shown(self.anim_cost, "_cost_shown", self._cost_value, 0.005, 0.05)
        self._roll_shown(self.anim_token, "_token_shown", float(self._token_value), 0.5, 50.0)

    def _roll_shown(self, anim, attr, target, min_delta, big_delta):
        """用动画把某个展示值滚到目标（差值很小时直接落值，不白跑动画）。"""
        cur = float(getattr(self, attr))
        target = float(target)
        delta = abs(target - cur)
        if delta < min_delta:
            anim.stop()
            setattr(self, attr, target)
        else:
            dur = COST_ROLL_MS if delta >= big_delta else max(160, int(COST_ROLL_MS * 0.6))
            anim.start(cur, target, dur, QEasingCurve.OutCubic)
        self.update()

    def _set_cost_shown(self, v):
        self._cost_shown = v
        self.update()

    def _set_token_shown(self, v):
        self._token_shown = v
        self.update()

    def hide_cost_bubble(self):
        self.cost_timer.stop()
        self.cost_bubble = False
        # _cost_lines 保留到文字透明后再恢复，否则收起淡出时会闪回余额内容
        self.hide_bubble()

    # ================= 预警 =================
    def _check_balance_alert(self):
        """余额预警：跌破阈值提醒一次；回到阈值上方后重新武装。"""
        if not self.alert_balance_on or self.alert_balance <= 0 or self.balance is None:
            return
        try:
            b = float(self.balance)
        except (TypeError, ValueError):
            return
        if not math.isfinite(b):
            return
        if b >= self.alert_balance:
            self._bal_alert_armed = True
            return
        if not self._bal_alert_armed:
            return
        self._bal_alert_armed = False
        line = self._fmt_amount(b, self.currency)
        warn = "预警线 " + self._fmt_amount(self.alert_balance, self.currency)
        self.fire_alert(
            "余额预警",
            f"余额不足：{line}（{warn}）",
            [{"t": "余额预警", "s": "A"},
             {"t": line, "s": "P", "c": "#e0433f"},
             {"t": "低于" + warn, "s": "C"}],
        )

    def _check_daily_alert(self):
        """每日使用预警：今日已用每超过一个阈值档位提醒一次（重启后不重复）。"""
        if not self.alert_daily_on or self.alert_daily <= 0 or self.today_usage is None:
            return
        try:
            u = float(self.today_usage)
        except (TypeError, ValueError):
            return
        if not math.isfinite(u) or u < self.alert_daily:
            return
        level = int(u // self.alert_daily)
        if level <= self.ledger.alert_level:
            return
        self.ledger.set_alert_level(level)      # 先落盘：避免重启后同一档位再提醒
        amount = self._fmt_amount(u, self.currency)
        warn = self._fmt_amount(self.alert_daily, self.currency)
        tip = f"超过预警线 {warn}" + (f"（{level} 倍）" if level > 1 else "")
        self.fire_alert(
            "每日使用预警",
            f"今日已用 {amount}，{tip}",
            [{"t": "每日使用预警", "s": "A"},
             {"t": "今日已用 " + amount, "s": "P", "c": "#e0433f"},
             {"t": tip, "s": "C"}],
        )

    def fire_alert(self, title: str, message: str, lines=None):
        """预警提醒：托盘系统通知 + 气泡（两者都不可用时至少保留一个）。"""
        try:
            tray = getattr(self, "tray", None)
            if tray is not None and tray.isVisible():
                tray.showMessage(title, message, QSystemTrayIcon.Warning, 8000)
        except Exception:
            pass
        if self.bubble_on and lines:
            self.show_alert_bubble(lines)

    def show_alert_bubble(self, lines, hold_ms: int = ALERT_BUBBLE_MS):
        """切到预警内容并停留一段时间（不受普通气泡 5s 收起限制；预警无视气泡冷却）。"""
        self.cost_timer.stop()
        self.cost_bubble = False
        self.bubble_timer.stop()
        self._text_delay.stop()       # 别让上次弹气泡残留的文字延迟重播淡入
        self._bubble_cool_until = 0.0
        self._restore_lines = False
        self.bubble_shown = True
        self._alert_hold_ms = int(hold_ms)      # 切换内容完成后按这个时长停留
        if self.text_t <= 0.01:
            self._apply_alert_lines(lines)
            self._pop_open()
            self.anim_text.start(0.0, 1.0, 230, QEasingCurve.OutCubic)
        else:
            self._swap_bubble_content(lambda: self._apply_alert_lines(lines))
        self.bubble_timer.start(int(hold_ms))
        self.update()

    def _apply_alert_lines(self, lines):
        """把气泡内容切到预警（可作为 _swap_bubble_content 的回调）。"""
        self._cost_lines = False
        self._alert_lines = True
        self.bubble_random = True     # 复用台词绘制路径：点击即收起
        self.random_lines = lines
        self._stop_gif()

    # ================= 鼠标 =================
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        self._drag = {"start": ev.globalPosition().toPoint(), "orig": self.pos(),
                      "moved": False}
        self._press_down()
        ev.accept()

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return
        delta = ev.globalPosition().toPoint() - self._drag["start"]
        if delta.x() * delta.x() + delta.y() * delta.y() >= CLICK_SQ:
            self._drag["moved"] = True
        b = self.base_px()
        geo = self._screen()
        if not geo:
            return
        nx = max(0, min(self._drag["orig"].x() + delta.x(), geo.width() - b))
        ny = max(0, min(self._drag["orig"].y() + delta.y(), geo.height() - b))
        self.move(int(nx), int(ny))
        self.state["left"], self.state["top"] = nx, ny
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if not self._drag:
            return
        d, self._drag = self._drag, None
        self._press_up()
        if not d["moved"]:
            self._on_click()
        else:
            self._snap_after_drag()
            self._save_pos()
        ev.accept()

    def _on_click(self):
        if self.cost_bubble:
            self.hide_cost_bubble()
            return
        if not self.bubble_shown:
            self.show_bubble(force=True)   # 用户主动点击：无视冷却
            self.refresh_balance(True)
            return
        if self.bubble_random:
            self.hide_bubble()
            return
        # 随机台词：先淡出，再应用内容，后淡入（更柔和）
        r = self._pick_random()
        self._swap_bubble_content(lambda: self._apply_random_lines(r))

    def _apply_random_lines(self, lines):
        """把气泡内容切到随机台词（可作为 _swap_bubble_content 的回调）。"""
        self._cost_lines = False
        self._alert_lines = False
        self.bubble_random = True
        self.random_lines = lines
        if isinstance(lines, dict) and lines.get("gif"):
            if self.gif_ok and self.movie:
                self.movie.start()
        else:
            self._stop_gif()

    def _swap_bubble_content(self, apply):
        """内容切换淡出淡入：fade out 150ms → 应用 → fade in 220ms。"""
        self._restore_lines = False      # 用户主动切内容，取消待恢复的余额内容
        self._swap_apply_fn = apply
        self.anim_text.start(self.text_t, 0.0, 150, QEasingCurve.InOutSine)
        self._swap_timer.start(170)

    def _swap_apply(self):
        apply, self._swap_apply_fn = self._swap_apply_fn, None
        if apply is None:
            return
        apply()
        # 重置自动关闭计时：消耗泡泡按设置，预警按自己的停留时长，普通气泡 5s
        if self.cost_bubble:
            if self.turn_cost_close_ms > 0:
                self.cost_timer.start(int(self.turn_cost_close_ms))
        elif self._alert_lines:
            self.bubble_timer.start(int(getattr(self, "_alert_hold_ms", ALERT_BUBBLE_MS)))
        else:
            self.bubble_timer.start(BUBBLE_MS)
        self.anim_text.start(0.0, 1.0, 230, QEasingCurve.OutCubic)
        self.update()

    def _snap_after_drag(self):
        b = self.base_px()
        geo = self._screen()
        if not geo:
            return
        vw, vh = geo.width(), geo.height()
        x, y = self.x(), self.y()
        cx, cy = x + b / 2, y + b / 2
        if cx < vw / 4:
            self.state["h"], self.state["hOff"] = "left", 0
            x = 0
        elif cx > vw * 3 / 4:
            self.state["h"], self.state["hOff"] = "right", 0
            x = vw - b
        else:
            self.state["h"] = None
            self.state["hOff"] = x
        if cy < vh / 4:
            self.state["v"], self.state["vOff"] = "top", 0
            y = 0
        elif cy > vh * 3 / 4:
            self.state["v"], self.state["vOff"] = "bottom", 0
            y = vh - b
        else:
            self.state["v"] = None
            self.state["vOff"] = y
        self.state["left"], self.state["top"] = x, y
        self.move(int(x), int(y))

    # ================= 按压 / 音效 =================
    def _press_down(self):
        self.anim_press.start(self.anim_press.value, 1.0, duration=90,
                              easing=QEasingCurve.OutCubic)
        self._play("press")

    def _press_up(self):
        self.anim_press.start(self.anim_press.value, 0.0, duration=240,
                              easing=QEasingCurve.OutBack)
        self._play("release")

    def _init_audio(self):
        # 音效节流状态（单调时钟，单位 ms）——快速连点时防止两声叠在一起
        self._sfx_at = -1e9        # 上次实际发声时刻
        self._sfx_until = -1e9     # 连点冷却截止时刻
        self._sfx_muted = False    # 本次点击是否已被冷却吞掉（回弹音一并吞掉）
        self._players = {}
        if not _HAS_AUDIO:
            return
        try:
            SOUND_FILES = {"duck": {"press": "Ya1.mp3", "release": "Ya2.mp3"},
                           "fx1": {"press": "D1.mp3", "release": "D2.mp3"}}
            for name, kinds in SOUND_FILES.items():
                for kind, fname in kinds.items():
                    player = QMediaPlayer()
                    out = QAudioOutput()
                    player.setAudioOutput(out)
                    out.setVolume(self.volume)
                    player.setSource(QUrl.fromLocalFile(str(ASSETS / fname)))
                    # 接近播放末尾时淡出，避免骤停爆音
                    player.positionChanged.connect(
                        lambda pos, p=player, o=out: self._maybe_fade_out(p, o, pos))
                    self._players[(name, kind)] = (player, out)
        except Exception:
            self._players = {}

    def _maybe_fade_out(self, player, out, pos):
        dur = player.duration()
        key = id(player)
        if dur <= 60 or key in self._fade_done:
            return
        if pos > dur - 45:
            self._fade_done.add(key)
            self._tween_volume(out, float(out.volume()), 0.0, 45)

    def _tween_volume(self, out, from_v, to_v, dur_ms):
        """音量缓动（快速淡入/淡出）。"""
        steps = max(1, int(dur_ms / 16))
        timer = QTimer()
        timer.setInterval(16)
        state = {"i": 0}

        def step():
            state["i"] += 1
            k = state["i"] / steps
            if k >= 1.0:
                out.setVolume(to_v)
                timer.stop()
                try:
                    self._fade_timers.remove(timer)
                except ValueError:
                    pass
            else:
                out.setVolume(from_v + (to_v - from_v) * k)

        timer.timeout.connect(step)
        self._fade_timers.append(timer)
        timer.start()

    def _play(self, kind):
        """播放按压/回弹音效。

        按下与回弹是两套播放器，快速连点时会同时发声（叠音）。这里做三重节流：
        - 连点冷却 SOUND_CLICK_COOLDOWN_MS：冷却期内的点击整体静音（含回弹音）；
        - 最短间隔 SOUND_MIN_GAP_MS：距上次发声太近的请求丢弃；
        - 单声道：发新声前把仍在响的旧声快速淡出后停掉（约 25ms 交叉渡入）。
        """
        if not self._players or self.volume <= 0:
            return
        entry = self._players.get((self.sound_set, kind))
        if not entry:
            return
        now = time.monotonic() * 1000.0
        if kind == "press":
            if now < self._sfx_until:
                self._sfx_muted = True      # 连点冷却：本轮点击不出声
                return
            self._sfx_muted = False
            self._sfx_until = now + SOUND_CLICK_COOLDOWN_MS
        elif self._sfx_muted:
            self._sfx_muted = False         # 与静音按下配对的回弹音也吞掉
            return
        if now - self._sfx_at < SOUND_MIN_GAP_MS:
            return                          # 距上一声太近：丢弃
        player, out = entry
        try:
            # 单声道：仍在响的旧声淡出并停掉，不让两声叠在一起
            for other, other_out in self._players.values():
                if other is player or other.playbackState() != QMediaPlayer.PlayingState:
                    continue
                self._tween_volume(other_out, float(other_out.volume()), 0.0,
                                   SOUND_SWITCH_FADE_MS)
                QTimer.singleShot(SOUND_SWITCH_FADE_MS, other.stop)
            player.stop()
            self._fade_done.discard(id(player))
            self._sfx_at = now
            # 快速淡入（约 40ms），避免突兀起音
            out.setVolume(0.0)
            player.play()
            self._tween_volume(out, 0.0, self.volume, 40)
        except Exception:
            pass

    # ================= 菜单 / 托盘 =================
    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        act_settings = menu.addAction("设置…")
        act_stats = menu.addAction("账单…")
        act_refresh = menu.addAction("刷新余额")
        act_toggle = menu.addAction("隐藏" if self.isVisible() else "显示")
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act = menu.exec(ev.globalPos())
        if act == act_settings:
            self.open_settings()
        elif act == act_stats:
            self.open_stats()
        elif act == act_refresh:
            self.refresh_balance(True)
        elif act == act_toggle:
            self.setVisible(not self.isVisible())
        elif act == act_quit:
            QApplication.quit()

    def setup_tray(self):
        icon = QIcon(self.img) if not self.img.isNull() else \
            QApplication.style().standardIcon(QStyle.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("DeepSeek 小鲸鱼")
        menu = QMenu()
        act_settings = menu.addAction("设置…")
        act_stats = menu.addAction("账单…")
        act_refresh = menu.addAction("刷新余额")
        act_show = menu.addAction("显示/隐藏")
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_settings.triggered.connect(self.open_settings)
        act_stats.triggered.connect(self.open_stats)
        act_refresh.triggered.connect(lambda: self.refresh_balance(True))
        act_show.triggered.connect(lambda: self.setVisible(not self.isVisible()))
        act_quit.triggered.connect(QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.show()
        self._tray_menu = menu

    def open_settings(self):
        from .ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self, first_run=False)
        if dlg.exec():
            self.cfg = load_config()
            self._apply_cfg()
        return dlg

    def open_stats(self):
        """账单对话框：最近 7 天 / 30 天每日消耗 + 按模型汇总。"""
        from .ui.stats_dialog import StatsDialog
        dlg = StatsDialog(self, self.ledger)
        dlg.exec()
        return dlg

    def _apply_cfg(self):
        self.scale = float(self.cfg.get("scale", 1.5))
        self.use_token_mode = self.cfg.get("usage_mode") == "token"
        self.peak_mode = self.cfg.get("peak_mode", "default")
        self.bubble_on = bool(self.cfg.get("bubble_on", True))
        self.turn_cost_on = bool(self.cfg.get("turn_cost_on", True))
        self.turn_cost_close_ms = float(self.cfg.get("turn_cost_close_ms", 5000))
        self.volume = float(self.cfg.get("volume", 0.9))
        self.sound_set = self.cfg.get("sound_set", "duck")
        self.alert_balance_on = bool(self.cfg.get("alert_balance_on", True))
        self.alert_balance = float(self.cfg.get("alert_balance", 10.0))
        self.alert_daily_on = bool(self.cfg.get("alert_daily_on", True))
        self.alert_daily = float(self.cfg.get("alert_daily", 10.0))
        if self._players:
            for _, out in (v[1] for v in self._players.values()):
                out.setVolume(self.volume)
        self._apply_scale_geometry()
        self._settle()
        self.update()
        self.refresh_balance(False)

    def reload_cfg(self):
        self.cfg = load_config()
