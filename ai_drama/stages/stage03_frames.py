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
from typing import Any, Dict, List, Optional, Tuple

from ..core import paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot
from .stage02_storyboard import character_in_shot


def _reference(ctx: StageContext, shot: Shot) -> Optional[Path]:
    ep = ctx.episode
    ch = character_in_shot(ep, shot)
    ref = Path(ch.ref_image) if (ch and ch.ref_image) else None
    if ref and not ref.exists():
        ref = None
    return ref


def _record(ctx: StageContext, shot: Shot, dest: Path, ref: Optional[Path],
            *, reused: bool) -> None:
    """把关键帧记进台账，并连上它是拿谁的定妆照生成的。

    这张图是整条链里**最该被追溯**的东西：成片里每一镜长什么样都能追到它，
    而它的提示词是模型写的。定妆照那条边找不到时不编（老 run 的台账里没有
    定妆照记录，那就只留关键帧这一条），宁可血缘断在这儿也不要指错人。
    """
    ref_asset = ctx.asset_of(ref) if ref else None
    ctx.asset(
        dest, kind="keyframe", via="image", prompt=shot.image_prompt,
        shot_id=shot.shot_id,
        refs=[ref_asset] if ref_asset else [],
        meta={"shot": shot.label, "framing": shot.framing or "",
              "ref_image": str(ref) if ref else "", "reused": reused},
    )


def _one(ctx: StageContext, shot: Shot) -> Tuple[str, Dict[str, Any], str]:
    """渲染一个镜头的关键帧。返回 (状态, usage, 说明)。

    为什么把整个 usage 字典传出来而不是只传钱：成本必须能按 provider
    归因（"这 3 块钱是 720p 图生视频花的还是 TTS 花的"），
    report.money 只是个汇总数，丢了 this 就没法做成本报表了。
    """
    dest = paths.keyframe(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    ref = _reference(ctx, shot)
    if dest.exists() and not ctx.force:
        _record(ctx, shot, dest, ref, reused=True)
        return ("skipped", {}, str(dest))

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
        _record(ctx, shot, dest, ref, reused=False)
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
            _record(ctx, shot, dest, _reference(ctx, shot), reused=True)
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
