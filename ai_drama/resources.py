"""数据目录（config/、skills/、agent/）在哪。

源码仓库里它们是仓库根目录下的普通文件夹。装成 wheel 之后就没有"仓库根"了，
所以这里给三级查找：

1. `AI_DRAMA_HOME` —— 显式指定，测试和高级用户用；
2. 包的两级父目录（源码仓库 / `pip install -e .`）—— 最常见；
3. `sysconfig` 的 data 目录下的 `share/ai-drama` —— `pip install` 装的是这里。

找不到就退回包目录本身：`config/models.yaml` 读不到时 catalog 用内置目录、
qc 用内置规则，不会崩 —— 只是少了"可覆盖"的那一层。
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path

_MARKER = "config"
_PACKAGE = Path(__file__).resolve().parent


def _looks_like_home(path: Path) -> bool:
    return (path / _MARKER).is_dir()


def data_root() -> Path:
    env = os.environ.get("AI_DRAMA_HOME", "").strip()
    if env:
        return Path(env).resolve()
    candidates = [_PACKAGE.parent, Path(sysconfig.get_path("data")) / "share" / "ai-drama"]
    for path in candidates:
        if _looks_like_home(path):
            return path
    return _PACKAGE


def skills_dir() -> Path:
    return data_root() / "skills"


def config_dir() -> Path:
    return data_root() / "config"
