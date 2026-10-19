"""aihubmix 适配器 —— 目前只实现视频（Seedance 系列）。

为什么单独一家、而不是塞进 dashscope 那个文件：**它不是一家模型厂**，
是一个聚合站。同一个 `sk-` key 背后可以是豆包、可灵、veo。把它写成
``dashscope.py`` 里的一个分支，等于把"哪家模型"这个事实埋进代码里，
而它本来就该是配置。

这一个适配器的存在理由只有一个：**单段时长上限**。百炼的 wanx 系列
一次最多 5 秒，一个 15 秒的镜头会被拆成 3 段，而 3 段是从同一张关键帧
各起各的（见 stage04_video._submit_one），拼起来会看到画面跳回去。
Seedance 一次能出 30 秒，所以 15 秒的镜头是**一条**连续的片子。

三条从 schema 里读出来、不看文档看不出来的事：

  * ``generate_audio`` 默认是 **true**。不显式关掉，每段片段自带一条
    音轨，而拼接阶段还要再铺一次台词 —— 声音会叠两遍。
  * ``duration`` 只收 **4..30 的整数**（或 -1 表示自动）。给 3.5 会被
    参数校验拒掉，所以这里做四舍五入并夹到区间内，**但超过上限不夹**：
    超上限说明上游拆分错了，静默夹会产出时长不对的片子。
  * ``frame_images`` 是 ``[{"frame_type": "first_frame", "image_url": {...}}]``，
    不是干巴巴一个 ``image``。字段名写错的后果是**接口不报错、只是安静地
    忽略**，然后你以为是"这个模型一致性不行"。

契约与别的 VideoProvider 一模一样，特别记两条（stage04 依赖它们）：

  * ``poll()`` 必须自己把产物**下载到 request["dest"]** 并返回那个本地
    路径 —— stage04 从不调用 ``fetch()``，它拿 poll 的第三个返回值
    直接 ``Path(info).exists()``。
  * ``cancel_path`` 在这个服务上是空的，所以 ``cancel()`` 只做本地记账。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import ProviderCaps, ProviderError, Usage, VideoProvider
from .registry import register

BASE_URL = "https://aihubmix.com/ai/v1"
VIDEOS_PATH = "/videos"

#: 单段时长的硬边界，来自 `GET /call/schema/models/<id>/endpoints` 的
#: request schema（duration 的 enum 是 -1 与 4..30）。
MIN_SECONDS = 4.0
MAX_SECONDS = 30.0

#: 重试判定用的 HTTP 状态码。429 是限流，5xx 是它自己抖。
_RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)

#: 画幅。schema 的 enum 就这几个，多一个少一个都会被拒。
_ASPECTS: Tuple[Tuple[str, float], ...] = (
    ("9:16", 9 / 16),
    ("16:9", 16 / 9),
    ("3:4", 3 / 4),
    ("4:3", 4 / 3),
    ("1:1", 1.0),
    ("21:9", 21 / 9),
)

#: 服务端的状态 -> 骨架的三态。``cancelled`` 归到 failed：
#: 对调用方来说"这条不会再出片了"是同一件事。
_STATE = {
    "pending": "running",
    "in_progress": "running",
    "queued": "running",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "failed",
    "canceled": "failed",
}


def _aspect_of(size: Tuple[int, int]) -> str:
    """按长宽比挑最接近的一档画幅。"""
    width, height = size
    if not width or not height:
        return "9:16"
    want = width / float(height)
    return min(_ASPECTS, key=lambda pair: abs(pair[1] - want))[0]


def _resolution_of(size: Tuple[int, int]) -> str:
    """按短边挑分辨率档位。竖屏 720x1280 的短边是 720 → 720p。"""
    short = min(size)
    if short <= 540:
        return "480p"
    if short <= 800:
        return "720p"
    return "1080p"


def _data_url(path: Path) -> str:
    """本地图片转 data URL。

    [!] 这条**还没在真接口上验证过** —— 见模块顶部"待核实"，
    第一次真调用要盯着它。
    """
    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(suffix, "image/png")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


class _Client:
    """一个只会说 JSON 的 HTTP 客户端。零第三方依赖。"""

    def __init__(self, api_key: str, *, base_url: str = BASE_URL, timeout: float = 120.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _error(self, status: int, body: str, label: str) -> ProviderError:
        message, code = body, ""
        try:
            payload = json.loads(body)
            err = payload.get("error") or {}
            code = str(err.get("code") or "")
            message = str(err.get("message") or body)
        except Exception:                                     # noqa: BLE001
            pass
        hint = ""
        if code == "async_not_enabled":
            hint = ("——异步任务没开。去 https://console.aihubmix.com/async-tasks "
                    "打开之后才能提交视频/图片任务。")
        return ProviderError(
            f"{label} HTTP {status} [{code}]: {message}{hint}",
            retryable=status in _RETRYABLE_HTTP or code in ("rate_limit_exceeded",),
            provider="aihubmix",
        )

    def _open(self, req: urllib.request.Request, timeout: Optional[float] = None):
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise self._error(exc.code, body, f"{req.get_method()} {req.full_url}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(f"{req.get_method()} {req.full_url} 连不上：{exc.reason}",
                                retryable=True, provider="aihubmix") from None

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                *, timeout: Optional[float] = None) -> Dict[str, Any]:
        url = self.base_url + path
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        with self._open(req, timeout) as resp:
            raw = resp.read()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ProviderError(f"{method} {path} 返回的不是 JSON：{raw[:200]!r}",
                                retryable=False, provider="aihubmix") from None

    def download(self, url: str, dest: Path, *, timeout: float = 600.0) -> Path:
        """把产物拉到 dest。

        直链是带签名的公开地址，**不需要 Authorization**；这里刻意不转发
        请求头，免得把 key 送到 OSS 上去。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        req = urllib.request.Request(url, method="GET")
        with self._open(req, timeout) as resp, open(part, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
        if part.stat().st_size == 0:
            part.unlink(missing_ok=True)
            raise ProviderError(f"下载到的产物是 0 字节：{url}", retryable=True,
                                provider="aihubmix")
        part.replace(dest)
        return dest


@register("aihubmix", "video")
class AiHubMixVideo(VideoProvider):
    """Seedance 系列：图生视频 / 文生视频，单段 4–30 秒。"""

    name = "aihubmix-video"
    kind = "video"
    caps = ProviderCaps(
        width=720, height=1280, fps=24,
        min_duration=MIN_SECONDS, max_duration=MAX_SECONDS,
        max_ref_images=1,
        notes="Seedance 一次能出 30 秒；超长镜头不用拆，不会出现拼接跳回",
    )

    default_model = "doubao-seedance-2-5-260628"
    model_env = "AI_DRAMA_AIHUBMIX_VIDEO_MODEL"
    POLL_BUDGET = 1800.0

    def __init__(self, model: str = "", client: Optional[_Client] = None):
        self.api_key = (os.environ.get("AI_DRAMA_AIHUBMIX_API_KEY") or "").strip()
        if not self.api_key and client is None:
            raise ProviderError(
                "aihubmix 需要 AI_DRAMA_AIHUBMIX_API_KEY。"
                "没配 key 时**不会**静默降级成假数据 —— 那会让你以为片子是这么出来的。",
                retryable=False, provider=self.name,
            )
        self.model = (model or os.environ.get(self.model_env) or self.default_model).strip()
        self.client = client or _Client(self.api_key)
        self.per_second = self._price_second()
        self._dest: Dict[str, Path] = {}
        self._want: Dict[str, float] = {}
        self._done: Dict[str, str] = {}

    # —————————————————————————————————————————————— 价格
    def _price_second(self) -> float:
        """每秒单价（**美元**）。默认 0 表示"没配"。

        这个服务是按 video token 计费的，没有公开的每秒价 —— 与其编一个
        数字让预算闸门失准，不如默认 0 并在 note 里写明"没配单价"，
        让成本表显示 0 而不是一个假的数。配上
        ``AI_DRAMA_AIHUBMIX_USD_PER_SECOND`` 之后才按金额累计。
        """
        raw = (os.environ.get("AI_DRAMA_AIHUBMIX_USD_PER_SECOND") or "").strip()
        if not raw:
            return 0.0
        try:
            value = float(raw)
        except ValueError:
            raise ProviderError(f"AI_DRAMA_AIHUBMIX_USD_PER_SECOND 不是数字：{raw!r}",
                                retryable=False, provider=self.name) from None
        if value < 0:
            raise ProviderError("每秒单价不能是负数", retryable=False, provider=self.name)
        return value

    # —————————————————————————————————————————————— 提交
    def _duration(self, request: Dict[str, Any]) -> int:
        want = float(request.get("duration") or MIN_SECONDS)
        if want > self.caps.max_duration + 1e-6:
            raise ProviderError(
                f"duration {want:.2f}s 超过 {self.name} 的单段上限 "
                f"{self.caps.max_duration:.0f}s（镜头 {request.get('shot_label', '?')}）。"
                f"拆分应该发生在 core.media.plan_shot_split 里，provider 不做静默截断。",
                retryable=False, provider=self.name,
            )
        return int(max(round(want), int(MIN_SECONDS)))

    def _payload(self, request: Dict[str, Any]) -> Dict[str, Any]:
        size = tuple(request.get("size") or (self.caps.width, self.caps.height))
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": str(request.get("prompt") or "").strip(),
            "duration": self._duration(request),
            "aspect_ratio": _aspect_of(size),           # type: ignore[arg-type]
            "resolution": _resolution_of(size),         # type: ignore[arg-type]
            # 关掉自带音轨：台词/配乐由 stage07 统一铺一次，否则声音叠两遍。
            "generate_audio": False,
        }
        image = request.get("image_path")
        if image and Path(str(image)).exists():
            payload["frame_images"] = [{
                "frame_type": "first_frame",
                "image_url": {"url": _data_url(Path(str(image)))},
            }]
        if not payload["prompt"] and "frame_images" not in payload:
            raise ProviderError("既没有 prompt 也没有首帧，模型无从生成",
                                retryable=False, provider=self.name)
        return payload

    def submit(self, request: Dict[str, Any]) -> str:
        payload = self._payload(request)
        label = f"POST {VIDEOS_PATH}"
        started = time.time()
        data = self.client.request("POST", VIDEOS_PATH, payload)
        task_id = str(data.get("id") or data.get("task_id") or "")
        if not task_id:
            raise ProviderError(f"提交成功但没拿到任务 id：{data}", retryable=False,
                                provider=self.name)
        self._dest[task_id] = Path(str(request["dest"]))
        self._want[task_id] = float(payload["duration"])
        return task_id

    # —————————————————————————————————————————————— 轮询
    def _usage(self, task_id: str, amount: float, *, note: str = "") -> Usage:
        money = amount * self.per_second
        bits = ["[aihubmix]"]
        if note:
            bits.append(note)
        if not self.per_second:
            bits.append("未配每秒单价（这个服务按 token 计费），成本记 0 而不是编一个数")
        return {
            "unit": "second",
            "amount": round(amount, 3),
            "money": round(money, 6),
            "provider": self.name,
            "stage": "video",
            "model": self.model,
            "note": "；".join(bits),
        }

    def _probe(self, path: Path) -> Optional[float]:
        from ..core import media

        return media.try_probe_duration(path)

    def poll(self, job_id: str, *, timeout: float = 300.0
             ) -> Tuple[str, Usage, Optional[str]]:
        data = self.client.request("GET", f"{VIDEOS_PATH}/{urllib.parse.quote(job_id)}")
        raw = str(data.get("status") or "").lower()
        state = _STATE.get(raw, "running")
        if state == "failed":
            err = data.get("error") or {}
            reason = err.get("message") if isinstance(err, dict) else err
            raise ProviderError(
                f"任务 {job_id} 失败（status={raw}）：{reason or data}",
                retryable=False, provider=self.name,
            )
        if state != "succeeded":
            return "running", {}, None

        dest = self._dest.get(job_id)
        if dest is None:
            raise ProviderError(f"任务 {job_id} 成功了，但我不记得它该写到哪",
                                retryable=False, provider=self.name)

        url = self._output_url(data)
        data = data  # 保持可读性：下面只用 url
        if dest.exists() and dest.stat().st_size > 0:
            self._done[job_id] = str(dest)
        else:
            if url:
                self.client.download(url, dest)
            else:
                self.client.download(
                    f"{self.client.base_url}{VIDEOS_PATH}/{urllib.parse.quote(job_id)}/content",
                    dest,
                )
            self._done[job_id] = str(dest)

        want = self._want.get(job_id, 0.0)
        got = self._probe(dest)
        amount = got if got else want
        note = ""
        if got and want and abs(got - want) > 0.5:
            note = f"要 {want:.0f}s，模型出了 {got:.1f}s"
        return "succeeded", self._usage(job_id, amount, note=note), str(dest)

    @staticmethod
    def _output_url(data: Dict[str, Any]) -> str:
        """产物地址可能在 output[].url，也可能在 URL 字段 —— 都试一遍。"""
        out = data.get("output")
        if isinstance(out, list):
            for item in out:
                if isinstance(item, dict):
                    for key in ("url", "video_url", "file_url"):
                        if item.get(key):
                            return str(item[key])
        elif isinstance(out, dict):
            for key in ("url", "video_url", "file_url"):
                if out.get(key):
                    return str(out[key])
        for key in ("url", "video_url", "content_url"):
            if data.get(key):
                return str(data[key])
        return ""

    def fetch(self, job_id: str, dest: Path) -> Usage:
        src = self._done.get(job_id)
        if src and Path(src).exists() and Path(src) != dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            Path(src).replace(dest)
        return {"unit": "file", "amount": 1.0, "money": 0.0,
                "provider": self.name, "stage": "video"}

    def cancel(self, job_id: str) -> None:
        """这个服务没有 cancel 接口（schema 里 cancel_path 是空的）。

        只清本地记账，让调用方知道"别再等它了"。任务在它那边会自己跑完
        并且**照样计费** —— 这一点写进 docstring，比假装取消成功好。
        """
        self._dest.pop(job_id, None)
        self._want.pop(job_id, None)
        self._done.pop(job_id, None)
