"""
============================================================================
tests/test_api.py —— HTTP 接口端到端测试
============================================================================
用 FastAPI 官方的 TestClient，不真正开端口，速度快也不需要额外服务。

覆盖：
    健康检查 / 情绪字典 / 写日记 / 列表 / 详情 / 修改 / 删除
    音乐生成全流程（含后台任务）/ 轮询 / 404 / 参数校验 / CORS

运行：pytest tests/test_api.py -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture()
def client() -> TestClient:
    """每个用例一个独立客户端（lifespan 会自动完成建表）。"""
    with TestClient(app) as c:
        yield c


# ===========================================================================
# 一、系统接口
# ===========================================================================
def test_health(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # 测试环境下未配 LLM，因此这里应当走本地通道
    assert data["llm_enabled"] is False
    assert data["music_provider_effective"] == "local"


def test_emotion_options(client: TestClient):
    resp = client.get("/api/meta/emotions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    codes = {item["code"] for item in data}
    assert {"joy", "sadness"}.issubset(codes)


def test_index_page_served(client: TestClient):
    """根路径应该返回前端 HTML 页面（同源部署，避免跨域）。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "旋律情绪速写" in resp.text


# ===========================================================================
# 二、写日记主流程
# ===========================================================================
def test_create_diary_and_get_emotion(client: TestClient):
    """主链路：POST /api/diary 必须返回结构完整的情绪分析结果。"""
    payload = {
        "title": "单元测试",
        "content": "今天项目终于上线了！熬了三个通宵，看到用户夸奖的时候超级开心，一切都值了。",
        "auto_generate_music": False,      # 这个用例只验证情绪部分
    }
    resp = client.post("/api/diary", json=payload)
    assert resp.status_code == 201, resp.text

    diary = resp.json()
    assert diary["id"] > 0
    assert diary["content_length"] == len(payload["content"])

    emotion = diary["emotion"]
    assert emotion is not None
    assert emotion["primary"] == "joy"
    assert emotion["primary_label"] == "喜悦"
    assert -1.0 <= emotion["valence"] <= 1.0
    assert 0.0 <= emotion["arousal"] <= 1.0
    assert emotion["summary"], "必须有一句话解读"

    # 顺手清理
    assert client.delete(f"/api/diary/{diary['id']}").status_code == 200


def test_create_diary_rejects_too_short(client: TestClient):
    """少于 2 个字的内容应被 422 拦下（pydantic 自动校验）。"""
    resp = client.post("/api/diary", json={"content": "好"})
    assert resp.status_code == 422


def test_create_diary_strips_whitespace(client: TestClient):
    """首尾空白要被自动清理，禁止只写空格的日记。"""
    resp = client.post("/api/diary", json={"content": "   \n\t  "})
    assert resp.status_code == 422


# ===========================================================================
# 三、查询 / 修改 / 删除
# ===========================================================================
def test_diary_crud_roundtrip(client: TestClient):
    created = client.post(
        "/api/diary",
        json={"content": "随便写点什么，今天平平淡淡。", "auto_generate_music": False},
    ).json()
    diary_id = created["id"]

    # 详情
    detail = client.get(f"/api/diary/{diary_id}")
    assert detail.status_code == 200
    assert detail.json()["content"] == "随便写点什么，今天平平淡淡。"

    # 列表里能查到
    listing = client.get("/api/diary?page=1&page_size=20").json()
    assert listing["total"] >= 1
    assert any(item["id"] == diary_id for item in listing["items"])

    # 修改标题
    patched = client.patch(f"/api/diary/{diary_id}", json={"title": "改过的标题"})
    assert patched.status_code == 200
    assert patched.json()["title"] == "改过的标题"

    # 删除后查不到
    assert client.delete(f"/api/diary/{diary_id}").status_code == 200
    assert client.get(f"/api/diary/{diary_id}").status_code == 404


def test_get_nonexistent_diary_returns_404(client: TestClient):
    assert client.get("/api/diary/99999999").status_code == 404
    assert client.delete("/api/diary/99999999").status_code == 404
    assert client.patch("/api/diary/99999999", json={"title": "x"}).status_code == 404


