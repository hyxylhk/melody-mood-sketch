/* =============================================================================
   frontend/app.js —— 页面交互逻辑（原生 JavaScript，不依赖任何框架）
   =============================================================================
   给不懂前端的同学，先记住三个概念：

   1) DOM
      浏览器把 HTML 解析成一棵"节点树"，JS 通过 document.getElementById('xxx')
      拿到某个节点（元素），然后改它的文字、样式、属性。

   2) fetch —— 发 HTTP 请求的原生 API
      const resp = await fetch('/api/diary', {method:'POST', body: JSON.stringify({...})})
      const data = await resp.json()
      这就相当于你在 Postman 里点一下发送。

   3) async / await
      网络请求要花时间。用 async 标记函数、await 标记耗时调用，
      代码写起来就像同步一样直观，但底层不会卡住浏览器界面。

   本文件的整体流程：
      页面加载 → 检查后端健康 → 拉历史列表
      用户提交 → POST /api/diary → 显示情绪 → 轮询音乐状态 → ready 后播放
   ============================================================================= */


/* ==========================================================================
   一、全局配置与工具函数
   ========================================================================== */

/**
 * 后端接口地址。
 * 默认留空 = 同源地址（因为 index.html 就是后端返回的，域名端口完全一样，
 * 这时候 fetch('/api/xxx') 天然不会有跨域问题）。
 * 如果你想把前端单独部署到别的地方，在 URL 后面加 ?api=http://你的后端:8000 即可，
 * 例如： http://127.0.0.1:5500/index.html?api=http://127.0.0.1:8000
 */
const API_BASE = (function resolveApiBase() {
    if (typeof window.API_BASE === 'string' && window.API_BASE) return window.API_BASE;
    const fromQuery = new URLSearchParams(location.search).get('api');
    if (fromQuery) return fromQuery.replace(/\/$/, '');
    const fromStorage = localStorage.getItem('apiBase');
    return fromStorage ? fromStorage : '';
})();

/** $ 是 idiomatic jQuery 风格的小工具：按 id 取元素 */
const $ = (id) => document.getElementById(id);

/** 显示右下角轻提示气泡（自动消失） */
function toast(message, type = 'ok', duration = 2600) {
    const el = $('toast');
    el.textContent = message;
    el.className = 'toast toast-' + type;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add('hidden'), duration);
}

/**
 * 把"浏览器原生的网络错误"翻译成人话。
 *
 * 场景：后端没启动 / 电脑休眠后进程没了 / 网络断了，
 * fetch 会直接抛 TypeError: Failed to fetch。
 * 直接把这句英文丢给用户，他只会觉得"操作失效"，根本不知道是服务没开。
 * 这里统一换成一句话说清楚"是什么 + 怎么办"，并把左上角状态灯打红。
 */
function networkErrorText(err) {
    markBackendDown();
    return '连不上服务器（' + err.message + '）。请确认后端服务还在运行，然后刷新页面。';
}

/** 把左上角的状态灯切成"已断开"，让用户一眼看出是服务挂了，不是功能坏了 */
function markBackendDown() {
    const dot = $('healthDot');
    const text = $('healthText');
    if (dot && text) {
        dot.className = 'dot dot-error';
        text.textContent = '后端连接中断（服务可能已关闭，请重新运行 python run.py）';
    }
}

