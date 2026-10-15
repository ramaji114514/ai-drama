"""阶段 2：分镜定稿。

这是**确定性骨架**和**创意判断**之间的接缝，也是整个项目最该由你来掌控的一层：

  * 时长：由 core/media.estimate_shot_seconds 用公式算（台词长度 + 景别），
    不是让模型随口给一个数字。你要调节奏，改公式参数，全片一起变。
  * 提示词：把"风格 + 角色外观 + 景别 + 地点 + 动作"拼成画面提示词；
    运动单独拼成视频提示词。
  * **听觉信息绝不进画面提示词** — 参考项目把 audio_desc 直接拼进了
    video prompt（script2video.py:190），这是概念性错误：
    你没法让画面模型"表演出"台词，只会污染画面。
  * **参考锁定**：把参考图**在提示词里点名**（`@图片1 = 角色「X」本人…`）
    并显式排除资产图版式。只把图传给模型不等于它会用 —— aigc-agent 的
    现场记录是"参考图传了也漂"。锁定段由 core/identity 生成。
  * 定妆照：为每个角色生成一张参考图，后续所有镜头复用同一张，
    这是**最低成本的跨镜头一致性方案**。

创意侧的提示词模板在 skills/storyboard.md，本文件只做拼装与校验。
"""

from __future__ import annotations

import json
from typing import List, Optional

from ..core import paths
from ..core.context import StageContext, StageReport
from ..core.faces import matches_character
from ..core.identity import shot_identity_prompt
from ..core.models import Character, Shot
from ..core.media import estimate_shot_seconds
from ..providers.base import caps_of

# 景别 -> 画面提示词片段
FRAMING_PROMPT = {
    "establishing": "远景定场，环境为主，人物很小",
    "wide": "全景，人物全身与环境关系清晰",
    "medium": "中景，人物腰部以上",
    "close": "特写，面部与情绪为主",
    "extreme_close": "极特写，局部细节充满画面",
}

CAMERA_MOVE = {
    "establishing": "镜头缓慢推进",
    "wide": "镜头稳定横移",
    "medium": "轻微手持跟随",
    "close": "固定机位，极缓推近",
    "extreme_close": "极缓推近后停顿",
}

NEGATIVE = "文字, 字幕, 水印, 畸形手指, 多手多脚, 面部崩坏, 低分辨率, 模糊"


def characters_in_shot(ep, shot: Shot) -> List[Character]:
    """找出镜头里出现的**全部**角色，按在镜头文本里出现的位置排序。

    两处刻意设计：

    1. 排序看"谁先被提到"，不看角色表的排列 —— 参考锁定段里的
       `@图片1` 必须对应真正传给模型的那张图，而"这张戏是谁的"
       应当由剧本决定。
    2. 名字匹配复用 `core.faces.matches_character`，它处理了
       `朱锦` 与 `朱锦娘` 这种包含关系（否则娘的脸会当成锦的基准）。

    [!] 名字子串匹配仍然是**已知的薄弱点**（参考项目就是这么做的，
    多角色场景立即失效）。要真正做多角色一致性，得让分镜阶段的模型
    显式输出 `shot.characters = [name, ...]`，再用脸/姿态检测回归验证。
    """
    blob = f"{shot.action} {shot.dialogue}"
    names = [c.name for c in ep.characters if c.name]
    hits = []
    for c in ep.characters:
        if c.name and matches_character(blob, c.name, names):
            hits.append((blob.find(c.name), c))
    return [c for _, c in sorted(hits, key=lambda t: t[0])]


def character_in_shot(ep, shot: Shot) -> Optional[Character]:
    """镜头里的**第一个**角色 —— 也就是关键帧拿谁的参考图。"""
    found = characters_in_shot(ep, shot)
    return found[0] if found else None


def reference_labels(ep, shot: Shot, max_refs: int = 1) -> List:
    """这一镜要在提示词里点名哪些参考图。

    **必须与 stage03 真正传给模型的图片同序同数**：多写一个 `@图片2`
    而只传了一张图，模型不会报错 —— 它会以为第二张就是它想象的任何东西。
    上限取 provider 自报的 `caps.max_ref_images`（见 core/catalog）。
    """
    found = [c for c in characters_in_shot(ep, shot) if c.ref_image]
    return [("角色", c.name) for c in found[:max(0, max_refs)]]


def compose_image_prompt(ep, shot: Shot, labels=()) -> str:
    parts = [ep.style or ""]
    ch = character_in_shot(ep, shot)
    if ch:
        parts.append(f"角色 {ch.name}：{ch.desc}")
    parts.append(FRAMING_PROMPT.get(shot.framing, "中景"))
    parts.append(shot.action)
    body = "，".join(p for p in parts if p)
    return shot_identity_prompt(body, labels)


