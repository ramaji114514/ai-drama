"""阶段 3：关键帧（并发）。

每个镜头出一张首帧图。首帧质量决定了视频模型的表现上限，
所以这一层值得你花最多时间调（提示词模板见 skills/frames.md）。

并发模型：ThreadPoolExecutor。为什么不用 asyncio？
因为真实厂商的 SDK 大多是同步阻塞的（requests / 各家 python-sdk），
用线程池可以**不加改动地**接入它们，避免为了异步而改写所有适配器。
需要真异步时，把 _one() 换成 asyncio.to_thread 即可，其余逻辑不变。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core import paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot
from .stage02_storyboard import character_in_shot


def _one(ctx: StageContext, shot: Shot) -> Tuple[str, Dict[str, Any], str]:
    """渲染一个镜头的关键帧。返回 (状态, usage, 说明)。

    为什么把整个 usage 字典传出来而不是只传钱：成本必须能按 provider
    归因（"这 3 块钱是 720p 图生视频花的还是 TTS 花的"），
    report.money 只是个汇总数，丢了 this 就没法做成本报表了。
    """
    dest = paths.keyframe(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if dest.exists() and not ctx.force:
        return ("skipped", {}, str(dest))

    ep = ctx.episode
    ch = character_in_shot(ep, shot)
    ref = Path(ch.ref_image) if (ch and ch.ref_image) else None
    if ref and not ref.exists():
        ref = None

    ctx.store.update_shot(shot.shot_id, state="running", error=None)
    try:
        usage = ctx.bundle.image.png(
            shot.image_prompt,
            dest,
            size=ctx.frame_size,
            ref_image=ref,          # 有定妆照就做图生图，这是跨镜头一致性的关键
            shot_label=shot.label,
            scene=shot.framing,
        )
        ctx.store.update_shot(shot.shot_id, state="done", keyframe=str(dest), error=None)
        return ("done", dict(usage or {}), str(dest))
    except Exception as exc:                      # noqa: BLE001
        ctx.heal_shot(shot, exc)                  # 失败留痕，绝不静默降级
        return ("failed", {}, f"{type(exc).__name__}: {exc}")


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="frames")
    ep = ctx.episode
    todo: List[Shot] = []

    for shot in ep.shots:
        dest = paths.keyframe(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        if dest.exists() and not ctx.force:
            ctx.store.update_shot(shot.shot_id, state="done", keyframe=str(dest))
            shot.keyframe = str(dest)
            report.skipped += 1
        else:
            todo.append(shot)

    if not todo:
        report.notes.append("所有关键帧已存在，无需重跑")
        ctx.log("[frames] [OK] 全部命中缓存")
        return report

    ctx.log(f"[frames] 并行渲染 {len(todo)} 张关键帧（并发 {ctx.concurrency}）…")
    with ThreadPoolExecutor(max_workers=max(1, ctx.concurrency)) as pool:
        futures = {pool.submit(_one, ctx, s): s for s in todo}
        for fut in as_completed(futures):
            shot = futures[fut]
            try:
                status, usage, info = fut.result()
            except Exception as exc:              # noqa: BLE001
                status, usage, info = "failed", {}, f"{type(exc).__name__}: {exc}"
            if usage:
                # 统一在收集点记账：只有这里能看到完整的 shot 上下文，
                # 也是唯一能保证"花了就一定记上"的位置。
                ctx.ledger.add(usage, stage="frames", shot_label=shot.label)
                report.money += float(usage.get("money") or 0)
            if status == "done":
                report.processed += 1
                shot.keyframe = info
            elif status == "failed":
                report.failed += 1
                report.notes.append(f"[{shot.label}] {info}")
            else:
                report.skipped += 1

    ctx.log(f"[frames] [OK] 成功 {report.processed} / 失败 {report.failed} / 跳过 {report.skipped}")
    return report
