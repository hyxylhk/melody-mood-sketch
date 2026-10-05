"""
==============================================================================
backend/services/local_composer.py —— 本地情绪→音乐合成引擎（离线兜底）
==============================================================================
这个文件做的事：**用纯 Python 把情绪参数变成一段能听的 WAV 音乐。**

为什么需要它？
    真实音乐生成 API（Suno / Mureka 等）普遍有三个问题：
        1. 要钱：生成一次几毛到几块
        2. 要等：一次 30 秒 ~ 3 分钟
        3. 要翻墙 / 限流：黑客松现场网络不可控
    于是我们自己写一个「情绪驱动的算法作曲引擎」作为兜底通道：
        - 零外部依赖，只用 Python 标准库（wave / math / array / random）
        - 零网络请求，断网可用
        - 秒级返回，演示节奏可控
    同时它也是一个技术亮点：**情绪参数化的可控音乐生成**。

==============================================================================
乐理小抄（看不懂没关系，下面每个概念使用前都有一句解释）
==============================================================================
    MIDI 音高：用整数表示音高，60 = 中央 C，每 +12 升高一个八度，每 +1 升高半音
    采样率(SR)：每秒记录多少个声音快照。44100 Hz = CD 音质
    ADSR 包络：一个音从出生到消失的音量曲线
                起音(A) 衰减(D) 持续(S) 释放(R)
    谐波叠加：真实乐器 = 基频 + 整数倍泛音，不同音量比例 → 不同音色
    混响(Reverb)：模拟房间里墙壁反射产生的"空间感"
"""

from __future__ import annotations

import array
import math
import random
import wave
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from backend.services.emotion_catalog import CHORD_SHAPES, EmotionProfile


# ===========================================================================
# 一、基础工具函数
# ===========================================================================
def midi_to_freq(midi_note: float) -> float:
    """
    MIDI 音高 → 频率(Hz)。
    公式：f = 440 * 2^((n-69)/12)，其中 A4(69号) = 440Hz 是国际标准音。
    """
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def clamp(value: float, low: float, high: float) -> float:
    """把数值限制在区间内（防止音量爆炸 / 下标越界）。"""
    return low if value < low else high if value > high else value


