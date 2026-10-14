"""阶段 4：镜头视频（并发 + 重试 + 按 provider 上限拆分）。

短剧最贵的一层，也是最该被骨架管住的一层：

  * **submit 全部，再 poll**。真厂商排队几分钟是常态，串行 submit 会让
    50 个镜头变成 50 倍的等待。这里先把所有任务提交出去，
    再并发轮询 — 排队时间被完全重叠掉。
  * **失败隔离**。某一个镜头挂了不能带走整片，但也绝不静默补一个占位视频，
    而是把该镜头标成 failed 并记录原因，最后在报告里列出来。
  * **原子落盘**。产物先写临时路径，成功后 rename。
    这样磁盘上永远不会出现"半个 mp4"导致下次 resume 误判已完成。
  * **幂等**。clip 已存在就跳过，所以中断后重跑只补缺失的镜头。
  * **按 provider 上限拆分**（见下）。这是骨架接真厂商后第一个会踩的坑。

# 关于拆分：为什么一个镜头会渲成多段

"每个镜头拍几秒"是**创作侧**定的 —— 由 core/media.estimate_shot_seconds()
按台词长度与景别推算。那 8 秒上限是"人说话需要多久"的舒适区，
**不是生成模型的能力上限**。

真厂商各有各的上限（常见 5 / 8 / 12 秒），而把 12 秒的信息量交给
上限 8 秒的模型，你会**不报错地**拿到一段只演完前 2/3 的视频：
后半截的动作凭空消失，成片时长却还写着 12 秒。这是最难归因的一类事故。

所以骨架的做法是：读 provider 的 `caps.max_duration`，超了就均分成
若干段各自生成，再本地拼成一个镜头片段。每一段都是它自己的完整镜头，
总时长不变，provider 也永远收到合法请求。

对照参考项目（aigc-agent）的同一问题：
    domain/storyboard/breakdown.py:1-15 明确记录原项目写死 10-15 秒
    "是按即梦的规格来的"，换成 veo3.1（上限 8 秒）之后
    "写 15 秒模型会按 15 秒的信息量去编排画面，生成出来必然是截断的"。
    它的解法是把时长做成调用的传入参数；骨架的解法多一步 ——
    在阶段 4 里真的把它拆开，因为到那一步改动成本最小。
"""

from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media, paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot
from ..providers.base import call_with_retry, caps_of


def _seed_for(shot: Shot, part: int, total: int) -> int:
    """确定性种子：同一个镜头第 N 段永远得到同一个 seed。

    对可恢复流水线这是硬需求 —— 中断重跑时不能排出不同的画面。
    用 md5 而不是内置 hash()：后者对 str 加了随机盐，
    换个进程就变了，那样重跑会得到不同的画面。
    """
    import hashlib

    key = f"{shot.shot_id}|{shot.scene_id}|{shot.order}|{part}|{total}"
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16)


