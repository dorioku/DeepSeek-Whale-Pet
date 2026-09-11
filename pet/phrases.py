"""
台词词典 —— 气泡随机台词改由**用户可编辑的 JSON 文件**提供（通用词典）。

原先台词（含峰谷文案）硬编码在 `whale_widget._pick_random / _group_peak` 里，
改一个字都要动 Python。现在拆成独立的「通用词典」：

- 默认词典与内置台词完全一致（权重 / 样式 / 换行标记逐条对应原实现）；
- 首次运行自动生成 `phrases.json`（与 config.json 同目录，默认
  `%APPDATA%\\WhalePet\\`，便携模式 = exe 同目录），文件里带一份 "_说明"；
- 每次抽台词前按 (mtime, size) 检查文件，**改完保存即生效，无需重启**；
- 连点切台词时“最近两次不重复”：`pick(recent=...)` 会避开刚显示过的内容
  （同一个至少隔两条才会再出现；词典可选内容太少时自动放宽，不会卡住）；
- 文件缺失 / 写坏 → 自动回退内置默认词典（原因记在 `PhraseBook.error`），
  绝不因为一句台词写错就让气泡失灵。

词典结构（`weight` 等字段省略即用默认值）::

    {
      "version": 1,
      "groups": [
        {"weight": 45, "type": "peak", "texts": {...}},            # 内置动态组
        {"weight": 7,  "style": "B", "lines": ["台词1", "台词2"]},  # 随机挑一条
        {"weight": 7,  "wrap": true, "lines": [                    # 一次显示多行
            {"text": "第一行", "style": "A"},
            {"text": "第二行", "style": "C"}]},
        {"weight": 10, "type": "gif"},                             # 动图组
        {"weight": 1,  "sfx": "ciallo", "lines": ["带音效的台词"]},  # 显示时放音效
        {"weight": 5,  "enabled": false, "lines": ["临时停用"]}
      ]
    }

- `weight`：相对权重（默认 1）；<=0、非数字或 `enabled: false` 的组直接忽略。
- `lines`：备选台词列表 —— 每次从里面**随机挑一个**；一个备选可以是
  一句话、一个对象（`{text/style/wrap/color}`），或一个列表（= 成套多行台词）。
- `style`：`A` 普通台词 / `B` 大字台词 / `P` 强调大字（默认按峰谷红绿，
  可用 `color` 指定色值）/ `C` 小字提示。
- `type`：`peak` = 时段 + 今日已用（动态，texts 按峰谷文案模式取名）、
  `gif` = 动图（assets/rua.gif），其余一律当台词组处理。
- `sfx`：组（或单条台词）可带音效名，显示时播放同名音效：先找 config 同目录的 `sounds/`
  （可自备，如 `sounds/ciallo.mp3`），再找内置 `assets/`（内置 "ciallo" → assets/ciallo.wav）。
"""
from __future__ import annotations

import json
import math
import os
import random
from pathlib import Path

VERSION = 1

# “最近两次不重复”的最大重抽次数：抽到全是被避开的内容时重抽，
# 超过这个次数就放宽限制（词典可选内容太少时不会卡死，只是允许重复）。
REPEAT_TRIES = 24

# 样式代号（绘制层 _style_lines 识别）：A 普通 / B 大字 / P 峰谷强调 / C 小字
STYLES = ("A", "B", "P", "C")

_PEAK_TITLE = "当前时间段为:"
_PEAK_OFF = "空闲时段"
_PEAK_PEAK = "高峰时段"

# 内置默认词典：与拆分前的硬编码台词（权重、样式、换行标记）逐条对应
DEFAULT_DICT = {
    "version": VERSION,
    "groups": [
        {"weight": 45, "type": "peak", "texts": {
            "default": {"title": _PEAK_TITLE, "off": _PEAK_OFF, "peak": _PEAK_PEAK},
            "liangwen": {"off": "梁文谷", "peak": "梁文峰"},
            "qiangqiang": {"off": "!?谷谷?!", "peak": "!?峰峰?!"},
        }},
        {"weight": 7, "style": "B", "lines": ["好模型... ↓", "好女孩...↓"]},
        {"weight": 7, "style": "A", "wrap": True, "lines": [
            "不知道用户有什么用，先赶走吧~", "事已至此，先吃饭吧。", "我...我...我也要挣钱吗？",
            "我去吃饭啦，测完叫我", "压力一只蓝色大肥鱼？！",
            "DeepSleep...", "看不太懂，瞎编一个应付下用户先。", "坏了...用户彻底怒了！"]},
        {"weight": 10, "type": "gif"},
        {"weight": 3, "style": "A", "wrap": True, "lines": [
            "你目录里的dsh是什么...大烧货吗...?",
            "恭喜你实现token自由！token全跑了！",
            "真当我是便宜货啊..."]},
        {"weight": 1, "style": "B", "lines": ["哦鲸鲸... "]},
        {"weight": 1, "sfx": "ciallo", "lines": [
            [{"text": "Ciallo～", "style": "B"},
             {"text": "(∠・ω< )⌒☆", "style": "C"}]]},
    ],
}

