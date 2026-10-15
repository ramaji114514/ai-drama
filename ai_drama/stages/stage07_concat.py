"""阶段 7：拼接 + 混音。

两件确定性的事，都该由代码而不是模型来做：

  1. **拼接**：场级拼成本场视频，再拼成全片。
     用 ffmpeg 的 concat demuxer（先统一成 .ts），比逐个 re-encode 快得多，
     也不会因为某两段编码参数不同而炸掉。
  2. **音画对齐**：这是最难也最容易做错的一步。
     每个镜头的台词必须落在**它自己那一镜的时间区间**内，
     而不是简单地从 0 秒开始播一遍。

     做法：按镜头顺序累加**实际时长**（ffprobe 量出来的，不是名义上的 duration），
     得到每句台词在全片时间轴上的起点，再用 ffmpeg 的 adelay 把它们铺平，
     最后和画面一起 amix 混流。

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
from typing import Dict, List, Optional, Tuple

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
        if out.exists() and not ctx.force:
            report.skipped += 1
        else:
            _concat_mp4(clips, out, paths.work_dir(ctx.root, ctx.run.run_id, scene.scene_id))
            report.processed += 1
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

    # ————————————————————————————----- 音画对齐
    segments: List[Tuple[Path, float]] = []
    offset = 0.0
    for _sid, _scene_path, shots, durations in scene_videos:
        for shot, dur in zip(shots, durations):
            voice = paths.voice(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
            if shot.dialogue.strip() and voice.exists() and voice.stat().st_size > 0:
                segments.append((voice, offset))
            offset += dur

    final = paths.episode_video(ctx.root, ctx.run.run_id)
    media.mix_voice_and_music(
        final_raw,
        segments,
        final,
        music=paths.bgm(ctx.root, ctx.run.run_id),
    )
    final_raw.unlink(missing_ok=True)

    report.artifacts["final_video"] = str(final)
    report.notes.append(
        f"成片 {final.name} / 约 {offset:.1f}s / 对齐台词 {len(segments)} 句"
    )
    ctx.log(f"[concat] [OK] {report.notes[-1]}")
    return report
