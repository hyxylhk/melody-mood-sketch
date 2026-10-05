"""
==============================================================================
backend/config.py —— 全局配置中心（唯一读取 .env 的地方）
==============================================================================
给不懂前后端的同学：
    这个文件的作用相当于 C++ 里的 "全局配置单例"。
    所有可能变化的参数（端口、密钥、模型名）都写在一个名叫 .env 的文本文件里，
    由本文件统一读进来，转成 Python 对象供全项目使用。

    这样做的好处：
    1. 密钥不会写死在代码里，提交 GitHub 时不会泄露（.env 已被 .gitignore 忽略）
    2. 换模型 / 换端口只需要改 .env，不需要改任何 .py 文件

    使用方式（其它文件里）：
        from backend.config import get_settings
        settings = get_settings()
        print(settings.llm_api_key)
"""

from __future__ import annotations

# functools.lru_cache：让函数在第一次调用后把结果缓存起来。
# 也就是说整个进程里 .env 只会读一次，之后大家都共用同一个 Settings 对象（类似单例）。
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
# Path(__file__)                    = .../emotion-music-diary/backend/config.py
# .resolve()                        = 转成绝对路径（消除 ../ 这种写法）
# .parent                           = .../backend
# .parent.parent                    = .../emotion-music-diary  （项目根目录）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# .env 文件的绝对路径。放在项目根目录，任何工作目录下都能正确读到。
ENV_FILE: Path = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """
    所有配置项都在这里声明。

    类属性名用小写下划线（app_debug），
    它会自动去 .env / 系统环境变量里找同名的大写变量（APP_DEBUG）。
    冒号后面的 `= xxx` 是默认值 —— .env 里没写就用默认值。
    """

    # model_config 是 pydantic-settings 规定的写法，告诉它去哪里找配置
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),        # .env 文件路径
        env_file_encoding="utf-8",     # 编码用 utf-8，不然中文注释/值会乱码
        case_sensitive=False,          # 环境变量名不区分大小写：APP_DEBUG 和 app_debug 等价
        extra="ignore",                # .env 里出现这里没声明的变量时忽略，不报错
    )

    # ======================= 一、基础服务 =======================
    APP_NAME: str = "Melody Mood Sketch"
    APP_VERSION: str = "1.0.0"
    APP_DEBUG: bool = True                 # True = 改代码自动重启（开发用）；上线要设 False
    APP_HOST: str = "0.0.0.0"              # 0.0.0.0 = 允许同一局域网的设备访问
    APP_PORT: int = 8000

    # ======================= 二、数据库 =======================
    # SQLAlchemy 的连接串格式：sqlite:///相对路径
    # 注意 SQLite 是文件型数据库，这个路径就是那个 .db 文件存放的位置
    DATABASE_URL: str = "sqlite:///./storage/diary.db"

    # 生成的音乐文件存放目录
    AUDIO_DIR: str = "./storage/audio"

    # ======================= 三、CORS 跨域 =======================
    # 浏览器有个安全机制：JS 默认不许访问"不同域名/端口"的接口。
    # 这里显式告诉浏览器"我允许这些来源访问"，否则前端调用后端会报
    # 'Access-Control-Allow-Origin' 错误。
    # "*" 表示允许一切来源（开发/演示最省事）
    CORS_ORIGINS: str = "*"

    # ======================= 四、大模型（情绪解析）=======================
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""                  # 留空 = 没配置，会自动走本地规则降级
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT: int = 30                  # 单次请求超时秒数
    LLM_FALLBACK_ENABLED: bool = True      # 大模型挂了是否自动降级为本地关键词分析

    # ======================= 五、音乐生成 ========================
    # auto    = 优先云端 API，失败自动降级本地合成（推荐）
    # local   = 只用本地算法合成 WAV（零费用零依赖）
    # httpapi = 只用第三方音乐生成 API（需配下面的 Key）
    MUSIC_PROVIDER: str = "auto"

    MUSIC_API_BASE_URL: str = ""
    MUSIC_API_KEY: str = ""
    MUSIC_API_TIMEOUT: int = 120
    MUSIC_MAX_WAIT_SECONDS: int = 120      # 云端生成轮询的最长等待时间

    # 本地合成参数
    # 音乐时长（秒）。默认 35 秒与曲库片段长度一致 ——
    # 这样"曲库命中"和"本地合成"两条路出来的都是短旋律，听感统一，
    # 也对应作品介绍里承诺的"短氛围旋律片段"。
    MUSIC_DURATION_SECONDS: int = 35
    MUSIC_SAMPLE_RATE: int = 44100         # 采样率，44100 = CD 音质

    # ---------------- 官方素材曲库（大赛提供的歌单音频放这里）----------------
    # 把下载好的素材音频（mp3/m4a/wav/flac/ogg）放进 storage/library 即可。
    # 文件命名规则：{情绪代号}_{任意名字}.{后缀}，例如 joy_01_晴天.mp3
    # 情绪代号见 backend/services/emotion_catalog.py，共 17 种：
    #   joy sadness anger anxiety loneliness fatigue nostalgia love excitement
    #   gratitude hope moved relief stress tenderness confusion neutral
    # 前缀不是有效情绪代号的文件会被归入 "unknown" 桶，只在找不到精确匹配时兜底。
    MUSIC_LIBRARY_DIR: str = "./storage/library"
    # True = 生成配乐时优先从曲库挑（好听、稳定、零耗时）；曲库没有匹配才本地合成
    MUSIC_LIBRARY_FIRST: bool = True

    # ======================= 六、业务限制 ========================
    MAX_DIARY_LENGTH: int = 5000           # 单篇日记最大字数
    MIN_DIARY_LENGTH: int = 2              # 最少字数（防止只写标点）

    # ------------------------------------------------------------------
    # 下面这些是「计算属性」：不是从 .env 读的，而是根据上面的值算出来的。
    # 用 @property 装饰后，可以像普通属性一样用 settings.cors_origins_list
    # ------------------------------------------------------------------

    @property
    def cors_origins_list(self) -> List[str]:
        """把 "http://a.com,http://b.com" 这种字符串切成列表，交给 CORSMiddleware。"""
        return [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]

    @property
    def audio_dir(self) -> Path:
        """音乐输出目录的绝对路径，不存在则自动创建。"""
        path = (PROJECT_ROOT / self.AUDIO_DIR).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def library_dir(self) -> Path:
        """官方素材曲库目录的绝对路径，不存在则自动创建。"""
        path = (PROJECT_ROOT / self.MUSIC_LIBRARY_DIR).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database_path(self) -> Path:
        """
        SQLite 数据库文件的绝对路径。

        优先从 DATABASE_URL 里解析路径（这样测试时可以把库指到临时目录，
        不会污染你真实的数据），没配就用默认的 storage/diary.db。
        """
        raw = (self.DATABASE_URL or "").strip()

        path_part = ""
        if raw.startswith("sqlite"):
            path_part = raw.split("///", 1)[-1]
        if not path_part:
            path_part = "storage/diary.db"

        path = Path(path_part)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def llm_enabled(self) -> bool:
        """
        判断大模型是否真的可用。
        过滤掉默认的占位符（sk-在这里填你的密钥 这种），避免"配了个假 Key"导致
        每次都调用失败白白浪费 2 秒。
        """
        key = (self.LLM_API_KEY or "").strip()
        if not key:
            return False
        # 常见的模板占位符，直接判定为未配置
        placeholders = ("在这里", "your", "xxx", "TODO", "changeme", "填写")
        lowered = key.lower()
        return not any(p.lower() in lowered for p in placeholders)

    @property
    def external_music_enabled(self) -> bool:
        """判断有没有配置第三方音乐生成 API。"""
        return bool(
            (self.MUSIC_API_BASE_URL or "").strip()
            and (self.MUSIC_API_KEY or "").strip()
        )

    @field_validator("APP_DEBUG", mode="before")
    @classmethod
    def _parse_bool(cls, value):
        """
        .env 里写的是字符串 "True"/"true"/"1"。
        pydantic 通常能自动转，但为了兼容性（有人写 True 有人写 true）这里手动统一处理。
        """
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    获取全局唯一的配置对象。

    第一次调用会真正读取 .env，之后都是直接返回缓存对象。
    （注意：正因为有缓存，改完 .env 必须重启服务才生效。）
    """
    return Settings()
