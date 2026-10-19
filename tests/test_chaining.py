"""续拍：模型一次只给 5 秒时，第二段要从第一段的**最后一帧**接着演。

这两条测的是「接得对不对」，不是「接得好不好看」—— 好看是模型的事，
接错是代码的事。以前的实现是每一段都从镜头关键帧重新起跑，于是拼接起来
观众会看到同一个动作演三遍、画面每 5 秒跳回开头。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_drama.core import media, paths                      # noqa: E402
from ai_drama.stages import stage04_video                   # noqa: E402


def _clip(path: Path, *, seconds: float, color: str) -> Path:
    """造一段纯色片段（不依赖任何 provider）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    media.run([
        media.require_ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s=160x284:d={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
    ])
    return path


@pytest.fixture()
def ctx(tmp_path: Path):
    (tmp_path / "runs").mkdir()
    run = SimpleNamespace(run_id="run_chain")
    return SimpleNamespace(root=tmp_path, run=run)


def test_the_second_part_starts_on_the_last_frame_of_the_first(ctx, monkeypatch):
    shot = SimpleNamespace(scene_id="sc01", order=1)
    first = _clip(paths.clip_part(ctx.root, ctx.run.run_id, "sc01", 1, 1),
                  seconds=1.0, color="red")
    # 第二段还没渲，所以 _chain_frame 抽的是第一段的尾帧
    frame = stage04_video._chain_frame(ctx, shot, 2)
    assert frame is not None and frame.exists() and frame.stat().st_size > 0

    # 尾帧必须是**第一段的**画面（红色），不能是随机一帧或者关键帧
    probe = subprocess.run(
        [media.require_ffmpeg(), "-v", "error", "-i", str(frame),
         "-vf", "crop=1:1:80:142,format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )
    r, g, b = probe.stdout[0], probe.stdout[1], probe.stdout[2]
    assert r > 150 and g < 100 and b < 100, (r, g, b)


def test_the_first_part_has_no_previous_frame_to_chain_from(ctx):
    assert stage04_video._chain_frame(ctx, SimpleNamespace(scene_id="sc01", order=1), 1) is None


def test_a_missing_previous_part_degrades_instead_of_exploding(ctx, monkeypatch):
    """上一段不存在（比如被人删了）时返回 None，调用方退回镜头关键帧。

    宁可安静地穿帮，也不要让整镜失败 —— 因为钱已经花了。
    """
    shot = SimpleNamespace(scene_id="sc01", order=1)
    assert stage04_video._chain_frame(ctx, shot, 3) is None

    # 片段在、但抽帧失败：同样返回 None 而不是抛
    _clip(paths.clip_part(ctx.root, ctx.run.run_id, "sc01", 1, 2), seconds=1.0, color="blue")

    def boom(*a, **k):
        raise RuntimeError("ffmpeg 抽不出帧")

    monkeypatch.setattr(media, "last_frame", boom)
    assert stage04_video._chain_frame(ctx, shot, 3) is None
