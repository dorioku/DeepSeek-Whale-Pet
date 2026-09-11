"""
生成 README 用的界面截图 —— 全部由**真实绘制代码**离屏渲染后合成，不截屏、不弹窗。

做法：
1. 桌宠 / 菜单 / 三个页面各自渲染成带 alpha 的位图（`QWidget.render` 到透明 QImage，
   按屏幕 DPR 渲染，所以字和图都是高清的）；
2. 合成到一张程序生成的“壁纸”上：画布放大到 1.5 倍、按逻辑坐标绘制 + 平滑缩放，
   控件位图基本是 1:1 贴上去，不会糊；
3. 玻璃面板（菜单 / 页面）在贴之前先垫一层**模糊壁纸 + 浅色 tint**，
   视觉上等价于 DWM 亚克力的「模糊 + 提亮」，所以在任何机器上生成的图都一致；
4. 输出 `docs/screenshots/*.png`。

demo 数据（余额 / 今日已用 / 账本 / 词典）全部写在临时目录，不碰用户真实配置。

用法：
    python tools/gen_screenshots.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
TMP = Path(tempfile.mkdtemp(prefix="whale-shots-"))

# 必须在导入 pet.* 之前指向临时配置，否则会读写用户真实的 config / 账本 / 词典
os.environ["WHALE_PET_CONFIG_PATH"] = str(TMP / "config.json")
os.environ["WHALE_PET_LEDGER_PATH"] = str(TMP / "ledger.json")
os.environ["WHALE_PET_PHRASES_PATH"] = str(TMP / "phrases.json")
os.environ["WHALE_PET_SOUNDS_PATH"] = str(TMP / "sounds")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt             # noqa: E402
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient,   # noqa: E402
                           QPainter, QPainterPath, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QGraphicsBlurEffect,    # noqa: E402
                               QGraphicsPixmapItem, QGraphicsScene)

from pet import config as cfg_mod                                   # noqa: E402
from pet.ui.menu import WhaleMenu                                   # noqa: E402
from pet.whale_widget import WhaleWidget                            # noqa: E402

SIZE = 375              # 桌宠在 scale=1.5 时的窗口边长（逻辑像素）
S = 1.5                 # 合成画布放大倍数（对齐控件位图的实际像素，贴上去 1:1）
UI_FONT = "Microsoft YaHei UI"
BG_TOP, BG_MID, BG_BOT = "#2b4067", "#1d2d4d", "#101a2e"


# ============================== 渲染工具 ==============================
def render_widget(w) -> QPixmap:
    """把控件画进一张**透明已填充**的位图（QWidget.grab 对半透明窗口不保证清零）。"""
    dpr = float(w.devicePixelRatio() or 1.0)
    img = QImage(max(1, int(w.width() * dpr)), max(1, int(w.height() * dpr)),
                 QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.GlobalColor.transparent)
    w.render(img)
    return QPixmap.fromImage(img)


def pm_size(pm: QPixmap) -> QSizeF:
    d = float(pm.devicePixelRatio() or 1.0)
    return QSizeF(pm.width() / d, pm.height() / d)


def new_canvas(w: int, h: int):
    """按逻辑尺寸开一张 S 倍分辨率的画布，返回 (画布, 已缩放画笔)。"""
    canvas = QPixmap(int(round(w * S)), int(round(h * S)))
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.scale(S, S)
    return canvas, p


def wallpaper(w: int, h: int, seed: int = 0) -> QPixmap:
    """程序生成一张“壁纸”：深蓝渐变 + 几处柔光，和 Win11 默认壁纸一个调子。

    直接按**设备像素**生成（w/h 传画布像素尺寸），贴上去就是 1:1。
    """
    pm = QPixmap(w, h)
    pm.fill(QColor(BG_BOT))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, w * 0.85, h)
    g.setColorAt(0.0, QColor(BG_TOP))
    g.setColorAt(0.5, QColor(BG_MID))
    g.setColorAt(1.0, QColor(BG_BOT))
    p.fillRect(0, 0, w, h, g)
    p.setPen(Qt.PenStyle.NoPen)
    glows = [(0.18, 0.16, 0.55, [70, 130, 220, 52]),
             (0.86, 0.24, 0.42, [96, 190, 220, 40]),
             (0.60, 0.92, 0.50, [40, 90, 170, 48]),
             (0.36, 0.68, 0.36, [120, 150, 255, 26])]
    for fx, fy, fr, (r, g_, b, a) in glows:
        cx, cy, rad = w * fx, h * fy, min(w, h) * fr
        rg = QRadialGradient(QPointF(cx, cy), rad)
        rg.setColorAt(0.0, QColor(r, g_, b, int(a * 1.35)))
        rg.setColorAt(1.0, QColor(r, g_, b, 0))
        p.setBrush(rg)
        p.drawEllipse(QPointF(cx, cy), rad, rad)
    p.end()
    return pm


def blur(pm: QPixmap, radius: float) -> QPixmap:
    """高斯模糊（模拟亚克力背后的模糊背景）。"""
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pm)
    eff = QGraphicsBlurEffect()
    eff.setBlurRadius(radius)
    item.setGraphicsEffect(eff)
    scene.addItem(item)
    out = QImage(pm.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    scene.render(p, QRectF(0, 0, pm.width(), pm.height()),
                 QRectF(0, 0, pm.width(), pm.height()))
    p.end()
    return QPixmap.fromImage(out)


def draw_bg(p: QPainter, pm: QPixmap, w: float, h: float):
    """把背景位图铺满整块逻辑画布（设备像素上就是 1:1）。"""
    p.drawPixmap(QRectF(0, 0, w, h), pm, QRectF(pm.rect()))


def glass_base(p: QPainter, rect: QRectF, radius: float, blurred: QPixmap,
               w: float, h: float, tint_alpha: int = 180):
    """玻璃面板底：先铺模糊壁纸，再叠一层浅色 tint（≈ DWM 亚克力的模糊 + 提亮）。"""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.save()
    p.setClipPath(path)
    draw_bg(p, blurred, w, h)
    p.fillRect(rect, QColor(246, 246, 246, tint_alpha))
    p.restore()


def soft_shadow(p: QPainter, rect: QRectF, radius: float,
                spread: int = 16, alpha: int = 30):
    """窗口投影（多层半透明圆角矩形叠出来，够用又不用上 QGraphicsEffect）。"""
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(spread, 0, -1):
        a = max(1, int(alpha * (1.0 - i / (spread + 1.0)) ** 2))
        p.setBrush(QColor(0, 0, 0, a))
        r = rect.adjusted(-i, -i + 3, i, i + 4)
        p.drawRoundedRect(r, radius + i, radius + i)
    p.restore()


def label(p: QPainter, text: str, cx: float, cy: float, size: int = 15,
          color=QColor(255, 255, 255, 200), weight=QFont.Weight.Normal):
    f = QFont(UI_FONT)
    f.setPixelSize(size)
    f.setWeight(weight)
    p.setFont(f)
    p.setPen(color)
    w = p.fontMetrics().horizontalAdvance(text)
    p.drawText(QRectF(cx - w / 2 - 6, cy - size, w + 12, size * 2.4),
               int(Qt.AlignmentFlag.AlignCenter), text)


def save(pm: QPixmap, name: str):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    pm.save(str(path), "PNG")
    print(f"  {path.relative_to(ROOT)}  {pm.width()}x{pm.height()}  "
          f"{path.stat().st_size / 1024:.0f} KB")


# ============================== demo 数据 ==============================
def setup_demo_config():
    """写一份 demo 配置 + 账本（临时目录），让截图里的数字/账单都有内容。"""
    import json
    from datetime import date, timedelta

    cfg = dict(cfg_mod.DEFAULTS)
    cfg.update({"api_key": "sk-demo-0000000000000000000000000000",
                "scale": 1.5, "usage_mode": "ledger",
                "alert_balance": 10.0, "alert_daily": 10.0})
    cfg_mod.save_config(cfg)

    def m(cost, tokens, turns):
        return {"cost": cost, "tokens": tokens, "turns": turns}

    today = date.today()
    history = {}
    for back, usage, model, turns in [(6, 0.42, "deepseek-chat", 6),
                                      (5, 1.86, "deepseek-reasoner", 11),
                                      (4, 0.74, "deepseek-chat", 8),
                                      (3, 2.41, "deepseek-reasoner", 14),
                                      (2, 0.31, "deepseek-chat", 5),
                                      (1, 1.12, "deepseek-reasoner", 9)]:
        key = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        history[key] = {"usage": usage,
                        "models": {model: m(usage, int(usage * 42000), turns)}}
    led = {
        "date": today.strftime("%Y-%m-%d"),
        "dayStart": 20.00, "lastBalance": 18.72, "lastCurrency": "CNY",
        "todayUsage": 1.28, "turnCost": 0.42, "syncedTurnCost": 0.30,
        "syncedUsage": 1.16, "deviation": 0.12, "alertLevel": 0,
        "models": {"deepseek-chat": m(0.86, 41200, 9),
                   "deepseek-reasoner": m(0.42, 18600, 3)},
        "modelsSync": {}, "modelsPlatform": {},
        "history": history, "migratedFrom": [],
    }
    Path(os.environ["WHALE_PET_LEDGER_PATH"]).write_text(
        json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================== 桌宠 ==============================
def pet_pixmap(kind: str) -> QPixmap:
    """渲染一张桌宠（气泡内容按 kind 切换：余额 / 峰谷台词 / 连续计费）。"""
    w = WhaleWidget()
    w.refresh_timer.stop()                 # 不联网
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    w.setFixedSize(SIZE, SIZE)
    w.state["h"] = "right"                 # 不要镜像
    w.press_x = w.press_y = 1.0
    w.bubble_t = w.pop_t = w.text_t = 1.0  # 跳过动画，直接看最终态
    w.balance, w.shown, w.currency = 12.34, 12.34, "CNY"
    w.today_usage, w.usage_dev, w.status = 1.28, 0.12, "ok"
    w.ledger.data["turnCost"] = 0.42       # 让“今日已用”后面的偏离值显示出来
    w.bubble_shown = True

    if kind == "peak":
        for _ in range(200):               # 抽到峰谷组（含 P 强调行）为止
            lines = w._pick_random()
            if isinstance(lines, list) and any(x.get("s") == "P" for x in lines):
                w._apply_random_lines(lines)
                break
    elif kind == "cost":
        w._turn_accum, w._cost_shown, w._token_shown = 3, 0.87, 12400
        w._apply_cost_lines()
        w.cost_bubble = True
    pm = render_widget(w)
    w.hide()
    w.deleteLater()
    return pm


def shot_hero():
    W, H = 1040, 620
    canvas, p = new_canvas(W, H)
    draw_bg(p, wallpaper(int(W * S), int(H * S)), W, H)
    label(p, "DeepSeek 小鲸鱼", 216, 68, size=28,
          color=QColor(255, 255, 255, 240), weight=QFont.Weight.DemiBold)
    label(p, "透明置顶的桌面宠物 · 点一下看余额 / 换台词 · 拖拽吸附 · 实时统计每轮消耗",
          352, 112, size=15, color=QColor(255, 255, 255, 150))
    p.drawPixmap(QPointF(W - SIZE - 34, H - SIZE - 26), pet_pixmap("balance"))
    p.end()
    save(canvas, "hero.png")


def shot_bubbles():
    gap, top = 22, 42
    W = 3 * SIZE + 2 * gap + 2 * 38
    H = top + SIZE + 26
    canvas, p = new_canvas(W, H)
    draw_bg(p, wallpaper(int(W * S), int(H * S)), W, H)
    tips = [("余额气泡（点一下）", "balance"),
            ("台词气泡（峰谷 / 随机台词）", "peak"),
            ("连续 N 轮消耗气泡", "cost")]
    for i, (text, kind) in enumerate(tips):
        x = 38 + i * (SIZE + gap)
        p.drawPixmap(QPointF(x, top), pet_pixmap(kind))
        label(p, text, x + SIZE / 2, 26, size=16)
    p.end()
    save(canvas, "bubbles.png")


# ============================== 菜单 / 页面 ==============================
def menu_pixmap() -> QPixmap:
    """渲染右键菜单（内容与真实菜单同一套 _fill_menu）。"""
    w = WhaleWidget()
    w.refresh_timer.stop()
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    menu = WhaleMenu()
    w._fill_menu(menu)
    menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    menu.show()
    QApplication.processEvents()
    menu._acrylic = True                  # 合成层自己模拟亚克力，这里只要它的卡片透明度
    pm = render_widget(menu)
    menu.hide()
    menu.deleteLater()
    w.deleteLater()
    return pm


def dialog_pixmap(dlg, size=None) -> QPixmap:
    dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dlg.show()
    QApplication.processEvents()
    dlg._acrylic = True
    dlg.adjustSize()
    if size:                             # 手动给个更舒服的窗口尺寸（页面本来就能拉伸）
        dlg.resize(*size)
    QApplication.processEvents()
    pm = render_widget(dlg)
    dlg.hide()
    return pm


def shot_menu():
    W, H = 760, 580
    wp = wallpaper(int(W * S), int(H * S), seed=2)
    canvas, p = new_canvas(W, H)
    draw_bg(p, wp, W, H)
    p.drawPixmap(QPointF(W - SIZE - 24, H - SIZE - 8), pet_pixmap("balance"))
    menu = menu_pixmap()
    ms = pm_size(menu)
    x, y = 52.0, 62.0
    blurred = blur(wp, 46 * S)
    soft_shadow(p, QRectF(x, y, ms.width(), ms.height()), 8, alpha=48)
    glass_base(p, QRectF(x, y, ms.width(), ms.height()), 8, blurred, W, H, 150)
    p.drawPixmap(QPointF(x, y), menu)
    p.end()
    save(canvas, "menu.png")


def shot_panel(name: str, dlg, pad=(52, 46), size=None):
    """菜单之外的页面：显示在模糊壁纸上（模糊 = 亚克力背后的观感）。"""
    pmp = dialog_pixmap(dlg, size)
    ds = pm_size(pmp)
    W = int(ds.width() + pad[0] * 2)
    H = int(ds.height() + pad[1] * 2)
    wp = wallpaper(int(W * S), int(H * S), seed=len(name))
    blurred = blur(wp, 46 * S)
    canvas, p = new_canvas(W, H)
    draw_bg(p, wp, W, H)
    x, y = float(pad[0]), float(pad[1])
    soft_shadow(p, QRectF(x, y, ds.width(), ds.height()), 8, alpha=52)
    glass_base(p, QRectF(x, y, ds.width(), ds.height()), 8, blurred, W, H, 160)
    p.drawPixmap(QPointF(x, y), pmp)
    p.end()
    save(canvas, name)
    dlg.deleteLater()


# ============================== 主流程 ==============================
def main():
    setup_demo_config()
    app = QApplication.instance() or QApplication(sys.argv)

    from pet.balance import Ledger
    from pet.ui.phrases_dialog import PhrasesDialog
    from pet.ui.settings_dialog import SettingsDialog
    from pet.ui.stats_dialog import StatsDialog

    # 限制图里的账本只要 demo 数据：别把本机真实账本（仓库根 / %APPDATA% 的旧文件）并进来
    Ledger._migrate = lambda self, paths: False

    print("生成中…")
    shot_hero()
    shot_bubbles()
    shot_menu()
    shot_panel("settings.png", SettingsDialog(None, first_run=False))
    # 账单默认 640 宽会把「模型」列截断，这里按拉宽后的样子截（页面本来就是可拉伸的）
    shot_panel("stats.png",
               StatsDialog(None, Ledger(Path(os.environ["WHALE_PET_LEDGER_PATH"]))),
               size=(860, 760))
    phrases = PhrasesDialog(None, cfg_mod.phrases_path())
    phrases.path_label.setText(r"%APPDATA%\WhalePet\phrases.json")   # 别把临时目录写进图里
    shot_panel("phrases.png", phrases)

    app.processEvents()
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
