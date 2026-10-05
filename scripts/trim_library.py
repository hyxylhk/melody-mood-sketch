"""
曲库裁剪工具：把整首歌裁成"30~45 秒短旋律片段"。

为什么要做这件事
================
1. 作品介绍里承诺的是"**短氛围旋律片段**"，但曲库里现在是整首歌
   （每首 1.7~8MB、时长好几分钟），和本地合成的 24 秒片段体验完全不一致。
2. 34 首整首 = 185MB，**塞不进 Git 仓库**，公网部署（Render）根本带不走。
   裁成 17 首 × 35 秒 ≈ 10MB，仓库秒 clone，云端也有真歌可听。

裁剪策略（不是傻瓜式从头截）
==========================
直接从头截 35 秒很可能截到前奏的安静段，听起来"没内容"。
所以这里做一步**能量分析**：把整首歌解码成低采样率 PCM，
按秒计算 RMS（能量），滑动找出**连续 35 秒能量最高**的那一段，
通常就是副歌/主旋律段。这样截出来的片段"一上来就有东西"。

用法
====
    python scripts/trim_library.py --dry-run    # 先看方案
    python scripts/trim_library.py              # 正式裁剪

常用参数
========
    --seconds 35            每段多长（默认 35 秒）
    --per-emotion 1         每种情绪保留几首（默认 1 首 → 17 首）
    --bitrate 128k          输出码率（默认 128k，音质和体积的平衡点）
    --no-backup             不备份原文件（默认会把整首歌移到 library_src/）

安全性
======
先裁到临时目录，全部成功后再替换，**中途失败不会破坏原曲库**。
原整首歌默认移到 storage/library_src/（已被 .gitignore 排除，不进仓库），
想要随时可以再跑一遍换参数重截。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

# 让脚本能 import backend.*（直接 python scripts/xxx.py 运行时，项目根目录不在 sys.path 里）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.emotion_catalog import EMOTION_CATALOG  # noqa: E402
from scripts.build_free_library import (  # noqa: E402
    _safe,
    load_pieces,
    plan_library,
)

LIBRARY_DIR = PROJECT_ROOT / "storage" / "library"
BACKUP_DIR = PROJECT_ROOT / "storage" / "library_src"

# build_free_library.py 当时是按"每种情绪 2 首"下载的，规划时必须沿用这个数字
# （原因见 pick_targets 的注释），否则会匹配到磁盘上不存在的文件。
DOWNLOADED_PER_EMOTION = 2

# 能量分析用的低采样率：8kHz 单声道足够判断"哪段响"，解码快、内存小
_ANALYZE_RATE = 8000


def _ffmpeg() -> str:
    """找到 ffmpeg 可执行文件；找不到就给出明确的安装提示。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit(
            "未找到 ffmpeg。本工具依赖它做音频解码与截取。\n"
            "  Windows:  winget install ffmpeg  （或去 gyan.dev 下载解压后加 PATH）\n"
            "  macOS:    brew install ffmpeg\n"
            "  Ubuntu:   sudo apt install ffmpeg"
        )
    return exe


def probe_duration(ffmpeg: str, path: Path) -> float:
    """读取音频总时长（秒）。用于 sanity check，避免截超出长度的片段。"""
    out = subprocess.run(  # noqa: S603
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        stderr=subprocess.STDOUT,
    )
    # ffmpeg 不直接给时长，用 -show_entries 更稳
    probe = subprocess.run(  # noqa: S603
        [
            ffmpeg, "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    text = (probe.stderr or "") + (out.stdout or "")
    # 从 "time=00:01:23.45" 这种进度输出里取最后时间点
    last = 0.0
    for token in text.replace("\r", "\n").split("\n"):
        if "time=" in token:
            part = token.split("time=", 1)[1].split(" ")[0]
            try:
                hh, mm, ss = part.split(":")
                last = max(last, int(hh) * 3600 + int(mm) * 60 + float(ss))
            except ValueError:
                continue
    return last


def per_second_energy(ffmpeg: str, path: Path) -> list[float]:
    """
    把音频解码成 8kHz 单声道 PCM，返回**每秒的 RMS 能量**列表。

    RMS（均方根）可以粗理解为"这一段听起来有多响"。
    安静的前奏 RMS 低，副歌 RMS 高 —— 我们要找的就是高能量区间。
    """
    proc = subprocess.run(  # noqa: S603
        [ffmpeg, "-v", "quiet", "-i", str(path),
         "-ac", "1", "-ar", str(_ANALYZE_RATE), "-f", "s16le", "-"],
        capture_output=True,
    )
    raw = proc.stdout
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])  # 截掉可能多出来的半个字节
    if not samples:
        return []

    energies: list[float] = []
    n = len(samples)
    for start in range(0, n, _ANALYZE_RATE):
        chunk = samples[start: start + _ANALYZE_RATE]
        if not chunk:
            break
        total = 0
        for value in chunk:
            total += value * value
        energies.append((total / len(chunk)) ** 0.5)
    return energies


def best_start(energies: list[float], clip_seconds: int) -> float:
    """
    滑动窗口找"连续 clip_seconds 秒总能量最高"的起点。

    用**增量滑动**而不是每段重新求和：窗口右移一秒时，
    只需加上新一秒、减掉离开窗口的那一秒，复杂度 O(总秒数)。
    """
    window = max(1, int(clip_seconds))
    if len(energies) <= window:
        return 0.0  # 整首还没片段长，那就从 0 开始

    current = sum(energies[:window])
    best_value = current
    best_index = 0
    for i in range(window, len(energies)):
        current += energies[i] - energies[i - window]
        if current > best_value:
            best_value = current
            best_index = i - window + 1
    return float(best_index)


