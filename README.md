# 旋律情绪速写 · AI 音乐情绪日记本

> 腾讯音乐黑客松 · 创新音乐产品赛道
> 写一段日记 → 大模型读出你的情绪 → 为这份情绪生成一段专属背景音乐

**技术栈**：Python 3.10+ / FastAPI / SQLAlchemy / SQLite + 原生 HTML·CSS·JS（无前端框架）
**实测环境**：Python 3.13.14 + Windows 11，47 条 pytest 用例 + 36 项端到端冒烟检查全部通过

---

## 一、项目目录结构

```
emotion-music-diary/
├── backend/                      # 后端（FastAPI）
│   ├── routers/                  # 接口层：只负责收发 HTTP
│   │   ├── health.py             #   健康检查 + 情绪字典元信息
│   │   ├── diary.py              #   日记的增查改删 + 统计
│   │   └── music.py              #   音乐生成 / 轮询 / 查询
│   ├── services/                 # 业务层：真正干活的地方
│   │   ├── emotion_catalog.py    #   ★ 情绪字典 + 情绪→音乐参数映射表（核心资产）
│   │   ├── llm_emotion.py        #   大模型情绪解析（含本地规则降级）
│   │   ├── local_composer.py     #   ★ 离线算法作曲引擎（纯标准库，无 numpy）
│   │   └── music_service.py      #   音乐生成编排（第三方 API / 本地 双通道）
│   ├── config.py                 # 配置中心（唯一读取 .env 的地方）
│   ├── database.py               # 数据库连接 / 会话 / 自动建表
│   ├── models.py                 # ORM 表结构（diaries、music_tracks）
│   ├── schemas.py                # 请求/响应数据结构 + 转换器
│   ├── crud.py                   # 数据库增删改查
│   └── main.py                   # 应用入口（CORS、静态托管、路由注册）
├── backend/
│   └── data/
│       └── song_emotion_map.json  # 官方歌单 151 首 → 17 种情绪的映射表
├── frontend/                     # 前端（原生三件套，零构建）
│   ├── index.html                #   页面骨架
│   ├── styles.css                #   样式
│   └── app.js                    #   交互逻辑（fetch 调接口 + 轮询播放）
├── scripts/
│   ├── init_db.py                # 数据库初始化/重建工具
│   ├── smoke_test.py             # 端到端冒烟测试（对着运行中的服务打真实请求）
│   ├── seed_demo.py              # 演示种子数据（30 天仿真日记，独立库一键装载/卸载）
│   ├── record_demo.py            # 一键生成参赛演示视频（1080×1920 竖屏 MP4+字幕）
│   ├── import_library.py         # 官方歌单一键导入（自动打情绪标签、跳过加密格式）
│   ├── build_free_library.py     # 公版免费曲库构建（CC-BY 4.0，自动选曲下载）
│   └── trim_library.py           # 曲库裁剪：整首歌 → 35 秒精华片段（185MB→9MB）
├── tests/                        # pytest 单元测试
│   ├── conftest.py               #   测试夹具（隔离数据库、强制本地通道）
│   ├── test_emotion.py           #   情绪分析测试
│   ├── test_composer.py          #   音乐合成引擎测试
│   ├── test_api.py               #   HTTP 接口端到端测试
│   ├── test_pin_library.py       #   置顶/删除/曲库优先测试
│   └── test_import_library.py    #   素材导入工具与映射表测试
├── docs/                         # 交付文档
│   ├── 01_启动与部署.md
│   ├── 02_测试用例.md
│   ├── 03_黑客松演示说明.md
│   ├── 04_打包与部署.md
│   ├── 05_参赛提交材料.md
│   └── 06_素材曲库使用说明.md
├── storage/                      # 运行期数据（自动创建，不进 Git）
│   ├── audio/                    #   本地合成的 wav 文件
│   ├── library/                  #   官方素材曲库（放 mp3/m4a/wav/flac/ogg）
│   └── diary.db                  #   SQLite 数据库
├── .env.example                  # 配置模板（复制成 .env 后填密钥）
├── .gitignore
├── pytest.ini
├── requirements.txt
├── run.py                        # ★ 一键启动脚本
└── 启动.bat                       # Windows 双击启动
```

**分层为什么这样切**（给不熟悉后端的同学）：

```
浏览器 ──HTTP──> routers（接请求/返响应）
                     │
                     ▼
                 services（业务逻辑：调 LLM、作曲）
                     │
                     ▼
                  crud（数据库读写） ──> models（表结构） ──> SQLite
```

每一层只依赖下一层，换任何一层（比如把 SQLite 换 MySQL、把音乐引擎换别家 API）
都只需要改对应目录，其它目录一行都不用动。

---

## 二、五分钟跑起来

### 1. 安装依赖

