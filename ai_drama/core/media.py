"""ffmpeg 封装。

骨架里所有确定性的事情（拼帧、混音、转码、烧字幕）都在这。
这些活儿**不该交给 LLM**：让模型去"想"一条 ffmpeg 命令是纯粹的浪费和风险源，
它们是可复现的工程步骤。
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .models import Shot


class FFmpegMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "ffmpeg not found on PATH.\n"
            "  Windows : winget install Gyan.FFmpeg\n"
            "  macOS   : brew install ffmpeg\n"
            "  Linux   : apt install ffmpeg\n"
            "然后重新开一个终端（PATH 需要刷新）。"
        )


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def require_ffmpeg() -> str:
    exe = find_ffmpeg()
    if not exe:
        raise FFmpegMissing()
    return exe


def require_ffprobe() -> str:
    exe = find_ffprobe()
    if not exe:
        raise FFmpegMissing()
    return exe


def run(cmd: Sequence[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    """跑一条 ffmpeg 命令；失败时把 stderr 尾部带进异常，方便定位。"""
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-8:]
        raise RuntimeError(
            "ffmpeg failed (exit %s):\n  %s\n  cmd: %s"
            % (proc.returncode, "\n  ".join(tail), " ".join(cmd[:6]) + " ...")
        )
    return proc


def probe_duration(path: Path, *, timeout: int = 60) -> float:
    """用 ffprobe 量真实时长；量不出来**抛异常**。

    为什么不用模型给的 duration：那是"打算拍多久"，而这是"实际渲染出来多久"。
    音画对齐必须按后者，否则几秒钟的累积误差就会让最后几句台词飘到别的镜头上去。
    """
    exe = require_ffprobe()
    proc = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-4:]
        raise RuntimeError("ffprobe failed on %s (exit %s): %s"
                           % (path, proc.returncode, " / ".join(tail)))
    raw = (proc.stdout or b"").decode("utf-8", "replace").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise RuntimeError("ffprobe 没给出时长（%s 返回 %r）：文件可能还没写完或是坏的" % (path, raw))


def try_probe_duration(path: Path) -> Optional[float]:
    """量不出来时返回 None。

    调用方**必须**把这个 None 当"没量成"处理并上报 —— 静默当成 0 秒或
    静默跳过，都会让"这段根本没量"这件事在报告里看不出来。
    """
    try:
        return probe_duration(path)
    except Exception:                              # noqa: BLE001
        return None


def image_to_clip(image: Path, out: Path, *, duration: float, fps: int = 24,
                  size: Tuple[int, int] = (720, 1280)) -> Path:
    """把一张静图变成 duration 秒的视频片段（外加极轻微的推镜，模拟运动）。"""
    ff = require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    # 用 zoompan 做一点缓慢推进，比纯静帧更能看出剪辑节奏是否正确
    vf = (
        f"scale={size[0] * 2}:{size[1] * 2},"
        f"zoompan=z='min(zoom+0.0008,1.12)':d={int(duration * fps)}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={size[0]}x{size[1]},"
        f"fps={fps},format=yuv420p"
    )
    run([
        ff, "-y", "-loop", "1", "-i", str(image),
        "-t", f"{duration:.3f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", str(out),
    ])
    return out


def concat_clips(clips: List[Path], out: Path, *, work_dir: Optional[Path] = None) -> Path:
    """无损拼接（先统一转成 ts 再 concat，避免分辨率不一致时炸掉）。"""
    ff = require_ffmpeg()
    if not clips:
        raise ValueError("concat_clips: no clips")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(work_dir or out.parent)
    tmp.mkdir(parents=True, exist_ok=True)

    normalized: List[Path] = []
    for idx, clip in enumerate(clips):
        ts = tmp / f"_norm_{idx:04d}.ts"
        # 注意：这里**不加音频参数**。镜头片段本身是无音的（音频在最后一步混），
        # 如果给无音轨的输入强加 -c:a aac，会得到一个空的音频流，
        # 后面 concat 就会报 "Input link parameters do not match"。
        run([
            ff, "-y", "-i", str(clip),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an",
            "-f", "mpegts", str(ts),
        ])
        normalized.append(ts)

    listing = tmp / "_concat.txt"
    # 必须写**绝对路径**：ffmpeg 的 concat demuxer 是相对"当前工作目录"
    # 解析 file 条目的，而 work_dir 未必就在 cwd 下面。
    # 用相对路径在 "cd repo && python run.py --root D:\\out" 这种用法下必然找不到文件。
    listing.write_text(
        "\n".join(f"file '{c.resolve().as_posix()}'" for c in normalized),
        encoding="utf-8",
    )
    run([
        ff, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", str(out),
    ])
    for f in normalized + [listing]:
        f.unlink(missing_ok=True)
    return out


def mux_audio(video: Path, audio: Path, out: Path, *, fade_out: float = 0.5) -> Path:
    """把音轨贴到画面上。音频短了补静音，长了按视频长度截断。"""
    ff = require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    run([
        ff, "-y", "-i", str(video), "-i", str(audio),
        "-filter_complex",
        f"[1:a]afade=t=out:st={max(0.0, fade_out)}:d={fade_out}[a]",
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out),
    ])
    return out


# ————————————————————————————————————————----
# 无需 ffmpeg 的兜底：直接写 WAV
# ————————————————————————————————————————----


def write_wav(path: Path, *, seconds: float, freq: float = 220.0,
              sample_rate: int = 44100, silent: bool = True,
              amp: int = 7000) -> Path:
    """生成一段 WAV。

    dryrun 用它产出"占位音轨"，形状完全正确（时长、声道、采样率），内容是一声
    带淡入淡出的正弦音 — 这样"音画对齐"能被真的验证：放成片时你能听出每句台词
    落在时间轴的哪里。静音只能验证"有个文件"，验证不了对齐。

    ``amp`` 默认 7000（约 -13 dBFS）：明显能听见，又不至于盖过别的音轨。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * sample_rate)
    frames = bytearray()
    amp = 0 if silent else max(0, min(32767, int(amp)))
    for i in range(n):
        # 淡入淡出，避免爆音
        env = min(1.0, i / (sample_rate * 0.05), (n - i) / (sample_rate * 0.05))
        v = int(amp * env * math.sin(2 * math.pi * freq * i / sample_rate))
        frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return path


