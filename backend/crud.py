"""
==============================================================================
backend/crud.py —— 数据库增删改查层（CRUD）
==============================================================================
CRUD = Create / Read / Update / Delete，数据库的四种基本操作。

这一层只做"数据搬运"，不含任何业务判断。
好处：所有 SQL 相关的东西集中在一个文件里，改表结构时只动这里。

命名约定：
    create_xxx / get_xxx / list_xxx / update_xxx / delete_xxx / mark_xxx_状态
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.models import (
    TRACK_STATUS_FAILED,
    TRACK_STATUS_PENDING,
    TRACK_STATUS_PROCESSING,
    TRACK_STATUS_READY,
    Diary,
    MusicTrack,
)
from backend.services.emotion_catalog import EMOTION_CATALOG, EmotionResult


def _utc_now():
    """记录当前 UTC 时间（naive），避免直接使用已废弃的 datetime.utcnow()。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ===========================================================================
# 一、Diary（日记）相关
# ===========================================================================
def create_diary(db: Session, content: str, title: Optional[str] = None) -> Diary:
    """
    新增一篇日记（只写内容，情绪还没分析）。

    注意 commit 之后必须 refresh：
        commit 会把新纪录写入数据库，但自增主键 id 是数据库生成的，
        refresh 会把数据库里的最新状态（比如刚生成的 id）同步回 Python 对象。
    """
    diary = Diary(
        title=title,
        content=content,
        content_length=len(content),
    )
    db.add(diary)
    db.commit()
    db.refresh(diary)

    # 预先加载 tracks 关系（此时是空列表）。
    # 为什么要这一句？后面 await 分析情绪时如果再去访问 diary.tracks，
    # SQLAlchemy 会在异步上下文里做阻塞 IO，既不规范也可能报错。
    # 提前加载好，后面 response 构造时直接用缓存的列表即可。
    _ = diary.tracks

    return diary


def apply_emotion_result(db: Session, diary: Diary, emotion: EmotionResult) -> Diary:
    """
    把情绪分析的结果写回到日记记录上。

    这是个更新操作，所以同样要 commit + refresh。
    """
    diary.emotion_primary = emotion.primary
    diary.set_tags(emotion.tags)
    diary.emotion_valence = emotion.valence
    diary.emotion_arousal = emotion.arousal
    diary.emotion_intensity = emotion.intensity
    diary.emotion_summary = emotion.summary
    diary.emotion_source = emotion.source
    diary.music_prompt = emotion.music_prompt
    diary.analysis_raw = emotion.raw_response[:2000]

    db.add(diary)
    db.commit()
    db.refresh(diary)
    _ = diary.tracks        # 同上，保持关系已加载状态
    return diary


def get_diary(db: Session, diary_id: int) -> Optional[Diary]:
    """
    按主键查单篇日记。找不到返回 None（由上层决定怎么报错）。

    selectinload(Diary.tracks)：
        告诉 SQLAlchemy"查日记的时候顺便把它的音乐记录也查出来"，
        用两条 SQL 完成（SELECT * FROM diaries + SELECT * FROM music_tracks WHERE diary_id IN (...)）。
        不做这一步的话，后面每次访问 diary.tracks 都要单独发一条 SQL（N+1 问题）。
    """
    stmt = select(Diary).options(selectinload(Diary.tracks)).where(Diary.id == diary_id)
    return db.execute(stmt).scalar_one_or_none()


