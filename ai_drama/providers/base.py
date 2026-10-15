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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

Usage = Dict[str, Any]           # {"unit": "token"|"second"|..., "amount": float, "money": float}
TextResult = Tuple[str, Usage]


# ————————————————————————————————————————----
# 能力声明：把"这家模型能干什么"变成数据，而不是散落在代码里的假设
# ————————————————————————————————————————----

@dataclass(frozen=True)
class ProviderCaps:
    """一个视频 provider 的硬能力边界。

    为什么必须有这个东西（这是本骨架第一版的真实设计缺陷）：

        "每个镜头拍几秒"在上一版里是**创作侧**决定的 —— 由
        `estimate_shot_seconds()` 按台词长度和景别推算，夹到 2–8 秒。
        那 8 秒是"人说话需要多久"的舒适区，**不是生成模型的能力上限**。

        接真厂商的第一天就会踩：有些模型单次最长 5 秒，有些 8 秒，有些 12 秒。
        按 12 秒的信息量写提示词、让一个上限 8 秒的模型去生成，
        出来的必然是**截断**的画面 —— 而且它不会报错，只是默默少了一截。

    所以骨架的规矩是：**创作侧提出"这个镜头需要 N 秒"，
    provider 声明"我一次最多给 M 秒"，两者不符时由骨架负责拆，
    而不是让模型悄悄截断。**

    None 表示"没有这方面限制"（dryrun 与自托管服务常用）。
    """

    width: int = 720
    height: int = 1280
    fps: int = 24
    max_duration: Optional[float] = None   # 单次调用的最长秒数
    min_duration: float = 1.0
    max_duration_ceiling: Optional[float] = None  # 硬顶：超了就报错，不拆
    aspect_ratios: Tuple[str, ...] = ("9:16", "16:9", "1:1")
    supports_image_input: bool = True
    supports_negative_prompt: bool = True
    max_ref_images: int = 1
    notes: str = ""

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def describe(self) -> str:
        if self.max_duration is None:
            dur = "不限"
        else:
            dur = f"{self.min_duration:g}-{self.max_duration:g}s"
        text = (
            f"{self.width}x{self.height}@{self.fps} 单段 {dur} "
            f"| {'图生视频' if self.supports_image_input else '仅文生视频'}"
        )
        # notes 存在的意义就是被看见（比如目录里那条"单段最长 8 秒 ——
        # 15 秒的分镜喂给它必然被截"）。不拼进来，它就只活在这个对象里。
        if self.notes:
            text += f" | {self.notes}"
        return text


# 没有声明 caps 的 provider 用它兜底 —— 保守但绝不限制创作侧。
DEFAULT_CAPS = ProviderCaps(
    max_duration=None,
    notes="provider 未声明能力，按不限制处理（把上限定低会静默改变你的片子节奏）",
)


def caps_of(provider: Any) -> ProviderCaps:
    """读一个 provider 的能力声明。

    用模块级函数而不是给每个类加方法，是为了让**已经写好的旧适配器
    一行都不改**也能继续跑：没有 ``caps`` 属性就当无限制。
    """
    caps = getattr(provider, "caps", None)
    if isinstance(caps, ProviderCaps):
        return caps
    return DEFAULT_CAPS


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
    kind = "text"

    @abc.abstractmethod
    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 4000,
                 scene: str = "") -> TextResult:
        """返回 (文本, usage)。"""

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "ok": True}


class ImageProvider(abc.ABC):
    """出图。关键帧和定妆照都走它。"""

    name = "image-base"
    kind = "image"

    @abc.abstractmethod
    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            ref_image: Optional[Path] = None, shot_label: str = "",
            scene: str = "") -> Usage:
        """把图写到 path，返回 usage。ref_image 用于跨镜头一致性（图生图）。"""

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "ok": True}


class VideoProvider(abc.ABC):
    """图生视频 / 文生视频。submit + poll 两段式。

    ``caps`` 是这个类最重要的一件事：它把"我一次最多能生成几秒"从
    厂商文档里搬进代码，让上游能在**提交任务之前**就发现自己要的时长不合理。
    不声明就是无限制，见 DEFAULT_CAPS。
    """

    name = "video-base"
    kind = "video"
    caps: ProviderCaps = DEFAULT_CAPS

    @abc.abstractmethod
    def submit(self, request: Dict[str, Any]) -> str:
        """提交任务，返回 job id。

        request 约定字段（各厂商实现自己挑用得上的）：

            prompt / image_path / duration / size / seed / negative_prompt
            shot_label / part        # part = (第几段, 共几段)，用于日志与去重

        实现方**必须**遵守 duration：拿到超过 ``caps.max_duration`` 的值时
        应该抛 ProviderError(retryable=False)，而不是自己截断 ——
        静默截断会让成片的节奏莫名其妙，且无法归因。
        """

    @abc.abstractmethod
    def poll(self, job_id: str, *, timeout: float = 300.0) -> Tuple[str, Usage, Optional[str]]:
        """等待完成，返回 (state, usage, output_path)。

        state ∈ running / succeeded / failed
        实现里必须自己带指数退避，别让上层轮询。

        **这个 job_id 可能来自另一次进程**。骨架会把提交成功的 job_id 立刻
        落库（见 stages/stage04_video.py），这样进程在等待中死掉之后，
        resume 能先认领那个已付费的任务、而不是重新提交再付一次钱。
        由此引出两条对实现方的硬要求：

        1. ``poll`` 必须能被一个**从没提交过这个任务**的进程调用 ——
           只要厂商侧还能按 id 查状态就行。把状态藏在进程内存里的实现，
           会让 resume 退化成重复付费。
        2. **查无此人 ≠ 还在跑**。任务已经过期、被清理、id 根本不存在时，
           实现方要返回 ``("failed", {}, "job not found")`` 这种**终局**状态，
           不要抛异常、也不要返回 running —— 抛异常会被上层的容错轮询吞掉并
           一直重试到超时，于是骨架把"任务没了"误判成"还在排队"，这一镜
           就永远认领不到也渲不出来。
        """

    @abc.abstractmethod
    def fetch(self, job_id: str, dest: Path) -> Usage:
        """把产物下载到 dest。"""

    def cancel(self, job_id: str) -> None:
        return None

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "ok": True,
            "caps": self.caps.describe(),
        }