/** 封装 GET 请求，统一处理网络异常并解析 JSON */
async function apiGet(path) {
    try {
        const resp = await fetch(API_BASE + path, { method: 'GET' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status + ' ' + resp.statusText);
        return { ok: true, data: await resp.json() };
    } catch (err) {
        return { ok: false, error: networkErrorText(err) };
    }
}

/** 封装 POST 请求（请求体统一用 JSON 格式） */
async function apiPost(path, body) {
    try {
        const resp = await fetch(API_BASE + path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        // 即使状态码不是 2xx，后端也会返回 JSON 错误详情，尽量解析出来给提示
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            const detail = data.detail ? JSON.stringify(data.detail) : ('HTTP ' + resp.status);
            return { ok: false, error: detail };
        }
        return { ok: true, data };
    } catch (err) {
        return { ok: false, error: networkErrorText(err) };
    }
}

/** 封装 PATCH 请求（局部更新，比如置顶） */
async function apiPatch(path, body) {
    try {
        const resp = await fetch(API_BASE + path, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            const detail = data.detail ? JSON.stringify(data.detail) : ('HTTP ' + resp.status);
            return { ok: false, error: detail };
        }
        return { ok: true, data };
    } catch (err) {
        return { ok: false, error: networkErrorText(err) };
    }
}

/** 封装 DELETE 请求 */
async function apiDelete(path) {
    try {
        const resp = await fetch(API_BASE + path, { method: 'DELETE' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            const detail = data.detail ? JSON.stringify(data.detail) : ('HTTP ' + resp.status);
            return { ok: false, error: detail };
        }
        return { ok: true, data };
    } catch (err) {
        return { ok: false, error: networkErrorText(err) };
    }
}

/** 把 ISO 时间字符串格式化成 "10-02 13:20" 这种本地习惯写法 */
function formatTime(isoString) {
    if (!isoString) return '';
    const d = new Date(isoString);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}


/* ==========================================================================
   二、页面状态
   ========================================================================== */
const state = {
    currentDiaryId: null,   // 当前正在展示的日记 ID
    currentTrackId: null,   // 当前正在轮询的音乐任务 ID
    polling: false,         // 是否正在轮询（防止重复触发）
};


/* ==========================================================================
   三、启动检查 & 初始化
   ========================================================================== */

/** 调 /api/health 看看后端活没活着，顺带展示当前技术路线 */
async function checkHealth() {
    const res = await apiGet('/api/health');
    const dot = $('healthDot');
    const text = $('healthText');

    if (!res.ok) {
        dot.className = 'dot dot-error';
        text.textContent = '后端未启动（请先运行 python run.py）';
        return;
    }

    const d = res.data;
    dot.className = 'dot dot-ok';
    // llm_enabled 为 true 说明 .env 里真的配了可用的 Key
    text.textContent = d.llm_enabled
        ? `后端已连接 · LLM ${d.llm_model}`
        : '后端已连接 · 未配 LLM（本地规则分析）';

    const tag = $('providerTag');
    tag.classList.remove('hidden');
    // 曲库里放了几首素材，会在通道文字后面带出来，方便确认素材已就位
    const libText = d.music_library_count ? ` · 素材曲库 ${d.music_library_count} 首` : '';
    tag.textContent = d.music_provider_effective === 'local'
        ? '音乐通道：本地合成' + libText
        : '音乐通道：第三方 API' + libText;
}

/** 页面所有事件绑定都集中在这里 */
function bindEvents() {
    // ---- 输入框字数统计 ----
    $('diaryContent').addEventListener('input', () => {
        $('charCount').textContent = $('diaryContent').value.length + ' / 5000';
    });

    // Ctrl / Cmd + Enter 快捷提交
    $('diaryContent').addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submitDiary();
    });

    // ---- 示例文本按钮 ----
    document.querySelectorAll('.chip[data-text]').forEach((btn) => {
        btn.addEventListener('click', () => {
            $('diaryContent').value = btn.dataset.text;
            $('diaryContent').dispatchEvent(new Event('input'));
            $('diaryContent').focus();
        });
    });

    // ---- 主按钮 & 音乐相关按钮 ----
    $('submitBtn').addEventListener('click', submitDiary);
    $('regenBtn').addEventListener('click', regenerateMusic);
    $('retryBtn').addEventListener('click', regenerateMusic);
    $('refreshBtn').addEventListener('click', () => {
        loadHistory();
        loadStats();
        loadTrajectory();
    });
}


/* ==========================================================================
   四、核心流程 1：提交日记
   ========================================================================== */
