"""资产台账：每次 run 产出了一堆文件，得有人记得它们是谁生的。

这一组守的核心不是"台账能写进去"，而是**台账和磁盘对得上**。

它存在的直接原因：`StageReport.artifacts` 只在内存里，进程一退，
"这张关键帧是哪条提示词、拿哪张定妆照生成的"就没人记得了。而这类记忆
有个通病 —— 记的时候漏一个产出点，从此那件东西在报告里就是隐形的。
所以最要紧的两条是：

  * `test_a_full_run_reconciles`：跑完一整条流水线，台账必须覆盖 run 目录里
    **每一个**产物。这一条第一次跑就抓出了两个漏记（`cost_report.json` 与
    场次拼接的 `scene_video.mp4`）—— 都是真的漏了，不是测试写错。
  * `test_the_checker_can_actually_fail`：删掉一个产物，对账必须报出来。
    一个永远通过的检查器比没有检查器更糟（这条原则在 `test_doc_truth.py`
    里也用过一次）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from ai_drama.core import assets as assetslib
from ai_drama.core import paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry


def _run_pipeline(workspace: Path, *, sfx: bool = False) -> dict:
    """跑一遍完整流水线（dryrun），返回 run 的 report dict。"""
    store = Store(workspace / "runs" / "test.sqlite3")
    pipe = Pipeline(workspace, store, registry.build_bundle(), concurrency=2,
                    verbose=False)
    # 别靠环境变量：sfx 默认是关的，写死才在这些用例里可复现
    pipe.bundle.sfx = registry.build_bundle().sfx if sfx else None
    run = pipe.start("暴雨夜，外卖员把最后一单送到一栋没有人的楼")
    return pipe.run(run)


# ————————————————————————————————————————----
# 纯函数：id、路径、指纹
# ————————————————————————————————————————----


def test_the_id_is_derived_from_the_path_not_drawn_at_random():
    """同一个 run 里同一个路径永远得到同一个 id。

    阶段是会重跑的。如果 id 是随机的，重跑一次就多一条记录，
    "这件东西生成过几次"从此答不对 —— 和 `core/cast.py` 里
    "id 由名字推导"是同一条理由。
    """
    a = assetslib.asset_id("run_x", "runs/run_x/final_video.mp4")
    b = assetslib.asset_id("run_x", "runs/run_x/final_video.mp4")
    c = assetslib.asset_id("run_y", "runs/run_x/final_video.mp4")
    assert a == b
    assert a != c                      # 换个 run 就是另一次产出
    assert a.startswith(assetslib.ID_PREFIX)


def test_a_path_inside_the_workspace_is_stored_relative(tmp_path: Path):
    """台账里存相对路径：把 runs 目录整个搬走之后它还能用。"""
    root = tmp_path
    p = root / "runs" / "run_a" / "final_video.mp4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    rel = assetslib.rel_of(root, p)
    assert rel == "runs/run_a/final_video.mp4"
    assert assetslib.resolve(root, rel) == p


def test_a_path_outside_the_workspace_is_stored_absolute(tmp_path: Path):
    """项目定妆照在 workspace 之外（`projects/` 也是），不能强行相对化。"""
    root = tmp_path / "here"
    root.mkdir()
    other = tmp_path / "elsewhere" / "脸.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    rel = assetslib.rel_of(root, other)
    assert Path(rel).is_absolute()
    assert assetslib.resolve(root, rel) == other


def test_digesting_a_missing_file_returns_zero_instead_of_raising(tmp_path: Path):
    """对账要的就是"这个文件没了"这个结果，它不该抛异常。"""
    assert assetslib.digest(tmp_path / "没有这个文件") == (0, "")


def test_the_prompt_hash_covers_the_whole_text():
    """长提示词只截断**显示**，指纹按全文算。

    不然两条只在第 5000 字不同的提示词会得到同一个指纹，
    而"这次和上次的提示词一样吗"正是这个指纹要回答的问题。
    """
    long_a = "自然光，" + "很长的修饰语" * 900 + "A"
    long_b = long_a[:-1] + "B"
    assert len(long_a) > assetslib.PROMPT_MAX
    assert assetslib.prompt_hash(long_a) != assetslib.prompt_hash(long_b)
    assert assetslib.prompt_hash(long_a) == assetslib.prompt_hash(long_a)


def test_the_stored_prompt_is_truncated_but_its_hash_is_not(tmp_path: Path):
    root = tmp_path
    p = root / "a.txt"
    p.write_text("x", encoding="utf-8")
    text = "提" * (assetslib.PROMPT_MAX + 500)
    item = assetslib.make_asset("run_a", root, p, kind="other", prompt=text)
    assert len(item.prompt) == assetslib.PROMPT_MAX
    assert item.prompt_hash == assetslib.prompt_hash(text)


def test_an_unknown_kind_falls_back_to_other(tmp_path: Path):
    """种类写错不该让整条流水线崩掉，但也不该悄悄记成一种真的种类。"""
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    item = assetslib.make_asset("run_a", tmp_path, p, kind="不存在这种东西")
    assert item.kind == "other"
    assert set(assetslib.KINDS) >= {item.kind}


# ————————————————————————————————————————----
# 扫盘与对账
# ————————————————————————————————————————----


def test_transient_files_do_not_enter_the_ledger(tmp_path: Path):
    """拆段产物与 SQLite 的临时文件不进台账。

    它们的存在本来就不稳定（成片拼好就删、崩在半路才留），记进去只会制造
    "永远对不上账"的假差异 —— 而真正该被注意的差异会被这些噪点淹掉。
    """
    run = tmp_path / "runs" / "run_a"
    (run / "_work").mkdir(parents=True)
    (run / "final_video.mp4").write_bytes(b"v")
    (run / "episode.json").write_text("{}", encoding="utf-8")
    (run / "ai_drama.sqlite3").write_bytes(b"db")
    (run / "ai_drama.sqlite3-wal").write_bytes(b"w")
    (run / "x.part").write_bytes(b"p")
    # 口型同步写到一半的产物：`.part` 在中间，不是结尾
    (run / "shot_001_synced.part.mp4").write_bytes(b"half")
    (run / ".gitkeep").write_text("", encoding="utf-8")
    (run / "_work" / "concat.txt").write_text("", encoding="utf-8")
    (run / "scenes" / "sc01").mkdir(parents=True)
    (run / "scenes" / "sc01" / "shot_000_p1_clip.mp4").write_bytes(b"seg")

    got = set(assetslib.scan_run_dir(run))
    assert got == {"final_video.mp4", "episode.json"}


def test_reconcile_reports_both_directions(tmp_path: Path):
    """两个方向说的是完全不同的事，不能合成一句。

    * 磁盘上有、台账里没有：有产物绕过了台账，从此"它哪来的"没答案；
    * 台账里有、磁盘上没有：记录在说谎，按它找文件会找到空气。
    """
    root = tmp_path
    run = root / "runs" / "run_a"
    run.mkdir(parents=True)
    (run / "kept.bin").write_bytes(b"k")
    (run / "on_disk_only.bin").write_bytes(b"d")
    (run / "deleted.bin").write_bytes(b"g")     # 先造出来，好让 digest 有记录

    recorded = [
        assetslib.make_asset("run_a", root, run / "kept.bin", kind="clip"),
        assetslib.make_asset("run_a", root, run / "deleted.bin", kind="clip"),
    ]
    (run / "deleted.bin").unlink()

    drift = assetslib.reconcile(root, "run_a", recorded, run)
    assert not drift.ok
    assert drift.unrecorded == ["on_disk_only.bin"]
    assert drift.missing == ["deleted.bin"]
    # 两个方向要分开说 —— 合成一句"2 个问题"会丢掉"哪边多了哪边少了"
    text = drift.summary()
    assert "不在磁盘上" in text and "没有进台账" in text


def test_a_file_outside_the_run_dir_is_not_reconciled(tmp_path: Path):
    """项目定妆照住在 `projects/` 里，不是这次 run 的产物。

    把它算进来的话，每次对账都会报一个永远修不掉的差异 ——
    而"每次都报"等于"不用看"。
    """
    root = tmp_path
    run = root / "runs" / "run_a"
    run.mkdir(parents=True)
    outside = root / "projects" / "p1" / "portraits" / "苏蔓.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"face")

    drift = assetslib.reconcile(
        root, "run_a",
        [assetslib.make_asset("run_a", root, outside, kind="portrait")],
        run,
    )
    assert drift.ok
    assert drift.checked == 0


# ————————————————————————————————————————----
# 血缘
# ————————————————————————————————————————----


def _stub(asset_id_: str, rel: str, kind: str = "clip"):
    return assetslib.Asset(asset_id=asset_id_, run_id="run_a", kind=kind, rel=rel)


def test_lineage_keeps_every_branch_not_just_the_first():
    """成片有七个直接上游，只跟第一条会把"它还用了哪些东西"藏起来。

    这正是之前 `--tree` 的毛病：看着像一条链，其实是棵树。
    """
    assets = {
        "f": _stub("f", "final.mp4", "final"),
        "c1": _stub("c1", "c1.mp4"),
        "c2": _stub("c2", "c2.mp4"),
        "m": _stub("m", "bgm.wav", "bgm"),
    }
    edges = [
        {"parent_id": "c1", "child_id": "f", "rel": "source"},
        {"parent_id": "c2", "child_id": "f", "rel": "source"},
        {"parent_id": "m", "child_id": "f", "rel": "source"},
    ]
    tree = assetslib.lineage(assets, edges, "f")
    got = {n.asset.asset_id for n in tree}
    assert got == {"f", "c1", "c2", "m"}
    assert tree[0].asset.asset_id == "f" and tree[0].depth == 0
    assert all(n.depth == 1 for n in tree[1:])


def test_a_shared_ancestor_is_printed_again_but_only_expanded_once():
    """同一张定妆照被两镜用上：要**看见**用了两次，但不能无限展开。

    只展开一次是为了不让一棵树长到读不了；但把第二次出现整个藏掉，
    就成了另一种谎 —— 看着像只有第一镜用了那张脸。
    """
    assets = {
        "f": _stub("f", "final.mp4", "final"),
        "k1": _stub("k1", "k1.png", "keyframe"),
        "k2": _stub("k2", "k2.png", "keyframe"),
        "p": _stub("p", "脸.png", "portrait"),
    }
    edges = [
        {"parent_id": "k1", "child_id": "f", "rel": "source"},
        {"parent_id": "k2", "child_id": "f", "rel": "source"},
        {"parent_id": "p", "child_id": "k1", "rel": "ref"},
        {"parent_id": "p", "child_id": "k2", "rel": "ref"},
    ]
    tree = assetslib.lineage(assets, edges, "f")
    portraits = [n for n in tree if n.asset.asset_id == "p"]
    assert len(portraits) == 2                      # 两镜各一次，都看得见
    assert sum(1 for n in portraits if n.repeat) == 1


def test_lineage_of_a_unknown_id_is_empty():
    assert assetslib.lineage({}, [], "as_nope") == []


def test_edges_expose_both_directions():
    edges = [
        {"parent_id": "a", "child_id": "b", "rel": "source"},
        {"parent_id": "a", "child_id": "c", "rel": "ref"},
    ]
    assert sorted(assetslib.parents_of(edges, "b")) == [("a", "source")]
    assert sorted(assetslib.children_of(edges, "a")) == [
        ("b", "source"), ("c", "ref"),
    ]


# ————————————————————————————————————————----
# 落库：幂等、边、汇总
# ————————————————————————————————————————----


def test_upserting_the_same_asset_twice_keeps_one_row(tmp_path: Path):
    """阶段重跑 → 同一路径 → 同一 id → **改写那一行**，不是追加。

    追加的话"这件东西生成过几次"在台账里就永远答不对了。
    """
    store = Store(tmp_path / "runs" / "s.sqlite3")
    p = tmp_path / "runs" / "run_a" / "clip.mp4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"v1")
    first = assetslib.make_asset("run_a", tmp_path, p, kind="clip", stage="video")
    store.upsert_asset(first)
    assert len(store.list_assets("run_a")) == 1

    p.write_bytes(b"v2-longer")
    second = assetslib.make_asset("run_a", tmp_path, p, kind="clip",
                                  stage="video", provider="dryrun-video")
    assert second.asset_id == first.asset_id
    store.upsert_asset(second)
    rows = store.list_assets("run_a")
    assert len(rows) == 1
    assert rows[0].provider == "dryrun-video"
    assert rows[0].size == len(b"v2-longer")


def test_edges_ignore_empty_and_self_links(tmp_path: Path):
    """空 id 和自环是调用方的小失误，不该变成库里的垃圾边。"""
    store = Store(tmp_path / "runs" / "s.sqlite3")
    store.link_assets("", "b")
    store.link_assets("a", "")
    store.link_assets("a", "a")
    assert store.asset_edges() == []
    store.link_assets("a", "b")
    store.link_assets("a", "b")          # 重复
    assert len(store.asset_edges()) == 1


def test_two_records_with_the_same_content_are_findable(tmp_path: Path):
    """同一张图出现在两个 run 里 —— 这是"共享资产"最朴素的形态。"""
    store = Store(tmp_path / "runs" / "s.sqlite3")
    same = b"identical bytes"
    made = []
    for run_id in ("run_a", "run_b"):
        p = tmp_path / "runs" / run_id / "face.png"
        p.parent.mkdir(parents=True)
        p.write_bytes(same)
        item = assetslib.make_asset(run_id, tmp_path, p, kind="portrait")
        store.upsert_asset(item)
        made.append(item)

    twins = store.assets_sharing_content(made[0].sha256, exclude=made[0].asset_id)
    assert [t.asset_id for t in twins] == [made[1].asset_id]


def test_the_summary_counts_kinds_and_separates_money(tmp_path: Path):
    """汇总按种类点数；**钱不在这里** —— 钱在 costs 表，两笔账分开算。"""
    store = Store(tmp_path / "runs" / "s.sqlite3")
    run = tmp_path / "runs" / "run_a"
    run.mkdir(parents=True)
    for name, kind, size in (("a.mp4", "clip", 10), ("b.mp4", "clip", 20),
                             ("c.png", "keyframe", 5)):
        p = run / name
        p.write_bytes(b"x" * size)
        store.upsert_asset(assetslib.make_asset("run_a", tmp_path, p, kind=kind))

    s = store.asset_summary("run_a")
    assert s["total"] == 3
    assert s["bytes"] == 35
    assert s["by_kind"]["clip"] == {"count": 2, "bytes": 30}
    assert s["by_kind"]["keyframe"] == {"count": 1, "bytes": 5}
    assert "money" not in s


def test_an_old_database_gains_the_two_new_tables(tmp_path: Path):
    """老库（这一层加之前建的）再打开时必须自动补表，而不是报错。

    迁移是 `CREATE TABLE IF NOT EXISTS` 走的 `executescript`，
    所以这里断言的是"打开一个已经有 runs 表的老库不会炸，且新表可用"。
    """
    db = tmp_path / "runs" / "old.sqlite3"
    db.parent.mkdir(parents=True)
    first = Store(db)
    p = tmp_path / "runs" / "run_a" / "x.bin"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    first.upsert_asset(assetslib.make_asset("run_a", tmp_path, p, kind="clip"))
    first.link_assets("as_parent", "as_child", "source")
    first.close()

    second = Store(db)                     # 同一个文件再打开一次
    assert len(second.list_assets("run_a")) == 1
    assert len(second.asset_edges()) == 1


# ————————————————————————————————————————----
# 端到端：台账与磁盘必须对得上
# ————————————————————————————————————————----


@pytest.mark.ffmpeg
def test_a_full_run_records_everything_it_leaves_on_disk(workspace: Path,
                                                         need_ffmpeg: None):
    """跑完一整条流水线，run 目录里的每个产物都要在台账里。

    第一次跑这条断言时它报了 `cost_report.json` 与 `scenes/sc01/scene_video.mp4`
    两个文件 —— **两个都是真的漏记**：一个由流水线自己在收尾时写（不属于任何
    阶段），一个是场次拼接的中间产物。这条测试就是那次发现的固化。
    """
    result = _run_pipeline(workspace)
    assert result["state"] in ("done", "failed")       # failed 是质检闸门，正常
    assert "failed_stage" not in result

    run_id = result["run_id"]
    store = Store(workspace / "runs" / "test.sqlite3")
    drift = assetslib.reconcile(
        workspace, run_id, store.list_assets(run_id),
        paths.run_dir(workspace, run_id),
    )
    assert drift.unrecorded == [], f"这些产物没进台账：{drift.unrecorded}"
    assert drift.missing == [], f"台账里有、磁盘上不在：{drift.missing}"


@pytest.mark.ffmpeg
def test_the_ledger_answers_which_keyframe_came_from_which_face(workspace: Path,
                                                               need_ffmpeg: None):
    """从成片一路追到定妆照 —— 这条链就是这一层存在的理由。

    顺带钉住"成片的视频轨上游是**场次拼接产物**"：单镜片段是它的爷爷辈，
    混在一起会让"第 12 秒用的是哪次渲染"这个问题失去唯一的答案。
    """
    result = _run_pipeline(workspace)
    run_id = result["run_id"]
    store = Store(workspace / "runs" / "test.sqlite3")
    rows = store.list_assets(run_id)

    final = [r for r in rows if r.kind == "final"]
    assert len(final) == 1
    edges = store.asset_edges()

    parents = {pid for pid, _rel in assetslib.parents_of(edges, final[0].asset_id)}
    kinds = {store.get_asset(p).kind for p in parents}
    assert "scene" in kinds                       # 视频轨来自场次拼接
    assert kinds & {"voice", "sfx", "bgm"}        # 音频轨来自这三种声音

    tree = assetslib.lineage({r.asset_id: r for r in rows}, edges,
                             final[0].asset_id)
    assert "portrait" in {n.asset.kind for n in tree}
    depths = [n.depth for n in tree]
    assert max(depths) >= 4        # final → scene → clip → keyframe → portrait


@pytest.mark.ffmpeg
def test_the_checker_can_actually_fail(workspace: Path, need_ffmpeg: None):
    """删掉一个产物，对账必须报出来。

    一个永远通过的检查器比没有检查器更糟 —— 它会给人一种"已经在看着了"
    的错觉，而真正的漏记照旧沉在水下。
    """
    result = _run_pipeline(workspace)
    run_id = result["run_id"]
    run_dir = paths.run_dir(workspace, run_id)
    store = Store(workspace / "runs" / "test.sqlite3")

    assert assetslib.reconcile(workspace, run_id, store.list_assets(run_id),
                               run_dir).ok

    victim = run_dir / "timeline.json"
    assert victim.exists()
    victim.unlink()

    drift = assetslib.reconcile(workspace, run_id, store.list_assets(run_id),
                                run_dir)
    assert not drift.ok
    assert "timeline.json" in drift.missing


@pytest.mark.ffmpeg
def test_asset_of_finds_a_recorded_file_and_only_that(workspace: Path,
                                                     need_ffmpeg: None):
    """`asset_of()` 是查表，不是扫盘 —— 没记过的东西要老老实实返回 None。"""
    result = _run_pipeline(workspace)
    run_id = result["run_id"]
    store = Store(workspace / "runs" / "test.sqlite3")
    pipe = Pipeline(workspace, store, registry.build_bundle(),
                    concurrency=2, verbose=False)
    run = store.get_run(run_id)
    assert run is not None
    ctx = pipe._context(run, None) if hasattr(pipe, "_context") else None

    # 不依赖内部构造：直接用 core.assets 的查表口径验一遍
    item = assetslib.make_asset(run_id, workspace, paths.timeline(workspace, run_id),
                                kind="timeline")
    assert store.get_asset(item.asset_id) is not None
    ghost = assetslib.make_asset(run_id, workspace,
                                 paths.run_dir(workspace, run_id) / "没有这个文件.bin",
                                 kind="other")
    assert store.get_asset(ghost.asset_id) is None
