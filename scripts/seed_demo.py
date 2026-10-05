"""
============================================================================
scripts/seed_demo.py —— 演示种子数据（独立演示库，一键装载 / 卸载）
============================================================================
为什么需要它
============
答辩时打开一个只有 5 篇日记的空页面，日历热力图和效价折线图几乎是空的 ——
"情绪变化轨迹"这个功能当场就演示不出来。
所以这里准备一份**仿真 30 天**的演示数据。

为什么用独立库
==============
直接往你自己的 diary.db 里塞假数据会和你真实写的日记混在一起，删起来很麻烦。
所以演示数据写进**独立的 storage/diary_demo.db**，
用 --load / --unload 切换 .env 里的 DATABASE_URL：
    --load    → 服务切到演示库（你的真实日记原封不动）
    --unload  → 切回主库
两条命令都不删任何数据。

用法
====
    python scripts/seed_demo.py --build          # 生成演示数据（默认 30 天）
    python scripts/seed_demo.py --load           # 装载：切换到演示库
    # （重启服务）python run.py
    python scripts/seed_demo.py --unload         # 卸载：切回你的真实数据
    python scripts/seed_demo.py --status         # 看当前用的是哪个库

常用参数
========
    --days 30            生成多少天的跨度
    --no-music           不给日记配乐（默认会给每篇挂上曲库片段，卡片可立即播放）
    --reset              重新生成（先删掉旧的演示库）

配乐为什么是"秒出"
==================
演示数据不走合成（30 首合成要好几分钟），而是直接把曲库里对应情绪的
**素材片段**（35 秒 mp3）挂上去 —— 写一条数据库记录即可，
所以点开任意一张卡片都能马上听到音乐，演示节奏不拖沓。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MAIN_DB = PROJECT_ROOT / "storage" / "diary.db"
DEMO_DB = PROJECT_ROOT / "storage" / "diary_demo.db"
ENV_FILE = PROJECT_ROOT / ".env"

# ---------------------------------------------------------------------------
# 仿真日记内容：(几天前, 正文)
# ---------------------------------------------------------------------------
# 编排成一条有起伏的情绪弧线（压力 → 崩溃 → 缓过来 → 好转 → 开心），
# 这样折线图有真实的高低起伏，而不是一条平线 —— 演示效果差很多。
DEMO_ENTRIES: list[tuple[int, str]] = [
    (29, "新一周光是需求评审就排了四场，排期全挤在一起，压力从第一天就开始了。"),
    (28, "连着开了四个会，脑子嗡嗡的，晚上还要改方案，压力有点大。"),
    (27, "连续熬了三个通宵改 bug，眼睛酸得睁不开，整个人累到不想说话。"),
    (26, "leader 说进度落后了，我心里一紧，手心都在出汗。"),
    (24, "需求又双叒改了，我真的有点生气，凭什么每次都是临上线才改。"),
    (23, "跟产品经理吵了一架，气得我把键盘都摔了，冷静下来又觉得没必要。"),
    (21, "上线延期了，大家都不好受，我有点难过，觉得对不起团队。"),
    (20, "一个人加班到十点，办公室只剩我工位那盏灯，安静得有点可怕。"),
    (19, "下班回到空荡荡的出租屋，没人说话，孤独就像窗外的雨。"),
    (17, "连续加班第五天，身体被掏空了，什么都不想干。"),
    (16, "周末睡到中午，什么都没做，也不觉得愧疚，就这样吧。"),
    (15, "今天天气很好，午休去楼下走了走，心情平静了不少。"),
    (14, "翻到大学时的照片，那时候真好啊，大家都还在。"),
    (13, "项目终于交付了，压了一个月的事总算放下，整个人都释然了。"),
    (12, "leader 在群里点名表扬了我，有点小开心，也有点不好意思。"),
    (11, "开始规划下个版本了，这次想做得更好一点，心里有期待。"),
    (10, "约了好朋友吃饭，聊天笑到肚子疼，很久没有这么开心了。"),
    (9,  "今天提案通过了！同事们一起吃了顿好的，特别开心！"),
    (8,  "收到她的消息，一整天嘴角都没放下来过，我好像喜欢上她了。"),
    (7,  "一起看了场电影，散场后沿着江边走，我好喜欢和她待在一起的感觉。"),
    (6,  "妈妈寄来了家乡的腊肉，心里特别感激她一直惦记着我。"),
    (5,  "看到山区孩子收到捐助书包的视频，鼻子一酸，被深深地感动了。"),
    (4,  "世界杯决赛看了全场，最后那个进球太燃了，我从沙发上直接蹦起来！"),
    (3,  "对未来有点迷茫，不知道这几年走的路对不对，先不想了。"),
    (2,  "傍晚在阳台吹风，看着天慢慢变暗，心里很平静，也很温柔。"),
    (1,  "陪她去看了医生，回来的路上她靠着我的肩膀睡着了。"),
    (0,  "今天把拖了很久的事全部做完了，还收到了一笔奖金，开心！"),
]


# ---------------------------------------------------------------------------
# .env 读写：只改 DATABASE_URL 一行，其他配置原样保留
# ---------------------------------------------------------------------------
def _read_env_lines() -> list[str]:
    return ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []


def _write_env_lines(lines: list[str]) -> None:
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_database_url(url: str | None) -> None:
    """把 DATABASE_URL 写成 url；传 None 表示删除这一行（回到默认主库）。"""
    lines = [l for l in _read_env_lines() if not l.strip().startswith("DATABASE_URL=")]
    if url is not None:
        lines.append(f"DATABASE_URL={url}")
    _write_env_lines(lines)


def _current_database_url() -> str:
    for line in _read_env_lines():
        if line.strip().startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return f"sqlite:///{MAIN_DB.as_posix()}（默认主库）"


def _demo_url() -> str:
    return f"sqlite:///{DEMO_DB.as_posix()}"


# ---------------------------------------------------------------------------
# 生成演示数据
# ---------------------------------------------------------------------------
def build(days: int, with_music: bool) -> int:
    # ⚠️ 必须在 import backend 之前设置好环境变量：
    #    settings 是单例，import 时就读走了环境变量，晚了设置无效。
    os.environ["DATABASE_URL"] = _demo_url()
    os.environ["AUDIO_DIR"] = str((PROJECT_ROOT / "storage" / "audio").as_posix())
    os.environ["LLM_API_KEY"] = ""          # 演示数据用本地规则分析，不依赖网络
    os.environ["MUSIC_LIBRARY_FIRST"] = "True"

    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)

    from backend import models  # noqa: F401 —— 注册表
    from backend.database import Base, SessionLocal, engine
    from backend.services.llm_emotion import analyze_emotion
    from backend.services.music_library import library_stats, pick_library_track
    from backend import crud

    if with_music:
        stats = library_stats()
        print(f"曲库状态：{stats.get('track_count', 0)} 首素材"
              f"（每种情绪命中后可立即播放）")

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    created = 0
    try:
        # 清空旧的演示数据（演示库是独立的，清掉的只有假数据）
        db.query(models.MusicTrack).delete()
        db.query(models.Diary).delete()
        db.commit()

        now_local = datetime.now(timezone.utc).astimezone()

        for days_ago, content in DEMO_ENTRIES:
            if days_ago >= days:
                continue

            diary = crud.create_diary(db, content=content)
            emotion = asyncio.run(analyze_emotion(content))
            crud.apply_emotion_result(db, diary, emotion)

            # 把创建时间改到"几天前的晚上"，模拟真实记录习惯
            target = (now_local - timedelta(days=days_ago)).replace(
                hour=21, minute=30, second=0, microsecond=0
            )
            diary.created_at = target.astimezone(timezone.utc).replace(tzinfo=None)
            diary.updated_at = diary.created_at
            db.add(diary)

            if with_music:
                picked = pick_library_track(emotion.primary)
                if picked:
                    track = models.MusicTrack(
                        diary_id=diary.id,
                        status="ready",
                        provider="library",
                        audio_url=str(picked["audio_url"]),
                        file_path=str(picked["file_path"]),
                        duration_seconds=35,
                        prompt=f"[{emotion.primary}] 演示素材",
                        meta_json=__import__("json").dumps(
                            picked.get("meta", {}), ensure_ascii=False
                        ),
                        created_at=diary.created_at,
                        updated_at=diary.created_at,
                        finished_at=diary.created_at,
                    )
                    db.add(track)

            db.commit()
            created += 1
            print(f"  [{created:>2}] {days_ago:>2} 天前  "
                  f"{emotion.primary:<11} {content[:26]}…")

        print(f"\n演示库已生成：{created} 篇日记 → {DEMO_DB.relative_to(PROJECT_ROOT)}")
    finally:
        db.close()
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/装载/卸载演示种子数据")
    parser.add_argument("--build", action="store_true", help="生成演示数据（默认动作）")
    parser.add_argument("--load", action="store_true", help="切换到演示库")
    parser.add_argument("--unload", action="store_true", help="切回你的真实数据")
    parser.add_argument("--status", action="store_true", help="查看当前使用的数据库")
    parser.add_argument("--days", type=int, default=30, help="演示跨度（天，默认 30）")
    parser.add_argument("--no-music", action="store_true", help="不挂配乐")
    args = parser.parse_args()

    if args.status:
        current = _current_database_url()
        which = "演示库" if "diary_demo.db" in current else "主库（真实数据）"
        print(f"当前生效数据库：{current}")
        print(f"判定：{which}")
        return 0

    if args.load:
        if not DEMO_DB.exists():
            print("演示库还不存在，先生成：")
            build(args.days, not args.no_music)
        _set_database_url(_demo_url())
        print(f"\n✔ 已装载演示库（你的真实日记未受影响）")
        print("  重启服务后生效：python run.py")
        return 0

    if args.unload:
        _set_database_url(None)
        print("✔ 已切回主库（真实数据）")
        print("  重启服务后生效：python run.py")
        return 0

    # 默认：生成
    build(args.days, not args.no_music)
    print("\n下一步：python scripts/seed_demo.py --load   然后重启服务")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