def _submit_one(ctx: StageContext, shot: Shot, part: int, total: int,
                duration: float, dest: Path) -> str:
    src_image = shot.keyframe
    if not src_image or not Path(src_image).exists():
        kf = paths.keyframe(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        src_image = str(kf) if kf.exists() else None

    caps = caps_of(ctx.bundle.video)
    request: Dict[str, Any] = {
        "prompt": shot.video_prompt or shot.action,
        "image_path": src_image,           # 图生视频：一致性来自首帧
        "duration": duration,
        "size": caps.size,
        "shot_label": shot.label,
        "dest": str(dest),
        "seed": _seed_for(shot, part, total),
        "part": (part, total),             # provider 可据此做日志/去重
        "negative_prompt": "文字, 字幕, 水印, 面部崩坏, 闪烁, 形变",
    }
    label = f"{shot.label} submit" + (f" p{part}/{total}" if total > 1 else "")
    job_id = call_with_retry(ctx.bundle.video.submit, request, label=label)
    # 注意：这里不把 shot 标成 running —— 拆分的情况下要等所有段都提交完，
    # 否则 store 上会出现"已经 running 但其实还有一半没提交"的假象。
    return job_id


def _poll_one(ctx: StageContext, job_id: str, *, timeout: float = 900.0
              ) -> Tuple[str, Dict[str, Any], str]:
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


def _cleanup_parts(ctx: StageContext, shot: Shot) -> None:
    """一个镜头中途失败时，把它已经渲好的分段删掉。

    为什么不留着：分段只有在**全部齐全且拼接过**之后才构成一个完整镜头。
    留着半套分段会让下一次 resume 看起来"有东西了"，但它们拼不出正确时长 ——
    这正是"半成品被当成成品"的典型形态。宁可全部重渲，也不要一部时长对的坏片。
    """
    for p in paths.clip_parts(ctx.root, ctx.run.run_id, shot.scene_id, shot.order):
        try:
            p.unlink()
        except OSError:
            pass


def _move_into_place(src: Path, dest: Path) -> None:
    """原子落盘：先写 .part，成功后 rename。"""
    tmp = dest.with_suffix(".part.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if Path(src) != tmp:
        if tmp.exists():
            tmp.unlink()
        Path(src).replace(tmp)
    if tmp.exists():
        tmp.replace(dest)


def _render_shot(ctx: StageContext, shot: Shot) -> Tuple[str, Dict[str, Any], str]:
    """渲一个镜头（必要时拆成多段）。返回 (状态, usage, 说明)。"""
    dest = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if dest.exists() and dest.stat().st_size > 0 and not ctx.force:
        return ("skipped", {}, str(dest))

    plan = media.split_plan_for(ctx, shot)
    total = len(plan)

    # 已有分段（上一次中断留下的）：只补缺的段，已付费的不重渲
    existing = {p: p for p in paths.clip_parts(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)}
    part_usages: List[Dict[str, Any]] = []

    for idx, duration in enumerate(plan, start=1):
        part_path = paths.clip_part(ctx.root, ctx.run.run_id, shot.scene_id, shot.order, idx)
        if part_path in existing and part_path.stat().st_size > 0 and not ctx.force:
            continue
        try:
            job_id = _submit_one(ctx, shot, idx, total, duration, part_path)
        except Exception as exc:                      # noqa: BLE001
            _cleanup_parts(ctx, shot)
            ctx.heal_shot(shot, exc)
            return ("failed", {}, f"submit p{idx}/{total}: {type(exc).__name__}: {exc}")

        ctx.store.update_shot(shot.shot_id, state="running", error=None)
        tmp = part_path.with_suffix(".part.mp4")
        status, usage, info = _poll_one(ctx, job_id)
        if usage:
            part_usages.append(usage)
        if status != "done":
            _cleanup_parts(ctx, shot)
            msg = f"p{idx}/{total}: {info}"
            ctx.store.update_shot(shot.shot_id, state="failed", error=msg[:800])
            return ("failed", _merge_usage(part_usages), msg)
        if not Path(info).exists():
            _cleanup_parts(ctx, shot)
            msg = f"provider 返回成功但产物不存在：{info}"
            ctx.store.update_shot(shot.shot_id, state="failed", error=msg)
            return ("failed", _merge_usage(part_usages), msg)
        try:
            _move_into_place(Path(info), part_path)
        except OSError as exc:
            _cleanup_parts(ctx, shot)
            ctx.heal_shot(shot, exc)
            return ("failed", _merge_usage(part_usages), f"落盘失败：{exc}")

    # ———————————————— 拼段
    # 拼段的工作目录必须**按镜头分开**。
    # ffmpeg 归一化时会在工作目录里写 `_norm_0001.ts` 这类固定名字，
    # 两个镜头并发拼段就会抢同一个文件 —— 表现出来是
    # `PermissionError: [WinError 32] 另一个程序正在使用此文件`，
    # 而那句话完全指不到"并发"这个真正的原因。
    work = paths.scene_dir(ctx.root, ctx.run.run_id, shot.scene_id) / "_work" / f"shot_{shot.order:03d}"
    try:
        parts = paths.clip_parts(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        if total > 1:
            if len(parts) != total:
                msg = f"分段不全：应有 {total} 段，实际 {len(parts)} 段"
                _cleanup_parts(ctx, shot)
                ctx.store.update_shot(shot.shot_id, state="failed", error=msg)
                return ("failed", _merge_usage(part_usages), msg)
            # 拼到工作目录里，成功了再原子搬进正式位置。
            # 直接拼到 sc01/ 下面的话，中途失败会在正式产物目录里留一个
            # 半成品 `shot_001_clip_joined.mp4` —— 下一轮看目录的人会以为
            # 那一镜渲好了。
            work.mkdir(parents=True, exist_ok=True)
            joined = work / f"joined_{shot.order:03d}.mp4"
            media.concat_clips(parts, joined, work_dir=work)
            _move_into_place(joined, dest)
            for p in parts:
                p.unlink(missing_ok=True)
        else:
            parts[0].replace(dest) if parts else None
    except Exception as exc:                          # noqa: BLE001
        ctx.heal_shot(shot, exc)
        return ("failed", _merge_usage(part_usages), f"拼段失败：{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not dest.exists() or dest.stat().st_size == 0:
        msg = "渲染流程走完但成片文件不存在"
        ctx.store.update_shot(shot.shot_id, state="failed", error=msg)
        return ("failed", _merge_usage(part_usages), msg)

    ctx.store.update_shot(shot.shot_id, state="done", video=str(dest), error=None)
    return ("done", _merge_usage(part_usages), str(dest))


def _merge_usage(usages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把同一镜头多段的 usage 合并成一条，避免成本被记 N 次却看不出来源。

    金额与数量相加；provider / stage / unit 取第一条并标注段数。
    """
    if not usages:
        return {}
    if len(usages) == 1:
        return usages[0]
    money = sum(float(u.get("money") or 0) for u in usages)
    amount = sum(float(u.get("amount") or 0) for u in usages)
    merged = dict(usages[0])
    merged["money"] = round(money, 6)
    merged["amount"] = round(amount, 3)
    merged["parts"] = len(usages)
    merged["note"] = (str(merged.get("note") or "") + f"；合并 {len(usages)} 段").strip("；")
    return merged


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="video")

    # 先算一遍拆分计划：这不是优化，是**在设计阶段就暴露冲突**。
    # 一个 12 秒的镜头在上限 8 秒的模型上会被拆，报告里要说清楚，
    # 否则用户只会看到账单里同一个镜头付了两次钱。
    caps = caps_of(ctx.bundle.video)
    todo: List[Shot] = []
    split_shots = 0
    for shot in ctx.episode.shots:
        dest = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        if dest.exists() and dest.stat().st_size > 0 and not ctx.force:
            shot.video = str(dest)
            ctx.store.update_shot(shot.shot_id, state="done", video=str(dest))
            report.skipped += 1
            continue
        plan = media.split_plan_for(ctx, shot)
        if len(plan) > 1:
            split_shots += 1
            report.notes.append(
                f"[{shot.label}] 需 {shot.duration:.1f}s，超过 {ctx.bundle.video.name} "
                f"单段上限 {caps.max_duration:g}s -> 拆 {len(plan)} 段"
                f"（{'+'.join(f'{p:g}' for p in plan)}）"
            )
        todo.append(shot)

    if not todo:
        report.notes.append("所有镜头视频已存在，无需重跑")
        ctx.log("[video] [OK] 全部命中缓存")
        return report

    if split_shots:
        ctx.log(
            f"[video] [!] {split_shots} 个镜头超出单段上限，将被拆分"
            f"（provider 单段上限 {caps.max_duration:g}s）"
        )

    ctx.log(f"[video] 并行生成 {len(todo)} 个镜头（并发 {ctx.concurrency}）…")
    with ThreadPoolExecutor(max_workers=max(1, ctx.concurrency)) as pool:
        futures = {pool.submit(_render_shot, ctx, s): s for s in todo}
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
