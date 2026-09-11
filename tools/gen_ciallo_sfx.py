"""生成 assets/ciallo.wav —— 台词「Ciallo～(∠・ω< )⌒☆」的专属小音效（合成占位，可替换）。

用法：
    python tools/gen_ciallo_sfx.py

想换成自己的声音：丢 `assets/ciallo.<格式>`（更推荐放用户目录 sounds/，不会被 git 跟踪），
支持 wav/mp3/aac/m4a/ogg/flac —— 同一目录按此顺序取第一个，所以已换成 ciallo.aac 后
别再重跑本脚本（写出的 ciallo.wav 会盖过 .aac）。桌宠在显示带 "sfx" 字段的台词时播放它。
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SR = 44100
OUT = Path(__file__).resolve().parent.parent / "assets" / "ciallo.wav"

# 四连上行小铃铛（ci-al-lo~ 的节奏感）：(频率 Hz, 时长 s)
NOTES = [(1046.50, 0.06), (1318.51, 0.06), (1567.98, 0.06), (2093.00, 0.26)]
# 铃音泛音（2.01 倍频略微失谐，听感更像小铃铛而不是纯正弦）
PARTIALS = [(1.0, 1.0), (2.01, 0.42), (2.99, 0.18), (4.16, 0.07)]
ATTACK = 0.004      # 起音淡入，避免爆音
DECAY = 10.5        # 指数衰减速度
TAIL_FADE = 0.03    # 结尾淡出，避免截断声
GAIN = 0.85


def render() -> bytes:
    samples: list[float] = []
    for freq, dur in NOTES:
        n = int(SR * dur)
        for i in range(n):
            t = i / SR
            env = min(1.0, t / ATTACK) * math.exp(-t * DECAY)
            s = sum(amp * math.sin(2 * math.pi * freq * mul * t)
                    for mul, amp in PARTIALS)
            samples.append(env * s)
    peak = max(abs(v) for v in samples) or 1.0
    fade = int(SR * TAIL_FADE)
    total = len(samples)
    out = bytearray()
    for i, v in enumerate(samples):
        g = GAIN / peak
        if i > total - fade:
            g *= (total - i) / fade
        v = max(-1.0, min(1.0, v * g))
        out += struct.pack("<h", int(v * 32767))
    return bytes(out)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(render())
    print(f"written: {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
