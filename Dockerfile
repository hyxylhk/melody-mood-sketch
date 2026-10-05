# =============================================================================
# Dockerfile —— 备用部署方案（Render Blueprint 之外的平台都能用）
# =============================================================================
# 什么时候用 Docker：
#   - 部署平台不认 render.yaml（比如腾讯云容器服务、阿里云、Railway 自定义）
#   - 想在国内云服务器上自己跑（配合 -p 80:8000）
#
# 构建与运行：
#   docker build -t melody-mood-sketch .
#   docker run -p 8000:8000 melody-mood-sketch
#
# 带大模型 Key 运行：
#   docker run -p 8000:8000 --env-file .env melody-mood-sketch
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
# 曲库片段（17 首 × 35 秒 ≈ 9MB）随镜像带上，云端才有真歌可放
COPY storage/library ./storage/library

# 运行期不需要 ffmpeg（只有裁剪曲库的离线脚本用）——镜像保持精简

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# 云平台会注入 PORT；没注入就用 8000（本地 docker run 直接可用）
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
