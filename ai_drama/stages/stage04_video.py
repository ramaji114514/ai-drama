"""阶段 4：镜头视频（并发 + 重试）。

短剧最贵的一层，也是最该被骨架管住的一层：

  * **submit 全部，再 poll**。真厂商排队几分钟是常态，串行 submit 会让
    50 个镜头变成 50 倍的等待。这里先把所有任务提交出去，
    再并发轮询 — 排队时间被完全重叠掉。
  * **失败隔离**。某一个镜头挂了不能带走整片，但也绝不静默补一个占位视频，
    而是把该镜头标成 failed 并记录原因，最后在报告里列出来。
  * **原子落盘**。产物先写临时路径，成功后 rename。
    这样磁盘上永远不会出现"半个 mp4"导致下次 resume 误判已完成。
  * **幂等**。clip 已存在就跳过，所以中断后重跑只补缺失的镜头。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core import paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot
from ..providers.base import call_with_retry


def _submit(ctx: StageContext, shot: Shot) -> Tuple[Shot, str, Path]:
    src_image = shot.keyframe
    if not src_image or not Path(src_image).exists():
        kf = paths.keyframe(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        src_image = str(kf) if kf.exists() else None

    dest = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    request: Dict[str, Any] = {
        "prompt": shot.video_prompt or shot.action,
        "image_path": src_image,           # 图生视频：一致性来自首帧
        "duration": shot.duration,
        "size": (720, 1280),
        "shot_label": shot.label,
        "dest": str(dest),
        "negative_prompt": "文字, 字幕, 水印, 面部崩坏, 闪烁, 形变",
    }
    job_id = call_with_retry(
        ctx.bundle.video.submit, request, label=f"{shot.label} submit"
    )
    ctx.store.update_shot(shot.shot_id, state="running", error=None)
    return shot, job_id, dest


def _poll_one(ctx: StageContext, shot: Shot, job_id: str, dest: Path,
              timeout: float = 900.0) -> Tuple[str, Dict[str, Any], str]:
    """轮询到出片。返回 (状态, usage, 说明)。

    注意这里对 poll 的异常是**容错继续**而不是直接失败 ——
    一次网络抖动不该让你重跑一个已经排队 3 分钟的任务。
    """
    started = time.time()
    last_err = ""
    while time.time() - started < timeout:
        try:
            state, usage, out = ctx.bundle.video.poll(job_id, timeout=timeout)
        except Exception as exc:                      # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(2.0)                           # 网络抖动：退避后重试
            continue
        if state == "succeeded" and out:
            return ("done", dict(usage or {}), out)
        if state == "failed":
            return ("failed", {}, last_err or "provider reported failure")
        time.sleep(1.5)
    return ("failed", {}, last_err or f"timeout after {timeout:.0f}s")


def _one(ctx: StageContext, shot: Shot) -> Tuple[str, Dict[str, Any], str]:
    dest = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if dest.exists() and dest.stat().st_size > 0 and not ctx.force:
        return ("skipped", {}, str(dest))

    try:
        shot, job_id, dest = _submit(ctx, shot)
    except Exception as exc:                          # noqa: BLE001
        ctx.heal_shot(shot, exc)
        return ("failed", {}, f"submit: {type(exc).__name__}: {exc}")

    # 产物写到临时文件，成功后再改名 — 避免半个文件被当成"已完成"
    tmp = dest.with_suffix(".part.mp4")
    try:
        status, usage, info = _poll_one(ctx, shot, job_id, tmp)
        if status != "done":
            ctx.store.update_shot(shot.shot_id, state="failed", error=info[:800])
            return ("failed", usage, info)
        if not Path(info).exists():
            msg = f"provider 返回成功但产物不存在：{info}"
            ctx.store.update_shot(shot.shot_id, state="failed", error=msg)
            return ("failed", usage, msg)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        if Path(info) != tmp:
            if tmp.exists():
                tmp.unlink()
            Path(info).replace(tmp)
        if tmp.exists():
            tmp.replace(dest)
        ctx.store.update_shot(shot.shot_id, state="done", video=str(dest), error=None)
        return ("done", usage, str(dest))
    except Exception as exc:                          # noqa: BLE001
        ctx.heal_shot(shot, exc)
        return ("failed", {}, f"{type(exc).__name__}: {exc}")


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="video")
    todo: List[Shot] = []
    for shot in ctx.episode.shots:
        dest = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        if dest.exists() and dest.stat().st_size > 0 and not ctx.force:
            shot.video = str(dest)
            ctx.store.update_shot(shot.shot_id, state="done", video=str(dest))
            report.skipped += 1
        else:
            todo.append(shot)

    if not todo:
        report.notes.append("所有镜头视频已存在，无需重跑")
        ctx.log("[video] [OK] 全部命中缓存")
        return report

    ctx.log(f"[video] 并行生成 {len(todo)} 个镜头（并发 {ctx.concurrency}）…")
    with ThreadPoolExecutor(max_workers=max(1, ctx.concurrency)) as pool:
        futures = {pool.submit(_one, ctx, s): s for s in todo}
        for fut in as_completed(futures):
            shot = futures[fut]
            try:
                status, usage, info = fut.result()
            except Exception as exc:                  # noqa: BLE001
                status, usage, info = "failed", {}, f"{type(exc).__name__}: {exc}"
            if usage:
                ctx.ledger.add(usage, stage="video", shot_label=shot.label)
                report.money += float(usage.get("money") or 0)
            if status == "done":
                report.processed += 1
                shot.video = info
            elif status == "failed":
                report.failed += 1
                report.notes.append(f"[{shot.label}] {info}")
            else:
                report.skipped += 1

    ctx.log(f"[video] [OK] 成功 {report.processed} / 失败 {report.failed} / 跳过 {report.skipped}")
    return report
