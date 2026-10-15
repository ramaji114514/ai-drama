"""项目：一部剧的"角色圣经"，让第 40 集的脸还是第 1 集那张。

## 为什么需要它

在此之前的骨架里，`Episode` 就是顶层叙事单元，一次 run 出一集，角色由
分镜阶段的 LLM **每次重新发明**。这对一部 60–100 集的竖屏短剧是致命的：

    第 1 集 -> LLM 造出"苏蔓，31 岁，短发" -> 生成定妆照 A -> 全片用 A
    第 2 集 -> LLM 再造一个"苏蔓，30 岁，大波浪" -> 生成定妆照 B -> 全片用 B

两集各自内部是自洽的（每集都复用了自己的那张定妆照），**集与集之间不是**。
观众第 2 集就会说"换人了"。而且 `core/faces.py`、`core/identity.py` 那些
一致性工具全是**集内**的，跨集用不上。

## 设计取舍

**圣经是文件，不是数据库。** `projects/<id>/project.json` 和 `episode.json`
一个待遇：人手可改、改完直接生效。制片说"苏蔓改成短发"，改这个文件即可，
不需要动代码、不需要重跑第 1 集。

**跨集一致性的硬保证是"复用同一个文件"，不是"提示词写得像"。**
所以定妆照存在**项目**目录（`projects/<id>/portraits/苏蔓.png`），
第 2 集开始直接复用这个文件、根本不重新生成 —— 既不花钱，也不可能漂。
提示词层面的锁定（`core/identity.py`）是第二道防线，不是第一道。

**只冻结"外观"，不冻结"剧情"。** 圣经里的角色带 `desc`（年龄性别性格外貌）
与 `anchor_prompt`（纯视觉的定妆照提示词）。每一集里这个角色的**遭遇**由当集
剧本决定，外貌与名字由圣经决定。合并时以圣经为准的正是这两个字段。

**新角色可以登场。** 第 12 集出现一个新反派是正常创作，接口不该拦它。
新角色这一集正常生成定妆照，然后**回写进圣经** —— 于是第 13 集开始它也
自动变成"在册角色"。圣经是随剧集生长的，不是一开始就要写全。

## 已知边界

* 目前只包住**角色**（含外观与音色）。场景/地点、服装（同一角色不同集换装）
  还没有；`Scene.location` 目前每集自由生成。要加的话是同样的形状：
  在 Project 里加一个 `locations` 字典 + 一个可复用的场景参考图。
* 圣经按**名字**匹配角色。同名不同人、改名、多重身份（卧底）都会错配。
  真要做扎实，得给每个角色一个稳定 id，让分镜阶段输出 id 而不是名字。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .models import Character, new_id, now_iso


@dataclass
class Project:
    """一部剧跨集共用的设定。**文件是唯一真相**，没有数据库镜像。"""

    project_id: str
    title: str = ""
    logline: str = ""
    style: str = ""
    #: 角色圣经。每个角色的 `desc` / `anchor_prompt` 一经写定就不再让 LLM 改，
    #: 这样"这个人长什么样"不会随集数漂移。
    characters: List[Character] = field(default_factory=list)
    #: 角色名 -> TTS 音色。同一个人每集必须用同一个声音，否则比脸漂还明显。
    voice_map: Dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    # ————————————————————————————————————--- 查

    def character_by_name(self, name: str) -> Optional[Character]:
        for c in self.characters:
            if c.name == name:
                return c
        return None

    def voice_for(self, name: str, fallback: str = "") -> str:
        return self.voice_map.get(name or "", fallback) or fallback

    def scripture_block(self) -> str:
        """给分镜阶段看的"角色圣经"片段。

        写成提示词而不是代码里的强校验，是因为**模型参与创作**：它要知道
        这些角色的存在，才可能在剧情里用对。但就算它不听话，合并那一步
        （merge_cast）也会把外观改回圣经里的值 —— 提示词负责"用对"，
        合并负责"改不坏"。
        """
        if not self.characters:
            return ""
        lines = [
            "【角色圣经】这部剧的角色外观已经定稿并生成了定妆照，**必须沿用**："
        ]
        for c in self.characters:
            desc = c.desc or c.anchor_prompt
            locked = "（定妆照已锁定，不要重新描写外貌）" if c.ref_image else ""
            lines.append(f"- {c.name}：{desc}{locked}")
        lines.append(
            "要求：`characters` 里必须原样带上以上**在册角色**（哪怕这一集它没戏），"
            "`name` / `desc` / `anchor_prompt` 逐字照抄，不得改名、不得改外貌。"
            "可以让新角色登场，新角色按你的设计写。"
        )
        return "\n".join(lines)

    # ————————————————————————————————————--- 合并

    def merge_cast(self, incoming: List[Character]) -> Tuple[List[Character], Dict[str, int]]:
        """把这一集模型给出的角色表，与圣经合并成本集的实际卡司。

        规则（顺序即优先级）：

        1. **在册角色一律以圣经为准** —— 名字、外貌、定妆照都取自圣经，
           模型这一集写了什么都不算数。它顶多能决定"这个人这一集出不出场"
           （而出场与否其实由台词和动作文本决定，见
           `stage02_storyboard.characters_in_shot`）。
        2. 所有在册角色都会进本集卡司，**即使模型忘了列**。否则一个在册角色
           只要模型漏写一次，这一集就锁不住脸；而漏写是模型最常见的失误。
        3. 新名字按模型给的写，并回写进圣经 —— 下一集它就变成在册角色。

        返回 (卡司, 统计)。统计里的 `new` 是**这一次新入册**的角色，调用方据此
        决定要不要生成新定妆照。
        """
        stats = {"locked": 0, "new": 0, "dropped": 0}
        cast: List[Character] = []

        # 1) 在册角色全部入卡司（以圣经为准）
        for canon in self.characters:
            cast.append(Character(
                name=canon.name,
                desc=canon.desc,
                anchor_prompt=canon.anchor_prompt,
                ref_image=canon.ref_image,
            ))
            stats["locked"] += 1

        by_name = {c.name: c for c in cast}

        # 2) 模型给的角色：在册的忽略其外观，新名字入册
        for inc in incoming:
            name = (inc.name or "").strip()
            if not name:
                continue
            if name in by_name:
                # 在册：外观以圣经为准，但允许模型补充圣经里缺失的字段
                # （早期项目可能只有名字，没有 desc）
                canon = by_name[name]
                if not canon.desc:
                    canon.desc = inc.desc
                if not canon.anchor_prompt:
                    canon.anchor_prompt = inc.anchor_prompt or inc.desc
                continue
            fresh = Character(
                name=name, desc=inc.desc,
                anchor_prompt=inc.anchor_prompt or inc.desc,
            )
            cast.append(fresh)
            by_name[name] = fresh
            self.characters.append(Character(
                name=name, desc=fresh.desc, anchor_prompt=fresh.anchor_prompt,
            ))
            stats["new"] += 1

        return cast, stats

    def remember_portrait(self, name: str, ref_image: str) -> None:
        """把定妆照写进圣经 —— 第 2 集起复用同一张文件。"""
        canon = self.character_by_name(name)
        if canon is not None:
            canon.ref_image = ref_image

    # ————————————————————————————————————--- 落盘

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "logline": self.logline,
            "style": self.style,
            "characters": [c.to_dict() for c in self.characters],
            "voice_map": dict(self.voice_map),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Project":
        return cls(
            project_id=d.get("project_id") or new_id("proj"),
            title=d.get("title", ""),
            logline=d.get("logline", ""),
            style=d.get("style", ""),
            characters=[Character.from_dict(c) for c in d.get("characters", []) or []],
            voice_map=dict(d.get("voice_map") or {}),
            created_at=d.get("created_at") or now_iso(),
            updated_at=d.get("updated_at") or now_iso(),
        )

    def save(self, root: Path) -> Path:
        self.updated_at = now_iso()
        p = paths.project_json(root, self.project_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return p


def new_project(title: str, *, logline: str = "", style: str = "") -> Project:
    return Project(
        project_id=new_id("proj"), title=title, logline=logline, style=style
    )


def load(root: Path, project_id: str) -> Optional[Project]:
    """读一个项目；不存在返回 None（**不报错**）。

    调用方据此决定"没有项目"和"项目读坏了"是两件事：前者是正常用法
    （单集跑法），后者要大声说出来。读坏了会抛，不会静默返回一个空项目 ——
    静默返回空项目等于把一部 60 集的片子悄悄变成每集重新投胎。
    """
    p = paths.project_json(root, project_id)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    proj = Project.from_dict(data)
    if not proj.project_id:
        proj.project_id = project_id
    return proj


def list_projects(root: Path) -> List[Project]:
    """扫 `projects/` 目录。坏掉的项目**跳过并继续**，不让一个坏文件
    把整个列表命令打挂 —— 但调用方需要能知道跳过了什么，见 list_projects_lenient。"""
    out: List[Project] = []
    for proj, _err in list_projects_lenient(root):
        if proj is not None:
            out.append(proj)
    return out


def list_projects_lenient(root: Path) -> List[Tuple[Optional[Project], str]]:
    """返回 [(项目或 None, 错误说明)]。列表命令要能显示"这个坏了"。"""
    base = paths.projects_root(root)
    if not base.exists():
        return []
    out: List[Tuple[Optional[Project], str]] = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        try:
            proj = load(root, d.name)
        except Exception as exc:                      # noqa: BLE001
            out.append((None, f"{d.name}: {type(exc).__name__}: {exc}"))
            continue
        if proj is None:
            out.append((None, f"{d.name}: 没有 project.json"))
        else:
            out.append((proj, ""))
    return out


__all__ = [
    "Project", "new_project", "load", "list_projects", "list_projects_lenient",
]
