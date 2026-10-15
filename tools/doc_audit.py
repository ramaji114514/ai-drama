"""审计：文档里提到的文件 / 子命令，代码里是不是真的有。

这个检查是**固化下来的一次教训**。字幕功能从第一天起就写在 README 的
依赖表、架构树和职责表里（"拼接 + 混音 + 字幕"），而代码里根本不存在 ——
一部竖屏短剧交付出去，观众看到的是没有台词的画面，而报告里一切正常。

文档说谎是这一类工具最贵的 bug：**它不会报错**。所以这里做一次机械核对，
并且由 `tests/test_doc_truth.py` 钉住 —— 以后往文档里写一个不存在的文件，
CI 会当场停下来。

    python tools/doc_audit.py

三种"看起来不存在但其实是好的"情况，都在下面显式列出原因，
而不是靠正则写得宽松一点（宽松等于不检查）：
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set

#: 运行期才生成的东西，静态检查时当然不在仓库里。
RUNTIME_ARTIFACTS = {
    "episode.json", "final_video.mp4", "timeline.json", "subtitles.srt",
    "subtitles.ass", ".env", "project.json", "qc_report.json", "cost.json",
    "runs", "shot.json", "voice.wav", "clip.mp4", "keyframe.png",
}

#: 引用的是**别的**项目，不是本仓库。文档里都写明了"参考项目"。
EXTERNAL_REFS = {
    "domain/realism.py",
    "domain/compliance/__init__.py",
    "script2video.py",
    "identity.py",
}

DOCS = (
    "README.md",
    "docs/architecture.md",
    "docs/adding-a-provider.md",
    "docs/making-a-series.md",
)
_PATH_RE = re.compile(r"`([a-zA-Z_][\w./-]*\.(?:py|md|yaml|yml|json|txt|env))`")
_CMD_RE = re.compile(r"python run\.py ([a-z][a-z-]*)")

# 文档里短写文件名（`base.py`、`test_qc.py`）是刻意的，不必写全路径。
# 所以匹配按"同名文件在仓库里存在"算数 —— 这仍然能抓住"整个功能都不存在"
# 那类问题（字幕当年就是这么露馅的：`media.py` 存在，但里面没有任何字幕函数，
# 所以这里还得配一条"关键词也要能在代码里找到"的检查）。
_BASENAME_CACHE: Dict[str, List[Path]] = {}


def _all_files(root: Path) -> List[Path]:
    out = []
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts and "runs" not in p.parts:
            out.append(p)
    return out


def _resolve(root: Path, rel: str, files: List[Path]) -> bool:
    if (root / rel).exists():
        return True
    base = rel.rsplit("/", 1)[-1]
    if base not in _BASENAME_CACHE:
        _BASENAME_CACHE[base] = [p for p in files if p.name == base]
    if _BASENAME_CACHE[base]:
        return True
    # 允许"目录名 + 文件名"的写法：提供器里写 base.py 指的是 providers/base.py
    shallow = rel.rsplit("/", 1)
    if len(shallow) == 2:
        return bool(_BASENAME_CACHE[shallow[1]])
    return False


def subcommands(root: Path) -> Set[str]:
    out = subprocess.run(
        [sys.executable, str(root / "run.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    groups = re.findall(r"\{([a-z,\-]+)\}", out)
    return {c.strip() for g in groups for c in g.split(",") if c.strip()}


def broken_references(root: Path) -> Dict[str, Dict[str, List[str]]]:
    """返回 {"paths": {缺失路径: [文档…]}, "commands": {未注册子命令: [文档…]}}。"""
    files = _all_files(root)
    known = subcommands(root)
    bad_paths: Dict[str, List[str]] = {}
    bad_cmds: Dict[str, List[str]] = {}

    for doc in DOCS:
        p = root / doc
        if not p.exists():
            bad_paths.setdefault(f"<文档本身缺失> {doc}", []).append(doc)
            continue
        text = p.read_text(encoding="utf-8")
        for m in _PATH_RE.finditer(text):
            rel = m.group(1)
            if rel in RUNTIME_ARTIFACTS or rel in EXTERNAL_REFS:
                continue
            if not _resolve(root, rel, files):
                bad_paths.setdefault(rel, []).append(doc)
        for m in _CMD_RE.finditer(text):
            cmd = m.group(1)
            if cmd not in known:
                bad_cmds.setdefault(cmd, []).append(doc)

    return {"paths": bad_paths, "commands": bad_cmds}


#
# 第二层：文档承诺的**能力**在代码里到底有没有落地。
#
# 只查文件名是不够的 —— 字幕当年 `ai_drama/core/media.py` 这个文件是存在的，
# 不存在的是"烧字幕"这件事。所以要按**行为关键词**再查一遍：
# 每条是一个 (文档说法, 必须能在代码里找到的正则)。
#
PROMISES = (
    ("字幕烧录", r"def burn_ass|def build_ass"),
    ("时间轴导出", r"def build_srt|timeline"),
    ("口型同步", r"class LipSyncProvider"),
    ("画面校验", r"class VisionProvider"),
    ("角色圣经", r"class Project"),
    ("出镜角色", r"characters_on_screen"),
)


def unkept_promises(root: Path) -> Dict[str, str]:
    """文档里点名承诺、而代码里找不到对应实现的东西。"""
    blob = ""
    for p in (root / "ai_drama").rglob("*.py"):
        blob += p.read_text(encoding="utf-8", errors="ignore")
    out: Dict[str, str] = {}
    for label, pattern in PROMISES:
        if not re.search(pattern, blob):
            out[label] = pattern
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    result = broken_references(root)
    bad = False

    print("== 文档提到但不存在的文件 ==")
    for rel, docs in sorted(result["paths"].items()):
        print(f"  {rel:<45} <- {', '.join(sorted(set(docs)))}")
        bad = True
    print("  （无）" if not result["paths"] else "")

    print("\n== 文档提到但没注册的子命令 ==")
    for cmd, docs in sorted(result["commands"].items()):
        print(f"  {cmd:<20} <- {', '.join(sorted(set(docs)))}")
        bad = True
    print("  （无）" if not result["commands"] else "")

    print("\n== 文档承诺但代码里找不到的能力 ==")
    unkept = unkept_promises(root)
    for label, pattern in sorted(unkept.items()):
        print(f"  {label:<20} 期望匹配 /{pattern}/")
        bad = True
    print("  （无）" if not unkept else "")

    if bad:
        print("\n文档和代码对不上 —— 这正是「字幕从第一天就写在文档里、"
              "代码里根本不存在」那类事故的样子。")
        return 1
    print("\n文档与代码一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
