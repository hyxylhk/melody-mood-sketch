"""
============================================================================
run.py —— 一键启动脚本
============================================================================
用法（必须在项目根目录执行）：

    python run.py

它会依次做这些事：
    1. 检查依赖装没装（缺了会提示 pip install -r requirements.txt）
    2. 检查有没有 .env（没有就自动从 .env.example 复制一份）
    3. 检查 SQLite 数据库与音频目录是否就绪（缺了自动创建，自动建表）
    4. 启动 uvicorn Web 服务器
    5. （可选）自动打开浏览器

相比直接输 uvicorn 命令的好处：把零散的环境准备工作集中到这里，
现场答辩不会因为"忘了建 .env"而卡住。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# 统一把工作目录切换到项目根目录。
# 这样即使你在别的位置执行 python /path/to/run.py，相对路径也不会错。
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"


def check_dependencies() -> bool:
    """检查必需的第三方包是否都已安装。"""
    required = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "sqlalchemy": "sqlalchemy",
        "pydantic": "pydantic",
        "pydantic_settings": "pydantic-settings",
        "httpx": "httpx",
    }
    missing = []
    for import_name, package_name in required.items():
        if importlib.util.find_spec(import_name) is None:
            missing.append(package_name)

    if missing:
        print("=" * 62)
        print("  缺少依赖：" + ", ".join(missing))
        print("  请先执行：  pip install -r requirements.txt")
        print("=" * 62)
        return False
    return True


def ensure_env_file() -> None:
    """
    没有 .env 就从模板复制一份。

    这样新手 clone 下来直接 python run.py 就能跑，
    不会莫名其妙报"配置不存在"。
    """
    if ENV_FILE.exists():
        return

    if ENV_EXAMPLE.exists():
        shutil.copy(ENV_EXAMPLE, ENV_FILE)
        print("[INFO] 未发现 .env，已从 .env.example 自动复制一份。")
        print("[INFO] 若要接大模型，请打开 .env 填入你的 API Key。")
    else:
        print("[WARN] 既没有 .env 也没有 .env.example，将全部使用默认配置（本地分析 + 本地合成）。")


def ensure_directories() -> None:
    """确保 storage/ 和 storage/audio/ 存在（SQLite 和生成的音乐要往这里写）。"""
    (PROJECT_ROOT / "storage").mkdir(exist_ok=True)
    (PROJECT_ROOT / "storage" / "audio").mkdir(exist_ok=True)


def main() -> None:
    print()
    print("🚀 旋律情绪速写 —— 启动中")
    print("-" * 62)

    if not check_dependencies():
        sys.exit(1)

    ensure_env_file()
    ensure_directories()

    # 到这里才 import 项目内的模块：
    # 因为 settings 在 import 时就会读取 .env，必须在 ensure_env_file() 之后。
    from backend.config import get_settings
    from backend.database import init_db

    settings = get_settings()

    # 自动建表（幂等：表已存在则什么都不做）
    init_db()
    print(f"[OK] 数据库就绪：{settings.database_path}")

    # 是否需要自动打开浏览器
    auto_open = "--no-browser" not in sys.argv

    print("-" * 62)
    print(f"  服务地址   : http://127.0.0.1:{settings.APP_PORT}")
    print(f"  接口文档   : http://127.0.0.1:{settings.APP_PORT}/docs")
    print(f"  调试模式   : {settings.APP_DEBUG}（改代码会自动重启）")
    print(f"  大模型     : {'已配置 → ' + settings.LLM_MODEL if settings.llm_enabled else '未配置 → 情绪分析走本地规则'}")
    print("-" * 62)
    print("  按 Ctrl + C 停止服务")
    print()

    if auto_open:
        # 稍微等一下确保服务起来了，再打开浏览器（用非阻塞方式）
        try:
            webbrowser.open_new_tab(f"http://127.0.0.1:{settings.APP_PORT}")
        except Exception:  # noqa: BLE001 —— 无图形界面的服务器上会失败，忽略即可
            pass

    import uvicorn

    uvicorn.run(
        "backend.main:app",        # 用字符串形式，这样 --reload 才能正确重启子进程
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.APP_DEBUG,
        log_level="debug" if settings.APP_DEBUG else "info",
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 已手动停止，再见 👋")
