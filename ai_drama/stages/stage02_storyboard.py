"""阶段 2：分镜定稿。

这是**确定性骨架**和**创意判断**之间的接缝，也是整个项目最该由你来掌控的一层：

  * 时长：由 core/media.estimate_shot_seconds 用公式算（台词长度 + 景别），
    不是让模型随口给一个数字。你要调节奏，改公式参数，全片一起变。
  * 提示词：把"风格 + 角色外观 + 景别 + 地点 + 动作"拼成画面提示词；
    运动单独拼成视频提示词。
  * **听觉信息绝不进画面提示词** — 参考项目把 audio_desc 直接拼进了
    video prompt（script2video.py:190），这是概念性错误：
    你没法让画面模型"表演出"台词，只会污染画面。
  * 定妆照：为每个角色生成一张参考图，后续所有镜头复用同一张，
    这是**最低成本的跨镜头一致性方案**。

创意侧的提示词模板在 skills/storyboard.md，本文件只做拼装与校验。
"""

from __future__ import annotations

import json
from typing import List, Optional

from ..core import paths
from ..core.context import StageContext, StageReport
from ..core.models import Character, Shot
from ..core.media import estimate_shot_seconds

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


def character_in_shot(ep, shot: Shot) -> Optional[Character]:
    """找出镜头里出现的角色。

    [!]️ 这里用的是名字子串匹配 — 够用于参考实现的 dryrun，
    但它是**已知的薄弱点**（参考项目就是这么做的，多角色场景立即失效）。
    真正要做"多角色一致性"，得换成：
      1) 让 LLM 在分镜阶段显式输出 shot.characters = [name, ...]
      2) 用脸/姿态检测在关键帧上回归验证
    见 docs/architecture.md 的"角色一致性"一节。
    """
    blob = f"{shot.action} {shot.dialogue}"
    for c in ep.characters:
        if c.name and c.name in blob:
            return c
    return None


def compose_image_prompt(ep, shot: Shot) -> str:
    parts = [ep.style or ""]
    ch = character_in_shot(ep, shot)
    if ch:
        parts.append(f"角色 {ch.name}：{ch.desc}")
    parts.append(FRAMING_PROMPT.get(shot.framing, "中景"))
    parts.append(shot.action)
    return "，".join(p for p in parts if p)


def compose_video_prompt(shot: Shot) -> str:
    """运动提示词 — 只描述摄影机和画面元素的运动，不含任何听觉信息。"""
    return f"{shot.action}。{CAMERA_MOVE.get(shot.framing, '镜头缓推')}。"


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="storyboard")
    ep = ctx.episode
    if not ep.scenes:
        report.notes.append("没有分镜可定稿（先跑 script 阶段）")
        return report

    changed = 0
    for shot in ep.shots:
        if not shot.image_prompt or ctx.force:
            shot.image_prompt = compose_image_prompt(ep, shot)
            shot.video_prompt = compose_video_prompt(shot)
            changed += 1
        # 时长统一在这里复核：模型给的值不可信，公式给的值才可复现
        refined = estimate_shot_seconds(shot)
        if abs(refined - shot.duration) > 0.01:
            shot.duration = refined
            changed += 1

    # ————————————————————————————————-- 定妆照
    portraits = 0
    warned_no_t2i = False          # 只在第一次缺 provider 时提示，别每个角色刷一行
    for ch in ep.characters:
        if not ch.anchor_prompt:
            ch.anchor_prompt = f"{ep.style}，人物定妆照，{ch.desc}"
        dest = paths.portrait(ctx.root, ctx.run.run_id, ch.name)
        if dest.exists() and not ctx.force:
            ch.ref_image = str(dest)
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
        try:
            usage = ctx.bundle.t2i.png(ch.anchor_prompt, dest, size=(720, 1280),
                                       shot_label=f"portrait/{ch.name}")
            ctx.ledger.add(usage, stage="storyboard")
            report.money += float(usage.get("money") or 0)
            ch.ref_image = str(dest)
            portraits += 1
        except Exception as exc:                      # noqa: BLE001
            # 定妆照失败不应该让整片停摆，但必须留痕
            report.notes.append(f"定妆照失败 {ch.name}: {type(exc).__name__}: {exc}")
            report.failed += 1

    ctx.store.save_episode(ep)
    ctx.store.sync_shots(ctx.run.run_id, ep)
    ep_json = paths.episode_json(ctx.root, ctx.run.run_id)
    ep_json.write_text(json.dumps(ep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    report.processed = changed
    report.artifacts["episode"] = str(ep_json)
    report.notes.append(
        f"定稿 {len(ep.shots)} 镜 / 总时长 {ep.total_duration}s / 定妆照 {portraits} 张"
    )
    ctx.log(f"[storyboard] [OK] {report.notes[-1]}")
    return report
