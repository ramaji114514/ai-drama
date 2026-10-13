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