def trim(ffmpeg: str, src: Path, dest: Path, start: float, seconds: int, bitrate: str) -> bool:
    """调用 ffmpeg 截取片段并重编码为固定码率 mp3。"""
    cmd = [  # noqa: S603
        ffmpeg, "-v", "error", "-y",
        "-ss", f"{start:.2f}",              # -ss 放在 -i 前 = 快速定位，秒级文件几乎瞬间
        "-t", str(seconds),
        "-i", str(src),
        "-vn",                               # 丢掉封面图等视频流，纯音频更小
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", "44100",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size < 50_000:
        print(f"    ! 截取失败: {src.name}\n      {result.stderr.strip()[:200]}")
        return False
    return True


def pick_targets(per_emotion: int) -> dict[str, list[Path]]:
    """
    决定每种情绪用哪几首。

    复用 build_free_library.plan_library() 的打分逻辑（feel 标签×3 +
    描述关键词×2 + BPM 区间×2），取每种情绪的**最高分**那几首，
    保证留下的是最贴情绪的一首，而不是随便挑。

    ⚠️ 一个容易踩的坑（这里特意绕开）：
        plan_library 内部有"同一首歌不能被多个情绪复用"的去重逻辑，
        所以 per_emotion=1 和 per_emotion=2 挑出来的 **Top1 可能不是同一首**
        ——前面情绪少占用一首，后面情绪的排序就会变。
        但磁盘上已经下载好的文件是按 `DOWNLOADED_PER_EMOTION=2` 那套方案下的，
        所以这里必须**按 2 来规划、再取前 N 首**，否则会匹配到根本不存在的文件。
    """
    plan = plan_library(load_pieces(), max(per_emotion, DOWNLOADED_PER_EMOTION))
    targets: dict[str, list[Path]] = {}
    for code, tracks in plan.items():
        picked: list[Path] = []
        for track in tracks[:per_emotion]:
            path = LIBRARY_DIR / f"{code}_{_safe(track['title'])}.mp3"
            if path.exists():
                picked.append(path)
        if picked:
            targets[code] = picked
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="把曲库整首歌裁成短旋律片段")
    parser.add_argument("--seconds", type=int, default=35, help="片段长度（秒，默认 35）")
    parser.add_argument("--per-emotion", type=int, default=1, help="每种情绪保留几首（默认 1）")
    parser.add_argument("--bitrate", default="128k", help="输出码率（默认 128k）")
    parser.add_argument("--dry-run", action="store_true", help="只打印方案，不动文件")
    parser.add_argument("--no-backup", action="store_true", help="不备份原整首歌")
    args = parser.parse_args()

    ffmpeg = _ffmpeg()
    targets = pick_targets(args.per_emotion)

    missing = sorted(set(EMOTION_CATALOG) - set(targets))
    print(f"曲库裁剪方案：{len(targets)} 种情绪 × {args.per_emotion} 首，"
          f"每段 {args.seconds} 秒，码率 {args.bitrate}")
    if missing:
        print(f"⚠ 以下情绪在曲库中找不到对应文件（将回退本地合成）: {', '.join(missing)}")
    print("-" * 68)

    if args.dry_run:
        for code, paths in sorted(targets.items()):
            for path in paths:
                size_mb = path.stat().st_size / 1024 / 1024
                print(f"  {code:<12} {path.name}  ({size_mb:.1f}MB 整首)")
        print("-" * 68)
        print("（--dry-run，未改动任何文件）")
        return 0

    # 第 1 步：全部裁到临时目录，成功后再统一替换 —— 中途失败不破坏原库
    staging = Path(tempfile.mkdtemp(prefix="trim_stage_"))
    done: list[tuple[str, Path, Path]] = []   # (情绪, 源文件, 临时产物)

    for code, paths in sorted(targets.items()):
        for src in paths:
            energies = per_second_energy(ffmpeg, src)
            if not energies:
                print(f"  ✘ {code:<12} 解码失败，跳过")
                continue
            start = best_start(energies, args.seconds)
            out = staging / src.name
            if not trim(ffmpeg, src, out, start, args.seconds, args.bitrate):
                continue
            kb = out.stat().st_size // 1024
            print(f"  ✔ {code:<12} 起点 {start:5.0f}s / 全长 {len(energies):3.0f}s → "
                  f"{args.seconds}s，{kb}KB")
            done.append((code, src, out))

    if not done:
        shutil.rmtree(staging, ignore_errors=True)
        print("没有任何片段裁剪成功，原曲库保持不变。")
        return 1

    # 第 2 步：把未选中的整首歌（以及即将被替换的原文件）移到备份目录
    kept_names = {src.name for _, src, _ in done}
    if not args.no_backup:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        moved = 0
        for path in LIBRARY_DIR.glob("*.mp3"):
            if path.name not in kept_names:
                shutil.move(str(path), str(BACKUP_DIR / path.name))
                moved += 1
        print(f"\n已备份 {moved} 首未选中的整首歌 → {BACKUP_DIR.relative_to(PROJECT_ROOT)}/")

    # 第 3 步：用裁剪后的片段替换原文件
    for code, src, out in done:
        shutil.move(str(out), str(LIBRARY_DIR / src.name))

    shutil.rmtree(staging, ignore_errors=True)

    total_mb = sum(p.stat().st_size for p in LIBRARY_DIR.glob("*.mp3")) / 1024 / 1024
    print(f"\n完成：曲库现有 {len(list(LIBRARY_DIR.glob('*.mp3')))} 首片段，共 {total_mb:.1f}MB"
          f"（原 185MB）")
    print("提示：重启服务后生效；别忘了更新 ATTRIBUTION.txt 里的说明。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
