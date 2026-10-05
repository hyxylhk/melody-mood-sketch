"""
============================================================================
scripts/record_demo.py —— 一键生成参赛演示视频（竖屏 1080×1920 MP4）
============================================================================
做什么
======
驱动真实浏览器（系统 Edge，无需下载内核）按"评委视角"走完整个产品流程，
同时用 Playwright 自动录像，最后用 ffmpeg 转成可提交的 MP4。

为什么这样做
============
- **可重复**：改一行代码就能重录，不像手动录屏每次都要人肉对齐。
- **带字幕**：字幕是直接注入到页面顶部的 DOM 元素，录进视频里就是原生字幕，
  不需要再学视频剪辑软件。
- **竖屏 9:16**：视口 405×720（正好 9:16），输出放大到 1080×1920，
  顺便证明了"这是真 H5，手机上能完整用"。

用法
====
    # 前置：服务已在本地跑起来（python run.py）
    python scripts/record_demo.py

产物
====
    docs/demo_video.mp4        1080×1920 的演示视频（约 60~75 秒，无声+字幕）

注意
====
- 录屏**没有声音**（Playwright 只录画面）。视频里"播放音乐"的那一段
  画面会显示播放器进度条在走，配合字幕"现场演示时可以亲耳听到"。
- 全程走本地服务 + 素材曲库，不依赖外网，可以反复重录。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BASE_URL = "http://127.0.0.1:8000"
OUT_DIR = PROJECT_ROOT / "docs"
RAW_VIDEO = OUT_DIR / "_demo_raw.webm"
FINAL_VIDEO = OUT_DIR / "demo_video.mp4"

# 竖屏 9:16：视口 = 录制尺寸 = 540×960，最后由 ffmpeg 等比放大到 1080×1920。
# ⚠️ 踩坑记录：Playwright 的录像**只缩小不放大**——如果 record_video_size
# 比 viewport 大，画面会以原始尺寸缩在角落、其余全是灰底。
# 所以这里必须"视口 = 录制尺寸"，放大交给 ffmpeg（无损等比 2 倍）。
VIEWPORT = {"width": 540, "height": 960}
VIDEO_SIZE = {"width": 540, "height": 960}

# 演示用的日记文本（开心 → joy → 曲库会命中 joy_Batty McFaddin）
DEMO_TEXT = (
    "熬了三个通宵项目终于上线了！看到用户发来的第一句夸奖，"
    "鼻子有点酸，但真的超级开心，一切都值了。"
)


# ---------------------------------------------------------------------------
# 字幕：直接把 DOM 元素注入页面顶部，录进视频就是字幕
# ---------------------------------------------------------------------------
CAPTION_JS = """
(text) => {
    let bar = document.getElementById('__demoCaption');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = '__demoCaption';
        bar.style.cssText = [
            'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:99999',
            'padding:14px 18px', 'text-align:center',
            'font-size:19px', 'font-weight:700', 'line-height:1.5',
            'color:#fff', 'background:linear-gradient(180deg, rgba(8,10,20,.94), rgba(8,10,20,.78))',
            'border-bottom:1px solid rgba(124,92,255,.5)',
            'text-shadow:0 2px 6px rgba(0,0,0,.8)',
            'transition:opacity .35s',
        ].join(';');
        document.body.appendChild(bar);
    }
    bar.textContent = text;
    bar.style.opacity = '1';
}
"""

CAPTION_HIDE_JS = """
() => {
    const bar = document.getElementById('__demoCaption');
    if (bar) bar.style.opacity = '0';
}
"""


def caption(page, text: str, hold_ms: int = 2600) -> None:
    """显示一条字幕并停留。hold_ms 里视频就是"看字幕 + 看画面"的时间。"""
    page.evaluate(CAPTION_JS, arg=text)
    page.wait_for_timeout(hold_ms)


def hide_caption(page) -> None:
    page.evaluate(CAPTION_HIDE_JS)


def wait_music_ready(page, timeout_ms: int = 60_000) -> bool:
    """轮询等音乐生成完成（播放器出现且不是 hidden）。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        visible = page.evaluate(
            """() => {
                const p = document.getElementById('audioPlayer');
                return !!(p && p.src && !p.classList.contains('hidden'));
            }"""
        )
        if visible:
            return True
        page.wait_for_timeout(500)
    return False


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if RAW_VIDEO.exists():
        RAW_VIDEO.unlink()

    with sync_playwright() as p:
        # channel="msedge"：用系统自带的 Edge，不用下载 195MB 的浏览器内核
        # --autoplay-policy=...：允许无手势自动播放（模拟点击播放时不受浏览器限制）
        browser = p.chromium.launch(
            channel="msedge",
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(OUT_DIR),
            record_video_size=VIDEO_SIZE,
        )
        page = context.new_page()

        # ---------- 第 1 幕：产品亮相 ----------
        page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(1200)
        caption(page, "旋律情绪速写 · 把心情写成音乐", 3000)

        # ---------- 第 2 幕：30 天情绪轨迹 ----------
        caption(page, "30 天情绪日历，一眼看清心情起伏", 2200)
        page.locator(".trajectory-panel").scroll_into_view_if_needed()
        page.wait_for_timeout(1600)
        caption(page, "效价折线：低落的谷底，和慢慢爬回来的路", 2600)
        page.locator(".trend-block").scroll_into_view_if_needed()
        page.wait_for_timeout(1800)

        # ---------- 第 3 幕：写日记 ----------
        caption(page, "现在，写下此刻的心情", 2000)
        page.locator("textarea.diary-input").scroll_into_view_if_needed()
        hide_caption(page)
        box = page.locator("textarea.diary-input")
        box.click()
        # 逐字输入，模拟真人打字节奏（40ms/字，约 2.5 秒输完）
        box.press_sequentially(DEMO_TEXT, delay=40)
        page.wait_for_timeout(600)

        # ---------- 第 4 幕：生成 ----------
        caption(page, "AI 读懂情绪，自动配上专属旋律", 2000)
        page.locator("#submitBtn").click()

        if not wait_music_ready(page, timeout_ms=90_000):
            print("⚠ 音乐生成超时，视频里可能缺少播放画面")
        else:
            # 把播放器滚进画面，让评委看到"正在播放"的进度条
            page.locator("#audioPlayer").scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            # 让进度条走 4 秒，画面上能看到"正在播放"
            page.evaluate(
                "() => { const p = document.getElementById('audioPlayer');"
                " p.muted = true; p.play().catch(() => {}); }"
            )
            caption(page, "这是今天的心情主题曲 🎵", 4200)

        # ---------- 第 5 幕：卡片沉淀 ----------
        caption(page, "每一篇都变成「文字 + 旋律」的卡片，留成你的情绪轨迹", 2800)
        page.locator(".history-panel").scroll_into_view_if_needed()
        page.wait_for_timeout(1800)
        caption(page, "旋律情绪速写 · 谢谢观看", 2600)

        video_path = page.video.path() if page.video else None
        context.close()      # 关闭上下文才会把视频写盘
        browser.close()

    if not video_path or not Path(video_path).exists():
        print("✘ 没有拿到视频文件")
        return 1

    # ---------- webm → mp4（H.264，方便评委在任何播放器打开）----------
    print("视频原始文件:", video_path)
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(video_path),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-pix_fmt", "yuv420p",          # 兼容性：部分播放器不支持 4:4:4
        "-movflags", "+faststart",      # 把索引放文件头，网页播放秒开
        "-vf", "scale=1080:1920",
        str(FINAL_VIDEO),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        print("ffmpeg 转码失败:", result.stderr[:400])
        return 1

    Path(video_path).unlink(missing_ok=True)   # 删掉中间 webm
    _cleanup_demo_db()

    size_mb = FINAL_VIDEO.stat().st_size / 1024 / 1024
    print(f"\n✔ 演示视频已生成：{FINAL_VIDEO.relative_to(PROJECT_ROOT)}（{size_mb:.1f}MB）")
    print("  时长约 60~75 秒、1080×1920 竖屏、带中文字幕、无声")
    return 0


def _cleanup_demo_db() -> None:
    """
    清理这次录制时提交的日记。

    录制会真实调用一次"写日记 + 生成音乐"，数据会留在当前数据库里；
    不清理的话每录一次就多一条一模一样的日记，日历上也会多一格。
    按"内容等于演示文本"精确删除，不会误伤其他数据。
    """
    import os

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'storage' / 'diary.db'}")

    from sqlalchemy import delete

    from backend.database import SessionLocal
    from backend.models import Diary

    db = SessionLocal()
    try:
        result = db.execute(delete(Diary).where(Diary.content == DEMO_TEXT))
        db.commit()
        if result.rowcount:
            print(f"已清理录制时产生的 {result.rowcount} 条测试日记")
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
