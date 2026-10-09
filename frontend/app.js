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
   删除 + 「抖动 → 粒子化消散」动效

   两个阶段：
     ① 抖动（约 0.5 秒）：卡片本体左右高频摆动，描边转红，像"被抓住摇醒"
     ② 粒子化（约 2 秒）：本体隐身，Canvas 在原位铺出约 2000 个彩色颗粒，
        先整体高频抖动制造"不稳定感"，再被风错峰吹向上方、湍流飘散并渐隐
        （灭霸打响指那种消散，参考粒子化动效实现）

   数据请求和动画并行：DELETE 请求在动画开跑的同一瞬间发出，互不等待。
--------------------------------------------------------------------------- */
async function shatterAndDelete(item, cardEl) {
    // dataset.deleting 是个"防重入锁"：连点删除按钮时只触发一次
    if (cardEl.dataset.deleting) return;
    cardEl.dataset.deleting = '1';

    // 这里故意不 await：请求立刻发出去，动画同时开跑，两条线并行
    const resPromise = apiDelete('/api/diary/' + item.id);

    await playShatter(cardEl);          // 等整套动画演完（约 1.5 秒）
    const res = await resPromise;       // 这时候结果通常早就回来了

    if (!res.ok) {
        toast('删除失败：' + res.error, 'error', 5000);
        loadHistory();                  // 失败就恢复原样
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
}

/** 完整动效：先抖动，再粉碎 */
async function playShatter(cardEl) {
    await shakeCard(cardEl);
    await burstIntoParticles(cardEl);
}

/**
 * 阶段一：抖动。
 * 用 element.animate() 给卡片本体排一组来回摆动的关键帧（幅度先小后大）。
 * 返回值是 Promise，所以调用方可以直接 await —— 比 setTimeout 精确。
 */
function shakeCard(cardEl) {
    cardEl.classList.add('card-doomed');      // 描边转红，提示"这篇要没了"
    const anim = cardEl.animate(
        [
            { transform: 'translate(0, 0) rotate(0deg)' },
            { transform: 'translate(-4px, 1px) rotate(-0.8deg)' },
            { transform: 'translate(4px, -1px) rotate(0.9deg)' },
            { transform: 'translate(-8px, 2px) rotate(-1.8deg)' },
            { transform: 'translate(8px, -2px) rotate(2deg)' },
            { transform: 'translate(-6px, 1px) rotate(-1.4deg)' },
            { transform: 'translate(3px, 0) rotate(0.6deg)' },
            { transform: 'translate(0, 0) rotate(0deg)' },
        ],
        { duration: 520, easing: 'ease-in-out' }
    );
    // anim.finished 是动画自带的 Promise；catch 是防页面切走导致动画被打断时报错
    return anim.finished.catch(() => {});
}

/**
 * 阶段二：灭霸式粒子化消散。
 * 原理（给想深究的同学）：
 *   1. getBoundingClientRect() 拿到卡片在**视口**里的位置和尺寸
 *   2. 建一个和卡片同大小的 <canvas>，fixed 定位叠在卡片原位
 *   3. 按 5px 网格采样整个卡片矩形，铺出约 2000 个彩色小粒子
 *      （情绪色打底 + 白灰质感 + 少量彩色点缀，像素颗粒感）
 *   4. 前一小段：全体粒子在原位高频抖动，像信号噪点一样"不稳定"
 *   5. 之后每个粒子随机错开 0~0.5 秒被"风"吹走：
 *        向上飘（灭霸消散的方向）+ 正弦湍流摆动 + 线性渐隐
 *   6. 全部飘完后移除 canvas，DOM 恢复干净
 */
function burstIntoParticles(cardEl) {
    const rect = cardEl.getBoundingClientRect();
    // 取卡片左边框的情绪色，让粒子保留这张卡片"是什么情绪"的颜色记忆
    const accent = getComputedStyle(cardEl).borderLeftColor || '#7aa2ff';

    cardEl.classList.remove('card-doomed');
    cardEl.style.visibility = 'hidden';         // 本体消失，只留粒子

    const canvas = document.createElement('canvas');
    canvas.className = 'shatter-canvas';
    const W = Math.ceil(rect.width), H = Math.ceil(rect.height);
    canvas.width = W;
    canvas.height = H;
    canvas.style.cssText = 'position:fixed;z-index:999;pointer-events:none;'
        + 'left:' + rect.left + 'px;top:' + rect.top + 'px;'
        + 'width:' + W + 'px;height:' + H + 'px;';
    document.body.appendChild(canvas);
    const ctx = canvas.getContext('2d');

    // 粒子调色板：情绪色打底 + 白/灰做卡片质感 + 少量彩色点缀（像素风）
    const palette = [accent, accent, '#e8ecf5', '#9aa5bd', '#ffd166', '#ef476f', '#4ecdc4', '#a78bfa'];

    // ---- 网格采样铺粒子 ----
    const GAP = 5;                    // 采样间距：越小粒子越密、越像原内容的形状
    const parts = [];
    for (let y = GAP / 2; y < H; y += GAP) {
        for (let x = GAP / 2; x < W; x += GAP) {
            parts.push({
                ox: x + (Math.random() * 2 - 1) * 1.6,   // 初始位置（带抖动）
                oy: y + (Math.random() * 2 - 1) * 1.6,
                size: 1.4 + Math.random() * 1.8,          // 1.4~3.2px 的彩色颗粒
                color: palette[(Math.random() * palette.length) | 0],
                delay: Math.random() * 0.5,               // 被风吹走的错峰时间
                vx: (Math.random() * 2 - 1) * 80,         // 水平风向（左右都可能）
                vy: -(60 + Math.random() * 130),          // 一律向上飘（灭霸消散方向）
                wobF: 1.5 + Math.random() * 2.5,          // 湍流频率
                wobA: 12 + Math.random() * 30,            // 湍流幅度
                phase: Math.random() * 6.283,
                life: 0.9 + Math.random() * 0.8,          // 起飞后存活时长（渐隐用）
            });
        }
    }

    const T0 = performance.now();
    const TOTAL_MS = 2600;                    // 整段动画时长（含最长尾的粒子）

    return new Promise((resolve) => {
        function frame(now) {
            const t = (now - T0) / 1000;      // 秒
            ctx.clearRect(0, 0, W, H);
            let alive = 0;

            for (const p of parts) {
                const lt = t - p.delay;       // 这个粒子自己的时间线

                let px, py, alpha;
                if (lt < 0) {
                    // 还没被吹走：在原位高频抖动（像信号不稳）
                    px = p.ox + Math.sin(t * 45 + p.phase) * 1.3;
                    py = p.oy + Math.cos(t * 38 + p.phase) * 1.3;
                    alpha = 1;
                } else if (lt < p.life) {
                    // 被风吹走：匀速上飘 + 正弦湍流 + 渐隐
                    px = p.ox + p.vx * lt + Math.sin(lt * p.wobF * 2 + p.phase) * p.wobA * lt;
                    py = p.oy + p.vy * lt + Math.cos(lt * p.wobF + p.phase) * p.wobA * 0.4 * lt;
                    alpha = 1 - lt / p.life;
                } else {
                    continue;                 // 已经飘没了
                }

                alive++;
                ctx.globalAlpha = alpha;
                ctx.fillStyle = p.color;
                ctx.fillRect(px, py, p.size, p.size);
            }

            if (alive > 0 && t * 1000 < TOTAL_MS + 400) {
                requestAnimationFrame(frame);
            } else {
                canvas.remove();
                resolve();
            }
        }
        requestAnimationFrame(frame);
    });
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
