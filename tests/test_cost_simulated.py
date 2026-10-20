"""账单不许说谎：dryrun 编出来的钱必须和真扣的钱分开。

这套工具唯一会让人紧张的数字就是账单。dryrun 的价格表是编的（"假装这张图
值 0.03 美元"），一分钱没出门；而只产提示词的人现在根本不用真 provider。
两者混在一个数里，用户会以为自己被扣了钱 —— 那不是显示问题，是会让工具
失去信任的问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_drama.cli import print_result
from ai_drama.core.context import Ledger, StageReport, is_simulated
from ai_drama.core.models import CostEntry, Run
from ai_drama.core.store import Store
from ai_drama.web.api import runs_payload


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "t.sqlite3")


def _usage(provider: str, money: float, unit: str = "call") -> dict:
    return {"provider": provider, "stage": "", "unit": unit, "amount": 1.0, "money": money}


# ————————————————————————————————————————---- 判定


def test_the_dryrun_prefix_is_the_one_signal():
    """靠名字前缀认占位实现，和 registry 的 `dryrun-<kind>` 命名一致。"""
    assert is_simulated("dryrun-text")
    assert is_simulated("dryrun")
    assert not is_simulated("dashscope-video")
    assert not is_simulated("")
    assert not is_simulated(None)


# ————————————————————————————————————————---- 落库


def test_a_dryrun_call_is_recorded_as_simulated(store: Store):
    ledger = Ledger(store, "run_s")
    ledger.add(_usage("dryrun-text", 0.03))

    summary = store.cost_summary("run_s")
    assert summary["lines"][0]["simulated"] is True
    assert summary["simulated_money"] == pytest.approx(0.03)
    assert summary["real_money"] == pytest.approx(0.0)


def test_a_real_call_is_not_simulated(store: Store):
    ledger = Ledger(store, "run_r")
    ledger.add(_usage("deepseek-text", 0.001))

    summary = store.cost_summary("run_r")
    assert summary["lines"][0]["simulated"] is False
    assert summary["real_money"] == pytest.approx(0.001)
    assert summary["simulated_money"] == pytest.approx(0.0)


def test_the_two_numbers_add_up(store: Store):
    """改了这么久的分账，最容易错的就是这里：real + simulated 必须等于 total。"""
    ledger = Ledger(store, "run_m")
    for provider, money in [("dryrun-image", 0.03), ("dryrun-video", 0.5),
                            ("deepseek-text", 0.002)]:
        ledger.add(_usage(provider, money))

    s = store.cost_summary("run_m")
    assert s["total_money"] == pytest.approx(0.532)
    assert s["real_money"] == pytest.approx(0.002)
    assert s["simulated_money"] == pytest.approx(0.53)
    assert s["real_money"] + s["simulated_money"] == pytest.approx(s["total_money"])


def test_the_per_stage_split_matches_the_lines(store: Store):
    ledger = Ledger(store, "run_d")
    ledger.add(_usage("dryrun-image", 0.03), stage="frames")
    ledger.add(_usage("deepseek-text", 0.004), stage="script")

    s = store.cost_summary("run_d")
    assert s["by_stage_simulated"] == pytest.approx({"frames": 0.03})
    assert s["by_provider_simulated"] == pytest.approx({"dryrun-image": 0.03})


def test_zero_money_groups_do_not_pollute_the_breakdown(store: Store):
    """价格为 0 的 provider（没配价）不该在"钱花在哪"里占一行。"""
    ledger = Ledger(store, "run_z")
    ledger.add(_usage("aihubmix-video", 0.0))
    ledger.add(_usage("deepseek-text", 0.01))

    s = store.cost_summary("run_z")
    assert set(s["by_provider"]) == {"deepseek-text"}


def test_the_budget_gate_still_counts_simulated_money(store: Store):
    """闸门照旧算全部（占位也算），但报表要分得清 —— 两者不能互相顶替。"""
    ledger = Ledger(store, "run_b", budget_usd=0.01)
    with pytest.raises(Exception):
        ledger.add(_usage("dryrun-image", 0.03))
    assert ledger.spent == pytest.approx(0.03)
    assert ledger.real_spent() == pytest.approx(0.0)


# ————————————————————————————————————————---- 阶段报告


def test_a_stage_report_knows_how_much_of_its_money_was_fake():
    rep = StageReport(stage="video")
    rep.add_usage(_usage("dryrun-video", 0.5))
    rep.add_usage(_usage("dashscope-video", 1.0))
    assert rep.money == pytest.approx(1.5)
    assert rep.simulated == pytest.approx(0.5)
    assert rep.to_dict()["simulated"] == pytest.approx(0.5)


def test_add_money_marks_by_provider_name():
    rep = StageReport(stage="qc")
    rep.add_money(0.2, provider="dryrun-vision")
    rep.add_money(0.1, provider="dashscope-vision")
    assert rep.simulated == pytest.approx(0.2)


# ————————————————————————————————————————---- 人看到的那一行


def _result(**cost) -> dict:
    return {
        "reports": [StageReport(stage="video").to_dict()],
        "cost": {
            "total_money": cost.get("total", 0.0),
            "real_money": cost.get("real", 0.0),
            "simulated_money": cost.get("sim", 0.0),
        },
    }


def test_the_total_reads_as_real_money_when_nothing_was_faked(capsys):
    print_result(_result(total=1.2, real=1.2))
    out = capsys.readouterr().out
    assert "总计花费 $1.2000" in out
    assert "占位" not in out


def test_the_total_says_so_when_part_of_it_was_fake(capsys):
    print_result(_result(total=0.532, real=0.002, sim=0.53))
    out = capsys.readouterr().out
    assert "实际花费 $0.0020" in out
    assert "占位估算" in out
    assert "没有真的扣费" in out


def test_a_stage_line_points_out_its_own_fake_part(capsys):
    rep = StageReport(stage="storyboard")
    rep.add_usage(_usage("dryrun-t2i", 0.06))
    print_result({"reports": [rep.to_dict()], "cost": {}})
    out = capsys.readouterr().out
    assert "占位估算" in out


def test_old_rows_are_backfilled_from_their_provider_name(tmp_path: Path):
    """升级前记的账没有 simulated 列，但 provider 名还在 —— 事实算得出来。

    不回填的话，老 run 一打开，那 0.18 美元占位费会被当成真花掉的钱。
    """
    db = tmp_path / "old.sqlite3"
    s = Store(db)
    s.create_run(Run(run_id="run_old", idea="i", out_dir="x"))
    s.add_cost(CostEntry(provider="dryrun-t2i", stage="storyboard", unit="image",
                         amount=2, money=0.06, run_id="run_old"))
    # 抹掉标记，模拟"这条是升级前写进去的"
    with s.tx() as c:
        c.execute("UPDATE costs SET simulated=0 WHERE run_id='run_old'")
    s.close() if hasattr(s, "close") else None

    again = Store(db).cost_summary("run_old")
    assert again["simulated_money"] == pytest.approx(0.06)
    assert again["real_money"] == pytest.approx(0.0)


# ————————————————————————————————————————---- 网页


def test_the_run_list_carries_both_numbers(store: Store):
    store.create_run(Run(run_id="run_w", idea="i", out_dir="x"))
    Ledger(store, "run_w").add(_usage("dryrun-image", 0.03))
    row = runs_payload(Path("."), store)[0]
    assert row["money"] == pytest.approx(0.03)
    assert row["real_money"] == pytest.approx(0.0)
    assert row["simulated_money"] == pytest.approx(0.03)