# 生成词典文件时附带的说明（JSON 没有注释，用 "_说明" 字段代替）
_HELP = [
    "台词词典：小鲸鱼气泡里的随机台词都在这里，随你改（保存即生效，不用重启）。",
    "groups = 台词组；每次弹台词先按 weight 随机挑一组，再从该组的 lines 里随机挑一条。",
    "weight：权重，越大越常出现（默认 1）；\"enabled\": false 可整组临时停用。",
    "lines：备选台词；一句话写成 \"文本\"，带样式写成 {\"text\": \"...\", \"style\": \"B\"}，",
    "       一次显示多行写成列表 [\"第一行\", \"第二行\"]（一次只挑其中一“条”备选）。",
    "style：A=普通台词 / B=大字台词 / P=强调大字（默认按峰谷红绿，可用 \"color\" 指定色值）/ C=小字提示。",
    "wrap：true 时长句自动换行（长台词建议打开）。",
    "sfx：可选，显示这组台词时播放同名音效（优先 config 同目录 sounds/，其次内置 assets/；支持 wav/mp3/aac 等）。",
    "type=\"peak\"：内置动态组，显示「当前时段 + 今日已用」；texts 按设置里的「峰谷文案」取名",
    "       （default / liangwen / qiangqiang，可自己加；字段 title=首行、off=谷时、peak=峰时）。",
    "type=\"gif\"：动图组（assets/rua.gif），不带文字。",
    "小鲸鱼连续点击切台词时，最近两次出现过的那条不会马上重复（同一个至少隔两条才会再出现；",
    "可选台词太少时会自动放宽，不会卡住）。",
    "改坏了不要紧：文件读不出来时会自动退回内置默认台词；想恢复出厂设置，删掉本文件即可。",
]

# 余额预警专用台词（whale_widget 触发余额预警时随机挑一条；与随机词典互不影响）
BALANCE_ALERT_LINES = (
    "没有 token 了，要饿死了……",
    "肥鱼巧施连环计，用户误失大白饭。",
)


# ================= 归一化：把词典文件里的写法变成绘制层认识的行 =================
def _style_of(value, fallback: str = "A") -> str:
    s = str(value or "").strip().upper()
    return s if s in STYLES else fallback


def _sfx_of(value):
    """音效名（如 "ciallo" → assets/ciallo.wav）；空 / 非字符串返回 None。"""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _norm_line(item, style: str = "A", wrap: bool = False, sfx=None):
    """一条台词 → 绘制层的 {t, s, w}[, c][, sfx]；不认识的内容返回 None。"""
    if isinstance(item, str):
        out = {"t": item, "s": style, "w": bool(wrap)}
    elif isinstance(item, dict):
        txt = item.get("text", item.get("t"))
        if not isinstance(txt, str):
            return None
        out = {
            "t": txt,
            "s": _style_of(item.get("style", item.get("s")), style),
            "w": bool(item.get("wrap", item.get("w", wrap))),
        }
        color = item.get("color", item.get("c"))
        if isinstance(color, str) and color.strip():
            out["c"] = color.strip()
        sfx = _sfx_of(item.get("sfx")) or sfx     # 单条台词可覆盖组级音效
    else:
        return None
    if sfx:
        out["sfx"] = sfx
    return out


def _norm_alt(alt, style: str, wrap: bool, sfx=None):
    """一个备选 → 行列表（列表 = 一个气泡里的多行文本）；无效返回 None。"""
    if isinstance(alt, list):
        rows = [ln for ln in (_norm_line(x, style, wrap, sfx) for x in alt) if ln]
        return rows or None
    ln = _norm_line(alt, style, wrap, sfx)
    return [ln] if ln else None


