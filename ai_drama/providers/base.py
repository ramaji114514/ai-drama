"""Provider 抽象层 — 全项目最重要的一个文件。

设计目标只有一句话：
    **厂商会换，流水线不该改。**

所以每个 provider 都是同一副接口，骨架里的 stages/* 只认抽象、
永远不认识可灵、即梦、MiniMax 或任何具体厂商。

两个刻意的约定：

1. `submit + poll` 而不是一个阻塞的 `generate()`。
   视频模型动辄排队几分钟，阻塞式接口会让你没法做并发、没法做超时、
   更没法在进程重启后恢复。submit 拿 id，poll 查状态，是唯一正确的形态。

2. 每次调用都返回 `(返回值, usage)`。
   provider 自己不做记账 — 它只报告"我花了多少"，
   由 store 统一落盘。这样成本统计永远和实际调用一一对应，
   不会出现"漏记"或"重记"。
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

Usage = Dict[str, Any]           # {"unit": "token"|"second"|..., "amount": float, "money": float}
TextResult = Tuple[str, Usage]


class ProviderError(RuntimeError):
    """provider 层的统一异常。所有厂商的错误都要归一化成它。"""

    def __init__(self, message: str, *, retryable: bool = False, provider: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.provider = provider


class BudgetExceeded(ProviderError):
    """预算超限。骨架的选择是**显式失败**，而不是静默降级成假数据。"""

    def __init__(self, spent: float, budget: float):
        super().__init__(
            f"budget exceeded: spent ${spent:.4f} / budget ${budget:.4f}",
            retryable=False,
            provider="budget",
        )
        self.spent = spent
        self.budget = budget


def call_with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.0,
                    label: str = "", **kwargs):
    """对一个 provider 调用做退避重试。

    规则（很重要，写在这里省得每个适配器自己发明一套）：

      * 默认重试。真实厂商的错误绝大多数是 429 / 5xx / 网络抖动 —— 这类
        重试一次就好了。把它们默认当致命错误，会让你跑 50 个镜头时
        必然挂在第 30 个上。
      * 只有**明确标了 retryable=False** 的错误（参数非法、内容违规、
        预算超限）才不重试，立刻抛出。这类错误重试只是浪费钱和时间。
      * 退避 1s / 2s / 4s（指数），避免把已经过载的接口打得更死。

    真实项目里这个函数应该再加"同 provider 熔断"，但那是优化不是骨架必需。
    """
    import time as _time

    last: Optional[BaseException] = None
    for i in range(max(1, attempts)):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                      # noqa: BLE001
            last = exc
            retryable = getattr(exc, "retryable", True)
            if not retryable:
                raise
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i)
            if label:
                print(f"    [retry] {label} 第 {i + 1} 次失败（{type(exc).__name__}: {exc}），{delay:.0f}s 后重试",
                      flush=True)
            _time.sleep(delay)
    assert last is not None
    raise last


class TextProvider(abc.ABC):
    """写剧本的模型。可以是云端 LLM，也可以是本地模板引擎。"""

    name = "text-base"

    @abc.abstractmethod
    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 4000,
                 scene: str = "") -> TextResult:
        """返回 (文本, usage)。"""

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": True}


class ImageProvider(abc.ABC):
    """出图。关键帧和定妆照都走它。"""

    name = "image-base"

    @abc.abstractmethod
    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            ref_image: Optional[Path] = None, shot_label: str = "",
            scene: str = "") -> Usage:
        """把图写到 path，返回 usage。ref_image 用于跨镜头一致性（图生图）。"""

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": True}


class VideoProvider(abc.ABC):
    """图生视频 / 文生视频。submit + poll 两段式。"""

    name = "video-base"

    @abc.abstractmethod
    def submit(self, request: Dict[str, Any]) -> str:
        """提交任务，返回 job id。

        request 约定字段（各厂商实现自己挑用得上的）：
            prompt / image_path / duration / size / seed / negative_prompt
        """

    @abc.abstractmethod
    def poll(self, job_id: str, *, timeout: float = 300.0) -> Tuple[str, Usage, Optional[str]]:
        """等待完成，返回 (state, usage, output_path)。

        state ∈ running / succeeded / failed
        实现里必须自己带指数退避，别让上层轮询。
        """

    @abc.abstractmethod
    def fetch(self, job_id: str, dest: Path) -> Usage:
        """把产物下载到 dest。"""

    def cancel(self, job_id: str) -> None:
        return None

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": True}


class AudioProvider(abc.ABC):
    """配音 / 配乐 / 音效。短剧里这一层是刚需，不是可选项。"""

    name = "audio-base"

    @abc.abstractmethod
    def tts(self, text: str, path: Path, *, voice: str = "female_01",
            speed: float = 1.0, shot_label: str = "") -> Usage:
        """台词转语音。同一条台词只允许调用一次（缓存由上层负责）。"""

    @abc.abstractmethod
    def music(self, path: Path, *, duration: float, mood: str = "") -> Usage:
        """整片 BGM。"""


class T2IProvider(abc.ABC):
    """纯文生图（不需要参考图）。定妆照用。"""

    name = "t2i-base"

    @abc.abstractmethod
    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            shot_label: str = "") -> Usage:
        ...


class Bundle:
    """一次 run 用到的全部 provider。

    这就是"依赖注入点"：换厂商只改 build_bundle()，
    stages/ 里的代码一行都不用动。
    """

    def __init__(
        self,
        text: TextProvider,
        image: ImageProvider,
        video: VideoProvider,
        audio: AudioProvider,
        t2i: Optional[T2IProvider] = None,
    ):
        self.text = text
        self.image = image
        self.video = video
        self.audio = audio
        self.t2i = t2i

    def all_providers(self) -> List[Any]:
        return [p for p in (self.text, self.image, self.video, self.audio, self.t2i) if p]

    def report(self) -> List[Dict[str, Any]]:
        return [p.health() for p in self.all_providers()]


# ————————————————————————————————————————----
# 成本核算基准价（2026 年常见量级，仅供骨架算账，接真厂商时按实际改）
# ————————————————————————————————————————----

PRICING: Dict[str, Dict[str, float]] = {
    "llm_per_1k_token": {"in": 0.003, "out": 0.015},
    "image_per_png": {"720x1280": 0.03, "1024x1024": 0.04},
    "video_per_second": {"720p": 0.08, "1080p": 0.20},
    "tts_per_1k_char": {"default": 0.015},
    "music_per_minute": {"default": 0.05},
}


def estimate_tokens(text: str) -> int:
    """粗估 token 数。中英混排按 1 token ≈ 1.6 字符算，够用。"""
    return max(1, int(len(text) / 1.6))
