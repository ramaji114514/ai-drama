"""流水线阶段。

每个阶段都是 `run(ctx: StageContext) -> StageReport`，彼此独立、可单独重跑。
阶段之间唯一的通信方式是 store + episode.json — 没有隐式全局状态。

顺序：
    01_script      剧本：idea -> 场次 + 镜头表
    02_storyboard  分镜定稿：补全提示词 / 定妆照 / 时长核算
    03_frames      关键帧（并发）
    04_video       镜头视频（并发，带重试；超 provider 单段上限时自动拆分）
    05_audio       台词配音 + BGM（音轨是短剧的刚需，不是可选项）
    06_lipsync     口型同步（并发；只做有台词的镜头，强校验时长不变）
    07_concat      场级拼接 -> 全片 + 混音
    08_qc          质检（确定性、不调模型；三态判定，unchecked 不算通过）
"""

from . import stage01_script as script  # noqa: F401
from . import stage02_storyboard as storyboard  # noqa: F401
from . import stage03_frames as frames  # noqa: F401
from . import stage04_video as video  # noqa: F401
from . import stage05_audio as audio  # noqa: F401
from . import stage06_lipsync as lipsync  # noqa: F401
from . import stage07_concat as concat  # noqa: F401
from . import stage08_qc as qc  # noqa: F401

BY_NAME = {
    "script": script.run,
    "storyboard": storyboard.run,
    "frames": frames.run,
    "video": video.run,
    "audio": audio.run,
    "lipsync": lipsync.run,
    "concat": concat.run,
    "qc": qc.run,
}

__all__ = ["BY_NAME", "script", "storyboard", "frames", "video", "audio",
           "lipsync", "concat", "qc"]
