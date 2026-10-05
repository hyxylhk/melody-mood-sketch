"""
==============================================================================
guard.py —— 服务守护启动器（解决"网址有时候进不去"）
==============================================================================
问题背景：
    之前是直接在终端里跑 `python run.py`。这样有三个毛病：
        1. 终端一关，进程跟着没 → 网址立刻打不开
        2. 电脑重启 / 休眠唤醒后，没人帮你把服务再拉起来
        3. 程序万一崩了，没人知道，页面就一直转圈

这个脚本做的事（"守护进程"的最小实现）：
    ① 先探测 8000 端口：如果服务已经活着 → 只开浏览器，不重复启动
    ② 否则启动 run.py 作为子进程，并持续盯着它
    ③ 子进程一退出（崩溃 / 报错），等 3 秒自动重启
    ④ Ctrl+C 可以正常退出，不会留下野进程

用法：
    双击  启动服务.bat          ← 推荐（内部就是调用本脚本）
    或命令行  python guard.py

实现要点（给不熟悉运维的同学）：
    subprocess 就是"在 Python 里启动另一个程序"，类似 C++ 的 system()/CreateProcess，
    但能拿到退出码、能随时终止，比 system() 可控得多。
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PORT = 8000
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"
RESTART_DELAY = 3          # 崩溃后隔几秒重启（秒）


def _read_port_from_env() -> int:
    """从 .env 里读一下 APP_PORT，允许改端口（读不到就用 8000）。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return PORT
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APP_PORT="):
                value = line.split("=", 1)[1].strip()
                if value.isdigit():
                    return int(value)
    except OSError:
        pass
    return PORT


def health_ok(url: str) -> bool:
    """探测服务是否活着：能拿到 /api/health 的 200 就算活着。"""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def open_browser(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open_new_tab(url)
    except Exception:      # 打不开浏览器无所谓，手动输地址也行
        pass


def main() -> int:
    port = _read_port_from_env()
    url = f"http://127.0.0.1:{port}/api/health"
    page = f"http://127.0.0.1:{port}"

    # ---------- ① 已经活着 → 不重复启动 ----------
    if health_ok(url):
        print(f"[GUARD] 服务已经在运行了，直接打开页面：{page}")
        open_browser(page)
        print("[GUARD] 这个窗口可以关掉，服务会在后台继续跑。")
        return 0

    python_exe = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)      # 没有虚拟环境就用当前解释器

    print("=" * 60)
    print("  旋律情绪速写 · 服务守护已启动")
    print(f"  页面地址 : {page}")
    print("  这个黑窗口不要关 —— 关掉服务就停了")
    print("  按 Ctrl+C 可以停止服务")
    print("=" * 60)

    opened = False
    restarts = 0

    while True:
        try:
            proc = subprocess.Popen(
                [str(python_exe), "run.py"],
                cwd=str(PROJECT_ROOT),
            )

            # ---------- 子进程刚起来时，探一下活没活 ----------
            for _ in range(40):                 # 最多等 20 秒
                time.sleep(0.5)
                if health_ok(url):
                    if not opened:
                        print(f"[GUARD] 服务就绪，正在打开 {page}")
                        open_browser(page)
                        opened = True
                    break
                if proc.poll() is not None:     # 还没起来就退出了 → 启动失败
                    break

            code = proc.wait()                  # 阻塞等待子进程结束
            restarts += 1
            print(f"[GUARD] 服务进程退出（退出码 {code}），{RESTART_DELAY} 秒后自动重启…")
            print("[GUARD] 如果是端口被占用，请先关掉另一个已经在跑的服务窗口。")
            time.sleep(RESTART_DELAY)

        except KeyboardInterrupt:
            print("\n[GUARD] 收到 Ctrl+C，正在停止服务…")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            print("[GUARD] 已停止。")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
