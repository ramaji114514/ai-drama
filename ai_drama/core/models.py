"""ai-drama 领域模型。

只用标准库 dataclass，不引入 pydantic — 骨架的目标是零依赖可跑。
如果你要用 FastAPI 暴露成服务，把这些 dataclass 直接换成 pydantic BaseModel
即可，字段名保持不变，其余各层不用动。

设计约定：
  * 所有可持久化的对象都提供 to_dict / from_dict，落盘格式稳定。
  * 时间一律用 UTC ISO8601 字符串，跨平台不踩时区坑。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ————————————————————————————————————————----
# 剧本层
# ————————————————————————————————————————----


@dataclass
class Character:
    """角色设定。

    anchor_prompt 是"定妆照"用的提示词，相当于角色的视觉锚点。
    同一角色在后续所有镜头里都必须复用同一个 anchor_prompt，
    这是角色一致性的最小成本实现方式（见 docs/architecture.md）。
    """

    name: str
    desc: str = ""
    anchor_prompt: str = ""
    ref_image: Optional[str] = None  # 定妆照产出的本地路径
    #: 稳定身份。空表示"还没发过 id"，由 core.cast.ensure_ids() 按名字补上。
    #: 用可推导的值而不是随机值，是为了让老 project.json 读两次得到同一套 id。
    char_id: str = ""
    #: 这个身份的别的叫法：化名、称号、旧名。引用解析会一并认（core.cast）。
    aliases: List[str] = field(default_factory=list)

    def labels(self) -> List[str]:
        """这个身份的**全部叫法**：名字 + 别名（去重、去空）。

        名字只是这个身份当下的叫法，别名是它别的叫法（化名、称号、旧名）。
        查人、写提示词、报错文案都走这里，免得每处各自拼一遍。
        """
        out: List[str] = []
        for lbl in [self.name, *self.aliases]:
            lbl = (lbl or "").strip()
            if lbl and lbl not in out:
                out.append(lbl)
        return out

    def to_dict(self) -> Dict[str, Any]:
        # 手写而不是 asdict()：让 project.json 里 id 排在名字前面，
        # 人翻这个文件时第一眼看到的是身份，不是叫法。
        return {
            "char_id": self.char_id,
            "name": self.name,
            "aliases": list(self.aliases),
            "desc": self.desc,
            "anchor_prompt": self.anchor_prompt,
            "ref_image": self.ref_image,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Character":
        aliases = d.get("aliases") or []
        if isinstance(aliases, str):  # 手改项目文件时写成了一行
            aliases = [aliases]
        return cls(
            name=d.get("name") or "",
            desc=d.get("desc") or "",
            anchor_prompt=d.get("anchor_prompt") or "",
            ref_image=d.get("ref_image"),
            char_id=d.get("char_id") or "",
            aliases=[str(a) for a in aliases if a],
        )


@dataclass
class Shot:
    """一个镜头 — 全项目最核心的单位。

    时长与提示词在"分镜"阶段一次定稿，后面三个阶段（关键帧/视频/音频）
    都只读不写，这样任何一步失败都能单独重跑。
    """

    scene_id: str
    order: int
    action: str = ""            # 画面里发生什么（给视频模型）
    #: 这一镜**结束那一刻**画面是什么样（纯视觉）。
    #:
    #: 为什么不能从 action 推出来：action 描述的是"这一镜发生了什么"，
    #: 而首尾帧工具要的是"第一帧长什么样"和"最后一帧长什么样" —— 一个是
    #: 过程，一个是两个静止画面。从过程推结论只能靠猜，猜出来的尾帧会
    #: 让模型把动作再演一遍。空字符串表示模型没写，此时 compose_last_frame
    #: 会退化成"用 action 的末态"，并在报告里说清楚这是退化结果。
    end_action: str = ""
    dialogue: str = ""          # 台词（给 TTS，绝不允许拼进画面提示词）
    duration: float = 4.0       # 秒
    framing: str = "medium"     # establishing / close / medium / wide ...
    image_prompt: str = ""      # 关键帧提示词（无对白、无音效）
    video_prompt: str = ""      # 运动提示词（无对白、无音效）
    #: 尾帧提示词 —— 给"首帧+尾帧"这类视频工具用的第二个画面。
    #: 与 image_prompt（首帧）对称：同样的风格、角色锚点、景别，只是把
    #: 中间那句换成 end_action。end_action 为空时会退化成 action 并记一笔。
    last_frame: str = ""

    #: 负向提示词 —— 关键帧/首帧用。
    #:
    #: 为什么必须单独一个字段：可灵、即梦、Sora、Veo 全都有独立的「负向提示词」
    #: 输入框，而把"不要有畸形手"写进正向提示词是**反效果**的 —— 正向提示词里
    #: 每个名词都会被当成"要画的东西"，模型只认名词，不太认否定。
    #: 拼在正向里的"no watermark"因此经常换来一个水印。
    negative_prompt: str = ""

    #: 负向提示词 —— 视频用。和上面那条分开，因为两者防的不是一类事故：
    #: 静态图坏在解剖和版面（多指、水印），视频坏在**时间维度**上
    #: （变脸、闪烁、跳帧、动作僵在半路）。写在一起就是一段谁也不专心的词。
    video_negative: str = ""

    # 运行时状态（由 store 维护，断点续跑的依据）
    shot_id: str = field(default_factory=lambda: new_id("shot"))
    state: str = "pending"      # pending / running / done / failed / skipped
    keyframe: Optional[str] = None
    video: Optional[str] = None
    audio: Optional[str] = None
    #: 口型同步后的片段（视频阶段产出的是原始片段，这里是"对上嘴"的那一份）。
    #: 没有它不代表这一镜不需要同步 —— 也可能只是没配 lip-sync provider。
    #: 这两件事在报告里必须能分辨，见 stages/stage06_lipsync.py。
    synced: Optional[str] = None
    error: Optional[str] = None
    #: 这一镜**出镜的角色名**，由分镜阶段的模型显式给出，顺序即"谁在画面里更重要"。
    #:
    #: 为什么不让骨架从文本里猜：名字子串匹配是已知的薄弱点 —— `朱锦` 会匹配到
    #: `朱锦娘`（core/faces.py 里为此写了特判），而多角色同框时"谁的脸算基准"
    #: 立刻失效。更要紧的是**猜错不会报错**：它只会让人在成片里看到一张陌生的脸。
    #:
    #: 三种取值要分清楚，合并成一种就会丢掉一个真实状态：
    #:   * ``None`` —— 没声明（旧 episode.json，或模型漏了这个字段）-> 退回名字猜测
    #:   * ``[]``   —— 声明了"画面里没有人"（空镜）-> 谁都不锁，也不许退回去猜
    #:   * 有名字   —— 按这个顺序锁脸
    characters: Optional[List[str]] = None

    #: 这一镜的**音效**（环境声与动作声：雨声、门被撞开、心跳）。
    #:
    #: 为什么单独一个字段：音效是"画面之外"的信息，写进画面提示词就等于把它
    #: 喂给视频模型 —— 而质检里那条 `forbidden_in_visual` 规则正是为此存在的
    #: （把"音效"写进画面提示词会让模型去**画**一个音箱或一行字）。
    #: 所以声音必须走自己的字段：台词走 `dialogue`，环境声与动作声走这里。
    #:
    #: 空列表是正常的（大部分镜头没有特别的音效）。
    sfx: List[str] = field(default_factory=list)

    updated_at: str = field(default_factory=now_iso)

    @property
    def label(self) -> str:
        return f"{self.scene_id}/shot_{self.order:03d}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Shot":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class Scene:
    scene_id: str
    title: str = ""
    summary: str = ""
    location: str = ""
    time_of_day: str = ""
    shots: List[Shot] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["shots"] = [s.to_dict() if isinstance(s, Shot) else s for s in self.shots]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Scene":
        shots = [Shot.from_dict(s) for s in d.get("shots", [])]
        return cls(
            scene_id=d["scene_id"],
            title=d.get("title", ""),
            summary=d.get("summary", ""),
            location=d.get("location", ""),
            time_of_day=d.get("time_of_day", ""),
            shots=shots,
        )


@dataclass
class Episode:
    """一集 = 一次 run 的最终产物描述。"""

    run_id: str
    idea: str
    title: str = ""
    logline: str = ""
    style: str = ""
    characters: List[Character] = field(default_factory=list)
    scenes: List[Scene] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    @property
    def shots(self) -> List[Shot]:
        return [s for sc in self.scenes for s in sc.shots]

    @property
    def total_duration(self) -> float:
        return round(sum(s.duration for s in self.shots), 2)

    def character_by_name(self, name: str) -> Optional[Character]:
        for c in self.characters:
            if c.name == name:
                return c
        return None

    def character_by_ref(self, ref: str) -> Optional[Character]:
        """按 id / 名字 / 别名认人。同名不同人时返回 None（**不猜**）。

        实现在 `core.cast`：延迟 import 是为了不让 models 反向依赖 cast
        （cast 依赖 models 的 Character）。
        """
        from .cast import resolve

        return resolve(ref, self.characters).character

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "idea": self.idea,
            "title": self.title,
            "logline": self.logline,
            "style": self.style,
            "created_at": self.created_at,
            "total_duration": self.total_duration,
            "characters": [c.to_dict() for c in self.characters],
            "scenes": [s.to_dict() for s in self.scenes],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Episode":
        return cls(
            run_id=d["run_id"],
            idea=d.get("idea", ""),
            title=d.get("title", ""),
            logline=d.get("logline", ""),
            style=d.get("style", ""),
            created_at=d.get("created_at", now_iso()),
            characters=[Character.from_dict(c) for c in d.get("characters", [])],
            scenes=[Scene.from_dict(s) for s in d.get("scenes", [])],
        )


# ————————————————————————————————————————----
# 任务层
# ————————————————————————————————————————----

#: 阶段顺序**就是契约**（tests/test_models.py 锁着它）。
#: lipsync 必须在 audio 之后（要先有音轨才能对嘴）、concat 之前
#: （拼接要用对完嘴的片段）。
STAGES = ["script", "storyboard", "frames", "video", "audio", "lipsync", "concat", "qc"]

RUN_STATES = ["pending", "running", "done", "failed", "cancelled"]


@dataclass
class Run:
    run_id: str
    idea: str
    state: str = "pending"
    stage: str = "script"
    out_dir: str = ""
    final_video: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Run":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class CostEntry:
    """成本记账条目。骨架自己不做判断，只忠实记录。"""

    provider: str
    stage: str
    unit: str               # token / image / second / char
    amount: float
    money: float            # 折算成美元
    run_id: str = ""
    shot_label: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
