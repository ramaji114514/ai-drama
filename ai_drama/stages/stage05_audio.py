"""阶段 5：音频（台词 + BGM）。

**这一阶段在原项目里是完全缺失的** — 它们把 audio_desc 拼进画面提示词
就当有声音了。但短剧之所以是短剧，靠的就是台词演出：
每句台词要有独立的音轨，并且能对齐到它所属的那个镜头。

骨架在这里定下的契约（换成真 TTS 时不用改）：
    run_dir/scenes/<scXX>/shot_<NNN>_audio.wav   ← 该镜台词，起始时刻由 06 排定
    run_dir/bgm.wav                              ← 全片 BGM

旁白、音效可以按同样的模式扩展（加 *_sfx.wav + 在 06 里多一路 amix）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core import media, paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot
from ..providers.base import call_with_retry


def _voice_for(ctx: StageContext, shot: Shot) -> str:
    """这一句台词用谁的嗓子。

    优先级：项目圣经里这个角色的音色 > `--voice` > 默认。

    同一个人每集必须用同一个声音 —— 界面上换脸比换声音更刺眼，但观众对
    声音的记忆更持久，第 3 集换了音色会直接被说成"配音换人了"。
    说话人从台词行首解析（`苏蔓：（低声）不对。` -> `苏蔓`）；
    解析不出来就退回全局音色。
    """
    fallback = str(ctx.run.meta.get("voice") or "female_01")
    if ctx.project is None:
        return fallback
    speaker = media.speaker_of(shot.dialogue)
    if not speaker:
        return fallback
    return ctx.project.voice_for(speaker, fallback)


def _one(ctx: StageContext, shot: Shot) -> Tuple[str, Dict[str, Any], str]:
    if not shot.dialogue.strip():
        return ("skipped", {}, "无台词")
    dest = paths.voice(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if dest.exists() and dest.stat().st_size > 0 and not ctx.force:
        return ("skipped", {}, str(dest))
    try:
        voice = _voice_for(ctx, shot)
        usage = call_with_retry(
            ctx.bundle.audio.tts, shot.dialogue, dest,
            voice=voice, speed=1.0, shot_label=shot.label,
            label=f"{shot.label} tts",
        )
        ctx.store.update_shot(shot.shot_id, state="done", audio=str(dest))
        return ("done", dict(usage or {}), str(dest))
    except Exception as exc:                      # noqa: BLE001
        ctx.heal_shot(shot, exc)
        return ("failed", {}, f"{type(exc).__name__}: {exc}")


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="audio")
    ep = ctx.episode
    talking: List[Shot] = [s for s in ep.shots if s.dialogue.strip()]

    report.notes.append(f"{len(talking)} 镜有台词 / 共 {len(ep.shots)} 镜")
    if not talking:
        report.notes.append(
            "整片没有台词。短剧通常不该这样 — 检查分镜阶段的 dialogue 字段是否被丢掉了。"
        )

    with ThreadPoolExecutor(max_workers=max(1, ctx.concurrency)) as pool:
        futures = {pool.submit(_one, ctx, s): s for s in talking}
        for fut in as_completed(futures):
            shot = futures[fut]
            try:
                status, usage, info = fut.result()
            except Exception as exc:              # noqa: BLE001
                status, usage, info = "failed", {}, f"{type(exc).__name__}: {exc}"
            if usage:
                ctx.ledger.add(usage, stage="audio", shot_label=shot.label)
                report.money += float(usage.get("money") or 0)
            if status == "done":
                report.processed += 1
                shot.audio = info
            elif status == "failed":
                report.failed += 1
                report.notes.append(f"[{shot.label}] {info}")
            else:
                report.skipped += 1

    # ————————————————————————————————----- BGM
    bgm = paths.bgm(ctx.root, ctx.run.run_id)
    if not bgm.exists() or ctx.force:
        try:
            usage = ctx.bundle.audio.music(
                bgm, duration=max(5.0, ep.total_duration), mood=ep.style
            )
            ctx.ledger.add(usage, stage="audio")
            report.money += float(usage.get("money") or 0)
            report.artifacts["bgm"] = str(bgm)
        except Exception as exc:                  # noqa: BLE001
            report.notes.append(f"BGM 生成失败（不致命）：{type(exc).__name__}: {exc}")
            report.failed += 1
    else:
        report.artifacts["bgm"] = str(bgm)

    ctx.log(f"[audio] [OK] 配音 {report.processed} / 失败 {report.failed} / 跳过 {report.skipped}")
    return report
