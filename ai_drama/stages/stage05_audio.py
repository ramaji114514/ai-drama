"""阶段 5：音频（台词 + 音效 + BGM）。

**这一阶段在原项目里是完全缺失的** — 它们把 audio_desc 拼进画面提示词
就当有声音了。但短剧之所以是短剧，靠的就是台词演出：
每句台词要有独立的音轨，并且能对齐到它所属的那个镜头。

骨架在这里定下的契约（换成真 TTS 时不用改）：
    run_dir/scenes/<scXX>/shot_<NNN>_audio.wav     ← 该镜台词，起始时刻由 07 排定
    run_dir/scenes/<scXX>/shot_<NNN>_sfx<NN>.wav   ← 该镜第 NN 条音效，同样由 07 排定
    run_dir/bgm.wav                                ← 全片 BGM

三种声音的分工不是随意分的：
    * 台词  —— 每句一条，和镜头一一对应（要说的话）
    * 音效  —— 每镜 0-N 条，和镜头一一对应（环境声与动作声）
    * BGM   —— 全片一条（情绪）
前两种**必须**和镜头对齐，所以它们共用同一个累加时间轴；
BGM 从头铺到尾，所以它单独走 mux。混音在 07 一起做。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core import media, paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot
from ..providers.base import call_with_retry
from .stage02_storyboard import characters_in_shot


def _voice_for(ctx: StageContext, shot: Shot) -> str:
    """这一句台词用谁的嗓子。

    优先级：项目圣经里这个角色的音色 > `--voice` > 默认。

    同一个人每集必须用同一个声音 —— 界面上换脸比换声音更刺眼，但观众对
    声音的记忆更持久，第 3 集换了音色会直接被说成"配音换人了"。

    说话人从台词行首解析（`苏蔓：（低声）不对。` -> `苏蔓`）。解析不出来时，
    如果这一镜**画面里只有一个人**，那就是他/她在说 —— 走**声明**（分镜给的
    角色引用）而不是文本猜，因为声明引用的是身份，文本只是字符串。
    这台词没写说话人的情况相当常见，退回全局默认会让整部剧听起来像一个人
    在自言自语。
    """
    fallback = str(ctx.run.meta.get("voice") or "female_01")
    if ctx.project is None:
        return fallback
    speaker = media.speaker_of(shot.dialogue)
    if speaker:
        return ctx.project.voice_for(speaker, fallback)
    only = characters_in_shot(ctx.episode, shot)
    if len(only) == 1:
        return ctx.project.voice_for(only[0], fallback)
    return fallback


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


def _sfx_for_shot(ctx: StageContext, shot: Shot) -> List[Tuple[str, Any]]:
    """这一镜的音效逐条合成。返回 [(路径, usage), ...]。

    每一条**单独一个文件**：合并成一个之后就没法单独重跑其中一条、
    也没法调这一条的响度 —— 而重跑一条音效比重跑一条台词便宜得多。
    已经存在且非空的直接复用（幂等重跑不重复花钱）。
    """
    out: List[Tuple[str, Any]] = []
    for idx, cue in enumerate(shot.sfx):
        dest = paths.sfx(ctx.root, ctx.run.run_id, shot.scene_id, shot.order, idx)
        if dest.exists() and dest.stat().st_size > 0 and not ctx.force:
            out.append((str(dest), {}))
            continue
        # 音效比镜头略长一点：短了会显得"点到为止"，长了会盖住下一镜
        # （拼接阶段按镜头切，所以宁可比 cue 稍长）。
        usage = call_with_retry(
            ctx.bundle.sfx.make, cue, dest,
            duration=min(3.0, max(0.6, shot.duration * 0.6)),
            scene=shot.label, label=f"{shot.label} sfx「{cue}」",
        )
        out.append((str(dest), usage))
    return out


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

    # ————————————————————————————————----- 音效
    #
    # 关掉 sfx 时**必须出声**：分镜里写着"门被撞开"，成片里没有那一声，
    # 是那种"一切正常"的缺失 —— 报告里不写就没人会发现。
    sfx_shots = [s for s in ep.shots if s.sfx]
    sfx_count = sum(len(s.sfx) for s in sfx_shots)
    if sfx_count:
        if ctx.bundle.sfx is None:
            report.notes.append(
                f"[!] 分镜里写了 {sfx_count} 条音效（{len(sfx_shots)} 镜），"
                f"但没有配 sfx provider —— 成片里不会有这些声音。"
                f"设 AI_DRAMA_SFX=<音效 provider> 才会真的合成。"
            )
        else:
            done = failed = 0
            with ThreadPoolExecutor(max_workers=max(1, ctx.concurrency)) as pool:
                futures = {pool.submit(_sfx_for_shot, ctx, s): s for s in sfx_shots}
                for fut in as_completed(futures):
                    shot = futures[fut]
                    try:
                        pairs = fut.result()
                    except Exception as exc:      # noqa: BLE001
                        failed += 1
                        report.notes.append(
                            f"[{shot.label}] 音效失败：{type(exc).__name__}: {exc}"
                        )
                        continue
                    for path, usage in pairs:
                        if usage:
                            ctx.ledger.add(usage, stage="audio", shot_label=shot.label)
                            report.money += float(usage.get("money") or 0)
                        done += 1
                        report.artifacts.setdefault("sfx", []).append(path)
            report.notes.append(f"音效 {done} 条 / {len(sfx_shots)} 镜，失败 {failed}")
            report.failed += failed

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
