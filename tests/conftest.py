"""pytest 夹具。

原则：**每个测试都在 tmp_path 里跑**，绝不污染工作目录。
骨架自己就爱往 ./runs 里写东西，测试如果不隔离，
跑一次测试就会在仓库里留下一堆成片。
"""

from __future__ import annotations

import os
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


@pytest.fixture(autouse=True)
def no_ambient_provider_config(monkeypatch):
    """把开发机上 `.env` 漏进来的配置清掉。

    这不是洁癖：`.env` 里写着 `AI_DRAMA_TEXT=deepseek`，而
    `cli.load_env()` 会把它塞进 `os.environ` —— 同一个进程里后续用例
    再问"默认用哪个 provider"，拿到的是**别人的机器配置**，"默认必须是
    dryrun"那条断言于是随机变红。测试要么全绿要么全红，不能看环境脸色。
    """
    for key in list(os.environ):
        if key.startswith("AI_DRAMA_") and key != "AI_DRAMA_SHOT_SECONDS":
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def short_shots(monkeypatch):
    """测试里把默认片长压到 3 秒。

    产品默认是 15 秒（见 core/media.default_shot_seconds），但 dryrun 是
    **真的**在本地推帧编码 —— 一个 12 镜头的用例从 3 秒变成 15 秒，
    整套测试就从 6 分钟变成半小时，而它验证的东西一点没变。

    所以这里统一下压，并且**专门留一条用例**去断言产品默认值真的是 15
    （tests/test_media.py::test_the_default_shot_length_is_fifteen_seconds），
    免得"测试里改小了"悄悄变成"产品里也改小了"。
    """
    monkeypatch.setenv("AI_DRAMA_SHOT_SECONDS", "3")