async function submitDiary() {
    const content = $('diaryContent').value.trim();
    const title = $('diaryTitle').value.trim();
    const autoMusic = $('autoMusic').checked;

    // ---- 前端侧校验（后端还会再校验一次，这里是省一次网络往返）----
    if (content.length < 2) {
        toast('至少写 2 个字吧 :)', 'error');
        $('diaryContent').focus();
        return;
    }

    // 按钮进入「忙碌」状态：禁用 + 改文案，防止用户连点造成重复提交
    const btn = $('submitBtn');
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '正在感受你的文字…';

    const res = await apiPost('/api/diary', {
        content,
        title: title || null,
        auto_generate_music: autoMusic,
    });

    btn.disabled = false;
    btn.querySelector('.btn-text').textContent = '生成我的音乐日记';

    if (!res.ok) {
        toast('保存失败：' + res.error, 'error', 5000);
        return;
    }

    const diary = res.data;
    state.currentDiaryId = diary.id;

    renderEmotion(diary);          // 情绪是同步返回的，立刻就能画出来
    resetMusicUI();

    if (diary.music && diary.music.id) {
        // 后端已经帮我们起好后台任务了，开始轮询它
        startPolling(diary.music, autoMusic);
    } else {
        $('progressText').textContent = '这次没有自动生成音乐，点「换一首」试试';
        $('musicProgress').classList.remove('hidden');
        $('regenBtn').classList.remove('hidden');
    }

    // 写完清空输入框，并刷新左下角的时间线
    $('diaryContent').value = '';
    $('diaryTitle').value = '';
    $('diaryContent').dispatchEvent(new Event('input'));
    loadHistory();
    loadStats();
    loadTrajectory();
}


/* ==========================================================================
   五、渲染情绪卡片
   ========================================================================== */
function renderEmotion(diary) {
    $('resultEmpty').classList.add('hidden');
    $('resultCard').classList.remove('hidden');

    const emo = diary.emotion;
    if (!emo) return;

    const card = $('emotionCard');
    // 把情绪主题色注入成 CSS 变量，卡片背景/左边框会自动跟着变
    card.style.setProperty('--emotion-color', hexToRgba(emo.primary_color, 0.22));

    $('emotionIcon').textContent = emo.primary_icon;
    $('emotionLabel').textContent = emo.primary_label;
    $('emotionDesc').textContent = emo.description;

    // ---- 来源徽章：区分是大模型还是本地算法分析的 ----
    const badge = $('sourceBadge');
    if (emo.source === 'llm') {
        badge.textContent = '大模型分析';
        badge.className = 'badge badge-llm';
    } else {
        badge.textContent = '本地规则分析';
        badge.className = 'badge badge-local';
    }

    // ---- 次要情绪标签 ----
    const tagBox = $('emotionTags');
    tagBox.innerHTML = '';
    if (emo.tags && emo.tags.length) {
        emo.tags.forEach((t) => {
            const span = document.createElement('span');
            span.className = 'emotion-tag';
            span.textContent = t.icon + ' ' + t.label;
            span.style.borderColor = hexToRgba(t.color, 0.5);
            tagBox.appendChild(span);
        });
    }

    // ---- 三个维度指标条 ----
    // 效价是 -1~1，要映射到 0~100 的进度条宽度
    setMetric('valence', (emo.valence + 1) / 2, emo.valence.toFixed(2));
    setMetric('arousal', emo.arousal, emo.arousal.toFixed(2));
    setMetric('intensity', emo.intensity, emo.intensity.toFixed(2));

    $('emotionSummary').textContent = emo.summary || '—';

    // music_prompt 存在日记对象上（这里从后端返回体里拿不到就留空）
    $('musicPrompt').textContent = (diary.music && diary.music.prompt) || '—';
}

/** 设置单个进度条：0~1 的比例值 + 显示用的文本 */
function setMetric(name, ratio, label) {
    const pct = Math.max(0, Math.min(1, ratio)) * 100;
    $(name + 'Bar').style.width = pct.toFixed(1) + '%';
    $(name + 'Text').textContent = label;
}

