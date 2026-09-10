"""
余额拉取 + 小鲸鱼记账（移植自 DeepSeek-Balance-Whale-Widget lib/index.js）。

- fetch_balance: 调 DeepSeek 官方接口 GET /user/balance，优先选 CNY 且余额 > 0。
- Ledger: 每次观测余额后按“余额差值”累计今日已用；跨天自动归零并归档（保留 30 天）；
  币种变化时只重置基准、不记差值（防止多币种切换污染账本）。
- fetch_platform_usage: 可选“实时·令牌”模式，调平台用量接口按峰谷定价换算今日已用。
"""
from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request
from datetime import datetime
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
def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Ledger:
    """小鲸鱼记账：余额差值累计今日已用，跨天归档，跨重启继续累加。

    设计（“即使没长期挂着也能对上消耗”）：
    - dayStart：当日首次观测到的余额（资金初始值），当天首次启动时就缓存下来；
    - todayUsage：余额差值累加的今日已用（权威值）——重启后拿上次观测值与
      当前余额作差，未挂载时段的消耗也能算进去；
    - turnCost：本地中转按每轮 usage 换算的累计消耗，每轮立刻叠加；
    - syncedTurnCost/deviation：上次同步（轮询/平台用量）时的 turnCost
      及其偏离值 = todayUsage - turnCost（正 = 实际比本地统计多）。

    展示值 = todayUsage + max(0, turnCost - syncedTurnCost)：
    两次轮询之间每轮消耗也能立刻看到，下一次同步时再修正。
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()
        self._lock = threading.Lock()

    def _load(self) -> dict:
        data = None
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("date"), str):
                data = loaded
        except (OSError, ValueError):
            pass
        if data is None:
            data = {"date": _today_key()}
        # 兼容旧账本：补齐新增字段
        data.setdefault("dayStart", None)
        data.setdefault("lastBalance", None)
        data.setdefault("lastCurrency", "")
        data.setdefault("todayUsage", 0.0)
        data.setdefault("turnCost", 0.0)
        data.setdefault("syncedTurnCost", 0.0)
        data.setdefault("deviation", 0.0)
        data.setdefault("history", {})
        return data

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError:
            pass

    def _rollover(self, day_start=None):
        """跳天：归档当日已用，重置基准与计数（资金初始值留待当日首次观测）。"""
        led = self.data
        if led.get("date") and isinstance(led.get("todayUsage"), (int, float)):
            led.setdefault("history", {})[led["date"]] = led["todayUsage"]
        led.update({
            "date": _today_key(),
            "dayStart": day_start,
            # 没有观测值就清空基准，避免拿昨天的余额跟今天作差（会误记一笔）
            "lastBalance": day_start,
            "todayUsage": 0.0,
            "turnCost": 0.0,
            "syncedTurnCost": 0.0,
            "deviation": 0.0,
        })

    def _sync_deviation(self):
        """同步：把当前每轮累计当作基准，算出与实际消耗的偏离值。"""
        tc = float(self.data.get("turnCost") or 0.0)
        self.data["syncedTurnCost"] = tc
        self.data["deviation"] = float(self.data.get("todayUsage") or 0.0) - tc

    def record(self, balance: float, currency: str) -> dict:
        """观测一次余额：维护当日初始值/差值累计，并同步偏离值。"""
        with self._lock:
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
            # 历史只保留最近 30 天
            hist = led.setdefault("history", {})
            for k in sorted(hist.keys())[:-30]:
                del hist[k]
            self._sync_deviation()
            self._save()
            return led

    def add_turn_cost(self, cost: float) -> float:
        """每轮对话的消耗立刻叠加（不等轮询），返回叠加后的今日已用展示值。"""
        try:
            c = float(cost)
        except (TypeError, ValueError):
            return self.today_usage
        if not math.isfinite(c):
            return self.today_usage
        with self._lock:
            if self.data.get("date") != _today_key():
                self._rollover()
            self.data["turnCost"] = float(self.data.get("turnCost") or 0.0) + c
            self._save()
        return self.today_usage

    def sync_external(self, usage: float) -> float:
        """用外部权威值（平台用量接口）同步今日已用，并重算偏离值。"""
        try:
            u = float(usage)
        except (TypeError, ValueError):
            return self.today_usage
        if not math.isfinite(u):
            return self.today_usage
        with self._lock:
            if self.data.get("date") != _today_key():
                self._rollover()
            self.data["todayUsage"] = u
            self._sync_deviation()
            self._save()
        return self.today_usage

    @property
    def today_usage(self) -> float:
        """今日已用（展示值）= 权威值 + 上次同步后新发生的每轮消耗。"""
        base = float(self.data.get("todayUsage") or 0.0)
        pending = (float(self.data.get("turnCost") or 0.0)
                   - float(self.data.get("syncedTurnCost") or 0.0))
        return base + max(0.0, pending)

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
    found = False
    for s in series:
        if not isinstance(s, dict):
            continue
        model = s.get("model", "")
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
            tokens += hit + miss + out
            cost += usage_cost(model, hit, miss, out, time_sec=b.get("time"))
    return {"amount": cost, "tokens": tokens} if found else None


def today_peak_now() -> bool:
    return is_peak_time(None)
