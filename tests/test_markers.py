"""标了 `ffmpeg` 的用例，和**真的**要 ffmpeg 的用例，得是同一批。

这条是 CI 逼出来的：仓库一直在一个装了 ffmpeg 的机器上跑，所以
`test_chaining` / `test_sfx` / `test_providers` 里四条**要真 ffmpeg** 的用例
忘了加标记。本机一切正常，而在没装 ffmpeg 的环境里 `-m "not ffmpeg"`
跑过去就红 —— 正是 README 承诺"只拿提示词不用装 FFmpeg"的那类人。

修法有两半：把漏掉的标上（这次），再加这条测试盯着别再漏（以后）。
扫的是**源码**不是运行结果：直接看每个 test 函数体里有没有调
`media.require_ffmpeg()` 之类，有就必须有标记。
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: 只要函数体里出现这些调用，就一定需要 ffmpeg/ffprobe 在 PATH 上
NEEDS_FFMPEG_CALLS = {
    "require_ffmpeg",
    "require_ffprobe",
    "last_frame",
    "image_to_clip",
    "mux_audio",
    "mix_voice_and_music",
    "concat_videos",
}
#: 只在这几个模块里叫这些名字才算 —— 别的模块可能有同名函数
FFMPEG_MODULES = {"media"}


def _marked_ffmpeg(node: ast.FunctionDef) -> bool:
    for dec in node.decorator_list:
        src = ast.unparse(dec)
        if src.endswith("mark.ffmpeg") or src == "pytest.mark.ffmpeg":
            return True
    return False


def _calls_ffmpeg(node: ast.FunctionDef) -> bool:
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        name = ""
        if isinstance(fn, ast.Attribute):
            name = fn.attr
            base = ast.unparse(fn.value)
            if base.split(".")[-1] not in FFMPEG_MODULES:
                continue
        elif isinstance(fn, ast.Name):
            name = fn.id
        if name in NEEDS_FFMPEG_CALLS:
            return True
    return False


def test_every_test_that_reaches_for_ffmpeg_carries_the_marker():
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            if _calls_ffmpeg(node) and not _marked_ffmpeg(node):
                offenders.append(f"{path.name}::{node.name}")
    assert not offenders, (
        "这些用例要 ffmpeg 却没标 @pytest.mark.ffmpeg —— "
        "没装 ffmpeg 的人跑 `-m \"not ffmpeg\"` 会红：\n  "
        + "\n  ".join(offenders))


def test_the_offline_selection_really_excludes_them():
    """反过来验一次：标了的用例，在 `-m "not ffmpeg"` 下确实不跑。"""
    import subprocess
    import sys

    root = TESTS.parent
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-m", "not ffmpeg", "tests/test_chaining.py"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    # 只该剩下不需要 ffmpeg 的那条
    assert "test_the_first_part_has_no_previous_frame_to_chain_from" in out.stdout
    assert "test_the_second_part_starts_on_the_last_frame_of_the_first" not in out.stdout
