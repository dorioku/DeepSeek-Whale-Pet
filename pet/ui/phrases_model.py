"""
台词词典文件（phrases.json）的读写模型 —— 纯 Python，便于单测。

目标：**编辑时不丢东西**。只认识我们支持的字段，其余字段（自定义 key、注释性的
"_说明"、每条台词上的额外属性）原样带回去；保存用临时文件 + `os.replace` 原子替换
（与 `phrases.py` / 账本同一套写法），写坏文件不会发生。

结构（与 `phrases.py` 的读取端一致）::

    {"version": 1, "_说明": [...], "groups": [
        {"weight": 45, "type": "peak", "texts": {...}},
        {"weight": 7, "style": "B", "lines": ["台词1", "台词2"]},
        {"weight": 3, "wrap": true, "lines": [{"text": "甲", "style": "A"}]},
        {"weight": 10, "type": "gif"}]}
"""
from __future__ import annotations

import json
from pathlib import Path

from ..phrases import _HELP, VERSION, _atomic_write, default_file_text

# 界面上的下拉选项（值, 显示名）
STYLE_LABELS = [("A", "A · 普通台词"), ("B", "B · 大字台词"),
                ("P", "P · 强调大字（峰谷红绿）"), ("C", "C · 小字提示")]
TYPE_LABELS = [("lines", "台词 · 随机挑一条"), ("peak", "峰谷文案 · 时段+今日已用"),
               ("gif", "动图 · assets/rua.gif")]
PEAK_COLS = ("模式名", "标题", "谷时（空闲）", "峰时（高峰）")
_LINE_KEYS = ("text", "t", "style", "s", "color", "c", "wrap", "w", "sfx")


class Line:
    """一条台词行（一个备选里的一行）。"""

    __slots__ = ("text", "style", "color", "wrap", "wrap_set", "sfx", "as_str", "extra")

    def __init__(self, text: str = "", style: str = "A", color: str = "",
                 wrap: bool = False, *, wrap_set: bool = False, sfx: str = "",
                 as_str: bool = True, extra: dict | None = None):
        self.text = text
        self.style = style
        self.color = color
        self.wrap = wrap
        self.wrap_set = wrap_set
        self.sfx = sfx
        self.as_str = as_str
        self.extra = dict(extra or {})

    @classmethod
    def from_raw(cls, raw, group_style: str, group_wrap: bool) -> "Line":
        if isinstance(raw, str):
            return cls(raw, group_style, "", group_wrap, as_str=True)
        if not isinstance(raw, dict):
            return cls(str(raw), group_style, "", group_wrap, as_str=True)
        txt = raw.get("text", raw.get("t"))
        wrap_set = "wrap" in raw or "w" in raw
        extra = {k: v for k, v in raw.items() if k not in _LINE_KEYS}
        return cls(
            "" if txt is None else str(txt),
            str(raw.get("style", raw.get("s", group_style)) or group_style).upper()[:1],
            str(raw.get("color", raw.get("c", "")) or ""),
            bool(raw.get("wrap", raw.get("w", group_wrap))),
            wrap_set=wrap_set,
            sfx=str(raw.get("sfx", "") or ""),
            as_str=False,
            extra=extra,
        )

    def to_raw(self, group_style: str, group_wrap: bool):
        """写回 JSON：一切跟组默认一致、且原本就是字符串 → 仍然写字符串。"""
        if (self.as_str and self.style == group_style and not self.color
                and self.wrap == group_wrap and not self.wrap_set and not self.sfx
                and not self.extra):
            return self.text
        out: dict = {"text": self.text}
        if self.style != group_style or not self.as_str:
            out["style"] = self.style
        if self.color:
            out["color"] = self.color
        if self.wrap != group_wrap or self.wrap_set:
            out["wrap"] = bool(self.wrap)
        if self.sfx:
            out["sfx"] = self.sfx
        out.update(self.extra)
        return out


