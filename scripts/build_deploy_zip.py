"""
============================================================================
scripts/build_deploy_zip.py —— 生成云平台部署包 cloudbase_deploy.zip
============================================================================
做什么
======
把部署到云托管（腾讯云 CloudBase / Render / Koyeb 等）需要的文件打进
一个 zip，排除开发产物（.git、docs、tests、数据库、缓存等）。

为什么需要脚本
==============
每次改完代码都要重新打包上传，手工挑文件容易漏（漏了 frontend/ 云端
就是旧版页面）。脚本保证每次产物一致。

用法
====
    python scripts/build_deploy_zip.py
产物
====
    项目根目录/cloudbase_deploy.zip
"""

from __future__ import annotations

import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT = PROJECT_ROOT / "cloudbase_deploy.zip"

# 进包的目录 / 文件（与 Dockerfile 的 COPY 清单保持一致）
DIRS = ["backend", "frontend", "scripts", "storage/library"]
FILES = ["Dockerfile", ".dockerignore", "requirements.txt", "run.py", "pytest.ini"]
SKIP_SUFFIX = (".pyc", ".pyo", ".db", ".db-shm", ".db-wal")
SKIP_DIR = ("__pycache__", ".pytest_cache")


def main() -> None:
    if OUT.exists():
        OUT.unlink()

    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in DIRS:
            base = PROJECT_ROOT / d
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if f.is_dir():
                    continue
                if any(part in SKIP_DIR for part in f.parts):
                    continue
                if f.suffix in SKIP_SUFFIX:
                    continue
                zf.write(f, f.relative_to(PROJECT_ROOT).as_posix())
                count += 1
        for name in FILES:
            f = PROJECT_ROOT / name
            if f.exists():
                zf.write(f, name)
                count += 1

    size_mb = OUT.stat().st_size / 1024 / 1024
    print(f"✔ {OUT.name} 已生成：{count} 个文件，{size_mb:.1f} MB")


if __name__ == "__main__":
    main()