# ————————————————————————————————————————----
# 分镜 -> 时长
# ————————————————————————————————————————----

def mix_voice_and_music(
    video: Path,
    segments: List[Tuple[Path, float]],
    out: Path,
    *,
    music: Optional[Path] = None,
    sample_rate: int = 48000,
) -> Path:
    """把每句台词按各自的起始时刻铺到同一条时间轴上，再和画面混流。

    segments = [(音轨文件, 相对全片开头的起始秒), ...]

    这一步是"短剧"和"拼接器"的分水岭：台词必须和对应镜头对齐，
    而不是从头到尾播一遍。骨架把这段对齐逻辑用 ffmpeg filtergraph 实现，
    所以即使换成真 TTS，这段代码也不用改。
    """
    ff = require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    if not segments:
        # 没有任何台词：直接和 BGM 混（或原样输出）
        if music and music.exists():
            return mux_audio(video, music, out)
        out.write_bytes(video.read_bytes())
        return out

    cmd: List[str] = [ff, "-y", "-i", str(video)]
    for voice, _ in segments:
        cmd += ["-i", str(voice)]
    if music and music.exists():
        cmd += ["-i", str(music)]

    parts: List[str] = []
    labels: List[str] = []
    for idx, (_, offset) in enumerate(segments, start=1):
        delay_ms = max(0, int(round(offset * 1000)))
        parts.append(
            f"[{idx}:a]aresample={sample_rate},"
            f"adelay={delay_ms}|{delay_ms},volume=1.0[v{idx}]"
        )
        labels.append(f"[v{idx}]")

    if music and music.exists():
        music_idx = len(segments) + 1
        parts.append(f"[{music_idx}:a]aresample={sample_rate},volume=0.16[bgm]")
        labels.append("[bgm]")

    parts.append(
        "".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0[aout]"
    )

    cmd += ["-filter_complex", ";".join(parts), "-map", "0:v:0", "-map", "[aout]"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]
    run(cmd)
    return out


