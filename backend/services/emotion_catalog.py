"""
==============================================================================
backend/services/emotion_catalog.py —— 情绪字典 & 情绪→音乐参数映射表
==============================================================================
这个文件是本项目**最核心的一张表**，定义了两件事：

  1. 一共有哪些情绪（把自然语言收敛到有限的 16 种标准情绪）
  2. 每种情绪对应什么样的音乐参数（速度、调式、和弦走向、音色）

为什么要这样做？
    大模型输出是自由文本，没法直接拿来作曲。
    我们先让大模型从 16 种情绪里"做选择题"（输出受控的标签），
    再根据这张表把标签翻译成具体的音乐参数。
    这一步「情绪 → 音乐参数」的映射，就是本项目的技术核心之一。

给 C++ 同学的类比：
    这就是一张手写的 switch-case 查表，只不过每格里塞的是一组结构体字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# 音阶定义：数字是相对主音的「半音数」
# 例如 [0,2,4,7,9]：主音、大二度、大三度、纯五度、大六度 → 大调五声音阶
#   C 大调五声 = C(0) D(2) E(4) G(7) A(9)
# ---------------------------------------------------------------------------
MAJOR_PENTATONIC: Tuple[int, ...] = (0, 2, 4, 7, 9)      # 明亮、欢快
MINOR_PENTATONIC: Tuple[int, ...] = (0, 3, 5, 7, 10)     # 忧郁、内敛
NATURAL_MINOR: Tuple[int, ...] = (0, 2, 3, 5, 7, 8, 10)  # 小调，更浓的情绪
DORIAN: Tuple[int, ...] = (0, 2, 3, 5, 7, 9, 10)         # 多利亚：小调但带一点希望感
LYDIAN: Tuple[int, ...] = (0, 2, 4, 6, 7, 9, 11)         # 利底亚：空灵、梦幻

# ---------------------------------------------------------------------------
# 和弦定义：也是相对和弦根音的半音数
#   maj = 大三和弦（明亮）  min = 小三和弦（忧伤）
#   maj7/min7 加了七音，听起来更"高级"、更松弛
# ---------------------------------------------------------------------------
CHORD_SHAPES: Dict[str, Tuple[int, ...]] = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10),
    "min9": (0, 3, 7, 10, 14),
    "sus2": (0, 2, 7),
}

# ---------------------------------------------------------------------------
# 音色定义：一串 (谐波倍数, 音量) 的列表。
# 原理：真实乐器的声音 = 基频 + 一堆整数倍泛音，不同泛音比例决定音色。
#   [(1, 1.0), (2, 0.3)]                 → 接近音叉/长笛，干净
#   [(1, 1), (2, .5), (3, .25), (4, .12)] → 接近钢琴/吉他，有颗粒感
#   [(1, 1), (2, .6), (3, .4), (4, .25), (5, .12)] → 偏弦乐，厚实
# ---------------------------------------------------------------------------
TIMBRE_FLUTE: Tuple[Tuple[float, float], ...] = ((1.0, 1.0), (2.0, 0.22), (3.0, 0.08))
TIMBRE_PIANO: Tuple[Tuple[float, float], ...] = ((1.0, 1.0), (2.0, 0.45), (3.0, 0.22), (4.0, 0.10))
TIMBRE_STRINGS: Tuple[Tuple[float, float], ...] = ((1.0, 1.0), (2.0, 0.55), (3.0, 0.33), (4.0, 0.18), (5.0, 0.08))
TIMBRE_BELL: Tuple[Tuple[float, float], ...] = ((1.0, 1.0), (2.76, 0.35), (5.4, 0.14), (8.9, 0.06))
TIMBRE_ORGAN: Tuple[Tuple[float, float], ...] = ((1.0, 1.0), (2.0, 0.60), (3.0, 0.05), (4.0, 0.28))


@dataclass(frozen=True)
class EmotionProfile:
    """
    一种情绪的完整定义 + 它对应的音乐参数。

    frozen=True 表示创建后不可修改（类似 C++ 的 const 对象），防止被误改。
    """

    code: str                       # 英文代号，作为数据库里存的主键字符串
    label: str                      # 中文名（前端展示用）
    label_en: str                   # 英文名
    color: str                      # 主题色（前端用来画卡片）
    icon: str                       # emoji，前端直接显示
    valence: float                  # 效价 -1(极负面) ~ +1(极正面)，心理学衡量「心情好坏」
    arousal: float                  # 唤醒度 0(平静) ~ 1(激动)，心理学衡量「能量高低」
    description: str                # 一句话描述，给答辩 PPT 用

    # ---- 用于本地关键词识别（大模型不可用时兜底）----
    keywords: Tuple[str, ...] = ()

    # ---- 音乐参数 ----
    tempo_bpm: int = 84             # 每分钟节拍数：越大越快
    root_midi: int = 60             # 主音的 MIDI 音高，60 = 中央 C(C4)
    scale: Tuple[int, ...] = MAJOR_PENTATONIC
    # 和弦走向：每小节一个和弦，(相对主音的半音数, 和弦类型)
    progression: Tuple[Tuple[int, str], ...] = ((0, "maj"), (5, "maj"), (7, "min"), (5, "maj"))
    lead_timbre: Tuple[Tuple[float, float], ...] = TIMBRE_PIANO   # 主旋律音色
    pad_timbre: Tuple[Tuple[float, float], ...] = TIMBRE_STRINGS  # 铺底和声音色
    bass_gain: float = 0.30         # 低音音量
    melody_density: float = 0.75    # 旋律音符密度 0~1：越大音符越密
    reverb_amount: float = 0.28     # 混响量：越大越"空旷/有空间感"
    style_words: Tuple[str, ...] = ()  # 英文风格词，用于拼云端音乐 API 的 prompt


# ===========================================================================
# 下面就是那张查表用的字典。16 种情绪。
# 顺序约定：正向在前，中性居中，负向在后。
# ===========================================================================
_EMOTION_PROFILES: Tuple[EmotionProfile, ...] = (
    # ---------------- 正向情绪 ----------------
    EmotionProfile(
        code="joy", label="喜悦", label_en="Joy", color="#FFB020", icon="😄",
        valence=0.85, arousal=0.72, description="明亮上行的日常快乐",
        keywords=("开心", "高兴", "快乐", "哈哈", "太棒", "超爽", "兴奋", "完美", "喜报", "成功", "通过", "升职", "加薪", "中奖", "过稿",
                  "通过了", "获奖", "搞定", "哈哈大笑", "happy", "开心到"),
        tempo_bpm=116, root_midi=62, scale=MAJOR_PENTATONIC,
        progression=((0, "maj7"), (5, "maj"), (7, "min7"), (5, "maj")),
        lead_timbre=TIMBRE_PIANO, pad_timbre=TIMBRE_ORGAN,
        bass_gain=0.32, melody_density=0.85, reverb_amount=0.20,
        style_words=("uplifting", "bright", "acoustic pop", "major key"),
    ),
    EmotionProfile(
        code="excitement", label="激动", label_en="Excitement", color="#FF7043", icon="🔥",
        valence=0.65, arousal=0.92, description="高能量、心跳加速的燃感",
        keywords=("激动", "燃", "热血", "冲刺", "往前冲", "迫不及待", "肾上腺素", "炸了", "太燃", "出发"),
        tempo_bpm=132, root_midi=64, scale=MAJOR_PENTATONIC,
        progression=((0, "maj"), (7, "maj"), (5, "maj"), (7, "maj")),
        lead_timbre=TIMBRE_ORGAN, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.38, melody_density=1.0, reverb_amount=0.16,
        style_words=("energetic", "driving beat", "synthwave", "festival"),
    ),
    EmotionProfile(
        code="love", label="爱意", label_en="Love", color="#FF7BA9", icon="💗",
        valence=0.80, arousal=0.50, description="柔软温暖的亲密感",
        keywords=("喜欢", "爱", "心动", "想你", "拥抱", "亲吻", "宝贝", "告白", "表白",
                   "脱单", "恋爱", "在一起了", "男朋友", "女朋友", "crush"),
        tempo_bpm=88, root_midi=60, scale=MAJOR_PENTATONIC,
        progression=((0, "maj7"), (9, "min7"), (5, "maj7"), (7, "min7")),
        lead_timbre=TIMBRE_PIANO, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.26, melody_density=0.65, reverb_amount=0.34,
        style_words=("romantic", "warm", "soft piano", "R&B ballad"),
    ),
    EmotionProfile(
        code="tenderness", label="温柔", label_en="Tenderness", color="#F7C8D8", icon="🌸",
        valence=0.60, arousal=0.30, description="被治愈的、低声说话般的安稳",
        keywords=("温柔", "治愈", "暖", "柔软", "笑了", "放松下来", "轻声", "小猫", "晚安", "被照顾"),
        tempo_bpm=76, root_midi=60, scale=MAJOR_PENTATONIC,
        progression=((0, "maj7"), (7, "min7"), (5, "maj7"), (2, "min7")),
        lead_timbre=TIMBRE_FLUTE, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.22, melody_density=0.55, reverb_amount=0.38,
        style_words=("gentle", "tender", "lullaby", "ambient"),
    ),
    EmotionProfile(
        code="gratitude", label="感激", label_en="Gratitude", color="#7BC86C", icon="🙏",
        valence=0.72, arousal=0.42, description="有人托底的踏实感谢",
        keywords=("谢谢", "感谢", "感恩", "多亏", "帮了我", "记得", "幸运", "遇到过", "支持我", "感激", "惦记"),
        tempo_bpm=84, root_midi=60, scale=MAJOR_PENTATONIC,
        progression=((0, "maj7"), (5, "maj7"), (7, "min7"), (0, "maj7")),
        lead_timbre=TIMBRE_PIANO, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.26, melody_density=0.62, reverb_amount=0.32,
        style_words=("heartwarming", "acoustic", "hopeful", "folk"),
    ),
    EmotionProfile(
        code="hope", label="期待", label_en="Hope", color="#5AC8FA", icon="🌱",
        valence=0.55, arousal=0.58, description="向前看的、慢慢升起的希望",
        keywords=("期待", "希望", "明天", "新征程", "计划", "出发", "未来", "打算", "第一次"),
        tempo_bpm=98, root_midi=62, scale=LYDIAN,
        progression=((0, "maj7"), (2, "maj7"), (5, "maj"), (7, "min7")),
        lead_timbre=TIMBRE_BELL, pad_timbre=TIMBRE_ORGAN,
        bass_gain=0.28, melody_density=0.70, reverb_amount=0.30,
        style_words=("hopeful", "cinematic rise", "inspirational", "uplifting"),
    ),
    EmotionProfile(
        code="moved", label="感动", label_en="Moved", color="#FFA8A8", icon="✨",
        valence=0.45, arousal=0.55, description="眼眶发热的、宏大的情绪涌动",
        keywords=("感动", "哭了", "眼泪", "鼻子酸", "破防", "致敬", "八年", "终于", "不容易", "泪目"),
        tempo_bpm=82, root_midi=57, scale=LYDIAN,
        progression=((0, "maj7"), (7, "min9"), (5, "maj7"), (9, "min7")),
        lead_timbre=TIMBRE_STRINGS, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.28, melody_density=0.60, reverb_amount=0.42,
        style_words=("emotional", "string orchestra", "cinematic swell", "tearjerker"),
    ),
    EmotionProfile(
        code="relief", label="释然", label_en="Relief", color="#9BD0FF", icon="🍃",
        valence=0.42, arousal=0.32, description="终于放下的、长出口气的轻松",
        keywords=("释然", "放下了", "松了口气", "无所谓了", "算了", "结束", "交差", "如释重负", "告别", "总算", "松一口气", "长舒一口气"),
        tempo_bpm=80, root_midi=60, scale=DORIAN,
        progression=((0, "maj7"), (2, "min7"), (7, "maj7"), (5, "maj7")),
        lead_timbre=TIMBRE_FLUTE, pad_timbre=TIMBRE_ORGAN,
        bass_gain=0.24, melody_density=0.55, reverb_amount=0.34,
        style_words=("breathy", "relaxed", "lo-fi", "calm down"),
    ),

    # ---------------- 中性 ----------------
    EmotionProfile(
        code="neutral", label="平静", label_en="Neutral", color="#9AA0A6", icon="😐",
        valence=0.0, arousal=0.40, description="没什么起伏的普通一天",
        keywords=("一般", "还行", "普通", "日常", "没什么", "照常", "平淡"),
        tempo_bpm=86, root_midi=60, scale=MAJOR_PENTATONIC,
        progression=((0, "maj7"), (5, "maj7"), (2, "min7"), (7, "maj7")),
        lead_timbre=TIMBRE_PIANO, pad_timbre=TIMBRE_ORGAN,
        bass_gain=0.24, melody_density=0.55, reverb_amount=0.30,
        style_words=("neutral", "ambient piano", "lo-fi", "study music"),
    ),
    EmotionProfile(
        code="nostalgia", label="怀念", label_en="Nostalgia", color="#C6A6E8", icon="🕰️",
        valence=0.05, arousal=0.36, description="隔着距离回看的旧时光",
        keywords=("怀念", "想以前", "以前", "小时候", "那时候", "曾经", "回忆", "老地方", "又想起", "再也"),
        tempo_bpm=74, root_midi=57, scale=DORIAN,
        progression=((0, "min7"), (5, "min7"), (7, "maj7"), (3, "maj7")),
        lead_timbre=TIMBRE_BELL, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.24, melody_density=0.52, reverb_amount=0.44,
        style_words=("nostalgic", "vintage tape", "music box", "wistful"),
    ),

    # ---------------- 负向情绪 ----------------
    EmotionProfile(
        code="sadness", label="忧伤", label_en="Sadness", color="#5B8DEF", icon="😢",
        valence=-0.65, arousal=0.32, description="缓慢下沉的、想哭的低落",
        keywords=("难过", "伤心", "失落", "哭", "难受", "委屈", "不开心", "低落", "空落落",
                   "分手", "心碎", "emo", "sad"),
        tempo_bpm=64, root_midi=57, scale=NATURAL_MINOR,
        progression=((0, "min7"), (3, "maj7"), (7, "min7"), (10, "maj7")),
        lead_timbre=TIMBRE_PIANO, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.26, melody_density=0.45, reverb_amount=0.46,
        style_words=("melancholic", "slow piano", "rainy day", "minor key"),
    ),
    EmotionProfile(
        code="loneliness", label="孤独", label_en="Loneliness", color="#7E93B8", icon="🌙",
        valence=-0.55, arousal=0.24, description="空房间里的、只有回声的安静",
        keywords=("孤独", "一个人", "没人", "只剩", "安静得", "空荡荡", "没人陪", "深夜", "没人说话", "只有我"),
        tempo_bpm=58, root_midi=55, scale=MINOR_PENTATONIC,
        progression=((0, "min9"), (7, "min7"), (3, "maj7"), (10, "min7")),
        lead_timbre=TIMBRE_FLUTE, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.22, melody_density=0.38, reverb_amount=0.52,
        style_words=("lonely", "solitude", "ambient drones", "night"),
    ),
    EmotionProfile(
        code="fatigue", label="疲惫", label_en="Fatigue", color="#8D9AA5", icon="😮‍💨",
        valence=-0.35, arousal=0.20, description="电量耗尽的、只想躺平的疲倦",
        keywords=("累", "疲惫", "困", "没力气", "加班", "通宵", "撑不住", "筋疲力尽", "瘫", "睡不够"),
        tempo_bpm=54, root_midi=53, scale=MINOR_PENTATONIC,
        progression=((0, "min7"), (5, "min7"), (10, "maj7"), (7, "min7")),
        lead_timbre=TIMBRE_FLUTE, pad_timbre=TIMBRE_ORGAN,
        bass_gain=0.20, melody_density=0.32, reverb_amount=0.48,
        style_words=("weary", "slow ambient", "drone", "sleep music"),
    ),
    EmotionProfile(
        code="confusion", label="迷茫", label_en="Confusion", color="#B39DDB", icon="🌫️",
        valence=-0.25, arousal=0.46, description="看不清方向的悬浮感",
        keywords=("迷茫", "不知道", "怎么办", "纠结", "不确定", "没方向", "摆烂", "困惑", "意义", "选择"),
        tempo_bpm=70, root_midi=58, scale=DORIAN,
        progression=((0, "min7"), (2, "maj7"), (5, "min7"), (10, "maj7")),
        lead_timbre=TIMBRE_BELL, pad_timbre=TIMBRE_ORGAN,
        bass_gain=0.22, melody_density=0.45, reverb_amount=0.50,
        style_words=("uncertain", "suspended chords", "ambient", "drifting"),
    ),
    EmotionProfile(
        code="stress", label="压力", label_en="Stress", color="#6C7CE8", icon="📌",
        valence=-0.50, arousal=0.74, description="deadline 逼近的紧绷感",
        keywords=("压力", "ddl", "deadline", "催", "加班", "做不完", "赶", "进度", "汇报", "时间不够"),
        tempo_bpm=108, root_midi=58, scale=NATURAL_MINOR,
        progression=((0, "min7"), (10, "maj7"), (8, "maj7"), (7, "min7")),
        lead_timbre=TIMBRE_ORGAN, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.34, melody_density=0.85, reverb_amount=0.22,
        style_words=("tense", "pulsing", "minimal techno undertones", "anxious"),
    ),
    EmotionProfile(
        code="anxiety", label="焦虑", label_en="Anxiety", color="#7E57C2", icon="😰",
        valence=-0.60, arousal=0.85, description="停不下来的内耗与担忧",
        keywords=("焦虑", "紧张", "慌", "担心", "害怕", "睡不着", "心慌", "不安", "怕", "万一",
                   "心率", "心跳加速", "手抖"),
        tempo_bpm=112, root_midi=59, scale=NATURAL_MINOR,
        progression=((0, "min7"), (1, "maj7"), (0, "min7"), (10, "maj7")),
        lead_timbre=TIMBRE_BELL, pad_timbre=TIMBRE_ORGAN,
        bass_gain=0.32, melody_density=0.90, reverb_amount=0.40,
        style_words=("anxious", "uneasy", "arpeggio", "dark ambient"),
    ),
    EmotionProfile(
        code="anger", label="愤怒", label_en="Anger", color="#E5533D", icon="😠",
        valence=-0.78, arousal=0.90, description="需要出口的、有攻击性的火气",
        keywords=("生气", "愤怒", "气死", "气得", "凭什么", "烦死了", "忍不了", "太过分", "不公平",
                   "受够了", "摔", "砸", "吵架", "被骂", "拉黑", "炸毛"),
        tempo_bpm=136, root_midi=56, scale=NATURAL_MINOR,
        progression=((0, "min"), (10, "maj"), (8, "maj"), (7, "min")),
        lead_timbre=TIMBRE_ORGAN, pad_timbre=TIMBRE_STRINGS,
        bass_gain=0.40, melody_density=0.95, reverb_amount=0.18,
        style_words=("aggressive", "distorted bass", "industrial", "intense"),
    ),
)

# ---------------------------------------------------------------------------
# 建索引：code 字符串 → Profile 对象
# 这是本项目里最常用的一次查表操作，用字典做成 O(1)
# ---------------------------------------------------------------------------
EMOTION_CATALOG: Dict[str, EmotionProfile] = {p.code: p for p in _EMOTION_PROFILES}

# 情绪代号的有序列表（保持上面定义的顺序，前端渲染色板时用）
EMOTION_CODES: List[str] = [p.code for p in _EMOTION_PROFILES]

# 兜底情绪：任何识别不出来 / 大模型返回非法值的情况都落到这里
DEFAULT_EMOTION_CODE = "neutral"


def get_profile(emotion_code: str) -> EmotionProfile:
    """
    根据情绪代号取到配置文件。

    为什么不用 EMOTION_CATALOG[code] 直接取？
    因为大模型可能返回我们字典里没有的词（幻觉），直接取会 KeyError 崩掉。
    这里做了兜底：查不到就返回 neutral。

    参数:
        emotion_code: 形如 "joy" / "sadness"
    返回:
        EmotionProfile 对象（永不返回 None）
    """
    if not emotion_code:
        return EMOTION_CATALOG[DEFAULT_EMOTION_CODE]
    normalized = str(emotion_code).strip().lower()
    if normalized in EMOTION_CATALOG:
        return EMOTION_CATALOG[normalized]
    # 再做一次模糊匹配：模型可能返回中文标签，比如 "忧伤"
    for profile in _EMOTION_PROFILES:
        if profile.label == normalized or profile.label_en.lower() == normalized:
            return profile
    return EMOTION_CATALOG[DEFAULT_EMOTION_CODE]


def list_emotion_options() -> List[Dict[str, object]]:
    """
    给前端用的情绪清单（渲染图例、下拉框）。
    返回的是纯字典列表，可以直接被 json.dumps 序列化。
    """
    return [
        {
            "code": p.code,
            "label": p.label,
            "color": p.color,
            "icon": p.icon,
            "valence": p.valence,
            "arousal": p.arousal,
        }
        for p in _EMOTION_PROFILES
    ]


@dataclass
class EmotionResult:
    """
    一次情绪分析的**结果对象**（不是配置，是运行时数据）。

    后端各层之间传输情绪分析结果时统一用这个结构，
    相当于 C++ 里一个 POD 结构体。
    """

    primary: str                              # 主情绪代号，如 "joy"
    tags: List[str] = field(default_factory=list)      # 次要情绪标签（0~3 个）
    valence: float = 0.0                      # 效价 -1 ~ 1
    arousal: float = 0.4                      # 唤醒度 0 ~ 1
    intensity: float = 0.5                    # 情绪强烈程度 0 ~ 1
    summary: str = ""                         # 一句话情绪解读（给用户看的）
    music_prompt: str = ""                    # 英文音乐生成提示词（给音乐 API 用的）
    source: str = "local_rule"                # 来源标记：llm / local_rule
    raw_response: str = ""                    # 大模型原始返回，方便排查问题

    @property
    def profile(self) -> EmotionProfile:
        """直接拿到主情绪对应的音乐参数配置，省得调用方再查一次表。"""
        return get_profile(self.primary)
