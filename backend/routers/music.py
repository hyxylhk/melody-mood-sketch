"""
==============================================================================
backend/routers/music.py —— 音乐生成相关接口
==============================================================================
接口清单（带 /api 前缀）：

    POST /api/music/generate          为某篇日记生成配乐（立即返回，后台生成）
    GET  /api/music/track/{track_id}  轮询某次生成任务的状态
    GET  /api/music/latest/{diary_id} 直接取某篇日记最近一次的音乐

为什么是"提交 + 轮询"两段式？
    音乐生成是慢活。如果让 HTTP 请求一直挂着等待，
    浏览器会超时、用户体验也很差。分成两步后：
        第一步瞬间返回 track_id
        第二步前端每 2 秒问一次状态，看到 ready 就可以播了
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from backend import crud
from backend.database import get_db
from backend.schemas import MusicGenerateRequest, MusicTrackInfo, build_track_info
from backend.services.llm_emotion import analyze_emotion
from backend.services.music_service import build_music_prompt, generate_music_task

router = APIRouter(prefix="/music", tags=["音乐生成"])


@router.post(
    "/generate",
    response_model=MusicTrackInfo,
    status_code=202,               # 202 Accepted：已接受请求但还没处理完，语义准确
    summary="为指定日记生成情绪配乐",
)
async def generate_music(
    payload: MusicGenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> MusicTrackInfo:
    """
    触发一次音乐生成。

    流程：
        1. 找到目标日记
        2. 如果之前没分析过情绪（比如旧数据），现场补一次分析
        3. 创建一条 MusicTrack 记录（状态 pending）
        4. 把真正的生成逻辑注册成后台任务
        5. 立刻把 track 返回给前端
    """
    diary = crud.get_diary(db, payload.diary_id)
    if diary is None:
        raise HTTPException(status_code=404, detail=f"找不到 ID 为 {payload.diary_id} 的日记")

    # 容错：万一这篇日记还没分析过情绪（理论上不会发生），这里补一次
    if not diary.emotion_primary:
        emotion = await analyze_emotion(diary.content)
        diary = crud.apply_emotion_result(db, diary, emotion)

    emotion_result = crud.rebuild_emotion_result(diary)

    track = crud.create_music_track(
        db, diary_id=diary.id, prompt=build_music_prompt(emotion_result)
    )

    # 把任务丢到后台。注意传进去的是**普通参数**（int + 业务对象），
    # 千万不要把数据库 Session 传进后台任务 —— 那时请求已经结束、Session 已关闭。
    background_tasks.add_task(generate_music_task, track.id, emotion_result, payload.force_local)

    return build_track_info(track)


@router.get(
    "/track/{track_id}",
    response_model=MusicTrackInfo,
    summary="查询音乐生成进度（前端轮询用）",
)
def get_track(track_id: int, db: Session = Depends(get_db)) -> MusicTrackInfo:
    """
    轮询接口。前端每隔 2 秒请求一次：

        status == "processing" → 显示转圈的进度条
        status == "ready"      → 拿到 audio_url，塞进 <audio> 播放器
        status == "failed"     → 显示 error_message 并给个重试按钮
    """
    track = crud.get_music_track(db, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"找不到 ID 为 {track_id} 的音乐任务")
    return build_track_info(track)


@router.get(
    "/latest/{diary_id}",
    response_model=MusicTrackInfo,
    summary="获取某篇日记最近一次配乐",
)
def get_latest_track(diary_id: int, db: Session = Depends(get_db)) -> MusicTrackInfo:
    """列表页点某条日记时，用它直接取到上一次生成好的音乐，避免重复消耗。"""
    track = crud.get_latest_track(db, diary_id)
    if track is None:
        raise HTTPException(status_code=404, detail="这篇日记还没有生成过音乐")
    return build_track_info(track)
