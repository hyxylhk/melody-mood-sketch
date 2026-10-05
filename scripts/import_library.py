"""
==============================================================================
scripts/import_library.py —— 官方素材一键导入曲库（自动打情绪标签）
==============================================================================
这个脚本干什么用？
    把素材目录里的音频文件，按 backend/data/song_emotion_map.json 的情绪映射表
    自动重命名成 {情绪代号}_{歌手} - {歌名}.{后缀}，复制进 storage/library/。
    导完之后，写日记生成的配乐就会优先命中这些真实歌曲。

怎么用（在项目根目录执行）：
    # 1) 先预览，不会真的写任何文件（强烈建议先跑一次）
    python scripts/import_library.py --dry-run

    # 2) 确认无误后正式导入
    python scripts/import_library.py

    # 3) 从别的目录导入
    python scripts/import_library.py --source "D:/我的音乐/素材"

    # 4) 导入后删除源文件（默认只复制，不删源文件）
    python scripts/import_library.py --move

    # 5) 每种情绪最多导 N 首（曲库太大时用），0 表示不限
    python scripts/import_library.py --limit-per-emotion 3

⚠️ 关于 .mgg / .qmc3 / .ncm / .kgm 这类文件：
    这些是各家音乐 App 的**加密专属格式**（DRM），只有自家客户端能解密，
    浏览器 <audio> 标签读不懂，放进曲库也**不会出声**。
    本脚本会主动识别并跳过它们，并打印原因。
    想拿到能用的文件，请走官方允许的路径（单曲购买后重新下载 / 大赛素材包等），
    得到 .mp3 / .m4a / .flac / .wav / .ogg 之后再来跑这个脚本。

    本脚本**不做也不该做**任何解密/破解动作 —— 这一点请务必理解。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Optional, Tuple

# 让脚本可以直接 `python scripts/xxx.py` 运行（把项目根目录塞进模块搜索路径）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import get_settings                      # noqa: E402
from backend.services.emotion_catalog import EMOTION_CATALOG  # noqa: E402

MAP_FILE = PROJECT_ROOT / "backend" / "data" / "song_emotion_map.json"

# 可被浏览器直接播放的音频后缀
PLAYABLE_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".ogg"}

# 各家 App 的加密专属格式：看见了直接跳过并给提示
ENCRYPTED_EXTENSIONS = {
    ".mgg": "QQ音乐 加密格式（VIP 专属）",
    ".qmc0": "QQ音乐 加密格式",
    ".qmc2": "QQ音乐 加密格式",
    ".qmc3": "QQ音乐 加密格式",
    ".qmcflac": "QQ音乐 加密格式（无损）",
    ".qmcogg": "QQ音乐 加密格式",
    ".ncm": "网易云音乐 加密格式",
    ".kgm": "酷狗音乐 加密格式",
    ".vpr": "酷狗音乐 加密格式",
    ".tm0": "酷我音乐 加密格式",
    ".tm3": "酷我音乐 加密格式",
    ".xm": "虾米音乐 加密格式",
}

# Windows 文件名非法字符
_UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


# ---------------------------------------------------------------------------
# 文件名解析
# ---------------------------------------------------------------------------
def parse_song_name(file_stem: str) -> Tuple[str, str]:
    """
    从文件名拆出 (歌手, 歌名)。

    支持这些常见写法：
        "永彬Ryan_B - 初_H"           → ("永彬Ryan_B", "初")
        "DP龙猪 _ 艾诗雨Sherry - 未来日记_H" → ("DP龙猪 _ 艾诗雨Sherry", "未来日记")
        "Joysun - 娃娃机"             → ("Joysun", "娃娃机")
        "初"                          → ("", "初")

    实现思路：先去掉下载器加的 "_H" 尾巴，再从**最后一个** " - " 处劈开。
    用最后一个而不是第一个，是因为歌手名里经常有 " _ "（多歌手分隔符），
    而 " - " 只出现在"歌手 - 歌名"这个位置。
    """
    stem = file_stem
    # 去掉 QQ音乐下载器加的 "_H" / "-H" 尾巴
    stem = re.sub(r"[_\-\s]*H$", "", stem).strip()
    stem = re.sub(r"_\d+$", "", stem).strip()   # 去掉 "_1" 这类重名序号

    if " - " in stem:
        artist, title = stem.rsplit(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem.strip()


def safe_filename(text: str) -> str:
    """清洗成 Windows 合法文件名片段（去掉 \\ / : * ? " < > |，并压缩空白）。"""
    text = _UNSAFE_NAME_CHARS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_map() -> Dict[str, dict]:
    """读取情绪映射表 JSON。"""
    if not MAP_FILE.exists():
        print(f"✗ 找不到映射表：{MAP_FILE}")
        sys.exit(1)
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    return data["mapping"]


