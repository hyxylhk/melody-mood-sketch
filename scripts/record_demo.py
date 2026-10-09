"""
============================================================================
scripts/record_demo.py —— 一键生成参赛演示视频（竖屏 1080×1920 MP4）
============================================================================
做什么
======
驱动真实浏览器（系统 Edge，无需下载内核）按"评委视角"走完整个产品流程，
同时用 Playwright 自动录像，最后用 ffmpeg 转成可提交的 MP4。

视频结构（约 1 分 50 秒，全程中文字幕）
======================================
    0  产品定位：为什么做这个     （痛点 + 解法）
    1  首页亮相
    2  30 天情绪日历热力图
    3  效价折线：心情起伏趋势
    4  写日记：逐字输入
    5  AI 读懂情绪
    6  自动配乐 + 播放
    7  历史卡片沉淀
    8  删除动效：抖动 → 粉碎成粒子坠落
    9  结尾：技术方案 + 价值

为什么这样做
============
- **可重复**：改一行代码就能重录，不像手动录屏每次都要人肉对齐。
- **带字幕**：字幕是直接注入到页面顶部的 DOM 元素，录进视频里就是原生字幕，
  满足"须含语音讲解或字幕"的要求，不用再学剪辑软件。
- **竖屏 9:16**：视口 405×720 → 输出放大到 1080×1920，顺便证明"这是真 H5，
  手机上能完整用"。

用法
====
    python scripts/record_demo.py              # 录公网部署版本（默认）
    python scripts/record_demo.py --local      # 录本地 http://127.0.0.1:8000

产物
====
    docs/demo_video.mp4        1080×1920 竖屏、带中文字幕

注意
====
- 录屏**没有声音**（Playwright 只录画面），靠字幕完成讲解。
- 录制会在公网服务上真实创建一篇日记，脚本结束前会自动删掉，不污染数据库。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 默认录公网部署版（评委看到的就是这个地址）；--local 切回本地调试
PUBLIC_URL = "https://melody-mood-sketch-325689-12-1501527734.sh.run.tcloudbase.com"
LOCAL_URL = "http://127.0.0.1:8000"
BASE_URL = LOCAL_URL if "--local" in sys.argv else PUBLIC_URL

OUT_DIR = PROJECT_ROOT / "docs"
RAW_VIDEO = OUT_DIR / "_demo_raw.webm"
FINAL_VIDEO = OUT_DIR / "demo_video.mp4"
FFMPEG = r"C:\Program Files\Tecplot\Tecplot 360 EX 2024 R1\bin\ffmpeg"

# 竖屏 9:16：视口 = 录制尺寸 = 540×960，最后由 ffmpeg 等比放大到 1080×1920。
# ⚠️ 踩坑：Playwright 录像**只缩小不放大**——record_video_size 比 viewport 大，
# 画面会缩在角落、其余全是灰底。所以必须"视口 = 录制尺寸"，放大交给 ffmpeg。
VIEWPORT = {"width": 540, "height": 960}
VIDEO_SIZE = {"width": 540, "height": 960}

# 演示用的日记文本（开心 → joy → 曲库命中 joy_Batty McFaddin）
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
            'padding:16px 18px', 'text-align:center',
            'font-size:20px', 'font-weight:700', 'line-height:1.5',
            'color:#fff', 'background:linear-gradient(180deg, rgba(8,10,20,.95), rgba(8,10,20,.80))',
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


# 字幕时间轴：记录每条字幕出现的时刻，之后给视频配语音旁白时要用
CAPTION_TIMELINE: list = []
RECORD_T0 = [0.0]


def caption(page, text: str, hold_ms: int = 2600) -> None:
    """显示一条字幕并停留。hold_ms 里视频就是"看字幕 + 看画面"的时间。"""
    CAPTION_TIMELINE.append({
        "text": text,
        "at_ms": int((time.time() - RECORD_T0[0]) * 1000),
        "hold_ms": hold_ms,
    })
    page.evaluate(CAPTION_JS, text)
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


def api_json(path: str, method: str = "GET"):
    """用标准库打后端接口（不额外依赖 requests）。"""
    req = urllib.request.Request(BASE_URL + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            import json
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 录制脚本，失败只打印
        print("接口调用失败:", path, exc)
        return None


def max_diary_id() -> int:
    """当前库里最大的日记 id，用来识别录制时新建的那条。"""
    data = api_json("/api/diary?limit=1")
    if not data:
        return 0
    items = data.get("items") or []
    return items[0]["id"] if items else 0


def cleanup_created(before_id: int) -> None:
    """删掉录制期间新建的日记，别把测试数据留在评委能看到的服务上。"""
    data = api_json("/api/diary?limit=5")
    if not data:
        return
    for item in data.get("items") or []:
        if item["id"] > before_id:
            api_json(f"/api/diary/{item['id']}", method="DELETE")
            print(f"已清理录制产生的日记 id={item['id']}")


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if RAW_VIDEO.exists():
        RAW_VIDEO.unlink()

    print(f"录制目标：{BASE_URL}")
    before_id = max_diary_id()
    # 字幕时间轴的 0 点会在 page 建好后重置（见下方 new_page 之后），
    # 这样时间轴 == 视频时间轴，后面配音才能对得上。

    with sync_playwright() as p:
        # channel="msedge"：用系统自带的 Edge，不用下载 195MB 的浏览器内核
        # --autoplay-policy：允许无手势自动播放（模拟点击播放时不受限制）
        browser = p.chromium.launch(
            channel="msedge",
            args=["--autoplay-policy=no-user-gesture-required", "--no-proxy-server"],
        )
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(OUT_DIR),
            record_video_size=VIDEO_SIZE,
        )
        page = context.new_page()
        # 录像从这里开始写盘，字幕时间轴必须以这一刻为 0 点
        RECORD_T0[0] = time.time()

        # ---------- 第 0 幕：产品定位（why） ----------
        page.goto(BASE_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(1500)

        # CloudBase 测试域名会先弹「风险提醒」拦截页，点掉"确定访问"才进真页面。
        # （评委用浏览器打开也会看到，已在部署文档里提醒要去掉它）
        if page.title() == "风险提醒":
            page.wait_for_timeout(2500)          # 等按钮上的 1 秒倒计时走完
            try:
                page.locator("button").first.click(timeout=8000)
                page.wait_for_timeout(3500)
                print("✓ 已点掉 CloudBase 风险提醒页")
            except Exception as exc:  # noqa: BLE001
                print("✗ 点风险提醒页失败:", type(exc).__name__)
        if page.locator(".trajectory-panel").count() == 0:
            print("✗ 页面没加载出来，后面的定位会超时")
        caption(page, "旋律情绪速写 · AI 音乐情绪日记本", 3500)
        caption(page, "痛点：文字日记写不出情绪的温度，也留不住感受", 4000)
        caption(page, "解法：写下心情 → AI 读懂情绪 → 自动配上专属旋律", 4500)

        # ---------- 第 1 幕：首页亮相 ----------
        caption(page, "打开就是今天：写下此刻的心情", 3000)

        # ---------- 第 2 幕：情绪轨迹 ----------
        caption(page, "30 天情绪日历，一眼看清心情的起伏", 4500)
        page.locator(".trajectory-panel").scroll_into_view_if_needed()
        page.wait_for_timeout(2000)

        # ---------- 第 3 幕：效价折线 ----------
        caption(page, "效价折线：低落的谷底，和慢慢爬回来的路", 4500)
        page.locator(".trend-block").scroll_into_view_if_needed()
        page.wait_for_timeout(2000)

        # ---------- 第 4 幕：写日记 ----------
        caption(page, "现在，写下此刻的心情", 2500)
        page.locator("textarea.diary-input").scroll_into_view_if_needed()
        hide_caption(page)
        box = page.locator("textarea.diary-input")
        box.click()
        box.press_sequentially(DEMO_TEXT, delay=45)   # 逐字输入，约 3 秒
        page.wait_for_timeout(800)

        # ---------- 第 5 幕：AI 分析 ----------
        caption(page, "点一下，AI 读懂情绪：17 类情绪 + 效价唤醒度", 4500)
        page.locator("#submitBtn").click()

        # ---------- 第 6 幕：配乐 + 播放 ----------
        if not wait_music_ready(page, timeout_ms=90_000):
            print("⚠ 音乐生成超时，视频里可能缺少播放画面")
        else:
            page.locator("#audioPlayer").scroll_into_view_if_needed()
            page.wait_for_timeout(600)
            page.evaluate(
                "() => { const p = document.getElementById('audioPlayer');"
                " p.muted = true; p.play().catch(() => {}); }"
            )
            caption(page, "公版钢琴曲库智能匹配，这是今天的心情主题曲 🎵", 6000)

        # ---------- 第 7 幕：卡片沉淀 ----------
        caption(page, "每一篇都变成「文字 + 旋律」的卡片，留成情绪轨迹", 4000)
        page.locator(".history-panel").scroll_into_view_if_needed()
        page.wait_for_timeout(2000)

        # ---------- 第 8 幕：删除动效 ----------
        caption(page, "删掉一篇：先抖动，再粉碎成粒子坠落消失", 3000)
        cards = page.locator(".history-item")
        if cards.count() > 0:
            card = cards.nth(0)
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(700)
            card.locator(".del-btn").click()
            page.wait_for_timeout(3000)      # 抖动 0.5s + 粉碎坠落 ~1.2s + toast
        page.wait_for_timeout(800)

        # ---------- 第 9 幕：结尾 ----------
        caption(page, "FastAPI + SQLite + 原生前端，零构建、可离线、可移植", 4500)
        caption(page, "让每个人都能听见自己的心情", 3500)
        caption(page, "旋律情绪速写 · 谢谢观看", 3000)

        video_path = page.video.path() if page.video else None
        context.close()      # 关闭上下文才会把视频写盘
        browser.close()

    # 把字幕时间轴存下来，add_narration.py 会按它给视频配音
    tl_path = OUT_DIR / "caption_timeline.json"
    tl_path.write_text(
        json.dumps(CAPTION_TIMELINE, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"字幕时间轴已保存：{tl_path.name}（{len(CAPTION_TIMELINE)} 条）")

    if not video_path or not Path(video_path).exists():
        print("✘ 没有拿到视频文件")
        return 1

    # ---------- webm → mp4（H.264，方便评委在任何播放器打开）----------
    cmd = [
        FFMPEG, "-v", "error", "-y",
        "-i", str(video_path),
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p",          # 兼容性：部分播放器不支持 4:4:4
        "-movflags", "+faststart",      # 索引放文件头，网页播放秒开
        "-vf", "scale=1080:1920",
        str(FINAL_VIDEO),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    Path(video_path).unlink(missing_ok=True)

    if result.returncode != 0:
        print("ffmpeg 转码失败:", result.stderr[:400])
        return 1

    cleanup_created(before_id)

    size_mb = FINAL_VIDEO.stat().st_size / 1024 / 1024
    print(f"\n✔ 演示视频已生成：{FINAL_VIDEO.relative_to(PROJECT_ROOT)}（{size_mb:.1f}MB）")
    print("  1080×1920 竖屏、带中文字幕、无声")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