/** #RRGGBB → rgba(r,g,b,a)，CSS 里做半透明要用这个格式 */
function hexToRgba(hex, alpha) {
    const clean = (hex || '#ffffff').replace('#', '');
    const r = parseInt(clean.slice(0, 2), 16);
    const g = parseInt(clean.slice(2, 4), 16);
    const b = parseInt(clean.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}


/* ==========================================================================
   六、核心流程 2：轮询音乐生成状态
   ========================================================================== */

/** 把音乐区域恢复到初始状态（准备迎接一次新的生成） */
function resetMusicUI() {
    state.polling = false;
    state.currentTrackId = null;
    $('audioPlayer').classList.add('hidden');
    $('audioPlayer').pause();
    $('musicError').classList.add('hidden');
    $('regenBtn').classList.add('hidden');
    $('musicProgress').classList.add('hidden');
    $('progressFill').style.width = '0%';
    $('musicMeta').textContent = '—';
}

/**
 * 轮询直到任务结束。
 *
 * 为什么是轮询而不是 WebSocket？
 *   WebSocket 实时性好但后端实现复杂得多；对"每隔几秒刷一次状态"这种需求，
 *   轮询足够用且代码简单可靠 —— 工程上是更合适的选择。
 */
function startPolling(track, enabled) {
    state.currentTrackId = track.id;

    if (!enabled) {
        $('musicMeta').textContent = '本次未生成配乐';
        $('regenBtn').classList.remove('hidden');
        return;
    }

    if (track.status === 'ready') {
        onMusicReady(track);
        return;
    }

    state.polling = true;
    $('musicProgress').classList.remove('hidden');
    $('progressText').textContent = '正在为你作曲…（本地合成一般 3~8 秒）';

    let tries = 0;
    const MAX_TRIES = 100;          // 最多等 100 × 1.5s = 150 秒
    const INTERVAL = 1500;          // 每 1.5 秒问一次后端

    const timer = setInterval(async () => {
        tries += 1;
        // 进度条走到 92% 就不再涨，剩下的等成功后再拉满，避免出现"假的 100%"
        const fake = Math.min(92, tries * 4);
        $('progressFill').style.width = fake + '%';

        const res = await apiGet('/api/music/track/' + state.currentTrackId);
        if (!res.ok) {
            clearInterval(timer);
            state.polling = false;
            showMusicError('轮询失败：' + res.error);
            return;
        }

        const t = res.data;

        if (t.status === 'ready') {
            clearInterval(timer);
            state.polling = false;
            $('progressFill').style.width = '100%';
            onMusicReady(t);
        } else if (t.status === 'failed') {
            clearInterval(timer);
            state.polling = false;
            showMusicError(t.error_message || '生成失败，原因未知');
        } else if (tries >= MAX_TRIES) {
            clearInterval(timer);
            state.polling = false;
            showMusicError('等待超时，音乐可能还在生成中，请稍后查询');
        }
    }, INTERVAL);
}

/** 生成成功：把地址塞给 <audio> 播放器 */
function onMusicReady(track) {
    $('musicProgress').classList.add('hidden');

    // 只有本地生成的音频地址是相对路径（/media/xxx.wav）；
    // 如果前端是单独部署的（配置了 API_BASE），就要拼上后端地址才能访问到。
    // 第三方 API 返回的是完整 https:// 地址，startsWith('/') 为 false，原样使用。
    const audioUrl = track.audio_url.startsWith('/')
        ? API_BASE + track.audio_url
        : track.audio_url;

    const player = $('audioPlayer');
    player.src = audioUrl;
    player.classList.remove('hidden');

    // play() 返回 Promise，浏览器可能因为"未经用户交互禁止自动播放"而拒绝，
    // 这种情况静默忽略即可（用户点了播放按钮照样能播）。
    player.play().catch(() => { });

    const meta = track.meta || {};
    // 三种来源：library=官方素材曲库（挑的现成好歌） / local=本地算法合成 / httpapi=第三方 API
    const providerText = track.provider === 'library' ? '官方素材曲库'
        : track.provider === 'local' ? '本地算法合成' : '第三方音乐 API';
    const titleText = meta.title ? ` · ♪ ${meta.title}` : '';
    const bpmText = meta.tempo_bpm ? ` · ${meta.tempo_bpm} BPM` : '';
    const keyText = meta.root_note ? ` · ${meta.root_note}` : '';
    $('musicMeta').textContent = `${providerText}${titleText}${bpmText}${keyText}`;

    $('regenBtn').classList.remove('hidden');
    toast('音乐生成好了，听听看 🎵');
    loadStats();
    loadTrajectory();
}

function showMusicError(message) {
    $('musicProgress').classList.add('hidden');
    $('musicError').classList.remove('hidden');
    $('musicErrorText').textContent = message;
    $('regenBtn').classList.remove('hidden');
}


/* ==========================================================================
   七、重新生成 / 换一首
   ========================================================================== */
async function regenerateMusic() {
    if (!state.currentDiaryId) {
        toast('请先写一篇日记', 'error');
        return;
    }

    resetMusicUI();
    $('musicProgress').classList.remove('hidden');
    $('progressText').textContent = '正在提交生成请求…';

    const res = await apiPost('/api/music/generate', {
        diary_id: state.currentDiaryId,
        force_local: true,           // 演示时使用零成本的本地合成，速度可控
    });

    if (!res.ok) {
        showMusicError('提交失败：' + res.error);
        return;
    }
    startPolling(res.data, true);
}


/* ==========================================================================
   八、历史列表 & 统计
   ========================================================================== */
async function loadHistory() {
    const res = await apiGet('/api/diary?page=1&page_size=30');
    const box = $('historyList');

    if (!res.ok) {
        box.innerHTML = '<p class="empty-hint">加载失败：' + res.error + '</p>';
        return;
    }

    const items = res.data.items || [];
    if (!items.length) {
        box.innerHTML = '<p class="empty-hint">还没有任何记录，写下第一篇吧。</p>';
        return;
    }

    box.innerHTML = '';
    items.forEach((item) => {
        const div = document.createElement('div');
        // is-pinned 类会让卡片带金色描边，并在列表里天然排到最前（后端已排好序）
        div.className = 'history-item' + (item.pinned ? ' is-pinned' : '');
        div.style.setProperty('--item-color', item.emotion_color);
        div.innerHTML = `
            <div class="card-actions">
                <button class="card-action-btn pin-btn"
                        title="${item.pinned ? '取消置顶' : '置顶'}">${item.pinned ? '📍' : '📌'}</button>
                <button class="card-action-btn del-btn" title="删除">🗑</button>
            </div>
            <div class="history-item-body">
                <div class="history-item-top">
                    <div class="history-item-left">
                        <span class="history-item-icon">${item.emotion_icon}</span>
                        <span class="history-item-label">${item.emotion_label}</span>
                    </div>
                    <span class="history-item-date">${formatTime(item.created_at)}</span>
                </div>
                <p class="history-item-excerpt">${escapeHtml(item.excerpt)}</p>
                <div class="history-item-foot">
                    <span>效价 ${item.valence.toFixed(2)}</span>
                    ${item.has_music ? '<span class="play-badge">🎵 已有配乐</span>' : '<span>暂无配乐</span>'}
                </div>
            </div>
            ${item.pinned ? '<span class="pin-badge">置顶</span>' : ''}
        `;
        // 点击某条历史 → 加载它的详情到右侧面板
        div.addEventListener('click', () => loadDiaryDetail(item.id));

        // 注意 stopPropagation()：按钮的点击不能冒泡到卡片本身，
        // 否则点一下"删除"会顺带打开这篇日记的详情，体验很怪。
        div.querySelector('.pin-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            togglePin(item);
        });
        div.querySelector('.del-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            shatterAndDelete(item, div);
        });

        box.appendChild(div);
    });
}

