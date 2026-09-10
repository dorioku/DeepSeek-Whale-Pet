"""
余额拉取 + 小鲸鱼记账（移植自 DeepSeek-Balance-Whale-Widget lib/index.js）。

- fetch_balance: 调 DeepSeek 官方接口 GET /user/balance，优先选 CNY 且余额 > 0。
- Ledger: 每次观测余额后按“余额差值”累计今日已用；同时按模型记录每日消耗
  （金额 / Token / 轮次），跳天自动归档并长期保留（HISTORY_KEEP 天）；
  币种变化时只重置基准、不记差值（防止多币种切换污染账本）；
  另记录“每日使用预警”已触发的档位（跨重启保留）。
  账本文件是**多进程共享的缓存**：写入用原子替换（读不到半个文件），读坏不静默清零；
  每次轮询前 `sync_from_disk()` 对比文件指纹，外部改动（另一个实例 / 手动编辑 /
  旧位置账本）按“单调计数器取大值”并入，不会互相覆盖。
- fetch_platform_usage: 可选“实时·令牌”模式，调平台用量接口按峰谷定价换算今日已用
  与按模型的明细。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .pricing import is_peak_time, usage_cost

BALANCE_TTL_MS = 25_000


def _pick_balance_info(infos: list) -> dict | None:
    if not infos:
        return None

    def num(x):
        return float(x.get("total_balance")) if x and "total_balance" in x else float("nan")

    for x in infos:
        if x and x.get("currency") == "CNY" and num(x) > 0:
            return x
    for x in infos:
        if x and num(x) > 0:
            return x
    for x in infos:
        if x and x.get("currency") == "CNY":
            return x
    return infos[0]


def fetch_balance(api_key: str, api_base: str = "https://api.deepseek.com",
                  timeout: float = 20.0) -> dict:
    """拉取余额。成功返回 {ok, totalBalance, currency, updatedAt}；
    失败返回 {ok:false, code, error, transient?}。"""
    if not api_key:
        return {"ok": False, "code": "NO_KEY", "error": "未配置 API Key"}
    url = api_base.rstrip("/") + "/user/balance"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + api_key})
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw)
            info = _pick_balance_info(data.get("balance_infos"))
            if not info or "total_balance" not in info:
                return {"ok": False, "code": "SHAPE", "error": "余额接口返回结构异常"}
            return {
                "ok": True,
                "totalBalance": float(info["total_balance"]),
                "currency": str(info.get("currency") or "CNY"),
                "updatedAt": datetime.now().isoformat(),
            }
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code < 500:
                break
        except Exception as e:  # 网络/超时/JSON 错误
            last_err = e
        if attempt == 0:
            import time
            time.sleep(0.5)
    transient = not (isinstance(last_err, urllib.error.HTTPError) and last_err.code < 500)
    msg = str(last_err) if last_err else "unknown"
    if len(msg) > 200:
        msg = msg[:200]
    return {"ok": False, "code": "HTTP", "transient": transient, "error": "余额接口请求失败: " + msg}


# ---------------------------------------------------------------- 记账账本
HISTORY_KEEP = 730        # 历史保留天数（长期记录：两年；每天一条，体积很小）


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _norm_model_rec(rec) -> dict:
    """把某模型的 {cost, tokens, turns} 规整成数值（兼容缺字段/脏数据）。"""
    if isinstance(rec, dict):
        return {"cost": float(rec.get("cost") or 0.0),
                "tokens": int(rec.get("tokens") or 0),
                "turns": int(rec.get("turns") or 0)}
    return {"cost": 0.0, "tokens": 0, "turns": 0}


def _norm_hist(rec) -> dict:
    """历史记录兼容：旧版是数字（当日已用），新版是 {usage, models}。"""
    if isinstance(rec, (int, float)) and not isinstance(rec, bool):
        return {"usage": float(rec), "models": {}}
    if isinstance(rec, dict):
        models = rec.get("models") if isinstance(rec.get("models"), dict) else {}
        return {"usage": float(rec.get("usage") or 0.0),
                "models": {str(k): _norm_model_rec(v) for k, v in models.items()}}
    return {"usage": 0.0, "models": {}}


def _usage_of(data) -> float:
    """一份账本数据里“当天已用”的展示值（history 记录与账本本体都适用）。"""
    if not isinstance(data, dict):
        return 0.0
    if "todayUsage" not in data:                 # history 记录形态
        return float(data.get("usage") or 0.0)
    base = float(data.get("todayUsage") or 0.0)
    pending = (float(data.get("turnCost") or 0.0)
               - float(data.get("syncedTurnCost") or 0.0))
    return base + max(0.0, pending)


def _models_of(data) -> dict:
    """一份账本数据里“按模型”的展示值：平台权威值 + 同步后新发生的本地消耗。"""
    if not isinstance(data, dict):
        return {}
    local = data.get("models") or {}
    snap = data.get("modelsSync") or {}
    plat = data.get("modelsPlatform") or {}
    out: dict[str, dict] = {}
    for name, raw in local.items():
        rec = _norm_model_rec(raw)
        s = _norm_model_rec(snap.get(name))
        p = _norm_model_rec(plat.get(name))
        out[str(name)] = {
            "cost": p["cost"] + max(0.0, rec["cost"] - s["cost"]),
            "tokens": p["tokens"] + max(0, rec["tokens"] - s["tokens"]),
            "turns": rec["turns"],
        }
    for name, raw in plat.items():
        if str(name) not in out:        # 平台有、本地没统计到的模型
            p = _norm_model_rec(raw)
            out[str(name)] = {"cost": p["cost"], "tokens": p["tokens"], "turns": 0}
    return out


def _models_total(models) -> float:
    return sum(_norm_model_rec(v)["cost"] for v in (models or {}).values())


class Ledger:
    """小鲸鱼记账：余额差值累计今日已用、按模型统计，跨天归档，跨重启继续累加。

    设计（“即使没长期挂着也能对上消耗”）：
    - dayStart：当日首次观测到的余额（资金初始值），当天首次启动时就缓存下来；
    - todayUsage：余额差值累加的今日已用（权威值）——重启后拿上次观测值与
      当前余额作差，未挂载时段的消耗也能算进去；
    - turnCost：本地中转按每轮 usage 换算的累计消耗，每轮立刻叠加；
    - syncedTurnCost/deviation：上次同步（轮询/平台用量）时的 turnCost
      及其偏离值 = todayUsage - turnCost（正 = 实际比本地统计多）；
    - models：按模型累计（cost/tokens/turns），每轮立刻叠加；
      modelsPlatform/modelsSync 是平台用量接口给出的权威按模型值及其同步快照，
      展示值 = 平台权威值 + 上次同步后新发生的本地消耗（同一套“基准 + 增量”）；
    - history：按天归档 {日期: {usage, models}}，长期保留（HISTORY_KEEP 天）。

    展示值 = todayUsage + max(0, turnCost - syncedTurnCost)：
    两次轮询之间每轮消耗也能立刻看到，下一次同步时再修正。
    """

    def __init__(self, path: Path, migrate_from: list[Path] | None = None):
        self.path = path
        self.data = self._load()
        self._stamp = self._file_stamp()   # 缓存文件指纹：用于发现外部改动
        # RLock：读方法（daily_series 等）内部也会取锁，且归档会在 _rollover 里重入
        self._lock = threading.RLock()
        if migrate_from and self._migrate(migrate_from):
            self._save()

    @staticmethod
    def _read_file(path: Path) -> dict | None:
        """读一份账本 JSON，带重试（撞上别的实例替换文件的瞬间重试即可）。"""
        for attempt in range(3):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                if attempt < 2:
                    time.sleep(0.05)
                    continue
                return None
            if isinstance(loaded, dict) and isinstance(loaded.get("date"), str):
                return loaded
            return None
        return None

    def _file_stamp(self):
        """文件指纹（mtime + 大小）：用来发现缓存被外部改写。"""
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self) -> dict:
        loaded = self._read_file(self.path)
        if loaded is None and self.path.exists():
            # 文件在、却读不出（写坏/被占用）：先留档，绝不静默清零
            try:
                shutil.copy2(self.path, self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
        data = loaded if loaded is not None else {"date": _today_key()}
        # 兼容旧账本：补齐新增字段
        data.setdefault("dayStart", None)
        data.setdefault("lastBalance", None)
        data.setdefault("lastCurrency", "")
        data.setdefault("todayUsage", 0.0)
        data.setdefault("turnCost", 0.0)
        data.setdefault("syncedTurnCost", 0.0)
        data.setdefault("deviation", 0.0)
        # 按模型：本地每轮累计 + 平台权威按模型值及其同步快照
        data.setdefault("models", {})
        data.setdefault("modelsSync", {})
        data.setdefault("modelsPlatform", {})
        data.setdefault("alertLevel", 0)     # 今日“每日使用预警”已触发档位（跳天清零）
        data.setdefault("migratedFrom", [])  # 已并入的旧账本路径（避免重复迁移）
        hist = data.get("history")
        data["history"] = ({str(k): _norm_hist(v) for k, v in hist.items()}
                           if isinstance(hist, dict) else {})
        return data

    def _save(self):
        """原子落盘：先写临时文件再替换，别的实例永远读不到半个文件。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            self._stamp = self._file_stamp()
        except OSError:
            pass

    def _trim_history(self):
        """历史长期保留（每天一条，730 天也只有百来 KB）。"""
        hist = self.data.setdefault("history", {})
        if len(hist) > HISTORY_KEEP:
            for k in sorted(hist.keys())[:-HISTORY_KEEP]:
                del hist[k]

    # ---- 缓存文件同步（多实例 / 外部编辑安全）----
    def sync_from_disk(self) -> bool:
        """对比缓存文件：被外部改动过就并入，保证显示与缓存一致且不丢数据。

        每次轮询（`record`）前调用。合并后返回 True。
        """
        with self._lock:
            stamp = self._file_stamp()
            if stamp is None or stamp == self._stamp:
                return False                 # 文件没变（或还没落盘），无需处理
            other = self._read_file(self.path)
            self._stamp = stamp
            if other is None:
                return False
            if self._merge(other):
                self._save()
                return True
            return False

    _COUNTERS = ("todayUsage", "turnCost", "syncedTurnCost", "alertLevel")

    def _merge(self, other: dict) -> bool:
        """把另一份账本（另一个实例写入 / 旧位置迁移）并入本账本。

        - 对方日期更新：整体接管对方当日状态（我方当天先归档进 history）
        - 同一天：单调递增的计数器与按模型数据逐项取大值，history 取并集
        - 对方日期更旧：把对方当天消耗并入 history
        返回是否有变化。
        """
        mine = self.data
        changed = False
        hist = mine.setdefault("history", {})
        for k, v in (other.get("history") or {}).items():
            rec = _norm_hist(v)
            if _norm_hist(hist.get(k))["usage"] < rec["usage"] or k not in hist:
                hist[k] = rec
                changed = True
        o_date = str(other.get("date") or "")
        m_date = str(mine.get("date") or "")
        if o_date and o_date > m_date:
            # 对方更新：我方当天先归档，再整体接管（history 保留并集）
            if m_date:
                self._archive_today()
            keep = mine["history"]
            mine.update({k: v for k, v in other.items() if k != "history"})
            mine["history"] = keep
            self._trim_history()
            return True
        if o_date and o_date < m_date:
            # 对方是旧的：只把对方当天消耗并入 history
            usage, models = _usage_of(other), _models_of(other)
            if usage > 0 or models:
                cur = _norm_hist(hist.get(o_date))
                if cur["usage"] < usage:
                    hist[o_date] = {"usage": usage, "models": models}
                    changed = True
            return changed
        # 同一天：计数器取大值
        for key in self._COUNTERS:
            try:
                if float(other.get(key) or 0.0) > float(mine.get(key) or 0.0):
                    mine[key] = other.get(key)
                    changed = True
            except (TypeError, ValueError):
                continue
        for key in ("dayStart", "lastBalance"):
            if mine.get(key) is None and other.get(key) is not None:
                mine[key] = other[key]
                changed = True
        if not mine.get("lastCurrency") and other.get("lastCurrency"):
            mine["lastCurrency"] = other["lastCurrency"]
            changed = True
        # 按模型逐项取大值（计数器单调递增，取大值不会重复计数）
        lm = mine.setdefault("models", {})
        for name, raw in (other.get("models") or {}).items():
            r = _norm_model_rec(raw)
            c = _norm_model_rec(lm.get(name))
            if (r["cost"] > c["cost"] or r["tokens"] > c["tokens"]
                    or r["turns"] > c["turns"] or str(name) not in lm):
                lm[str(name)] = {"cost": max(r["cost"], c["cost"]),
                                 "tokens": max(r["tokens"], c["tokens"]),
                                 "turns": max(r["turns"], c["turns"])}
                changed = True
        # 平台按模型：取合计更大的一方（权威值）
        if _models_total(other.get("modelsPlatform")) > _models_total(mine.get("modelsPlatform")):
            mine["modelsPlatform"] = other.get("modelsPlatform") or {}
            mine["modelsSync"] = other.get("modelsSync") or {}
            changed = True
        self._trim_history()
        return changed

    def _migrate(self, paths) -> bool:
        """把旧位置的账本 / 缓存一次性并入（记录已迁移路径，避免重复）。"""
        done = self.data.setdefault("migratedFrom", [])
        changed = False
        for p in paths:
            try:
                p = Path(p)
            except TypeError:
                continue
            key = str(p)
            if key in done or not p.exists() or p == self.path:
                continue
            other = self._read_file(p)
            if other is not None and self._merge(other):
                changed = True
            done.append(key)
        return changed

    def _archive_today(self):
        """把当天记录归档进 history（跳天时调用；锁内使用）。"""
        day = self.data.get("date")
        if not day:
            return
        self.data.setdefault("history", {})[day] = {
            "usage": _usage_of(self.data),
            "models": _models_of(self.data),
        }

    def _rollover(self, day_start=None):
        """跳天：归档当日已用（含按模型明细），重置基准与计数。

        资金初始值留待当日首次观测；没有观测值就清空基准，避免拿昨天的余额
        跟今天作差（会误记一笔）。
        """
        led = self.data
        self._archive_today()
        led.update({
            "date": _today_key(),
            "dayStart": day_start,
            "lastBalance": day_start,
            "todayUsage": 0.0,
            "turnCost": 0.0,
            "syncedTurnCost": 0.0,
            "deviation": 0.0,
            "models": {},
            "modelsSync": {},
            "modelsPlatform": {},
            "alertLevel": 0,
        })

    def _sync_deviation(self):
        """同步：把当前每轮累计当作基准，算出与实际消耗的偏离值。"""
        tc = float(self.data.get("turnCost") or 0.0)
        self.data["syncedTurnCost"] = tc
        self.data["deviation"] = float(self.data.get("todayUsage") or 0.0) - tc

    # ---- 按模型统计 ----
    def _local_model(self, name: str) -> dict:
        """取（必要时创建）某模型的本地累计记录（锁内使用）。"""
        models = self.data.setdefault("models", {})
        rec = models.get(name)
        if not isinstance(rec, dict):
            rec = {"cost": 0.0, "tokens": 0, "turns": 0}
            models[name] = rec
        rec.setdefault("cost", 0.0)
        rec.setdefault("tokens", 0)
        rec.setdefault("turns", 0)
        return rec

    def _models_breakdown(self) -> dict:
        """今日各模型展示值 = 平台权威值 + 上次同步后新发生的本地消耗。"""
        return _models_of(self.data)

    def _display_usage(self) -> float:
        """今日已用展示值（锁内使用；对外见 today_usage）。"""
        return _usage_of(self.data)

    def record(self, balance: float, currency: str) -> dict:
        """观测一次余额：维护当日初始值/差值累计，并同步偏离值。"""
        with self._lock:
            self.sync_from_disk()        # 每次轮询先对比缓存：外部改动先并入再落盘
            t = _today_key()
            led = self.data
            cur = str(currency or "")
            currency_changed = (led.get("lastCurrency") and cur
                                and led["lastCurrency"] != cur)
            if led.get("date") != t:
                # 跳天：当日初始值 = 今天首次观测到的余额
                self._rollover(balance)
                led["lastCurrency"] = cur
            elif currency_changed:
                led["lastBalance"] = balance
                led["lastCurrency"] = cur
                if led.get("dayStart") is None:
                    led["dayStart"] = balance
            else:
                if led.get("dayStart") is None:
                    led["dayStart"] = balance      # 老账本补齐当日初始值
                prev = led.get("lastBalance")
                if isinstance(prev, (int, float)) and isinstance(balance, (int, float)) \
                        and balance < prev:
                    led["todayUsage"] = (led.get("todayUsage") or 0.0) + (prev - balance)
                led["lastBalance"] = balance
                led["lastCurrency"] = cur
            # 历史长期保留（每天一条，730 天也只有百来 KB）
            self._trim_history()
            self._sync_deviation()
            self._save()
            return led

    def add_turn_cost(self, cost: float, model: str = "", tokens: int = 0) -> float:
        """每轮对话的消耗立刻叠加（不等轮询），并按模型累计，返回今日已用展示值。"""
        try:
            c = float(cost)
        except (TypeError, ValueError):
            return self.today_usage
        if not math.isfinite(c):
            return self.today_usage
        with self._lock:
            self.sync_from_disk()
            if self.data.get("date") != _today_key():
                self._rollover()
            self.data["turnCost"] = float(self.data.get("turnCost") or 0.0) + c
            if c > 0:
                name = str(model or "").strip() or "未知模型"
                rec = self._local_model(name)
                rec["cost"] = float(rec.get("cost") or 0.0) + c
                rec["tokens"] = int(rec.get("tokens") or 0) + max(0, int(tokens or 0))
                rec["turns"] = int(rec.get("turns") or 0) + 1
            self._save()
        return self.today_usage

    def sync_external(self, usage: float, models: dict | None = None) -> float:
        """用外部权威值（平台用量接口）同步今日已用，并重算偏离值。

        models 为平台按模型明细 {模型: {cost, tokens}} 时一并同步：
        之后各模型展示值 = 平台值 + 上次同步后新发生的本地消耗。
        """
        try:
            u = float(usage)
        except (TypeError, ValueError):
            return self.today_usage
        if not math.isfinite(u):
            return self.today_usage
        with self._lock:
            self.sync_from_disk()
            if self.data.get("date") != _today_key():
                self._rollover()
            self.data["todayUsage"] = u
            snap = {str(k): _norm_model_rec(v)
                    for k, v in (self.data.get("models") or {}).items()}
            if isinstance(models, dict) and models:
                plat = {}
                for k, v in models.items():
                    rec = _norm_model_rec(v)
                    plat[str(k)] = {"cost": rec["cost"], "tokens": rec["tokens"]}
                self.data["modelsPlatform"] = plat
                self.data["modelsSync"] = snap
            else:
                # 没有按模型数据：全部按本地统计展示
                self.data["modelsPlatform"] = {}
                self.data["modelsSync"] = {}
            self._sync_deviation()
            self._save()
        return self.today_usage

    @property
    def today_usage(self) -> float:
        """今日已用（展示值）= 权威值 + 上次同步后新发生的每轮消耗。"""
        with self._lock:
            return self._display_usage()

    @property
    def deviation(self) -> float:
        """偏离值：最后一次同步时 实际消耗 - 每轮统计（可为负）。"""
        return float(self.data.get("deviation") or 0.0)

    @property
    def turn_cost(self) -> float:
        return float(self.data.get("turnCost") or 0.0)

    @property
    def day_start(self):
        """当日资金初始值（当天首次观测到的余额，未观测时为 None）。"""
        return self.data.get("dayStart")

    # ---- 预警档位（跨重启保留，跳天清零）----
    @property
    def alert_level(self) -> int:
        """今日“每日使用预警”已触发到第几档（0=尚未触发）。"""
        with self._lock:
            return int(self.data.get("alertLevel") or 0)

    def set_alert_level(self, level: int):
        with self._lock:
            self.data["alertLevel"] = max(0, int(level))
            self._save()

    # ---- 统计查询（账单对话框用）----
    def models_today(self) -> dict:
        """今日按模型汇总：{模型: {cost, tokens, turns}}。"""
        with self._lock:
            return self._models_breakdown()

    def daily_series(self, days: int = 7) -> list[dict]:
        """最近 days 天（含今天）的每日记录，按日期升序。

        每项：{date, usage, tokens, turns, models, is_today}；没有记录的日期
        也会给出空项，方便直接铺表格。
        """
        days = max(1, int(days))
        with self._lock:
            hist = self.data.get("history") or {}
            today = _today_key()
            now = datetime.now()
            out = []
            for i in range(days - 1, -1, -1):
                key = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                if key == today:
                    models, usage = self._models_breakdown(), self._display_usage()
                else:
                    rec = _norm_hist(hist.get(key))
                    models, usage = rec["models"], rec["usage"]
                out.append({
                    "date": key,
                    "usage": float(usage or 0.0),
                    "tokens": sum(int(m.get("tokens") or 0) for m in models.values()),
                    "turns": sum(int(m.get("turns") or 0) for m in models.values()),
                    "models": models,
                    "is_today": key == today,
                })
            return out

    def model_totals(self, days: int = 7) -> dict:
        """最近 days 天按模型合计（金额降序）：{模型: {cost, tokens, turns, days}}。"""
        tot: dict[str, dict] = {}
        for day in self.daily_series(days):
            for name, rec in day["models"].items():
                t = tot.setdefault(name, {"cost": 0.0, "tokens": 0,
                                          "turns": 0, "days": 0})
                cost = float(rec.get("cost") or 0.0)
                t["cost"] += cost
                t["tokens"] += int(rec.get("tokens") or 0)
                t["turns"] += int(rec.get("turns") or 0)
                if cost > 0 or rec.get("tokens"):
                    t["days"] += 1
        return dict(sorted(tot.items(), key=lambda kv: kv[1]["cost"], reverse=True))

    def range_total(self, days: int = 7) -> float:
        """最近 days 天合计金额（与账单表格同口径）。"""
        return sum(float(d["usage"]) for d in self.daily_series(days))


