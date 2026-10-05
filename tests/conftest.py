"""
============================================================================
tests/conftest.py —— 测试夹具（fixture）配置
============================================================================
fixture 是什么？
    可以理解成"测试用的共享资源"。函数参数里写了某个 fixture 的名字，
    pytest 会在调用前自动帮你准备好这个对象。

本文件的核心目的：**让测试完全隔离**
    - 数据库指向临时目录（跑完自动删），绝不污染你真实的 storage/diary.db
    - 强制 LLM_KEY 为空 → 情绪分析走本地规则，测试不依赖网络、不烧钱
    - 音乐时长压到 2 秒 → 单测跑得快

⚠️ 关键细节：
    环境变量必须在**第一次 import backend 之前**设置好。
    因为 settings 是 lru_cache 单例，晚一步设置就晚了 —— 所以这里用
    模块顶部的语句，pytest 会最先加载 conftest.py。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1. 准备一个临时目录，作为本次测试的数据库 / 音频输出位置
# ---------------------------------------------------------------------------
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="emodiary_test_"))

# ---------------------------------------------------------------------------
# 2. 设置环境变量（必须早于任何 backend 模块的 import）
# ---------------------------------------------------------------------------
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_ROOT / 'test.db').as_posix()}"
os.environ["AUDIO_DIR"] = str((_TMP_ROOT / "audio").as_posix())
os.environ["MUSIC_LIBRARY_DIR"] = str((_TMP_ROOT / "library").as_posix())
os.environ["MUSIC_LIBRARY_FIRST"] = "False"    # 默认测试不走素材曲库，保证断言 provider==local 稳定
os.environ["LLM_API_KEY"] = ""                 # 留空 → 情绪分析降级为本地规则
os.environ["MUSIC_PROVIDER"] = "local"         # 只用本地合成，测试不依赖外网
os.environ["MUSIC_DURATION_SECONDS"] = "2"     # 2 秒音频，合成飞快
os.environ["MUSIC_SAMPLE_RATE"] = "22050"      # 降采样率，进一步提速
os.environ["APP_DEBUG"] = "False"

# ---------------------------------------------------------------------------
# 3. 把项目根目录加入 sys.path
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 4. 常用 fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def project_root() -> Path:
    """项目根目录路径。"""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def test_client():
    """
    FastAPI 官方测试客户端。

    TestClient 会"假装"自己是浏览器给 app 发请求，
    但不需要真的启动 uvicorn 监听端口。

    ⚠️ 注意：TestClient 会在返回响应前**同步执行完 BackgroundTasks**，
    所以音乐生成的后台任务在这里是阻塞完成的 —— 这也是为什么 conftest
    要把音乐时长压到 2 秒，否则每个用例都要等好几秒。
    """
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def db_session():
    """
    一个独立的数据库会话，用完自动关闭。
    用于直接测试 crud 层，不经过 HTTP。
    """
    from backend.database import Base, SessionLocal, engine
    from backend import models  # noqa: F401 —— 确保表已注册

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
