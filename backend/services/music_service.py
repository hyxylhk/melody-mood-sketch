"""
==============================================================================
backend/services/music_service.py —— 音乐生成编排层
==============================================================================
职责：拿到情绪分析结果后，决定"用哪种方式生成音乐"，并且负责把进度写回数据库。

    情绪结果 EmotionResult
            │
            ▼
    ┌──────────────────┐
    │  provider 决策   │  ← 读 settings.MUSIC_PROVIDER
    └────────┬─────────┘
             │
     ┌───────┴────────┬──────────────────┐
     ▼                ▼                  ▼
  "httpapi"      "local"          "auto"
  第三方API      本地算法合成      优先第三方，失败转本地
     │                │                  │
     └────────┬───────┴──────────────────┘
              ▼
        更新 MusicTrack 状态 + 音频地址

关于前台为什么要「轮询」而不是「等着返回」：
    音乐生成是慢活（本地 2~8 秒，云端 30 秒~3 分钟）。
    如果 HTTP 请求一直不返回，浏览器会超时、界面卡死。
    所以做法是：
        1. 前端发请求 POST /api/music/generate  → 立刻拿到一个 track_id
        2. 后端在**后台任务**里慢慢生成
        3. 前端每隔 2 秒问一次 GET /api/music/{track_id} → 看 status 变成 ready 没
    这就是所谓的「异步任务 + 状态轮询」，是 Web 开发里处理耗时任务的标准套路。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, Optional

import httpx

from backend.config import get_settings
from backend.database import SessionLocal
from backend import crud as crud_ops
from backend.services.emotion_catalog import EmotionResult
from backend.services import music_library


# ===========================================================================
# 一、Prompt 构造：把中文情绪翻译成英文音乐描述
# ===========================================================================
def build_music_prompt(emotion: EmotionResult) -> str:
    """
    优先用大模型给的 music_prompt（它理解上下文更深）；
    没有的话就用情绪字典里的默认模板兜底。
    """
    if emotion.music_prompt:
        return emotion.music_prompt

    profile = emotion.profile
    # 用 _default_music_prompt 里同样的逻辑拼一份（避免循环 import）
    style = ", ".join(profile.style_words)
    return (
        f"{style}, around {profile.tempo_bpm} BPM, instrumental only, "
        f"no vocals, loopable background music, mood: {profile.label_en.lower()}"
    )


def resolve_provider() -> str:
    """
    决定实际要走的通道。返回 "httpapi" 或 "local"。

    这样写的意义：前端 / 健康检查接口可以提前告诉用户
    "你现在用的是哪条通道"，演示时心里有数。
    """
    settings = get_settings()
    provider = (settings.MUSIC_PROVIDER or "auto").strip().lower()

    if provider == "local":
        return "local"
    if provider == "httpapi":
        return "httpapi"
    # auto：配了第三方 Key 就试第三方，否则本地
    return "httpapi" if settings.external_music_enabled else "local"


# ===========================================================================
# 二、通道 A：第三方音乐生成 API（模板实现）
# ===========================================================================
async def _generate_by_external_api(emotion: EmotionResult, duration: int) -> Dict[str, object]:
    """
    调用第三方音乐生成服务（Suno 类）。

    ⚠️ 注意（务必读这段注释）：
    各家第三方音乐 API 的字段名都不一样，不可能写出一个通用实现。
    这里给出的是**标准轮询模式模板**：
        1) POST 提交生成任务 → 拿到 task_id
        2) 反复 GET 查询状态 → 直到成功/失败/超时
        3) 成功后拿到远端音频 URL

    换成你自己的服务商时，只需要改下面三处：
        - 提交地址 _SUBMIT_PATH
        - payload 的字段名
        - 从 body 里取音频地址的键名
    其余轮询逻辑（超时、退避间隔、异常处理）可以直接复用。
    """
    settings = get_settings()
    base = settings.MUSIC_API_BASE_URL.rstrip("/")

    headers = {
        "Authorization": f"Bearer {settings.MUSIC_API_KEY}",
        "Content-Type": "application/json",
    }
    submit_payload = {
        "prompt": build_music_prompt(emotion),
        "make_instrumental": True,        # 只要纯音乐，不要人声（日记是文字的，别抢戏）
        "duration": duration,
        "model": "music-default",
    }

    async with httpx.AsyncClient(timeout=settings.MUSIC_API_TIMEOUT) as client:
        # ---------- 步骤 1：提交任务 ----------
        response = await client.post(f"{base}/generate", headers=headers, json=submit_payload)
        response.raise_for_status()
        body = response.json()
        task_id = body.get("id") or body.get("task_id") or body.get("data", {}).get("id")
        if not task_id:
            raise RuntimeError(f"第三方接口没返回任务 ID：{body}")

        # ---------- 步骤 2：轮询结果 ----------
        waited = 0
        poll_interval = 5          # 每 5 秒问一次，别把对方接口打挂
        max_wait = settings.MUSIC_MAX_WAIT_SECONDS

        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval

            status_resp = await client.get(f"{base}/status/{task_id}", headers=headers)
            status_resp.raise_for_status()
            status_body = status_resp.json()

            # 不同厂商返回这一段的字段名差别最大，这里兼容几种常见写法
            data = status_body.get("data", status_body)
            state = str(data.get("status") or data.get("state") or "").lower()

            if state in ("complete", "completed", "success", "succeeded", "ready"):
                audio_url = (
                    data.get("audio_url")
                    or data.get("audioUrl")
                    or data.get("url")
                    or (data.get("clips") or [{}])[0].get("audio_url")
                )
                if not audio_url:
                    raise RuntimeError(f"生成成功但没拿到音频地址：{data}")
                return {"audio_url": audio_url, "provider": "httpapi", "meta": data}

            if state in ("failed", "error", "cancelled"):
                raise RuntimeError(f"第三方生成失败：{data.get('message') or data}")

            # 其它状态继续等

    raise TimeoutError(f"第三方音乐生成超过 {max_wait} 秒仍未完成")


# ===========================================================================
# 三、通道 B：本地算法合成
# ===========================================================================
async def _generate_by_local_engine(
    emotion: EmotionResult,
    track_id: int,
    duration: int,
) -> Dict[str, object]:
    """
    调用本地合成。

    关键一行是 `asyncio.to_thread`：
        CPU 密集型的合成运算如果在协程里直接跑，会**阻塞事件循环**，
        导致这 2~8 秒内整个后端无法响应任何请求（健康检查都超时）。
        to_thread 会把它丢到线程池里跑，主线程继续处理其它请求。
    """
    from backend.services.local_composer import render_wav_for_emotion

    settings = get_settings()
    audio_dir: Path = settings.audio_dir

    def _blocking_render() -> Dict[str, object]:
        return render_wav_for_emotion(
            profile=emotion.profile,
            audio_dir=audio_dir,
            emotion_code=emotion.primary,
            track_id=track_id,
            duration_sec=duration,
            sample_rate=settings.MUSIC_SAMPLE_RATE,
        )

    meta = await asyncio.to_thread(_blocking_render)

    return {
        # 前端拿这个相对路径去请求后端的静态文件服务
        "audio_url": f"/media/{meta['file_name']}",
        "file_path": meta["file_path"],
        "provider": "local",
        "meta": meta,
    }


# ===========================================================================
# 四、后台任务主入口
# ===========================================================================
async def generate_music_task(
    track_id: int,
    emotion: EmotionResult,
    force_local: bool = False,
) -> None:
    """
    后台生成任务。由 FastAPI 的 BackgroundTasks 触发，在响应返回之后继续执行。

    ⚠️ 重要：这里**必须新建数据库会话**。
    因为触发这个任务的那个 HTTP 请求早已返回，它对应的 Session 已经关闭了，
    继续用会报 "Session is closed"。

    整个函数被 try/except 包住，保证无论出什么错，
    数据库里的状态都会被标记成 failed，前端轮询时能看到错误信息而不是一直转圈。
    """
    settings = get_settings()
    db = SessionLocal()

    try:
        crud_ops.mark_track_processing(db, track_id)

        # ------------------------------------------------------------------
        # 【第 0 通道】官方素材曲库（优先级最高）
        # ------------------------------------------------------------------
        # 曲库里有的歌 = 官方审过的、好听的、零耗时的。
        # 只要开了 MUSIC_LIBRARY_FIRST 且挑到匹配曲目，就直接用它，
        # 完全跳过后面昂贵的"生成"环节。
        #
        # 注意：这里故意不把 file_path 写进 MusicTrack ——
        # 曲库文件是共享资产，如果记了 file_path，
        # 删除日记时会被当成本次生成的产物一起清掉（见 routers/diary.py 的删除逻辑）。
        # 文件位置改存 meta_json 里，仅作展示用途。
        if settings.MUSIC_LIBRARY_FIRST:
            lib_track = music_library.pick_library_track(emotion.primary)
            if lib_track is None:
                # 该情绪在曲库里没有专属曲目 → 从库里挑一首平静曲兜底。
                # 演示场合宁可"情绪不那么精准"也不出现算法合成的音乐，
                # 保证用户听到的永远是素材库里的真歌。
                lib_track = music_library.pick_library_track("neutral")
            if lib_track is not None:
                print(f"[INFO] track_id={track_id} 命中素材曲库：{lib_track['title']}")
                crud_ops.mark_track_ready(
                    db=db,
                    track_id=track_id,
                    audio_url=str(lib_track["audio_url"]),
                    provider="library",
                    duration_seconds=0,
                    file_path=None,          # 留空 = 删除日记时不清理这个文件
                    meta=lib_track["meta"],
                    prompt=build_music_prompt(emotion),
                )
                return

        # force_local=True 时直接钉死用本地合成（演示零成本/断网路线用）
        provider = "local" if force_local else resolve_provider()
        duration = settings.MUSIC_DURATION_SECONDS
        result: Optional[Dict[str, object]] = None

        if provider == "httpapi":
            try:
                result = await _generate_by_external_api(emotion, duration)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] 第三方音乐 API 失败，降级本地合成：{exc}")
                result = None
                if (settings.MUSIC_PROVIDER or "").lower() != "auto":
                    # 用户明确指定了只用 httpapi，那就不再降级，直接报错
                    raise

        if result is None:
            result = await _generate_by_local_engine(emotion, track_id, duration)

        crud_ops.mark_track_ready(
            db=db,
            track_id=track_id,
            audio_url=str(result["audio_url"]),
            provider=str(result.get("provider", "local")),
            file_path=result.get("file_path"),
            duration_seconds=duration,
            meta=result.get("meta"),
            prompt=build_music_prompt(emotion),
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 音乐生成失败 track_id={track_id}: {exc}")
        crud_ops.mark_track_failed(db, track_id, f"{type(exc).__name__}: {exc}")
    finally:
        # 一定要关会话，否则 SQLite 连接会泄漏
        db.close()
