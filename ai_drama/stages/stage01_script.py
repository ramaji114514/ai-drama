"""阶段 1：剧本。

    idea（一句话）-> 场次 + 镜头表（结构化 JSON）

这一步产出的是**全项目唯一的"真相源"**：后面所有阶段都只读它。
所以它必须落盘（episode.json）并且**人工可编辑** ---
你觉得第 7 镜不对，直接改 JSON，然后 `resume` 从后面的阶段接着跑。

对应的创意侧资产：skills/script.md、skills/storyboard.md
（提示词模板放在 skills/ 里，代码只负责调用和校验，不写死创作方法）
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import paths
from ..core.context import StageContext, StageReport
from ..core.media import estimate_shot_seconds
from ..core.models import Character, Episode, Scene, Shot
from ..providers.base import ProviderError


def build_prompt(idea: str, *, style: str = "", scene_count: int = 3,
                 shots_per_scene: int = 4, scripture: str = "") -> str:
    """把 idea 包装成给文本模型的指令。

    刻意把"镜头数/场次数"作为**显式参数**而不是让模型自由发挥：
    短剧的时长是产品约束，不是创作自由度。模型决定"拍什么"，
    骨架决定"有多少格"。

    `scripture` 是项目的角色圣经片段（见 core/project.py）。它进提示词是为了
    让模型**用对**在册角色；就算模型不听话，合并那一步也会把外观改回圣经里的值。
    """
    style_line = f"\n画面风格要求：{style}" if style else ""
    # 角色圣经接在"想法"后面、硬性要求前面：模型先看到"这部剧已经有哪些人、
    # 长什么样"，再去排场次。放到最后面容易被当成补充说明忽略掉。
    bible_line = f"\n\n{scripture}" if scripture else ""
    return f"""把一个想法展开成一部竖屏短剧的分镜表。

想法：{idea}{style_line}{bible_line}

硬性要求：
1. 共 {scene_count} 场，每场 {shots_per_scene} 个镜头，严格按顺序编号。
2. 输出严格 JSON，不要任何解释文字，结构如下：
{{
  "title": "片名",
  "logline": "一句话梗概",
  "style": "整体视觉风格（色调/镜头语言/质感）",
  "characters": [
    {{"name": "角色名", "desc": "年龄性别性格外貌", "anchor_prompt": "用于生成定妆照的提示词，纯视觉描述"}}
  ],
  "scenes": [
    {{
      "scene_id": "sc01",
      "title": "场次标题",
      "summary": "这一场发生什么",
      "location": "地点",
      "time_of_day": "时间",
      "shots": [
        {{
          "order": 0,
          "framing": "establishing|wide|medium|close|extreme_close",
          "action": "画面里发生什么（纯视觉）",
          "dialogue": "这一镜的台词，没有就留空字符串",
          "characters": ["这一镜画面里**看得见**的角色名，按重要性排序"],
          "location": "本镜地点"
        }}
      ]
    }}
  ]
}}

重要约束：
- `action` 和后续的画面提示词里**绝对不能出现台词、音效、音乐、旁白**等听觉信息，
  画面提示词只描述看得见的东西。
- 台词统一放 `dialogue` 字段，它会被送进 TTS，不会进画面模型。
- `characters` 必须是 `characters` 顶层数组里出现过的名字，**逐字照抄**。
  只写画面里真的看得见的人：空镜、只闻其声、画外音都留空数组。
  这个字段决定后续拿谁的定妆照去锁脸，写错不会报错，只会让人看到一张陌生的脸。
