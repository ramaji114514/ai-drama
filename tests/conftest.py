"""pytest 夹具。

原则：**每个测试都在 tmp_path 里跑**，绝不污染工作目录。
骨架自己就爱往 ./runs 里写东西，测试如果不隔离，
跑一次测试就会在仓库里留下一堆成片。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def has_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def need_ffmpeg() -> None:
    if not has_ffmpeg():
        pytest.skip("ffmpeg/ffprobe 不在 PATH 上（装法见 README）")