class Group:
    """一个台词组。`alts` = 备选列表，每个备选是若干行（成套多行）。

    `alt_forms[i]` 记录该备选在文件里原本是不是数组写法 —— 原本写成一整句
    （`"台词"` 或 `{"text": …}`）的，保存时仍写成一整句，不强行改成 `["台词"]`。
    """

    __slots__ = ("weight", "enabled", "type", "style", "wrap", "sfx", "alts",
                 "alt_forms", "texts", "extra")

    def __init__(self, *, weight: float = 1.0, enabled: bool = True, type: str = "lines",
                 style: str = "A", wrap: bool = False, sfx: str = "",
                 alts=None, texts=None, extra=None, alt_forms=None):
        self.weight = float(weight)
        self.enabled = bool(enabled)
        self.type = type
        self.style = style
        self.wrap = bool(wrap)
        self.sfx = sfx
        self.alts: list[list[Line]] = (
            [[x] if isinstance(x, Line) else list(x) for x in alts] if alts else [])
        self.alt_forms: list[bool] = (list(alt_forms) if alt_forms
                                      else [False] * len(self.alts))
        self.texts: dict = texts if texts is not None else {}
        self.extra = dict(extra or {})

    # ---- 读取 ----
    @classmethod
    def from_raw(cls, raw: dict) -> "Group":
        kind = str(raw.get("type") or "").strip().lower()
        if not kind:
            kind = "gif" if raw.get("gif") else "lines"
        style = str(raw.get("style", raw.get("s", "A")) or "A").strip().upper()[:1] or "A"
        wrap = bool(raw.get("wrap", raw.get("w", False)))
        sfx = str(raw.get("sfx", "") or "")
        known = ("weight", "type", "gif", "style", "s", "wrap", "w", "sfx",
                 "enabled", "lines", "texts")
        extra = {k: v for k, v in raw.items() if k not in known}
        g = cls(weight=raw.get("weight", 1) or 1, enabled=raw.get("enabled") is not False,
                type=kind, style=style, wrap=wrap, sfx=sfx, extra=extra)
        lines = raw.get("lines")
        if isinstance(lines, list):
            for alt in lines:
                if isinstance(alt, list):
                    rows = [Line.from_raw(x, style, wrap) for x in alt]
                else:
                    rows = [Line.from_raw(alt, style, wrap)]
                rows = [r for r in rows if r.text != "" or r.extra or not r.as_str]
                if rows:
                    g.alts.append(rows)
                    g.alt_forms.append(isinstance(alt, list))
        texts = raw.get("texts")
        if isinstance(texts, dict):
            g.texts = {str(k): (dict(v) if isinstance(v, dict) else v)
                       for k, v in texts.items()}
        return g

    # ---- 写回 ----
    def to_raw(self) -> dict:
        out: dict = {"weight": (int(self.weight) if float(self.weight).is_integer()
                                else self.weight)}
        if not self.enabled:
            out["enabled"] = False
        if self.type == "gif":
            out["type"] = "gif"
        elif self.type == "peak":
            out["type"] = "peak"
            out["texts"] = self.texts
        else:
            out["style"] = self.style
            if self.wrap:
                out["wrap"] = True
            if self.sfx:
                out["sfx"] = self.sfx
            out["lines"] = [self.alt_raw(i) for i in range(len(self.alts))]
        if self.type == "gif" and self.sfx:
            out["sfx"] = self.sfx
        if self.type == "peak" and self.sfx:
            out["sfx"] = self.sfx
        out.update(self.extra)
        return out

    def alt_raw(self, i: int):
        """第 i 条备选的写回形式：原本是「一整句」的就仍写成一整句。"""
        alt = self.alts[i]
        rows = [ln.to_raw(self.style, self.wrap) for ln in alt]
        if len(rows) == 1 and not (i < len(self.alt_forms) and self.alt_forms[i]):
            return rows[0]
        return rows

    # ---- 界面用 ----
    def summary(self) -> str:
        w = f"{self.weight:g}"
        off = "" if self.enabled else " · 已停用"
        if self.type == "gif":
            return f"权重 {w} · 动图{off}"
        if self.type == "peak":
            return f"权重 {w} · 峰谷文案 · {len(self.texts)} 套{off}"
        n = len(self.alts)
        body = f"{n} 条备选" if n else "（还没有台词）"
        return f"权重 {w} · 台词 {self.style} · {body}{off}"

    def ensure_alt(self) -> list[Line]:
        """至少有一个备选（给界面用）。"""
        if not self.alts:
            self.alts.append([Line("新台词", self.style, wrap=self.wrap)])
        return self.alts[-1]

    def peak_modes(self) -> list[str]:
        return [k for k in self.texts if not str(k).startswith("_")] or ["default"]

    def peak_row(self, mode: str) -> dict:
        v = self.texts.get(mode)
        if not isinstance(v, dict):
            v = {}
            self.texts[mode] = v
        return v


