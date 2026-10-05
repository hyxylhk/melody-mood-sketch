"""
============================================================================
tests/test_trajectory.py —— 情绪轨迹接口测试
============================================================================
对应作品介绍里"留存自己的情绪变化轨迹"这句承诺。
没有轨迹数据，时间线就只是"一条列表"，称不上"轨迹"。

重点验证三件事：
    1. 空白天必须占位（calendar 长度恒等于 days），否则前端日历格子会错位
    2. 时区不能错（UTC 存储 → 本地日期归并），晚上写的日记不能被算成第二天
    3. 连续记录天数 streak 的断更逻辑正确
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.main import app
from backend.models import Diary


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db() -> Session:
    """
    独立数据库会话。

    这里显式 create_all 一次：本文件的 autouse 清理夹具会**早于** TestClient 的
    lifespan 建表执行，单独跑这个文件（pytest tests/test_trajectory.py）时
    表还不存在，DELETE 会直接报 "no such table"。
    """
    from backend import models  # noqa: F401 —— 注册表定义
    from backend.database import Base, engine

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clean_db(db: Session):
    """
    每个用例前清空日记表。

    ⚠️ 为什么必须清：本文件多个用例都在造"今天/昨天/前天"的日记，
    如果前一个用例的数据留着，后一个用例断言 count / trend 长度就会莫名其妙多出来，
    而且这种失败只在**按文件整体跑**时才出现、单跑却能通过 —— 非常难查。
    轨迹类测试对"数据干净"依赖很强，所以这里强制隔离。
    """
    db.query(Diary).delete()
    db.commit()
    yield


def _write(client: TestClient, content: str) -> int:
    """通过接口写一篇日记（不自动生成音乐，测试更快），返回 diary_id。"""
    resp = client.post("/api/diary", json={"content": content, "auto_generate_music": False})
    # 新建资源按 REST 规范返回 201 Created（不是 200）
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def _move_to(db: Session, diary_id: int, days_ago: int, hour: int = 12) -> None:
    """
    把某篇日记的创建时间改到"几天前的本地时间"。

    数据库存的是 UTC，所以这里要：本地时间 → 转 UTC → 去掉时区信息再写入，
    模拟真实写入路径（models.utc_now() 存的就是 naive UTC）。
    """
    diary = db.get(Diary, diary_id)
    assert diary is not None
    local_target = (
        datetime.now(timezone.utc).astimezone() - timedelta(days=days_ago)
    ).replace(hour=hour, minute=0, second=0, microsecond=0)
    diary.created_at = local_target.astimezone(timezone.utc).replace(tzinfo=None)
    db.commit()


# ===========================================================================
# 一、空库：不能报错，且日历必须给满占位
# ===========================================================================
def test_trajectory_empty_library(client: TestClient):
    """一篇日记都没有时，接口仍要返回完整长度的日历（前端靠它占格子）。"""
    data = client.get("/api/diary/trajectory", params={"days": 7}).json()

    assert data["days"] == 7
    assert len(data["calendar"]) == 7, "空白天必须占位，否则日历会错位"
    assert data["trend"] == [], "没有日记时折线不应有数据点"
    assert data["streak"] == 0
    assert data["active_days"] == 0
    assert data["total_entries"] == 0
    assert all(day["count"] == 0 for day in data["calendar"])


# ===========================================================================
# 二、聚合正确性：按天归并、主导情绪、效价、连续天数
# ===========================================================================
def test_trajectory_aggregates_by_local_day(client: TestClient, db: Session):
    """
    今天写 2 篇开心的、昨天写 1 篇难过的：
    日历最后两格应分别是 joy(2 篇) / sadness(1 篇)，streak = 2。
    """
    a = _write(client, "今天升职加薪了，太开心了！")
    b = _write(client, "今天提案顺利通过，特别兴奋！")
    c = _write(client, "今天分手了，心里特别难过。")
    _move_to(db, a, days_ago=0)
    _move_to(db, b, days_ago=0)
    _move_to(db, c, days_ago=1)

    data = client.get("/api/diary/trajectory", params={"days": 30}).json()

    assert len(data["calendar"]) == 30
    today = data["calendar"][-1]
    yesterday = data["calendar"][-2]

    assert today["count"] == 2
    assert today["emotion"] == "joy", today
    assert today["valence"] > 0, "开心的一天效价应为正"

    assert yesterday["count"] == 1
    assert yesterday["emotion"] == "sadness", yesterday
    assert yesterday["valence"] < 0, "难过的一天效价应为负"

    assert data["active_days"] == 2
    assert data["total_entries"] == 3
    assert data["streak"] == 2, "今天和昨天连续有记录"


def test_trajectory_streak_breaks_on_gap(client: TestClient, db: Session):
    """中间空一天就断更：streak 只数最近连续的天数（这里是 1）。"""
    a = _write(client, "今天很平静，没什么特别的事。")
    b = _write(client, "前天跟朋友吵架了，气死我了。")
    _move_to(db, a, days_ago=0)
    _move_to(db, b, days_ago=2)      # 故意跳过"昨天"

    data = client.get("/api/diary/trajectory", params={"days": 10}).json()
    assert data["streak"] == 1, "昨天没写 → 连续天数只算今天"
    assert data["active_days"] == 2


def test_trajectory_trend_skips_empty_days(client: TestClient, db: Session):
    """折线图只要"有记录的天"，空白天不能参与，否则折线会被拉平失真。"""
    # 用"焦虑"特征明确的文本（带 deadline 那句会被规则引擎判成 stress 压力，别踩这个坑）
    a = _write(client, "今天很焦虑，心慌得厉害，一直担心会出问题。")
    _move_to(db, a, days_ago=4)

    data = client.get("/api/diary/trajectory", params={"days": 10}).json()
    assert len(data["calendar"]) == 10
    assert len(data["trend"]) == 1, "trend 只包含有记录的天"
    assert data["trend"][0]["emotion"] == "anxiety"


# ===========================================================================
# 三、参数边界
# ===========================================================================
@pytest.mark.parametrize("bad", [0, -1, 121, 9999])
def test_trajectory_days_bounds(client: TestClient, bad: int):
    """days 超出 1~120 应被 FastAPI 的参数校验挡下（422），不能偷偷跑全表。"""
    resp = client.get("/api/diary/trajectory", params={"days": bad})
    assert resp.status_code == 422


def test_trajectory_default_days(client: TestClient):
    """不带 days 参数时默认 30 天。"""
    data = client.get("/api/diary/trajectory").json()
    assert data["days"] == 30
    assert len(data["calendar"]) == 30


def test_trajectory_date_range_is_continuous(client: TestClient):
    """日历日期必须严格连续且升序，前端画格子时依赖这个顺序。"""
    data = client.get("/api/diary/trajectory", params={"days": 14}).json()
    dates = [day["date"] for day in data["calendar"]]
    assert len(set(dates)) == 14, "日期不能重复"
    assert dates == sorted(dates)
    assert data["start_date"] == dates[0]
    assert data["end_date"] == dates[-1]