def test_list_filter_by_emotion(client: TestClient):
    """按情绪过滤要只返回该情绪的记录。"""
    resp = client.get("/api/diary?emotion=joy&page_size=50").json()
    for item in resp["items"]:
        assert item["emotion_primary"] == "joy"


def test_list_keyword_search(client: TestClient):
    resp = client.get("/api/diary?keyword=单元测试&page_size=50").json()
    assert resp["total"] >= 0      # 主要是验证不会因为搜索语法出错


# ===========================================================================
# 四、音乐生成全流程
# ===========================================================================
def test_music_generation_full_flow(client: TestClient):
    """
    完整走一遍：写日记 → 触发配乐 → 轮询到 ready → 音频文件可下载。

    注意 TestClient 会同步执行 BackgroundTasks，
    所以请求返回时音乐其实已经生成完了（这里 duration 被 conftest 压到 2 秒）。
    """
    diary = client.post(
        "/api/diary",
        json={
            "content": "今天一个人在家，深夜特别孤独，没人说话。",
            "auto_generate_music": True,
        },
    ).json()
    diary_id = diary["id"]

    # 1) 立即返回的任务状态必须是 pending 之外的终态（因为 TestClient 已跑完后台任务）
    #    这里检查字段结构完整即可
    assert diary["music"] is not None
    track_id = diary["music"]["id"]
    assert track_id > 0

    # 2) 直接查任务状态
    track = client.get(f"/api/music/track/{track_id}").json()
    assert track["status"] == "ready", track.get("error_message")
    assert track["provider"] == "local"
    assert track["audio_url"].endswith(".wav")

    # 3) 音频必须能通过静态文件服务下载下来
    audio_resp = client.get(track["audio_url"])
    assert audio_resp.status_code == 200
    assert len(audio_resp.content) > 10_000
    # WAV 文件头校验：前 4 字节 RIFF，8~12 字节 WAVE
    assert audio_resp.content[:4] == b"RIFF"
    assert audio_resp.content[8:12] == b"WAVE"

    # 4) latest 接口应返回同一条任务
    latest = client.get(f"/api/music/latest/{diary_id}").json()
    assert latest["id"] == track_id

    client.delete(f"/api/diary/{diary_id}")


def test_regenerate_music_creates_new_track(client: TestClient):
    """再次调用生成接口应创建新任务而不是复用旧的。"""
    diary = client.post(
        "/api/diary",
        json={"content": "很焦虑，deadline 要到了，做不完。", "auto_generate_music": True},
    ).json()
    diary_id = diary["id"]
    first_track_id = diary["music"]["id"]

    resp = client.post("/api/music/generate", json={"diary_id": diary_id, "force_local": True})
    assert resp.status_code == 202, resp.text
    second_track_id = resp.json()["id"]
    assert second_track_id != first_track_id

    latest = client.get(f"/api/music/latest/{diary_id}").json()
    assert latest["id"] == second_track_id

    client.delete(f"/api/diary/{diary_id}")


def test_generate_music_for_missing_diary(client: TestClient):
    assert client.post("/api/music/generate", json={"diary_id": 99999999}).status_code == 404


def test_get_missing_track_returns_404(client: TestClient):
    assert client.get("/api/music/track/99999999").status_code == 404


# ===========================================================================
# 五、CORS（需求明确要求）
# ===========================================================================
def test_cors_preflight_allowed(client: TestClient):
    """OPTIONS 预检必须带上允许跨域的头，否则浏览器会拦截。"""
    resp = client.options(
        "/api/diary",
        headers={
            "Origin": "http://localhost:5500",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert "access-control-allow-origin" in resp.headers
    assert "access-control-allow-methods" in resp.headers


def test_cors_header_on_normal_request(client: TestClient):
    resp = client.get("/api/health", headers={"Origin": "http://example.com"})
    assert "access-control-allow-origin" in resp.headers


# ===========================================================================
# 六、统计接口
# ===========================================================================
def test_stats_endpoint(client: TestClient):
    resp = client.get("/api/diary/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "diary_count" in data
    assert "emotion_distribution" in data
    assert isinstance(data["emotion_distribution"], dict)
