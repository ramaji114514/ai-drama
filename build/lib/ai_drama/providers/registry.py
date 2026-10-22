"""Provider 注册表 + 依赖注入点。

换厂商的**唯一入口**就在这里：

    # .env 或环境变量
    AI_DRAMA_TEXT=my_text_provider
    AI_DRAMA_VIDEO=my_video_provider

然后写一个 30 行的适配器继承 providers.base 里的 ABC，
用 @register("four", "text") 或 @register("my_video", "video") 注册即可。
stages/ 完全不需要改动 — 这是整个骨架存在的意义。

默认全部走 dryrun：不花钱、不需要任何 API key、能端到端出片。

八种形态：text / image / video / audio / t2i（定妆照）/ lipsync（口型）/
vision（看一眼已经做出来的画面）/ sfx（音效）。后四种是可以显式关掉的，
其中两种**默认就是关的**：

  * `vision` 关掉意味着**没有任何人看过画面**，质检里那几项会报"没查成"
    而不是"通过"。
  * `sfx`   关掉意味着成片里只有台词和 BGM —— 音效是加分项，
    而它的用量是每镜 0-3 条，默认打开会把已经很贵的 run 变得更贵。
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable, Dict, List

from .base import (
    AudioProvider,
    Bundle,
    ImageProvider,
    LipSyncProvider,
    SfxProvider,
    T2IProvider,
    TextProvider,
    VideoProvider,
    VisionProvider,
)

_REGISTRY: Dict[str, Dict[str, Callable[..., Any]]] = {
    "text": {},
    "image": {},
    "video": {},
    "audio": {},
    "t2i": {},
    "lipsync": {},
    "vision": {},
    "sfx": {},
}

#: 每个形态由哪个环境变量决定。``doctor`` 与 ``build_bundle`` 共用这一份。
KIND_ENV: Dict[str, str] = {
    "text": "AI_DRAMA_TEXT",
    "image": "AI_DRAMA_IMAGE",
    "video": "AI_DRAMA_VIDEO",
    "audio": "AI_DRAMA_AUDIO",
    "t2i": "AI_DRAMA_T2I",
    "lipsync": "AI_DRAMA_LIPSYNC",
    "vision": "AI_DRAMA_VISION",
    "sfx": "AI_DRAMA_SFX",
}

#: 环境变量没设时用的 provider。
DEFAULT_PROVIDER = "dryrun"

#: 环境变量没设时**默认关闭**的形态。
#:
#: 这里放的都是"不做也不会做出错东西"的形态，反过来 `text/image/video/audio`
#: 默认 dryrun（零配置也要能端到端出片）。两条理由：
#:
#:   * ``vision``：一个"假装看过了"的默认检查器比没有检查器**更糟** ——
#:     它会把"没查成"洗成"通过了"，而 unchecked 与 pass 的区别正是质检
#:     那一层的全部意义。
#:   * ``sfx``：音效是**加分项**，而它的用量是每镜 0-3 条。默认打开的后果是
#:     每个已经跑得好好的项目突然多出几十次调用、成本表被搅乱、成片里塞满
#:     一段段占位噪声 —— 而用户从没要求过音效。要音效就显式打开。
DEFAULT_OFF_KINDS = ("vision", "sfx")

#: 写这些值表示"我明确不要这个形态"。
#: 只有 ``t2i`` / ``lipsync`` / ``vision`` 关掉是有意义的 —— 另外四个是必须的。
_OFF_VALUES = ("", "none", "off", "null", "0", "false", "no")

#: 允许被显式关掉的形态。这些都是可选增强：
#:   * ``t2i``     不配就是没有定妆照（参考锁定段会静默失效，所以报告里要写）
#:   * ``lipsync`` 不配就是成片嘴型没对齐
#:   * ``vision``  不配就是**画面没被任何人看过** —— 质检里那几项会报"没查成"
#:   * ``sfx``     不配就是没有音效（还在"台词 + BGM"那一档）
#: 它们在流水线里缺席时都会被如实报出来，而不是静默跳过。
OPTIONAL_KINDS = ("t2i", "lipsync", "vision", "sfx")


def kind_env(kind: str) -> str:
    """这个形态由哪个环境变量决定。"""
    if kind not in KIND_ENV:
        raise ValueError(f"unknown provider kind: {kind} (expected one of {list(KIND_ENV)})")
    return KIND_ENV[kind]


def default_name(kind: str) -> str:
    """这个形态没设环境变量时用什么；空串表示"默认就是关掉的"。"""
    return "" if kind in DEFAULT_OFF_KINDS else DEFAULT_PROVIDER


def effective_name(kind: str) -> str:
    """这个形态**最终**会用哪个 provider；返回空字符串表示显式关闭。

    **判定入口只有这一个。** 流水线与 `doctor` 都必须走它：两边各写一份默认值
    的结果就是 `doctor` 说"未配置，定妆照会跳过"而流水线其实在跑定妆照 ——
    报告和现实各说一套，正是这个项目要消灭的那类失败。
    """
    raw = os.environ.get(kind_env(kind))
    if raw is None:
        return default_name(kind)
    raw = raw.strip()
    return "" if raw.lower() in _OFF_VALUES else raw


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
    made: Dict[str, Any] = {}
    for kind in ("text", "image", "video", "audio", "t2i", "lipsync", "vision", "sfx"):
        override = overrides.get(kind)
        if override is not None:
            made[kind] = override
            continue
        name = effective_name(kind)
        if not name:
            # 显式关闭。t2i / lipsync 关掉是合法的（只跑分镜不要定妆照；
            # 或者先不做口型同步），另外四个关掉没有意义 —— 与其让
            # create(kind, "") 抛一个看不懂的 KeyError，不如直接说清楚。
            if kind in OPTIONAL_KINDS:
                made[kind] = None
                continue
            raise ValueError(
                f"{kind_env(kind)} 是空的，但 {kind} provider 是必须的。"
                f"删掉这个变量就会用默认值 {DEFAULT_PROVIDER}。"
            )
        made[kind] = create(kind, name)

    bundle = Bundle(
        text=made["text"], image=made["image"], video=made["video"],
        audio=made["audio"], t2i=made["t2i"], lipsync=made["lipsync"],
        vision=made["vision"], sfx=made["sfx"],
    )
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
    "kind_env", "effective_name", "default_name",
    "KIND_ENV", "DEFAULT_PROVIDER", "DEFAULT_OFF_KINDS", "OPTIONAL_KINDS",
    "TextProvider", "ImageProvider", "VideoProvider", "AudioProvider",
    "T2IProvider", "LipSyncProvider", "VisionProvider", "SfxProvider",
]