- 最后一个镜头必须留悬念（短剧的钩子）。"""


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="script")
    ep_json = paths.episode_json(ctx.root, ctx.run.run_id)

    if ep_json.exists() and not ctx.force:
        # 已完成过：直接读回来，保证幂等重跑不会浪费一次 LLM 调用
        data = json.loads(ep_json.read_text(encoding="utf-8"))
        loaded = Episode.from_dict(data)
        ctx.episode.characters = loaded.characters
        ctx.episode.scenes = loaded.scenes
        ctx.episode.title = loaded.title
        ctx.episode.logline = loaded.logline
        ctx.episode.style = loaded.style
        ctx.store.save_episode(ctx.episode)
        ctx.store.sync_shots(ctx.run.run_id, ctx.episode)
        report.skipped = len(ctx.episode.shots)
        report.notes.append(f"episode.json 已存在，跳过生成（{ep_json}）")
        report.artifacts["episode"] = str(ep_json)
        return report

    scene_count = int(ctx.run.meta.get("scenes", 3))
    shots_per_scene = int(ctx.run.meta.get("shots_per_scene", 4))
    scripture = ctx.project.scripture_block() if ctx.project is not None else ""
    prompt = build_prompt(
        ctx.run.idea,
        style=ctx.run.meta.get("style", ""),
        scene_count=scene_count,
        shots_per_scene=shots_per_scene,
        scripture=scripture,
    )

    ctx.log(f"[script] 调用 {ctx.bundle.text.name} 生成分镜…")
    # 把格数一起传给 provider：不传的话，dryrun 之类的确定性实现
    # 会退回到自己的默认值（3 场 × 4 镜），于是 `--scenes 1` 静默失效 —
    # 用户以为改的是产品参数，结果什么都没变。
    text, usage = ctx.bundle.text.generate(
        prompt, system="短剧编剧引擎", scene="script",
        scene_count=scene_count, shots_per_scene=shots_per_scene,
    )
    ctx.ledger.add(usage, stage="script")
    report.money += float(usage.get("money") or 0)

    payload = _parse(text)
    ep = _to_episode(ctx, payload)

    # ———————————————— 与角色圣经合并
    # 在册角色的名字/外貌/定妆照**一律以圣经为准**，模型这一集写了什么都不算数。
    # 这是跨集一致性的第一道保障（第二道在 stage02：复用同一张定妆照文件）。
    if ctx.project is not None:
        cast, stats = ctx.project.merge_cast(ep.characters)
        ep.characters = cast
        # 新登场的角色立刻入册，下一集它就变成在册角色。
        # 定妆照要等 stage02 生成，那时再 remember_portrait()。
        ctx.save_project()
        report.notes.append(
            f"角色圣经：沿用 {stats['locked']} 个在册角色"
            + (f"，新入册 {stats['new']} 个" if stats["new"] else "")
        )

    ctx.store.save_episode(ep)
    ctx.store.sync_shots(ctx.run.run_id, ep)
    ep_json.parent.mkdir(parents=True, exist_ok=True)
    ep_json.write_text(
        json.dumps(ep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 把解析好的结果回写到 ctx.episode，供本进程内后续阶段直接使用
    ctx.episode.title = ep.title
    ctx.episode.logline = ep.logline
    ctx.episode.style = ep.style
    ctx.episode.characters = ep.characters
    ctx.episode.scenes = ep.scenes

    report.processed = len(ep.shots)
    report.notes.append(f"{len(ep.scenes)} 场 / {len(ep.shots)} 镜 / 角色 {len(ep.characters)} 个")
    report.artifacts["episode"] = str(ep_json)
    ctx.log(f"[script] [OK] {report.notes[-1]}")
    return report


# ————————————————————————————————————————----


def _parse(text: str) -> Dict[str, Any]:
    """从模型回复里抠出 JSON。

    模型经常在 JSON 外面包一层 ```json ``` 或加一句"好的，这是分镜表"。
    这里做三层兜底，第三层才报错 — 但**一定报错**，
    绝不返回一份写死的假剧本（参考项目的致命反模式）。
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if len(raw.split("```")) > 1 else raw
        raw = raw[4:] if raw.lower().startswith("json") else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"text provider 返回的不是合法 JSON（{exc}）。"
                f"回复前 400 字：{text[:400]!r}",
                retryable=True,
                provider="text",
            ) from exc
    raise ProviderError(
        f"text provider 回复里找不到 JSON。前 400 字：{text[:400]!r}",
        retryable=True,
        provider="text",
    )


def _cast_of(sh: Dict[str, Any]) -> Optional[List[str]]:
    """这一镜的出镜角色。**None 和 [] 必须分开**：

      * 字段没给 / 给了 null -> None（"没声明"，骨架退回名字子串猜）
      * 给了空数组         -> []（"画面里没有人"，空镜）

    合并成一种就会丢掉一个真实状态：要么空镜被迫锁一张不该出现的脸，
    要么所有旧 episode.json 都被当成空镜、一个人都不锁。
    """
    raw = sh.get("characters")
    return None if raw is None else _names(raw)


def _names(raw: Any) -> List[str]:
    """把模型给的 `characters` 洗成干净的角色名列表。

    模型有时给逗号分隔的字符串而不是数组，有时给个 null，有时重复。
    这里一律吃掉 —— 但**绝不把非字符串塞进去**：一个 dict 溜进来会让后面
    按名字查表的地方静默失配。
    """
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,，、/|;；]", raw)]
    out: List[str] = []
    for name in raw:
        if not isinstance(name, str):
            continue
        name = name.strip().strip("「」\"' ")
        if name and name not in out:
            out.append(name)
    return out


def _to_episode(ctx: StageContext, payload: Dict[str, Any]) -> Episode:
    ep = Episode(
        run_id=ctx.run.run_id,
        idea=ctx.run.idea,
        title=payload.get("title") or ctx.run.idea[:20],
        logline=payload.get("logline", ""),
        style=payload.get("style", ""),
    )
    for c in payload.get("characters", []) or []:
        if not c.get("name"):
            continue
        ep.characters.append(
            Character(
                name=c["name"],
                desc=c.get("desc", ""),
                anchor_prompt=c.get("anchor_prompt", "") or c.get("desc", ""),
            )
        )

    for si, sc in enumerate(payload.get("scenes", []) or []):
        scene = Scene(
            scene_id=sc.get("scene_id") or f"sc{si + 1:02d}",
            title=sc.get("title", ""),
            summary=sc.get("summary", ""),
            location=sc.get("location", ""),
            time_of_day=sc.get("time_of_day", ""),
        )
        for oi, sh in enumerate(sc.get("shots", []) or []):
            shot = Shot(
                scene_id=scene.scene_id,
                order=int(sh.get("order", oi)),
                action=sh.get("action", ""),
                dialogue=sh.get("dialogue", "") or "",
                framing=sh.get("framing", "medium"),
                # 区分"没声明"（None）和"声明画面里没人"（[]）：
                # 两者都退回去猜的话，"这是一镜空镜"这句话就白说了。
                characters=_cast_of(sh),
            )
            shot.duration = estimate_shot_seconds(shot)
            scene.shots.append(shot)
        if scene.shots:
            ep.scenes.append(scene)

    if not ep.scenes:
        raise ProviderError(
            "分镜表里一场戏都没有 — 拒绝产出空片。检查 text provider 的输出格式。",
            retryable=False,
            provider="text",
        )
    return ep
