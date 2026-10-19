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
* 圣经按**身份**（`Character.char_id`）匹配角色，名字与别名只是这个身份的
  叫法 —— 同名不同人、改名、多重身份都不会再配错脸。引用的解析规则见
  `core/cast.py`。`locations`（场景/地点）仍然没有，形状同上。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths, voice
from .cast import ensure_ids, id_table, mint_id, resolve
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
    #: 角色 -> TTS 音色。键可以是 `char_id`（首选）、名字或别名。
    #: 同一个人每集必须用同一个声音，否则比脸漂还明显。
    voice_map: Dict[str, str] = field(default_factory=dict)
    #: 角色 -> 这个音色是**凭什么**定下来的（"男声（凭「42岁」判为adult）"）。
    #: 只记自动推断出来的那些；人手填的 `voice_map` 不写，因为人不需要向自己解释。
    #: 留在文件里是为了让"声音不对"这件事有个可以查、可以改的地方。
    voice_notes: Dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    # ————————————————————————————————————--- 查

    def character_by_name(self, name: str) -> Optional[Character]:
        for c in self.characters:
            if c.name == name:
                return c
        return None

    def character_by_ref(self, ref: str) -> Optional[Character]:
        """按 id / 名字 / 别名认人。名字有歧义时返回 None（**不猜**）。"""
        return resolve(ref, self.characters).character

    def voice_for(self, who: Any, fallback: str = "") -> str:
        """这个人用什么音色。

        `who` 可以是 `Character`、角色 id、名字或别名。音色表按 id 存，
        但**回落到名字**：手写的项目文件一般只写了名字，不能因为没填 id
        就让整部剧换嗓子。
        """
        c = who if isinstance(who, Character) else self.character_by_ref(str(who or ""))
        if c is not None:
            for key in (c.char_id, c.name, *c.aliases):
                if key and key in self.voice_map:
                    return self.voice_map[key]
        # 认不出这个人时，也允许直接用字符串查一次（老项目文件按名字存的表）
        if isinstance(who, str):
            return self.voice_map.get(who, fallback) or fallback
        return fallback

    def ensure_voices(
        self, chars: List[Character]
    ) -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, str]]]:
        """给圣经里还没定音色的角色补一个，并且**记下凭什么**。

        为什么要有这一步：`voice_map` 一直是空的，`voice_for()` 于是对所有人
        回落到同一个全局默认音色 —— 一部剧里苍老的男人和十七岁的少年会用
        同一个嗓子，而且**没有任何一层会报出来**。填一次就永久生效，因为
        音色和定妆照一样是跨集属性。

        只补不覆盖：人手写进 `voice_map` 的值（包括故意写错的）一律不动。
        定不出音色的也**不硬塞默认值** —— 返回在第二个返回值里，由调用方
        报出来，人看到了才知道该手填哪一条。

        返回 `([(名字, 音色, 依据)], [(名字, 为什么定不了)])`。
        """
        filled: List[Tuple[str, str, str]] = []
        unresolved: List[Tuple[str, str]] = []
        for c in chars:
            key = c.char_id or c.name
            if not key:
                continue
            # 已有音色（不管按 id、名字还是别名存的）就跳过
            if self.voice_for(c, fallback=""):
                continue
            guess = voice.suggest(c.desc or "", c.anchor_prompt or "")
            if guess is None:
                unresolved.append(
                    (
                        c.name,
                        voice.unresolved_reason(c.desc or "", c.anchor_prompt or ""),
                    )
                )
                continue
            self.voice_map[key] = guess.voice
            self.voice_notes[key] = guess.why
            filled.append((c.name, guess.voice, guess.why))
        return filled, unresolved

    def scripture_block(self) -> str:
        """给分镜阶段看的"角色圣经"片段。

        写成提示词而不是代码里的强校验，是因为**模型参与创作**：它要知道
        这些角色的存在，才可能在剧情里用对。但就算它不听话，合并那一步
        （merge_cast）也会把外观改回圣经里的值 —— 提示词负责"用对"，
        合并负责"改不坏"。

        片段里带上**角色表**（`id = 名字`），并要求镜头里的 `characters`
        填 id：这样第 5 集把名字改掉时，前面引用这个人的镜头不会一起失配。
        模型抄错也无所谓 —— 解析会退回到名字，并把降级记进报告。
        """
        if not self.characters:
            return ""
        lines = [
            "【角色圣经】这部剧的角色外观已经定稿并生成了定妆照，**必须沿用**："
        ]
        for c in self.characters:
            desc = c.desc or c.anchor_prompt
            locked = "（定妆照已锁定，不要重新描写外貌）" if c.ref_image else ""
            alias = f"（别名：{'、'.join(c.aliases)}）" if c.aliases else ""
            lines.append(f"- {c.name}：{desc}{alias}{locked}")
        table = id_table(self.characters)
        if table:
            lines.append(table)
        lines.append(
            "要求：`characters` 里必须原样带上以上**在册角色**（哪怕这一集它没戏），"
            "镜头里的出镜角色填**角色表里的 id**。"
            "`name` / `desc` / `anchor_prompt` 逐字照抄，不得改外貌。"
            "可以让新角色登场，新角色按你的设计写。"
        )
        return "\n".join(lines)

    # ————————————————————————————————————--- 合并

    def merge_cast(self, incoming: List[Character]) -> Tuple[List[Character], Dict[str, int]]:
        """把这一集模型给出的角色表，与圣经合并成本集的实际卡司。

        规则（顺序即优先级）：

        1. **在册角色一律以圣经为准** —— 外貌、定妆照、id 都取自圣经，
           模型这一集写了什么都不算数。它顶多能决定"这个人这一集出不出场"
           （而出场与否其实由台词和动作文本决定，见
           `stage02_storyboard.characters_in_shot`）。
        2. 所有在册角色都会进本集卡司，**即使模型忘了列**。否则一个在册角色
           只要模型漏写一次，这一集就锁不住脸；而漏写是模型最常见的失误。
        3. **按身份认人**：模型给了 id 就按 id 认；只给了名字/别名就按名字认。
           认出来之后，如果它给的名字与圣经不同，那个名字**记进别名**而不是
           改掉圣经 —— id 定身份，名字只是这个身份当下的叫法。
        4. 新名字按模型给的写，并回写进圣经（含新 id）—— 下一集它就是在册角色。

        返回 (卡司, 统计)。统计里的 `new` 是**这一次新入册**的角色，调用方据此
        决定要不要生成新定妆照。

        [!] 已知边界：名字写错一个字（苏曼 / 苏蔓）且模型没给 id 时，会被当成
        一个新角色入册。这是刻意的 —— 模糊匹配会把「朱锦」和「朱锦娘」合成
        一个人，那比多一个角色更难发现。报告里那句"新入册 N 个"就是给人看的信号。
        """
        ensure_ids(self.characters)                 # 老 project.json 补 id
        stats = {"locked": 0, "new": 0, "renamed": 0, "dropped": 0}
        cast: List[Character] = []

        # 1) 在册角色全部入卡司（以圣经为准）
        for canon in self.characters:
            cast.append(Character(
                name=canon.name,
                desc=canon.desc,
                anchor_prompt=canon.anchor_prompt,
                ref_image=canon.ref_image,
                char_id=canon.char_id,
                aliases=list(canon.aliases),
            ))
            stats["locked"] += 1

        by_id = {c.char_id: c for c in cast if c.char_id}

        # 2) 模型给的角色：认出来的忽略其外观，认不出的入册
        for inc in incoming:
            name = (inc.name or "").strip()
            cid = (inc.char_id or "").strip()
            if not name and not cid:
                continue

            hit: Optional[Character] = None
            if cid and cid in by_id:
                hit = by_id[cid]
            else:
                # 只给了名字（或别名）时按圣经解析，再按 id 找回卡司里的副本。
                found = resolve(name or cid, self.characters).character
                if found is not None:
                    hit = next((c for c in cast if c.char_id == found.char_id), None)

            if hit is None:
                fresh_id = mint_id(name or cid, [c.char_id for c in self.characters])
                fresh = Character(
                    name=name or cid, desc=inc.desc,
                    anchor_prompt=inc.anchor_prompt or inc.desc,
                    char_id=fresh_id,
                )
                cast.append(fresh)
                # 新角色也回写进圣经：下一集开始它就是在册角色
                self.characters.append(Character(
                    name=fresh.name, desc=fresh.desc,
                    anchor_prompt=fresh.anchor_prompt,
                    char_id=fresh_id,
                ))
                by_id[fresh_id] = fresh
                stats["new"] += 1
                continue

            # 在册：外观以圣经为准，但允许模型补充圣经里缺失的字段
            # （早期项目可能只有名字，没有 desc）
            if not hit.desc:
                hit.desc = inc.desc
            if not hit.anchor_prompt:
                hit.anchor_prompt = inc.anchor_prompt or inc.desc
            # 换了个叫法：记成别名，而不是改掉名字。id 没变，身份还是同一个人，
            # 于是前几集引用旧名字的镜头不会一起失联。
            for label in [name, *inc.aliases]:
                label = (label or "").strip()
                if label and label != hit.name and label not in hit.aliases:
                    hit.aliases.append(label)
                    self._remember_alias(hit.char_id, label)
                    stats["renamed"] += 1

        return cast, stats

    def _remember_alias(self, char_id: str, label: str) -> None:
        """把新学到的叫法写回圣经，这样下一集按旧名字也能认出这个人。"""
        for canon in self.characters:
            if canon.char_id == char_id and label not in canon.aliases:
                canon.aliases.append(label)
                return

    def remember_portrait(self, ref: str, ref_image: str) -> None:
        """把定妆照写进圣经 —— 第 2 集起复用同一张文件。"""
        canon = self.character_by_ref(ref) or self.character_by_name(ref)
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
            "voice_notes": dict(self.voice_notes),
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
            voice_notes=dict(d.get("voice_notes") or {}),
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
    # 老文件没有 char_id：读进来时按名字补上。幂等，所以不需要立刻回写 ——
    # 下一次 save()（merge_cast 之后必然发生）会把 id 落进文件。
    ensure_ids(proj.characters)
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
