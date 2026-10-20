"""仓库里不能有真 key。

这个项目靠 .env 拿厂商的 key，而 .env 被 .gitignore 挡住 —— 但"被挡住"不是保证：
复制一个 .env.example 改名、或者在文档里贴一句"我的配置长这样"，key 就进去了。
仓库是公开的，一次误提交等于把账号交给别人用。

所以这里在测试里把这条线钉住：凡是 git 跟踪的文件，出现像真 key 的串就红。

为什么不是查"包含 sk-"：占位符（`sk-xxx`、`sk-你的key`）必须能写，
否则示例文件没法教人填。所以查的是**长度像真 key**的形态。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 各家真 key 的形态。故意写长一点：占位符一般只有几个 x，真 key 是一长串。
PATTERNS = {
    "通用 sk- 前缀（OpenAI / DeepSeek / aihubmix / 百炼）": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "Anthropic": re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [ROOT / line for line in out.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def tracked() -> list[Path]:
    try:
        files = _tracked_files()
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - 不在 git 仓库里
        pytest.skip("不在 git 仓库里，跳过")
    files = [p for p in files if p.is_file()]
    # 防止 git 调用悄悄失败变成"扫了 0 个文件、什么都没找到"的假绿
    assert len(files) > 50, f"只扫到 {len(files)} 个文件，git ls-files 大概没工作"
    return files


def test_nothing_that_looks_like_a_real_key_is_committed(tracked):
    hits = []
    for path in tracked:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - 二进制/权限
            continue
        for label, pattern in PATTERNS.items():
            for m in pattern.finditer(text):
                hits.append(f"{path.relative_to(ROOT)}: {label} -> {m.group()[:8]}…")
    assert not hits, "仓库里出现了像真 key 的串：\n" + "\n".join(hits)


def test_the_env_file_is_ignored_and_never_tracked(tracked):
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert "!.env.example" in gitignore
    assert ROOT / ".env" not in tracked


def test_the_example_env_only_carries_placeholders(tracked):
    """示例文件里可以有占位符，因为上面那条测试查的是长度像真 key 的串。"""
    example = ROOT / ".env.example"
    assert example in tracked
    text = example.read_text(encoding="utf-8")
    assert "AI_DRAMA_DEEPSEEK_API_KEY=sk-xxx" in text