/* ---------------------------------------------------------------------------
   置顶 / 取消置顶：改完只需刷新列表，后端会把置顶的排到最前
--------------------------------------------------------------------------- */
async function togglePin(item) {
    const res = await apiPatch(`/api/diary/${item.id}/pin`, { pinned: !item.pinned });
    if (!res.ok) {
        toast('操作失败：' + res.error, 'error');
        return;
    }
    toast(item.pinned ? '已取消置顶' : '已置顶 📌');
    loadHistory();
}

/* ---------------------------------------------------------------------------
   删除 + 「粉碎」动效
   流程：卡片瞬间隐身 → 在原位生成 16 块碎片 → 各自旋转飞散渐隐 →
        后台调 DELETE 接口 → 刷新列表
   视觉与数据互不等待：动画在前台跑，删除请求同时发出去。
--------------------------------------------------------------------------- */
async function shatterAndDelete(item, cardEl) {
    // dataset.deleting 是个"防重入锁"：连点删除按钮时只触发一次
    if (cardEl.dataset.deleting) return;
    cardEl.dataset.deleting = '1';

    playShatter(cardEl);                              // 前台：先炸再说
    const res = await apiDelete('/api/diary/' + item.id);   // 后台：真的删数据

    // 碎片飞完大概 0.8 秒，等它落定再刷新界面
    setTimeout(() => {
        if (!res.ok) {
            toast('删除失败：' + res.error, 'error', 5000);
            loadHistory();                            // 失败就恢复原样
            return;
        }
        // 如果删的正好是当前正在查看的日记，把右侧面板清回空状态
        if (state.currentDiaryId === item.id) {
            state.currentDiaryId = null;
            resetMusicUI();
            $('resultCard').classList.add('hidden');
            $('resultEmpty').classList.remove('hidden');
        }
        loadHistory();
        loadStats();
        loadTrajectory();
        toast('日记已粉碎 🗑');
    }, 720);
}

