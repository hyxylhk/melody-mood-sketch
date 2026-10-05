"""
==============================================================================
scripts/build_free_library.py —— 公版免费曲库构建器（自动选曲 + 下载 + 打标）
==============================================================================
背景：
    大赛官方 Q2 明确说：Demo 可以使用"公版免费歌曲"。
    本脚本从 Kevin MacLeod 的 incompetech 曲库（1443 首，全部为
    Creative Commons: By Attribution 4.0 许可，免费商用，需署名）按情绪自动选曲，
    下载进 storage/library/，命名成曲库约定的 {情绪代号}_{曲名}.mp3。

为什么选它而不是别的免费曲库？
    1) 许可干净：CC-BY 4.0，允许商用，唯一义务是署名（脚本会生成署名文件）
    2) 全部器乐无人声 —— 正适合当日记背景音乐
    3) 官方开放 pieces.json 元数据：每首都有 feel(情绪标签)、BPM、时长、描述，
       我们能据此做**可解释的**情绪匹配，正好呼应项目的核心创新点

怎么用：
    python scripts/build_free_library.py --plan          # 只看选曲方案，不下载
    python scripts/build_free_library.py                 # 下载全部（断点续传，可反复跑）
    python scripts/build_free_library.py --per-emotion 1 # 每种情绪只要 1 首
    python scripts/build_free_library.py --parallel 6    # 并发数（默认 4）

署名义务（CC-BY 4.0）：
    脚本会在曲库目录生成 ATTRIBUTION.txt，答辩/演示页面引用本曲库时保留即可。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.emotion_catalog import EMOTION_CATALOG  # noqa: E402

PIECES_URL = "https://incompetech.com/music/royalty-free/pieces.json"
MP3_BASE = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
PIECES_CACHE = PROJECT_ROOT / "storage" / "pieces.json"
LIBRARY_DIR = PROJECT_ROOT / "storage" / "library"
ATTRIBUTION_FILE = LIBRARY_DIR / "ATTRIBUTION.txt"

CREDIT = (
    "本目录音乐素材来自 Kevin MacLeod (incompetech.com)，\n"
    "Licensed under Creative Commons: By Attribution 4.0 License\n"
    "http://creativecommons.org/licenses/by/4.0/\n"
    "（比赛期间仅用于赛事评审演示，不作商业用途、不对外发布上线。）\n"
)

# ---------------------------------------------------------------------------
# 情绪 → 匹配规则
# ---------------------------------------------------------------------------
# 每种情绪的匹配规则由三层信号加权得到：
#   feel        —— 官方情绪标签（权重最高，作者自己打的）
#   description —— 官方文字描述里的关键词
#   bpm / 时长  —— 数值约束，用来做二次筛选
RULES: Dict[str, dict] = {
    "joy": dict(feel=["humorous", "bright", "bouncy"],
                desc=["fun", "happy", "silly", "playful", "cheerful", "quirky"], bpm=(100, 180)),
    "excitement": dict(feel=["action", "driving", "grooving", "bouncy"],
                       desc=["energetic", "chase", "race", "fast", "sport"], bpm=(118, 200)),
    "love": dict(feel=["bright", "relaxed", "calming"],
                 desc=["romantic", "love", "wedding", "valentine", "affection"], bpm=(70, 120)),
    "tenderness": dict(feel=["calming", "relaxed"],
                       desc=["gentle", "soft", "quiet", "warm", "lullaby", "tender", "intimate"],
                       bpm=(55, 100)),
    "gratitude": dict(feel=["bright", "uplifting", "relaxed"],
                      desc=["wholesome", "family", "home", "thankful", "hometown"], bpm=(80, 130)),
    "hope": dict(feel=["uplifting", "bright"],
                 desc=["hopeful", "sunrise", "beginning", "journey", "rising", "inspired"],
                 bpm=(85, 135)),
    "moved": dict(feel=["epic", "uplifting"],
                  desc=["emotional", "majestic", "beautiful", "sweeping", "grand", "cinematic"],
                  bpm=(70, 140)),
    "relief": dict(feel=["relaxed", "calming"],
                   desc=["laid back", "lazy", "afternoon", "easy", "resolve", "breeze"],
                   bpm=(70, 115)),
    "neutral": dict(feel=["calming", "relaxed"],
                    desc=["background", "simple", "plain", "minimal", "office"], bpm=(80, 130)),
    "nostalgia": dict(feel=["mystical", "bright"],
                      desc=["olden", "vintage", "memory", "carousel", "music box", "victorian",
                            "past", "retro", "carnival"],
                      bpm=(70, 125)),
    "sadness": dict(feel=["somber"],
                    desc=["sad", "melancholy", "sorrow", "mourning", "tragic", "weeping"],
                    bpm=(50, 105)),
    "loneliness": dict(feel=["somber", "mysterious"],
                       desc=["lonely", "alone", "empty", "desolate", "isolated", "abandoned"],
                       bpm=(50, 95)),
    "fatigue": dict(feel=["somber", "relaxed"],
                    desc=["sleepy", "tired", "weary", "slow", "exhausted", "drift"],
                    bpm=(45, 85)),
    "confusion": dict(feel=["mysterious", "mystical"],
                      desc=["puzzling", "strange", "surreal", "curious", "dream", "wonder"],
                      bpm=(70, 130)),
    "stress": dict(feel=["suspenseful", "intense", "driving"],
                   desc=["deadline", "rush", "frantic", "pressure", "urgent", "countdown"],
                   bpm=(110, 200)),
    "anxiety": dict(feel=["unnerving", "eerie", "mysterious"],
                    desc=["nervous", "uneasy", "restless", "obsessive", "worry", "paranoid"],
                    bpm=(80, 150)),
    "anger": dict(feel=["aggressive", "dark", "intense"],
                  desc=["angry", "rage", "furious", "battle", "war", "fight", "menacing"],
                  bpm=(110, 200)),
}

# 时长上限（秒）：曲库是当背景音乐的，太长没意义，还拖慢下载
MAX_LENGTH_SECONDS = 210


def _parse_len(length: str) -> int:
    """'00:03:45' → 225 秒。"""
    parts = [int(x) for x in length.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else 0


def _safe(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', " ", text)).strip()


def load_pieces() -> List[dict]:
    """拉取并缓存曲目元数据（首次联网，之后读缓存）。"""
    if PIECES_CACHE.exists():
        return json.loads(PIECES_CACHE.read_text(encoding="utf-8"))
    print("下载曲目元数据 pieces.json ...")
    PIECES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(PIECES_URL, timeout=60) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    PIECES_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def score_track(track: dict, rule: dict) -> int:
    """给一首曲子对某个情绪打分：feel 命中 3 分/个，描述命中 2 分/个，BPM 区间 2 分。"""
    score = 0
    feels = {w.strip().lower() for w in re.split(r"[,/]", track.get("feel") or "")}
    score += len(feels & set(rule["feel"])) * 3
    desc = (track.get("description") or "").lower()
    score += sum(2 for kw in rule["desc"] if kw in desc)
    try:
        bpm = int(track.get("bpm") or 0)
        if rule["bpm"][0] <= bpm <= rule["bpm"][1]:
            score += 2
    except ValueError:
        pass
    return score


def plan_library(pieces: List[dict], per_emotion: int) -> Dict[str, List[dict]]:
    """按情绪挑曲：过滤超长曲目，按分数取前 N，同曲不可复用于多个情绪。"""
    candidates = [t for t in pieces if 0 < _parse_len(t.get("length", "")) <= MAX_LENGTH_SECONDS]
    used_titles: set = set()
    plan: Dict[str, List[dict]] = {}
    for code in EMOTION_CATALOG:
        rule = RULES[code]
        ranked = sorted(
            (t for t in candidates if t["title"] not in used_titles),
            key=lambda t: (-score_track(t, rule), t["title"]),
        )
        picked = [t for t in ranked if score_track(t, rule) >= 3][:per_emotion]
        plan[code] = picked
        used_titles.update(t["title"] for t in picked)
    return plan


def download(url: str, dest: Path, referer: str = "https://incompetech.com/") -> bool:
    """单文件下载（带 Referer 和超时），失败返回 False，外层负责重试。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as f:  # noqa: S310
            shutil.copyfileobj(resp, f)
        tmp.rename(dest)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"    ! 下载失败 {dest.name}: {exc}")
        tmp.unlink(missing_ok=True)
        return False