def note_name(midi_note: int) -> str:
    """把 MIDI 数字转成 C4 / D#5 这种音名，主要用于日志和调试。"""
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[midi_note % 12]}{midi_note // 12 - 1}"


# ===========================================================================
# 二、单个音符的渲染（整个引擎里唯一"重数学"的地方）
# ===========================================================================
def mix_tone(
    left: array.array,
    right: array.array,
    start_sample: int,
    freq: float,
    harmonics: Sequence[Tuple[float, float]],
    sample_rate: int,
    duration_sec: float,
    gain: float = 0.2,
    attack_sec: float = 0.02,
    release_sec: float = 0.25,
    pan: float = 0.0,
    vibrato_hz: float = 0.0,
    vibrato_depth: float = 0.0,
) -> None:
    """
    把一个音符「叠加写入」(mix) 到左右声道缓冲区里。

    参数说明：
        left / right   : array('d') 类型的浮点缓冲区，代表左右声道
                         （用 array 而不是 list，内存省一半、速度也更快）
        start_sample   : 这个音从第几个采样点开始
        freq           : 基频(Hz)
        harmonics      : [(谐波倍数, 音量), ...]，决定音色
        duration_sec   : 音持续多久
        gain           : 总音量 0~1
        attack_sec     : 起音时长（太小会有"啪"的爆音，一般 >= 0.01）
        release_sec    : 释放时长（让音自然消失，不要硬切）
        pan            : 声场位置 -1(最左) ~ +1(最右)，0 = 居中
        vibrato_hz     : 颤音频率，0 = 不加颤音
        vibrato_depth  : 颤音深度（音高浮动的比例）

    为什么要用"增量累加"而不是"覆盖写入"？
        因为多个音经常在时间上重叠（和弦就是同时响的多个音），
        音乐的本质就是波形叠加。
    """
    total_samples = int(duration_sec * sample_rate)
    if total_samples <= 0 or start_sample >= len(left):
        return

    # 防止音符尾巴超出缓冲区长度（最后一小节的音可能被截断）
    total_samples = min(total_samples, len(left) - start_sample)

    attack_n = max(1, int(attack_sec * sample_rate))
    release_n = max(1, int(release_sec * sample_rate))
    # 如果包络总长比音符还长（超短音符），按比例压缩，避免出现零长度
    if attack_n + release_n > total_samples:
        attack_n = max(1, total_samples // 4)
        release_n = max(1, total_samples // 3)

    sustain_start = attack_n
    sustain_end = total_samples - release_n

    # 把 Math 库函数绑定到局部变量：这是 CPython 的经典优化手段，
    # 避免在百万次循环里反复做全局符号查找
    sin = math.sin
    pi2 = math.pi * 2.0

    # pan → 左右声道音量系数（等功率声像：cos/sin 而非线性插值）
    pan = clamp(pan, -1.0, 1.0)
    angle = (pan + 1.0) * math.pi / 4.0
    gain_left = gain * math.cos(angle) * 1.414
    gain_right = gain * math.sin(angle) * 1.414

    # 每个谐波一个相位累加器。
    # 相位累加写法：phase += increment，避免每次都做乘法，也避免浮点累积误差过大
    increments: List[float] = []
    phases: List[float] = []
    amps: List[float] = []
    for mult, amp in harmonics:
        f = freq * mult
        # 奈奎斯特准则：超过采样率一半的频率会产生失真（混叠），直接丢弃该泛音
        if f >= sample_rate * 0.45:
            continue
        increments.append(pi2 * f / sample_rate)
        phases.append(0.0)
        amps.append(amp)

    if not amps:
        return

    vib_increment = pi2 * vibrato_hz / sample_rate if vibrato_hz > 0 else 0.0
    vib_phase = 0.0

    for i in range(total_samples):
        # ---------- 1) 计算 ADSR 包络 ----------
        if i < attack_n:
            env = i / attack_n                              # 线性起音
        elif i < sustain_end:
            env = 1.0                                       # 持续段
        else:
            env = (total_samples - i) / release_n           # 线性释放
            if env <= 0.0:
                env = 0.0

        # ---------- 2) 颤音（可选）----------
        mod = 1.0
        if vib_increment:
            mod = 1.0 + vibrato_depth * sin(vib_phase)
            vib_phase += vib_increment

        # ---------- 3) 谐波叠加得到当前采样值 ----------
        value = 0.0
        for idx in range(len(amps)):
            p = phases[idx]
            value += amps[idx] * sin(p * mod)
            new_p = p + increments[idx]
            # 周期性归一：防止相位累加到很大导致浮点精度损失
            if new_p > pi2:
                new_p -= pi2
            phases[idx] = new_p

        # ---------- 4) 乘包络并写入左右声道 ----------
        value *= env
        pos = start_sample + i
        left[pos] += value * gain_left
        right[pos] += value * gain_right


# ===========================================================================
# 三、混响（用梳状滤波器模拟房间反射）
# ===========================================================================
def apply_reverb(
    buffer: array.array,
    sample_rate: int,
    delay_ms: float,
    decay: float,
    wet_mix: float,
) -> None:
    """
    给单声道加混响。

    原理很简单 y[i] = x[i] + decay * y[i - D]：
        声音会延迟 D 个采样点并以 decay 倍衰减地重复出现，
        听起来就像在房间里来回反射。多次调用不同的 D 就有"空间感"了。
    """
    delay_samples = max(1, int(delay_ms * sample_rate / 1000.0))
    if delay_samples >= len(buffer):
        return

    dry_gain = 1.0 - wet_mix
    history_i = delay_samples

    for i in range(history_i, len(buffer)):
        buffer[i] = dry_gain * buffer[i] + wet_mix * decay * buffer[i - delay_samples]


def apply_multi_reverb(buffer: array.array, sample_rate: int, amount: float) -> None:
    """叠加几组不同延时的混响，得到更自然的空间感。"""
    if amount <= 0.0:
        return
    wet = clamp(amount, 0.0, 0.85)
    # 三个互质的延时长度，避免产生明显的金属感（周期性回声）
    apply_reverb(buffer, sample_rate, 29.7, 0.42, wet * 0.55)
    apply_reverb(buffer, sample_rate, 37.1, 0.38, wet * 0.40)
    apply_reverb(buffer, sample_rate, 53.3, 0.31, wet * 0.30)


# ===========================================================================
# 四、旋律生成：在给定音阶上做"随机游走"
# ===========================================================================
def generate_melody_notes(
    rng: random.Random,
    scale_intervals: Sequence[int],
    base_midi: int,
    count: int,
) -> List[int]:
    """
    生成一条听起来还算好听的旋律。

    算法：马尔可夫式随机游走
        - 把音阶展开成两级八度的候选音列表
        - 每一步从上一个音的位置出发，按 [-2,-1,-1,0,+1,+1,+2] 的偏好随机跳一步
        - 偏好小跳（-1/0/+1 权重更高），因为旋律进行以级进为主、跳进为辅，
          这符合人类听觉习惯（gestalt 原则中的"良好延续"）

    参数:
        scale_intervals: 音阶的半音偏移，如大调五声 (0,2,4,7,9)
        base_midi      : 旋律的起始 MIDI 音高
        count          : 要生成多少个音符
    返回:
        MIDI 音高列表
    """
    # 把音阶铺成两个八度，旋律有更宽的活动空间
    degrees: List[int] = []
    for octave in (0, 12):
        for interval in scale_intervals:
            degrees.append(octave + interval)

    steps = (-2, -1, -1, 0, 1, 1, 2, -3, 3)
    index = len(degrees) // 2       # 从音阶中间起步（不高不低）
    notes: List[int] = []

    for _ in range(count):
        notes.append(base_midi + degrees[index])
        index += rng.choice(steps)
        # 碰到边界就"反弹"回来，防止旋律跑出可用音域
        if index < 0:
            index = 1
        elif index >= len(degrees):
            index = len(degrees) - 2

    return notes


# ===========================================================================
# 五、主函数：按情绪合成一整段音乐并落盘为 WAV
# ===========================================================================
def compose_emotion_music(
    profile: EmotionProfile,
    output_path: Path,
    duration_sec: int = 20,
    sample_rate: int = 44100,
    seed: int | None = None,
) -> Dict[str, object]:
    """
    根据情绪配置合成音乐，写成 16bit PCM 立体声 WAV 文件。

    整体结构（可以对照着听）：
        第 1~2 小节：只有 pad 和 bass（营造氛围，像是慢慢进入）
        第 3 小节起  ：主旋律加入
        最后 2 秒   ：整体淡出

    参数:
        profile      : 情绪配置（速度、音阶、和弦走向、音色都在里面）
        output_path  : 输出 wav 文件路径
        duration_sec : 时长（秒）
        sample_rate  : 采样率
        seed         : 随机种子。给定相同的 seed 必然生成完全相同的音乐
                       —— 这叫"可复现"，便于测试和调试
    返回:
        一个信息字典，包含文件路径、时长、调式、BPM 等元数据（要写进数据库）
    """
    if seed is None:
        seed = random.randint(0, 2 ** 31 - 1)
    rng = random.Random(seed)

    total_samples = int(duration_sec * sample_rate)

    # array('d') = 双精度浮点数组。类比 C++ 里的 std::vector<double>
    left = array.array("d", [0.0]) * total_samples
    right = array.array("d", [0.0]) * total_samples

    # ---------------- 计算节奏网格 ----------------
    beat_duration = 60.0 / profile.tempo_bpm           # 一拍多少秒
    bar_duration = beat_duration * 4.0                 # 一小节 4 拍
    # 向上取整：宁可让最后一小节被淡出切掉，也不要结尾留一大段空白
    total_bars = max(1, math.ceil(duration_sec / bar_duration))

    # ---------------- 铺底和声（Pad）：每小节一个和弦 ----------------
    # 情绪的主音在 profile.root_midi，pad 放在低一个八度的位置更温润
    pad_base = profile.root_midi - 12
    for bar_index in range(total_bars):
        bar_start_sec = bar_index * bar_duration
        bar_start_sample = int(bar_start_sec * sample_rate)

        root_offset, chord_quality = profile.progression[bar_index % len(profile.progression)]
        chord_root = pad_base + root_offset
        shape = CHORD_SHAPES.get(chord_quality, CHORD_SHAPES["maj"])

        for tone_index, semitone in enumerate(shape):
            pan_position = -0.35 + tone_index * 0.22   # 和弦音从左往右散开，立体感更好
            mix_tone(
                left, right, bar_start_sample,
                freq=midi_to_freq(chord_root + semitone),
                harmonics=profile.pad_timbre,
                sample_rate=sample_rate,
                duration_sec=bar_duration * 0.98,
                gain=0.11,
                attack_sec=0.35,          # 慢起音 = 弦乐/管风琴那种缓缓涌入的感觉
                release_sec=0.45,
                pan=pan_position,
            )

    # ---------------- 低音（Bass）：每小节 2 个长音 ----------------
    bass_base = profile.root_midi - 24
    for bar_index in range(total_bars):
        bar_start_sec = bar_index * bar_duration
        root_offset, _quality = profile.progression[bar_index % len(profile.progression)]
        for half in (0, 2):     # 第 1 拍和第 3 拍
            start_sec = bar_start_sec + half * beat_duration
            mix_tone(
                left, right, int(start_sec * sample_rate),
                freq=midi_to_freq(bass_base + root_offset),
                # 低音只用正弦 + 一点点二次谐波，纯低频更干净有力
                harmonics=((1.0, 1.0), (2.0, 0.18)),
                sample_rate=sample_rate,
                duration_sec=beat_duration * 1.9,
                gain=profile.bass_gain,
                attack_sec=0.05,
                release_sec=0.30,
                pan=0.0,
            )

    # ---------------- 主旋律（Lead）：装饰音符 ----------------
    melody_base = profile.root_midi + 12
    # 从第 3 小节才开始进旋律，前面留白更有"起承转合"
    melody_start_bar = 2 if total_bars > 3 else 0
    available_beats = int((total_bars - melody_start_bar) * 4)
    if available_beats > 0:
        # density 决定每拍是否落音：0.85 表示约 85% 的拍子有音符
        note_count = max(1, int(available_beats * profile.melody_density))
        melody = generate_melody_notes(rng, profile.scale, melody_base, note_count)

        note_index = 0
        for bar in range(melody_start_bar, total_bars):
            for beat in range(4):
                if note_index >= len(melody):
                    break
                # 不是每一拍都放音，用密度阈值做随机的休止
                if rng.random() > profile.melody_density:
                    continue

                start_sec = bar * bar_duration + beat * beat_duration
                # 音符长度：大部分是 1 拍，偶尔拉长到 1.5 拍
                length_factor = rng.choice((1.0, 1.0, 0.75, 1.5))
                mix_tone(
                    left, right, int(start_sec * sample_rate),
                    freq=midi_to_freq(melody[note_index]),
                    harmonics=profile.lead_timbre,
                    sample_rate=sample_rate,
                    duration_sec=beat_duration * length_factor,
                    gain=0.16,
                    attack_sec=0.015,
                    release_sec=0.28,
                    pan=rng.uniform(-0.28, 0.28),
                    vibrato_hz=4.6,                    # 轻微颤音，让音色不那么电子/死板
                    vibrato_depth=0.004,
                )
                note_index += 1

    # ---------------- 后期处理：混响 → 淡入淡出 → 归一化 → 写文件 ----------------
    apply_multi_reverb(left, sample_rate, profile.reverb_amount)
    apply_multi_reverb(right, sample_rate, profile.reverb_amount)

    _apply_fade(left, sample_rate, fade_in_sec=0.05, fade_out_sec=2.0)
    _apply_fade(right, sample_rate, fade_in_sec=0.05, fade_out_sec=2.0)

    _normalize(left, target_peak=0.85)
    _normalize(right, target_peak=0.85)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_wav(output_path, left, right, sample_rate)

    return {
        "file_path": str(output_path),
        "file_name": output_path.name,
        "duration_seconds": duration_sec,
        "sample_rate": sample_rate,
        "tempo_bpm": profile.tempo_bpm,
        "root_note": note_name(profile.root_midi),
        "seed": seed,
    }


def _apply_fade(
    buffer: array.array,
    sample_rate: int,
    fade_in_sec: float,
    fade_out_sec: float,
) -> None:
    """
    音量淡入淡出。
    淡入避免开头爆音，淡出让结尾自然消失（而不是"咔"一下切断）。
    """
    length = len(buffer)
    fade_in_n = min(length // 2, int(fade_in_sec * sample_rate))
    fade_out_n = min(length // 2, int(fade_out_sec * sample_rate))

    for i in range(fade_in_n):
        buffer[i] *= i / fade_in_n

    for i in range(fade_out_n):
        pos = length - fade_out_n + i
        buffer[pos] *= 1.0 - i / fade_out_n


def _normalize(buffer: array.array, target_peak: float = 0.95) -> None:
    """
    归一化：把整段音频的最大振幅缩放到 target_peak。

    为什么要做？多个音叠加后总音量可能很小（听不见）或者超过 1.0。
    超过 1.0 叫"削波"，波形会被平顶切开，听起来是刺耳的破音。

    做法：先找到整段的峰值，然后整体乘一个系数让这个峰值刚好落在 target_peak。
    因为是按最大值缩放的，所以缩放后**不可能**再有样本越界，不需要额外限幅。
    """
    peak = 0.0
    for value in buffer:
        abs_value = value if value >= 0 else -value
        if abs_value > peak:
            peak = abs_value

    if peak <= 1e-9:
        return      # 静音段，跳过避免除以 0

    scale = target_peak / peak
    for i in range(len(buffer)):
        buffer[i] *= scale


def _write_wav(path: Path, left: array.array, right: array.array, sample_rate: int) -> None:
    """
    把浮点数组写成 16bit PCM 立体声 WAV 文件（用标准库 wave 模块）。

    WAV 文件结构（很简单）：
        [RIFF 头][fmt 块描述格式][data 块放真正的采样点]
    每个采样点用 2 字节有符号整数表示，范围 -32768 ~ 32767。
    """
    length = len(left)
    stereo = array.array("h")       # 'h' = signed short = 2 字节

    for i in range(length):
        # 浮点 -1.0~1.0 → 整数 -32767~32767；正负用心地各自夹住防止溢出
        sample_left = int(left[i] * 32767.0)
        sample_right = int(right[i] * 32767.0)
        if sample_left > 32767:
            sample_left = 32767
        elif sample_left < -32767:
            sample_left = -32767
        if sample_right > 32767:
            sample_right = 32767
        elif sample_right < -32767:
            sample_right = -32767
        # WAV 立体声的排列是：左1 右1 左2 右2 ...（交错存放）
        stereo.append(sample_left)
        stereo.append(sample_right)

    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)        # 2 = 立体声
        wav_file.setsampwidth(2)        # 2 字节 = 16 bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(stereo.tobytes())


# ===========================================================================
# 六、给上层调用的便捷封装
# ===========================================================================
def render_wav_for_emotion(
    profile: EmotionProfile,
    audio_dir: Path,
    emotion_code: str,
    track_id: int,
    duration_sec: int = 20,
    sample_rate: int = 44100,
    seed: int | None = None,
) -> Dict[str, object]:
    """
    生成文件名、调用合成引擎、返回元数据。上层(music_service)只需要调这个。

    文件名格式： emo_{情绪}_{track_id}_{时间戳}.wav
    例如：      emo_joy_12_20261002_124530.wav
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if seed is None:
        # 用 track_id 做种子 → 同一条记录重新生成会得到同样的音乐（可复现）
        seed = track_id * 7919 + 13

    filename = f"emo_{emotion_code}_{track_id}_{timestamp}.wav"
    output_path = audio_dir / filename

    return compose_emotion_music(
        profile=profile,
        output_path=output_path,
        duration_sec=duration_sec,
        sample_rate=sample_rate,
        seed=seed,
    )
