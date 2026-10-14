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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Character":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class Shot:
    """一个镜头 — 全项目最核心的单位。

    时长与提示词在"分镜"阶段一次定稿，后面三个阶段（关键帧/视频/音频）
    都只读不写，这样任何一步失败都能单独重跑。
    """

    scene_id: str
    order: int
    action: str = ""            # 画面里发生什么（给视频模型）
    dialogue: str = ""          # 台词（给 TTS，绝不允许拼进画面提示词）
    duration: float = 4.0       # 秒
    framing: str = "medium"     # establishing / close / medium / wide ...
    image_prompt: str = ""      # 关键帧提示词（无对白、无音效）
    video_prompt: str = ""      # 运动提示词（无对白、无音效）

    # 运行时状态（由 store 维护，断点续跑的依据）
    shot_id: str = field(default_factory=lambda: new_id("shot"))
    state: str = "pending"      # pending / running / done / failed / skipped
    keyframe: Optional[str] = None
    video: Optional[str] = None
    audio: Optional[str] = None
    error: Optional[str] = None
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

STAGES = ["script", "storyboard", "frames", "video", "audio", "concat", "qc"]

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
