"""
==============================================================================
backend/models.py —— 数据库表结构（ORM 模型）
==============================================================================
这里定义两张表：

    diaries（日记表）          存：正文、标题、情绪分析结果
        │
        │ 1 : N  一篇日记可以生成多首曲子（重新生成、换风格都算新的一首）
        ▼
    music_tracks（音乐任务表） 存：生成状态、生成通道、音频地址

给 C++ 同学的类比：
    每个类 = 一张表；每个类属性 = 一个字段（列）；每个实例 = 一行记录。
    relationship() 相当于表之间的指针/引用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base


# ---------------------------------------------------------------------------
# 工具函数：记录 UTC 时间
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    """
    当前 UTC 时间（naive，不带时区信息）。

    为什么要统一存 UTC？
        UTC 到哪台机器都一样，展示时再按用户本地时区转换。
        如果直接存本地时间，换台机器/换个时区部署就会全部错乱。

    为什么不直接用 datetime.utcnow()？
        Python 3.12 起它已被标记为废弃。这里先用带时区的 now(UTC) 拿时间，
        再去掉时区信息变成 naive，兼容 SQLite 存储的同时避免 FutureWarning。

    注意 SQLAlchemy 的 default 接收的是**函数本身**（不带括号），
    它会在每次插入时调用这个函数生成新值。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Diary(Base):
    """日记表"""

    __tablename__ = "diaries"

    # ---------------- 主键与内容 ----------------
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, default=None)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_length: Mapped[int] = mapped_column(Integer, default=0)

    # ---------------- 时间戳 ----------------
    # index=True 表示建索引：按时间倒序查列表时会快很多
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    # ---------------- 情绪分析结果 ----------------
    # 主情绪代号，对应 emotion_catalog.EMOTION_CATALOG 的 key
    emotion_primary: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True, default=None
    )
    # 次要情绪：数据库里存逗号分隔的字符串 "nostalgia,sadness"
    # （SQLite 没有数组类型，这是最通用的做法）
    emotion_tags: Mapped[str] = mapped_column(String(200), default="")
    # 效价/唤醒度/强度：心理学上描述情绪的三个连续维度
    emotion_valence: Mapped[float] = mapped_column(Float, default=0.0)
    emotion_arousal: Mapped[float] = mapped_column(Float, default=0.0)
    emotion_intensity: Mapped[float] = mapped_column(Float, default=0.0)
    emotion_summary: Mapped[str] = mapped_column(Text, default="")
    # 分析结果来源：llm（大模型） / local_rule（本地兜底算法）
    emotion_source: Mapped[str] = mapped_column(String(32), default="pending")
    # 英文音乐生成提示词
    music_prompt: Mapped[str] = mapped_column(Text, default="")
    # 大模型原始返回，排错用（比如模型返回了奇怪的东西）
    analysis_raw: Mapped[str] = mapped_column(Text, default="")

    # 置顶标记：True 的日记在时间线里永远排在最前面
    # （SQLite 里布尔值实际存成 0/1 整数，SQLAlchemy 的 Boolean 类型会自动转换）
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # ---------------- 关系 ----------------
    # back_populates 让两边互相可见：diary.tracks 和 track.diary
    # cascade="all, delete-orphan" 表示删日记时它下面的音乐记录也一起删
    tracks: Mapped[List["MusicTrack"]] = relationship(
        "MusicTrack",
        back_populates="diary",
        cascade="all, delete-orphan",
        order_by="MusicTrack.id.desc()",
    )

    # ---------------- 辅助属性（不会落库，只是方便在 Python 里用）----------------
    @property
    def tags_list(self) -> List[str]:
        """把 "joy,hope" 这种字符串还原成 ["joy", "hope"]。"""
        if not self.emotion_tags:
            return []
        return [tag for tag in self.emotion_tags.split(",") if tag]

    def set_tags(self, tags: List[str]) -> None:
        """把列表存成逗号分隔字符串。"""
        self.emotion_tags = ",".join(tags[:5])

    @property
    def latest_track(self) -> Optional["MusicTrack"]:
        """拿到最新一条音乐生成记录（tracks 已按 id 倒序，索引 0 就是最新的）。"""
        return self.tracks[0] if self.tracks else None

    def __repr__(self) -> str:  # pragma: no cover —— 仅调试用
        return f"<Diary id={self.id} emotion={self.emotion_primary} len={len(self.content)}>"


class MusicTrack(Base):
    """音乐生成任务表"""

    __tablename__ = "music_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    diary_id: Mapped[int] = mapped_column(
        # ForeignKey("diaries.id") 表示这一列引用 diaries 表的 id 列
        ForeignKey("diaries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 生成状态流转：pending → processing → ready / failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 实际使用的生成通道：local（本地算法合成） / httpapi（第三方 API）
    provider: Mapped[str] = mapped_column(String(32), default="")
    # 给前端播放用的地址：本地生成的填 /media/xxx.wav；第三方的填 https://...
    audio_url: Mapped[str] = mapped_column(String(600), default="")
    # 本地生成的文件绝对路径（清理磁盘用，前端不需要）
    file_path: Mapped[str] = mapped_column(String(600), default="")
    # 音频时长（秒）
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    # 生成时用的提示词
    prompt: Mapped[str] = mapped_column(Text, default="")
    # 生成元数据的 JSON 字符串（BPM、调式、随机种子等）
    meta_json: Mapped[str] = mapped_column(Text, default="")
    # 失败原因
    error_message: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 反向引用：track.diary 可以拿到所属日记对象
    diary: Mapped["Diary"] = relationship("Diary", back_populates="tracks")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MusicTrack id={self.id} diary={self.diary_id} status={self.status}>"


# 联合索引："查某篇日记的最新音乐" 是最常见的查询，给它单独建索引
Index("ix_music_tracks_diary_id_status", MusicTrack.diary_id, MusicTrack.status)


# ---------------------------------------------------------------------------
# 状态常量：避免魔法字符串散落在各处（用常量，写错会在 import 阶段就炸）
# ---------------------------------------------------------------------------
TRACK_STATUS_PENDING = "pending"
TRACK_STATUS_PROCESSING = "processing"
TRACK_STATUS_READY = "ready"
TRACK_STATUS_FAILED = "failed"