/**
 * 粉碎动画本体。
 * 原理（给想深究的同学）：
 *   1. getBoundingClientRect() 拿到卡片在**视口**里的位置和尺寸
 *   2. 建一个 fixed 全屏透明容器 .shatter-holder，按视口坐标摆放碎片
 *   3. 克隆 16 个和卡片同尺寸同外观的 div，用 clip-path 裁出 4x4 中的某一格
 *      （每格边界加随机抖动，看起来是不规则的碎块而不是方格）
 *   4. element.animate() 让每块碎片飞向随机方向 + 旋转 + 变透明
 *      这个 API 是浏览器原生的 Web Animations，性能好且无需任何库
 *   5. setTimeout 后整层移除，页面恢复干净
 */
function playShatter(cardEl) {
    const rect = cardEl.getBoundingClientRect();
    cardEl.style.visibility = 'hidden';               // 本体瞬间消失，只留碎片

    const holder = document.createElement('div');
    holder.className = 'shatter-holder';
    document.body.appendChild(holder);

    // 取卡片左边框的情绪色，让碎片保留这张卡片"是什么情绪"的颜色记忆
    const accent = getComputedStyle(cardEl).borderLeftColor || '#7aa2ff';
    const COLS = 4, ROWS = 4;
    const frags = [];

    for (let r = 0; r < ROWS; r++) {
        for (let c = 0; c < COLS; c++) {
            const frag = document.createElement('div');
            frag.className = 'shatter-fragment';
            frag.style.left = rect.left + 'px';
            frag.style.top = rect.top + 'px';
            frag.style.width = rect.width + 'px';
            frag.style.height = rect.height + 'px';
            frag.style.borderLeftColor = accent;

            // 用百分比裁出这一格；jitter 让边界歪歪扭扭，更像真碎片
            const x0 = (c * 100) / COLS, y0 = (r * 100) / ROWS;
            const x1 = ((c + 1) * 100) / COLS, y1 = ((r + 1) * 100) / ROWS;
            const j = () => (Math.random() * 2 - 1) * 1.6;
            frag.style.clipPath = `polygon(${x0 + j()}% ${y0 + j()}%, ${x1 + j()}% ${y0 + j()}%, ${x1 + j()}% ${y1 + j()}%, ${x0 + j()}% ${y1 + j()}%)`;

            holder.appendChild(frag);
            frags.push(frag);
        }
    }

    frags.forEach((frag) => {
        const dx = (Math.random() - 0.5) * 240;              // 左右飞散
        const dy = (Math.random() - 0.2) * 200 + 60;         // 整体偏向下坠（重力感）
        const rot = (Math.random() - 0.5) * 100;             // 随机翻转
        const dur = 480 + Math.random() * 320;               // 0.48~0.8 秒
        frag.animate(
            [
                { transform: 'translate(0, 0) rotate(0deg)', opacity: 1 },
                { transform: `translate(${dx}px, ${dy}px) rotate(${rot}deg)`, opacity: 0 },
            ],
            { duration: dur, easing: 'cubic-bezier(.2, .65, .35, 1)', fill: 'forwards' }
        );
    });

    // 动画结束后拆掉整个动画层，DOM 恢复干净
    setTimeout(() => holder.remove(), 900);
}

