"""ffmpeg 封装。

骨架里所有确定性的事情（拼帧、混音、转码、烧字幕）都在这。
这些活儿**不该交给 LLM**：让模型去"想"一条 ffmpeg 命令是纯粹的浪费和风险源，
它们是可复现的工程步骤。
"""

from __future__ import annotations

import math
import os
import re
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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


def run(cmd: Sequence[str], *, timeout: int = 900,
        cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """跑一条 ffmpeg 命令；失败时把 stderr 尾部带进异常，方便定位。

    ``cwd`` 存在的唯一理由：Windows 上 ffmpeg 的滤镜参数里写绝对路径极易炸
    （盘符的冒号、反斜杠、中文目录都是雷）。烧字幕那类滤镜的参数改成
    **先切到工作目录、再传一个裸文件名**，就能全部绕开。
    """
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
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


#: 支持的画幅 -> 实际像素。
#:
#: 竖屏是短剧的主战场（默认值），但"画幅"不该是一条写在两个调用点里的
#: 字面量：它同时决定出图的 size、图生视频请求里的 size（stage04 送的是
#: `caps.size`）、封面和字幕边距的比例参照。这些只要有一处对不上，
#: 出来的片子就会在中间某个时刻换一次尺寸 —— 而 ffmpeg 不会为此报错。
ASPECTS: Dict[str, Tuple[int, int]] = {
    "9:16": (720, 1280),     # 竖屏（默认）
    "16:9": (1280, 720),     # 横屏
    "1:1": (720, 720),       # 方形
    "4:5": (864, 1080),      # 信息流常见的竖版
}


def normalize_aspect(aspect: str) -> str:
    """把用户写的画幅归一化；不认识就**报错**，不退回默认值。

    退回默认值（比如把 `16:9` 猜成 `9:16`）会让整部横屏片竖着交付，
    而要等到看成片才发现。
    """
    key = (aspect or "").strip().replace("x", ":").replace("×", ":")
    if key in ASPECTS:
        return key
    # 允许直接写像素："720x1280" 也认
    for name, (w, h) in ASPECTS.items():
        if key == f"{w}:{h}":
            return name
    raise ValueError(
        f"不认识的画幅 '{aspect}'。支持：{'、'.join(ASPECTS)}"
        f"（也可以直接写像素，例如 1280x720）"
    )


def frame_size(aspect: str) -> Tuple[int, int]:
    """画幅 -> 实际像素。"""
    return ASPECTS[normalize_aspect(aspect)]


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


def last_frame(video: Path, out: Path) -> Path:
    """抽出视频的**最后一帧**，用来续拍下一段。

    为什么是"最后一帧"而不是"平均一帧"：续拍要的是**动作接得上**，
    也就是第二段的第一帧必须长成第一段的最后一帧的样子。

    ``-sseof -0.4`` 是从结尾往前数 0.4 秒再取帧 —— 直接 seek 到最后一帧
    在关键帧稀疏的视频上会取不到。取 0.4 秒是因为再往前挪就可能落到
    倒数第二帧之前，动作接缝会明显。
    """
    ff = require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    run([
        ff, "-y", "-v", "error", "-sseof", "-0.4", "-i", str(video),
        "-frames:v", "1", "-update", "1", str(out),
    ])
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"抽不出最后一帧：{video}")
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
    effects: Sequence[Tuple[Path, float]] = (),
    sample_rate: int = 48000,
) -> Path:
    """把每句台词、每条音效按各自的起始时刻铺到同一条时间轴上，再和画面混流。

    segments = [(台词音轨, 相对全片开头的起始秒), ...]
    effects  = [(音效音轨, 相对全片开头的起始秒), ...]

    这一步是"短剧"和"拼接器"的分水岭：台词必须和对应镜头对齐，
    而不是从头到尾播一遍。骨架把这段对齐逻辑用 ffmpeg filtergraph 实现，
    所以即使换成真 TTS，这段代码也不用改。

    **音效走的是同一套对齐逻辑**（同一个 offset 来源），只是响度不同：
    台词 1.0、音效 0.55、BGM 0.16。分开调是必须的 —— 音效是"环境"，
    和台词一样响就会把台词盖掉，而盖掉台词是观众唯一会立刻发现的问题。
    """
    ff = require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    fx = [(p, o) for p, o in effects if p and Path(p).exists()]
    if not segments and not fx:
        # 没有任何台词和音效：直接和 BGM 混（或原样输出）
        if music and music.exists():
            return mux_audio(video, music, out)
        out.write_bytes(video.read_bytes())
        return out

    cmd: List[str] = [ff, "-y", "-i", str(video)]
    for voice, _ in segments:
        cmd += ["-i", str(voice)]
    for fx_path, _ in fx:
        cmd += ["-i", str(fx_path)]
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

    fx_base = len(segments)
    for j, (_, offset) in enumerate(fx, start=1):
        delay_ms = max(0, int(round(offset * 1000)))
        parts.append(
            f"[{fx_base + j}:a]aresample={sample_rate},"
            f"adelay={delay_ms}|{delay_ms},volume=0.55[e{j}]"
        )
        labels.append(f"[e{j}]")

    if music and music.exists():
        music_idx = fx_base + len(fx) + 1
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


