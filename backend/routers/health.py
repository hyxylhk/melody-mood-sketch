"""
==============================================================================
backend/routers/health.py —— 系统自检 & 元信息接口
==============================================================================
这里的接口不属于业务，主要服务于：
    1. 启动后先 ping 一下，确认服务活着
    2. 演示前确认大模型 Key 有没有配好、现在走的是哪条音乐通道
    3. 给前端提供情绪字典（画情绪色板/图例）
"""

from __future__ import annotations

import sys

from fastapi import APIRouter

from backend.config import get_settings
from backend.schemas import EmotionOption, HealthResponse
from backend.services.emotion_catalog import list_emotion_options
from backend.services.music_library import library_stats
from backend.services.music_service import resolve_provider

# APIRouter：把一组相关接口打包，最后统一挂到主 app 上。
# tags 只影响自动生成的接口文档的分组显示。
router = APIRouter(tags=["系统与元信息"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
def health_check() -> HealthResponse:
    """
    服务健康自检。

    答辩演示第一步：浏览器打开 http://127.0.0.1:8000/api/health
    看到 "status": "ok" 就说明后端起来了。

    顺便把关键配置也返回出来（但不返回密钥本身，只返回"有没有配"），
    方便一眼判断当前跑的是哪条技术路线。
    """
    settings = get_settings()
    return HealthResponse(
        status="ok",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        python=sys.version.split()[0],
        database=settings.database_path.name,
        llm_enabled=settings.llm_enabled,
        llm_model=settings.LLM_MODEL if settings.llm_enabled else "未配置（走本地规则）",
        music_provider_effective=resolve_provider(),
        music_provider_config=settings.MUSIC_PROVIDER,
        external_music_enabled=settings.external_music_enabled,
        music_library_count=int(library_stats()["track_count"]),
        music_library_first=settings.MUSIC_LIBRARY_FIRST,
    )


@router.get(
    "/meta/emotions",
    response_model=list[EmotionOption],
    summary="获取全部情绪类型",
)
def list_emotions() -> list[EmotionOption]:
    """
    返回 16 种情绪的元数据（代号、中文名、配色、效价/唤醒度）。

    前端用它来：
        - 画情绪图例
        - 把后端返回的英文代号 joy 渲染成带 emoji 和颜色的中文标签「😄 喜悦」
    """
    return [EmotionOption(**item) for item in list_emotion_options()]