def compose_video_prompt(shot: Shot) -> str:
    """运动提示词 — 只描述摄影机和画面元素的运动，不含任何听觉信息。

    这里**不加参考锁定段**，是有意的：视频 provider 收到的是这一镜的
    关键帧（`request.image_path`），而关键帧的构图正是我们要的画面本身。
    把资产图那套 "不要把白背景和站姿搬进来" 的排除条款套上去，
    只会让模型去改掉一个本来就对的构图。

    什么时候该加：如果哪天视频侧改成拿**角色资产图**做参考，
    就要用 identity.reference_block，并且注意顺序与图片列表对齐。
    """
    return f"{shot.action}。{CAMERA_MOVE.get(shot.framing, '镜头缓推')}。"


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="storyboard")
    ep = ctx.episode
    if not ep.scenes:
        report.notes.append("没有分镜可定稿（先跑 script 阶段）")
        return report

    # ————————————————————————————————-- 定妆照
    #
    # 顺序很重要：参考图得先存在，提示词才能**点名**它。反过来写的话，
    # 第一次跑时 ch.ref_image 全是空的，参考锁定段会静默地一个字都不写 ——
    # 而那正是"参考图传了也漂"的成因。
    #
    # 有项目时，定妆照存在**项目**目录并跨集复用同一个文件。这才是跨集
    # 一致性的硬保证：第 40 集的脸和第 1 集是同一张图，而不是"提示词写得像"。
    pid = ctx.project.project_id if ctx.project is not None else ""
    portraits = 0
    reused = 0                     # 项目里已有、这一集直接复用的
    warned_no_t2i = False          # 只在第一次缺 provider 时提示，别每个角色刷一行
    for ch in ep.characters:
        if not ch.anchor_prompt:
            ch.anchor_prompt = f"{ep.style}，人物定妆照，{ch.desc}"
        run_level = paths.portrait(ctx.root, ctx.run.run_id, ch.name)
        canon = paths.project_portrait(ctx.root, pid, ch.name) if pid else None

        # 1) 项目里已经有了 -> 直接复用。**不重新生成**：既不花钱，也不可能漂。
        if canon is not None and canon.exists() and canon.stat().st_size > 0 and not ctx.force:
            ch.ref_image = str(canon)
            ctx.project.remember_portrait(ch.name, str(canon))   # type: ignore[union-attr]
            reused += 1
            continue

        # 2) 单集模式的退路：本 run 里已经生成过
        if canon is None and run_level.exists() and not ctx.force:
            ch.ref_image = str(run_level)
            report.skipped += 1
            continue

        if not ctx.bundle.t2i:
            if not warned_no_t2i:
                warned_no_t2i = True
                report.notes.append(
                    "未配置 t2i provider（AI_DRAMA_T2I），已跳过定妆照。"
                    "接真图像模型时建议单独配一个，定妆照和人像一致性关系很大。"
                )
            continue
        # 项目模式下**不拿 run 级的那张顶替**：那一张可能是"项目建立之前"
        # 由模型自己写的外貌生成的，脸未必对。宁可付一次生成费，也不要把
        # 一张可能是错的脸固化进圣经 —— 固化之后会一路错到第 100 集。
        dest = canon if canon is not None else run_level
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            usage = ctx.bundle.t2i.png(ch.anchor_prompt, dest, size=(720, 1280),
                                       shot_label=f"portrait/{ch.name}")
            ctx.ledger.add(usage, stage="storyboard")
            report.money += float(usage.get("money") or 0)
            ch.ref_image = str(dest)
            if ctx.project is not None:
                ctx.project.remember_portrait(ch.name, str(dest))
            portraits += 1
        except Exception as exc:                      # noqa: BLE001
            # 定妆照失败不应该让整片停摆，但必须留痕
            report.notes.append(f"定妆照失败 {ch.name}: {type(exc).__name__}: {exc}")
            report.failed += 1

    if ctx.project is not None:
        ctx.save_project()

    # ————————————————————————————————-- 提示词与时长
    max_refs = int(getattr(caps_of(ctx.bundle.image), "max_ref_images", 1) or 1)
    locked = 0
    crowded = 0
    changed = 0
    for shot in ep.shots:
        labels = reference_labels(ep, shot, max_refs)
        if labels:
            locked += 1
        # 出镜人数超过 provider 能吃的参考图数时，多出来的人**锁不住**。
        # 这不会报错，只会让人在成片里看到一张陌生的脸 —— 所以要说出来。
        if len(characters_in_shot(ep, shot)) > max_refs:
            crowded += 1
        if not shot.image_prompt or ctx.force:
            shot.image_prompt = compose_image_prompt(ep, shot, labels)
            shot.video_prompt = compose_video_prompt(shot)
            changed += 1
        # 时长统一在这里复核：模型给的值不可信，公式给的值才可复现
        refined = estimate_shot_seconds(shot)
        if abs(refined - shot.duration) > 0.01:
            shot.duration = refined
            changed += 1

    ctx.store.save_episode(ep)
    ctx.store.sync_shots(ctx.run.run_id, ep)
    ep_json = paths.episode_json(ctx.root, ctx.run.run_id)
    ep_json.write_text(json.dumps(ep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    report.processed = changed
    report.artifacts["episode"] = str(ep_json)
    head = f"定稿 {len(ep.shots)} 镜 / 总时长 {ep.total_duration}s / 定妆照 {portraits} 张"
    if reused:
        head += f"（另有 {reused} 张沿用项目里的，未重复生成）"
    lock = f"参考锁定 {locked}/{len(ep.shots)} 镜（provider 单镜最多 {max_refs} 张参考图）"
    report.notes.append(head)
    report.notes.append(lock)
    if crowded:
        report.notes.append(
            f"[!] {crowded} 个镜头出镜人数超过参考图上限（{max_refs}），"
            "多出来的人锁不住脸 —— 拆成单人镜头，或换支持更多参考图的模型"
        )
    ctx.log(f"[storyboard] [OK] {head}")
    ctx.log(f"[storyboard] {lock}")
    for note in report.notes:
        if note.startswith("[!]"):
            ctx.log(f"[storyboard] {note}")
    return report
