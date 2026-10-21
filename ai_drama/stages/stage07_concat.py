"""阶段 7：拼接 + 混音 + 字幕。

三件确定性的事，都该由代码而不是模型来做：

  1. **拼接**：场级拼成本场视频，再拼成全片。
     用 ffmpeg 的 concat demuxer（先统一成 .ts），比逐个 re-encode 快得多，
     也不会因为某两段编码参数不同而炸掉。
  2. **音画对齐**：这是最难也最容易做错的一步。
     每个镜头的台词必须落在**它自己那一镜的时间区间**内，
     而不是简单地从 0 秒开始播一遍。

     做法：按镜头顺序累加**实际时长**（ffprobe 量出来的，不是名义上的 duration），
     得到每句台词在全片时间轴上的起点，再用 ffmpeg 的 adelay 把它们铺平，
     最后和画面一起 amix 混流。
  3. **字幕**：时间轴跟音画对齐用的是**同一份累加结果** —— 这一点是刻意的，
     字幕飘掉几乎永远是"字幕自己算了一遍时间"造成的。烧录走 ASS
     （字号是真实像素），同时写出一份标准 SRT 当交付物。

素材优先取**口型同步后**的片段（stage06 的产物），没有才退回原始片段 ——
见 _clip_for()。所以这一阶段也是"lip-sync 有没有真的接上"的最终检验点：
对完嘴的片段时长若和原来不一致，累加出来的时间轴就会整体错位。

参考项目在这一步的等价代码是 `video_results` 被过滤后
`enumerate` 下标错位（script2video.py:97-110），会把 video_url 写到错误的镜头上。
本骨架的规避方式是：**永远按 label 而不是下标寻址**，见 _clip_for()。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media, paths
from ..core.context import StageContext, StageReport
from ..core.models import Shot, now_iso


_DURATION_CACHE: Dict[str, float] = {}


def probe_duration(path: Path) -> Optional[float]:
    """量真实时长（带缓存）；**量不出来返回 None**。

    为什么不用模型给的 duration：那是"打算拍多久"，而这是"实际渲染出来多久"。
    音画对齐必须按后者，否则几秒钟的累积误差就会让最后几句台词飘到别的镜头上去。

    为什么要返回 None 而不是 0.0：这两件事完全不同 —— "0 秒文件"和"量不出来"。
    合成一个 0.0，调用方就没法把后者报出来，只能静默用别的值顶替。
    """
    key = str(path)
    if key in _DURATION_CACHE:
        return _DURATION_CACHE[key]
    dur = media.try_probe_duration(path)
    _DURATION_CACHE[key] = dur
    return dur


def _clip_for(ctx: StageContext, shot: Shot) -> Optional[Path]:
    """按 label 找这个镜头的视频，找不到就去 store 里查。

    优先用**口型同步后**的片段（stage06 产出），没有才退回原始片段 ——
    拼接要用对完嘴的那一份。注意这里**不用列表下标**：镜头可能因为失败而缺号，
    按下标取必然错位（这正是参考项目踩的坑）。
    """
    synced = paths.synced(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if synced.exists() and synced.stat().st_size > 0:
        return synced
    if shot.synced and Path(shot.synced).exists():
        return Path(shot.synced)

    dest = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    if shot.video and Path(shot.video).exists():
        return Path(shot.video)
    return None


def _concat_mp4(clips: List[Path], out: Path, work: Path) -> Path:
    return media.concat_clips(clips, out, work_dir=work)


def _scene_is_stale(out: Path, clips: List[Path], durations: List[float],
                    *, tolerance: float = 0.25) -> bool:
    """这一场的成片还能不能直接复用。

    只判断"文件在不在"是不够的，这正是之前放过的一个真 bug：
    重渲一个镜头之后 `resume --stages concat`，时间轴是按**这批新片段**
    重新量的，画面却还是旧成片 —— 画面旧、字幕新，整场 A/V 错位，
    而 QC 只查文件存在，一路判 done。

    所以这里比两件事：
    - 有没有素材比成片新（重渲过的片段 mtime 必然更晚）；
    - 成片实际长度和这批片段的实测时长之和差得远不远。
    """
    if not out.exists():
        return True
    try:
        built_at = out.stat().st_mtime
    except OSError:
        return True
    for clip in clips:
        try:
            if clip.stat().st_mtime > built_at:
                return True
        except OSError:
            return True
    want = sum(durations)
    got = probe_duration(out)
    if got is None:
        return True
    if want <= 0:
        return False
    return abs(got - want) > tolerance


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="concat")
    ep = ctx.episode
    if not ep.scenes:
        report.notes.append("没有分镜，无法拼接")
        return report

    # ————————————————————————————----- 场级拼接
    scene_videos: List[Tuple[str, Path, List[Shot], List[float]]] = []
    for scene in ep.scenes:
        clips: List[Path] = []
        used_shots: List[Shot] = []
        durations: List[float] = []
        missing: List[str] = []
        for shot in scene.shots:
            clip = _clip_for(ctx, shot)
            if clip:
                clips.append(clip)
                used_shots.append(shot)
                # 时长在这里量一次并缓存，后面音画对齐直接查。
                # 量不出来时**如实记一笔**再退回名义时长：静默用 shot.duration
                # 顶替，会让"这段根本没量"在报告里看不出来（参考项目踩过）。
                measured = probe_duration(clip)
                if measured is None:
                    report.notes.append(
                        f"[{shot.label}] 量不出片段时长（{clip.name}），"
                        f"这一镜按名义时长 {shot.duration:.2f}s 排，对齐可能不准"
                    )
                    report.failed += 1
                    measured = shot.duration
                durations.append(measured)
            else:
                missing.append(shot.label)
        if missing:
            # 缺镜必须显式列出来。静默跳过会让你交付一部"少了三镜"的片子
            # 而完全不知道 — 参考项目的"假数据兜底"就是这么把人坑掉的。
            report.notes.append(f"[{scene.scene_id}] 缺失镜头 {len(missing)} 个：{', '.join(missing)}")
            report.failed += len(missing)
        if not clips:
            report.notes.append(f"[{scene.scene_id}] 一个可用镜头都没有，跳过这一场")
            continue

        out = paths.scene_video(ctx.root, ctx.run.run_id, scene.scene_id)
        stale = _scene_is_stale(out, clips, durations)
        reused = out.exists() and not ctx.force and not stale
        if reused:
            report.skipped += 1
        else:
            if stale and out.exists():
                # 复用错的东西比重新拼更贵：时间轴会按**新片段**算，
                # 画面却还是旧的，字幕和台词整体飘掉，而且 QC 看不出来。
                report.notes.append(
                    f"[{scene.scene_id}] 场次成片比它的素材旧（片段重渲过或时长变了），"
                    f"已重新拼接"
                )
            _concat_mp4(clips, out, paths.work_dir(ctx.root, ctx.run.run_id, scene.scene_id))
            report.processed += 1
        # 场次拼接产物也要进台账：它在磁盘上占地最大、也是成片的直接上游，
        # 漏掉它的话对账会报"磁盘上有、台账里没有" —— 而它确实是个产物。
        ctx.asset(
            out, kind="scene", sources=[ctx.asset_of(c) for c in clips],
            meta={"scene": scene.scene_id, "shots": len(clips),
                  "seconds": round(sum(durations), 3), "reused": reused},
        )
        scene_videos.append((scene.scene_id, out, used_shots, durations))

    if not scene_videos:
        report.notes.append("所有场次都没有可用素材 — 拒绝产出空片")
        return report

    # ————————————————————————————----- 全片拼接
    final_raw = paths.episode_video(ctx.root, ctx.run.run_id).with_name("final_video_silent.mp4")
    _concat_mp4(
        [v for _, v, _, _ in scene_videos],
        final_raw,
        paths.run_dir(ctx.root, ctx.run.run_id) / "_work",
    )

    # ————————————————————————————----- 音画对齐 + 时间轴
    segments: List[Tuple[Path, float]] = []
    effects: List[Tuple[Path, float]] = []
    cues: List[Tuple[float, float, str]] = []
    timeline_rows: List[Dict[str, object]] = []
    sfx_missing: List[str] = []
    # 台账里的两批上游 —— 它们回答的是**两个不同的问题**：
    #   * final_sources  成片的直接上游。视频轨来自场次拼接产物（不是单镜片段），
    #                    音频轨来自配音/音效/BGM。只收去重后的 id。
    #   * timeline_sources 时间轴逐镜的来源：这一镜的片段（口型同步过就是同步那段）、
    #                    这句配音、这几条音效。它是"第 12 秒用的是哪次渲染"的答案。
    # 用一条链去回答两个问题，结果就是两个都答不准。
    final_sources: List[Any] = []
    timeline_sources: List[Any] = []
    _seen_final: set = set()
    _seen_tl: set = set()

    def _add(bucket: List[Any], seen: set, path: Optional[Path]) -> None:
        if path is None:
            return
        item = ctx.asset_of(path)
        if item is not None and item.asset_id not in seen:
            seen.add(item.asset_id)
            bucket.append(item)

    def _use(path: Optional[Path]) -> None:
        _add(final_sources, _seen_final, path)

    def _use_tl(path: Optional[Path]) -> None:
        _add(timeline_sources, _seen_tl, path)

    offset = 0.0

    for _sid, _scene_path, shots, durations in scene_videos:
        _use(_scene_path)          # 成片的视频轨就是这些场次拼接产物
        for shot, dur in zip(shots, durations):
            voice = paths.voice(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
            has_voice = voice.exists() and voice.stat().st_size > 0
            line = shot.dialogue.strip()
            if line and has_voice:
                segments.append((voice, offset))
                _use(voice)
                _use_tl(voice)
                # 字幕停留多久：以**配音实际长度**为准（加一点收尾留白），
                # 但不超出这一镜 —— 镜头切了字幕还在，观众会以为说这话的是下一镜的人。
                spoken = probe_duration(voice) or dur
                span = min(dur, max(0.8, spoken + 0.4))
                cues.append((offset, offset + span, media.speech_text(line)))
            # 音效和台词走**同一个** offset：它属于这一镜，起止跟着这一镜。
            # 分开算一遍时间轴就是字幕当初飘掉的同一个错误。
            shot_sfx: List[str] = []
            for idx, cue in enumerate(shot.sfx):
                s_path = paths.sfx(ctx.root, ctx.run.run_id, shot.scene_id,
                                   shot.order, idx)
                if s_path.exists() and s_path.stat().st_size > 0:
                    effects.append((s_path, offset))
                    _use(s_path)
                    _use_tl(s_path)
                    shot_sfx.append(cue)
                else:
                    sfx_missing.append(f"{shot.label}「{cue}」")
            # 这一镜实际用的是哪一段（口型同步过就用同步后的）—— 这正是
            # "成片里第 12 秒用的是哪次渲染"的答案，而不是"应该有"的那一段。
            _use_tl(_clip_for(ctx, shot))
            timeline_rows.append({
                "scene": shot.scene_id,
                "shot": shot.label,
                "start": round(offset, 3),
                "duration": round(dur, 3),
                "dialogue": line,
                "sfx": shot_sfx,
            })
            offset += dur

    tl_path = paths.timeline(ctx.root, ctx.run.run_id)
    tl_path.write_text(
        json.dumps(timeline_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 时间轴记进台账：它描述的是**这一条片子怎么排出来的**，是排查
    # "字幕飘了/音效晚了"时要看的第一个文件。上游是上面收集的那批片段与声音。
    ctx.asset(tl_path, kind="timeline", sources=timeline_sources,
              meta={"rows": len(timeline_rows), "seconds": round(offset, 3)})

    final = paths.episode_video(ctx.root, ctx.run.run_id)
    media.mix_voice_and_music(
        final_raw,
        segments,
        final,
        music=paths.bgm(ctx.root, ctx.run.run_id),
        effects=effects,
    )
    final_raw.unlink(missing_ok=True)

    if not ctx.bundle.sfx and any(s.sfx for s in ctx.episode.shots):
        report.notes.append(
            "[!] 分镜里写了音效，但成片里只有台词和 BGM（没配 sfx provider）"
        )

    # ————————————————————————————----- 字幕
    # SRT 一律写出来（它是交付物：多数平台要的是字幕轨，不是烙在画面上的字），
    # 烧不烧由 ctx.subtitles 决定。这两个开关分开是有意的 ——
    # "我不要字幕" 和 "我要一份能自己调样式的外挂字幕" 是两件事。
    srt_note = "无台词，未生成字幕"
    srt_asset = None
    if cues:
        srt = media.build_srt(cues, paths.subtitles(ctx.root, ctx.run.run_id))
        report.artifacts["subtitles"] = str(srt)
        report.artifacts["timeline"] = str(tl_path)
        srt_asset = ctx.asset(srt, kind="subtitle",
                              meta={"lines": len(cues), "format": "srt",
                                    "burned": bool(ctx.subtitles)})
        if ctx.subtitles:
            size = media.probe_size(final)
            if not size:
                report.notes.append(
                    "[!] 量不出成片画面尺寸，字幕没有烧录（SRT 已写出，可用剪辑软件外挂）"
                )
                report.failed += 1
                srt_note = f"字幕 {len(cues)} 句（SRT 已写出，未烧录：量不出画面尺寸）"
            else:
                width, height = size
                ass = media.build_ass(
                    cues, paths.subtitle_ass(ctx.root, ctx.run.run_id),
                    width=width, height=height,
                )
                subbed = final.with_name("final_video_subbed.mp4")
                media.burn_ass(final, ass, subbed,
                               work_dir=paths.run_dir(ctx.root, ctx.run.run_id) / "_work")
                final.unlink()
                subbed.replace(final)
                report.artifacts["subtitle_script"] = str(ass)
                # ASS 是**从 SRT 的那几句**排出来的，不是独立一份 ——
                # 把这条边记上，改一句台词时才知道要重排哪两个文件。
                ctx.asset(ass, kind="subtitle", derives=[srt_asset] if srt_asset else [],
                          meta={"lines": len(cues), "format": "ass",
                                "canvas": f"{width}x{height}"})
                srt_note = (
                    f"字幕 {len(cues)} 句已烧录（{width}x{height} / 字体 "
                    f"{media.subtitle_font()} / 字号按画面高度版式换算）"
                )
        else:
            # 两个开关都能关掉烧录，就别只报其中一个 —— 报告里点名一个
            # 用户没动过的开关，是最让人怀疑报告的一类小事。
            srt_note = (
                f"字幕 {len(cues)} 句写成 SRT，**未烧录**"
                f"（--no-subtitles 或 AI_DRAMA_SUBTITLES=0）"
            )
    else:
        report.artifacts["timeline"] = str(tl_path)

    report.artifacts["final_video"] = str(final)
    bgm = paths.bgm(ctx.root, ctx.run.run_id)
    if bgm.exists():
        _use(bgm)
    ctx.asset(final, kind="final", sources=final_sources,
              meta={"seconds": round(offset, 3), "scenes": len(scene_videos),
                    "shots": len(timeline_rows), "subtitles": len(cues)})
    report.notes.append(
        f"成片 {final.name} / 约 {offset:.1f}s / 对齐台词 {len(segments)} 句"
    )
    report.notes.append(srt_note)
    ctx.log(f"[concat] [OK] {report.notes[-2]}")
    ctx.log(f"[concat] {srt_note}")
    return report
