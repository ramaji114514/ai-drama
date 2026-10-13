"""接真厂商的模板 —— 复制这个文件改，不用碰流水线任何一处。

以视频 provider 为例（最麻烦的一个，因为它是 submit + poll 两段式）。
图像 / 语音 / 文本 provider 更简单，照着 base.py 的 ABC 实现即可。

三个必须遵守的约定：

1. 错误统一抛 ProviderError(msg, retryable=...)
   网络抖动 / 429 / 5xx  -> retryable=True   （骨架会指数退避重试 3 次）
   参数非法 / 内容违规 / 余额不足 -> retryable=False（立刻抛出，重试纯属浪费钱）

2. usage 必须如实报告，字段固定：
   {"provider": ..., "stage": ..., "unit": "second"|"call"|"token"|"char",
    "amount": float, "money": float}
   money 按 your-vendor 的真实单价算。基准价见 base.py 的 PRICING。

3. 密钥从环境变量读，永远不要写进代码。
   命名约定：AI_DRAMA_<PROVIDER>_API_KEY / AI_DRAMA_<PROVIDER>_BASE_URL
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 把下面两行换成你的实际 HTTP 客户端。骨架本身不依赖 requests/httpx，
# 你在这里引入什么都可以 —— 装依赖是你的自由，但别把依赖带进 stages/。
# import httpx

from .base import ProviderError, Usage, VideoProvider
from .registry import register


@register("your-vendor", "video")
class YourVendorVideo(VideoProvider):
    """把这里所有 your-vendor / YOUR_VENDOR 换掉。"""

    name = "your-vendor"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        # 构造函数签名随意，只要 build_bundle() 能传进来。
        # 建议支持显式参数（便于测试）和环境变量（便于部署）两条路。
        self.api_key = api_key or os.environ.get("AI_DRAMA_YOUR_VENDOR_API_KEY", "")
        self.base_url = (
            base_url
            or os.environ.get("AI_DRAMA_YOUR_VENDOR_BASE_URL", "")
            or "https://api.your-vendor.example/v1"
        )
        if not self.api_key:
            # 不要静默降级成假数据。缺 key 就明确报错，
            # 让用户知道该配什么 —— 这是本项目对参考实现最重要的修正。
            raise ProviderError(
                "缺少 AI_DRAMA_YOUR_VENDOR_API_KEY；不想调用真厂商就把 "
                "AI_DRAMA_VIDEO=dryrun（默认值）",
                retryable=False,
                provider=self.name,
            )

    # ——————————————————————————————————————— 三个必须实现的方法

    def submit(self, request: Dict[str, Any]) -> str:
        """提交任务，返回 job id。不要在这里等待结果。

        request 由 stages/stage04_video.py 构造，约定字段：
            prompt        str            这一镜要发生什么运动
            image_path    str | None     首帧（图生视频）。None 表示文生视频
            duration      float          秒
            size          (w, h)         竖屏默认 (720, 1280)
            seed          int | None
            negative_prompt str
        """
        # 示例（伪代码，换成你的真实请求）：
        #
        # payload = {
        #     "model": "your-video-model",
        #     "prompt": request["prompt"],
        #     "duration": request.get("duration", 3.0),
        #     "aspect_ratio": "9:16",
        # }
        # if request.get("image_path"):
        #     payload["first_frame"] = _to_data_url(request["image_path"])
        #
        # resp = httpx.post(
        #     f"{self.base_url}/video/submit",
        #     headers={"Authorization": f"Bearer {self.api_key}"},
        #     json=payload,
        #     timeout=30.0,
        # )
        # if resp.status_code == 429 or resp.status_code >= 500:
        #     raise ProviderError(f"submit failed: {resp.status_code}",
        #                         retryable=True, provider=self.name)
        # if resp.status_code >= 400:
        #     raise ProviderError(f"submit rejected: {resp.text[:300]}",
        #                         retryable=False, provider=self.name)
        # return resp.json()["task_id"]
        raise NotImplementedError("把上面这段换成你的真实提交逻辑")

    def poll(self, job_id: str, *, timeout: float = 300.0) -> Tuple[str, Usage, Optional[str]]:
        """轮询到完成，返回 (state, usage, output_url_or_path)。

        state ∈ "running" / "succeeded" / "failed"

        要求：
        - **退避要在这一层做**，别让上层轮询（上层只有一次调用）。
        - 超时抛 ProviderError(retryable=True)。
        - 厂商报"内容违规"这类失败，用 retryable=False —— 重试会一直失败。
        """
        # 示例（伪代码）：
        #
        # deadline = time.monotonic() + timeout
        # delay = 2.0
        # while time.monotonic() < deadline:
        #     resp = httpx.get(f"{self.base_url}/video/{job_id}",
        #                      headers={"Authorization": f"Bearer {self.api_key}"},
        #                      timeout=20.0)
        #     data = resp.json()
        #     status = data.get("status")
        #     if status == "succeeded":
        #         # 单价按你的合同改。这里是"按时长计价"的例子。
        #         seconds = float(data.get("duration", 0.0))
        #         usage = {
        #             "provider": self.name, "stage": "video", "unit": "second",
        #             "amount": seconds, "money": round(seconds * 0.08, 6),
        #         }
        #         return "succeeded", usage, data["video_url"]
        #     if status == "failed":
        #         raise ProviderError(f"generation failed: {data.get('error')}",
        #                             retryable=False, provider=self.name)
        #     time.sleep(delay)
        #     delay = min(delay * 1.5, 10.0)
        #
        # raise ProviderError(f"poll timeout after {timeout}s", retryable=True,
        #                     provider=self.name)
        raise NotImplementedError("把上面这段换成你的真实轮询逻辑")

    def fetch(self, job_id: str, dest: Path) -> Usage:
        """把产物下载到 dest。返回下载这一步的 usage（不计费就给零成本）。

        注意：有些厂商的产物 URL 有效期很短，poll 成功要尽快 fetch。
        """
        # 示例（伪代码）：
        #
        # with httpx.stream("GET", url, timeout=120.0) as r:
        #     r.raise_for_status()
        #     with open(dest, "wb") as f:
        #         for chunk in r.iter_bytes(65536):
        #             f.write(chunk)
        # return {"provider": self.name, "stage": "video", "unit": "call",
        #         "amount": 1.0, "money": 0.0}
        raise NotImplementedError("把上面这段换成你的真实下载逻辑")

    # ——————————————————————————————————————— 可选覆盖

    def cancel(self, job_id: str) -> None:
        """尽力取消。失败也不要抛 —— 这是个 best-effort 接口。"""
        return None

    def health(self) -> Dict[str, Any]:
        """doctor 命令会调用它。别在里面花超过 1 秒。"""
        return {"name": self.name, "ok": bool(self.api_key), "base_url": self.base_url}


# ————————————————————————————————————————————————————————————————
# 写完之后怎么用：
#
# 1) 把这个模块 import 一次（注册靠装饰器生效）。推荐放到你自己的
#    providers/__init__.py 里：
#
#        from . import your_vendor  # noqa: F401
#
# 2) 选它：
#
#        $env:AI_DRAMA_VIDEO="your-vendor"
#        $env:AI_DRAMA_YOUR_VENDOR_API_KEY="xxx"
#        python run.py providers          # 应该能看到 your-vendor
#        python run.py doctor             # 应该报 [OK] video
#        python run.py run --idea "..."
#
# 3) 想省钱试水：先只把 AI_DRAMA_TEXT 换成真模型（剧本质量提升最明显、
#    最便宜），图像和视频继续用 dryrun。跑通再逐层替换。
# ————————————————————————————————————————————————————————————————
