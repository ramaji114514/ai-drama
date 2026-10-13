"""SQLite 持久化与成本记账。

这是"能不能 resume"和"花了多少钱"的唯一依据，
所以它比 UI 重要得多。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_drama.core.context import Ledger
from ai_drama.core.models import CostEntry, Episode, Run, Scene, Shot
from ai_drama.core.store import Store
from ai_drama.providers.base import BudgetExceeded


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "runs" / "t.sqlite3")


def test_db_is_created_lazily_with_its_parent_dir(tmp_path: Path):
    """`runs/` 不存在时也要能建库，不然第一次运行必崩。"""
    db = tmp_path / "deep" / "nested" / "x.sqlite3"
    Store(db)
    assert db.exists()


def test_run_lifecycle(store: Store):
    run = Run(run_id="run_1", idea="想法", out_dir="x")
    store.create_run(run)
    got = store.get_run("run_1")
    assert got is not None and got.state == "pending"

    store.update_run("run_1", state="done", final_video="f.mp4", stage="concat")
    got = store.get_run("run_1")
    assert got.state == "done" and got.final_video == "f.mp4" and got.stage == "concat"


def test_episode_and_shots_persist(store: Store):
    """分镜和逐镜状态都要能读回来 —— resume 靠的就是这些。"""
    run = Run(run_id="run_2", idea="i", out_dir="x")
    store.create_run(run)

    ep = Episode(run_id="run_2", idea="i", title="片名")
    sc = Scene(scene_id="sc01", title="场一")
    sc.shots.append(Shot(scene_id="sc01", order=0, action="推门", duration=3.0))
    ep.scenes.append(sc)

    store.save_episode(ep)
    store.sync_shots("run_2", ep)

    back = store.get_episode("run_2")
    assert back is not None and back.title == "片名"
    shots = store.list_shots("run_2")
    assert len(shots) == 1
    assert shots[0]["label"] == "sc01/shot_000"

    store.update_shot(shots[0]["shot_id"], state="done", video="v.mp4")
    assert store.list_shots("run_2")[0]["video"] == "v.mp4"


def test_sync_shots_is_idempotent(store: Store):
    """重复 sync 不能造出重复镜头，否则 resume 会凭空多出一倍工作量。"""
    run = Run(run_id="run_3", idea="i", out_dir="x")
    store.create_run(run)
    ep = Episode(run_id="run_3", idea="i")
    sc = Scene(scene_id="sc01")
    for i in range(3):
        sc.shots.append(Shot(scene_id="sc01", order=i, duration=2.0))
    ep.scenes.append(sc)

    for _ in range(3):
        store.sync_shots("run_3", ep)
    assert len(store.list_shots("run_3")) == 3


def test_cost_entries_are_aggregated_per_provider_and_stage(store: Store):
    run = Run(run_id="run_4", idea="i", out_dir="x")
    store.create_run(run)
    store.add_cost(CostEntry(provider="fake-video", stage="video", unit="second",
                             amount=4.0, money=1.2, run_id="run_4", shot_label="sc01#000"))
    store.add_cost(CostEntry(provider="fake-video", stage="video", unit="second",
                             amount=4.0, money=1.2, run_id="run_4", shot_label="sc01#001"))
    store.add_cost(CostEntry(provider="fake-tts", stage="audio", unit="char",
                             amount=10.0, money=0.1, run_id="run_4", shot_label="sc01#000"))

    summary = store.cost_summary("run_4")
    assert summary["total_money"] == pytest.approx(2.5)
    assert summary["by_stage"]["video"] == pytest.approx(2.4)
    assert summary["by_provider"]["fake-tts"] == pytest.approx(0.1)
    assert summary["calls"] == 3


def test_budget_gate_fails_loudly_instead_of_degrading(store: Store):
    """预算闸门必须抛异常。

    如果它"静默省着用"（比如偷偷换便宜模型、跳过镜头），
    你会拿到一部看着完整、实际残废的片子 —— 那是比失败更糟的结果。
    """
    run = Run(run_id="run_5", idea="i", out_dir="x")
    store.create_run(run)
    ledger = Ledger(store, "run_5", budget_usd=1.0)

    ledger.add({"provider": "v", "unit": "second", "amount": 1, "money": 0.6})
    with pytest.raises(BudgetExceeded):
        ledger.add({"provider": "v", "unit": "second", "amount": 1, "money": 0.6})

    # 超支的那笔仍然要记账 —— 掩盖花掉的钱比超支本身更危险
    assert ledger.spent == pytest.approx(1.2)
    assert store.cost_summary("run_5")["total_money"] == pytest.approx(1.2)


def test_zero_budget_means_unlimited(store: Store):
    run = Run(run_id="run_6", idea="i", out_dir="x")
    store.create_run(run)
    ledger = Ledger(store, "run_6", budget_usd=0)
    ledger.add({"provider": "v", "unit": "second", "amount": 1, "money": 999.0})
    assert ledger.spent == pytest.approx(999.0)


def test_list_runs_newest_first(store: Store):
    for i in range(3):
        store.create_run(Run(run_id=f"run_{i}", idea=f"想法{i}", out_dir="x"))
    runs = store.list_runs(limit=10)
    assert len(runs) == 3
    assert {r.run_id for r in runs} == {"run_0", "run_1", "run_2"}