# ------------------------------------------------- 平台用量（实时·令牌模式）
def fetch_platform_usage(platform_token: str, timeout: float = 15.0) -> dict:
    """调用平台用量接口，返回 {amount, tokens} 或 {error}。"""
    token = (platform_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return {"error": "no platform token"}
    now = datetime.now()
    start = int(datetime(now.year, now.month, now.day).timestamp())
    end = start + 86400
    tz = -now.utcoffset().total_seconds() if now.utcoffset() else 0
    url = (f"https://platform.deepseek.com/api/v0/usage/by_api_key/amount"
           f"?start={start}&end={end}&tz={int(tz)}")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        u = compute_today_usage(data)
        if u and math.isfinite(u["amount"]):
            return u
        return {"error": "no usage"}
    except Exception as e:
        return {"error": str(e)[:200]}


def compute_today_usage(data: dict) -> dict | None:
    """从平台用量响应换算今日已用金额。

    data.data.biz_data.series[]: [{model, buckets:[{time, usage:{RESPONSE_TOKEN,
    PROMPT_CACHE_HIT_TOKEN, PROMPT_CACHE_MISS_TOKEN}}]}]
    """
    d = data
    if d and isinstance(d.get("data"), dict):
        bd = d["data"].get("biz_data")
        if isinstance(bd, dict) and isinstance(bd.get("series"), list):
            d = bd
        elif isinstance(d["data"].get("series"), list):
            d = d["data"]
    series = d.get("series") if isinstance(d, dict) else None
    if not series:
        return None
    cost = 0.0
    tokens = 0
    models: dict[str, dict] = {}
    found = False
    for s in series:
        if not isinstance(s, dict):
            continue
        model = str(s.get("model") or "")
        for b in s.get("buckets") or []:
            if not isinstance(b, dict):
                continue
            u = b.get("usage") or {}
            hit = float(u.get("PROMPT_CACHE_HIT_TOKEN") or 0)
            miss = float(u.get("PROMPT_CACHE_MISS_TOKEN") or 0)
            out = float(u.get("RESPONSE_TOKEN") or 0)
            if hit + miss + out == 0:
                continue
            found = True
            n = hit + miss + out
            c = usage_cost(model, hit, miss, out, time_sec=b.get("time"))
            tokens += n
            cost += c
            if model:
                rec = models.setdefault(model, {"cost": 0.0, "tokens": 0})
                rec["cost"] += c
                rec["tokens"] += int(n)
    return {"amount": cost, "tokens": tokens, "models": models} if found else None


def today_peak_now() -> bool:
    return is_peak_time(None)
