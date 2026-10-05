"""
==============================================================================
backend/services/llm_emotion.py —— 情绪解析服务
==============================================================================
职责：输入一段日记正文，输出结构化的情绪结果（EmotionResult）。

设计要点（答辩可以重点讲这里）：

    ┌─────────────┐
    │  用户日记   │
    └──────┬──────┘
           │
           ▼
    ① 调用大模型做"受控分类"
       （让模型从 16 种情绪里做选择题，而不是自由发挥）
           │
           ├── 成功 → 解析 JSON → EmotionResult(source="llm")
           │
           └── 失败 / 没配 Key / 超时
                  │
                  ▼
    ② 降级到"本地关键词加权算法"
                  → EmotionResult(source="local_rule")

    为什么要做降级（fallback）？
        黑客松现场的网络和 API 余额都不可控。降级策略保证
        **断网、没 Key、余额不足** 这三种情况下系统依然能完整演示，
        这是工程健壮性（robustness）的体现，评委很吃这一套。
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

import httpx

from backend.config import get_settings
from backend.services.emotion_catalog import (
    DEFAULT_EMOTION_CODE,
    EMOTION_CATALOG,
    EmotionProfile,
    EmotionResult,
    EMOTION_CODES,
)

# ---------------------------------------------------------------------------
# 常量区
# ---------------------------------------------------------------------------

# 送给大模型的请求超时、重试策略都在 settings 里，这里只放"提示词"这类常量

_SYSTEM_PROMPT = """\
你是一名专业的「情绪分析引擎」，服务于一款叫做《旋律情绪速写》的产品。
你的任务是阅读用户写的日记，判断他此刻的情绪，并输出**严格符合 JSON Schema** 的结果。

硬性规则：
1. 只输出一个 JSON 对象，不要有任何前言、解释、Markdown 代码块标记。
2. primary_emotion 必须从下面给定的候选代号中选择一个，不能自己造词。
3. tags 是次要情绪（可以是空数组），最多 3 个，也必须从候选代号中选。
4. valence 表示正负倾向，范围 -1.0（极端负面）到 1.0（极端正面）。
5. arousal 表示激动程度，范围 0.0（非常平静）到 1.0（非常激动）。
6. intensity 表示情绪强度，范围 0.0 到 1.0。
7. summary 用中文，不超过 40 个字，站在"朋友安慰你"的角度，不要说教。
8. music_prompt 用英文写一段给 AI 音乐生成模型的描述，包含：流派、乐器、速度、情绪形容词。
"""


def _build_user_prompt(diary_text: str, candidate_codes: List[str]) -> str:
    """
    拼装给大模型的用户消息。

    注意这里把「候选情绪代号 + 中文解释」一起给模型，
    相当于给它一份"选择题选项"，能显著提高输出稳定性（减少幻觉）。
    """
    options = "\n".join(
        f"  - {code}（{EMOTION_CATALOG[code].label}：{EMOTION_CATALOG[code].description}）"
        for code in candidate_codes
    )
    return f"""\
候选情绪代号：
{options}

请分析下面这篇日记：

--------------------
{diary_text}
--------------------

输出 JSON 格式（字段名必须完全一致）：
{{
  "primary_emotion": "候选代号之一",
  "tags": ["候选代号之一"],
  "valence": 0.0,
  "arousal": 0.0,
  "intensity": 0.0,
  "summary": "中文一句话",
  "music_prompt": "english description"
}}
"""


def _clamp(value: float, low: float, high: float, default: float) -> float:
    """
    把一个数限制在 [low, high] 区间内。

    为什么需要？大模型偶尔会返回 3.5 这种越界值，或者 "0.6abc" 这种非法字符串，
    不处理的话后面算音乐参数就会出问题。这就是所谓的"输入消毒"。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    # 处理 NaN / Infinity 这种 JSON 里可能出现的怪值
    if num != num or num in (float("inf"), float("-inf")):
        return default
    return max(low, min(high, num))


def _extract_json_object(raw: str) -> Optional[Dict]:
    """
    从大模型的原始返回里抠出 JSON 对象。

    大模型很诚实地说了"不要 Markdown"，但它经常还是会返回：
        ```json
        {...}
        ```
    所以这里要做三层容错：直接解析 → 去掉代码块围栏再解析 → 正则找第一个 {...}
    """
    if not raw:
        return None

    text = raw.strip()

    # 第 1 层：直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 第 2 层：去掉 ```json ... ``` 围栏
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(fenced)
    except json.JSONDecodeError:
        pass

    # 第 3 层：正则抓取第一个完整的 {...} 块
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _sanitize_tags(raw_tags, primary: str) -> List[str]:
    """
    清洗次要情绪标签：
      - 去重
      - 去掉主情绪本身（主情绪已经在 primary 字段了，重复没意义）
      - 过滤掉不在情绪字典里的非法值
      - 最多保留 3 个
    """
    if not isinstance(raw_tags, list):
        return []

    result: List[str] = []
    for item in raw_tags:
        code = str(item).strip().lower()
        if code in EMOTION_CATALOG and code != primary and code not in result:
            result.append(code)
        if len(result) >= 3:
            break
    return result


