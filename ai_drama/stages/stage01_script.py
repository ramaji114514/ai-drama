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
from typing import Any, Dict, List, Optional, Tuple

from ..core import cast as castlib, paths
from ..core.context import StageContext, StageReport
from ..core.media import estimate_shot_seconds, preferred_shot_seconds
from ..core.models import Character, Episode, Scene, Shot
from ..providers.base import ProviderError

#: 明显是"音乐"而不是"音效"的描述。BGM 是全片一条、走单独的字段，
#: 混进 sfx 会让每镜都去合成一段弦乐 —— 成本翻倍而且听起来很假。
_MUSIC_WORDS = re.compile(
    r"音乐|配乐|背景乐|bgm|BGM|弦乐|钢琴曲|旋律|主题曲|插曲|伴奏|music|soundtrack"
)


def build_prompt(idea: str, *, style: str = "", scene_count: int = 3,
                 shots_per_scene: int = 4, scripture: str = "",
                 notes: str = "") -> str:
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
    # 「额外要求」是**变体**用的：同一句话要出 N 个不同调度时，这是唯一让
    # 两次生成真正分开的输入。不给就完全不出现在提示词里。
    note_line = f"\n\n额外要求：{notes}" if notes.strip() else ""
    return f"""把一个想法展开成一部竖屏短剧的分镜表。

想法：{idea}{style_line}{bible_line}{note_line}

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
          "end_action": "这一镜**结束那一刻**画面是什么样（纯视觉）。要能看出和 action 的差别：如果 action 是「他推开门走进来」，end_action 就写「他已经站在柜台前，手还扶在门把上」。只写和 action 不同的那部分，不要重复一遍",
          "dialogue": "这一镜的台词，没有就留空字符串",
          "sfx": ["这一镜的**音效**（环境声与动作声），没有就留空数组"],
          "characters": ["这一镜画面里**看得见**的角色 id（有角色表时）或名字，按重要性排序"],
          "location": "本镜地点"
        }}
      ]
    }}
  ]
}}

重要约束：
- `action` 和后续的画面提示词里**绝对不能出现台词、音效、音乐、旁白**等听觉信息，
  画面提示词只描述看得见的东西。把"音效"写进画面提示词会让模型去**画**一个
  音箱或一行字 —— 质检会判 block。
- 台词统一放 `dialogue` 字段，它会被送进 TTS，不会进画面模型。
  **有人说话就必须写成「说话人：台词」**（例：`苏蔓：（低声）不对。`）。
  配音要按说话人挑嗓子，没有前缀就只能全片一个声音 —— 一个苍老的男人
  用稚气的女声，观众一耳朵就出戏。画外音也要写清是谁。
  一句台词不要超过 20 个字：竖屏短剧一句话一口气说完。
- `sfx` 放**环境声与动作声**：雨声、门被撞开、玻璃碎、心跳、脚步。
  写具体的、能听见的声音，一条 2-8 个字（"夜雨落在铁皮棚上"、"门被撞开"）。
  **不要写音乐**（BGM 是全片一条，走单独的字段），也不要写台词里已经有的东西。
  大部分镜头可以留空数组 —— 每一条音效都是一次真实调用，别为了填满而填。
- `characters` 必须是顶层 `characters` 数组里出现过的角色 —— **有角色表就填表里的 id**
  （逐字照抄），没有角色表就填名字。
  只写画面里真的看得见的人：空镜、只闻其声、画外音都留空数组。
  这个字段决定后续拿谁的定妆照去锁脸，写错不会报错，只会让人看到一张陌生的脸。
- 最后一个镜头必须留悬念（短剧的钩子）。"""