async function loadDiaryDetail(diaryId) {
    const res = await apiGet('/api/diary/' + diaryId);
    if (!res.ok) {
        toast('加载失败：' + res.error, 'error');
        return;
    }
    const diary = res.data;
    state.currentDiaryId = diary.id;

    renderEmotion(diary);
    resetMusicUI();

    if (diary.music) {
        if (diary.music.status === 'ready') {
            onMusicReady(diary.music);
        } else if (diary.music.status === 'failed') {
            showMusicError(diary.music.error_message || '上一次生成失败了');
        } else {
            startPolling(diary.music, true);
        }
    } else {
        $('musicMeta').textContent = '这篇还没有配乐';
        $('regenBtn').classList.remove('hidden');
    }
}

async function loadStats() {
    const res = await apiGet('/api/diary/stats');
    if (!res.ok) return;
    $('statDiaries').textContent = res.data.diary_count;
    $('statTracks').textContent = res.data.ready_track_count;
}

/* ==========================================================================
   八点五、情绪轨迹（日历热力图 + 效价折线图）
   --------------------------------------------------------------------------
   作品介绍写的是"留存自己的情绪变化轨迹"。
   光有一条列表配不上"轨迹"两个字，所以这里补两种可视化：
     · 日历热力图 —— 哪天写了、什么情绪（看分布）
     · 效价折线图 —— 心情高低起伏（看趋势）
   两者都是纯手写 DOM / SVG，不引第三方图表库，零体积负担。
   ========================================================================== */

async function loadTrajectory() {
    const res = await apiGet('/api/diary/trajectory?days=30');
    if (!res.ok) return;
    const data = res.data;
    $('trajStreak').textContent = data.streak;
    $('trajActive').textContent = data.active_days;
    $('trajTotal').textContent = data.total_entries;
    renderCalendar(data.calendar || []);
    renderTrend(data.trend || [], data.start_date, data.end_date);
}

/**
 * 渲染日历热力图。
 *
 * 两个细节：
 *  1. 首行要按"周几"留出空格 —— 否则日历的星期会对不上，看着很别扭。
 *     JS 的 getDay() 是 0=周日，国内习惯周一开始就排，所以做 (day+6)%7 换算。
 *  2. 没写日记的日子也必须渲染成占位格（不能跳过），否则后面的日期整体前移错位。
 */
function renderCalendar(calendar) {
    const grid = $('calendarGrid');
    grid.innerHTML = '';
    if (!calendar.length) return;

    const first = new Date(calendar[0].date + 'T00:00:00');
    const leadingBlanks = (first.getDay() + 6) % 7;      // 周一 = 0
    for (let i = 0; i < leadingBlanks; i++) {
        const blank = document.createElement('div');
        blank.className = 'calendar-cell is-empty';
        grid.appendChild(blank);
    }

    const legend = new Map();     // 情绪代号 → { label, color, count }，顺手统计做图例
    calendar.forEach((day) => {
        const cell = document.createElement('div');
        const hasEntry = day.count > 0;
        cell.className = 'calendar-cell' + (hasEntry ? ' has-entry' : ' is-empty');
        if (hasEntry) {
            cell.style.backgroundColor = day.color;
            // title 属性 = 浏览器原生 tooltip，鼠标悬停就能看到当天详情
            cell.title = `${day.date}　${day.icon} ${day.label}　${day.count} 篇`;
            if (day.count > 1) {
                const badge = document.createElement('span');
                badge.className = 'cell-count';
                badge.textContent = day.count;
                cell.appendChild(badge);
            }
            const item = legend.get(day.emotion) || { label: day.label, color: day.color, count: 0 };
            item.count += day.count;
            legend.set(day.emotion, item);
        } else {
            cell.title = `${day.date}　未记录`;
        }
        grid.appendChild(cell);
    });

    // 图例：按出现次数从多到少排
    const box = $('calendarLegend');
    box.innerHTML = '';
    [...legend.entries()]
        .sort((a, b) => b[1].count - a[1].count)
        .forEach(([, info]) => {
            const item = document.createElement('span');
            item.className = 'legend-item';
            item.innerHTML =
                `<i class="legend-dot" style="background:${info.color}"></i>${escapeHtml(info.label)}`;
            box.appendChild(item);
        });
}

