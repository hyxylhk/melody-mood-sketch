"""
==============================================================================
backend/schemas.py —— 请求 / 响应的数据结构（DTO）
==============================================================================
什么是 DTO？
    Data Transfer Object，数据传输对象。
    作用：**规定 API 接收什么格式、返回什么格式，并自动做校验**。

为什么不让前端直接看数据库模型？
    1. 安全：数据库模型可能有敏感字段（比如 file_path 的绝对路径）不想暴露
    2. 稳定：数据库改了字段不影响接口契约
    3. 便利：pydantic 自动生成 JSON Schema，FastAPI 靠它自动生成接口文档

给 C++ 同学的类比：
    这就是一组带校验的 struct，加上序列化/反序列化能力。

本文件里有两类东西：
    - *Request  ：前端 → 后端，进来时自动校验，不合法直接返回 422
    - *Response ：后端 → 前端，决定 JSON 长什么样
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.services.emotion_catalog import get_profile


# ===========================================================================
# 一、请求体（前端 → 后端）
# ===========================================================================
class DiaryCreateRequest(BaseModel):
    """写日记的请求体。"""

    content: str = Field(
        ...,                       # ... 表示必填
        min_length=1,
        max_length=5000,
        description="日记正文",
    )
    title: Optional[str] = Field(default=None, max_length=120, description="标题，可留空")
    auto_generate_music: bool = Field(
        default=True,
        description="是否在保存后立即自动开始生成配乐",
    )

    @field_validator("content")
    @classmethod
    def _strip_and_check(cls, value: str) -> str:
        """
        清洗输入：
            - 去掉首尾空白（防止用户只敲了几个空格也算一篇日记）
            - 掐掉 5000 字以外的部分
        """
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("日记内容太短了，至少写 2 个字吧")
        return cleaned[:5000]

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value else None


class DiaryUpdateRequest(BaseModel):
    """修改日记（标题 / 正文）。改完可以重新触发情绪分析。"""

    content: Optional[str] = Field(default=None, min_length=2, max_length=5000)
    title: Optional[str] = Field(default=None, max_length=120)


class DiaryPinRequest(BaseModel):
    """置顶 / 取消置顶的请求体。"""

    pinned: bool = Field(..., description="true=置顶，false=取消置顶")


class MusicGenerateRequest(BaseModel):
    """为某篇日记生成/重新生成音乐。"""

    diary_id: int = Field(..., gt=0, description="目标日记 ID")
    force_local: bool = Field(
        default=False,
        description="是否强制使用本地合成（演示零成本路线时很有用）",
    )


# ===========================================================================
# 二、响应体（后端 → 前端）
# ===========================================================================
class EmotionTag(BaseModel):
    """一个情绪标签（前端渲染彩色小胶囊用）"""

    code: str = Field(..., description="情绪代号，如 joy")
    label: str = Field(..., description="中文名，如 喜悦")
    color: str = Field(..., description="主题色")
    icon: str = Field(..., description="emoji")


class EmotionInfo(BaseModel):
    """完整的情绪分析结果。前端的情绪卡片就渲染这个。"""

    primary: str = Field(..., description="主情绪代号")
    primary_label: str = Field(..., description="主情绪中文名")
    primary_color: str = Field(..., description="主情绪主题色")
    primary_icon: str = Field(..., description="主情绪 emoji")
    description: str = Field("", description="一句话解释这个情绪")

    tags: List[EmotionTag] = Field(default_factory=list, description="次要情绪标签")

    valence: float = Field(0.0, ge=-1, le=1, description="效价：-1 极负面 ~ +1 极正面")
    arousal: float = Field(0.0, ge=0, le=1, description="唤醒度：0 平静 ~ 1 激动")
    intensity: float = Field(0.0, ge=0, le=1, description="情绪强度")
    summary: str = Field("", description="一句话情绪解读")
    source: str = Field("", description="分析来源：llm / local_rule")


class MusicTrackInfo(BaseModel):
    """一条音乐生成任务的状态。前端靠反复请求这个接口来轮询生成进度。"""

    id: int
    diary_id: int
    status: str = Field(..., description="pending / processing / ready / failed")
    provider: str = Field("", description="local / httpapi")
    audio_url: str = Field("", description="播放地址，ready 之后才有值")
    duration_seconds: int = 0
    prompt: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict, description="BPM / 调式 / 随机种子等")
    error_message: str = Field("", description="失败原因")
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class DiaryResponse(BaseModel):
    """单篇日记的完整信息（详情页用）"""

    id: int
    title: Optional[str] = None
    content: str
    content_length: int = 0
    pinned: bool = False
    created_at: datetime
    updated_at: datetime
    emotion: Optional[EmotionInfo] = None
    music: Optional[MusicTrackInfo] = None


class DiaryListItem(BaseModel):
    """日记列表里的一行（列表页用，字段精简，不带全文）"""

    id: int
    title: Optional[str] = None
    excerpt: str = Field("", description="正文摘要，最多 60 字")
    emotion_primary: Optional[str] = None
    emotion_label: str = "待分析"
    emotion_color: str = "#9AA0A6"
    emotion_icon: str = "😐"
    tags: List[str] = Field(default_factory=list)
    valence: float = 0.0
    pinned: bool = False
    created_at: datetime
    has_music: bool = False
    audio_url: str = ""


class DiaryListResponse(BaseModel):
    """分页日记列表"""

    total: int
    items: List[DiaryListItem]


class HealthResponse(BaseModel):
    """
    健康检查响应。

    演示时先调这个接口，确认后端活没活着、大模型配没配好。
    """
    status: str = "ok"
    app: str = ""
    version: str = ""
    python: str = ""
    database: str = ""
    llm_enabled: bool = False
    llm_model: str = ""
    music_provider_effective: str = ""
    music_provider_config: str = ""
    external_music_enabled: bool = False
    music_library_count: int = 0
    music_library_first: bool = True


class StatsResponse(BaseModel):
    """统计数据：用于答辩时展示数据沉淀能力"""
    diary_count: int = 0
    track_count: int = 0
    ready_track_count: int = 0
    emotion_distribution: Dict[str, int] = Field(default_factory=dict)
    valence_average: float = 0.0


class TrajectoryDay(BaseModel):
    """
    情绪轨迹里的一天。

    count = 0 表示这天没写日记 —— 前端画日历热力图时要画成空格，
    但**不能省略这条数据**，否则日历的格子会错位（缺失的天没有占位）。
    """
    date: str = ""
    count: int = 0
    emotion: Optional[str] = None
    label: str = ""
    color: str = ""
    icon: str = ""
    valence: float = 0.0


class TrajectoryResponse(BaseModel):
    """
    情绪变化轨迹（对应作品介绍里"留存自己的情绪变化轨迹"这句承诺）。

    calendar —— 连续 days 天，含空白天，画日历热力图
    trend    —— 只含有记录的天，画效价折线图（避免空白天把折线拉平）
    streak   —— 连续记录天数，产品上用来激励用户"别断更"
    """
    days: int = 0
    start_date: str = ""
    end_date: str = ""
    calendar: List[TrajectoryDay] = Field(default_factory=list)
    trend: List[TrajectoryDay] = Field(default_factory=list)
    streak: int = 0
    active_days: int = 0
    total_entries: int = 0


class MessageResponse(BaseModel):
    """通用的一句消息返回（比如删除成功）"""
    message: str
    detail: str = ""


class EmotionOption(BaseModel):
    """情绪字典的一项，前端画情绪图例用"""
    code: str
    label: str
    color: str
    icon: str
    valence: float
    arousal: float


# ===========================================================================
# 三、ORM 对象 → 响应对象的转换器
# ===========================================================================
# 为什么不直接用 model_validate(orm_object)？
#     因为嵌套结构（emotion 里的 tags 需要去查情绪字典才能补齐中文名和颜色）
#     自动转换做不到这种"业务加工"。手动写转换器虽然啰嗦，
#     但每一行在做什么一目了然 —— 对不熟悉后端的同学反而更友好。

def _tags_to_info(codes: List[str]) -> List[EmotionTag]:
    """把 ["hope", "gratitude"] 转成带中文名和颜色的对象列表。"""
    result: List[EmotionTag] = []
    for code in codes:
        profile = get_profile(code)
        result.append(
            EmotionTag(
                code=profile.code,
                label=profile.label,
                color=profile.color,
                icon=profile.icon,
            )
        )
    return result


def build_emotion_info(diary: Any) -> Optional[EmotionInfo]:
    """从 Diary 对象构造情绪信息。没有分析过则返回 None。"""
    if not diary.emotion_primary:
        return None

    profile = get_profile(diary.emotion_primary)
    return EmotionInfo(
        primary=profile.code,
        primary_label=profile.label,
        primary_color=profile.color,
        primary_icon=profile.icon,
        description=profile.description,
        tags=_tags_to_info(diary.tags_list),
        valence=float(diary.emotion_valence or 0.0),
        arousal=float(diary.emotion_arousal or 0.0),
        intensity=float(diary.emotion_intensity or 0.0),
        summary=diary.emotion_summary or "",
        source=diary.emotion_source or "",
    )


def build_track_info(track: Any) -> MusicTrackInfo:
    """从 MusicTrack 对象构造任务状态信息。"""
    meta: Dict[str, Any] = {}
    if track.meta_json:
        try:
            meta = json.loads(track.meta_json)
        except json.JSONDecodeError:
            # 元信息解析失败不影响主流程，宁可丢个空字典也不能让接口崩
            meta = {}

    return MusicTrackInfo(
        id=track.id,
        diary_id=track.diary_id,
        status=track.status,
        provider=track.provider or "",
        audio_url=track.audio_url or "",
        duration_seconds=int(track.duration_seconds or 0),
        prompt=track.prompt or "",
        meta=meta,
        error_message=track.error_message or "",
        created_at=track.created_at,
        finished_at=track.finished_at,
    )


def build_diary_response(diary: Any, music_track: Any = None) -> DiaryResponse:
    """
    完整日记响应（含最新一条音乐任务状态）。

    参数 music_track 存在的原因：
        新建流程里，刚刚插入数据库的 diary 对象并不知道后来才创建的 MusicTrack，
        这时由调用方显式把新任务传进来，保证响应里带上最新的音乐状态。
    """
    latest = music_track if music_track is not None else diary.latest_track
    return DiaryResponse(
        id=diary.id,
        title=diary.title,
        content=diary.content,
        content_length=diary.content_length or len(diary.content),
        pinned=bool(getattr(diary, "pinned", False)),
        created_at=diary.created_at,
        updated_at=diary.updated_at,
        emotion=build_emotion_info(diary),
        music=build_track_info(latest) if latest else None,
    )


def build_diary_list_item(diary: Any) -> DiaryListItem:
    """列表项（正文截断、情绪信息展平，方便前端直接渲染）。"""
    profile = get_profile(diary.emotion_primary or "")
    content = diary.content or ""
    excerpt = content if len(content) <= 60 else content[:60] + "…"

    latest = diary.latest_track

    return DiaryListItem(
        id=diary.id,
        title=diary.title,
        excerpt=excerpt,
        emotion_primary=diary.emotion_primary,
        emotion_label=profile.label if diary.emotion_primary else "待分析",
        emotion_color=profile.color,
        emotion_icon=profile.icon,
        tags=diary.tags_list,
        valence=float(diary.emotion_valence or 0.0),
        pinned=bool(diary.pinned),
        created_at=diary.created_at,
        has_music=bool(latest and latest.status == "ready" and latest.audio_url),
        audio_url=latest.audio_url if latest and latest.status == "ready" else "",
    )
