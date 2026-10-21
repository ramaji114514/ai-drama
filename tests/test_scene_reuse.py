"""场次成片能不能复用，不能只看"文件在不在"。

重渲一个镜头之后 `resume --stages concat`（不带 --force）会怎样：
时间轴按**这批新片段**重新量，画面却还是旧成片 —— 画面旧、字幕新，
整场音画错位，而 QC 只查文件存在，一路判 done，用户带着错片发布。

所以复用前要比两件事：素材有没有比成片新，成片长度对不对得上。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_drama.stages import stage07_concat      # noqa: E402


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def test_a_missing_scene_video_is_stale(tmp_path):
    clip = _touch(tmp_path / "c.mp4", 100.0)
    assert stage07_concat._scene_is_stale(tmp_path / "nope.mp4", [clip], [3.0])


def test_a_clip_rendered_after_the_scene_video_makes_it_stale(tmp_path, monkeypatch):
    """这就是那个真 bug：重渲之后成片比素材旧。"""
    monkeypatch.setattr(stage07_concat, "probe_duration", lambda p: 3.0)
    out = _touch(tmp_path / "scene.mp4", 100.0)
    clip = _touch(tmp_path / "c.mp4", 200.0)
    assert stage07_concat._scene_is_stale(out, [clip], [3.0])


def test_a_length_mismatch_makes_it_stale(tmp_path, monkeypatch):
    """片段被换成了别的时长（模型重出、trim 变了），画面就对不上了。"""
    out = _touch(tmp_path / "scene.mp4", 200.0)
    clip = _touch(tmp_path / "c.mp4", 100.0)
    monkeypatch.setattr(stage07_concat, "probe_duration", lambda p: 3.0)
    assert stage07_concat._scene_is_stale(out, [clip], [5.0])


def test_a_fresh_scene_video_is_reused(tmp_path, monkeypatch):
    out = _touch(tmp_path / "scene.mp4", 200.0)
    clip = _touch(tmp_path / "c.mp4", 100.0)
    monkeypatch.setattr(stage07_concat, "probe_duration", lambda p: 3.0)
    assert not stage07_concat._scene_is_stale(out, [clip], [3.0])


def test_an_unmeasurable_scene_video_is_stale(tmp_path, monkeypatch):
    """量不出长度就无法证明它和素材对得上 —— 宁可重拼。"""
    out = _touch(tmp_path / "scene.mp4", 200.0)
    clip = _touch(tmp_path / "c.mp4", 100.0)
    monkeypatch.setattr(stage07_concat, "probe_duration", lambda p: None)
    assert stage07_concat._scene_is_stale(out, [clip], [3.0])


def test_a_missing_clip_makes_it_stale(tmp_path, monkeypatch):
    out = _touch(tmp_path / "scene.mp4", 200.0)
    monkeypatch.setattr(stage07_concat, "probe_duration", lambda p: 3.0)
    assert stage07_concat._scene_is_stale(
        out, [tmp_path / "gone.mp4"], [3.0])
