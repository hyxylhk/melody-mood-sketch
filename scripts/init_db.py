"""
============================================================================
scripts/init_db.py —— 数据库初始化脚本（手动版）
============================================================================
正常情况下你**不需要**执行它：
    后端启动时（lifespan）会自动调用 init_db() 建表。

什么时候会用到这个脚本？
    1. 想在不启动服务的前提下先确认表能不能建出来
    2. 数据库文件损坏，想删掉重建
    3. 想把当前库里的表结构打印出来看看

用法（项目根目录执行）：
    python scripts/init_db.py              # 检查并补建缺失的表
    python scripts/init_db.py --reset      # ⚠️ 删库重建（会清空所有数据）
    python scripts/init_db.py --info       # 只看信息，不动数据
"""

from __future__ import annotations

import sys
from pathlib import Path

# 把项目根目录加进模块搜索路径，这样 "from backend import xxx" 才找得到
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.database import SessionLocal, engine, init_db  # noqa: E402
from backend.models import Diary, MusicTrack  # noqa: E402


def print_info() -> None:
    """打印数据库当前状态：有哪些表、各有多少行。"""
    settings = get_settings()
    db_file = settings.database_path

    print("=" * 60)
    print("  数据库信息")
    print("=" * 60)
    print(f"  文件位置 : {db_file}")
    print(f"  是否存在 : {'是' if db_file.exists() else '否'}")
    if db_file.exists():
        print(f"  文件大小 : {db_file.stat().st_size / 1024:.1f} KB")

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"  表       : {tables if tables else '（还没有任何表）'}")

    if "diaries" in tables:
        db = SessionLocal()
        try:
            diary_count = db.execute(text("SELECT COUNT(*) FROM diaries")).scalar()
            track_count = (
                db.execute(text("SELECT COUNT(*) FROM music_tracks")).scalar()
                if "music_tracks" in tables else 0
            )
            print(f"  日记数   : {diary_count}")
            print(f"  音乐数   : {track_count}")
        finally:
            db.close()
    print("=" * 60)


def reset_database() -> None:
    """⚠️ 危险操作：删除所有数据后重建空表。"""
    settings = get_settings()
    db_file = settings.database_path

    if db_file.exists():
        # 用 SQLite 的 VACUUM 之外，最干净的做法是直接删文件重建
        db_file.unlink()
        print(f"[OK] 已删除旧数据库文件：{db_file}")

    init_db()
    print("[OK] 已重建空表")


def main() -> None:
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        return

    if "--reset" in args:
        confirm = input("确定要清空所有数据吗？输入 yes 继续：").strip().lower()
        if confirm != "yes":
            print("[取消] 什么都没改。")
            return
        reset_database()
        print_info()
        return

    if "--info" in args:
        print_info()
        return

    # 默认行为：补建缺失的表
    init_db()
    print("[OK] 表结构已是最新（已存在的表不会被改动，数据不会丢）\n")
    print_info()


if __name__ == "__main__":
    main()