# ===========================================================================
# 路线 ①：调用大模型
# ===========================================================================
def _chat_completions_url(base_url: str) -> str:
    """
    统一把 base_url 补全成 OpenAI 兼容的 chat/completions 地址。

    用户可能填：
        https://api.deepseek.com/v1        → 需要补 /chat/completions
        https://api.deepseek.com/v1/       → 同上，但要去掉多余斜杠
        https://xxx/v1/chat/completions    → 已经完整，直接返回
    """
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


async def _call_llm(text: str) -> EmotionResult:
    """
    真正发起 HTTP 请求调用大模型。

    这里用 httpx.AsyncClient（可以理解为 requests 的异步版本）。
    为什么要用异步？因为 FastAPI 是异步框架，
    如果用同步 requests，一个请求在等大模型返回时会把整个服务线程卡住，
    其他用户就得排队。async + await 让等待期间可以去处理别的请求。
    """
    settings = get_settings()

    url = _chat_completions_url(settings.LLM_BASE_URL)
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(text[:3000], EMOTION_CODES)},
        ],
        "temperature": 0.3,      # 低温度 = 输出更稳定，情绪分类不需要创造力
        "max_tokens": 500,
        "stream": False,
        # response_format 让部分厂商强制返回 JSON（OpenAI / DeepSeek 支持）
        "response_format": {"type": "json_object"},
    }

    # async with：离开代码块时自动关闭连接池（类似 C++ 的 RAII）
    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()          # 4xx / 5xx 会抛异常，交给上层处理
        body = response.json()

    # OpenAI 兼容格式里，回答藏在 choices[0].message.content
    raw_content: str = body["choices"][0]["message"]["content"]

    parsed = _extract_json_object(raw_content)
    if parsed is None:
        # JSON 都解析不出来，认为这次调用失败，交给上层走降级
        raise ValueError(f"大模型返回的不是合法 JSON：{raw_content[:200]}")

    primary = str(parsed.get("primary_emotion", DEFAULT_EMOTION_CODE)).strip().lower()
    profile = EMOTION_CATALOG.get(primary)

    if profile is None:
        # 模型返回了字典里没有的情绪 → 说明它没按 options 来
        # 我们不强硬报错，而是回落到 neutral，并记录原始返回方便排查
        profile = EMOTION_CATALOG[DEFAULT_EMOTION_CODE]
        primary = DEFAULT_EMOTION_CODE

    return EmotionResult(
        primary=primary,
        tags=_sanitize_tags(parsed.get("tags"), primary),
        valence=_clamp(parsed.get("valence"), -1.0, 1.0, profile.valence),
        arousal=_clamp(parsed.get("arousal"), 0.0, 1.0, profile.arousal),
        intensity=_clamp(parsed.get("intensity"), 0.0, 1.0, 0.6),
        summary=str(parsed.get("summary", "")).strip()[:200]
        or f"这天的心情大约是{profile.label}。",
        music_prompt=str(parsed.get("music_prompt", "")).strip()[:400]
        or _default_music_prompt(profile),
        source="llm",
        raw_response=raw_content[:1000],
    )


