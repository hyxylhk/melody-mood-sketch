"""
==============================================================================
backend/database.py —— 数据库连接与会话管理
==============================================================================
给不懂后端的同学讲清楚三件事：

【1】为什么需要 ORM？
    原生 SQL 要手写 "INSERT INTO diary VALUES (...)" 这种字符串。
    ORM 让你直接操作 Python 对象：db.add(Diary(content=...))，
    它自动生成并执行 SQL。缺点是有性能损耗，优点是**写起来不容易出错、可读性高**。

【2】SQLite 是什么？
    一个把整个数据库存成单个文件的轻量数据库（本项目是 storage/diary.db）。
    零安装、零配置、零服务进程 —— 非常适合黑客松演示。
    生产环境换成 MySQL 只需要改一行 DATABASE_URL。

【3】Session 是什么？
    可以理解为"一次数据库会话 / 事务"。每个 HTTP 请求进来时开一个，
    请求结束时关掉。它由 FastAPI 的依赖注入机制自动管理（见 routers 里的 Depends(get_db)）。
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, Dict

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import get_settings


class Base(DeclarativeBase):
    """
    所有数据表模型的父类。

    SQLAlchemy 2.0 推荐这种写法（对比老版本 declarative_base()）。
    继承了 Base 的类，会被自动记录在 Base.metadata 里，
    后面一句 Base.metadata.create_all() 就能把所有表一次性建出来。
    """
    pass


def _create_engine() -> Engine:
    """
    创建数据库引擎。

    connect_args={"check_same_thread": False}
        是 SQLite 特有的开关。SQLite 默认只允许创建连接的那个线程使用它，
        但 FastAPI 是多线程/协程的，必须放开，否则报
        "SQLite objects created in a thread can only be used in that same thread"。
    """
    settings = get_settings()

    # 确保 storage 目录存在，否则 SQLite 会因为找不到目录而报错
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 直接用绝对路径拼 URL，避开相对路径带来的所有坑（Windows 上也稳定）
    url = f"sqlite:///{db_path.as_posix()}"

    connect_args: Dict[str, Any] = {
        "check_same_thread": False,
        # 关键：SQLite 在「后台音乐任务写库」和「HTTP 请求写库」撞车时会抛
        # "database is locked"。默认只等 5 秒就放弃，界面上表现为
        # 「偶尔保存失败 / 操作失败」这种玄学问题。这里放宽到 30 秒，
        # 撞锁时排队等而不是直接报错。
        "timeout": 30.0,
    }

    return create_engine(
        url,
        connect_args=connect_args,
        echo=False,        # echo=True 会把每条 SQL 打到终端，调试时可以开
        future=True,       # 使用 SQLAlchemy 2.0 的新行为
    )


# ---------------------------------------------------------------------------
# 全局唯一的引擎对象 + 会话工厂
# ---------------------------------------------------------------------------
engine: Engine = _create_engine()

# sessionmaker 是个"会话工厂"，每次调用 SessionLocal() 就得到一个新 Session。
#   autocommit=False → 需要显式 commit 才落库（事务安全）
#   autoflush=False  → 不自动把改动刷到数据库（避免意料之外的写盘）
#   expire_on_commit=False → commit 后对象仍然可用（不然取属性会解释器里再查一次库）
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


# 每次新建数据库连接时，先设置 SQLite 的三个关键参数
@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    """
    三个 PRAGMA 的作用：

    foreign_keys=ON
        SQLite 的外键约束默认是关闭的，打开后 DELETE 级联才生效。

    journal_mode=WAL
        默认的 rollback journal 模式下，「读」和「写」互斥 ——
        后台音乐任务在写库时，前端刷新列表的读请求会被挡住，
        撞得厉害就直接报 "database is locked"。
        WAL（Write-Ahead Logging）模式下读不阻塞写、写不阻塞读，
        是单文件数据库抗并发的标准解法。

    busy_timeout=30000
        真撞上写锁时，最多排队等 30 秒而不是立刻抛异常。
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
    except Exception:  # noqa: BLE001 —— 非 SQLite 数据库会走到这里，忽略即可
        pass


def get_db() -> Generator:
    """
    FastAPI 依赖注入用的生成器函数。

    典型用法（在接口函数参数里写 db: Session = Depends(get_db)）：
        yield 之前 = 请求开始时执行（开会话）
        yield 之后 = 响应返回后执行（关会话）
    这就是 Python 生成器实现的"RAII"，和 C++ 的构造/析构一个思路。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_columns() -> None:
    """
    轻量级表结构迁移：给"老库"补新列。

    为什么需要它？
        Base.metadata.create_all() 只会创建**不存在**的表；
        如果数据库文件是旧版本程序建的（比如没有 pinned 列），
        新代码一查询就会报 "no such column" 直接崩掉。
        正式的迁移要用 Alembic，但黑客松项目上那套太重了 ——
        这里用最朴素的方式：启动时检查每个表缺哪些列，缺就 ALTER TABLE 补上。

    ALTER TABLE ... ADD COLUMN 对 SQLite 是安全操作，不会动已有数据。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)

    # 表名 → 需要保证存在的列（列名 → 建列 SQL）
    required: Dict[str, Dict[str, str]] = {
        "diaries": {
            "pinned": "ALTER TABLE diaries ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0",
        },
    }

    for table, columns in required.items():
        if not inspector.has_table(table):
            continue                     # 表还没建，create_all 稍后会连列一起建
        existing = {col["name"] for col in inspector.get_columns(table)}
        with engine.begin() as conn:     # begin() 保证执行失败自动回滚
            for col_name, ddl in columns.items():
                if col_name not in existing:
                    conn.execute(text(ddl))
                    print(f"[MIGRATE] 已为表 {table} 补充新列：{col_name}")


def init_db() -> None:
    """
    自动建表。项目启动时会调用一次。

    注意：create_all 只会创建"还不存在"的表，不会修改已有表结构，也不会删数据。
    所以重复启动服务是安全的。

    ⚠️ 这里必须 import models，否则 SQLAlchemy 根本不知道有哪些类需要建表。
    """
    # 函数内部延迟 import，避免 database → models → database 的循环导入
    from backend import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def drop_all_tables() -> None:
    """
    危险操作：删除所有表（仅测试脚本用）。
    正常服务永远不要调它。
    """
    from backend import models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
