"""
============================================================================
tests/test_pin_library.py —— 置顶功能 & 官方素材曲库 测试
============================================================================
覆盖：
    置顶 / 取消置顶接口、置顶排序、删除清理音频文件、
    曲库扫描索引、按情绪选歌、曲库优先的生成链路、曲库静态托管。

运行：pytest tests/test_pin_library.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """
    本文件专用的测试客户端。

    与 conftest 的全局配置不同，这里把 MUSIC_LIBRARY_FIRST 动态开关交给
    每个用例自己控制（monkeypatch 会在用例结束时自动还原）。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_FIRST", False)

    with TestClient(get_app()) as c:
        yield c


def get_app():
    from backend.main import app
    return app


def _create_diary(client: TestClient, content: str) -> dict:
    resp = client.post("/api/diary", json={"content": content, "auto_generate_music": False})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# 一、置顶功能
# ===========================================================================
def test_pin_then_list_orders_first(client: TestClient):
    """置顶的日记必须排在列表最前面，取消置顶后恢复按时间倒序。"""
    old = _create_diary(client, "这是较早的一篇日记，内容平淡无奇。")
    new = _create_diary(client, "这是较新的一篇日记，时间上应该排在前面。")

    # 默认：新的在前
    items = client.get("/api/diary?page=1&page_size=10").json()["items"]
    ids = [i["id"] for i in items]
    assert ids.index(new["id"]) < ids.index(old["id"])

    # 置顶旧的 → 它必须跳到第一位，且 pinned 字段为 True
    resp = client.patch(f"/api/diary/{old['id']}/pin", json={"pinned": True})
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True

    items = client.get("/api/diary?page=1&page_size=10").json()["items"]
    assert items[0]["id"] == old["id"]
    assert items[0]["pinned"] is True

    # 取消置顶 → 恢复时间倒序
    client.patch(f"/api/diary/{old['id']}/pin", json={"pinned": False})
    items = client.get("/api/diary?page=1&page_size=10").json()["items"]
    assert ids.index(new["id"]) < ids.index(old["id"])

    client.delete(f"/api/diary/{old['id']}")
    client.delete(f"/api/diary/{new['id']}")


def test_pin_missing_diary_returns_404(client: TestClient):
    resp = client.patch("/api/diary/99999999/pin", json={"pinned": True})
    assert resp.status_code == 404


def test_pin_requires_bool(client: TestClient):
    """pinned 传字符串应当被 pydantic 校验拦截（422），而不是静默存进去。"""
    resp = client.patch("/api/diary/1/pin", json={"pinned": "yes-please"})
    assert resp.status_code == 422


# ===========================================================================
# 二、删除清理音频文件
# ===========================================================================
def test_delete_diary_cleans_audio_files(client: TestClient):
    """
    删除带配乐的日记后，它在 storage/audio 里生成的 wav 文件必须被清掉，
    不能留下"没有主人"的孤儿音频。
    """
    settings = get_settings()

    diary = client.post(
        "/api/diary",
        json={"content": "深夜一个人听雨，安静又有点孤独。", "auto_generate_music": True},
    ).json()
    diary_id = diary["id"]

    # 生成完成后音频目录里应该有文件
    files_before = list(Path(settings.audio_dir).glob("*.wav"))
    assert files_before, "音乐生成后音频目录不应为空"

    assert client.delete(f"/api/diary/{diary_id}").status_code == 200

    files_after = list(Path(settings.audio_dir).glob("*.wav"))
    assert len(files_after) < len(files_before), "删除日记后音频文件应被清理"


# ===========================================================================
# 三、官方素材曲库
# ===========================================================================
def _make_library(tmp_path: Path) -> Path:
    """造一个迷你曲库：joy 两首、unknown 一首（空文件即可，扫描不读音频内容）。"""
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "joy_01_晴天.mp3").write_bytes(b"fake-mp3")
    (lib / "joy_02_小幸运.mp3").write_bytes(b"fake-mp3")
    (lib / "mystery_白噪音.mp3").write_bytes(b"fake-mp3")     # 前缀不是情绪 → unknown 桶
    (lib / "顺手放个文本.txt").write_text("不是音频，应被忽略")
    return lib


def test_library_scan_and_pick(tmp_path: Path):
    """曲库索引：按情绪分桶、unknown 兜底、非音频文件被忽略。"""
    from backend.services.music_library import library_stats, pick_library_track

    lib = _make_library(tmp_path)

    stats = library_stats(lib)
    assert stats["track_count"] == 3
    assert stats["by_emotion"]["joy"] == 2
    assert stats["by_emotion"]["unknown"] == 1

    # joy 日记必须精确命中 joy 桶
    pick = pick_library_track("joy", lib)
    assert pick is not None
    assert pick["emotion"] == "joy"
    assert pick["audio_url"].startswith("/media/library/joy_")
    assert pick["title"] in {"晴天", "小幸运"}

    # sadness 没有专属曲目 → 兜底到 unknown 桶
    fallback = pick_library_track("sadness", lib)
    assert fallback is not None
    assert fallback["audio_url"].startswith("/media/library/mystery_")

    # 空曲库 → 返回 None，上层自动降级为本地合成
    empty = tmp_path / "empty_lib"
    empty.mkdir()
    assert pick_library_track("joy", empty) is None


def test_library_first_generation_flow(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """
    开启曲库优先后：写日记 → 配乐直接命中曲库文件（不合成），
    provider=library，且**删除日记不会误删曲库文件**（曲库是共享资产）。

    注意：静态挂载 /media/library 在 app 导入时就绑定了 conftest 配置的
    临时曲库目录（settings.library_dir），所以测试文件必须写进同一个目录，
    否则 HTTP 下载会 404。
    """
    lib = Path(get_settings().library_dir)
    target = lib / "joy_主题曲.mp3"
    target.write_bytes(b"fake-mp3-data")

    settings = get_settings()
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_FIRST", True)

    diary = client.post(
        "/api/diary",
        json={"content": "今天升职加薪，请全组喝了奶茶，非常开心！", "auto_generate_music": True},
    ).json()
    track_id = diary["music"]["id"]

    # 响应体里的 music 是后台任务启动前的快照（pending），
    # TestClient 会在请求返回后同步跑完后台任务，所以要重新查一次状态
    track = client.get(f"/api/music/track/{track_id}").json()
    assert track["status"] == "ready", track.get("error_message")
    assert track["provider"] == "library"
    assert track["audio_url"] == "/media/library/joy_主题曲.mp3"
    assert track["meta"]["source"] == "官方素材曲库"

    # 曲库静态托管必须真的能下载到这个文件
    audio = client.get(track["audio_url"])
    assert audio.status_code == 200
    assert audio.content == b"fake-mp3-data"

    # 关键回归：删除日记后曲库文件必须还在
    assert client.delete(f"/api/diary/{diary['id']}").status_code == 200
    assert target.exists()

    # 测试自产自销的素材文件清掉，不影响其它用例的曲库统计
    target.unlink(missing_ok=True)


def test_health_reports_library_count(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "music_library_count" in data
    assert "music_library_first" in data