#: 生成完一集之后要写回 `ctx.episode` 的字段。
#:
#: 为什么列成一张表而不是随手写五六行 setattr：`Episode` 加字段时，
#: 忘的**恰恰是这里** —— 少写一行不会报错，只会在下游静静读到旧值
#: （target_seconds 就这么漏过一次）。tests/test_target_duration.py 里
#: 有一条用例拿 dataclass 字段对着这张表验一遍，漏了当场红。
ADOPTED_FIELDS = ("title", "logline", "style", "characters", "scenes",
                  "target_seconds")


def _adopt(ctx: StageContext, ep: Episode) -> None:
    for name in ADOPTED_FIELDS:
        setattr(ctx.episode, name, getattr(ep, name))


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="script")
    ep_json = paths.episode_json(ctx.root, ctx.run.run_id)

    if ep_json.exists() and not ctx.force:
        # 已完成过：直接读回来，保证幂等重跑不会浪费一次 LLM 调用
        data = json.loads(ep_json.read_text(encoding="utf-8"))
        loaded = Episode.from_dict(data)
        _adopt(ctx, loaded)
        ctx.store.save_episode(ctx.episode)
        ctx.store.sync_shots(ctx.run.run_id, ctx.episode)
        report.skipped = len(ctx.episode.shots)
        report.notes.append(f"episode.json 已存在，跳过生成（{ep_json}）")
        report.artifacts["episode"] = str(ep_json)
        ctx.asset(ep_json, kind="episode", via="text",
                  meta={"scenes": len(ctx.episode.scenes),
                        "shots": len(ctx.episode.shots), "reused": True})
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
        notes=str(ctx.run.meta.get("notes") or ""),
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

    # ———————————————— 补尾帧
    # `end_action` 是尾帧提示词的唯一来源。缺了它，stage02 只能拿 action 顶替，
    # 首尾两帧同文 —— 粘进图生图出来是同一张图，尾帧等于没有。
    # 一次写十几镜时模型经常把这个字段漏掉（schema 里要求了也漏），
    # 所以对漏掉的那几镜**再问一次**，只要它补这一段。
    # 输出很短，比整段重写便宜得多，也不会把已经写好的分镜搅乱。
    missing = [s for s in ep.shots if not (s.end_action or "").strip()]
    if missing and ctx.bundle.text.name != "dryrun":
        got, extra = _fill_end_actions(ctx, missing)
        report.add_money(extra, provider=ctx.bundle.text.name)
        if got:
            report.notes.append(f"尾帧：模型漏了 end_action，补写了 {got}/{len(missing)} 镜")
        left = len(missing) - got
        if left:
            report.notes.append(
                f"尾帧：[!] 还有 {left} 镜补不上，那几镜的首尾两帧会是同一张图"
            )

    # ———————————————— 与角色圣经合并
    # 在册角色的名字/外貌/定妆照**一律以圣经为准**，模型这一集写了什么都不算数。
    # 这是跨集一致性的第一道保障（第二道在 stage02：复用同一张定妆照文件）。
    if ctx.project is not None:
        cast, stats = ctx.project.merge_cast(ep.characters)
        ep.characters = cast
        # 音色和外观一样是跨集属性：给还没定音色的角色补一个，免得整部剧
        # 所有人共用同一个默认嗓子（见 core/voice.py）。
        # **必须放在 save_project() 之前** —— 晚于落盘就等于没写进去。
        filled, unresolved = ctx.project.ensure_voices(ep.characters)
        # 新登场的角色立刻入册，下一集它就变成在册角色。
        # 定妆照要等 stage02 生成，那时再 remember_portrait()。
        ctx.save_project()
        line = f"角色圣经：沿用 {stats['locked']} 个在册角色"
        if stats["new"]:
            line += f"，新入册 {stats['new']} 个"
        if stats["renamed"]:
            # 换了叫法：身份没变（id 还认得出），名字记成了别名
            line += f"，记下 {stats['renamed']} 个新叫法"
        report.notes.append(line)
        if filled:
            used = "、".join(f"{name}→{v}" for name, v, _ in filled)
            report.notes.append(f"音色：按角色描述定了 {len(filled)} 个（{used}）")
        for name, why in unresolved:
            report.notes.append(
                f"音色：[!] {name} {why}，这一集它会用全局默认音色"
                f"（{ctx.run.meta.get('voice') or 'female_01'}）—— 在 "
                f"projects/{ctx.project.project_id}/project.json 的 voice_map 里手填一个"
            )
    # 单集模式（没有 project）也要发 id：这样同一集里"同名不同人"能被发现，
    # 而不是等到了第 5 集才有人发现两张脸是同一个人。id 在这里是集内稳定的。
    minted = castlib.ensure_ids(ep.characters)
    if minted:
        ctx.log(f"[script] 为 {minted} 个角色分配了稳定 id")

    ctx.store.save_episode(ep)
    ctx.store.sync_shots(ctx.run.run_id, ep)
    ep_json.parent.mkdir(parents=True, exist_ok=True)
    ep_json.write_text(
        json.dumps(ep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 把解析好的结果回写到 ctx.episode，供本进程内后续阶段直接使用
    _adopt(ctx, ep)

    report.processed = len(ep.shots)
    report.notes.append(f"{len(ep.scenes)} 场 / {len(ep.shots)} 镜 / 角色 {len(ep.characters)} 个")
    report.artifacts["episode"] = str(ep_json)
    # 剧本进台账：往下每一张关键帧、每一句配音追到头都是它。
    # prompt 存的是**想法**（"暴雨夜，外卖员…"），那才是这一集的起点。
    ctx.asset(ep_json, kind="episode", via="text", prompt=ctx.run.idea,
              meta={"scenes": len(ep.scenes), "shots": len(ep.shots),
                    "characters": len(ep.characters)})
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


def _order_of(sh: Dict[str, Any], fallback: int) -> int:
    """模型写的 `order` 可能不是数字。

    这里**不抛异常**：一镜的序号写坏，不该让整集报废 —— 用它在数组里的位置
    顶上，顺序仍然是对的。`int("1st")` 会让整条流水线停在这里，而错因在
    四百行外的提示词里。
    """
    try:
        return int(sh.get("order", fallback))
    except (TypeError, ValueError):
        try:
            return int(str(sh.get("order", "")).strip().split(".")[0])
        except (TypeError, ValueError):
            return fallback


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


def _cues(raw: Any) -> List[str]:
    """洗 `sfx`：去重、去空白、丢掉非字符串，并且**砍掉音乐类描述**。

    模型会忍不住把 BGM 写进来（"紧张的低音弦乐"）—— 而 BGM 是全片一条、
    走单独的字段。放进来会让每镜都去合成一段"弦乐"，成本翻倍且听起来
    像有人在旁边一直按琴键。这里把明显的音乐描述挑掉，并在报告里说明。
    """
    cues = _names(raw)
    kept: List[str] = []
    for cue in cues:
        if _MUSIC_WORDS.search(cue):
            continue
        if len(cue) > 24:            # 一句话的描述不是音效，是镜头说明写错了地方
            continue
        kept.append(cue)
    return kept


def _fill_end_actions(ctx: StageContext, shots: List[Shot]) -> Tuple[int, float]:
    """让模型给缺 `end_action` 的镜头补上结束画面。返回 (补上了几镜, 花了多少钱)。

    为什么值得单独一次调用：`end_action` 决定尾帧提示词，而尾帧是"续拍"的接口 ——
    下一镜从首帧开始，接的就是上一镜的尾帧。它退化成 action 的复述，
    整条链的第二半就断了，而且断得很安静（不报错，只是两帧一样）。

    写不好的代价也只是"多花一次小调用"，比重新出一整集分镜便宜两个数量级。
    """
    lines = []
    for sh in shots:
        lines.append(f"- 场次 {sh.scene_id} · 第 {sh.order + 1} 镜（{sh.framing}）\n"
                     f"  动作：{sh.action}\n"
                     f"  台词：{sh.dialogue or '（无）'}")
    prompt = f"""下面这些镜头缺「结束那一刻的画面」。逐镜补上。

{chr(10).join(lines)}

要求：
- 只写这一镜**结束那一刻**的画面，纯视觉，不要写台词、音效、音乐、旁白
- 必须和「动作」看得出差别：动作写"推开门走进来"，结束画面就写
  "人已经站在柜台前，手还扶在门把上"
- 只写和动作不同的那部分，不要把动作重抄一遍
- 一句话，15-40 字

只输出 JSON：
{{"end_actions": [{{"scene_id": "sc01", "order": 0, "end_action": "……"}}]}}"""
    try:
        text, usage = ctx.bundle.text.generate(
            prompt, system="短剧编剧引擎", scene="script-end_action",
        )
    except ProviderError as exc:
        # 补不上不等于这一集报废 —— 报告里说不说清楚由调用方负责。
        ctx.log(f"[script] 补尾帧调用失败：{exc}")
        ctx.ledger.add({"provider": getattr(exc, "provider", "text") or "text",
                        "stage": "script", "unit": "call", "amount": 0,
                        "money": 0.0}, stage="script")
        return 0, 0.0
    ctx.ledger.add(usage, stage="script")
    money = float(usage.get("money") or 0)

    try:
        data = _parse(text)
    except ProviderError as exc:
        # 补丁没打上，不能把整集拖下水：剧本本身是好的，缺的只是尾帧。
        # 钱已经花了（上面已记账），所以这里返回 money 而不是 0。
        ctx.log(f"[script] 补尾帧的回复读不懂：{exc}")
        return 0, money
    rows = data.get("end_actions") or []
    by_key: Dict[Tuple[str, int], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        val = str(row.get("end_action") or "").strip()
        if not val:
            continue
        try:
            order = int(row.get("order", -1))
        except (TypeError, ValueError):
            continue
        by_key[(str(row.get("scene_id") or ""), order)] = val

    got = 0
    for sh in shots:
        val = by_key.get((sh.scene_id, sh.order))
        if val:
            sh.end_action = val
            got += 1
    return got, money


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
        aliases = c.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        ep.characters.append(
            Character(
                name=c["name"],
                desc=c.get("desc", ""),
                anchor_prompt=c.get("anchor_prompt", "") or c.get("desc", ""),
                # 模型照抄角色表时会把 id 带回来 —— 这就是"改名不失联"的凭据。
                # 它没给也无所谓，ensure_ids() 会补一个。
                char_id=str(c.get("char_id") or "").strip(),
                aliases=[str(a).strip() for a in aliases if str(a).strip()],
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
                order=_order_of(sh, oi),
                action=sh.get("action", ""),
                end_action=sh.get("end_action", "") or "",
                dialogue=sh.get("dialogue", "") or "",
                framing=sh.get("framing", "medium"),
                # 区分"没声明"（None）和"声明画面里没人"（[]）：
                # 两者都退回去猜的话，"这是一镜空镜"这句话就白说了。
                characters=_cast_of(sh),
                sfx=_cues(sh.get("sfx")),
            )
            scene.shots.append(shot)
        if scene.shots:
            ep.scenes.append(scene)

    # 时长在**所有镜头都在场**之后统一算：整部目标的均摊要除以总镜数，
    # 边parse边算会拿到"此刻已知的镜数"，前几镜被摊得比后几镜长。
    ep.target_seconds = float(ctx.run.meta.get("target_seconds") or 0.0)
    for sc in ep.scenes:
        for shot in sc.shots:
            shot.duration = estimate_shot_seconds(
                shot, preferred_s=preferred_shot_seconds(ep))

    if not ep.scenes:
        raise ProviderError(
            "分镜表里一场戏都没有 — 拒绝产出空片。检查 text provider 的输出格式。",
            retryable=False,
            provider="text",
        )
    return ep
