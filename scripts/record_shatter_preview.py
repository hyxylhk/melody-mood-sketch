"""
============================================================================
scripts/record_shatter_preview.py —— 录制「删除日记」粉碎动效预览
============================================================================
用途
====
把删除日记的完整动效（抖动 → 粉碎成粒子 → 抛物线坠落）录成一段短视频，
用于自查效果 / 给别人展示。产物是 docs/shatter_preview.mp4。

为什么单独写一个脚本（而不塞进 record_demo.py）
================================================
record_demo.py 录的是"评委视角完整流程"（竖屏 9:16），而粉碎动效是
横向卡片，竖屏里太小看不清。这里用横屏视口、只聚焦历史列表区域，
镜头稳定、动画占满画面。

用法
====
    # 前置：本地服务已启动（python run.py --no-browser）
    python scripts/record_shatter_preview.py

注意事项
========
- 会真实删除一篇日记（取历史列表里的第 2 张卡片），数据是演示库可再灌。
- 录屏用系统 Edge（channel="msedge"），无需下载浏览器内核；
  --no-proxy-server 防止本机代理拦截 127.0.0.1。
- Playwright 只录视口画面（无声音），视口尺寸 = 录制尺寸，避免缩放错位。
"""

from __future__ import annotations

import subprocess  # noqa: S404
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "docs"
RAW_DIR = OUT_DIR / "_shatter_raw"
FINAL_VIDEO = OUT_DIR / "shatter_preview.mp4"
FFMPEG = r"C:\Program Files\Tecplot\Tecplot 360 EX 2024 R1\bin\ffmpeg"

BASE_URL = "http://127.0.0.1:8000"
VIEWPORT = {"width": 1180, "height": 860}


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for old in RAW_DIR.glob("*.webm"):
        old.unlink()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", args=["--no-proxy-server"])
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(RAW_DIR),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(1200)

        cards = page.locator(".history-item")
        if cards.count() < 2:
            print("✘ 历史列表不足 2 张卡片，无法录制（先跑 scripts/seed_demo.py）")
            context.close()
            browser.close()
            return 1

        # 取第 2 张（第 1 张往往是置顶卡），滚进视口正中再点删除
        card = cards.nth(1)
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(800)

        card.locator(".del-btn").click()
        # 抖动 0.52s + 粉碎坠落约 1.2s + toast 提示，留足时间
        page.wait_for_timeout(2800)

        video_path = page.video.path() if page.video else None
        context.close()
        browser.close()

    if not video_path or not Path(video_path).exists():
        print("✘ 没拿到视频文件")
        return 1

    cmd = [
        FFMPEG, "-v", "error", "-y",
        "-i", str(video_path),
        "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-vf", f"scale={VIEWPORT['width']}:{VIEWPORT['height']},fps=30",
        str(FINAL_VIDEO),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    for f in RAW_DIR.glob("*.webm"):
        f.unlink()
    RAW_DIR.rmdir()

    if result.returncode != 0:
        print("ffmpeg 转码失败:", result.stderr[:300])
        return 1

    print(f"✔ 预览已生成：{FINAL_VIDEO.relative_to(PROJECT_ROOT)}"
          f"（{FINAL_VIDEO.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
