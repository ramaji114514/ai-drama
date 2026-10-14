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

    还有一个隐式输入：**模型目录**（`config/models.yaml`）。适配器自己声明了
    `caps` 就以它为准；没声明就照目录里同名条目的能力上限补上。这样"这个
    模型一次能生成几秒"不会因为适配器作者忘了写而变成"不限制"，也就不会
    冒出被静默截断的成片。
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

    bundle = Bundle(text=text, image=image, video=video, audio=audio, t2i=t2i)
    catalog = overrides.get("catalog")
    if catalog is None:
        catalog = _load_catalog()
    _apply_catalog_caps(bundle, catalog)
    bundle.catalog = catalog
    return bundle


def _load_catalog():
    """按当前工作目录读模型目录。读不到就退回内置的（并在 bundle 上留痕）。"""
    from ..core.catalog import load_catalog
    from pathlib import Path

    try:
        return load_catalog(Path.cwd())
    except Exception as exc:                       # noqa: BLE001
        from ..core.catalog import ModelCatalog

        cat = ModelCatalog.builtin()
        cat.problem = f"{type(exc).__name__}: {exc}"
        return cat


def _apply_catalog_caps(bundle: Bundle, catalog: Any) -> None:
    """给"没自己声明上限"的视频 provider 补上目录里的上限。

    只对视频 provider 生效：单段时长是视频独有的约束，把一条视频模型的
    上限挂到文本 provider 上只会让 `providers` 命令的输出变得难以理解。
    """
    from .base import DEFAULT_CAPS

    for provider in bundle.all_providers():
        if not isinstance(provider, VideoProvider):
            continue
        current = getattr(provider, "caps", None)
        if current is not None and current is not DEFAULT_CAPS:
            continue          # 适配器自己说了算
        found = catalog.caps_for_provider(getattr(provider, "name", ""))
        if found is not None:
            try:
                provider.caps = found
            except AttributeError:          # pragma: no cover - 只读适配器
                continue


__all__ = [
    "register", "available", "create", "build_bundle",
    "TextProvider", "ImageProvider", "VideoProvider", "AudioProvider", "T2IProvider",
]
