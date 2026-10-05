"""
============================================================================
scripts/smoke_test.py —— 端到端冒烟测试（对着**正在运行**的服务发真实请求）
============================================================================
它模拟一个真实用户的完整操作流程：

    健康检查 → 写日记 → 看情绪 → 等音乐生成 → 下载音频校验 → 查列表/统计 → 清理

用法：
    1. 先在一个终端里启动服务：  python run.py
    2. 再在另一个终端里执行：    python scripts/smoke_test.py

    可选参数：
        python scripts/smoke_test.py --url http://127.0.0.1:8000
        python scripts/smoke_test.py --keep      # 不删除测试数据

退出码：0 = 全部通过，1 = 有步骤失败。
（断言失败会打印 [FAIL]，全部通过会打印 summary）
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, Optional

try:
    import httpx
except ImportError:
    print("缺少 httpx，请先执行：pip install -r requirements.txt")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 测试用例：覆盖正向 / 中性 / 负向三类情绪
# ---------------------------------------------------------------------------
SAMPLE_DIARIES = [
    {
        "name": "开心样本",
        "payload": {
            "title": "上线日",
            "content": "今天项目终于上线了！熬了三个通宵，看到用户第一句夸奖的时候，"
                       "鼻子有点酸，但真的超级开心，觉得一切都值了，哈哈！",
            "auto_generate_music": True,
        },
    },
    {
        "name": "孤独样本",
        "payload": {
            "title": "一个人的夜晚",
            "content": "又是一个人吃饭、一个人走路、一个人回到空荡荡的房间。"
                       "窗外的灯一盏盏亮起来，没有一盏是为我亮的。突然有点难过。",
            "auto_generate_music": True,
        },
    },
    {
        "name": "焦虑样本",
        "payload": {
            "title": "deadline 前两天",
            "content": "距离 deadline 只剩两天，还有三个模块没做完，leader 又在催进度，"
                       "感觉快要喘不过气来了，很焦虑很慌。",
            "auto_generate_music": True,
        },
    },
]


class SmokeTester:
    """一个简单的测试执行器，记录成功/失败数量并打印彩色结果。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.passed = 0
        self.failed = 0
        self.created_diary_ids: list[int] = []

    # ---------------- 断言工具 ----------------
    def check(self, condition: bool, description: str, extra: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  [PASS] {description}" + (f"  {extra}" if extra else ""))
        else:
            self.failed += 1
            print(f"  [FAIL] {description}  {extra}")
        return condition

    # ---------------- 步骤 1：健康检查 ----------------
    def step_health(self) -> bool:
        print("\n[步骤 1] 健康检查 GET /api/health")
        resp = httpx.get(f"{self.base_url}/api/health", timeout=10)
        if resp.status_code != 200:
            self.check(False, "健康检查状态码 200", f"实际 {resp.status_code}")
            return False

        data = resp.json()
        self.check(data.get("status") == "ok", "服务状态 ok")
        print(f"          Python={data.get('python')}  DB={data.get('database')}")
        print(f"          LLM={'已配置 ' + str(data.get('llm_model')) if data.get('llm_enabled') else '未配置(本地规则)'}")
        print(f"          音乐通道={data.get('music_provider_effective')}")
        return True

    # ---------------- 步骤 2：CORS 跨域检查 ----------------
    def step_cors(self) -> None:
        print("\n[步骤 2] CORS 跨域预检 OPTIONS /api/diary")
        resp = httpx.options(
            f"{self.base_url}/api/diary",
            headers={
                "Origin": "http://localhost:5500",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=10,
        )
        allow_origin = resp.headers.get("access-control-allow-origin")
        self.check(
            allow_origin is not None,
            "响应含 Access-Control-Allow-Origin",
            f"= {allow_origin}",
        )

    # ---------------- 步骤 3：情绪字典 ----------------
    def step_emotions(self) -> None:
        print("\n[步骤 3] 情绪字典 GET /api/meta/emotions")
        resp = httpx.get(f"{self.base_url}/api/meta/emotions", timeout=10)
        data = resp.json()
        self.check(resp.status_code == 200, "状态码 200")
        self.check(isinstance(data, list) and len(data) >= 10, f"情绪种类数量 >= 10，实际 {len(data)}")

    # ---------------- 步骤 4：写日记 + 情绪分析 ----------------
    def step_create_diary(self, case: Dict[str, Any]) -> Optional[int]:
        print(f"\n[步骤 4] 写日记 POST /api/diary  —— {case['name']}")
        resp = httpx.post(f"{self.base_url}/api/diary", json=case["payload"], timeout=60)

        if not self.check(resp.status_code == 201, "状态码 201 Created", f"实际 {resp.status_code}"):
            print(f"         响应体：{resp.text[:300]}")
            return None

        diary = resp.json()
        diary_id = diary.get("id")

        emo = diary.get("emotion") or {}
        self.check(diary_id is not None, "返回了日记 ID", f"id={diary_id}")
        self.check(bool(emo.get("primary")), "返回了主情绪", f"{emo.get('primary_icon')} {emo.get('primary_label')}")
        self.check(-1 <= emo.get("valence", 0) <= 1, "效价在 [-1,1] 区间", f"valence={emo.get('valence')}")
        self.check(0 <= emo.get("arousal", 0) <= 1, "唤醒度在 [0,1] 区间", f"arousal={emo.get('arousal')}")
        print(f"         解读：{emo.get('summary')}")
        print(f"         来源：{emo.get('source')}")

        # 必要条件检查：这些内容要写进测试用例文档，所以打印全一点
        return diary_id

    # ---------------- 步骤 5：轮询音乐生成 ----------------
    def step_wait_music(self, diary_id: int, timeout: int = 180) -> Optional[Dict[str, Any]]:
        print(f"\n[步骤 5] 等待音乐生成 GET /api/music/track/{{id}}")
        start = time.time()
        last_status = ""

        while time.time() - start < timeout:
            resp = httpx.get(f"{self.base_url}/api/music/latest/{diary_id}", timeout=15)
            if resp.status_code != 200:
                self.check(False, "查询音乐任务", f"HTTP {resp.status_code}")
                return None

            track = resp.json()
            status = track.get("status")
            if status != last_status:
                print(f"         状态变化：{last_status or '—'} → {status}")
                last_status = status

            if status == "ready":
                elapsed = time.time() - start
                self.check(True, "音乐生成完成", f"耗时 {elapsed:.1f}s")
                print(f"         provider = {track.get('provider')}")
                print(f"         audio_url = {track.get('audio_url')}")
                print(f"         meta = {track.get('meta')}")
                return track

            if status == "failed":
                self.check(False, "音乐生成失败", track.get("error_message", ""))
                return None

            time.sleep(1.5)

        self.check(False, f"音乐生成超时（{timeout}s）")
        return None

    # ---------------- 步骤 6：下载并校验音频 ----------------
    def step_verify_audio(self, track: Dict[str, Any]) -> None:
        print("\n[步骤 6] 下载音频校验 GET <audio_url>")
        url = track["audio_url"]
        if url.startswith("/"):
            url = self.base_url + url

        resp = httpx.get(url, timeout=60, follow_redirects=True)
        self.check(resp.status_code == 200, "音频可下载", f"HTTP {resp.status_code}")
        self.check(len(resp.content) > 50_000, "音频大小合理", f"{len(resp.content)/1024:.1f} KB")

        # 音频格式校验（两种都合法，取决于命中的通道）：
        #   本地合成 → WAV，头 4 字节 'RIFF'，第 8~12 字节 'WAVE'
        #   曲库命中 → MP3，开头 'ID3' 或 0xFF 帧同步
        content = resp.content
        is_wav = content[:4] == b"RIFF" and content[8:12] == b"WAVE"
        is_mp3 = content[:3] == b"ID3" or (len(content) > 2 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0)
        self.check(
            is_wav or is_mp3,
            "文件格式是标准 WAV/MP3",
            "WAV" if is_wav else ("MP3" if is_mp3 else f"未知头: {content[:4]!r}"),
        )

        content_type = resp.headers.get("content-type", "")
        print(f"         Content-Type = {content_type}")

    # ---------------- 步骤 7：列表 / 统计 ----------------
    def step_list_and_stats(self) -> None:
        print("\n[步骤 7] 列表与统计")
        resp = httpx.get(f"{self.base_url}/api/diary?page=1&page_size=10", timeout=15)
        data = resp.json()
        self.check(resp.status_code == 200, "列表接口 200")
        self.check(data.get("total", 0) >= 1, "总数 >= 1", f"total={data.get('total')}")

        resp = httpx.get(f"{self.base_url}/api/diary/stats", timeout=15)
        stats = resp.json()
        self.check(resp.status_code == 200, "统计接口 200")
        print(f"         日记数={stats.get('diary_count')} 配乐数={stats.get('ready_track_count')}")
        print(f"         情绪分布={stats.get('emotion_distribution')}")

    # ---------------- 步骤 8：异常分支 ----------------
    def step_error_cases(self) -> None:
        print("\n[步骤 8] 异常分支测试")
        # 内容太短 → 应被校验拦下，返回 422
        resp = httpx.post(f"{self.base_url}/api/diary", json={"content": "好"}, timeout=20)
        self.check(resp.status_code == 422, "超短内容被拒（422）", f"实际 {resp.status_code}")

        # 不存在的 ID → 404
        resp = httpx.get(f"{self.base_url}/api/diary/99999999", timeout=15)
        self.check(resp.status_code == 404, "不存在的日记返回 404", f"实际 {resp.status_code}")

    # ---------------- 步骤 9：置顶功能 ----------------
    def step_pin(self) -> None:
        print("\n[步骤 9] 置顶功能 PATCH /api/diary/{id}/pin")
        if not self.created_diary_ids:
            self.check(False, "没有可用的测试日记，跳过置顶检查")
            return

        target_id = self.created_diary_ids[0]

        # 置顶
        resp = httpx.patch(
            f"{self.base_url}/api/diary/{target_id}/pin",
            json={"pinned": True},
            timeout=15,
        )
        ok = resp.status_code == 200 and resp.json().get("pinned") is True
        self.check(ok, "置顶成功且返回 pinned=true", f"实际 {resp.status_code}")

        # 列表第一位必须是它
        items = httpx.get(f"{self.base_url}/api/diary?page=1&page_size=10", timeout=15).json()["items"]
        self.check(bool(items) and items[0]["id"] == target_id, "置顶日记排在列表第一位")

        # 取消置顶
        resp = httpx.patch(
            f"{self.base_url}/api/diary/{target_id}/pin",
            json={"pinned": False},
            timeout=15,
        )
        self.check(resp.status_code == 200 and resp.json().get("pinned") is False, "取消置顶成功")

    # ---------------- 步骤 10：官方素材曲库 ----------------
    def step_library(self) -> None:
        print("\n[步骤 10] 官方素材曲库检查")
        resp = httpx.get(f"{self.base_url}/api/health", timeout=15)
        data = resp.json()
        count = data.get("music_library_count", 0)
        first = data.get("music_library_first", False)
        self.check("music_library_count" in data, "健康检查暴露曲库字段")
        if count:
            print(f"         曲库现有 {count} 首，曲库优先={first}")
        else:
            print("         曲库为空（storage/library/ 没有素材文件）→ 配乐将走本地合成，属正常现象")

    # ---------------- 清理 ----------------
    def cleanup(self) -> None:
        print("\n[清理] 删除本次测试产生的数据")
        for diary_id in self.created_diary_ids:
            resp = httpx.delete(f"{self.base_url}/api/diary/{diary_id}", timeout=15)
            status = "成功" if resp.status_code == 200 else f"失败({resp.status_code})"
            print(f"         删除 diary_id={diary_id} → {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="旋律情绪速写 —— 端到端冒烟测试")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="后端服务地址")
    parser.add_argument("--keep", action="store_true", help="保留测试数据不清删")
    args = parser.parse_args()

    print("=" * 64)
    print("  旋律情绪速写 —— 端到端冒烟测试")
    print(f"  目标地址：{args.url}")
    print("=" * 64)

    tester = SmokeTester(args.url)

    # 先确认服务活着，否则后面全是无效的
    try:
        if not tester.step_health():
            print("\n❌ 服务未就绪，请先在另一个终端执行：python run.py")
            return 1
    except httpx.ConnectError:
        print(f"\n❌ 连不上 {args.url}，请先启动服务：python run.py")
        return 1

    tester.step_cors()
    tester.step_emotions()

    # 逐个跑样本日记
    for case in SAMPLE_DIARIES:
        diary_id = tester.step_create_diary(case)
        if diary_id is None:
            continue
        tester.created_diary_ids.append(diary_id)

        if case["payload"].get("auto_generate_music"):
            track = tester.step_wait_music(diary_id)
            if track:
                tester.step_verify_audio(track)

    tester.step_list_and_stats()
    tester.step_error_cases()
    tester.step_pin()
    tester.step_library()

    if not args.keep:
        tester.cleanup()

    print("\n" + "=" * 64)
    print(f"  测试结果：通过 {tester.passed} 项，失败 {tester.failed} 项")
    print("=" * 64)
    return 0 if tester.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