# ===========================================================================
# 路线 ②：本地关键词加权算法（降级方案）
# ===========================================================================
def _local_rule_analyze(text: str) -> EmotionResult:
    """
    不用任何外部 API 的纯本地情绪识别。

    算法思路（答辩可以讲 "轻量可解释"）：
        1. 遍历情绪字典里每种情绪的关键词表
        2. 统计每个关键词在日记里出现的次数
        3. 得分 = Σ(出现次数 × 关键词长度权重)
           —— 长关键词权重更高，因为「五味杂陈」比「不」这种宽泛词更有区分度
        4. 取总分最高的情绪作为主情绪，第二三名作为次要标签
        5. 用标点符号做微调（连续感叹号 → 唤醒度升高；省略号 → 降低）

    这个算法的优点是：零延迟、零成本、完全可解释、断网可用。
    缺点是：不理解反讽和复杂语境（这正是大模型要补的部分）。
    """
    lowered = text.lower()
    scores: Dict[str, float] = {}

    for code, profile in EMOTION_CATALOG.items():
        score = 0.0
        for keyword in profile.keywords:
            count = lowered.count(keyword.lower())
            if count > 0:
                # 关键词越长越具体，权重高一点；出现多次则累加
                weight = 1.0 + len(keyword) * 0.1
                score += count * weight
        if score > 0:
            scores[code] = score

    # 标点符号带来的微调项
    exclamation_count = text.count("!") + text.count("！")
    question_count = text.count("?") + text.count("？")
    ellipsis_count = text.count("...") + text.count("…")

    if not scores:
        # 一个关键词都没命中 → 判定为中性。
        # 此时根据标点做一些"情绪底色"推断，让结果不要死板
        profile = EMOTION_CATALOG[DEFAULT_EMOTION_CODE]
        arousal = 0.40
        if exclamation_count >= 2:
            arousal = 0.70
        if ellipsis_count >= 1:
            arousal = 0.28
        return EmotionResult(
            primary=DEFAULT_EMOTION_CODE,
            tags=[],
            valence=profile.valence,
            arousal=arousal,
            intensity=0.30,
            summary="没有匹配到明显的情绪关键词，先按平静处理吧。",
            music_prompt=_default_music_prompt(profile),
            source="local_rule",
            raw_response="no keyword matched",
        )

    # 按分数从高到低排序
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    primary_code = ranked[0][0]
    primary_profile = EMOTION_CATALOG[primary_code]

    # 次要标签：取第 2、3 名，且分数至少要达到主情绪的 30%，otherwise 噪声太大
    tags: List[str] = []
    threshold = ranked[0][1] * 0.3
    for code, score in ranked[1:]:
        if score >= threshold and len(tags) < 3:
            tags.append(code)

    # 用最高分做一个粗略的强度估计：分数越高，情绪表达越密集
    intensity = _clamp(ranked[0][1] / 8.0, 0.2, 1.0, 0.5)

    # 标点微调唤醒度
    arousal = primary_profile.arousal
    if exclamation_count >= 2:
        arousal = _clamp(arousal + 0.12, 0.0, 1.0, arousal)
    if ellipsis_count >= 1:
        arousal = _clamp(arousal - 0.10, 0.0, 1.0, arousal)
    if question_count >= 3:
        # 大量问号通常意味着纠结 / 迷茫
        arousal = _clamp(arousal + 0.05, 0.0, 1.0, arousal)

    hit_keywords = [
        kw for kw in primary_profile.keywords if kw.lower() in lowered
    ][:3]
    keyword_hint = "、".join(hit_keywords) if hit_keywords else "整体语气"

    return EmotionResult(
        primary=primary_code,
        tags=tags,
        valence=primary_profile.valence,
        arousal=arousal,
        intensity=intensity,
        summary=f"从「{keyword_hint}」这些表达看，今天的底色是{primary_profile.label}。",
        music_prompt=_default_music_prompt(primary_profile),
        source="local_rule",
        raw_response=f"keyword_scores={dict(ranked)}",
    )


def _default_music_prompt(profile: EmotionProfile) -> str:
    """
    当大模型没给 music_prompt 时，用情绪字典里的参数拼一段英文提示词。

    这个函数保证了 music_prompt 字段永远有值（调用云端音乐 API 时必填）。
    """
    style = ", ".join(profile.style_words)
    tempo_desc = (
        "very slow" if profile.tempo_bpm < 65
        else "slow" if profile.tempo_bpm < 85
        else "moderate" if profile.tempo_bpm < 105
        else "fast" if profile.tempo_bpm < 125
        else "very fast"
    )
    return (
        f"{style}, {tempo_desc} tempo around {profile.tempo_bpm} BPM, "
        f"instrumental only, no vocals, loopable background music, "
        f"mood: {profile.label_en.lower()}"
    )


# ===========================================================================
# 对外唯一入口
# ===========================================================================
async def analyze_emotion(text: str) -> EmotionResult:
    """
    情绪分析总入口 —— 其它模块只需要调这一个函数。

    执行顺序：
        1. 如果配置了 LLM → 尝试调用大模型
        2. 调用失败且 settings.LLM_FALLBACK_ENABLED=True → 降级本地算法
        3. 如果压根没配 LLM → 直接本地算法

    参数:
        text: 日记正文
    返回:
        EmotionResult
    """
    settings = get_settings()

    if settings.llm_enabled:
        try:
            return await _call_llm(text)
        except Exception as exc:  # noqa: BLE001 —— 这里是刻意的"兜底墙"
            # 注意：这里捕获 Exception 是有意为之。
            # 网络超时、鉴权失败、余额不足、JSON 解析失败、服务端 500……
            # 任何一种都不应该让用户的日记写不进去，全部降级处理。
            if not settings.LLM_FALLBACK_ENABLED:
                raise
            print(f"[WARN] 大模型调用失败，降级为本地算法。原因：{type(exc).__name__}: {exc}")

            result = _local_rule_analyze(text)
            # 把失败原因记进 raw_response，方便调试时看
            result.raw_response = f"llm_failed[{type(exc).__name__}]: {exc}"
            return result

    # 没配 Key，直接走本地
    return _local_rule_analyze(text)
