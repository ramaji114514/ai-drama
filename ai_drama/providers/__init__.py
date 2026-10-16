"""Provider 层：抽象接口 + 注册表 + dryrun 实现。

换厂商的唯一入口是 registry.build_bundle()，见 docs/adding-a-provider.md。
"""

from . import base, registry  # noqa: F401
from . import dashscope  # noqa: F401  导入即注册（装饰器生效）
from .base import (  # noqa: F401
    AudioProvider,
    BudgetExceeded,
    Bundle,
    ImageProvider,
    ProviderError,
    T2IProvider,
    TextProvider,
    VideoProvider,
)
from .registry import available, build_bundle, create, register  # noqa: F401

__all__ = [
    "TextProvider", "ImageProvider", "VideoProvider", "AudioProvider", "T2IProvider",
    "Bundle", "ProviderError", "BudgetExceeded",
    "register", "create", "available", "build_bundle",
]