```bash
cd emotion-music-diary
python -m venv .venv                 # 建议建虚拟环境，避免污染本机 Python
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置密钥（**可跳过**）

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

然后用记事本打开 `.env`，填写你的大模型 Key：

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的真实密钥
LLM_MODEL=deepseek-chat
```

> **没有 Key 也能完整演示**：`LLM_API_KEY` 留空会自动降级为本地关键词算法，
> 音乐则走内置的算法作曲引擎。断网、没钱、限流都不影响演示。

### 3. 启动

```bash
python run.py
```

看到下面的输出就成功了，浏览器会自动打开：

```
==================================================================
  Melody Mood Sketch  v1.0.0
==================================================================
  数据库文件 : ...\storage\diary.db
  音频输出   : ...\storage\audio
  大模型     : 已配置 deepseek-chat
  音乐通道   : auto（第三方 Key: 未配）
------------------------------------------------------------------
  打开页面   : http://127.0.0.1:8000
  接口文档   : http://127.0.0.1:8000/docs
==================================================================
```

Windows 用户也可以直接**双击 `启动.bat`**。

### 4. 手动验证

- 浏览器打开 `http://127.0.0.1:8000`
- 点左上角任一个情绪示例（会自动填入文本）→ 点「生成我的音乐日记」
- 右侧先看情绪卡片，音乐生成完自动开始播放（曲库命中约 1~3 秒）
- 另一个终端跑：`python scripts/smoke_test.py`（端到端全流程自检）

### 4.5 灌入演示数据（答辩/截图前推荐）

空账户下"情绪轨迹"的日历和折线图是空的，灌入 30 天仿真数据立刻就有完整曲线：

```bash
python scripts/seed_demo.py --build    # 生成 30 天演示数据（写进独立演示库，不动你的真实日记）
python scripts/seed_demo.py --load     # 装载（重启服务后生效）
python scripts/seed_demo.py --unload   # 看完切回自己的真实数据
```

### 4.6 重新生成演示视频（可选）

`docs/demo_video.mp4` 是已录好的参赛演示视频（1080×1920 竖屏 + 中文字幕）。
想改演示内容后重录：

```bash
pip install -r requirements-dev.txt    # 录屏工具（Playwright），只开发机需要
python scripts/record_demo.py          # 自动驱动浏览器走完流程并录像
```

> 用的是系统自带的 Edge 浏览器（`channel="msedge"`），无需下载浏览器内核；
> 转码用 ffmpeg（Windows: `winget install ffmpeg`）。

### 5. 接入真实音乐曲库（可选，让配乐变成真实歌曲）

曲库有两条合法路线，可同时使用（详见 [docs/06_素材曲库使用说明.md](docs/06_素材曲库使用说明.md)）：

**路线一 · 公版免费曲库（推荐，一条命令建好）**

```bash
python scripts/build_free_library.py --plan     # 先看选曲方案
python scripts/build_free_library.py            # 从 incompetech 下载 34 首（17 种情绪 × 2）
```

音乐来自 Kevin MacLeod (incompetech.com)，CC-BY 4.0 许可（免费商用，需署名——
脚本会生成 `storage/library/ATTRIBUTION.txt` 署名清单，页面页脚也已标注）。
按官方情绪标签 + BPM + 描述自动匹配 17 种情绪，全器乐无人声，适合当日记背景乐。

**路线二 · 大赛官方歌单素材**

```bash
python scripts/import_library.py --dry-run      # 预览（自动打情绪标签 + 跳过加密格式）
python scripts/import_library.py                # 导入
```

⚠️ QQ 音乐 VIP 下载的 `.mgg` 是 DRM 加密格式，浏览器无法播放，需**单曲购买后重新下载**
获取标准 mp3（官方指定办法）；Demo 只需覆盖 17 种情绪各 1 首即可，不必全买 151 首。
映射表在 `backend/data/song_emotion_map.json`。

---

## 三、接口一览

服务启动后有自动生成的交互式文档：**http://127.0.0.1:8000/docs**（可直接点按钮调试）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查，顺便返回当前用了哪条技术路线 |
| GET | `/api/meta/emotions` | 16 种情绪的代号/中文名/配色 |
| POST | `/api/diary` | **写日记**（→ 情绪分析 → 可选自动配乐），返回 201 |
| GET | `/api/diary` | 日记列表，支持 `page`/`page_size`/`keyword`/`emotion` |
| GET | `/api/diary/stats` | 统计：日记数、配乐数、情绪分布、平均效价 |
| GET | `/api/diary/{id}` | 单篇详情（含情绪 + 最新音乐） |
| PATCH | `/api/diary/{id}` | 改标题/正文（会自动重跑情绪分析） |
| DELETE | `/api/diary/{id}` | 删除（级联删除关联的音乐记录） |
| POST | `/api/music/generate` | 生成/重新生成配乐，返回 202 + track_id |
| GET | `/api/music/track/{id}` | 轮询生成状态（pending→processing→ready/failed） |
| GET | `/api/music/latest/{diary_id}` | 取某篇日记最近一次配乐 |
| GET | `/media/xxx.wav` | 生成的音频文件（浏览器可直接播放/下载） |
| GET | `/` | 前端页面（同源部署，无跨域问题） |

