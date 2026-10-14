"""产物路径约定。

集中在一处，是为了让 dryrun 和真厂商的产物落在同一个位置 ---
否则你切到真模型之后会得到两套目录结构，前一次的缓存全部失效、
断点续跑也就无从谈起。

目录结构：
    runs/<run_id>/
        episode.json          分镜定稿（人工可编辑，改完可 resume）
        character_<name>.png  定妆照
        scenes/sc01/shot_000_keyframe.png
        scenes/sc01/shot_000_clip.mp4
        scenes/sc01/shot_000_audio.wav
        scenes/sc01/scene_video.mp4
        final_video.mp4
        cost_report.json
"""

from __future__ import annotations

from pathlib import Path


def run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / "runs" / run_id


def scene_dir(root: Path, run_id: str, scene_id: str) -> Path:
    return run_dir(root, run_id) / "scenes" / scene_id


def work_dir(root: Path, run_id: str, scene_id: str) -> Path:
    return scene_dir(root, run_id, scene_id) / "_work"


def episode_json(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "episode.json"


def portrait(root: Path, run_id: str, name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "char"
    return run_dir(root, run_id) / f"character_{safe}.png"


def keyframe(root: Path, run_id: str, scene_id: str, order: int) -> Path:
    return scene_dir(root, run_id, scene_id) / f"shot_{order:03d}_keyframe.png"


def clip(root: Path, run_id: str, scene_id: str, order: int) -> Path:
    return scene_dir(root, run_id, scene_id) / f"shot_{order:03d}_clip.mp4"


def clip_part(root: Path, run_id: str, scene_id: str, order: int, part: int) -> Path:
    """长镜头被拆成多段时的第 part 段（从 1 开始）。

    和 clip() 分开放，是为了让"这个镜头到底渲了几段"在文件系统上一眼可见 ——
    排查"某镜为什么比别的贵"时，看目录就知道它被拆了几次。
    """
    return scene_dir(root, run_id, scene_id) / f"shot_{order:03d}_p{part:02d}_clip.mp4"


def clip_parts(root: Path, run_id: str, scene_id: str, order: int) -> Path:
    """该镜头已渲出的所有分段，按段号排序。"""
    return sorted(
        scene_dir(root, run_id, scene_id).glob(f"shot_{order:03d}_p*_clip.mp4")
    )


def voice(root: Path, run_id: str, scene_id: str, order: int) -> Path:
    return scene_dir(root, run_id, scene_id) / f"shot_{order:03d}_audio.wav"


def scene_video(root: Path, run_id: str, scene_id: str) -> Path:
    return scene_dir(root, run_id, scene_id) / "scene_video.mp4"


def episode_video(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "final_video.mp4"


def bgm(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "bgm.wav"


def cost_report(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "cost_report.json"


def qc_report(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "qc_report.json"


def rules_file(root: Path) -> Path:
    """质检规则。放在**仓库根**而不是 run 目录里 —— 它是全项目共用的配置，
    改一次所有片子都跟着变，不该跟着单次 run 走。"""
    return Path(root) / "config" / "qc_rules.yaml"
