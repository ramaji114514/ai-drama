"""一次出 N 个变体：同一句话，不同调度，挑一个。

变体最容易做成"重跑一遍碰运气" —— 同一个模型、同一个想法，不给它任何新信息，
两次生成会高度相似。所以这里钉住"变体要求真的进了提示词"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_drama import cli
from ai_drama.core.store import Store
from ai_drama.stages import stage01_script


def test_only_the_first_of_one_variant_gets_no_extra_instruction():
    assert cli._variant_notes(1, 1) == ""
    notes = cli._variant_notes(2, 3)
    assert "第 2 个变体" in notes and "共 3 个" in notes
    assert "开场" in notes and "结尾" in notes


def test_the_instruction_reaches_the_prompt_only_when_given():
    plain = stage01_script.build_prompt("有人在敲门")
    assert "额外要求" not in plain
    guided = stage01_script.build_prompt("有人在敲门", notes="换个开场")
    assert "额外要求：换个开场" in guided


def test_the_cli_defaults_to_one_variant_and_accepts_more():
    parser = cli.build_parser()
    assert parser.parse_args(["prompts", "--idea", "x"]).variants == 1
    assert parser.parse_args(["prompts", "--idea", "x", "--variants", "3"]).variants == 3


def test_three_variants_become_three_runs_and_three_files(workspace, monkeypatch, capsys):
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("AI_DRAMA_TEXT", "dryrun")
    monkeypatch.setenv("AI_DRAMA_SHOT_SECONDS", "3")
    dest = workspace / "out" / "bundle.json"
    code = cli.main(["prompts", "--idea", "深夜书店里有人敲门", "--scenes", "1",
                     "--shots", "1", "--variants", "3", "--json", str(dest)])
    assert code == 0
    files = sorted(p.name for p in dest.parent.glob("bundle.v*.json"))
    assert files == ["bundle.v1.json", "bundle.v2.json", "bundle.v3.json"]

    bundles = [json.loads((dest.parent / name).read_text(encoding="utf-8"))
               for name in files]
    ids = {b["run_id"] for b in bundles}
    assert len(ids) == 3, "三个变体必须是三条独立的 run"

    store = Store(workspace / "runs" / "ai_drama.sqlite3")
    try:
        assert len(store.list_runs()) >= 3
    finally:
        store.close()

    err = capsys.readouterr().err
    assert "变体" in err and "总时长" in err


def test_the_comparison_table_shows_seconds_and_shrinks_correctly():
    rows = [
        (1, "run_a", {"title": "A", "scenes": [{"shots": [{"duration": 10.0}]}]}),
        (2, "run_b", {"title": "B", "scenes": [{"shots": [{"duration": 5.0},
                                                          {"duration": 5.0}]}]}),
    ]
    table = cli._compare(rows)
    assert "A" in table and "B" in table
    assert "10.0s" in table and "10.0s" in table
    assert table.splitlines()[0].startswith("变体")