def verify_mp3(path: Path) -> bool:
    """简单校验：文件存在、>200KB、开头是 ID3 或 0xFF 帧同步（防止下到 404 页面）。"""
    if not path.exists() or path.stat().st_size < 200_000:
        return False
    with open(path, "rb") as f:
        return f.read(3)[:3] == b"ID3" or f.read(1)[0:1] == b"\xff"


def main() -> int:
    parser = argparse.ArgumentParser(description="从 incompetech 构建公版免费曲库")
    parser.add_argument("--plan", action="store_true", help="只打印选曲方案，不下载")
    parser.add_argument("--per-emotion", type=int, default=2, help="每种情绪几首（默认 2）")
    parser.add_argument("--parallel", type=int, default=4, help="并发下载数（默认 4）")
    args = parser.parse_args()

    pieces = load_pieces()
    plan = plan_library(pieces, args.per_emotion)

    total = sum(len(v) for v in plan.values())
    print("=" * 66)
    print(f"选曲方案：{len(plan)} 种情绪 × 至多 {args.per_emotion} 首 = {total} 首")
    print("=" * 66)

    # ---------- 第一步：展示方案 ----------
    jobs: List[tuple] = []
    for code, tracks in plan.items():
        label = getattr(EMOTION_CATALOG[code], "label", code)
        if not tracks:
            print(f"  ⚠ {code:<12}{label}：没有高分候选（将回退本地合成）")
            continue
        for t in tracks:
            name = f"{code}_{_safe(t['title'])}.mp3"
            jobs.append((t, LIBRARY_DIR / name))
            print(f"  {code:<12}{label} ← 《{t['title']}》  feel={t['feel']}  bpm={t['bpm']}  {t['length']}")
    if not jobs:
        return 1

    attribution_lines = [CREDIT, f"曲目清单（共 {len(jobs)} 首）：", ""]
    for t, dest in jobs:
        attribution_lines.append(f"- {dest.name}  |  《{t['title']}》  ISRC: {t.get('isrc') or 'N/A'}")

    if args.plan:
        print("\n【预览模式】未下载。去掉 --plan 开始下载。")
        return 0

    # ---------- 第二步：并行下载 ----------
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    pending = [(t, dest) for t, dest in jobs if not (dest.exists() and verify_mp3(dest))]
    print(f"\n开始下载：待下 {len(pending)} 首（已有 {len(jobs) - len(pending)} 首跳过）…")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {
            pool.submit(download, MP3_BASE + urllib.request.quote(t["filename"]), dest): (t, dest)
            for t, dest in pending
        }
        for i, fut in enumerate(as_completed(futures), 1):
            t, dest = futures[fut]
            if fut.result() and verify_mp3(dest):
                ok += 1
                print(f"  [{i:>2}/{len(pending)}] ✔ {dest.name}  {dest.stat().st_size // 1024}KB")
            else:
                failed.append((t, dest))
                print(f"  [{i:>2}/{len(pending)}] ✘ {dest.name}")

    ATTRIBUTION_FILE.write_text("\n".join(attribution_lines) + "\n", encoding="utf-8")
    print(f"\n完成：成功 {ok} / 失败 {len(failed)}（失败的可以重跑本脚本续传）")
    print(f"署名文件已生成：{ATTRIBUTION_FILE}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
