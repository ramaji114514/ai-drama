"""文档说的，代码里得真有。

这条测试是**固化下来的一次教训**。字幕功能从第一天起就写在 README 的依赖表、
架构树和职责表里（"拼接 + 混音 + 字幕"），而代码里根本不存在 —— 一部竖屏短剧
交付出去，观众看到的是没有台词的画面，而报告里一切正常。

文档说谎是这类工具最贵的 bug，因为它**不会报错**。所以这里把当时那个手工
检查变成一条会失败的测试：

    python tools/doc_audit.py     # 同一份逻辑，命令行跑给人看

它查三层：

1. 文档里点名提到的文件，真的存在吗？
2. 文档里写的 `python run.py <子命令>`，真的注册了吗？
3. 文档承诺的**能力**（不只是文件名）在代码里找得到对应实现吗？
   `core/media.py` 当年是一直存在的 —— 不存在的是"烧字幕"这件事。

三种"看起来像缺失但其实没问题"的情况在 `tools/doc_audit.py` 里显式列出
（运行期产物、外部参考项目），而不是把正则放宽 —— 放宽等于不检查。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "doc_audit.py"


def _load():
    spec = importlib.util.spec_from_file_location("doc_audit", _TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _load()


def test_every_file_the_docs_point_at_exists(audit):
    root = _TOOL.parent.parent
    bad = audit.broken_references(root)["paths"]
    assert not bad, (
        "文档里提到了不存在的文件："
        + "；".join(f"{rel} <- {', '.join(d)}" for rel, d in sorted(bad.items()))
    )


def test_every_cli_command_the_docs_show_is_registered(audit):
    root = _TOOL.parent.parent
    bad = audit.broken_references(root)["commands"]
    assert not bad, (
        "文档里写了没注册的子命令："
        + "；".join(f"{c} <- {', '.join(d)}" for c, d in sorted(bad.items()))
    )


def test_every_image_the_docs_show_exists(audit):
    """文档引用的图得真的在仓库里。

    坏掉的图片链接也是"文档说谎"，而且是**最不会报错的那种**：主页上留一个
    空白框，谁都不会收到通知。README 是项目的门面，门面上破个洞比正文里
    写错一句话更显眼。
    """
    root = _TOOL.parent.parent
    bad = audit.broken_references(root)["images"]
    assert not bad, (
        "文档里引用了不存在的图："
        + "；".join(f"{rel} <- {', '.join(d)}" for rel, d in sorted(bad.items()))
    )


def test_the_english_readme_is_a_real_translation_not_a_stub():
    """英文 README 得是同一份东西，不是一句 "see Chinese"。

    英文页最容易烂掉的方式不是写错，是**没人看、慢慢和中文版走散**：中文版加了
    `--target`、海螺、CSV 导出，英文页还停在上个月。所以这里不比对措辞，只钉
    三件"走散就一定会暴露"的事实：两种语言互相有链接、英文页引了同一批图、
    英文页也写了那几条命令。
    """
    root = _TOOL.parent.parent
    zh = (root / "README.md").read_text(encoding="utf-8")
    en_path = root / "README.en.md"
    assert en_path.exists(), "README.en.md 不存在"
    en = en_path.read_text(encoding="utf-8")

    assert "README.en.md" in zh, "中文 README 没有链接到英文版"
    assert "README.md" in en, "英文 README 没有链接回中文版"

    for image in ("docs/images/web-prompts.jpg", "docs/images/web-dialects.jpg",
                  "docs/images/web-cost.jpg", "docs/images/pipeline.png"):
        assert image in zh and image in en, f"图 {image} 只出现在一种语言里"

    for flag in ("--target", "--dialect", "--format csv"):
        assert flag in zh and flag in en, f"{flag} 只出现在一种语言里"


def test_no_capability_is_promised_but_missing(audit):
    """比"文件在不在"更要紧的一层：能力有没有落地。

    字幕那次露馅的地方：`ai_drama/core/media.py` 这个**文件**一直存在，
    不存在的是"烧字幕"这件事。所以这里按行为关键词再查一遍。
    """
    root = _TOOL.parent.parent
    unkept = audit.unkept_promises(root)
    assert not unkept, (
        "文档承诺了但代码里找不到对应实现："
        + "；".join(f"{label}（期望 /{pat}/）" for label, pat in sorted(unkept.items()))
    )


def test_the_audit_can_actually_fail(tmp_path):
    """它得真的会失败 —— 一个永远通过的检查器比没有检查器更糟。

    造一个文档里提到不存在文件的最小仓库，断言审计能抓到。
    """
    audit = _load()
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "见 `ai_drama/core/nowhere.py` 和 `python run.py nonexistent`。",
        encoding="utf-8")
    (tmp_path / "docs" / "architecture.md").write_text("空", encoding="utf-8")
    (tmp_path / "docs" / "adding-a-provider.md").write_text("空", encoding="utf-8")
    (tmp_path / "docs" / "making-a-series.md").write_text("空", encoding="utf-8")
    (tmp_path / "ai_drama").mkdir()
    (tmp_path / "run.py").write_text(
        "import argparse\np=argparse.ArgumentParser()\n"
        "s=p.add_subparsers(dest='c')\n"
        "s.add_parser('run')\n"
        "p.parse_args()\n", encoding="utf-8")

    result = audit.broken_references(tmp_path)
    assert "ai_drama/core/nowhere.py" in result["paths"]
    assert "nonexistent" in result["commands"]

    # 图片那一层也要能抓到：引一张不存在的图
    (tmp_path / "README.md").write_text(
        "![演示](docs/images/nowhere.png)", encoding="utf-8")
    assert "docs/images/nowhere.png" in audit.broken_references(tmp_path)["images"]

    # 能力层也要能抓到：把"字幕烧录"从一个空仓库里承诺出来
    (tmp_path / "README.md").write_text("字幕会烧进画面。", encoding="utf-8")
    assert "字幕烧录" in audit.unkept_promises(tmp_path)