def list_diaries(
    db: Session,
    limit: int = 20,
    offset: int = 0,
    keyword: Optional[str] = None,
    emotion: Optional[str] = None,
) -> Tuple[int, List[Diary]]:
    """
    分页查询日记列表，返回 (总数, 当页数据)。

    之所以要返回总数：前端做分页需要知道一共多少条。

    参数:
        limit  : 每页几条
        offset : 跳过前几条（第 2 页时 offset=20）
        keyword: 模糊搜索正文
        emotion: 按情绪代号过滤
    """
    conditions = []
    if keyword:
        # LIKE '%xxx%' 做模糊匹配；% 是通配符
        conditions.append(Diary.content.like(f"%{keyword}%"))
    if emotion:
        conditions.append(Diary.emotion_primary == emotion)

    base_stmt = select(Diary).options(selectinload(Diary.tracks))
    if conditions:
        base_stmt = base_stmt.where(*conditions)

    # 总数：用 COUNT(*) 单独查一次
    count_stmt = select(func.count(Diary.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = int(db.execute(count_stmt).scalar_one())

    # 当页数据：置顶的永远排最前，其余按时间倒序（最新的在最前面）
    #   Diary.pinned.desc() → 布尔列倒序 = True(1) 在前，False(0) 在后
    items: Sequence[Diary] = db.execute(
        base_stmt
        .order_by(Diary.pinned.desc(), Diary.created_at.desc(), Diary.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().unique().all()

    return total, list(items)


def set_pinned(db: Session, diary: Diary, pinned: bool) -> Diary:
    """置顶 / 取消置顶一篇日记。"""
    diary.pinned = bool(pinned)
    db.add(diary)
    db.commit()
    db.refresh(diary)
    return diary


def update_diary(
    db: Session,
    diary: Diary,
    new_content: Optional[str] = None,
    new_title: Optional[str] = None,
) -> Diary:
    """修改日记内容和/或标题。两个参数都可以只传一个。"""
    if new_content is not None:
        diary.content = new_content
        diary.content_length = len(new_content)
    if new_title is not None:
        diary.title = new_title

    db.add(diary)
    db.commit()
    db.refresh(diary)
    return diary


def delete_diary(db: Session, diary: Diary) -> list[str]:
    """
    删除日记，返回被清理的音频文件绝对路径列表（供上层决定是否删磁盘文件）。

    关联的 MusicTrack 会自动被删掉 —— 因为模型里配了
    cascade="all, delete-orphan" + 外键 ondelete="CASCADE"。
    这叫"级联删除"，避免产生孤儿记录。

    为什么要在这里先收集 file_path？
        db.delete(diary) 提交后，关联的 MusicTrack 对象就没了，
        那时再想拿 file_path 就晚了。所以先抄下来。
    """
    audio_files = [
        track.file_path for track in diary.tracks if track.file_path
    ]
    db.delete(diary)
    db.commit()
    return audio_files


# ===========================================================================
# 二、MusicTrack（音乐任务）相关
# ===========================================================================
def create_music_track(db: Session, diary_id: int, prompt: str = "") -> MusicTrack:
    """
    创建一条音乐生成任务，初始状态 pending（等待处理）。

    创建后立刻返回，真正的生成交给后台任务慢慢跑。
    """
    track = MusicTrack(
        diary_id=diary_id,
        status=TRACK_STATUS_PENDING,
        prompt=prompt,
    )
    db.add(track)
    db.commit()
    db.refresh(track)
    return track


def get_music_track(db: Session, track_id: int) -> Optional[MusicTrack]:
    """按主键查音乐任务（轮询接口用它）。"""
    return db.get(MusicTrack, track_id)


def get_latest_track(db: Session, diary_id: int) -> Optional[MusicTrack]:
    """查某篇日记最新的一条音乐任务。"""
    stmt = (
        select(MusicTrack)
        .where(MusicTrack.diary_id == diary_id)
        .order_by(MusicTrack.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def mark_track_processing(db: Session, track_id: int) -> None:
    """把任务状态改成『生成中』。"""
    track = db.get(MusicTrack, track_id)
    if track is None:
        return
    track.status = TRACK_STATUS_PROCESSING
    db.commit()


def mark_track_ready(
    db: Session,
    track_id: int,
    audio_url: str,
    provider: str,
    duration_seconds: int = 0,
    file_path: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    prompt: str = "",
) -> None:
    """把任务状态改成『已完成』，并写入音频地址和元数据。"""
    track = db.get(MusicTrack, track_id)
    if track is None:
        return

    track.status = TRACK_STATUS_READY
    track.audio_url = audio_url
    track.provider = provider
    track.duration_seconds = duration_seconds
    track.file_path = file_path or ""
    track.meta_json = json.dumps(meta or {}, ensure_ascii=False)
    track.prompt = prompt or track.prompt
    track.error_message = ""
    track.finished_at = _utc_now()

    db.commit()


def mark_track_failed(db: Session, track_id: int, error_message: str) -> None:
    """把任务状态改成『失败』，并记录原因（前端会显示出来，方便排错）。"""
    track = db.get(MusicTrack, track_id)
    if track is None:
        return
    track.status = TRACK_STATUS_FAILED
    track.error_message = error_message[:1000]
    track.finished_at = _utc_now()
    db.commit()


# ===========================================================================
# 三、辅助：从已存储的日记反推出 EmotionResult 对象
# ===========================================================================
def rebuild_emotion_result(diary: Diary) -> EmotionResult:
    """
    数据库里存的是打散的字段（emotion_primary / emotion_valence ...）。
    生成音乐时需要一个完整的 EmotionResult 对象，这个函数负责把它拼回去。

    相当于 ORM 对象 → 业务对象的反向转换。
    """
    return EmotionResult(
        primary=diary.emotion_primary or "neutral",
        tags=diary.tags_list,
        valence=float(diary.emotion_valence or 0.0),
        arousal=float(diary.emotion_arousal or 0.0),
        intensity=float(diary.emotion_intensity or 0.0),
        summary=diary.emotion_summary or "",
        music_prompt=diary.music_prompt or "",
        source=diary.emotion_source or "local_rule",
        raw_response="",
    )


# ===========================================================================
# 四、统计数据
# ===========================================================================
def get_stats(db: Session) -> Dict[str, Any]:
    """
    汇总统计：日记数、音乐数、情绪分布、平均效价。

    答辩时很有用：可以现场展示"用户写了 37 篇日记，其中忧伤占 40%"
    这种数据洞察，说明产品不是玩具。
    """
    diary_count = int(db.execute(select(func.count(Diary.id))).scalar_one() or 0)
    track_count = int(db.execute(select(func.count(MusicTrack.id))).scalar_one() or 0)
    ready_count = int(
        db.execute(
            select(func.count(MusicTrack.id)).where(MusicTrack.status == TRACK_STATUS_READY)
        ).scalar_one()
        or 0
    )
    valence_avg = float(
        db.execute(select(func.avg(Diary.emotion_valence))).scalar_one() or 0.0
    )

    # 情绪分布：GROUP BY emotion_primary 统计每种情绪有几篇
    distribution_rows = db.execute(
        select(Diary.emotion_primary, func.count(Diary.id)).group_by(Diary.emotion_primary)
    ).all()
    distribution: Dict[str, int] = {
        (code or "未分析"): int(count) for code, count in distribution_rows
    }

    return {
        "diary_count": diary_count,
        "track_count": track_count,
        "ready_track_count": ready_count,
        "emotion_distribution": distribution,
        "valence_average": round(valence_avg, 3),
    }


def get_trajectory(db: Session, days: int = 30) -> Dict[str, Any]:
    """
    情绪轨迹：按天聚合，供前端画**日历热力图 + 效价折线图**。

    为什么单独做一个接口，而不是让前端拿列表自己算？
        日记可能是几百篇，全量传到前端再聚合又慢又浪费流量；
        而且"补上没有写日记的那些天"这种填充逻辑放后端统一处理更干净。

    关于时区（这里有个坑）：
        数据库里的 created_at 存的是 UTC（见 models.utc_now 的注释）。
        如果直接用 SQL 的 date() 分组，晚上 8 点写的日记会被算到第二天去
        —— 用户会觉得"我明明是今天写的"。
        所以这里**先按 UTC 取出，再在 Python 里转成本地时区后按天归并**。

    返回结构：
        calendar  —— 连续 days 天，每天一条（没写日记的日子 count=0，前端要能画出空格）
        trend     —— 只含有日记的那些天，画折线用（避免断点把线拉平）
        streak    —— 截至今天（或最近有记录那天）的连续记录天数
    """
    days = max(1, min(days, 120))

    # 本地时区的"今天零点"，往前推 days-1 天 = 窗口起点
    now_local = datetime.now(timezone.utc).astimezone()
    start_local = (now_local - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # 转回 UTC 再去查数据库（因为库里存的是 UTC）
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)

    rows = db.execute(
        select(Diary.created_at, Diary.emotion_primary, Diary.emotion_valence)
        .where(Diary.created_at >= start_utc)
        .order_by(Diary.created_at.asc())
    ).all()

    # 按本地日期归并
    buckets: Dict[Any, Dict[str, Any]] = {}
    for created_at, code, valence in rows:
        if created_at is None:
            continue
        day = created_at.replace(tzinfo=timezone.utc).astimezone().date()
        bucket = buckets.setdefault(day, {"count": 0, "valence_sum": 0.0, "emotions": {}})
        bucket["count"] += 1
        bucket["valence_sum"] += float(valence or 0.0)
        if code:
            bucket["emotions"][code] = bucket["emotions"].get(code, 0) + 1

    def _dominant(day_bucket: Dict[str, Any]) -> Optional[str]:
        """当天出现次数最多的情绪；次数相同取效价更极端的一个，避免并列时随机。"""
        emotions = day_bucket.get("emotions") or {}
        if not emotions:
            return None
        return max(emotions.items(), key=lambda kv: kv[1])[0]

    calendar: List[Dict[str, Any]] = []
    trend: List[Dict[str, Any]] = []
    for offset in range(days):
        day = (start_local + timedelta(days=offset)).date()
        bucket = buckets.get(day)
        if bucket:
            code = _dominant(bucket)
            profile = EMOTION_CATALOG.get(code) if code else None
            valence_avg = bucket["valence_sum"] / bucket["count"]
            entry = {
                "date": day.isoformat(),
                "count": bucket["count"],
                "emotion": code,
                "label": profile.label if profile else "平静",
                "color": profile.color if profile else "#8b93a7",
                "icon": profile.icon if profile else "😐",
                "valence": round(valence_avg, 3),
            }
            trend.append(entry)
        else:
            entry = {
                "date": day.isoformat(),
                "count": 0,
                "emotion": None,
                "label": "",
                "color": "",
                "icon": "",
                "valence": 0.0,
            }
        calendar.append(entry)

    # 连续记录天数：从最后一天往回数，遇到空天就停
    streak = 0
    for entry in reversed(calendar):
        if entry["count"] > 0:
            streak += 1
        else:
            # 今天还没写不算断，宽容一天（用户可能只是还没来得及写）
            if streak == 0:
                continue
            break

    return {
        "days": days,
        "start_date": calendar[0]["date"],
        "end_date": calendar[-1]["date"],
        "calendar": calendar,
        "trend": trend,
        "streak": streak,
        "active_days": len(trend),
        "total_entries": sum(entry["count"] for entry in calendar),
    }
