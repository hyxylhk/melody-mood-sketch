"""
==============================================================================
backend/main.py —— FastAPI 应用入口
==============================================================================
这是整个后端的"总装车间"，负责把各个零件拼成一个可运行的服务：

    1. 创建 FastAPI 应用对象
    2. 注册生命周期钩子（启动时自动建表）
    3. 挂载 CORS 中间件（解决前后端跨域）
    4. 挂载静态文件服务（生成的音频 + 前端页面）
    5. 注册各个 router（接口分组）
    6. 注册全局异常处理

启动方式（任选其一，都在项目根目录执行）：
    python run.py                         ← 推荐，一行搞定
    python -m uvicorn backend.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import PROJECT_ROOT, get_settings
from backend.database import init_db
from backend.routers import diary as diary_router
from backend.routers import health as health_router
from backend.routers import music as music_router

# 前端页面所在目录
FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"


# ---------------------------------------------------------------------------
# 一、生命周期管理（lifespan）
# ---------------------------------------------------------------------------
# @asynccontextmanager 装饰的函数会变成：
#       yield 之前 = 服务启动时执行
#       yield 之后 = 服务关闭时执行
# 这里我们在启动时做「自动建表」和「打印配置自检」两件事。
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    print("=" * 66)
    print(f"  {settings.APP_NAME}  v{settings.APP_VERSION}")
    print("=" * 66)
    print(f"  数据库文件 : {settings.database_path}")
    print(f"  音频输出   : {settings.audio_dir}")
    print(f"  大模型     : {'已配置 ' + settings.LLM_MODEL if settings.llm_enabled else '未配置 → 情绪分析走本地规则'}")
    print(f"  音乐通道   : {settings.MUSIC_PROVIDER}（第三方 Key: {'已配' if settings.external_music_enabled else '未配'}）")
    print("-" * 66)
    print(f"  打开页面   : http://127.0.0.1:{settings.APP_PORT}")
    print(f"  接口文档   : http://127.0.0.1:{settings.APP_PORT}/docs")
    print("=" * 66)

    # 自动建表：没有 db 文件就新建，已有则什么都不做（不会清数据）
    init_db()

    yield   # ← 服务在 this 之后正式开始对外提供服务

    print("[INFO] 服务已停止，资源已释放")


# ---------------------------------------------------------------------------
# 二、创建应用对象
# ---------------------------------------------------------------------------
app = FastAPI(
    title="旋律情绪速写 API",
    description=(
        "写一段日记 → 大模型解析情绪 → 按情绪生成专属背景音乐。\n\n"
        "本后端同时提供前端页面的托管，浏览器直接访问根路径即可使用。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# 三、CORS 跨域配置（非常关键，不加前端一定报错）
# ---------------------------------------------------------------------------
# 科普：浏览器的同源策略规定，JS 只能访问"协议+域名+端口"三者都相同的接口。
# 如果你用 file:// 打开 index.html，或者前端跑在 5500 端口而后端在 8000，
# 就属于跨源请求，浏览器会先发一个 OPTIONS 预检请求问后端"我能不能调"。
# CORSMiddleware 的作用就是自动回答这个预检并加上允许头。
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,   # 允许的来源列表，["*"] = 全部放行
    allow_credentials=False,                    # allow_origins 为 * 时必须 False
    allow_methods=["*"],                        # 允许所有 HTTP 方法（GET/POST/PATCH/DELETE/OPTIONS）
    allow_headers=["*"],                        # 允许所有请求头（含 Content-Type、Authorization）
    expose_headers=["Content-Length", "Content-Disposition"],
    max_age=86400,                              # 预检结果缓存 24 小时，减少 OPTIONS 请求数量
)

# ---------------------------------------------------------------------------
# 四、注册业务路由
# ---------------------------------------------------------------------------
# prefix="/api" 表示这些接口最终的 URL 是 /api/diary、/api/music ...
app.include_router(health_router.router, prefix="/api")
app.include_router(diary_router.router, prefix="/api")
app.include_router(music_router.router, prefix="/api")

# ---------------------------------------------------------------------------
# 五、静态文件服务
# ---------------------------------------------------------------------------
# 【0】官方素材曲库：/media/library/xxx.mp3
# 注意：必须挂在 /media **之前** —— Starlette 的路由按注册顺序匹配，
# 如果 /media 先注册，/media/library 就会被它截走，导致曲库文件 404。
app.mount(
    "/media/library",
    StaticFiles(directory=str(settings.library_dir), check_dir=False),
    name="media-library",
)

# 【1】生成的音频：/media/emo_joy_1_xxx.wav
# StaticFiles 会把一个本地目录映射成可以直接 HTTP 访问的静态资源，
# 等价于 Nginx 的 alias 配置，省得我们再手写读文件+设置响应头的逻辑。
app.mount(
    "/media",
    StaticFiles(directory=str(settings.audio_dir), check_dir=False),
    name="media",
)

# 【2】前端静态资源（css/js）：/static/app.js
app.mount(
    "/static",
    StaticFiles(directory=str(FRONTEND_DIR), check_dir=False),
    name="static",
)


# 【3】前端零散资源（styles.css / app.js）的兜底路由
# 为什么要这个？因为 index.html 里写的是相对路径 "styles.css"，
# 这样无论后端托管访问，还是本地双击 file:// 打开，静态资源都能找到。
# 而 /static 挂载则用于反向代理 / CDN 场景。


def _no_cache_response(path: Path) -> FileResponse:
    """
    返回前端文件，并强制浏览器「每次都来问一次有没有更新」。

    为什么必须加这个头？
        我们迭代前端代码很频繁。浏览器为了快，会把 app.js 缓存在本地；
        结果就是：后端已经加了新接口，用户浏览器里跑的还是上一版 JS，
        调用旧路径 → 404 → 界面上弹出「操作失败 / 轮询失败」这种莫名其妙的错误。
        no-cache 不是"不缓存"，而是"缓存可以留，但每次用之前必须向服务器确认"。
        这是开发期和演示期最省心的一行配置。
    """
    return FileResponse(
        str(path),
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
@app.get("/{asset}", include_in_schema=False)
def serve_frontend_asset(asset: str):
    """
    只有在 frontend 目录里真实存在同名文件才返回，
    其它一律 404 —— 不会泄漏项目其它目录的内容。
    """
    candidate = FRONTEND_DIR / asset
    if candidate.is_file() and candidate.parent == FRONTEND_DIR:
        return _no_cache_response(candidate)
    return JSONResponse(status_code=404, content={"detail": f"资源不存在：{asset}"})


# ---------------------------------------------------------------------------
# 六、首页路由：直接返回前端页面
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False, summary="前端首页")
def serve_index() -> FileResponse:
    """
    访问 http://127.0.0.1:8000 时返回前端页面。

    这样做的好处是：前端和后端同在一个域名端口下（同源），
    前端 JS 调用 /api/xxx 完全不会触发跨域问题 —— 最省事的部署形态。
    """
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"前端页面不存在：{index_file}"},
        )
    return _no_cache_response(index_file)


# ---------------------------------------------------------------------------
# 七、全局异常处理
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    兜底异常处理。

    没有这个处理器，未捕获异常会以 HTML 格式返回 500，
    前端 fetch 拿到 HTML 解析会失败，页面只会显示"未知错误"。
    统一转成 JSON 后，前端至少能把错误原因显示给用户（排查问题友好）。
    """
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误",
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:500],
            "path": request.url.path,
        },
    )