class AudioProvider(abc.ABC):
    """配音 / 配乐 / 音效。短剧里这一层是刚需，不是可选项。"""

    name = "audio-base"
    kind = "audio"

    @abc.abstractmethod
    def tts(self, text: str, path: Path, *, voice: str = "female_01",
            speed: float = 1.0, shot_label: str = "") -> Usage:
        """台词转语音。同一条台词只允许调用一次（缓存由上层负责）。"""

    @abc.abstractmethod
    def music(self, path: Path, *, duration: float, mood: str = "") -> Usage:
        """整片 BGM。"""


class SfxProvider(abc.ABC):
    """音效：环境声与动作声（雨声、门被撞开、心跳、玻璃碎）。

    为什么是**单独一个形态**、而不是 `AudioProvider` 上的第三个方法：

      * 它的粒度完全不同。台词与 BGM 是"全片一条 / 每句一条"，
        音效是"每镜零到三条"—— 一集 60 个镜头可能是 40 次调用，
        而 BGM 永远只有 1 次。把用量差两个数量级的东西塞进同一个 provider，
        `doctor` 和成本表都会变得没法读。
      * 供货方也不是同一家。TTS 与音乐生成是成熟品类，音效（sound effects /
        foley）通常是另一套 API，甚至是一个本地素材库。
      * 它是**可选**的：没配音效时台词与 BGM 照样成立，成片只是"很干净"。
        可选形态必须能被显式关掉并且**关掉时有人说话**（见 registry.OPTIONAL_KINDS）。

    契约只有一条，但很硬：**写出来的文件时长要够覆盖它出现的那一刻**。
    音效比画面短会显得"点到为止"，比画面长会盖住下一镜的声音。
    合成器拿不准时宁可比 cue 稍长一点点（拼接阶段会按镜头切）。
    """

    name = "sfx-base"
    kind = "sfx"

    @abc.abstractmethod
    def make(self, cue: str, dest: Path, *, duration: float = 1.5,
             scene: str = "") -> Usage:
        """按文字描述合成一条音效，写到 dest。

        `cue` 是一句人话（"门被撞开"、"夜雨落在铁皮棚上"），**不是标签**。
        实现方要不要把它当成检索关键词、还是当成生成提示词，自己决定；
        但返回的 usage 必须如实说明"这到底是不是真的音效"——
        dryrun 那一档会老老实实写"合成的是一段噪声，不是雨声"。
        """

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "ok": True}


class T2IProvider(abc.ABC):
    """纯文生图（不需要参考图）。定妆照用。"""

    name = "t2i-base"
    kind = "t2i"

    @abc.abstractmethod
    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            shot_label: str = "") -> Usage:
        ...

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "ok": True}


class LipSyncProvider(abc.ABC):
    """口型同步：把一段已经渲好的画面，按音轨对上嘴。

    为什么单独一个形态、而不是塞进 VideoProvider：它是**已经出片之后**才做的
    一步，输入输出都是片段，跟"生成"是两件事。真实厂商里做这件事的
    （sync.so / latentsync / wav2lip 一类）和做图生视频的通常也不是同一家。

    实现方必须遵守的两条契约（violate 了会静默做出错片）：

      1. **输出不能带音轨**。音轨由 concat 阶段统一铺一次；lip-sync 再塞一条
         进去，成片里每句台词都会说两遍（一遍原声一遍对齐后的）。
      2. **输出时长必须等于输入片段时长**。concat 的音画对齐按 ffprobe 量出的
         **片段实际时长**累加时间轴 —— 这里一改时长，后面所有镜头的声音都会错位。
         对不上的时候：画面该拉伸/定格就拉伸定格，但长度不能变。

    ``sync`` 拿到 ``audio``（该镜的配音，可能不存在）时，只有确实需要对齐才做事；
    没有配音的镜头（空镜、纯动作）上游根本不会调这里。
    """

    name = "lipsync-base"
    kind = "lipsync"

    @abc.abstractmethod
    def sync(self, video: Path, audio: Optional[Path], dest: Path, *,
             shot_label: str = "", scene: str = "") -> Usage:
        """把 video（+ audio）对齐后写到 dest，返回 usage。

        没有 audio 时（该镜无台词）允许直接把 video 重编码到 dest ——
        但要如实报告，别假装同步过。
        """

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "ok": True}


