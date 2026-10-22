"""CLI 的上手体验测试。

这里测的不是"代码能跑"，而是**第一眼看到的信息是不是真的**：`doctor` 报的
provider 必须就是流水线真会用的那个；`.env` 必须真的会被读（文件头的注释
曾经说不会）；子命令必须都能在 `--help` 里被看见。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_drama import cli
from ai_drama.providers import registry

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """每个用例都从"什么都没配、也没有 .env"开始。"""
    for kind in registry.KIND_ENV:
        monkeypatch.delenv(registry.kind_env(kind), raising=False)
    for var in ("AI_DRAMA_BUDGET", "AI_DRAMA_CONCURRENCY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path, raising=True)


def _doctor(capsys) -> tuple[int, str]:
    code = cli.main(["doctor"])
    return code, capsys.readouterr().out


def test_doctor_reports_what_the_pipeline_will_actually_use(capsys, monkeypatch, tmp_path):
    """doctor 说"定妆照会跳过"，流水线却在跑定妆照 —— 这种谎报必须绝迹。

    真实历史：doctor 自己维护了一份默认值表，把 `t2i` 写成"未配置，定妆照
    会跳过"，而 `build_bundle` 早已默认 `dryrun`。两处各写一份，必然漂移。
    """
    # 离开仓库根：否则 doctor 会读到开发机自己的 `.env`，
    # "未设置" 那几行就永远看不到（这不是被测行为）。
    monkeypatch.chdir(tmp_path)
    code, out = _doctor(capsys)
    assert "AI_DRAMA_T2I 未设置 -> dryrun" in out
    assert "定妆照会跳过" not in out
    for kind in registry.KIND_ENV:
        assert f"] {kind:<6}" in out, kind        # 五个形态各有一行交代
    assert registry.build_bundle().t2i is not None   # 与流水线一致
    assert code in (0, 1)          # ffmpeg 装没装不该影响 provider 那一段


def test_doctor_flags_a_misconfigured_provider(capsys, monkeypatch):
    """写错名字要当场报出来并算阻断项，不能等到跑了一半才炸。"""
    monkeypatch.setenv("AI_DRAMA_VIDEO", "no-such-vendor")
    code, out = _doctor(capsys)
    assert "[X] video" in out
    assert "没注册过这个名字" in out
    assert code == 1


def test_doctor_says_when_t2i_is_off(capsys, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_T2I", "none")
    code, out = _doctor(capsys)
    assert "AI_DRAMA_T2I=none -> 已关闭" in out
    assert code in (0, 1)


def test_env_file_is_really_read(tmp_path, monkeypatch):
    """.env 会被读 —— `.env.example` 的文件头曾经说"骨架不读 .env"。

    按那句注释照做的人会在 shell 里白设一遍环境变量，或者以为 `.env` 是摆设。
    """
    (tmp_path / ".env").write_text(
        "# 注释行\nAI_DRAMA_VIDEO=dryrun\nAI_DRAMA_TEST_MARKER=hello\n\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AI_DRAMA_TEST_MARKER", raising=False)
    monkeypatch.chdir(tmp_path)   # `.env` 跟着当前目录走（装成命令之后也是这规矩）
    cli.load_env()
    assert os.environ.get("AI_DRAMA_TEST_MARKER") == "hello"
    assert registry.effective_name("video") == "dryrun"


def test_env_file_does_not_override_a_real_env_var(tmp_path, monkeypatch):
    """真实环境变量的优先级更高（手写解析走 setdefault）。"""
    (tmp_path / ".env").write_text("AI_DRAMA_TEXT=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("AI_DRAMA_TEXT", "from-shell")
    cli.load_env()
    assert os.environ["AI_DRAMA_TEXT"] == "from-shell"


def test_help_lists_every_subcommand(capsys):
    """新人靠 `--help` 发现能力；漏掉一个就等于那个功能不存在。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("doctor", "providers", "runs", "run", "resume", "status", "shots",
                "cost", "qc", "rules", "models", "skills", "faces"):
        assert cmd in out, cmd


def test_pytest_ini_does_not_pre_quiet_the_summary():
    """README 让人敲的测试命令必须能看见结果。

    回归点：`addopts` 里带 `-q`，用户再敲 `python -m pytest -q` 就叠成 `-qq`，
    连 "149 passed" 那行汇总都不打印，只剩进度点。
    """
    ini = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    line = next((ln for ln in ini.splitlines() if ln.strip().startswith("addopts")), "")
    assert line, "pytest.ini 里应当有 addopts（至少 --strict-markers）"
    tokens = line.split("=", 1)[1].split()
    assert "--strict-markers" in tokens
    assert "-q" not in tokens and "-qq" not in tokens, line


def test_split_shows_up_in_help_and_needs_a_real_episode(capsys):
    """`split` 在帮助里能被看见；没有分镜时给一句人话，不是 traceback。"""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "split" in out

    code = cli.main(["split", "run_does_not_exist"])
    assert code == 1
    assert "没有分镜记录" in capsys.readouterr().out