def _norm_alts(raw, style: str, wrap: bool, sfx=None):
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for alt in raw:
        rows = _norm_alt(alt, style, wrap, sfx)
        if rows:
            out.append(rows)
    return out


def _norm_peak_texts(texts) -> dict:
    """峰谷组文案：{模式名: {title, off, peak}}（title 缺省=None → 用内置标题）。"""
    out: dict = {}
    if not isinstance(texts, dict):
        return out
    for key, val in texts.items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        title = val.get("title")
        out[str(key)] = {
            "title": None if title is None else str(title),
            "off": None if val.get("off") is None else str(val.get("off")),
            "peak": None if val.get("peak") is None else str(val.get("peak")),
        }
    return out


def _norm_group(group):
    """一个台词组 → 归一化后的组；权重非法 / 停用 / 无内容 → None。"""
    if not isinstance(group, dict) or group.get("enabled") is False:
        return None
    kind = str(group.get("type") or "").strip().lower()
    if not kind:
        kind = "gif" if group.get("gif") else "lines"
    try:
        weight = float(group.get("weight", 1))
    except (TypeError, ValueError):
        weight = 1.0
    if not math.isfinite(weight) or weight <= 0:
        return None
    if kind == "gif":
        return {"type": "gif", "weight": weight}
    if kind == "peak":
        return {"type": "peak", "weight": weight,
                "texts": _norm_peak_texts(group.get("texts"))}
    style = _style_of(group.get("style", group.get("s")), "A")
    wrap = bool(group.get("wrap", group.get("w", False)))
    alts = _norm_alts(group.get("lines"), style, wrap, _sfx_of(group.get("sfx")))
    if not alts:
        return None
    return {"type": "lines", "weight": weight, "alts": alts}


def default_groups() -> list:
    """内置默认组（归一化后）；任何情况下 pick 都有内容可抽。"""
    return [g for g in (_norm_group(x) for x in DEFAULT_DICT["groups"]) if g]


def content_key(lines) -> tuple:
    """一段气泡内容的指纹（“最近几次不重复”用）：文案 + 样式 + 颜色 + 音效。

    只有内容完全一致才算重复（同一组里换一条备选 = 不同内容，不算重复）。
    """
    if isinstance(lines, dict):            # 目前只有动图：{"gif": True}
        return ("gif",) if lines.get("gif") else ("dict", tuple(sorted(map(str, lines))))
    if not isinstance(lines, list):
        return ("?", str(lines))
    return tuple(
        (str(ln.get("t", "")), str(ln.get("s", "A")), str(ln.get("c", "")),
         bool(ln.get("w")), str(ln.get("sfx", "")))
        for ln in lines if isinstance(ln, dict)
    )


_DEFAULT_GROUPS = default_groups()


