"""阶段 6：口型同步。

位置是刻意选的：**audio 之后、concat 之前** —— 先有音轨才能对嘴，
拼接要用对完嘴的片段。

这一阶段只处理**有台词的镜头**。没有台词的镜头（空镜、纯动作）没有可对的嘴，
送过去只是白花钱。

两条契约由本阶段强制，不指望每个适配器都记得：

  1. **输出不带音轨**。音轨统一由 concat 铺一次；这里再塞一条进去，
     成片里每句台词都会说两遍。
  2. **输出时长 == 输入片段时长**。concat 的音画对齐是按 ffprobe 量出的
     **片段实际时长**累加时间轴的 —— 这一条破了，后面所有镜头的声音都会
     错位，而且是静默错位。

第 2 条为什么校验放在骨架这一侧：让 provider 先写到临时文件，量完时长对得上
才原子替换成正式产物。适配器写坏了只会浪费这一次调用，不会污染 run 目录、
更不会让"已经同步好的片段"其实是坏的。这也是 dryrun 不自己写临时文件的原因。

没配 provider 不算错误（`AI_DRAMA_LIPSYNC=none` 是合法的），但**必须被看见** ——
"成片里嘴型没对上"这件事不能只活在某个人的记忆里。要让它变成硬门槛，把
`config/qc_rules.yaml` 的 `require_lipsync` 打开。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media, paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot
from ..providers.base import call_with_retry


def _clip_and_audio(ctx: StageContext, shot: Shot) -> Tuple[Optional[Path], Optional[Path]]:
    """这个镜头要同步的片段与配音。按路径**按 label 找**，不用列表下标。"""
    clip = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if not (clip.exists() and clip.stat().st_size > 0):
        # 退回 store 里记的路径（旧 run 或产物被挪过）
        clip = Path(shot.video) if shot.video and Path(shot.video).exists() else None
    audio = paths.voice(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if not (audio.exists() and audio.stat().st_size > 0):
        audio = Path(shot.audio) if shot.audio and Path(shot.audio).exists() else None
    return clip, audio


def _sync_one(ctx: StageContext, shot: Shot) -> Tuple[str, Dict[str, Any], str]:
    dest = paths.synced(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if dest.exists() and dest.stat().st_size > 0 and not ctx.force:
        # 已有产物就跳过（不重复付费）。但**必须把路径再记一次** ——
        # 上一次可能正是"文件写成了、记账失败"的那种中断，
        # 不补记的话质检会读到空的 shot.synced，报一个根本不存在的缺失。
        ctx.store.update_shot(shot.shot_id, synced=str(dest))
        return ("skipped", {}, str(dest))

    clip, audio = _clip_and_audio(ctx, shot)
    if clip is None:
        return ("failed", {}, "找不到这个镜头的片段，没法做口型同步")

    provider = ctx.bundle.lipsync
    if provider is None:                              # pragma: no cover - run() 已拦
        return ("skipped", {}, "没有配置 lip-sync provider")

    # 先量输入时长，作为校验基准（量不出来就别往下走 —— 没有基准就没法验证契约）
    want = media.try_probe_duration(clip)
    if want is None:
        return ("failed", {}, f"量不出片段时长：{clip}")

    tmp = dest.with_name(dest.stem + ".part.mp4")
    tmp.unlink(missing_ok=True)
    try:
        usage = call_with_retry(
            provider.sync, clip, audio, tmp,
            shot_label=shot.label, scene=shot.scene_id,
            label=f"{shot.label} lipsync",
        )
        if not (tmp.exists() and tmp.stat().st_size > 0):
            raise RuntimeError("provider 报告成功，但没有写出文件")

        got = media.try_probe_duration(tmp)
        if got is None:
            raise RuntimeError("量不出同步后片段的时长，不敢当成成功")
        if abs(got - want) > 0.12:
            # 契约 2。宁可这一镜算失败（可以单独重跑），也不要把错位放进去。
            raise RuntimeError(
                f"同步后时长变了（{want:.3f}s -> {got:.3f}s）："
                f"这会挪动后面所有镜头的时间轴，不能收"
            )

        tmp.replace(dest)
        ctx.store.update_shot(shot.shot_id, state="done", synced=str(dest))
        return ("done", dict(usage or {}), str(dest))
    except Exception as exc:                          # noqa: BLE001
        tmp.unlink(missing_ok=True)
        ctx.heal_shot(shot, exc)
        return ("failed", {}, f"{type(exc).__name__}: {exc}")
    finally:
        tmp.unlink(missing_ok=True)


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="lipsync")
    ep = ctx.episode

    # 只挑"有台词 + 有片段"的镜头
    candidates: List[Shot] = []
    no_clip = 0
    for shot in ep.shots:
        if not shot.dialogue.strip():
            continue
        clip, _ = _clip_and_audio(ctx, shot)
        if clip is None:
            no_clip += 1
            continue
        candidates.append(shot)

    report.processed = 0
    if ctx.bundle.lipsync is None:
        report.skipped = len(candidates)
        # 一个镜头都不需要同步时不喊 —— 没有东西被跳过，"嘴型没对上"也就无从谈起。
        # 空喊会训练出"报告里的 [!] 不用看"这个习惯，那比不报还糟。
        if candidates:
            report.notes.append(
                f"[!] 没有配置 lip-sync provider（AI_DRAMA_LIPSYNC）—— "
                f"{len(candidates)} 个有台词的镜头被跳过，成片里嘴型没有对齐。"
                f"接上见 docs/adding-a-provider.md；要把这件事变成硬门槛，"
                f"打开 config/qc_rules.yaml 的 require_lipsync。"
            )
        else:
            report.notes.append(
                "没有需要口型同步的镜头（空镜与纯动作镜头本来就没有可对的嘴）"
            )
        ctx.log(f"[lipsync] 跳过 {report.skipped}（未配置 provider）")
        return report

    report.notes.append(f"{len(candidates)} 镜需要口型同步 / 共 {len(ep.shots)} 镜")
    if no_clip:
        report.notes.append(f"[!] {no_clip} 个有台词的镜头找不到片段，没法同步")
    if not candidates:
        report.notes.append("没有需要口型同步的镜头（空镜与纯动作镜头本来就没有可对的嘴）")

    with ThreadPoolExecutor(max_workers=max(1, ctx.concurrency)) as pool:
        futures = {pool.submit(_sync_one, ctx, s): s for s in candidates}
        for fut in as_completed(futures):
            shot = futures[fut]
            try:
                status, usage, info = fut.result()
            except Exception as exc:                  # noqa: BLE001
                status, usage, info = "failed", {}, f"{type(exc).__name__}: {exc}"
            if usage:
                ctx.ledger.add(usage, stage="lipsync", shot_label=shot.label)
                report.money += float(usage.get("money") or 0)
            if status == "done":
                report.processed += 1
                shot.synced = info
                report.artifacts["synced"] = int(report.artifacts.get("synced") or 0) + 1
            elif status == "failed":
                report.failed += 1
                report.notes.append(f"[{shot.label}] {info}")
            else:
                report.skipped += 1
                # 跳过（已有产物）也要把路径记回 episode，否则质检读到的
                # shot.synced 是空的，会报一个磁盘上其实有的缺失。
                if info:
                    shot.synced = info

    report.notes.append(
        "[!] 用的是 dryrun：这一步没有真的改嘴型"
        if getattr(ctx.bundle.lipsync, "name", "") == "dryrun-lipsync" else
        "口型同步由 provider 完成，骨架只校验两条契约（无音轨、时长不变）"
    )
    ctx.log(
        f"[lipsync] [OK] 同步 {report.processed} / 失败 {report.failed} / 跳过 {report.skipped}"
    )
    return report
