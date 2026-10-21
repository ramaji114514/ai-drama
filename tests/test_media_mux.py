"""混流不能拿音频去截画面。

`-shortest` 会以**最短的那条流**收尾。音轨比画面早结束时，被砍的是视频：
片尾长镜、BGM 按名义时长生成、最后一句台词说完画面还留着，这几种都很常见。
结果是成片静默地少几秒，而且 QC 只查文件存在，一路判 done —— 没人会发现。

修法是给音轨先 `apad`（补成无限长）再 `-shortest`：能截的只剩音频。

淡出也有同一类错误：`afade=st=0.5` 是从第 0.5 秒开始淡出，
只有 BGM 的镜头会变成"开头半秒有声音，剩下全静音"。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_drama.core import media                        # noqa: E402


def _clip(path: Path, *, seconds: float, color: str = "navy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    media.run([
        media.require_ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s=160x284:d={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
    ])
    return path


def _tone(path: Path, *, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    media.run([
        media.require_ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-ar", "16000", "-ac", "1", str(path),
    ])
    return path


def _mean_volume_db(path: Path, start: float, end: float) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", str(start),
         "-t", str(max(0.05, end - start)),
         "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore").stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
    assert m, out[-500:]
    return float(m.group(1))


@pytest.mark.ffmpeg
def test_a_short_audio_track_does_not_shorten_the_video(tmp_path):
    video = _clip(tmp_path / "v.mp4", seconds=3.0)
    audio = _tone(tmp_path / "a.wav", seconds=0.8)

    out = media.mux_audio(video, audio, tmp_path / "out.mp4")

    assert media.probe_duration(out) == pytest.approx(3.0, abs=0.2)


@pytest.mark.ffmpeg
def test_a_short_line_does_not_shorten_the_video(tmp_path):
    """最常见的形态：3 秒的镜头里只有一句 0.6 秒的台词。"""
    video = _clip(tmp_path / "v.mp4", seconds=3.0)
    voice = _tone(tmp_path / "line.wav", seconds=0.6)

    out = media.mix_voice_and_music(video, [(voice, 0.1)], tmp_path / "out.mp4")

    assert media.probe_duration(out) == pytest.approx(3.0, abs=0.2)


@pytest.mark.ffmpeg
def test_the_fade_lands_at_the_end_not_at_the_beginning(tmp_path):
    video = _clip(tmp_path / "v.mp4", seconds=4.0)
    music = _tone(tmp_path / "m.wav", seconds=4.0)

    out = media.mux_audio(video, music, tmp_path / "out.mp4", fade_out=0.5)

    early = _mean_volume_db(out, 0.4, 0.9)
    tail = _mean_volume_db(out, 3.9, 4.0)
    assert early > -30, f"开头就不该在淡出（{early} dB）"
    assert tail < early - 12, f"淡出没有落在末尾（开头 {early}dB / 结尾 {tail}dB）"


@pytest.mark.ffmpeg
def test_music_only_still_covers_the_whole_shot(tmp_path):
    """只有 BGM 的镜头（`segments` 为空）以前会走到"开头半秒有声音"。"""
    video = _clip(tmp_path / "v.mp4", seconds=2.0)
    music = _tone(tmp_path / "m.wav", seconds=2.0)

    out = media.mix_voice_and_music(video, [], tmp_path / "out.mp4", music=music)

    assert media.probe_duration(out) == pytest.approx(2.0, abs=0.2)
    assert _mean_volume_db(out, 1.0, 1.6) > -40, "中段应当是听得见的"
