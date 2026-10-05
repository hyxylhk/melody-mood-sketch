"""
==============================================================================
backend/services/music_library.py —— 官方素材曲库（按情绪自动选歌）
==============================================================================
背景：
    大赛官方提供了一批"可用素材歌单"。把其中你喜欢的音频文件下载下来，
    放进 storage/library/ 目录，系统就会在生成配乐时**优先**从这里挑一首
    情绪匹配的曲子直接播放 —— 好听、稳定、零耗时、零 API 成本。

文件命名规则（重要）：
    {情绪代号}_{任意名字}.{后缀}

    例如：
        joy_01_晴天.mp3          → 情绪=joy（喜悦）
        sadness_深夜电台.mp3     → 情绪=sadness（悲伤）
        stress_雨夜加班.mp3      → 情绪=stress（压力大）

    支持的音频后缀：.mp3 / .m4a / .wav / .flac / .ogg
    情绪代号必须与 backend/services/emotion_catalog.py 里的 key 一致（共 17 种）：
        joy, sadness, anger, anxiety, loneliness, fatigue, nostalgia,
        love, excitement, calmⒶ ... 见下方"注意"
    ⚠️ 注意：真实可用的 key 以 EMOTION_CATALOG 为准，当前是：
        joy, sadness, anger, anxiety, loneliness, fatigue, nostalgia, love,
        excitement, gratitude, hope, moved, relief, stress, tenderness,
        confusion, neutral
    （没有 calm / lonely 这类俗称，写错的前缀会归入 unknown 兜底桶）

    前缀不是有效情绪代号的文件（比如 01_ intro.mp3）会被归入 "unknown" 桶，
    只在找不到精确匹配时随机兜底使用；曲库完全为空则走原来的本地合成。

为什么用"文件名约定"而不是建一张数据库表？
    黑客松场景下，往文件夹里丢文件是最直观的操作方式 ——
    不需要任何管理界面，拖进去立刻生效（每次选歌时重新扫描，无需重启）。
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Dict, List, Optional

from backend.config import get_settings
from backend.services.emotion_catalog import EMOTION_CATALOG

# 曲库支持的音频后缀（大写小写都会被兼容，扫描时会统一转小写）
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".ogg"}

# 无效文件名字符（Windows 禁止出现在文件名里的字符），选择曲目时用于清洗标题
_UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')
# 文件名里紧跟情绪前缀之后的"序号"（joy_02_xxx → 02），提取标题时要剥掉
_LEADING_INDEX = re.compile(r"^[\d]+[\s_\-]*")


def _scan_library(library_dir: Path) -> Dict[str, List[Path]]:
    """
    扫描曲库目录，返回 {情绪代号: [文件路径, ...]} 的索引。

    每次选歌都重新扫描（曲库文件通常就几十个，开销可以忽略），
    好处是：往文件夹里加/删文件**立即生效**，不需要重启服务。
    """
    buckets: Dict[str, List[Path]] = {}

    if not library_dir.exists():
        return buckets

    for entry in sorted(library_dir.rglob("*")):
        # rglob 递归遍历（子文件夹里的也算），跳过临时文件和隐藏文件
        if not entry.is_file():
            continue
        if entry.name.startswith(".") or entry.name.startswith("~$"):
            continue
        if entry.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        # 解析文件名前缀：取第一个下划线之前的部分作为情绪代号
        prefix = entry.stem.split("_", 1)[0].strip().lower()
        code = prefix if prefix in EMOTION_CATALOG else "unknown"
        buckets.setdefault(code, []).append(entry)

    return buckets


def pick_library_track(emotion_code: str, library_dir: Optional[Path] = None) -> Optional[Dict[str, object]]:
    """
    按情绪从曲库挑一首曲子。

    匹配优先级：
        1. 精确匹配：文件名前缀 == 情绪代号（joy 日记优先挑 joy_ 前缀的曲子）
        2. 模糊兜底：unknown 桶里随便挑一首
        3. 都没有  → 返回 None（上层自动降级为本地合成）

    返回结构（给上层直接写进 MusicTrack）：
        {
            "audio_url": "/media/library/joy_01_晴天.mp3",
            "file_path": "C:/.../storage/library/joy_01_晴天.mp3",
            "title":     "晴天",
            "emotion":   "joy",
            "meta": {...},      # 存进 meta_json 的展示信息
        }
    """
    settings = get_settings()
    lib_dir = Path(library_dir) if library_dir is not None else settings.library_dir
    buckets = _scan_library(lib_dir)

    candidates = buckets.get(emotion_code) or buckets.get("unknown") or []
    if not candidates:
        return None

    picked = random.choice(candidates)

    # 从文件名提取展示标题：
    #   joy_晴天.mp3      → "晴天"
    #   joy_02_晴天.mp3   → "晴天"（情绪前缀后的序号也要剥掉）
    #   sadness_深夜.mp3  → "深夜"
    stem = picked.stem
    parts = stem.split("_", 1)
    rest = parts[1].strip() if len(parts) > 1 else stem
    rest = _LEADING_INDEX.sub("", rest).strip() or stem
    title = _UNSAFE_NAME_CHARS.sub("", rest) or stem

    return {
        "audio_url": f"/media/library/{picked.name}",
        "file_path": str(picked.resolve()),
        "title": title,
        "emotion": emotion_code,
        "meta": {
            "source": "官方素材曲库",
            "title": title,
            "file_name": picked.name,
            "matched_emotion": emotion_code,
        },
    }


def library_stats(library_dir: Optional[Path] = None) -> Dict[str, object]:
    """
    曲库概况（健康检查接口用）：
        { "track_count": 12, "by_emotion": {"joy": 3, "sadness": 2, "unknown": 7} }
    """
    settings = get_settings()
    lib_dir = Path(library_dir) if library_dir is not None else settings.library_dir
    buckets = _scan_library(lib_dir)
    return {
        "track_count": sum(len(v) for v in buckets.values()),
        "by_emotion": {k: len(v) for k, v in sorted(buckets.items())},
    }
