"""阿里云百炼（DashScope）适配器 —— 本项目第一个接真厂商的实现。

为什么是百炼：它一个 key 就把六种形态里的五种覆盖了（文本 / 出图 /
图生视频 / 配音 / 视觉校验），国内直连不用代理，而且都是 HTTP + 异步任务，
不需要任何 SDK —— 这跟"骨架本身零依赖"是相容的。

    $env:AI_DRAMA_DASHSCOPE_API_KEY = "sk-..."
    $env:AI_DRAMA_TEXT   = "dashscope"     # 剧本
    $env:AI_DRAMA_T2I    = "dashscope"     # 定妆照
    $env:AI_DRAMA_IMAGE  = "dashscope"     # 关键帧
    $env:AI_DRAMA_VIDEO  = "dashscope"     # 片段
    $env:AI_DRAMA_AUDIO  = "dashscope"     # 台词配音
    $env:AI_DRAMA_VISION = "dashscope"     # 质检时"看一眼"
    python run.py doctor

  建议**一层一层换**：先只换 TEXT（最便宜、对剧本质量提升最直接），
  跑通一集再把 IMAGE / VIDEO 换掉。整集一次性切到真模型之前，
  先用 `--budget` 卡一道 —— 视频按秒计费，一集几十秒就是几块钱。

三条比功能更重要的约定（都来自 _template.py，这里逐一落实）：

1. **缺 key 就报错，不静默降级成假数据。** 每个类的构造函数都会检查
   `AI_DRAMA_DASHSCOPE_API_KEY`，没有就抛 ProviderError(retryable=False)。
   假装成功比失败更贵：你会以为出片了，其实看的是占位图。

2. **usage 如实报账。** 单价写在下面的 `_price()` 调用里（2026-10 从百炼
   模型页核对：文生图 0.14 元/张、图片编辑 0.14 元/张、
   wanx2.1-i2v-turbo 480P 0.24 元/秒、qwen3-tts-flash 0.8 元/万字符）。
   厂商会调价，所以每个单价都能用环境变量覆盖 —— 成本表失真比不报还糟。

   **单位要小心**：百炼的价目表是**元**，而这个项目的钱**一律是美元**
   （`models.py:288` 的 `money: float  # 折算成美元`，CLI 的 `--budget`
   也是美元）。所以单价按元写、出门前按 `AI_DRAMA_DASHSCOPE_USD_RATE`
   （默认 7.1，只是个能用的默认值，不是某天的牌价）折成美元。
   想精确对账就自己把汇率设准。

3. **不骗调用方。** 两处特别说明，别被静默行为坑到：
   * `music()` —— 百炼**没有**配乐生成接口。这里宁可写一段本地合成的
     占位音轨，也不假装生成了 BGM，usage 的 note 会写明这件事。
   * `fetch()` —— `wanx2.1-i2v-turbo` 不接受 duration，永远出 5 秒。
     所以拿到产物后会**按请求时长裁一刀**（用 ffmpeg），否则 3 秒的镜头
     会变成 5 秒，整条时间轴跟着漂。裁没裁都写在 note 里。

单价与模型名都可以改：`AI_DRAMA_DASHSCOPE_*_MODEL` / `AI_DRAMA_DASHSCOPE_PRICE_*`。
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    AudioProvider,
    ImageProvider,
    LipSyncProvider,
    ProviderCaps,
    ProviderError,
    T2IProvider,
    TextProvider,
    Usage,
    VideoProvider,
    VisionProvider,
)
from .registry import register

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"

#: 百炼按人民币计价，本项目的钱一律是美元（见 `_DashScope._rate`）。
#: 7.1 只是个能用的默认值，不是某天的牌价 —— 要精确对账就自己设
#: `AI_DRAMA_DASHSCOPE_USD_RATE`。
DEFAULT_USD_RATE = 7.1

CHAT_PATH = "/compatible-mode/v1/chat/completions"
T2I_PATH = "/api/v1/services/aigc/text2image/image-synthesis"
IMAGE_EDIT_PATH = "/api/v1/services/aigc/image2image/image-synthesis"
VIDEO_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
#: VideoRetalk（口型同步）挂在与图生视频**同一个** path 上，靠 model 名区分。
LIPSYNC_PATH = "/api/v1/services/aigc/image2video/video-synthesis"
UPLOAD_PATH = "/api/v1/uploads"
TASK_PATH = "/api/v1/tasks/{task_id}"

# 这两种 HTTP 状态是可以重试的：429 是排队挤不上，5xx 是对方抖动。
# 其余（400 参数非法 / 401 key 不对 / 403 没权限 / 404 模型名写错）
# 重试多少次都是同一个结果，只会白烧调用量。
_RETRYABLE_HTTP = (429, 500, 502, 503, 504)

# 百炼把"能重试"的错误也放在 200/400 的 body 里，光看状态码不够。
_RETRYABLE_CODES = frozenset({
    "Throttling", "Throttling.RateQuota", "Throttling.AllocationQuota",
    "limit_requests", "ServiceUnavailable", "InternalError", "RequestTimeout",
    "ModelLoading", "AsyncTaskQueueFull",
})


# —————————————————————————————————————————————————————————— HTTP 小客户端


class _Client:
    """够用的 urllib 封装：POST / GET / 下载。

    只做三件事：拼 header、把百炼的错误翻译成 ProviderError、把响应解成 dict。
    特意**不用 requests** —— 骨架零依赖，装了 requests 反而让"拷走就能跑"
    这件事多一道门槛。
    """

    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 下载用的 opener 对，见 _openers()。建一次就够，别每次下载都重建。
        self._opener_cache: Optional[List[Tuple[str, Any]]] = None

    # —— 内部

    def _request(self, url: str, *, data: Optional[bytes] = None, method: str = "GET",
                 extra: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
                 label: str = "") -> bytes:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            code, message = _parse_error(raw)
            retryable = exc.code in _RETRYABLE_HTTP or code in _RETRYABLE_CODES
            raise ProviderError(
                f"{label or method} HTTP {exc.code}"
                + (f" [{code}]" if code else "")
                + (f": {message}" if message else ""),
                retryable=retryable,
                provider="dashscope",
            ) from None
        except urllib.error.URLError as exc:
            # 连不上、DNS 挂了、超时 —— 这类都值得重试
            raise ProviderError(f"{label or method} 连不上: {exc.reason}",
                                retryable=True, provider="dashscope") from None
        except Exception as exc:  # noqa: BLE001
            # `http.client.RemoteDisconnected` 是 ConnectionResetError、不是
            # URLError，上面那条接不住它。不兜这一层，调用方会收到一个
            # 来自 http.client 的堆栈，而它跟"百炼出错了"完全看不出关系。
            raise ProviderError(f"{label or method} 连接被掐断: "
                                f"{type(exc).__name__}: {exc}",
                                retryable=True, provider="dashscope") from None

    def _json(self, raw: bytes, label: str) -> Dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{label} 返回的不是 JSON: {raw[:200]!r} ({exc})",
                                retryable=True, provider="dashscope") from None
        if not isinstance(data, dict):
            raise ProviderError(f"{label} 返回结构不认识: {str(data)[:200]}",
                                retryable=True, provider="dashscope")
        return data

    # —— 对外

    def post(self, path: str, payload: Dict[str, Any], *, async_: bool = False,
             oss: bool = False, timeout: Optional[float] = None,
             label: str = "") -> Dict[str, Any]:
        extra: Dict[str, str] = {}
        if async_:
            extra["X-DashScope-Async"] = "enable"
        if oss:
            # 用 `oss://` 开头的临时 URL 时必须显式带这个头，
            # 否则服务端不认这个协议，报"URL 无效"。
            extra["X-DashScope-OssResourceResolve"] = "enable"
        raw = self._request(self.base_url + path, data=json.dumps(payload).encode("utf-8"),
                            method="POST", extra=extra or None, timeout=timeout,
                            label=label or path)
        return self._json(raw, label or path)

    def get(self, path: str, *, query: Optional[Dict[str, str]] = None,
            timeout: Optional[float] = None, label: str = "") -> Dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        raw = self._request(url, timeout=timeout, label=label or path)
        return self._json(raw, label or path)

    # —— opener：直连与走系统代理各一个

    def _openers(self) -> List[Tuple[str, Any]]:
        """两个 opener，按"先直连、再系统代理"的顺序试。

        为什么非要有一个"直连"的：百炼给的产物地址是**带签名的 OSS 直链**，
        不需要任何凭证，谁拿到都能下。而 Windows 上的系统代理设置会被
        urllib **自动**读进来（`getproxies()` 直接读注册表），curl 不读 ——
        所以会出现"同一条地址 curl 秒下、urllib 必然超时"这种看着像
        "百炼给的地址是坏的"的怪事。

        实测踩过：产物地址是 `http://dashscope-xxxx.oss-cn-shanghai.aliyuncs.com/...`，
        `urllib` 走系统代理时稳定 `TimeoutError` / `RemoteDisconnected`，
        而 `curl` 直连 200。所以先直连，直连不通再退回系统代理
        —— 后者是有些内网唯一的出门方式，不能直接扔掉。
        """
        if self._opener_cache is None:
            self._opener_cache = [
                ("直连", urllib.request.build_opener(urllib.request.ProxyHandler({}))),
                ("系统代理", urllib.request.build_opener()),
            ]
        return self._opener_cache

    def download(self, url: str, dest: Path, *, timeout: float = 300.0,
                 attempts: int = 3) -> None:
        """把产物拉到 dest。

        不带头 —— 百炼给的产物地址是带签名的 OSS 直链，塞上 Authorization
        反而会被 OSS 拒掉。

        这里要比别处多兜两层：

          * `http.client.RemoteDisconnected` 是 `ConnectionResetError`，
            **不是** `URLError`，`except URLError` 接不住它，会以一个和
            本模块毫不相干的堆栈冒到调用方去。
          * 产物已经付过钱了，所以既重试、也换出口（直连 / 系统代理）再试。
        """
        req = urllib.request.Request(url, headers={"User-Agent": "ai-drama/0.1"})
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tried: List[str] = []
        for attempt in range(1, max(1, attempts) + 1):
            for label, opener in self._openers():
                try:
                    with opener.open(req, timeout=timeout) as resp, \
                            open(tmp, "wb") as fh:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            fh.write(chunk)
                except urllib.error.HTTPError as exc:
                    # 签名过期 / 地址被改过这类重试无意义，立刻报
                    tmp.unlink(missing_ok=True)
                    raise ProviderError(
                        f"下载产物失败 HTTP {exc.code}",
                        retryable=exc.code in _RETRYABLE_HTTP,
                        provider="dashscope") from None
                except Exception as exc:  # noqa: BLE001  URLError / RemoteDisconnected / 超时
                    tmp.unlink(missing_ok=True)
                    tried.append(f"{label}→{type(exc).__name__}: {exc}")
                    continue
                if not tmp.stat().st_size:
                    tmp.unlink(missing_ok=True)
                    tried.append(f"{label}→拿到 0 字节")
                    continue
                tmp.replace(dest)
                return
            if attempt < attempts:
                time.sleep(2.0 * attempt)
        raise ProviderError(f"下载产物失败（试了 {attempts} 轮）：" + "；".join(tried[-3:]),
                            retryable=True, provider="dashscope")

    def upload(self, path: Path, *, model: str, timeout: float = 900.0) -> str:
        """把本地文件传进百炼的临时存储，拿回一个 `oss://` 开头的地址。

        为什么非绕这一道：口型同步只收**公网可访问**的 http(s) 或 `oss://`
        地址，不像图片接口那样吃 data URL（同样一个 base64 视频，几 MB 起步，
        塞进请求体也是自找没趣）。所以本地片段必须先上传。

        三条容易踩的规矩，都写在文档里：

          * 文件**和模型绑定** —— 这里报 `model` 必须和后面调用时的 model
            一模一样，name 对不上时报"文件不存在"，不说为什么。
          * `x-oss-forbid-overwrite=true` —— 同名文件传第二次会被拒。
            所以 key 里的文件名带一串随机后缀，不靠文件名去区分。
          * 凭证 300 秒有效、URL 48 小时有效。一次调用里用完即可。

        上传的那个 host 不是百炼的 API host、也不要 Authorization 头，
        所以这里绕开 `_request` 自己拼。
        """
        policy = self.get(UPLOAD_PATH, query={"action": "getPolicy", "model": model},
                          label="取上传凭证").get("data") or {}
        missing = [k for k in ("upload_host", "upload_dir", "oss_access_key_id",
                               "signature", "policy") if not policy.get(k)]
        if missing:
            raise ProviderError(f"上传凭证里少了 {', '.join(missing)}：{str(policy)[:200]}",
                                retryable=True, provider="dashscope")

        # 文件名唯一：x-oss-forbid-overwrite 开着，重名会被拒。
        name = f"{path.stem}-{uuid.uuid4().hex[:12]}{path.suffix}"
        key = f"{policy['upload_dir']}/{name}"
        fields = [
            ("OSSAccessKeyId", policy["oss_access_key_id"]),
            ("policy", policy["policy"]),
            ("Signature", policy["signature"]),
            ("key", key),
            ("x-oss-object-acl", policy.get("x_oss_object_acl") or "private"),
            ("x-oss-forbid-overwrite", policy.get("x_oss_forbid_overwrite") or "true"),
            ("success_action_status", "200"),
        ]

        limit_mb = float(policy.get("max_file_size_mb") or 0) or 0.0
        size_mb = path.stat().st_size / (1024 * 1024)
        if limit_mb and size_mb > limit_mb:
            raise ProviderError(
                f"{path.name} 有 {size_mb:.1f}MB，超过 {model} 允许的 {limit_mb:.0f}MB",
                retryable=False, provider="dashscope")

        # 表单体写到临时文件再以文件对象发出去 —— 视频几 MB 到几十 MB，
        # 全读进内存没必要；设了 Content-Length 就不会退化成 chunked。
        body = path.with_name(path.name + ".upload-body")
        boundary = "----ai-drama" + uuid.uuid4().hex
        sep = b"--" + boundary.encode("ascii") + b"\r\n"
        try:
            with open(body, "wb") as fh:
                for k, v in fields:
                    fh.write(sep)
                    fh.write(f'Content-Disposition: form-data; name="{k}"'
                             .encode("utf-8") + b"\r\n\r\n")
                    fh.write(str(v).encode("utf-8") + b"\r\n")
                # file 必须是最后一个表单域。
                fh.write(sep)
                fh.write(f'Content-Disposition: form-data; name="file"; '
                         f'filename="{name}"'.encode("utf-8") + b"\r\n")
                fh.write(b"Content-Type: application/octet-stream\r\n\r\n")
                with open(path, "rb") as src:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                fh.write(b"\r\n--" + boundary.encode("ascii") + b"--\r\n")
            total = body.stat().st_size

            req = urllib.request.Request(
                policy["upload_host"], method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with open(body, "rb") as fh:
                req.data = fh                                    # 文件对象：流式发
                # Content-Length 必须**在** `req.data =` 之后设：
                # Request.data 的 setter 会顺手把已有的 Content-Length 删掉
                # （为了不让它和新的 body 对不上，CPython bpo-16464）。
                # 先设就会被无声地抹掉，退化成 chunked 传输 —— OSS 的表单
                # 上传不吃 chunked，报的错还跟长度毫无关系。
                req.add_header("Content-Length", str(total))
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status != 200:
                            raise ProviderError(
                                f"上传 {path.name} 失败 HTTP {resp.status}",
                                retryable=resp.status >= 500, provider="dashscope")
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                    raise ProviderError(
                        f"上传 {path.name} 失败 HTTP {exc.code}: {detail}",
                        retryable=exc.code in _RETRYABLE_HTTP,
                        provider="dashscope") from None
                except urllib.error.URLError as exc:
                    raise ProviderError(f"上传 {path.name} 连不上: {exc.reason}",
                                        retryable=True, provider="dashscope") from None
        finally:
            body.unlink(missing_ok=True)

        return f"oss://{key}"


def _parse_error(raw: str) -> Tuple[str, str]:
    """从百炼的错误体里挖出 (code, message)。挖不出来就给空串。"""
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return "", raw[:200]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or ""), str(err.get("message") or "")[:300]
    if isinstance(data, dict) and data.get("code"):
        return str(data["code"]), str(data.get("message") or "")[:300]
    return "", raw[:200]


def _data_url(path: Path) -> str:
    """本地图片 → data URL。

    定妆照 / 首帧在本地，百炼的 img_url / base_image_url 都能吃 data URL，
    所以不需要先把图上传到某个公网位置 —— 少一个会过期的中间环节。
    实测 720x1280 的 PNG base64 之后约 1MB，在请求体限制之内。
    """
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    import base64

    blob = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{blob}"


# —————————————————————————————————————————————————————————— 公共基类


class _DashScope:
    """六种形态共用的部分：key 检查、客户端、单价、退避轮询。"""

    name = "dashscope"
    # 每个子类覆盖
    kind = ""
    default_model = ""
    model_env = ""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 60.0, **_: Any):
        key = (api_key or os.environ.get("AI_DRAMA_DASHSCOPE_API_KEY", "")).strip()
        if not key:
            raise ProviderError(
                "缺少 AI_DRAMA_DASHSCOPE_API_KEY；不想调用真厂商就把这个形态"
                "换回 dryrun（或是删掉对应环境变量，默认就是 dryrun）",
                retryable=False,
                provider=self.name,
            )
        self.base_url = (base_url or os.environ.get("AI_DRAMA_DASHSCOPE_BASE_URL")
                         or DEFAULT_BASE_URL)
        self.client = _Client(key, self.base_url, timeout)
        self.model = (model or (os.environ.get(self.model_env) if self.model_env else "")
                      or self.default_model)
        self.usd_rate = self._rate()
        # 单价的读取与校验在各子类的 __init__ 里（_price 会在写错时报错）

    def _rate(self) -> float:
        """人民币 → 美元的折算汇率。

        百炼的价目表是元，而这个项目的钱**一律是美元**：
        `models.py:288` 写着 `money: float  # 折算成美元`，CLI 的 `--budget` 也是美元。
        所以单价在这儿按元定义（方便照着官方价目表抄、也方便对账），
        出门前折一次。汇率会浮动，所以做成可覆盖的 —— 改汇率不用改代码：

            AI_DRAMA_DASHSCOPE_USD_RATE=7.3
        """
        raw = os.environ.get("AI_DRAMA_DASHSCOPE_USD_RATE")
        try:
            rate = float(raw) if raw else DEFAULT_USD_RATE
        except ValueError:
            raise ProviderError(
                f"AI_DRAMA_DASHSCOPE_USD_RATE={raw!r} 不是数字",
                retryable=False, provider=self.name) from None
        if rate <= 0:
            raise ProviderError(f"汇率得是正数，拿到 {rate!r}",
                                retryable=False, provider=self.name)
        return rate

    def _price(self, key: str, default: str) -> float:
        """单价：**元 → 美元**。

        `AI_DRAMA_DASHSCOPE_PRICE_<KEY>` 按**元**写（照官方价目表抄即可）。
        """
        raw = os.environ.get(f"AI_DRAMA_DASHSCOPE_PRICE_{key}")
        try:
            cny = float(raw) if raw else float(default)
        except ValueError:
            raise ProviderError(f"AI_DRAMA_DASHSCOPE_PRICE_{key}={raw!r} 不是数字",
                                retryable=False, provider=self.name) from None
        return cny / self.usd_rate

    def _price_usd(self, key: str, default: str) -> float:
        """单价：**官方报价本来就是美元**的那些，不再折算。

        价目表两种都有 —— 大多是元（走 `_price`），口型同步（国际站）标的
        是 $0.011469/秒。把美元价也塞进"元÷汇率"那台机器，汇率先行就把
        它的含义悄悄改掉了，所以单开一个入口，环境变量也分开叫：

            AI_DRAMA_DASHSCOPE_PRICE_USD_LIPSYNC=0.011469
        """
        raw = os.environ.get(f"AI_DRAMA_DASHSCOPE_PRICE_USD_{key}")
        try:
            return float(raw) if raw else float(default)
        except ValueError:
            raise ProviderError(f"AI_DRAMA_DASHSCOPE_PRICE_USD_{key}={raw!r} 不是数字",
                                retryable=False, provider=self.name) from None

    def health(self) -> Dict[str, Any]:
        """doctor 会调它。**不发网络请求** —— doctor 不该为了探活而花钱。"""
        return {"name": self.name, "kind": self.kind, "ok": True,
                "model": self.model, "base_url": self.base_url}

    def _media(self):
        """延迟拿到 core.media。

        放在模块头会成环：core 顶层会拉起 providers，而 providers 又要 core。
        也正因为这个，调用方要**容忍拿不到** —— 导入失败时最多是裁不了段 /
        写不了占位音，不该让一次已经付过钱的生成整个失败。
        """
        try:
            from ..core import media
            return media
        except Exception:  # noqa: BLE001
            return None

    def _probe(self, path: Path) -> Optional[float]:
        """量一段媒体有多长。量不出来返回 None（调用方各自决定怎么办）。"""
        media = self._media()
        return media.try_probe_duration(path) if media else None


# —————————————————————————————————————————————————————————— 1. 文本


@register("dashscope", "text")
class DashScopeText(_DashScope, TextProvider):
    """写剧本。走 OpenAI 兼容模式，所以换模型只改一个字符串。

    默认 qwen-plus：实测可用，且比 qwen-max 便宜一个数量级。
    （同日实测：qwen-turbo 返回 429 limit_requests、qwen-max 返回
    403 AllocationQuota.FreeTierOnly，都是账号侧的限制，不是代码问题。）
    """

    kind = "text"
    default_model = "qwen-plus"
    model_env = "AI_DRAMA_DASHSCOPE_TEXT_MODEL"

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        # 下面这些字面量都按**元**写（照百炼价目表抄），_price 出门时折成美元
        self.price_in = self._price("TEXT_IN", "0.0008")     # 元 / 1k 输入 token
        self.price_out = self._price("TEXT_OUT", "0.002")    # 元 / 1k 输出 token

    def _token_usage(self, data: Dict[str, Any], stage: str) -> Usage:
        u = data.get("usage") or {}
        p_in = int(u.get("prompt_tokens") or 0)
        p_out = int(u.get("completion_tokens") or 0)
        money = p_in / 1000.0 * self.price_in + p_out / 1000.0 * self.price_out
        return {"provider": self.name, "stage": stage, "unit": "token",
                "amount": float(p_in + p_out), "money": round(money, 6),
                "tokens_in": p_in, "tokens_out": p_out, "model": self.model}

    def _say(self, messages: List[Dict[str, Any]], *, max_tokens: int,
             stage: str) -> Tuple[str, Usage]:
        data = self.client.post(CHAT_PATH, {
            "model": self.model, "messages": messages, "max_tokens": max_tokens,
        }, label=f"text/{self.model}")
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"文本返回里没有 choices: {str(data)[:200]}",
                                retryable=True, provider=self.name)
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise ProviderError("文本返回是空的", retryable=True, provider=self.name)
        return text, self._token_usage(data, stage)

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 4000,
                 scene: str = "", scene_count: int = 3,
                 shots_per_scene: int = 4) -> Tuple[str, Usage]:
        """写剧本 / 拆分镜。

        `scene_count` / `shots_per_scene` 是**收下但不用**的：
        它们是给确定性实现（dryrun）用的旋钮，真模型这边格数已经写进
        `prompt` 里了（stage01_script.py 拼指令时带上了）。

        为什么仍然必须收 —— 契约在 `dryrun.generate()` 上，
        `stage01_script.py:138` 一定会传这两个关键字；不接就是 TypeError。
        这正是"骨架自己没对齐 ABC"的那类坑：base.py 的抽象签名里
        没有它们，照着 ABC 写就会在第一次真跑时炸。
        """
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._say(messages, max_tokens=max_tokens, stage="script")


# —————————————————————————————————————————————————————————— 2. 视觉


@register("dashscope", "vision")
class DashScopeVision(_DashScope, VisionProvider):
    """质检时"看一眼"。消息进、文本出，判定逻辑一律留在 core/identity.py。

    默认 qwen3-vl-plus —— 同日实测：qwen-vl-plus / qwen-vl-max 报 404
    model_not_found，qwen-vl-max-latest / qwen2.5-vl-72b-instruct 报
    403 access_denied，只有 qwen3-vl-plus 真的答上了。
    所以模型名**必须**可配：百炼的模型上下架很勤，写死 = 过两个月就坏。

    消息体直接用 OpenAI 风格（core/identity.check_messages 生成的就是这个），
    content 里的 {"type":"image_url","image_url":{"url":...}} 原样转发 ——
    本地文件早在那边就转成 data URL 了，这里不重复造轮子。
    """

    kind = "vision"
    default_model = "qwen3-vl-plus"
    model_env = "AI_DRAMA_DASHSCOPE_VISION_MODEL"

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        # 视觉模型按 token 计费，百炼的单价页按输入/输出分开列。
        # 默认值借用 qwen-plus 的档位 —— 这是个**保守近似**，
        # 拿到正式单价后用 AI_DRAMA_DASHSCOPE_PRICE_VISION_IN/OUT 覆盖掉。
        self.price_in = self._price("VISION_IN", "0.0008")
        self.price_out = self._price("VISION_OUT", "0.002")

    def chat(self, messages: List[Dict[str, Any]], *, scene: str = "") -> Tuple[str, Usage]:
        data = self.client.post(CHAT_PATH, {
            "model": self.model, "messages": messages, "max_tokens": 1024,
        }, label=f"vision/{self.model}")
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"视觉返回里没有 choices: {str(data)[:200]}",
                                retryable=True, provider=self.name)
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        u = data.get("usage") or {}
        p_in = int(u.get("prompt_tokens") or 0)
        p_out = int(u.get("completion_tokens") or 0)
        money = p_in / 1000.0 * self.price_in + p_out / 1000.0 * self.price_out
        usage: Usage = {"provider": self.name, "stage": "vision", "unit": "token",
                        "amount": float(p_in + p_out), "money": round(money, 6),
                        "tokens_in": p_in, "tokens_out": p_out, "model": self.model}
        # 空文本不当异常抛：core/identity 那边把"答不上来"记成 unchecked，
        # 这个三态语义比在这里硬失败更准（看不见 != 没问题）。
        return text, usage


# —————————————————————————————————————————————————————————— 3+4. 出图


#: 文生图只认这三档尺寸（传 720*1279 这种会直接 InvalidParameter）。
#: 所以这里做一次"贴合"：按长宽比挑最近的一档，不按像素数硬套。
_T2I_SIZES: Tuple[Tuple[int, int], ...] = ((720, 1280), (1280, 720), (1024, 1024))


def _snap_size(size: Tuple[int, int]) -> Tuple[int, int]:
    want = (float(size[0]) / max(1.0, float(size[1])))
    return min(_T2I_SIZES, key=lambda s: abs(s[0] / s[1] - want))


class _ImageMaker(_DashScope):
    """出图的公共部分：提交异步任务 → 轮询 → 下载。t2i 与 image 共用。"""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.price_png = self._price("PNG", "0.14")  # 元 / 张（折算后是美元）

    def _wait_image(self, task_id: str, *, timeout: float, label: str) -> str:
        """轮询到出图，返回图片地址。退避在这一层做，上层只调一次。"""
        deadline = time.monotonic() + timeout
        delay = 2.0
        while time.monotonic() < deadline:
            data = self.client.get(TASK_PATH.format(task_id=task_id),
                                   label=f"{label} 轮询")
            out = data.get("output") or {}
            status = str(out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                results = out.get("results") or []
                if not results or not results[0].get("url"):
                    raise ProviderError(f"出图成功但没有 url: {str(out)[:200]}",
                                        retryable=False, provider=self.name)
                # 百炼会顺手改写提示词（actual_prompt），记下来便于回看
                return results[0]["url"]
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                code = str(out.get("code") or "")
                msg = str(out.get("message") or status)
                raise ProviderError(f"出图任务失败 [{code}] {msg}",
                                    retryable=code in _RETRYABLE_CODES,
                                    provider=self.name)
            time.sleep(delay)
            delay = min(delay * 1.5, 8.0)
        raise ProviderError(f"出图轮询超过 {timeout:g}s 还没结果（task={task_id}）",
                            retryable=True, provider=self.name)

    def _png_usage(self, *, shot_label: str, note: str) -> Usage:
        usage: Usage = {"provider": self.name, "stage": "frames", "unit": "image",
                        "amount": 1.0, "money": round(self.price_png, 6),
                        "model": self.model, "note": note}
        if shot_label:
            usage["shot_label"] = shot_label
        return usage


@register("dashscope", "t2i")
class DashScopeT2I(_ImageMaker, T2IProvider):
    """定妆照：纯文生图，默认 wanx2.1-t2i-turbo（0.14 元/张）。"""

    kind = "t2i"
    default_model = "wanx2.1-t2i-turbo"
    model_env = "AI_DRAMA_DASHSCOPE_T2I_MODEL"

    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            shot_label: str = "") -> Usage:
        w, h = _snap_size(size)
        payload = {
            "model": self.model,
            "input": {"prompt": prompt,
                      "negative_prompt": "文字, 字幕, 水印, 多只手, 多只眼, 面部崩坏"},
            "parameters": {"n": 1, "size": f"{w}*{h}", "prompt_extend": True},
        }
        label = shot_label or "定妆照"
        data = self.client.post(T2I_PATH, payload, async_=True, label=f"t2i/{self.model} {label}")
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise ProviderError(f"提交出图没拿到 task_id: {str(data)[:200]}",
                                retryable=True, provider=self.name)
        url = self._wait_image(str(task_id), timeout=300.0, label=label)
        self.client.download(url, path)
        return self._png_usage(shot_label=shot_label,
                               note=f"[{self.model}] {w}x{h} 文生图")


@register("dashscope", "image")
class DashScopeImage(_ImageMaker, ImageProvider):
    """关键帧。

    有定妆照（ref_image）时走 `wanx2.1-imageedit` 的 description_edit：
    把定妆照当底图、用一句"改成什么"的指令改场景 —— 这是跨镜头保住同一张脸
    最省事也最稳的一招（纯文生图每镜都会重新长一张脸）。
    没有定妆照时才退回纯文生图。

    **实测出来的代价，写清楚免得下一个人以为是自己写错了：**
    图生图只有**一张**输入图（base_image_url），而它同时承担了
    "这个人的脸"和"这个人的构图"两件事 —— 于是 description_edit 会把画面
    往定妆照的景别上拉。实跑一集：分镜写了 medium / close 两个景别，
    两张关键帧出来几乎一样（都是定妆照那张半身站姿），特写没特起来。

    所以给了一个开关：

        AI_DRAMA_DASHSCOPE_IMAGE_USE_PORTRAIT=0

    关掉之后这一路退回纯文生图 —— 景别听分镜的，代价是每一镜的脸都会重新长，
    跨镜一致性交给"提示词描述得像"（定妆照那套 reference_block 依然会写进
    prompt，只是没有图给它看）。**一致性和景别在这个接口上只能选一个**，
    默认选一致性，因为脸漂了观众一眼就看出来，景别不准只是"不够讲究"。
    """

    kind = "image"
    default_model = "wanx2.1-t2i-turbo"
    model_env = "AI_DRAMA_DASHSCOPE_IMAGE_MODEL"

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.edit_model = (os.environ.get("AI_DRAMA_DASHSCOPE_IMAGEEDIT_MODEL")
                           or "wanx2.1-imageedit")
        self.use_portrait = _flag("AI_DRAMA_DASHSCOPE_IMAGE_USE_PORTRAIT", True)

    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            ref_image: Optional[Path] = None, shot_label: str = "",
            scene: str = "") -> Usage:
        label = shot_label or "关键帧"
        negative = "文字, 字幕, 水印, 多只手, 多只眼, 面部崩坏, 闪烁, 形变"

        if self.use_portrait and ref_image and Path(ref_image).exists():
            # 图生图：定妆照当底图。尺寸跟着底图走，所以不传 size。
            payload = {
                "model": self.edit_model,
                "input": {"function": "description_edit", "prompt": prompt,
                          "base_image_url": _data_url(Path(ref_image)),
                          "negative_prompt": negative},
                "parameters": {"n": 1},
            }
            path_used = IMAGE_EDIT_PATH
            note = f"[{self.edit_model}] 以定妆照为底图改写"
        else:
            w, h = _snap_size(size)
            payload = {
                "model": self.model,
                "input": {"prompt": prompt, "negative_prompt": negative},
                "parameters": {"n": 1, "size": f"{w}*{h}", "prompt_extend": True},
            }
            path_used = T2I_PATH
            if ref_image and Path(ref_image).exists():
                # 有定妆照却没用它：是那个开关关掉了。别写成"没有可参考的"。
                note = (f"[{self.model}] 纯文生图（景别优先："
                        "AI_DRAMA_DASHSCOPE_IMAGE_USE_PORTRAIT=0，没拿定妆照当底图）")
            else:
                note = f"[{self.model}] 纯文生图（这一镜没有定妆照可参考）"

        data = self.client.post(path_used, payload, async_=True,
                                label=f"image {label}")
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise ProviderError(f"提交出图没拿到 task_id: {str(data)[:200]}",
                                retryable=True, provider=self.name)
        url = self._wait_image(str(task_id), timeout=300.0, label=label)
        self.client.download(url, path)
        return self._png_usage(shot_label=shot_label, note=note)


# —————————————————————————————————————————————————————————— 5. 视频


#: 哪些模型认 parameters.duration。不认的（wanx2.1-i2v-turbo / wan2.2-i2v-plus）
#: 传了会直接 InvalidParameter「duration customization is not supported」，
#: 而且是**提交即失败**——所以这个表不是优化，是必需项。
_ACCEPTS_DURATION = ("wan2.5-", "wan2.6-", "wan2.7-")


def _accepts_duration(model: str) -> bool:
    return any(model.startswith(p) for p in _ACCEPTS_DURATION)


#: 认得出"关掉"的写法，跟 registry 判 vision/sfx 是否显式关闭用的是同一组词。
_OFF_VALUES = frozenset({"", "0", "off", "none", "false", "no", " "})


def _flag(env: str, default: bool) -> bool:
    """读一个布尔开关。没设就用 default，设了就按词判。"""
    raw = os.environ.get(env)
    if raw is None:
        return default
    return raw.strip().lower() not in _OFF_VALUES


@register("dashscope", "video")
class DashScopeVideo(_DashScope, VideoProvider):
    """图生视频。默认 wanx2.1-i2v-turbo（480P 0.24 元/秒）。

    **caps 是这个类里最要紧的一行。** wanx2.1-i2v-turbo 一次只出 5 秒，
    所以 max_duration=5：stage04 会据此把 12 秒的长镜拆成 3 段来生成，
    而不是悄悄给你前 5 秒（那样后半截的动作会凭空消失，成片时长却还写着 12 秒）。
    min_duration 给 3 秒是为了避免拆出 0.4 秒的碎片 —— 那种东西在成片里就是闪光。

    它**不接受** duration 参数（实测报 duration customization is not supported），
    所以每次都出 5 秒。fetch 时按请求时长裁一刀补齐这个差 —— 不裁的话，
    3 秒的镜头会变成 5 秒，整条时间轴跟着漂。
    想原生按时长出片就把模型换成 wan2.5-i2v-preview：
        $env:AI_DRAMA_DASHSCOPE_VIDEO_MODEL = "wan2.5-i2v-preview"
    """

    kind = "video"
    default_model = "wanx2.1-i2v-turbo"
    model_env = "AI_DRAMA_DASHSCOPE_VIDEO_MODEL"

    caps = ProviderCaps(
        width=720, height=1280, fps=24,
        min_duration=3.0, max_duration=5.0,
        aspect_ratios=("9:16", "16:9", "1:1"),
        supports_image_input=True,
        # 百炼的负向词是走 input.negative_prompt 的，i2v 那条线实测不吃，
        # 声明成 False 让上游别把这个字段当成一定会生效的东西。
        supports_negative_prompt=False,
        max_ref_images=1,
        notes="wanx2.1-i2v-turbo：单段固定 5 秒、不接受 duration，拿到后会按请求时长裁切",
    )

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.price_second = self._price("I2V_SEC", "0.24")   # 元 / 秒（折算后是美元）
        self.resolution = os.environ.get("AI_DRAMA_DASHSCOPE_VIDEO_RESOLUTION", "480P")
        self._want: Dict[str, float] = {}     # job -> 请求时长（裁切用）
        self._dest: Dict[str, Path] = {}      # job -> 落盘位置（poll 里写文件用）
        self._url: Dict[str, str] = {}        # job -> 产物地址

    # —— 生命周期

    def submit(self, request: Dict[str, Any]) -> str:
        duration = float(request.get("duration") or 0.0)
        if self.caps.max_duration and duration > self.caps.max_duration + 1e-6:
            # 按 base.py 的约定：超上限就报错，**不自己截断**。
            # 正常情况下 stage04 已经拆过段，走到这里说明上游漏了。
            raise ProviderError(
                f"要 {duration:.1f}s，超过 {self.model} 单段上限 "
                f"{self.caps.max_duration:g}s（上游应当先拆段）",
                retryable=False, provider=self.name)

        image_path = request.get("image_path")
        prompt = str(request.get("prompt") or "")
        if not image_path:
            raise ProviderError(
                f"{self.model} 是图生视频，必须给首帧（image_path）；"
                "想要纯文生视频请把模型换成 wan2.5-t2v-preview",
                retryable=False, provider=self.name)

        params: Dict[str, Any] = {"resolution": self.resolution}
        if _accepts_duration(self.model) and duration > 0:
            params["duration"] = int(round(duration))
        payload = {
            "model": self.model,
            "input": {"prompt": prompt, "img_url": _data_url(Path(image_path))},
            "parameters": params,
        }
        seed = request.get("seed")
        if seed is not None:
            params["seed"] = int(seed)

        label = str(request.get("shot_label") or "video")
        data = self.client.post(VIDEO_PATH, payload, async_=True,
                                label=f"video/{self.model} {label}")
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise ProviderError(f"提交视频没拿到 task_id: {str(data)[:200]}",
                                retryable=True, provider=self.name)
        job = str(task_id)
        self._want[job] = duration
        dest = request.get("dest")
        if dest:
            self._dest[job] = Path(str(dest))
        self._url.pop(job, None)
        return job

    def poll(self, job_id: str, *, timeout: float = 300.0) -> Tuple[str, Usage, Optional[str]]:
        """轮询到出片，**把文件落到 dest**，返回 (状态, 账, 本地路径)。

        第三项必须是**本地已存在的文件路径**，不是厂商的 URL ——
        stage04_video 拿到它就直接 `Path(info).exists()` 然后搬进正式位置，
        它不会替你下载。这一点 dryrun 的实现也是同样的约定。
        """
        deadline = time.monotonic() + timeout
        delay = 5.0
        while time.monotonic() < deadline:
            data = self.client.get(TASK_PATH.format(task_id=job_id), label="video 轮询")
            out = data.get("output") or {}
            status = str(out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                url = str(out.get("video_url") or out.get("url") or "")
                if not url:
                    raise ProviderError(f"视频成功但没有地址: {str(out)[:200]}",
                                        retryable=False, provider=self.name)
                self._url[job_id] = url
                return self._collect(job_id, url)
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                code = str(out.get("code") or "")
                msg = str(out.get("message") or status)
                # 内容违规 / 参数非法重试多少次都一样，别烧钱
                raise ProviderError(f"视频任务失败 [{code}] {msg}",
                                    retryable=code in _RETRYABLE_CODES,
                                    provider=self.name)
            time.sleep(delay)
            delay = min(delay * 1.5, 15.0)
        raise ProviderError(f"视频轮询超过 {timeout:g}s 还没结果（task={job_id}）",
                            retryable=True, provider=self.name)

    def _collect(self, job_id: str, url: str) -> Tuple[str, Usage, Optional[str]]:
        """下载产物、按需裁切、如实计费。"""
        dest = self._dest.get(job_id)
        want = float(self._want.get(job_id) or 0.0)
        notes = [f"[{self.model}] {self.resolution}"]
        if dest is None:
            # 调用方没给 dest（少见）。把地址交回去，由 fetch() 落盘 —— 但账先记上。
            return "succeeded", self._video_usage(want, notes + ["调用方没给 dest，产物未落盘"]), url

        self.client.download(url, dest)
        produced = self._probe(dest) or want
        if self._trim_to(dest, want, produced):
            notes.append(f"模型出 {produced:.2f}s，已按请求裁到 {want:.2f}s")
        # 计费按模型**实际产出**的秒数，不是按请求的秒数：
        # wanx2.1-i2v-turbo 固定出 5 秒，账单上就是 5 秒 ——
        # 按请求的 3 秒记会少报四成，成本表一旦失真，预算闸门也就跟着失准。
        return "succeeded", self._video_usage(produced, notes), str(dest)

    def _video_usage(self, seconds: float, notes: List[str]) -> Usage:
        return {"provider": self.name, "stage": "video", "unit": "second",
                "amount": round(seconds, 3),
                "money": round(seconds * self.price_second, 6),
                "model": self.model, "resolution": self.resolution,
                "note": "；".join(notes)}

    def fetch(self, job_id: str, dest: Path) -> Usage:
        """显式落盘。stage04 走的是 poll 那条路，这个留给别的调用方。"""
        if dest.exists() and Path(dest) == self._dest.get(job_id):
            return {"provider": self.name, "stage": "video", "unit": "file",
                    "amount": 1.0, "money": 0.0, "note": "产物已在位，未重复下载"}
        url = self._url.get(job_id)
        if not url:
            raise ProviderError("还没有产物地址：先 poll 到 succeeded 再 fetch",
                                retryable=False, provider=self.name)
        self.client.download(url, dest)
        want = float(self._want.get(job_id) or 0.0)
        produced = self._probe(dest) or want
        note = f"[{self.model}] {self.resolution}"
        if self._trim_to(dest, want, produced):
            note += f"；模型出 {produced:.2f}s，已按请求裁到 {want:.2f}s"
        return {"provider": self.name, "stage": "video", "unit": "file",
                "amount": 1.0, "money": 0.0, "note": note}

    def cancel(self, job_id: str) -> None:
        """百炼没有取消接口。best-effort 接口，安静返回。"""
        return None

    # —— 裁切

    def _trim_to(self, path: Path, want: float, produced: Optional[float] = None) -> bool:
        """产物比请求时长长就裁掉尾巴。返回是否真的裁了。

        为什么必须裁：stage07 按 ffprobe 量出的**实际**时长累加时间轴。
        这里多出 2 秒，后面每个镜头的声画都会往后错 2 秒。
        """
        if want <= 0.05:
            return False
        media = self._media()
        if media is None:
            return False
        got = produced if produced is not None else media.try_probe_duration(path)
        if not got or got <= want + 0.05:
            return False
        ffmpeg = media.find_ffmpeg()
        if not ffmpeg:
            return False

        tmp = path.with_name(path.stem + ".cut" + path.suffix)
        # 重编码而不是 -c copy：copy 只能切在关键帧上，5 秒的片段里
        # 往往只有开头一个关键帧，切出来还是 5 秒 —— 等于没裁。
        try:
            media.run([ffmpeg, "-y", "-v", "error", "-i", str(path),
                       "-t", f"{want:.3f}", "-an",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                       "-pix_fmt", "yuv420p", str(tmp)])
        except Exception:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            return False
        if not tmp.exists() or not tmp.stat().st_size:
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(path)
        return True


# —————————————————————————————————————————————————————————— 6. 配音


#: 项目里的通用音色名 → 百炼的音色名。
#: 认不出来的一律原样透传，所以项目圣经里可以直接写 "Cherry"。
_VOICES = {
    "female_01": "Cherry", "female_02": "Serena", "female_03": "Chelsie",
    "male_01": "Ethan", "male_02": "Dylan", "male_03": "Peter",
    "narrator": "Cherry",
}


@register("dashscope", "audio")
class DashScopeAudio(_DashScope, AudioProvider):
    """台词配音。默认 qwen3-tts-flash（0.8 元/万字符）。

    **接口形状踩过坑，写清楚免得下一个人再踩：**

    官方的 OpenAI 兼容写法（`/compatible-mode/v1/chat/completions` +
    `modalities:["audio"]` + `audio.voice`）实测三种放法全部报
    「The voice property is required」或「invalid text, invalid audio was returned」，
    开 stream 也只有 1 个空 chunk。真正能用的是**原生端点**：

        POST /api/v1/services/aigc/multimodal-generation/generation
        {"model": "qwen3-tts-flash",
         "input": {"text": "...", "voice": "Cherry"}}

    它回的是 `output.audio.url`（一个临时 wav 直链）+ `usage.characters`
    （按字符计费的真实字数）—— 不是内联 base64。所以这里要下载一次。
    同端点换 `qwen-tts` 报 404 Model not exist，`cosyvoice-v2` 报 url error，
    只有 qwen3-tts-flash 是通的。

    `music()` 是另一回事：百炼**没有**配乐生成接口。这里不假装生成，
    而是本地合成一段占位音轨并在 note 里写明 —— 想听真 BGM 的话，
    把 AI_DRAMA_AUDIO 指到别的厂商。
    """

    kind = "audio"
    default_model = "qwen3-tts-flash"
    model_env = "AI_DRAMA_DASHSCOPE_TTS_MODEL"

    TTS_PATH = "/api/v1/services/aigc/multimodal-generation/generation"

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.price_char = self._price("TTS_CHAR", "0.00008")  # 元 / 字（折算后是美元）

    def tts(self, text: str, path: Path, *, voice: str = "female_01",
            speed: float = 1.0, shot_label: str = "") -> Usage:
        spoken = (text or "").strip()
        if not spoken:
            raise ProviderError("台词是空的，不该调用 TTS", retryable=False,
                                provider=self.name)
        name = _VOICES.get(voice, voice)
        data = self.client.post(self.TTS_PATH, {
            "model": self.model,
            "input": {"text": spoken, "voice": name},
        }, timeout=180.0, label=f"tts/{self.model}")
        out = data.get("output") or {}
        audio = out.get("audio") or {}
        url = str(audio.get("url") or "")
        if not url:
            raise ProviderError(f"TTS 没返回音频地址: {str(data)[:200]}",
                                retryable=True, provider=self.name)
        self.client.download(url, path)

        # 按**接口自己报的**字数计费，而不是 local len(text)：
        # 它会把标点、英文单词按自己的规则折算，用本地字数会跟账单对不上。
        chars = int((data.get("usage") or {}).get("characters") or len(spoken))
        usage: Usage = {"provider": self.name, "stage": "audio", "unit": "char",
                        "amount": float(chars),
                        "money": round(chars * self.price_char, 6),
                        "model": self.model, "voice": name,
                        "note": f"[{self.model}] {voice} -> {name}"}
        if shot_label:
            usage["shot_label"] = shot_label
        return usage

    def music(self, path: Path, *, duration: float, mood: str = "") -> Usage:
        """百炼没有配乐接口 —— 这里写一段本地占位音轨，并**如实说明**。

        为什么不在 note 里含糊过去：BGM 占位和真 BGM 在成片里听起来
        差别很大（一段静音 vs 一段配乐），报告里不说清楚，用户会以为
        自己听到的是"模型觉得合适的配乐"。
        """
        media = self._media()
        if media is None:
            raise ProviderError("百炼没有配乐接口，且本地兜底不可用（core.media 导不进来）",
                                retryable=False, provider=self.name)
        seconds = max(1.0, float(duration or 0.0))
        media.write_wav(path, seconds=seconds, freq=110.0)
        return {"provider": self.name, "stage": "music", "unit": "second",
                "amount": round(seconds, 3), "money": 0.0,
                "note": "[dashscope] 百炼没有配乐生成接口 —— 这是本地合成的占位音轨，"
                        "不是真 BGM；换 AI_DRAMA_AUDIO 到别的厂商才有真配乐"}


# —————————————————————————————————————————————————————————— 7. 口型同步


#: VideoRetalk 的官方报价（国际站）：**$0.011469 / 秒**，按产出视频的秒数计。
#: 它是少数按美元标的接口，所以走 `_price_usd` 而不是 `_price`。
#: 国内站价目表按元就设 `AI_DRAMA_DASHSCOPE_PRICE_LIPSYNC`（元/秒）。
LIPSYNC_USD_PER_SECOND = "0.011469"

#: 文档写死的输入区间。越界**本地就拦** —— 发过去也是 400，
#: 白排一次队，还看不到真正的原因。
LIPSYNC_MIN_SECONDS = 2.0
LIPSYNC_MAX_SECONDS = 120.0

#: 画面的长边和短边都必须落在这个区间里。**短边这条最容易被撞上**：
#: 竖屏 9:16 的 480P 是 464x832，短边 464 就不够。
LIPSYNC_MIN_SIDE = 640
LIPSYNC_MAX_SIDE = 2048


@register("dashscope", "lipsync")
class DashScopeLipsync(_DashScope, LipSyncProvider):
    """百炼的口型同步：VideoRetalk。

    它和别的形态不一样的地方一句话：**输入输出都是已经渲好的片段**。
    它不生成画面，只把嘴按音轨重画一遍。

    两条契约（`base.LipSyncProvider` 的 docstring 写了为什么）：

      1. 出去的东西**不带音轨**。音轨由 concat 统一铺一次；这里再留一条，
         成片里每句台词都会说两遍。
      2. **时长不许变**。stage07 按 ffprobe 量出的实际时长累加时间轴，
         这里一改，后面所有镜头的声音都会错位。

    所以拿到产物后一定自己量一次，对不上就裁/补到原长，而不是相信接口
    报的数。`video_extension=False` 也是刻意的：让它出"和输入一样长"的
    片子，别自作主张把画面延长去将就音轨。

    还有一处和图片接口不同：**它不吃 data URL**。图片那边 base64 一塞就完，
    视频几 MB 起步，必须先传进百炼的临时存储换个 `oss://` 地址 ——
    这也是本文件里唯一用到 `_Client.upload` 的地方。
    """

    kind = "lipsync"
    default_model = "videoretalk"
    model_env = "AI_DRAMA_DASHSCOPE_LIPSYNC_MODEL"

    #: 轮询总预算。口型同步比出图慢得多：排队 + 逐帧重画，几分钟是常态。
    POLL_BUDGET = 900.0

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.price_second = self._price_usd("LIPSYNC", LIPSYNC_USD_PER_SECOND)

    # —— 对外

    def sync(self, video: Path, audio: Optional[Path], dest: Path, *,
             shot_label: str = "", scene: str = "") -> Usage:
        video = Path(video)
        dest = Path(dest)
        if not (video.exists() and video.stat().st_size > 0):
            raise ProviderError(f"要同步的片段不存在或是空的：{video}",
                                retryable=False, provider=self.name)

        has_audio = bool(audio and Path(audio).exists() and Path(audio).stat().st_size > 0)
        if not has_audio:
            # 没有音轨可对 —— 允许直接重编码过去，但**如实说明没同步过**。
            # 上游（stage06）只挑有台词的镜头调这里，走到这条支路通常是
            # 配音文件被挪走或没生成成功。
            self._write_silent(video, dest)
            seconds = self._probe(dest) or self._probe(video) or 0.0
            return self._usage(seconds, synced=False,
                               notes=["[dashscope] 这一镜没有配音文件，只做了去音轨重编码，"
                                      "**没有做口型同步**"])

        want = self._probe(video)
        if want is None:
            raise ProviderError(
                f"量不出 {video.name} 的时长，没有基准就没法保证同步后长度不变",
                retryable=False, provider=self.name)
        if not (LIPSYNC_MIN_SECONDS < want < LIPSYNC_MAX_SECONDS):
            raise ProviderError(
                f"{self.model} 只收 {LIPSYNC_MIN_SECONDS:g}s 到 {LIPSYNC_MAX_SECONDS:g}s 的片段，"
                f"这一镜是 {want:.2f}s —— 拆镜或换个实现（见 docs/adding-a-provider.md）",
                retryable=False, provider=self.name)

        media = self._media()
        if media is None:
            raise ProviderError("口型同步要用 ffmpeg 对齐音轨，但 core.media 导不进来",
                                retryable=False, provider=self.name)

        work = dest.with_name(dest.stem + ".sync")
        work.mkdir(parents=True, exist_ok=True)
        try:
            # 音轨先对齐到画面长度：`video_extension=False` 时产物取
            # min(画面, 音轨)，音轨短了产物就跟着短 —— 那是契约 2 的雷。
            # 先补/裁成一样长，产物自然就是画面长度。
            fitted = work / "audio.wav"
            self._fit_audio(media, Path(audio), fitted, want)

            # 接口要求长边和短边都在 640~2048。竖屏 480P 是 464x832，
            # 短边不够 —— 放大到够格再传，产物回来再缩回原尺寸
            # （尺度必须还原：stage07 要拿它和别的镜头拼在一起）。
            size = self._size(media, video)
            upload_video = video
            notes_pre: List[str] = []
            if size and min(size) < LIPSYNC_MIN_SIDE:
                upload_video = work / "upload.mp4"
                self._upscale(media, video, upload_video, size)
                notes_pre.append(
                    f"输入 {size[0]}x{size[1]} 短边不足 {LIPSYNC_MIN_SIDE}，"
                    f"已放大到上传要求的最小尺寸、产物再缩回原尺寸")

            video_url = self.client.upload(upload_video, model=self.model)
            audio_url = self.client.upload(fitted, model=self.model)

            data = self.client.post(LIPSYNC_PATH, {
                "model": self.model,
                "input": {"video_url": video_url, "audio_url": audio_url},
                # False：出和输入一样长的片子。True 会把画面拉长去就音轨，
                # 那就直接违反契约 2 了。
                "parameters": {"video_extension": False},
            }, async_=True, oss=True, timeout=180.0, label=f"lipsync/{self.model}")
            task_id = str((data.get("output") or {}).get("task_id") or "")
            if not task_id:
                raise ProviderError(f"口型同步没拿到 task_id: {str(data)[:200]}",
                                    retryable=True, provider=self.name)

            url = self._wait(task_id)
            raw = work / "raw.mp4"
            self.client.download(url, raw)

            note = self._normalize(media, raw, dest, want, size=size)
            got = self._probe(dest) or want
            notes = [f"[{self.model}]"] + notes_pre
            if note:
                notes.append(note)
            # 归一化没能生效（比如没装 ffmpeg）：宁可不交，也不交一个
            # 会让后面所有镜头错位的产物 —— stage06 会把它记成这一镜失败，
            # 单独重跑一次就好。
            if abs(got - want) > 0.12:
                raise ProviderError(
                    f"同步后时长是 {got:.2f}s，输入是 {want:.2f}s —— "
                    f"差得太多（>0.12s），收下就会挪动后面所有镜头的时间轴",
                    retryable=False, provider=self.name)
            return self._usage(got, synced=True, notes=notes, shot_label=shot_label)
        finally:
            for f in work.glob("*"):
                f.unlink(missing_ok=True)
            work.rmdir()

    # —— 轮询

    def _wait(self, task_id: str) -> str:
        deadline = time.monotonic() + self.POLL_BUDGET
        delay = 5.0
        while time.monotonic() < deadline:
            data = self.client.get(TASK_PATH.format(task_id=task_id), timeout=60.0,
                                   label="lipsync 轮询")
            out = data.get("output") or {}
            status = str(out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                url = str(out.get("video_url") or out.get("url") or "")
                if not url:
                    raise ProviderError(f"口型同步成功但没有产物地址: {str(out)[:200]}",
                                        retryable=False, provider=self.name)
                return url
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                code = str(out.get("code") or "")
                msg = str(out.get("message") or status)
                raise ProviderError(f"口型同步任务失败 [{code}] {msg}",
                                    retryable=code in _RETRYABLE_CODES,
                                    provider=self.name)
            time.sleep(delay)
            delay = min(delay * 1.5, 15.0)
        raise ProviderError(f"口型同步轮询超过 {self.POLL_BUDGET:g}s 还没结果（task={task_id}）",
                            retryable=True, provider=self.name)

    # —— 音轨与时长

    def _fit_audio(self, media, src: Path, dest: Path, want: float) -> None:
        """把配音补/裁到正好 want 秒，并统一成 wav。

        补静音用 `apad`、超长用 `-t`。同一刀切两件事：接口对音轨长度也有
        自己的限制，而产物长度取决于它，所以不能让它比画面短。
        """
        ffmpeg = media.find_ffmpeg()
        if not ffmpeg:
            raise ProviderError("口型同步需要 ffmpeg 对齐音轨，但这台机器上没有",
                                retryable=False, provider=self.name)
        media.run([ffmpeg, "-y", "-v", "error", "-i", str(src),
                   "-af", "apad", "-t", f"{want:.3f}",
                   "-ar", "16000", "-ac", "1", str(dest)], timeout=300)

    def _write_silent(self, video: Path, dest: Path) -> None:
        """没有配音时的退路：重编码、去音轨，长度原样。"""
        media = self._media()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if media is None:
            raise ProviderError("没有 ffmpeg 就没法把片段写出去（core.media 导不进来）",
                                retryable=False, provider=self.name)
        ffmpeg = media.find_ffmpeg()
        if not ffmpeg:
            raise ProviderError("没有 ffmpeg 就没法把片段写出去",
                                retryable=False, provider=self.name)
        media.run([ffmpeg, "-y", "-v", "error", "-i", str(video), "-an",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-pix_fmt", "yuv420p", str(dest)], timeout=600)

    def _normalize(self, media, src: Path, dest: Path, want: float,
                   size: Optional[Tuple[int, int]] = None) -> Optional[str]:
        """src → dest：掐掉音轨、把长度对到 want、尺寸还原。返回说明（没动就 None）。

        契约 1 和 2 都落实在这一个函数里，外加一件容易被忘的事：
        **把画幅缩回去**。上传前为了满足短边要求放大过，产物就是放大后的
        尺寸；不缩回来，stage07 拼接时这一镜和别的镜头分辨率不一样，
        concat 会直接炸（或者拼出一段花屏）。
        """
        ffmpeg = media.find_ffmpeg()
        if not ffmpeg:
            return None
        got = media.try_probe_duration(src)
        filters: List[str] = []
        notes: List[str] = []
        if size:
            filters.append(f"scale={size[0]}:{size[1]}:flags=lanczos")
        if got is None:
            notes.append("量不出产物时长，只能去音轨")
        elif got > want + 0.05:
            notes.append(f"接口出 {got:.2f}s，已裁回 {want:.2f}s")
        elif got < want - 0.05:
            pad = want - got
            filters.append(f"tpad=stop_mode=clone:stop_duration={pad:.3f}")
            notes.append(f"接口出 {got:.2f}s，已定格补 {pad:.2f}s 到 {want:.2f}s")

        args = [ffmpeg, "-y", "-v", "error", "-i", str(src)]
        if filters:
            args += ["-vf", ",".join(filters)]
        if got is not None and got > want + 0.05:
            args += ["-t", f"{want:.3f}"]
        args += ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", str(dest)]
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            media.run(args, timeout=600)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"整理同步产物失败（{type(exc).__name__}: {exc}）",
                                retryable=False, provider=self.name) from None
        if not dest.exists() or not dest.stat().st_size:
            raise ProviderError("整理同步产物之后文件是空的",
                                retryable=False, provider=self.name)
        return "；".join(notes) if notes else ""

    def _size(self, media, path: Path) -> Optional[Tuple[int, int]]:
        """量画面的像素尺寸。量不出来返回 None（那就按原样传，让接口去报错）。"""
        probe = getattr(media, "probe_size", None)
        if not probe:
            return None
        try:
            return probe(path)
        except Exception:  # noqa: BLE001
            return None

    def _upscale(self, media, src: Path, dest: Path, size: Tuple[int, int]) -> None:
        """把短边撑到 LIPSYNC_MIN_SIDE 以上，比例不变。

        这件事本身不增加任何细节 —— 只是为了过接口的尺寸门槛。
        所以产物必须是**同一段画面**、时长一致，回来还要缩回原尺寸。
        尺寸取偶数：yuv420p 的编码器不吃奇数。
        """
        scale = LIPSYNC_MIN_SIDE / min(size)
        w = int(round(size[0] * scale / 2)) * 2
        h = int(round(size[1] * scale / 2)) * 2
        ffmpeg = media.find_ffmpeg()
        if not ffmpeg:
            raise ProviderError("没有 ffmpeg 就没法把画面撑到接口要求的尺寸",
                                retryable=False, provider=self.name)
        media.run([ffmpeg, "-y", "-v", "error", "-i", str(src),
                   "-vf", f"scale={w}:{h}:flags=lanczos",
                   "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                   "-pix_fmt", "yuv420p", str(dest)], timeout=600)

    def _usage(self, seconds: float, *, synced: bool, notes: List[str],
               shot_label: str = "") -> Usage:
        """按**产出片段自己的秒数**计费。

        不用输入的秒数：接口报的 `usage.video_duration` 是它实际处理了多久，
        而它处理的正是我们交上去的那段画面 —— 两者本该一致，不一致时以
        量出来的为准，因为那才是账单的依据。

        `synced=False` 时（没配音、根本没调模型）**不记账** ——
        重编码是我们自己在本地做的，一分钱没花。记成花钱会让预算闸门
        留出一笔不存在的余量。
        """
        seconds = max(0.0, round(float(seconds), 3))
        money = round(seconds * self.price_second, 6) if synced else 0.0
        usage: Usage = {
            "provider": self.name, "stage": "lipsync", "unit": "second",
            "amount": seconds, "money": money, "model": self.model,
            "note": "；".join(notes + ([] if synced else ["本地重编码，未调用模型"])),
        }
        if shot_label:
            usage["shot_label"] = shot_label
        return usage