写日记请求示例：

```bash
curl -X POST http://127.0.0.1:8000/api/diary \
  -H "Content-Type: application/json" \
  -d '{"content":"今天终于搞定了困扰一周的 bug，走在回宿舍的路上，风很轻。","auto_generate_music":true}'
```

---

## 四、核心设计：它到底"创新"在哪

### 1. 情绪不是分类，是「可作曲的参数」

常见的情绪分析项目到"输出一个标签"就结束了。本项目多做一步：
把 16 种情绪各自定义为一组**音乐参数**（`emotion_catalog.py`）：

| 情绪 | BPM | 调式 | 和弦走向 | 主音色 | 混响 |
|------|-----|------|----------|--------|------|
| 喜悦 | 116 | 大调五声 | Imaj7–IV–V7 | 钢琴 | 0.20 |
| 孤独 | 58 | 小调五声 | Im9–Vm7–IIImaj7 | 长笛 | 0.52 |
| 愤怒 | 136 | 自然小调 | Im–VII–VI–Vm | 风琴 | 0.18 |

同时输出心理学的三个连续维度：**效价（valence） / 唤醒度（arousal） / 强度（intensity）**，
前端把它们画成三条可视化进度条，让"情绪"这件事可度量、可比较、可追踪。

### 2. 双通道音乐生成（断网也能演示）

```
         ┌─────────────────────────────────────┐
情绪参数 → │ provider = auto                     │
         │   ① 有第三方 Key → 调 API            │
         │   ② 超时/失败/没配 → 本地算法作曲    │
         └─────────────────────────────────────┘
```

其中**本地合成引擎是本项目的技术护城河**：纯 Python 标准库（`wave`/`math`/`array`）
实现谐波叠加 + ADSR 包络 + 梳状滤波器混响 + 随机游走旋律生成，
**零依赖、零网络、零成本、3~8 秒出 20 秒的立体声 WAV**。
这在黑客松现场意味着：评委 WiFi 挂了、你的 API 余额没了，产品照样能跑。

### 3. 大模型降级链路

LLM 超时 / 401 / 余额不足 / 返回非法 JSON → 自动切到本地关键词加权算法，
且在响应里用 `source` 字段明确标注来源（`llm` / `local_rule`），前端会显示徽章。
**诚实标注，而不是偷偷假装是 AI 分析的。**

### 4. 异步任务 + 轮询

音乐生成是慢活，所以设计成 `POST 提交 → 立刻拿 track_id → 前端每 1.5s 轮询状态`，
服务端用 `BackgroundTasks` + `asyncio.to_thread` 执行（CPU 密集任务丢线程池，
不阻塞事件循环）。这是处理耗时任务的标准工程做法，而不是让浏览器干等超时。

---

## 五、常见问题排查

| 现象 | 原因 | 解决办法 |
|------|------|----------|
| `ModuleNotFoundError: No module named 'fastapi'` | 依赖没装 / 装到了别的 Python | `pip install -r requirements.txt`，确认用的哪个 python |
| 前端页面全白，控制台报 `Failed to fetch` | 后端没启动 | 先 `python run.py`，刷新页面 |
| 页面右上角显示"后端未启动" | 端口不对 | 检查 `.env` 里 `APP_PORT` 是否是 8000 |
| 情绪一直显示"本地规则分析" | 没配 Key 或 Key 是占位符 | 在 `.env` 里填真实 `LLM_API_KEY` 然后**重启服务** |
| 改了 `.env` 没生效 | 配置有缓存 | 必须重启服务，光刷新浏览器没用 |
| 音乐一直是"正在作曲" | 云端 API 慢 | 演示时建议 `MUSIC_PROVIDER=local`，秒出 |
| 端口被占用 `Address already in use` | 8000 被别的程序占了 | `.env` 里改成 `APP_PORT=8001` |
| 浏览器说没有声音 | 系统音量 / 自动播放被拦 | 手动点播放器的 ▶ 按钮 |

---

## 六、更多文档

- [启动与部署说明（含免费云部署、Docker）](docs/01_启动与部署.md)
- [测试用例与验收标准](docs/02_测试用例.md)
- [黑客松答辩演示说明（PPT 素材）](docs/03_黑客松答辩演示文档.md)
- [参赛提交材料（作品介绍 / 功能 / 创新点）](docs/05_参赛提交材料.md)
- [官方素材曲库使用说明（让配乐更好听）](docs/06_素材曲库使用说明.md)
- [打包与部署方案（Render / Railway / PyInstaller）](docs/04_打包与部署.md)
