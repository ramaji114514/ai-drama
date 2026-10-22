"""DeepSeek —— 只做 `text` 这一种形态。

为什么单独一个文件、而不是在 dashscope 里加个分支：这一层的主人已经换了。
项目现在只产提示词，唯一会调模型的地方就是"把一句话写成剧本和分镜"，
那家厂商值不值得接，只看它写中文分镜写得怎么样 —— 跟它会不会画图、
会不会配音没有关系。把 DeepSeek 塞进 dashscope 的分支表里，等于把
"谁在写剧本"这个事实埋进一个叫"百炼"的文件里。

用法（其余形态仍然默认 dryrun，不受影响）::

    $env:AI_DRAMA_TEXT = "deepseek"
    $env:AI_DRAMA_DEEPSEEK_API_KEY = "sk-..."

    # 可选
    $env:AI_DRAMA_DEEPSEEK_MODEL    = "deepseek-reasoner"   # 默认 deepseek-chat
    $env:AI_DRAMA_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

协议是 OpenAI 兼容的那套（`POST /chat/completions`），所以这里没有
"百炼特有"的异步任务、轮询、OSS 上传那些东西 —— 一次请求一次返回，
写剧本本来就是短请求。

计费：**官方标价就是美元**，跟 `models.py:288` 的 `money` 同币种，
不需要百炼那套"元 ÷ 汇率"的折算。单价按 token 记，可覆盖::

    AI_DRAMA_DEEPSEEK_PRICE_IN     # 美元 / 1K 输入 token（未命中缓存）
    AI_DRAMA_DEEPSEEK_PRICE_CACHE  # 美元 / 1K 输入 token（命中缓存）
    AI_DRAMA_DEEPSEEK_PRICE_OUT    # 美元 / 1K 输出 token

命中缓存的那部分会单独算 —— 写剧本时 system 提示和角色圣经在整集里
反复出现，缓存命中率不低，全按未命中价算会把账记贵。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .base import ProviderError, TextProvider, Usage, call_with_retry
from .registry import register

#: 官方入口。注意 OpenAI 兼容接口挂在根上（`/chat/completions`），
#: 不是 `/v1/chat/completions` —— 两个都能用，但根路径是官方文档里写的那个。
DEFAULT_BASE_URL = "https://api.deepseek.com"
CHAT_PATH = "/chat/completions"

#: 这些 HTTP 状态码值得重试：限流和服务端抖动。其余的（400 参数、401 key、
#: 402 余额、403 权限）重试只是浪费时间，而且余额不足时重试还会刷屏。
_RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)


def _post_json(url: str, payload: Dict[str, Any], key: str,
               timeout: float) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise ProviderError(
            f"deepseek HTTP {exc.code}: {detail or exc.reason}",
            retryable=exc.code in _RETRYABLE_HTTP,
            provider="deepseek",
        ) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise ProviderError(
            f"deepseek 网络错误: {type(exc).__name__}: {exc}",
            retryable=True, provider="deepseek",
        ) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(f"deepseek 返回的不是 JSON: {exc}",
                            retryable=True, provider="deepseek") from None


@register("deepseek", "text")
class DeepSeekText(TextProvider):
    """写剧本 / 拆分镜的文本模型。"""

    name = "deepseek"
    kind = "text"
    default_model = "deepseek-chat"
    model_env = "AI_DRAMA_DEEPSEEK_MODEL"
    key_env = "AI_DRAMA_DEEPSEEK_API_KEY"
    base_env = "AI_DRAMA_DEEPSEEK_BASE_URL"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 120.0, **_: Any):
        key = (api_key or os.environ.get(self.key_env, "")).strip()
        if not key:
            # 缺 key 一律响亮报错，绝不悄悄退回假数据：假装成功比失败贵。
            raise ProviderError(
                f"缺少 {self.key_env}；不想调用真厂商就把 AI_DRAMA_TEXT 换回 "
                f"dryrun（或删掉这个环境变量，默认就是 dryrun）",
                retryable=False,
                provider=self.name,
            )
        self.api_key = key
        self.base_url = ((base_url or os.environ.get(self.base_env) or DEFAULT_BASE_URL)
                         .rstrip("/"))
        self.model = ((model or (os.environ.get(self.model_env) if self.model_env else "")
                       or self.default_model).strip())
        self.timeout = timeout
        self.price_in = self._price("IN", "0.00027")
        self.price_cache = self._price("CACHE", "0.00007")
        self.price_out = self._price("OUT", "0.0011")

    # —————————————————————————————————————————————— 单价

    def _price(self, key: str, default: str) -> float:
        """美元 / 1K token。写错要报错，别把 "0.0027" 手滑成 "0.027" 还安静生效。"""
        raw = os.environ.get(f"AI_DRAMA_DEEPSEEK_PRICE_{key}")
        try:
            value = float(raw) if raw else float(default)
        except ValueError:
            raise ProviderError(
                f"AI_DRAMA_DEEPSEEK_PRICE_{key}={raw!r} 不是数字",
                retryable=False, provider=self.name) from None
        if value < 0:
            raise ProviderError(
                f"AI_DRAMA_DEEPSEEK_PRICE_{key} 不能是负数（拿到 {value}）",
                retryable=False, provider=self.name)
        return value

    # —————————————————————————————————————————————— 契约

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 4000,
                 scene: str = "", scene_count: int = 3,
                 shots_per_scene: int = 4) -> Tuple[str, Usage]:
        """写剧本 / 拆分镜。

        `scene_count` / `shots_per_scene` **收下但不用**：格数已经写进
        `prompt` 里了（`stage01_script.py:131` 拼指令时带上）。仍然必须收 ——
        契约在 `dryrun.generate()` 上，调用方一定会传这两个关键字，
        不接就是 TypeError（base.py 的抽象签名里没有它们，照 ABC 写必炸）。
        """
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = call_with_retry(
            _post_json, f"{self.base_url}{CHAT_PATH}", payload, self.api_key,
            self.timeout, label=f"deepseek/{self.model}",
        )
        return self._read(data, scene=scene or "script")

    def _read(self, data: Dict[str, Any], *, scene: str) -> Tuple[str, Usage]:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(
                f"文本返回里没有 choices: {str(data)[:200]}",
                retryable=True, provider=self.name)
        msg = choices[0].get("message") or {}
        text = (msg.get("content") or "").strip()
        if not text:
            # reasoner 把思考过程放 reasoning_content、正文放 content；
            # 正文空而思考非空，说明 token 预算被思考吃完了 —— 这条线索
            # 比一句"返回空"有用得多，照着它去调 max_tokens。
            thinking = (msg.get("reasoning_content") or "").strip()
            if thinking:
                raise ProviderError(
                    f"{self.model} 只输出了思考过程、正文是空的"
                    f"（思考 {len(thinking)} 字）—— 把 max_tokens 调大",
                    retryable=False, provider=self.name)
            raise ProviderError("文本返回是空的", retryable=True, provider=self.name)
        usage: Usage = self._usage(data, text, scene=scene)
        return text, usage

    def _usage(self, data: Dict[str, Any], text: str, *, scene: str) -> Usage:
        u = data.get("usage") or {}
        p_in = int(u.get("prompt_tokens") or 0)
        p_out = int(u.get("completion_tokens") or 0)
        # 命中缓存的那部分算便宜价。老版本/别的兼容实现没有这两个字段，
        # 那时 hit=0，全部按未命中算 —— 宁可记贵，不要记便宜。
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        miss = int(u.get("prompt_cache_miss_tokens") or (p_in - hit if p_in else 0))
        hit = max(0, min(hit, p_in))
        miss = max(0, min(miss, p_in - hit))
        money = (hit / 1000.0 * self.price_cache
                 + miss / 1000.0 * self.price_in
                 + p_out / 1000.0 * self.price_out)
        return {
            "provider": self.name,
            "stage": scene,
            "unit": "token",
            "amount": float(p_in + p_out),
            "money": round(money, 6),
            "tokens_in": p_in,
            "tokens_out": p_out,
            "tokens_cached": hit,
            "model": self.model,
        }

    def health(self) -> Dict[str, Any]:
        """`doctor` 会调它。**不发网络请求** —— 探活不该花钱。"""
        return {"name": self.name, "kind": self.kind, "ok": True,
                "model": self.model, "base_url": self.base_url}
