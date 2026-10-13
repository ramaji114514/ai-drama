"""Provider 注册表 + 依赖注入点。

换厂商的**唯一入口**就在这里：

    # .env 或环境变量
    AI_DRAMA_TEXT=my_text_provider
    AI_DRAMA_VIDEO=my_video_provider

然后写一个 30 行的适配器继承 providers.base 里的 ABC，
用 @register("four", "text") 或 @register("my_video", "video") 注册即可。
stages/ 完全不需要改动 — 这是整个骨架存在的意义。

默认全部走 dryrun：不花钱、不需要任何 API key、能端到端出片。
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable, Dict, List

from .base import AudioProvider, Bundle, ImageProvider, T2IProvider, TextProvider, VideoProvider

_REGISTRY: Dict[str, Dict[str, Callable[..., Any]]] = {
    "text": {},
    "image": {},
    "video": {},
    "audio": {},
    "t2i": {},
}


def register(name: str, kind: str) -> Callable[[type], type]:
    """把一个 provider 类注册进指定种类。"""
    if kind not in _REGISTRY:
        raise ValueError(f"unknown provider kind: {kind} (expected one of {list(_REGISTRY)})")

    def deco(cls: type) -> type:
        _REGISTRY[kind][name] = cls
        return cls

    return deco


def available(kind: str | None = None) -> Dict[str, List[str]]:
    if kind:
        return {kind: sorted(_REGISTRY[kind])}
    return {k: sorted(v) for k, v in _REGISTRY.items()}


# 内置的 dryrun 实现在这里被显式 import，保证一定注册上。
# 外部适配器可以放到 providers/contrib/ 下，schema 见 docs/adding-a-provider.md
from . import dryrun  # noqa: E402,F401

# ``python -m providers.contrib.<name>`` 形式的外部适配器：用分号分隔。
_EXTRA = os.environ.get("AI_DRAMA_EXTRA_PROVIDERS", "").strip()
if _EXTRA:
    for mod in [m.strip() for m in _EXTRA.split(";") if m.strip()]:
        importlib.import_module(mod)


def create(kind: str, name: str, **kwargs: Any) -> Any:
    """按名字实例化一个 provider。"""
    if kind not in _REGISTRY:
        raise ValueError(f"unknown provider kind: {kind}")
    if name not in _REGISTRY[kind]:
        raise KeyError(
            f"provider '{name}' is not registered for kind '{kind}'. "
            f"available: {sorted(_REGISTRY[kind])}. "
            f"Register yours with @register('{name}', '{kind}')."
        )
    return _REGISTRY[kind][name](**kwargs)


def build_bundle(**overrides: Any) -> Bundle:
    """构造一次 run 需要的 provider 组合。

    优先级：函数参数 > 环境变量 > dryrun 默认值。
    """
    env = os.environ
    text = overrides.get("text") or create("text", env.get("AI_DRAMA_TEXT", "dryrun"))
    image = overrides.get("image") or create("image", env.get("AI_DRAMA_IMAGE", "dryrun"))
    video = overrides.get("video") or create("video", env.get("AI_DRAMA_VIDEO", "dryrun"))
    audio = overrides.get("audio") or create("audio", env.get("AI_DRAMA_AUDIO", "dryrun"))
    t2i_name = env.get("AI_DRAMA_T2I", "")
    t2i = overrides.get("t2i")
    if t2i is None and t2i_name:
        t2i = create("t2i", t2i_name)
    return Bundle(text=text, image=image, video=video, audio=audio, t2i=t2i)


__all__ = [
    "register", "available", "create", "build_bundle",
    "TextProvider", "ImageProvider", "VideoProvider", "AudioProvider", "T2IProvider",
]
