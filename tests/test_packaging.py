"""能不能装成一条命令：`pip install .` 之后 `ai-drama` 真的能跑。

零依赖是卖点，所以这里钉的第一件事是 **dependencies 必须是空的**：多一个
隐藏依赖，README 里那句"装完就能跑"就成了谎话。第二件事是 `config/` 和
`skills/` 得随包发出去 —— 源码仓库里它们是两个普通文件夹，wheel 里没有
"仓库根"这种东西，不发就会在装完之后静默少一层默认值。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")   # 3.11+；3.10 上直接跳过
ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_it_ships_with_no_dependencies_at_all(project):
    assert project["dependencies"] == []
    # 可选增强可以有，但必须是 extras，不能进 dependencies
    extras = project.get("optional-dependencies") or {}
    assert "dotenv" in extras and "images" in extras


def test_the_command_is_ai_drama(project):
    assert project["scripts"]["ai-drama"] == "ai_drama.cli:main"


def test_python_floor_matches_what_the_code_uses(project):
    assert project["requires-python"] == ">=3.10"


def test_config_skills_and_agent_go_into_the_wheel():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"]["data-files"]
    joined = {k: " ".join(v) for k, v in data.items()}
    assert "share/ai-drama/config" in joined
    assert "config/*.yaml" in joined["share/ai-drama/config"]
    assert "skills/*.md" in joined["share/ai-drama/skills"]
    assert "agent/SKILL.md" in joined["share/ai-drama/agent"]


def test_data_root_prefers_the_env_var_then_the_repo_then_the_share_dir(monkeypatch, tmp_path):
    from ai_drama import resources

    monkeypatch.setenv("AI_DRAMA_HOME", str(tmp_path / "home"))
    assert resources.data_root() == (tmp_path / "home").resolve()

    monkeypatch.delenv("AI_DRAMA_HOME")
    monkeypatch.setattr(resources, "_PACKAGE", tmp_path / "site-packages" / "ai_drama")
    monkeypatch.setattr(resources.sysconfig, "get_path",
                        lambda name: str(tmp_path / "prefix"))
    share_config = tmp_path / "prefix" / "share" / "ai-drama" / "config"
    share_config.mkdir(parents=True)
    assert resources.data_root() == share_config.parent
    assert resources.skills_dir() == share_config.parent / "skills"


def test_without_any_data_dir_it_falls_back_to_the_package(monkeypatch, tmp_path):
    """读不到 config 不该崩：catalog 用内置目录、qc 用内置规则。"""
    from ai_drama import resources

    monkeypatch.delenv("AI_DRAMA_HOME", raising=False)
    monkeypatch.setattr(resources, "_PACKAGE", tmp_path / "ai_drama")
    monkeypatch.setattr(resources.sysconfig, "get_path",
                        lambda name: str(tmp_path / "nowhere"))
    assert resources.data_root() == tmp_path / "ai_drama"