FRAMING_PACE = {
    "establishing": 3.5,
    "wide": 3.5,
    "medium": 3.0,
    "close": 2.4,
    "extreme_close": 2.0,
}


def estimate_shot_seconds(shot: Shot, *, cps: float = 4.5, min_s: float = 2.0,
                          max_s: float = 8.0) -> float:
    """按台词长度和景别推算镜头时长。

    这是短剧节奏的量化模型：台词决定下限（人说话要时间），景别决定基础节奏。
    骨架先给出一个**可计算、可解释**的公式，而不是让模型随口说一个数字 ---
    以后要调节奏，改这里的参数即可，全片一起变。

    [!] 注意这里的 max_s 是**创作侧**的上限（这个镜头最多值得拍多久），
    不是生成模型的能力上限。后者由 provider 的 ProviderCaps 声明，
    两者的差距由 split_shot_seconds() 负责弥合 —— 不要在这里夹到模型上限，
    否则你等于让"模型能生成多久"偷偷决定了"这场戏该拍多久"。
    """
    base = FRAMING_PACE.get(shot.framing, 3.0)
    speech = len(shot.dialogue.strip()) / cps if shot.dialogue.strip() else 0.0
    need = max(base, speech + 0.4)
    return round(max(min_s, min(max_s, need)), 2)


def plan_shot_split(duration: float, *, max_duration: Optional[float],
                    min_duration: float = 1.0) -> List[float]:
    """把一个镜头的时长切成若干段，每段都不超过 provider 的单段上限。

    返回每段的秒数，总和 == duration（在浮点误差内）。

    为什么是"拆"而不是"截断"：

        一个 12 秒的镜头意味着**画面里有 12 秒的信息量**（三四个动作节拍）。
        把它交给一个上限 8 秒的模型，你会在不报错的情况下拿到一段
        只演完前 2/3 的视频 —— 后半截的动作凭空消失，而成片时长还写着 12 秒。
        正确做法是拆成 8s + 4s 两次生成再接上，每一段都是它自己的完整镜头。

    均分而不是"先塞满"：先塞满会让最后一段只剩 0.5 秒，无法成镜。
    """
    total = max(0.0, float(duration))
    if not total:
        return []
    cap = max_duration if (max_duration and max_duration > 0) else total
    floor = max(0.1, float(min_duration or 0.1))
    if cap > floor or total <= cap:
        cap = max(cap, floor)
    parts = max(1, int(math.ceil(total / cap - 1e-9)))
    each = total / parts
    if each < floor and parts > 1:
        # 切出来会有段落短于下限：少切一段，宁可某一段超一点上限，
        # 也不要生成一串 0.3 秒的碎片（那在成片里就是闪光）。
        parts = max(1, int(math.floor(total / floor)))
        each = total / parts
    out = [round(each, 3) for _ in range(parts)]
    # 把舍入误差补给最后一段，保证总和不变
    out[-1] = round(out[-1] + (total - sum(out)), 3)
    return [p for p in out if p > 0] or [round(total, 3)]


def split_plan_for(ctx, shot: Shot) -> List[float]:
    """按当前 bundle 里 video provider 的声明，算出这个镜头要怎么拆。

    没有 caps 声明的 provider 一律当作不限制 —— **这是刻意的**：
    把上限定低会静默改变你片子的节奏，比不限制更糟。
    """
    from ..providers.base import caps_of

    caps = caps_of(ctx.bundle.video)
    return plan_shot_split(
        shot.duration,
        max_duration=caps.max_duration,
        min_duration=caps.min_duration,
    )