def default_file_text() -> str:
    """词典文件内容（默认词典 + 说明）。"""
    data = {
        "version": VERSION,
        "_说明": list(_HELP),
        "groups": DEFAULT_DICT["groups"],
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _atomic_write(path: Path, text: str):
    """临时文件 + 原子替换：避免写到一半留下半截词典（与账本同一套做法）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class PhraseBook:
    """用户词典：读盘（含改动自动重载）+ 按权重抽一组台词。"""

    def __init__(self, path=None, *, create: bool = False):
        self.path = Path(path) if path else None
        self.error = ""            # 最近一次加载失败原因（界面提示用）
        self.groups: list = []     # 归一化后的组；空 = 当前在用内置默认
        self._stamp = None         # (mtime_ns, size) 指纹
        if create:
            try:
                self.ensure_file()
            except OSError as e:   # 只读目录等：不影响使用，继续用内置默认
                self.error = f"无法生成词典文件：{e}"
        self.load()

    # ---- 读写 ----
    def _fingerprint(self):
        if self.path is None:
            return None
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self):
        """读盘并归一化；缺失 / 损坏 → 回退内置默认（error 记录原因）。"""
        self.error = ""
        self._stamp = self._fingerprint()
        data = None
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = None
            except (OSError, ValueError, UnicodeDecodeError) as e:
                self.error = f"{type(e).__name__}: {e}"
        groups: list = []
        if isinstance(data, dict):
            raw = data.get("groups")
            if isinstance(raw, list):
                groups = [g for g in (_norm_group(x) for x in raw) if g]
        if not groups and not self.error:
            # 文件在、但一个可用组都没有（空词典 / 格式不对）
            if data is not None:
                self.error = "词典里没有可用的台词组（groups 为空或格式不对），已用内置默认"
        self.groups = groups or list(_DEFAULT_GROUPS)

    def reload_if_changed(self) -> bool:
        """文件有改动才重读（抽台词前调用，改完保存即生效）。"""
        if self._fingerprint() == self._stamp:
            return False
        self.load()
        return True

    def ensure_file(self) -> Path:
        """确保词典文件存在（缺失则写入默认词典），返回路径。"""
        if self.path is None:
            raise OSError("未指定词典路径")
        if not self.path.exists():
            _atomic_write(self.path, default_file_text())
            self.load()
        return self.path

    # ---- 抽取 ----
    def pick(self, *, today_text: str = "", peak: bool = False,
             peak_mode: str = "default", recent=()):
        """按权重随机抽一组 → 绘制层认识的内容（行列表，或 {"gif": True}）。

        `recent`：最近显示过的内容指纹（`content_key`，按时间先后排列）——
        新抽的内容会避开**最近两次**（连点不重复，同一个至少隔两次才可能出现）；
        词典可选内容太少（全被避开）时依次放宽到“只避最近一次”→“不避”，不会卡死。
        """
        keys = [tuple(k) for k in recent if k]
        out = None
        for depth in (2, 1, 0):
            avoid = set(keys[-depth:]) if depth else set()
            if not avoid:
                return self._pick_once(today_text=today_text, peak=peak,
                                       peak_mode=peak_mode)
            for _ in range(REPEAT_TRIES):
                out = self._pick_once(today_text=today_text, peak=peak,
                                      peak_mode=peak_mode)
                if content_key(out) not in avoid:
                    return out
        return out                                      # pragma: no cover - 防御

    def _pick_once(self, *, today_text: str = "", peak: bool = False,
                   peak_mode: str = "default"):
        """按权重抽一次（不做“最近不重复”的避让，见 `pick`）。"""
        groups = self.groups or list(_DEFAULT_GROUPS)
        total = sum(g["weight"] for g in groups)
        if total <= 0:                                  # pragma: no cover - 防御
            groups, total = list(_DEFAULT_GROUPS), sum(g["weight"] for g in _DEFAULT_GROUPS)
        r = random.random() * total
        chosen = groups[-1]
        for g in groups:
            r -= g["weight"]
            if r < 0:
                chosen = g
                break
        if chosen["type"] == "gif":
            return {"gif": True}
        if chosen["type"] == "peak":
            return self._peak_lines(chosen, today_text, peak, peak_mode)
        # 复制一份：别让调用方（或绘制层）改到词典缓存
        return [dict(ln) for ln in random.choice(chosen["alts"])]

    @staticmethod
    def _peak_lines(group, today_text: str, peak: bool, mode: str):
        """峰谷组：标题 + 峰/谷状态 + 今日已用（文案取 texts[mode]，缺省 default）。"""
        texts = group.get("texts") or {}
        cfg = texts.get(mode)
        if not isinstance(cfg, dict):
            cfg = texts.get("default")
        if not isinstance(cfg, dict):
            cfg = next((v for v in texts.values() if isinstance(v, dict)), {})
        cfg = cfg or {}
        title = cfg.get("title")
        if title is None:
            title = _PEAK_TITLE
        state = (cfg.get("peak") if peak else cfg.get("off")) or \
            (_PEAK_PEAK if peak else _PEAK_OFF)
        lines = []
        if title:                                   # title = "" 表示不要首行
            lines.append({"t": title, "s": "A", "w": False})
        lines.append({"t": state, "s": "P", "w": False,
                      "c": "#e0433f" if peak else "#2fa24c"})
        if today_text:
            lines.append({"t": today_text, "s": "C", "w": False})
        return lines


def open_dict_file(parent=None, path=None) -> bool:
    """界面入口：确保词典文件存在 →（读坏时提示）→ 用系统默认程序打开。

    Qt 在函数内导入：本模块自身不依赖 PySide6（便于单独测试 / 复用）。
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    if path is None:
        from .config import phrases_path
        path = phrases_path()
    book = PhraseBook(path)
    try:
        target = book.ensure_file()
    except OSError as e:
        QMessageBox.warning(parent, "台词词典", f"无法生成词典文件：\n{e}")
        return False
    if book.error:
        QMessageBox.warning(
            parent, "台词词典",
            "词典读取失败，当前仍用内置默认台词：\n\n"
            f"{book.error}\n\n修好后保存即可生效（改动会自动重载，无需重启）。")
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
    return True
