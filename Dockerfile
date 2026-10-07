# =============================================================================
# Dockerfile —— 备用部署方案（Render Blueprint 之外的平台都能用）
# =============================================================================
# 什么时候用 Docker：
#   - 部署平台不认 render.yaml（Koyeb / Railway / 腾讯云容器服务 / 阿里云）
#   - 想在国内云服务器上自己跑（配合 -p 80:8000）
#
# 构建与运行：
#   docker build -t melody-mood-sketch .
#   docker run -p 8000:8000 melody-mood-sketch
#
# 带大模型 Key 运行：
#   docker run -p 8000:8000 --env-file .env melody-mood-sketch
#
# 已经用它部署到了：Koyeb（免费 Web Service，不休眠，评委点开即开）
# =============================================================================

FROM python:3.13-slim

WORKDIR /app

# 先只复制依赖清单再安装：利用 Docker 层缓存，
# 代码改动不会触发重新下载依赖（构建从几分钟降到几秒）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制运行期必需的代码与资源
COPY backend ./backend
COPY frontend ./frontend
COPY run.py pytest.ini ./
# scripts/ 里有 seed_demo.py —— 云端冷启动时靠它灌入 27 篇演示数据，
# 否则评委点开看到的是空页面 + 空轨迹图。少了这一行会报找不到脚本。
COPY scripts ./scripts
# 曲库片段（17 首 × 35 秒 ≈ 9MB）随镜像带上，云端才有真歌可放
COPY storage/library ./storage/library

# 运行期不需要 ffmpeg（只有裁剪曲库的离线脚本用）——镜像保持精简

ENV PYTHONUNBUFFERED=1
# 云端一律关调试模式：reload 会让服务在项目文件变动时整体重启
ENV APP_DEBUG=False
ENV MUSIC_LIBRARY_FIRST=True
# 和 seed_demo.py 写的是同一个库，改一个必须改另一个
ENV DATABASE_URL=sqlite:///./storage/diary_demo.db
EXPOSE 8000

# ⚠️ 端口写死 8000（不用 ${PORT}）的原因：
#   腾讯云云托管会在容器里注入 PORT 环境变量（常为 80），但控制台
#   「监听端口」又是单独填的一项 —— 两边一旦不一致，健康检查就失败、
#   访问直接 502。写死 8000 + 控制台监听端口填 8000，零歧义。
#   换到 Render 这类强制用注入端口的平台时，再改回 --port ${PORT:-8000}。
#
# 先 seed 再启动：云托管容器文件系统是易失的，冷启动后库是空的，
# 不灌数据评委第一眼只看到"暂无日记"。seed 是纯本地脚本，约 1 秒。
CMD ["sh", "-c", "python scripts/seed_demo.py --build && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