def run_import(source: Path, dest: Path, move: bool, dry_run: bool,
               limit_per_emotion: int) -> int:
    """执行导入，返回退出码。"""
    song_map = load_map()

    if not source.exists():
        print(f"✗ 源目录不存在：{source}")
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    files = sorted([p for p in source.rglob("*") if p.is_file()])

    imported: list = []
    skipped_encrypted: list = []
    skipped_no_map: list = []
    per_emotion: Dict[str, int] = defaultdict(int)

    for file_path in files:
        suffix = file_path.suffix.lower()

        # ① 加密格式 → 跳过
        if suffix in ENCRYPTED_EXTENSIONS:
            skipped_encrypted.append((file_path.name, ENCRYPTED_EXTENSIONS[suffix]))
            continue

        # ② 不是音频（比如那个 .txt 说明文件）→ 忽略，不计入统计
        if suffix not in PLAYABLE_EXTENSIONS:
            continue

        # ③ 可播放音频 → 查情绪标签
        artist, title = parse_song_name(file_path.stem)

        # 映射表 key 可能与文件名略有出入，做几次容错匹配
        entry: Optional[dict] = None
        for candidate in (title, title.replace("_", " "), title.strip("()（）")):
            if candidate in song_map:
                entry = song_map[candidate]
                break

        if entry is None:
            skipped_no_map.append(file_path.name)
            continue

        emotion = entry["emotion"]
        if emotion not in EMOTION_CATALOG:
            skipped_no_map.append(f"{file_path.name}（情绪代号非法：{emotion}）")
            continue

        # ④ 每种情绪的数量上限
        if limit_per_emotion > 0 and per_emotion[emotion] >= limit_per_emotion:
            continue
        per_emotion[emotion] += 1

        # ⑤ 组装目标文件名：{情绪代号}_{歌手} - {歌名}.{后缀}
        display = f"{artist} - {title}" if artist else title
        new_name = safe_filename(f"{emotion}_{display}") + suffix
        target = dest / new_name

        imported.append((file_path, target, emotion, title))
        if not dry_run:
            # 同名文件已存在就跳过（幂等：反复跑不会产生重复文件）
            if not target.exists():
                if move:
                    shutil.move(str(file_path), str(target))
                else:
                    shutil.copy2(file_path, target)

    # ---------------- 打印报告 ----------------
    tag = "【预览模式】" if dry_run else ""
    print("=" * 66)
    print(f"{tag}素材导入报告")
    print("=" * 66)
    print(f"源目录  : {source}")
    print(f"目标目录: {dest}\n")

    counter = Counter(e for _, _, e, _ in imported)
    print(f"✔ 成功匹配 {len(imported)} 首（情绪分布）：")
    for code, num in sorted(counter.items(), key=lambda x: -x[1]):
        label = getattr(EMOTION_CATALOG[code], "label", code)
        print(f"    {code:<12} {label:<4} {num:>3} 首")
    if dry_run:
        print("\n  前 8 个文件的新名字预览：")
        for _, target, _, _ in imported[:8]:
            print(f"    → {target.name}")

    if skipped_no_map:
        print(f"\n⚠ 映射表里查不到 {len(skipped_no_map)} 个文件（已跳过）：")
        for name in skipped_no_map[:10]:
            print(f"    - {name}")
        if len(skipped_no_map) > 10:
            print(f"    ... 还有 {len(skipped_no_map) - 10} 个")
        print("  解决办法：往 backend/data/song_emotion_map.json 里补一条，再跑一次。")

    if skipped_encrypted:
        print(f"\n✘ 加密格式 {len(skipped_encrypted)} 个（浏览器无法播放，已跳过）：")
        for name, reason in skipped_encrypted[:5]:
            print(f"    - {name}  [{reason}]")
        print(f"    ... 共 {len(skipped_encrypted)} 个")
        print("  说明：这类文件带 DRM 加密，只有原客户端能解，网页播放器读不出来。")
        print("  请通过官方允许的方式获取标准音频（mp3/m4a/flac/wav/ogg）后再导入。")

    # 覆盖率：哪些情绪在曲库里还没有歌
    missing = [c for c in EMOTION_CATALOG if c not in counter]
    if missing:
        print("\n⚠ 以下情绪在曲库中暂无素材，命中时会回退到本地算法合成：")
        for code in missing:
            print(f"    - {code}（{getattr(EMOTION_CATALOG[code], 'label', code)}）")

    print("\n" + "=" * 66)
    if dry_run:
        print("这是预览，没有改动任何文件。确认后去掉 --dry-run 再跑一次即可。")
    else:
        action = "移动" if move else "复制"
        print(f"完成：{action}了 {len(imported)} 首进曲库。刷新网页即可生效。")
    print("=" * 66)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把素材音频按情绪标签导入官方素材曲库 storage/library/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        default=r"C:\Users\88448\Music\VipSongsDownload",
        help="素材文件所在目录（默认：C:\\Users\\88448\\Music\\VipSongsDownload）",
    )
    parser.add_argument("--dest", help="曲库目录（默认读配置项 MUSIC_LIBRARY_DIR）")
    parser.add_argument("--move", action="store_true", help="移动而非复制（默认复制，源文件保留）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写文件")
    parser.add_argument(
        "--limit-per-emotion", type=int, default=0,
        help="每种情绪最多导几首，0=不限（默认 0）",
    )
    args = parser.parse_args()

    dest = Path(args.dest) if args.dest else get_settings().library_dir
    return run_import(Path(args.source), dest, args.move, args.dry_run, args.limit_per_emotion)


if __name__ == "__main__":
    raise SystemExit(main())
