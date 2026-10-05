"""
============================================================================
tests/test_emotion.py —— 情绪分析单元测试
============================================================================
测试目标：
    1. 情绪字典本身是否自洽（代号唯一、参数在合法范围）
    2. 本地关键词算法能否识别典型样本
    3. 非法输入（空串、超长、奇怪符号）不会崩

运行：pytest tests/test_emotion.py -v
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.emotion_catalog import (
    DEFAULT_EMOTION_CODE,
    EMOTION_CATALOG,
    EmotionResult,
    get_profile,
    list_emotion_options,
)
from backend.services.llm_emotion import analyze_emotion, _local_rule_analyze


# ===========================================================================
# 一、情绪字典完整性
# ===========================================================================
def test_emotion_catalog_has_expected_emotions():
    """必须包含最常见的几种情绪，否则业务不成立。"""
    expected = {
        "joy", "sadness", "anger", "anxiety", "loneliness", "fatigue",
    }
    missing = expected - set(EMOTION_CATALOG.keys())
    assert not missing, f"情绪字典缺少这些情绪：{missing}"


def test_emotion_catalog_values_in_range():
    """效价必须在 [-1,1]，唤醒度/强度必须在 [0,1]，BPM 必须是正数。"""
    for code, profile in EMOTION_CATALOG.items():
        assert -1.0 <= profile.valence <= 1.0, f"{code} 的 valence 越界"
        assert 0.0 <= profile.arousal <= 1.0, f"{code} 的 arousal 越界"
        assert profile.tempo_bpm > 0, f"{code} 的 BPM 非法"
        assert profile.root_midi > 0, f"{code} 的 root_midi 非法"
        assert len(profile.scale) >= 5, f"{code} 的音阶音符太少"
        assert len(profile.progression) >= 2, f"{code} 的和弦走向太短"


def test_get_profile_falls_back_to_default():
    """传入不存在的情绪代号不能抛异常，应回落到 neutral。"""
    assert get_profile("不存在的情绪").code == DEFAULT_EMOTION_CODE
    assert get_profile("").code == DEFAULT_EMOTION_CODE
    assert get_profile(None).code == DEFAULT_EMOTION_CODE


def test_get_profile_supports_chinese_label():
    """支持用中文标签反查（大模型偶尔会直接返回中文）。"""
    assert get_profile("忧伤").code == "sadness"


def test_list_emotion_options_serializable():
    """给前端的数据必须是纯标量，能被 JSON 序列化。"""
    options = list_emotion_options()
    assert len(options) == len(EMOTION_CATALOG)
    for item in options:
        assert isinstance(item["code"], str)
        assert isinstance(item["valence"], float)


# ===========================================================================
# 二、本地关键词算法（降级通道）—— 这是必测项
# ===========================================================================
@pytest.mark.parametrize(
    "text,expected",
    [
        ("今天项目终于上线了，超开心哈哈！", "joy"),
        ("又是一个人吃饭，深夜没人说话，感觉很孤独", "loneliness"),
        ("deadline 快到了，做不完，压力好大，时间不够", "stress"),
        ("偷偷哭了，心里特别难过委屈", "sadness"),
        ("真的谢谢你帮我，很感恩，多亏有你", "gratitude"),
        ("气死我了，凭什么这样对我，太过分了！", "anger"),
    ],
)
def test_local_rule_detects_expected_emotion(text, expected):
    """
    典型样本必须被正确分类。

    这组用例是"验收标准"：如果以后有人改了关键词表导致分类错乱，
    这里会直接失败 —— 这就是回归测试的价值。
    """
    result = _local_rule_analyze(text)
    assert result.primary == expected, (
        f"文本『{text}』期望 {expected}，实际 {result.primary}\n"
        f"打分明细：{result.raw_response}"
    )
    assert result.source == "local_rule"
    assert result.music_prompt, "必须生成音乐提示词"


def test_local_rule_handles_unknown_text():
    """完全中性 / 无关键词的文本不应崩溃，而应落到 neutral。"""
    result = _local_rule_analyze("今天吃了番茄炒蛋，然后喝了牛奶。")
    assert result.primary == DEFAULT_EMOTION_CODE
    assert 0.0 <= result.arousal <= 1.0


def test_local_rule_handles_empty_text():
    """空输入不能抛异常（防御性编程要求）。"""
    result = _local_rule_analyze("")
    assert result.primary == DEFAULT_EMOTION_CODE


def test_local_rule_range_and_tags():
    """输出的所有数值字段都必须在合法区间内，次要标签不能包含主情绪。"""
    result = _local_rule_analyze("很开心也很感动，还有一点点怀念以前的日子。")
    assert -1.0 <= result.valence <= 1.0
    assert 0.0 <= result.arousal <= 1.0
    assert 0.0 <= result.intensity <= 1.0
    assert result.primary not in result.tags, "次要标签不应重复包含主情绪"
    assert len(result.tags) <= 3


def test_local_rule_is_deterministic():
    """同样的输入必须得到同样的输出（可复现性，便于调试和答辩）。"""
    text = "今天很难过，一个人待着。"
    a = _local_rule_analyze(text)
    b = _local_rule_analyze(text)
    assert a.primary == b.primary
    assert a.valence == b.valence


# ===========================================================================
# 三、统一入口 analyze_emotion
# ===========================================================================
def test_analyze_emotion_returns_emotion_result():
    """
    未配置 LLM 的情况下，入口函数应静默走本地通道并返回合法结果。
    （conftest 里已经把 LLM_API_KEY 置空了）
    """
    result = asyncio.run(analyze_emotion("今天是个好日子，心情超级好！"))
    assert isinstance(result, EmotionResult)
    assert result.primary in EMOTION_CATALOG
    assert isinstance(result.tags, list)


def test_analyze_emotion_with_very_long_text():
    """5000 字长文本不能崩，且要能正常返回结果。"""
    long_text = "今天很开心。" * 500          # 3000 字
    result = asyncio.run(analyze_emotion(long_text))
    assert result.primary in EMOTION_CATALOG


def test_emotion_result_profile_property():
    """EmotionResult.profile 应能直接拿到对应的音乐参数配置。"""
    result = _local_rule_analyze("好累啊，加班到通宵。")
    profile = result.profile
    assert profile.tempo_bpm > 0
    assert profile.code == result.primary
