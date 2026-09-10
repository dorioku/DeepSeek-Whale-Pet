"""
峰谷定价模块 —— 从 DeepSeek-Balance-Whale-Widget 的 lib/index.js 移植。

DeepSeek CNY 价格（每百万 token）：[空闲时段价, 高峰时段价]。
高峰时段：工作日 9:00-12:00 与 14:00-18:00（北京时间）；
2026-08-23 起（北京时间）周末（周六/周日）全天按谷价。
DeepSeek 调价时修改下方常量即可。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

PEAK_HOURS = [(9, 12), (14, 18)]

BASE_PRICE = {"hit": [0.05, 0.1], "miss": [1.5, 3.0], "out": [4.5, 9.0]}
# deepseek-v4-pro 为 flash 的 3 倍价（官方 2026-08-17 生效）；vision-exp 与 flash 同价
PRO_PRICE = {"hit": [0.15, 0.3], "miss": [4.5, 9.0], "out": [13.5, 27.0]}

PRICING = {
    "deepseek-v4-flash-vision-exp": BASE_PRICE,
    "deepseek-v4-flash": BASE_PRICE,
    "deepseek-v4-pro": PRO_PRICE,
    "deepseek-chat": BASE_PRICE,
    "deepseek-reasoner": BASE_PRICE,
    "_default": BASE_PRICE,
}

# 北京时间 2026-08-23 00:00 的 epoch 秒（UTC 2026-08-22 16:00）
WEEKEND_VALLEY_FROM_SEC = math.floor(
    datetime(2026, 8, 22, 16, 0, 0, tzinfo=timezone.utc).timestamp()
)

BJ_TZ = timezone(timedelta(hours=8))


def price_for(model: str) -> dict:
    """返回该模型对应的 {hit, miss, out} 价格表。"""
    m = str(model or "").lower()
    for key, price in PRICING.items():
        if key == "_default":
            continue
        if key in m:
            return price
    return PRICING["_default"]


def is_peak_time(time_sec: float | int | None) -> bool:
    """判断某个 epoch 秒（或 None=当前时刻）是否处于高峰时段。"""
    if time_sec is None:
        now = datetime.now(BJ_TZ)
        return _is_peak_bj(now.year, now.month, now.day, now.hour)
    try:
        n = float(time_sec)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(n):
        return False
    bj = datetime.fromtimestamp(n, tz=BJ_TZ)
    return _is_peak_bj(bj.year, bj.month, bj.day, bj.hour)


def _is_peak_bj(year: int, month: int, day: int, hour: int) -> bool:
    # 2026-08-23 起（北京时间）周末全天谷价；更早的历史仍按旧规则（工作日高峰）
    wk = datetime(year, month, day, tzinfo=BJ_TZ).weekday()  # 0=周一 6=周日
    dt_utc = datetime(year, month, day, hour, tzinfo=BJ_TZ).astimezone(timezone.utc)
    if dt_utc.timestamp() >= WEEKEND_VALLEY_FROM_SEC and wk >= 5:
        return False
    for start, end in PEAK_HOURS:
        if start <= hour < end:
            return True
    return False


def usage_cost(
    model: str,
    cache_hit_tokens: float = 0,
    cache_miss_tokens: float = 0,
    output_tokens: float = 0,
    reasoning_tokens: float = 0,
    time_sec: float | int | None = None,
    peak: bool | None = None,
) -> float:
    """按峰谷定价换算 token 成本（元，CNY）。

    cache_hit（缓存命中输入）-> hit 档；cache_miss（未命中输入）-> miss 档；
    output + reasoning -> out 档。peak 为 None 时按 time_sec/当前时刻判定。
    """
    p = price_for(model)
    if peak is None:
        peak = is_peak_time(time_sec)
    off = 1 if peak else 0
    cost = 0.0
    cost += (float(cache_hit_tokens or 0) / 1e6) * p["hit"][off]
    cost += (float(cache_miss_tokens or 0) / 1e6) * p["miss"][off]
    cost += (float(output_tokens or 0) + float(reasoning_tokens or 0)) / 1e6 * p["out"][off]
    return cost


def cost_from_usage(model: str, usage: dict, time_sec: float | int | None = None,
                    peak: bool | None = None) -> dict:
    """从 OpenAI 兼容 usage 结构计算成本。

    支持 DeepSeek 的 usage 字段：
      prompt_tokens / completion_tokens / total_tokens
      prompt_tokens_details.cached_tokens
      completion_tokens_details.reasoning_tokens
    返回 {cost, tokens:{prompt, completion, cache_hit, cache_miss, reasoning, total}}。
    """
    usage = usage or {}
    prompt = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    details_p = usage.get("prompt_tokens_details") or {}
    cache_hit = float(details_p.get("cached_tokens") or 0)
    if cache_hit == 0 and isinstance(usage.get("cache_read_tokens"), (int, float)):
        cache_hit = float(usage["cache_read_tokens"] or 0)

    cache_miss = max(0.0, prompt - cache_hit)
    if cache_miss == 0 and isinstance(usage.get("prompt_cache_miss_tokens"), (int, float)):
        cache_miss = float(usage["prompt_cache_miss_tokens"] or 0)

    details_c = usage.get("completion_tokens_details") or {}
    reasoning = float(details_c.get("reasoning_tokens") or 0)
    if reasoning == 0 and isinstance(usage.get("reasoning_tokens"), (int, float)):
        reasoning = float(usage["reasoning_tokens"] or 0)

    output = max(0.0, completion)
    cost = usage_cost(model, cache_hit, cache_miss, output, reasoning, time_sec, peak)
    return {
        "cost": cost,
        "tokens": {
            "prompt": prompt,
            "completion": completion,
            "cache_hit": cache_hit,
            "cache_miss": cache_miss,
            "reasoning": reasoning,
            "total": prompt + completion,
        },
    }