class VisionProvider(abc.ABC):
    """多模态：给一段带图的消息，返回文本。第七种形态。

    为什么单独一个形态：它是**检查**用的，不是生产用的。前面六种形态都在
    "做出东西"，这一种只负责"看一眼已经做出来的东西，然后说话" ——
    它不产出任何交付物，不写文件，不改画面。

    接口刻意做得很笨：**消息进、文本出**。理由是判断逻辑不在 provider 里：

      * 提示词、逐角色最低分门槛、`unchecked`（没查成）的三态语义，
        全都写在 `core/identity.py` 里，而且是**纯函数、离线可测**的
        （不需要任何 API key 就能测"输出乱码时算不算通过"）。
      * provider 的活只剩"把 messages 发出去、把文本拿回来、如实报账"。

    如果反过来把提示词和解析塞进 provider，等于把已经写好并测过的判定逻辑
    复制一份到每个厂商实现里，而且从此再也测不动它。

    ``messages`` 用的是 OpenAI 风格的 ``[{"role": "user", "content": [...]}]``
    （`core/identity.check_messages()` 生成的就是这个），content 里可以混
    ``{"type": "text"}`` 与 ``{"type": "image_url"}``。自研接口的实现方
    自己拆一遍即可 —— 但**拆的时候把 detail 一起考虑**：参考图要看清五官，
    抽帧用低分辨率就够。
    """

    name = "vision-base"
    kind = "vision"

    @abc.abstractmethod
    def chat(self, messages: List[Dict[str, Any]], *,
             scene: str = "") -> TextResult:
        """发出去，拿回 (文本, usage)。

        实现方要如实报账：视觉调用通常按图 + token 计费，
        而这一层是**可选的**（配置里默认不开），花了钱必须看得见。

        失败时抛 `ProviderError` —— 上层会把它变成 `unchecked`
        （没查成），而不是"通过"。这是本形态最重要的一条：
        **看不见不等于没问题。**
        """

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "ok": True}


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
        lipsync: Optional[LipSyncProvider] = None,
        vision: Optional[VisionProvider] = None,
        sfx: Optional[SfxProvider] = None,
        catalog: Any = None,
    ):
        self.text = text
        self.image = image
        self.video = video
        self.audio = audio
        self.t2i = t2i
        self.lipsync = lipsync
        self.vision = vision
        self.sfx = sfx
        #: 模型目录（core.catalog.ModelCatalog）。适配器可以拿它查
        #: 「我这个模型的多长上限、参考素材该放哪个字段」。允许为 None ——
        #: 手搓 bundle 跑测试时不该被迫造一份目录。
        self.catalog = catalog

    def all_providers(self) -> List[Any]:
        return [p for p in (self.text, self.image, self.video, self.audio,
                            self.t2i, self.lipsync, self.vision, self.sfx) if p]

    def report(self) -> List[Dict[str, Any]]:
        out = [p.health() for p in self.all_providers()]
        if self.catalog is not None:
            catalog = getattr(self.catalog, "problem", "")
            out.append({
                "name": "catalog",
                "kind": "catalog",
                "ok": not catalog,
                "problem": catalog,
                "models": len(getattr(self.catalog, "models", {}) or {}),
            })
        return out


# ————————————————————————————————————————----
# 成本核算基准价（2026 年常见量级，仅供骨架算账，接真厂商时按实际改）
# ————————————————————————————————————————----

PRICING: Dict[str, Dict[str, float]] = {
    "llm_per_1k_token": {"in": 0.003, "out": 0.015},
    "image_per_png": {"720x1280": 0.03, "1024x1024": 0.04},
    "video_per_second": {"720p": 0.08, "1080p": 0.20},
    "tts_per_1k_char": {"default": 0.015},
    "music_per_minute": {"default": 0.05},
    "lipsync_per_second": {"default": 0.02},
    # 视觉校验按**次**算：一次调用通常带 1-2 张图。这里是"看一眼"的单价，
    # 不是生产单价 —— 它便宜，但乘上"每镜都查"就不便宜了，
    # 所以质检里默认不开（见 config/qc_rules.yaml）。
    "vision_per_call": {"default": 0.006},
    # 音效按**条**算：一集 60 个镜头可能有 40 条，比 BGM（永远 1 条）
    # 贵两个数量级，所以它默认是**关**的（见 registry.DEFAULT_OFF_KINDS）。
    "sfx_per_cue": {"default": 0.01},
}


def estimate_tokens(text: str) -> int:
    """粗估 token 数。中英混排按 1 token ≈ 1.6 字符算，够用。"""
    return max(1, int(len(text) / 1.6))
