"""
==============================================================================
tests/test_import_library.py —— 素材导入工具与情绪映射表的测试
==============================================================================
为什么要测这个？
    导入工具一旦有 bug，最惨的结果是"把 151 首歌导进去却一首匹配不上"，
    而这在页面上只表现为"怎么还在用合成音"，很难排查。这里把关键环节钉死。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_library import (  # noqa: E402
    ENCRYPTED_EXTENSIONS,
    PLAYABLE_EXTENSIONS,
    load_map,
    parse_song_name,
    run_import,
    safe_filename,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAP_FILE = PROJECT_ROOT / "backend" / "data" / "song_emotion_map.json"


# ---------------------------------------------------------------------------
# 1. 文件名解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stem, expected",
    [
        ("永彬Ryan_B - 初_H", ("永彬Ryan_B", "初")),                      # 歌手名带下划线
        ("DP龙猪 _ 艾诗雨Sherry - 未来日记_H", ("DP龙猪 _ 艾诗雨Sherry", "未来日记")),  # 双歌手
        ("Joysun - 娃娃机", ("Joysun", "娃娃机")),                        # 无 _H 尾巴
        ("初", ("", "初")),                                              # 只有歌名
        ("Uu (刘梦妤) - 傻傻_H", ("Uu (刘梦妤)", "傻傻")),                 # 带英文括号
        ("幺妹 - 未来有光 (ลูกชายของพ่อ)_H", ("幺妹", "未来有光 (ลูกชายของพ่อ)")),  # 带泰文
    ],
)
def test_parse_song_name(stem: str, expected: tuple):
    """'歌手 - 歌名_H' 必须能正确拆开，多歌手分隔符 ' _ ' 不能被误当成切分点。"""
    assert parse_song_name(stem) == expected


def test_parse_strips_duplicate_suffix():
    """下载器重名时加的 '_1' 尾巴要被剥掉，否则查表会 miss。"""
    artist, title = parse_song_name("某人 - 晴天_1_H")
    assert title == "晴天"


def test_safe_filename_removes_illegal_chars():
    """Windows 非法字符必须清干净，否则 copy 时会直接抛 OSError。"""
    assert safe_filename('joy_a/b:c*d?e"f<g>h|i') == "joy_a b c d e f g h i"


# ---------------------------------------------------------------------------
# 2. 映射表本身
# ---------------------------------------------------------------------------
def test_map_file_is_valid_and_complete():
    """映射表必须能解析，情绪代号全都合法（写错会掉进 unknown 兜底桶）。"""
    from backend.services.emotion_catalog import EMOTION_CATALOG

    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    mapping = data["mapping"]

    assert len(mapping) == 151, "应覆盖官方歌单全部 151 首"
    for title, info in mapping.items():
        assert info["emotion"] in EMOTION_CATALOG, f"《{title}》情绪代号非法"


def test_every_emotion_has_song_except_stress():
    """除 stress 外每种情绪都应有素材，保证 Demo 覆盖度（缺的会在导入报告里提示）。"""
    mapping = load_map()
    covered = {info["emotion"] for info in mapping.values()}
    missing = set(__import__(
        "backend.services.emotion_catalog", fromlist=["EMOTION_CATALOG"]
    ).EMOTION_CATALOG.keys()) - covered
    assert missing == {"stress"}, f"预料之外的缺失情绪：{missing}"


# ---------------------------------------------------------------------------
# 3. 导入流程
# ---------------------------------------------------------------------------
def _touch(directory: Path, name: str, header: bytes = b"ID3\x03") -> Path:
    """造一个"看起来像音频"的测试文件。"""
    path = directory / name
    path.write_bytes(header + b"\x00" * 64)
    return path


def test_import_tags_and_renames(tmp_path: Path):
    """mp3 素材 → 查表打标签 → 复制成 {情绪}_{歌手} - {歌名}.mp3。"""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "library"
    _touch(src, "永彬Ryan_B - 初_H.mp3")
    _touch(src, "INTO1 - 古蜀回响_H.mp3")
    _touch(src, "hello.txt")                       # 非音频应被忽略

    assert run_import(src, dest, move=False, dry_run=False, limit_per_emotion=0) == 0

    names = sorted(p.name for p in dest.iterdir())
    assert names == ["love_永彬Ryan_B - 初.mp3", "moved_INTO1 - 古蜀回响.mp3"]
    # 源文件必须还在（默认是复制不是移动）
    assert (src / "永彬Ryan_B - 初_H.mp3").exists()


def test_import_skips_encrypted_files(tmp_path: Path):
    """.mgg 这类加密格式必须被跳过，一个都不能导进曲库。"""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "library"
    _touch(src, "Joysun - 娃娃机_H.mgg", header=b"\x7d\x55\x10\x2a")

    run_import(src, dest, move=False, dry_run=False, limit_per_emotion=0)

    assert list(dest.iterdir()) == [], "加密文件绝不能被导入"


def test_import_dry_run_writes_nothing(tmp_path: Path):
    """预览模式不能产生任何副作用。"""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "library"
    _touch(src, "Joysun - 娃娃机_H.mp3")

    run_import(src, dest, move=False, dry_run=True, limit_per_emotion=0)
    assert list(dest.iterdir()) == []


def test_import_is_idempotent(tmp_path: Path):
    """重复跑两次不应该产生重复文件，也不该报错。"""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "library"
    _touch(src, "Joysun - 娃娃机_H.mp3")

    run_import(src, dest, move=False, dry_run=False, limit_per_emotion=0)
    run_import(src, dest, move=False, dry_run=False, limit_per_emotion=0)
    assert len(list(dest.iterdir())) == 1


def test_limit_per_emotion(tmp_path: Path):
    """每种情绪的上限要生效。"""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "library"
    for i, title in enumerate(["初", "告白前一秒", "傻傻", "全是你"]):
        _touch(src, f"某人{i} - {title}_H.mp3")

    run_import(src, dest, move=False, dry_run=False, limit_per_emotion=2)
    assert len(list(dest.iterdir())) == 2


def test_extensions_classification():
    """加密格式和播放格式不能有交集，否则会误判。"""
    assert not (ENCRYPTED_EXTENSIONS.keys() & PLAYABLE_EXTENSIONS)
    for ext in (".mgg", ".ncm", ".kgm", ".qmc3", ".tm0"):
        assert ext in ENCRYPTED_EXTENSIONS
    for ext in (".mp3", ".m4a", ".wav", ".flac", ".ogg"):
        assert ext in PLAYABLE_EXTENSIONS
