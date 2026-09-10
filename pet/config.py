"""
配置管理 —— 桌宠独立程序。

配置持久化为 JSON。搜索顺序（可被环境变量覆盖）：
  1. WHALE_PET_CONFIG_PATH 显式指定的路径
  2. exe/脚本同目录下 config.json（便携模式：存在即用）
  3. %APPDATA%/WhalePet/config.json（默认）
首次启动（无 api_key）时由 GUI 弹出设置对话框，填入后缓存到磁盘。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    # DeepSeek API Key（必填）：用于拉取余额 + 中转转发
    "api_key": "",
    # 中转服务监听地址（OpenAI 兼容，类似 DSH 的会话监听）
    "proxy_enabled": True,
    "proxy_host": "127.0.0.1",
    "proxy_port": 11434,  # 惯例端口，可用菜单修改
    # DeepSeek 官方 API 地址（可换中转/镜像）
    "api_base": "https://api.deepseek.com",
    # 今日已用模式：ledger=小鲸鱼记账（余额差值）/ token=平台令牌实时
    "usage_mode": "ledger",
    # DeepSeek 平台网页会话令牌（可选，usage_mode=token 时使用）
    "platform_token": "",
    # 挂件外观/行为
    "scale": 1.5,
    "sound_set": "duck",       # duck=小黄鸭 / fx1=音效1
    "volume": 0.9,
    "bubble_on": True,
    "turn_cost_on": True,
    "turn_cost_close_ms": 5000,  # 0=不自动关闭
    "peak_mode": "default",      # default / liangwen / qiangqiang
    "pos": {"h": "right", "v": "bottom", "hOff": 0, "vOff": 0, "x": None, "y": None},
    # 预警：余额低于阈值 / 今日已用超过阈值（0=关闭该项阈值判断）
    "alert_balance_on": True,
    "alert_balance": 10.0,
    "alert_daily_on": True,
    "alert_daily": 10.0,
    # 记账账本与历史（跨天自动归档）
    "ledger": {"date": "", "lastBalance": None, "lastCurrency": "", "todayUsage": 0.0,
               "history": {}},
}


def _appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "WhalePet"


def _portable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    env = os.environ.get("WHALE_PET_CONFIG_PATH")
    if env:
        return Path(env)
    portable = _portable_dir() / "config.json"
    if portable.exists():
        return portable
    return _appdata_dir() / "config.json"


def ledger_path() -> Path:
    """账本（缓存）路径：与 config.json 同目录。

    统一成一份缓存，源码运行与打包版看到的是**同一个**“今日已用”：
      1. WHALE_PET_LEDGER_PATH 显式指定
      2. 否则 config.json 所在目录（便携 = exe/项目目录，否则 %APPDATA%/WhalePet/）
    注意 PyInstaller onefile：运行期的 __file__ 在临时解包目录，若按“源码目录”推断
    会把账本写进临时目录、退出即丢，长期统计无从谈起。
    """
    env = os.environ.get("WHALE_PET_LEDGER_PATH")
    if env:
        return Path(env)
    return config_path().parent / "ledger.json"


def legacy_ledger_paths() -> list[Path]:
    """旧位置的账本 / 旧版缓存（启动时一次性并入，见 balance.Ledger._migrate）。"""
    primary = ledger_path()
    out: list[Path] = []
    seen = set()
    for p in (_portable_dir() / "ledger.json", _appdata_dir() / "usage.json"):
        try:
            key = str(p.resolve()).lower()
        except OSError:
            key = str(p).lower()
        if key in seen or p == primary or not p.exists():
            continue
        seen.add(key)
        out.append(p)
    return out


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    p = config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    # 深合并默认值：兼容旧版本配置缺字段
    cfg = _deep_merge(DEFAULTS, data)
    return cfg


def save_config(cfg: dict) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 只保存非默认的键，避免把 DEFAULTS 原样写盘（历史/账本等仍会保留）
    data = _deep_merge(DEFAULTS, cfg)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def is_first_run(cfg: dict) -> bool:
    return not bool(cfg.get("api_key"))


if __name__ == "__main__":
    c = load_config()
    print(json.dumps(c, ensure_ascii=False, indent=2))
    print("config file:", config_path())
    print("first run:", is_first_run(c))
