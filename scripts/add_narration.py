"""
============================================================================
scripts/add_narration.py —— 给演示视频配上语音旁白
============================================================================
做什么
======
读 record_demo.py 录好的字幕时间轴（docs/caption_timeline.json），
用微软在线语音（edge-tts，中文女声）把每条字幕念出来，按时间轴对齐，
最后和无声视频合成出带语音的最终版。

为什么旁白文案不直接用字幕原文
==============================
字幕停留只有 3~4.5 秒，而字幕原文往往 20 多字，按正常语速要念 5 秒以上，
会和下一条旁白重叠成一团。所以每条配一句 8~15 字的短旁白，
念完 2~3 秒，画面字幕负责"完整信息"，旁白负责"关键节奏"。

用法
====
    python scripts/record_demo.py        # 先录视频（会生成字幕时间轴）
    python scripts/add_narration.py      # 再配音

依赖
====
    pip install --user edge-tts
    ffmpeg（脚本里写死了本机路径，换机器要改 FFMPEG）

产物
====
    docs/demo_video.mp4   —— 覆盖成带语音的最终版
    docs/demo_video_silent.mp4 —— 无声原版（脚本会自动备份）
"""

from __future__ import annotations

import asyncio
import json
import subprocess  # noqa: S404
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS = PROJECT_ROOT / "docs"
TIMELINE = DOCS / "caption_timeline.json"
SILENT = DOCS / "demo_video_silent.mp4"
FINAL = DOCS / "demo_video.mp4"
TMP = PROJECT_ROOT / ".narration"
FFMPEG = r"C:\Program Files\Tecplot\Tecplot 360 EX 2024 R1\bin\ffmpeg"
VOICE = "zh-CN-XiaoxiaoNeural"     # 微软中文女声，自然度够用
RATE = "+12%"                       # 稍微加速，避免和下一条旁白撞车

# 每条字幕对应的旁白（顺序与 record_demo.py 里的幕次一致）
NARRATION = [
    "旋律情绪速写，一款 AI 音乐情绪日记本",
    "文字日记，留不住那一天的感受",
    "写下心情，AI 读懂情绪，配上专属旋律",
    "打开就能写下此刻的心情",
    "30 天情绪日历，起伏一目了然",
    "效价折线，记录低落与回升",
    "现在，写下此刻的心情",
    "点一下，AI 识别 17 类情绪",
    "曲库智能匹配，这就是今天的主打歌",
    "每篇日记，都变成文字加旋律的卡片",
    "删掉一篇，抖动后碎成粒子消散",
    "FastAPI 加 SQLite，零构建可离线",
    "让每个人，都能听见自己的心情",
    "旋律情绪速写，谢谢观看",
]


async def synth_all(lines: list[str]) -> list[Path]:
    """把每条旁白合成成 mp3，返回文件路径列表。"""
    import edge_tts

    TMP.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for i, text in enumerate(lines):
        path = TMP / f"n{i:02d}.mp3"
        if path.exists():          # 已经合成过就复用，重跑时省时间
            out.append(path)
            print(f"  [{i + 1}/{len(lines)}] {text}（复用缓存）")
            continue
        comm = edge_tts.Communicate(text, VOICE, rate=RATE)
        await comm.save(str(path))
        out.append(path)
        print(f"  [{i + 1}/{len(lines)}] {text}")
    return out


def main() -> int:
    if not TIMELINE.exists():
        print("✘ 没找到字幕时间轴，先跑 scripts/record_demo.py")
        return 1
    if not SILENT.exists():
        print("✘ 没找到无声原版视频 docs/demo_video_silent.mp4")
        return 1

    timeline = json.loads(TIMELINE.read_text(encoding="utf-8"))
    print(f"字幕 {len(timeline)} 条，开始合成旁白…")

    # 旁白条数不够就按可用条数截断，多了就忽略（不报索引错误）
    count = min(len(timeline), len(NARRATION))
    paths = asyncio.run(synth_all(NARRATION[:count]))

    # ---------- 用 ffmpeg 按时间轴把 14 段旁白拼成一条音轨 ----------
    # adelay 让每段在正确时刻开口；amix 把它们混在一起（normalize=0 保持原音量）
    inputs: list[str] = ["-i", str(SILENT)]
    for p in paths:
        inputs += ["-i", str(p)]

    delays = [int(item["at_ms"]) for item in timeline[:count]]
    parts = []
    labels = []
    for i, d in enumerate(delays):
        parts.append(f"[{i + 1}:a]adelay={d}|{d}[a{i}]")
        labels.append(f"[a{i}]")
    # 注意：本机 ffmpeg 版本较老，amix 不支持 normalize=0（会把音量除以输入数）。
    # 所以后面补一个 volume 把音量乘回来；再挂 dynaudnorm 压一下偶发的叠加峰值。
    parts.append(f"{''.join(labels)}amix=inputs={count},volume={count},dynaudnorm=f=150[aout]")

    cmd = (
        [FFMPEG, "-v", "error", "-y"]
        + inputs
        + [
            "-filter_complex", ";".join(parts),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy",              # 画面原样复制，不重新编码
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-shortest",
            str(FINAL),
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        print("ffmpeg 合成失败:", result.stderr[:400])
        return 1

    for p in paths:
        p.unlink(missing_ok=True)
    try:
        TMP.rmdir()
    except OSError:
        pass

    print(f"\n✔ 带语音版已生成：{FINAL.name}"
          f"（{FINAL.stat().st_size / 1024 / 1024:.1f}MB）")
    print(f"  无声原版保留在：{SILENT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
