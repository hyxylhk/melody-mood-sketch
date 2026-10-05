"""
==============================================================================
backend/routers/diary.py —— 日记相关的 HTTP 接口
==============================================================================
接口清单（最终 URL 会有 /api 前缀）：

    POST   /api/diary                写新日记（自动分析情绪 + 可选自动配乐）
    GET    /api/diary                日记列表（分页 / 搜索 / 按情绪过滤）
    GET    /api/diary/stats          统计数据
    GET    /api/diary/{diary_id}     单篇详情
    PATCH  /api/diary/{diary_id}     修改日记
    DELETE /api/diary/{diary_id}     删除日记

给不懂后端的同学讲两个关键概念：

【装饰器 @router.post("/xxx")】
    作用是把下面的函数注册成"收到某个 URL 的某种请求时，调用这个函数"。
    可以理解为路由表的一行：URL → 处理函数。

【依赖注入 Depends(get_db)】
    参数写成 db: Session = Depends(get_db)，FastAPI 会在调用前自动
    帮你开一个数据库会话并传进来，请求结束后自动关掉。
    你不需要写任何 new / delete —— 类似 C++ 里由框架管理的智能指针。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend import crud
from backend.database import get_db
from backend.schemas import (
    DiaryCreateRequest,
    DiaryListResponse,
    DiaryListItem,
    DiaryPinRequest,
    DiaryResponse,
    DiaryUpdateRequest,
    MessageResponse,
    StatsResponse,
    TrajectoryResponse,
    build_diary_list_item,
    build_diary_response,
)
from backend.services.llm_emotion import analyze_emotion
from backend.services.music_service import build_music_prompt, generate_music_task

router = APIRouter(prefix="/diary", tags=["日记"])


# ---------------------------------------------------------------------------
# 1. 写新日记
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=DiaryResponse,
    status_code=201,                       # 201 Created：更符合 REST 语义
    summary="写一篇新日记并分析情绪",
    description="保存正文 → 调用 LLM 分析情绪 →（可选）在后台开始生成配乐。",
)
async def create_diary(
    payload: DiaryCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> DiaryResponse:
    """
    主链路接口。整个产品的核心就是这一个请求。

    执行流程：
        1. 先把正文写进数据库（哪怕后面情绪分析失败，用户的日记也不会丢）
        2. 异步调用大模型做情绪分析（失败会自动降级到本地算法）
        3. 把情绪结果写回数据库
        4. 如果前端要求自动配乐 → 建一条音乐任务，丢给后台慢慢生成
        5. 立刻返回（不等待音乐生成，否则请求要好几秒甚至几分钟）

    BackgroundTasks 是什么？
        它让你的 handler 可以先返回响应，FastAPI 在响应发出去之后
        再执行 add_task 注册的函数。这是 FastAPI 内置的轻量任务队列。
    """
    # ---------- 1) 落库 ----------
    diary = crud.create_diary(db, content=payload.content, title=payload.title)

    # ---------- 2) 情绪分析（await 表示这一步要等网络返回）----------
    emotion = await analyze_emotion(payload.content)

    # ---------- 3) 写回分析结果 ----------
    diary = crud.apply_emotion_result(db, diary, emotion)

    # ---------- 4) 需要的话起后台配乐任务 ----------
    track = None
    if payload.auto_generate_music:
        # 从数据库对象反推出业务对象，传给后台任务
        emotion_result = crud.rebuild_emotion_result(diary)
        track = crud.create_music_track(
            db, diary_id=diary.id, prompt=build_music_prompt(emotion_result)
        )
        background_tasks.add_task(generate_music_task, track.id, emotion_result)

    # ---------- 5) 组装响应 ----------
    return build_diary_response(diary, music_track=track)


# ---------------------------------------------------------------------------
# 2. 日记列表
# ---------------------------------------------------------------------------
@router.get("", response_model=DiaryListResponse, summary="日记列表（分页/搜索/过滤）")
def list_diaries(
    page: int = Query(1, ge=1, description="第几页，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    keyword: str | None = Query(None, description="按正文模糊搜索"),
    emotion: str | None = Query(None, description="按情绪代号过滤，如 joy / sadness"),
    db: Session = Depends(get_db),
) -> DiaryListResponse:
    """
    分页列表。

    Query(...) 表示这个参数从 URL 的查询串里取，例如：
        GET /api/diary?page=2&page_size=10&keyword=加班

    ge / le 是取值约束（greater equal / less equal），
    传 page=0 会被直接拒绝并返回 422，不需要我们在函数体里手写判断。
    """
    offset = (page - 1) * page_size
    total, diaries = crud.list_diaries(
        db, limit=page_size, offset=offset, keyword=keyword, emotion=emotion
    )

    items: list[DiaryListItem] = [build_diary_list_item(item) for item in diaries]
    return DiaryListResponse(total=total, items=items)


# ---------------------------------------------------------------------------
# 3. 统计（注意：必须在 /{diary_id} 之前注册，否则 "stats" 会被当成 ID 去匹配）
# ---------------------------------------------------------------------------
@router.get("/stats", response_model=StatsResponse, summary="情绪/音乐数据统计")
def get_stats(db: Session = Depends(get_db)) -> StatsResponse:
    """返回日记总数、音乐总数、情绪分布、平均效价。答辩时展示数据沉淀用。"""
    data = crud.get_stats(db)
    return StatsResponse(**data)


# ---------------------------------------------------------------------------
# 3.5 情绪轨迹（日历热力图 + 效价折线图的数据源）
# 注意：和 /stats 一样必须排在 /{diary_id} 之前，否则 "trajectory" 会被当成 ID
# ---------------------------------------------------------------------------
@router.get("/trajectory", response_model=TrajectoryResponse, summary="情绪变化轨迹（按天聚合）")
def get_trajectory(
    days: int = Query(default=30, ge=1, le=120, description="往前看多少天（1~120）"),
    db: Session = Depends(get_db),
) -> TrajectoryResponse:
    """
    按天聚合情绪数据，供前端画「日历热力图 + 效价折线图」。

    这是作品介绍里"留存自己的情绪变化轨迹"这句承诺的落地接口 ——
    没有它，时间线只是"一条列表"，称不上"轨迹"。
    """
    return TrajectoryResponse(**crud.get_trajectory(db, days=days))


# ---------------------------------------------------------------------------
# 4. 单篇详情
# ---------------------------------------------------------------------------
@router.get("/{diary_id}", response_model=DiaryResponse, summary="获取单篇日记详情")
def get_diary(diary_id: int, db: Session = Depends(get_db)) -> DiaryResponse:
    """
    按 ID 查详情。

    HTTPException(404) 是 FastAPI 抛错的标准写法：
        它会立刻中断函数执行，返回对应的 HTTP 状态码和 JSON 错误信息。
    """
    diary = crud.get_diary(db, diary_id)
    if diary is None:
        raise HTTPException(status_code=404, detail=f"找不到 ID 为 {diary_id} 的日记")
    return build_diary_response(diary)


# ---------------------------------------------------------------------------
# 5. 修改日记
# ---------------------------------------------------------------------------
@router.patch(
    "/{diary_id}",
    response_model=DiaryResponse,
    summary="修改日记内容或标题",
    description="修改正文后会重新做一次情绪分析，保证情绪标签和内容始终一致。",
)
async def update_diary(
    diary_id: int,
    payload: DiaryUpdateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> DiaryResponse:
    diary = crud.get_diary(db, diary_id)
    if diary is None:
        raise HTTPException(status_code=404, detail=f"找不到 ID 为 {diary_id} 的日记")

    diary = crud.update_diary(db, diary, new_content=payload.content, new_title=payload.title)

    # 内容变了 → 情绪要重新分析，否则标签会和新内容不符
    if payload.content is not None:
        emotion = await analyze_emotion(payload.content)
        diary = crud.apply_emotion_result(db, diary, emotion)

    # 更新后返回。这里不自动重生成音乐，避免用户改一个字就烧一次 API 预算；
    # 真想重新配乐请调 POST /api/music/generate。
    return build_diary_response(diary)


# ---------------------------------------------------------------------------
# 6. 置顶 / 取消置顶
# ---------------------------------------------------------------------------
@router.patch(
    "/{diary_id}/pin",
    response_model=DiaryResponse,
    summary="置顶或取消置顶一篇日记",
    description="置顶后该日记在时间线里永远排在最前面（其余仍按时间倒序）。",
)
def pin_diary(
    diary_id: int,
    payload: DiaryPinRequest,
    db: Session = Depends(get_db),
) -> DiaryResponse:
    """
    置顶是很轻的操作：只改一个布尔字段，不碰正文和音乐。

    为什么不塞进上面的 PATCH /{diary_id} 通用修改接口？
        通用修改接口会触发"重新做情绪分析"等重逻辑；
        置顶要求毫秒级响应（用户会连点），所以拆成独立小接口，各管各的。
    """
    diary = crud.get_diary(db, diary_id)
    if diary is None:
        raise HTTPException(status_code=404, detail=f"找不到 ID 为 {diary_id} 的日记")

    diary = crud.set_pinned(db, diary, payload.pinned)
    return build_diary_response(diary)


# ---------------------------------------------------------------------------
# 7. 删除日记
# ---------------------------------------------------------------------------
@router.delete("/{diary_id}", response_model=MessageResponse, summary="删除日记")
def delete_diary(diary_id: int, db: Session = Depends(get_db)) -> MessageResponse:
    """
    删除日记及其关联的音乐任务记录，并顺手清掉已生成到磁盘的 wav 文件，
    避免删多了之后 storage/audio 里堆满"没有主人"的孤儿音频。
    """
    diary = crud.get_diary(db, diary_id)
    if diary is None:
        raise HTTPException(status_code=404, detail=f"找不到 ID 为 {diary_id} 的日记")

    removed_files = crud.delete_diary(db, diary)

    # 数据库删干净后再删磁盘文件。就算文件删除失败也不影响主流程
    # （文件已经没有数据库记录引用它，顶多算垃圾，不影响正确性）
    cleaned = 0
    for file_path in removed_files:
        try:
            Path(file_path).unlink(missing_ok=True)
            cleaned += 1
        except OSError:
            continue

    detail = f"diary_id={diary_id}"
    if cleaned:
        detail += f", 已清理音频文件 x{cleaned}"
    return MessageResponse(message="删除成功", detail=detail)