class PhraseDoc:
    """整个词典文件：读盘 → 内存模型 → 原子写回。"""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.version = VERSION
        self.help: list[str] = list(_HELP)
        self.groups: list[Group] = []
        self.extra: dict = {}          # 顶层不认识的字段，原样带回去
        self.error = ""

    # ---- 读 ----
    def load(self) -> str:
        """读盘；返回错误信息（"" = 成功）。文件不存在 → 用内置默认（不报错）。"""
        self.error = ""
        raw = None
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as e:
                self.error = f"{type(e).__name__}: {e}"
                return self.error
        if not isinstance(raw, dict):
            self._load_default()
            return self.error
        data = raw
        self.version = data.get("version", VERSION) or VERSION
        hp = data.get("_说明")
        self.help = [str(x) for x in hp] if isinstance(hp, list) else list(_HELP)
        self.extra = {k: v for k, v in data.items()
                      if k not in ("version", "_说明", "groups")}
        groups = data.get("groups")
        self.groups = [Group.from_raw(g) for g in groups if isinstance(g, dict)] \
            if isinstance(groups, list) else []
        if not self.groups:
            self.error = "词典里没有可用的台词组（groups 为空或格式不对）"
            self._load_default(keep_error=True)
        return self.error

    def _load_default(self, keep_error: bool = False):
        """内置默认词典（源与 `phrases.DEFAULT_DICT` 一致）。"""
        err = self.error
        try:
            data = json.loads(default_file_text())
            self.version = data.get("version", VERSION)
            self.help = [str(x) for x in data.get("_说明", [])] or list(_HELP)
            self.groups = [Group.from_raw(g) for g in data.get("groups", [])
                           if isinstance(g, dict)]
            self.extra = {k: v for k, v in data.items()
                          if k not in ("version", "_说明", "groups")}
        except Exception as e:                 # pragma: no cover - 兜底
            self.error = f"{type(e).__name__}: {e}"
            return
        if keep_error:
            self.error = err

    def restore_default(self) -> None:
        """写回内置默认词典（含说明），并把内存模型重置为默认。"""
        if self.path is not None:
            _atomic_write(self.path, default_file_text())
        self.error = ""
        self._load_default()

    # ---- 写 ----
    def to_data(self) -> dict:
        data = {"version": self.version or VERSION, "_说明": list(self.help),
                "groups": [g.to_raw() for g in self.groups]}
        data.update(self.extra)
        return data

    def save(self) -> str:
        """原子写回；返回错误信息（"" = 成功）。"""
        if self.path is None:
            return "没有词典文件路径"
        bad = self.check()
        if bad:
            return bad
        try:
            _atomic_write(self.path, json.dumps(self.to_data(), ensure_ascii=False,
                                                indent=2) + "\n")
        except OSError as e:
            return f"写入失败：{e}"
        return ""

    def check(self) -> str:
        """保存前的自检；返回错误信息（"" = 没问题）。"""
        for i, g in enumerate(self.groups, 1):
            if not (g.weight > 0):
                return f"第 {i} 组权重必须大于 0（权重 <= 0 的组会被桌宠忽略）"
            if g.type == "lines" and not any(any(ln.text.strip() for ln in alt)
                                             for alt in g.alts):
                return f"第 {i} 组没有任何台词文本"
        return ""