DEFAULT_SHOT_SECONDS = 15.0


def default_shot_seconds() -> float:
    """这个项目默认一个镜头拍几秒。

    **这是创作参数，不是模型能力。** 模型能出多久由 provider 的
    ProviderCaps 声明，两者刻意分开：把默认值写死在 provider 那边，
    等于让"我换了个模型"偷偷改掉"这场戏该怎么剪"。

    默认 15 秒是有代价的：15 秒里塞一句 0.8 秒的台词，剩下 14 秒全是
    没有声音的画面。所以台词真的短的时候，得靠 stage02 的景别与动作把它
    填满 —— 或者把这个值调小。
    """
    raw = (os.environ.get("AI_DRAMA_SHOT_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_SHOT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SHOT_SECONDS
    return value if value > 0 else DEFAULT_SHOT_SECONDS


def estimate_shot_seconds(shot: Shot, *, cps: float = 4.5, min_s: float = 2.0,
                          max_s: float = 8.0,
                          preferred_s: Optional[float] = None) -> float:
    """按台词长度和景别推算镜头时长。

    这是短剧节奏的量化模型：台词决定下限（人说话要时间），景别决定基础节奏。
    骨架先给出一个**可计算、可解释**的公式，而不是让模型随口说一个数字 ---
    以后要调节奏，改这里的参数即可，全片一起变。

    [!] 注意这里的 max_s 是**创作侧**的上限（这个镜头最多值得拍多久），
    不是生成模型的能力上限。后者由 provider 的 ProviderCaps 声明，
    两者的差距由 split_shot_seconds() 负责弥合 —— 不要在这里夹到模型上限，
    否则你等于让"模型能生成多久"偷偷决定了"这场戏该拍多久"。

    ``preferred_s`` 是**默认片长**（见 default_shot_seconds）：它抬的是下限
    而不是上限 —— 台词比它还长时，镜头仍然跟着台词长。这样"默认 15 秒"
    和"时长跟着台词走"不是互相打架的两条规则，而是同一个 max() 的两项。
    """
    if preferred_s is None:
        preferred_s = default_shot_seconds()
    base = FRAMING_PACE.get(shot.framing, 3.0)
    speech = len(shot.dialogue.strip()) / cps if shot.dialogue.strip() else 0.0
    need = max(base, speech + 0.4, preferred_s)
    # 台词是**硬下限**：max_s 只约束"没台词时最多松到多久"，
    # 不能让一句 30 秒的话被剪成 15 秒 —— 那等于把话说一半。
    ceiling = max(max_s, preferred_s, speech + 0.4)
    return round(max(min_s, min(ceiling, need)), 2)


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


# ———————————————————————————————————————————————————————————— 字幕
#
# 为什么自己生成 ASS、而不是直接拿 SRT 去烧：
#
# libass 读 SRT 时会**假定脚本坐标系是 384x288**，于是 force_style 里的
# FontSize / MarginV 都不是像素，而是按 (视频高度 / 288) 放大的相对值。
# 实测 720x1280 下 FontSize=22 渲染出约 90px 高的字、MarginV=40 直接贴到画面
# 底部；同一个字号换到 1080p 又是另一个大小（加 original_size= 也没用，
# 它改的是"这份字幕是按多大尺寸排的"，不改变坐标系）。
#
# 自己写 ASS 并把 PlayResX / PlayResY 设成**视频的真实尺寸**，FontSize 与
# MarginV 就是真正的像素 —— 竖屏横屏一套参数都成立，想调多大就写多大。
# 实测确认：PlayRes 720x1280 + Fontsize 44 → 字高约 44px，中文正常渲染。


_FONT_CANDIDATES = (
    ("win32", ("Microsoft YaHei", "微软雅黑", "SimHei", "SimSun",
               "Noto Sans CJK SC", "Arial")),
    ("darwin", ("PingFang SC", "Hiragino Sans GB", "STHeiti", "Songti SC",
                "Noto Sans CJK SC", "Helvetica")),
    ("linux", ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei",
               "Noto Sans SC", "DejaVu Sans")),
)


def subtitle_font() -> str:
    """烧字幕用的字体名（libass 按名字找系统字体，不是按文件路径）。

    默认取当前平台上第一个常见的 CJK 字体。**中文片千万别用默认的 sans** ——
    libass 找不到能画汉字的字体时不是报错，而是画出一排方块（豆腐块），
    那种成片看上去像渲染事故。
    要换字体设环境变量 AI_DRAMA_SUBTITLE_FONT（写字体名，不是路径）。
    """
    override = (os.environ.get("AI_DRAMA_SUBTITLE_FONT") or "").strip()
    if override:
        return override
    plat = "win32" if sys.platform.startswith("win") else (
        "darwin" if sys.platform == "darwin" else "linux")
    for name, fonts in _FONT_CANDIDATES:
        if name == plat:
            return fonts[0]
    return "Noto Sans CJK SC"


def probe_size(path: Path, *, timeout: int = 60) -> Optional[Tuple[int, int]]:
    """量视频画面的宽高；量不出来返回 None。

    字幕的字号与边距按画面高度换算，所以这个数必须来自**成片本身**，
    不能拿 provider 声明的尺寸顶替 —— 中间任何一步（拆段、lip-sync、
    concat）改了分辨率，声明值就和实际对不上了。
    """
    exe = require_ffprobe()
    proc = subprocess.run(
        [exe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    if not raw:
        return None
    parts = raw[0].strip().split("x")
    if len(parts) != 2:
        return None
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (w, h) if w > 0 and h > 0 else None


def _srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: List[Tuple[float, float, str]], path: Path) -> Path:
    """写出标准 SRT。

    烧录用的是 build_ass()，这份 SRT 是**给平台和剪辑软件用的交付物** ——
    真要发布时多数平台更希望你上传一份干净的字幕轨而不是烙在画面上的字。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: List[str] = []
    for idx, (start, end, text) in enumerate(cues, start=1):
        blocks.append(
            f"{idx}\n{_srt_time(start)} --> {_srt_time(end)}\n{text.strip()}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


_SPEAKER_PREFIX = re.compile(r"^\s*[^：:（(]{1,12}\s*[：:]\s*")
_STAGE_DIRECTION = re.compile(r"[（(][^（()）]{0,40}[)）]")


def speech_text(line: str) -> str:
    """把剧本行变成"屏幕上该显示的那句话"。

    剧本行是给演员和配音看的：``苏蔓：（低声）不对。`` 里的说话人和语气提示
    烙到画面上就是业余 —— 观众看到的是"谁在说"和"怎么说"，不是台词本身。

    去掉两样：圆括号里的表演提示、行首的说话人前缀。
    去完什么都不剩就退回原文：显示一句带提示的台词，总比显示空白强。
    """
    out = _STAGE_DIRECTION.sub("", line or "")
    out = _SPEAKER_PREFIX.sub("", out)
    out = " ".join(out.split())
    return out or (line or "").strip()


def speaker_of(line: str) -> str:
    """台词行首的说话人（``苏蔓：（低声）不对。`` -> ``苏蔓``），没有则空串。

    用来给同一个角色固定同一个音色（见 stages/stage05_audio.py）。
    注意匹配的是``说话人：``这个形状，所以 ``没有前缀的台词`` 返回空串 ——
    它自然地被当成"speaker 字段缺失"，退回全局音色，而不是把半句话当人名。
    """
    m = _SPEAKER_PREFIX.match(line or "")
    if not m:
        return ""
    return m.group(0).rstrip("：: \t").strip()


def _ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    """把可能被 ASS 当成语法的字符换成全角。

    ``{}`` 在 ASS 里是特效覆盖块的分界，``\\`` 后面跟 N/n/h 会被当成换行/硬空格。
    台词是模型写的，出现这些字符的概率不为零，而一旦出现，轻则样式乱掉、
    重则整句消失。换成全角是最省事又看得出区别的处理。
    """
    out = text.replace("{", "｛").replace("}", "｝")
    out = out.replace("\\", "＼")
    return out.replace("\r", "").replace("\n", "\\N").strip()


def build_ass(
    cues: List[Tuple[float, float, str]],
    path: Path,
    *,
    width: int,
    height: int,
    font: str = "",
    font_ratio: float = 0.042,
    margin_v_ratio: float = 0.075,
    margin_lr_ratio: float = 0.083,
) -> Path:
    """生成带真实 PlayRes 的 ASS（字号/边距都是像素）。

    尺寸全部按**画面高度的比例**换算，所以同一套参数在 720x1280 和 1920x1080
    上观感一致；要改观感就改这三个比例，不要改像素值。
    """
    name = font or subtitle_font()
    font_px = max(12, int(round(height * font_ratio)))
    margin_v = max(8, int(round(height * margin_v_ratio)))
    margin_lr = max(8, int(round(width * margin_lr_ratio)))
    outline = max(1, int(round(font_px * 0.045)))

    head = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        # 描边是必须的：短剧画面明暗变化大，白字压在浅色背景上会直接看不见。
        f"Style: Default,{name},{font_px},&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,2,"
        f"{margin_lr},{margin_lr},{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    lines: List[str] = []
    for start, end, text in cues:
        body = _ass_escape(text)
        if not body:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{body}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def burn_ass(video: Path, ass: Path, out: Path, *,
             work_dir: Optional[Path] = None) -> Path:
    """把 ASS 烧进画面。

    ASS 文件先复制进工作目录、再以**裸文件名**传给滤镜，并且让 ffmpeg
    在这个目录里跑 —— Windows 下滤镜参数里的绝对路径（盘符冒号、反斜杠、
    中文目录）是踩过几次的雷，绕开它比转义它省事得多。
    """
    ff = require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    work = work_dir or (out.parent / "_subwork")
    work.mkdir(parents=True, exist_ok=True)
    staged = work / "_sub.ass"
    staged.write_bytes(ass.read_bytes())
    cmd = [
        ff, "-y",
        "-i", str(video.resolve()),
        "-vf", "ass=_sub.ass",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out.resolve()),
    ]
    try:
        run(cmd, cwd=work)
    finally:
        staged.unlink(missing_ok=True)
    return out