/**
 * 渲染效价折线图（SVG 手绘）。
 *
 * 坐标系换算（这块是图表最容易搞错的地方）：
 *   效价 v ∈ [-1, +1]，+1 是最开心。
 *   SVG 的 y 轴**向下为正**（屏幕坐标系），所以开心（v 大）要对应**较小的 y**。
 *   公式：y = 中线Y - v × 半高  →  v=+1 落在顶部，v=-1 落在底部。
 */
function renderTrend(trend, startDate, endDate) {
    const svg = $('trendChart');
    $('trendStart').textContent = startDate || '—';
    $('trendEnd').textContent = endDate || '—';

    if (!trend || trend.length < 2) {
        svg.innerHTML =
            '<text x="320" y="94" text-anchor="middle" fill="#98a2b8" font-size="13">' +
            '至少记录 2 天，这里才会画出心情曲线</text>';
        return;
    }

    const W = 640, H = 180;
    const padL = 34, padR = 22, padT = 26, padB = 26;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;
    const midY = padT + innerH / 2;        // 效价 0 的水平线
    const bottom = H - padB;

    const points = trend.map((d, i) => ({
        x: padL + (i * innerW) / (trend.length - 1),
        y: midY - Math.max(-1, Math.min(1, d.valence)) * (innerH / 2),
        day: d,
    }));

    const linePoints = points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
    const areaPath =
        `M ${points[0].x.toFixed(1)},${bottom} L ` +
        points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' L ') +
        ` L ${points[points.length - 1].x.toFixed(1)},${bottom} Z`;

    const dots = points
        .map(
            (p) =>
                `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4.2"` +
                ` fill="${p.day.color || '#7c5cff'}" stroke="rgba(11,14,26,.85)" stroke-width="1.6">` +
                `<title>${p.day.date}　${p.day.icon} ${p.day.label}　效价 ${p.day.valence}</title>` +
                `</circle>`
        )
        .join('');

    svg.innerHTML = `
        <defs>
            <linearGradient id="trendGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="rgba(124,92,255,.42)"/>
                <stop offset="100%" stop-color="rgba(124,92,255,0)"/>
            </linearGradient>
        </defs>
        <line x1="${padL}" y1="${midY}" x2="${W - padR}" y2="${midY}"
              stroke="rgba(255,255,255,.16)" stroke-width="1" stroke-dasharray="4 4"/>
        <path class="trend-area" d="${areaPath}"/>
        <polyline points="${linePoints}" fill="none" stroke="#a78bfa"
                  stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>
        ${dots}
        <text x="${padL - 8}" y="${padT + 4}" text-anchor="end" fill="#6b7591" font-size="10">+1</text>
        <text x="${padL - 8}" y="${midY + 3}" text-anchor="end" fill="#6b7591" font-size="10">0</text>
        <text x="${padL - 8}" y="${bottom + 3}" text-anchor="end" fill="#6b7591" font-size="10">-1</text>
    `;
}

/**
 * HTML 转义：把用户输入的尖括号换成实体字符。
 * 不做这一步的话，如果有人写了 <script> 标签，会被浏览器当成真代码执行（XSS 攻击）。
 * 这是前端最基本的安全习惯。
 */
function escapeHtml(text) {
    const map = { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' };
    return String(text || '').replace(/[<>&"]/g, (ch) => map[ch]);
}


/* ==========================================================================
   九、入口
   ========================================================================== */
// DOMContentLoaded：等 HTML 全部解析完再执行，保证 getElementById 都能拿到东西
document.addEventListener('DOMContentLoaded', () => {
    bindEvents();
    checkHealth();
    loadHistory();
    loadStats();
    loadTrajectory();
});
