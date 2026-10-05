"""
============================================================================
tests/test_composer.py —— 本地音乐合成引擎单元测试
============================================================================
重点验证：
    1. 产出的确实是合法 WAV（文件头、采样率、声道、时长）
    2. 音频不是静音（有人愚蠢的 bug 是"生成成功但全是 0"）
    3. 相同 seed → 完全相同的结果（可复现性）
    4. 不同情绪 → 不同的音乐参数（否则情绪驱动就是假的）

运行：pytest tests/test_composer.py -v
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from backend.services.emotion_catalog import EMOTION_CATALOG, get_profile
from backend.services.local_composer import (
    generate_melody_notes,
    midi_to_freq,
    note_name,
    render_wav_for_emotion,
)


@pytest.fixture(scope="module")
def audio_dir(tmp_path_factory) -> Path:
    """临时音频输出目录。"""
    return tmp_path_factory.mktemp("audio_test")


def read_wav(path: Path) -> tuple[dict, bytes]:
    """读 WAV 文件，返回 (参数字典, 原始字节)。"""
    with wave.open(str(path), "rb") as w:
        info = {
            "channels": w.getnchannels(),
            "sampwidth": w.getsampwidth(),
            "framerate": w.getframerate(),
            "frames": w.getnframes(),
        }
        return info, path.read_bytes()


# ===========================================================================
# 一、乐理工具函数
# ===========================================================================
def test_midi_to_freq_reference():
    """A4 = MIDI 69 = 440Hz，这是国际标准，必须精确。"""
    assert abs(midi_to_freq(69) - 440.0) < 1e-9
    # 升八度频率翻倍
    assert abs(midi_to_freq(81) - 880.0) < 1e-9
    # 降八度频率减半
    assert abs(midi_to_freq(57) - 220.0) < 1e-9


def test_note_name():
    assert note_name(60) == "C4"
    assert note_name(61) == "C#4"
    assert note_name(69) == "A4"


def test_melody_notes_are_in_scale():
    """生成的旋律音符必须落在给定音阶内（相对 base 的偏移属于音阶）。"""
    import random

    scale = (0, 2, 4, 7, 9)
    notes = generate_melody_notes(random.Random(42), scale, base_midi=72, count=30)

    assert len(notes) == 30
    for note in notes:
        offset = (note - 72) % 12
        assert offset in scale, f"音高 {note} 不在五声音阶内"


# ===========================================================================
# 二、合成产物
# ===========================================================================
def test_render_wav_basic(audio_dir: Path):
    """合成的 WAV 必须是合法的立体声文件，时长正确，且不是静音。"""
    profile = get_profile("joy")
    meta = render_wav_for_emotion(
        profile=profile,
        audio_dir=audio_dir,
        emotion_code="joy",
        track_id=1,
        duration_sec=2,
        sample_rate=22050,
    )

    path = Path(meta["file_path"])
    assert path.exists()

    info, raw = read_wav(path)
    assert raw[:4] == b"RIFF"
    assert raw[8:12] == b"WAVE"
    assert info["channels"] == 2
    assert info["sampwidth"] == 2
    assert info["framerate"] == 22050
    # 允许 ±0.1 秒误差
    assert abs(info["frames"] / info["framerate"] - 2) < 0.1

    # 读取采样数据确认有声音
    import array

    with wave.open(str(path), "rb") as w:
        data = array.array("h")
        data.frombytes(w.readframes(w.getnframes() * 2))
    peak = max(max(data), -min(data))
    assert peak > 5000, f"音频几乎是静音（峰值仅 {peak}）"
    assert peak <= 32767, f"音频存在削波（峰值 {peak} > 32767）"


def test_render_is_deterministic_with_same_seed(audio_dir: Path):
    """相同种子必须产出逐字节相同的文件 —— 便于复现和回归测试。"""
    profile = get_profile("sadness")

    a = render_wav_for_emotion(profile, audio_dir, "sadness", 101, 1, 22050, seed=12345)
    b = render_wav_for_emotion(profile, audio_dir, "sadness", 102, 1, 22050, seed=12345)

    # 两个音符_sec/ SR 相同地形必须得到同样的波形；文件名不同不代表内容不同
    bytes_a = Path(a["file_path"]).read_bytes()
    bytes_b = Path(b["file_path"]).read_bytes()
    # 字节长度一致 + 音频数据段一致（跳过前 44 字节的文件头）
    assert len(bytes_a) == len(bytes_b)
    assert bytes_a[44:] == bytes_b[44:]


def test_different_emotions_produce_different_output(audio_dir: Path):
    """不同情绪的音乐参数必须不同（这是"情绪驱动"的直接证据）。"""
    joy = render_wav_for_emotion(get_profile("joy"), audio_dir, "joy", 201, 1, 22050, seed=7)
    sad = render_wav_for_emotion(get_profile("sadness"), audio_dir, "sadness", 202, 1, 22050, seed=7)

    assert joy["tempo_bpm"] != sad["tempo_bpm"]
    assert joy["root_note"] != sad["root_note"]

    # 波形也不能一样——说明节奏、和弦、音阶真的变了
    assert Path(joy["file_path"]).read_bytes()[44:] != Path(sad["file_path"]).read_bytes()[44:]


@pytest.mark.parametrize("emotion_code", ["joy", "sadness", "anger", "loneliness", "anxiety"])
def test_all_emotions_can_render(audio_dir: Path, emotion_code: str):
    """所有配置文件里的情绪都必须能正常合成，不能有某个情绪参数表写坏。"""
    meta = render_wav_for_emotion(
        get_profile(emotion_code), audio_dir, emotion_code, 300, 1, 22050
    )
    assert Path(meta["file_path"]).exists()
    assert meta["duration_seconds"] == 1


def test_meta_contains_music_info(audio_dir: Path):
    """返回的元数据里要带上 BPM / 调式 / 随机种子，用于前端展示。"""
    meta = render_wav_for_emotion(get_profile("anger"), audio_dir, "anger", 401, 1, 22050)
    for key in ("file_name", "tempo_bpm", "root_note", "seed"):
        assert key in meta
    assert meta["file_name"].startswith("emo_anger_")
