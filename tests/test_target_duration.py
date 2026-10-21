"""整部时长目标：`--target 60` 时说人话，别让人自己数。

难点不在"相除"，在**两个方向都要交代清楚**：

* 均摊是**愿望**，台词是**硬下限** —— 一句 40 字的话读不完，不能为了
  凑 60 秒把它剪掉一半。所以实际片长允许超过目标，但必须写出差了多少。
* 目标 ÷ 镜数 可能小到不成立（24 镜摊 1.25 秒），那不是"算不出来"，
  是**用户要的东西自相矛盾**，报告里得说破，而不是默默给他 48 秒。
"""

from __future__ import annotations

import pytest

from ai_drama.core import media
from ai_drama.core.models import Episode, Scene, Shot
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry


def _pipe(workspace, **kw) -> Pipeline:
    store = Store(workspace / "runs" / "t.sqlite3")
    return Pipeline(workspace, store, registry.build_bundle(), verbose=False, **kw)


def _ep(*durations_and_dialogue, target=0.0) -> Episode:
    ep = Episode(run_id="run_x", idea="一句话")
    scene = Scene(scene_id="sc01", title="场")
    for i, (dur, line) in enumerate(durations_and_dialogue):
        scene.shots.append(Shot(scene_id="sc01", order=i + 1, action="走", 
                                dialogue=line, duration=dur))
    ep.scenes.append(scene)
    ep.target_seconds = target
    return ep


def test_without_a_target_the_default_shot_length_rules():
    """没定目标时行为**一点都不能变** —— 这是老用户的地盘。"""
    ep = _ep((3.0, ""), (3.0, ""))
    assert media.preferred_shot_seconds(ep) == media.default_shot_seconds()


def test_the_target_is_shared_out_evenly():
    ep = _ep(*[(3.0, "")] * 4, target=60.0)
    assert media.preferred_shot_seconds(ep) == pytest.approx(15.0)

    ep24 = _ep(*[(3.0, "")] * 24, target=60.0)
    assert media.preferred_shot_seconds(ep24) == pytest.approx(2.5)


def test_the_floor_is_a_line_not_a_division():
    """24 镜摊 1.25 秒是**不可能**的：画面比台词还短。要报出来，不能偷偷算。"""
    ep = _ep(*[(3.0, "")] * 24, target=30.0)
    assert media.preferred_shot_seconds(ep) == pytest.approx(1.25)
    # 但真去估算时，最短还是 2 秒 —— 公式自己会挡住
    shot = Shot(scene_id="sc01", order=1, action="走")
    got = media.estimate_shot_seconds(
        shot, preferred_s=media.preferred_shot_seconds(ep))
    # 挡住的不是 1.25 秒而是景别本身的节奏下限（中景 3 秒）——
    # 无论哪个先到，结论一样：这条镜头不会真的按 1.25 秒去拍
    assert got >= 2.0
    assert got > media.preferred_shot_seconds(ep)


def test_dialogue_still_beats_the_target():
    """台词是硬下限：目标 30 秒、两镜各 15，但其中一镜 90 字（≈20 秒）。"""
    ep = _ep((3.0, ""), (3.0, "这是一个很长很长的台词" * 8), target=30.0)
    preferred = media.preferred_shot_seconds(ep)
    assert preferred == pytest.approx(15.0)
    long_shot = ep.shots[1]
    got = media.estimate_shot_seconds(long_shot, preferred_s=preferred)
    assert got > 15.0, "台词读不完的镜头被目标压短了 —— 等于把话说一半"
    assert got >= len(long_shot.dialogue) / 4.5


def test_target_survives_the_json_round_trip():
    ep = _ep((15.0, ""), target=45.0)
    back = Episode.from_dict(ep.to_dict())
    assert back.target_seconds == pytest.approx(45.0)
    # 老 episode.json 没有这个字段 —— 不能炸
    old = ep.to_dict()
    del old["target_seconds"]
    assert Episode.from_dict(old).target_seconds == 0.0


def test_the_run_reads_the_target_and_the_report_says_the_gap(workspace):
    pipe = _pipe(workspace)
    # 2 场 × 2 镜 = 4 镜，目标 40 秒 → 每镜 10 秒（conftest 把默认片长压到 3，
    # 所以这里能证明它真的读了 target 而不是吃默认值）
    run = pipe.start("雨夜，一个女人在等一个不会来的人",
                     scenes=2, shots_per_scene=2, target_seconds=40.0)
    result = pipe.run(run, stages=["script", "storyboard"])
    ep = pipe.store.get_episode(run.run_id)
    assert ep.target_seconds == pytest.approx(40.0)
    assert len(ep.shots) == 4
    for shot in ep.shots:
        assert shot.duration == pytest.approx(10.0), shot.duration
    assert ep.total_duration == pytest.approx(40.0)

    notes = "\n".join(
        n for rep in result["reports"] for n in (rep.get("notes") or []))
    assert "目标 40s" in notes


def test_an_impossible_target_is_called_out_not_silently_rounded(workspace):
    """3 场 × 4 镜 = 12 镜，目标 12 秒 → 1 秒/镜。要明说做不到。"""
    pipe = _pipe(workspace)
    run = pipe.start("一句话", scenes=3, shots_per_scene=4, target_seconds=12.0)
    result = pipe.run(run, stages=["script", "storyboard"])
    notes = "\n".join(
        n for rep in result["reports"] for n in (rep.get("notes") or []))
    assert "低于 2 秒下限" in notes
    ep = pipe.store.get_episode(run.run_id)
    assert ep.total_duration > 12.0


def test_the_web_page_is_told_the_target(workspace):
    """页面要能把"目标/实际"并排显示 —— 只给总时长，用户没法判断差多少。"""
    from ai_drama.web import api

    pipe = _pipe(workspace)
    run = pipe.start("一句话", scenes=1, shots_per_scene=2, target_seconds=30.0)
    pipe.run(run, stages=["script", "storyboard"])
    ep = pipe.store.get_episode(run.run_id)
    sb = api.storyboard_payload(workspace, pipe.store, run.run_id, ep)
    assert sb["target_seconds"] == pytest.approx(30.0)
    assert sb["seconds"] == pytest.approx(30.0)


def test_the_cli_flag_exists_and_is_documented_in_its_help():
    from ai_drama.cli import build_parser

    parser = build_parser()
    # 找到 run 子命令的 parser
    sub = next(a for a in parser._actions if getattr(a, "choices", None))
    run_parser = sub.choices["run"]
    acts = {a.dest: a for a in run_parser._actions if a.dest == "target"}
    assert acts, "run 少了 --target"
    assert "%(default)s" not in (acts["target"].help or "")
    assert "整部目标" in acts["target"].help


def test_every_episode_field_is_carried_back_to_the_context():
    """`Episode` 加了字段却忘了写回 `ctx.episode`，是本项目踩过的真坑。

    target_seconds 第一版就漏在这里：stage01 把它算对了、存进 episode.json，
    但没写回 ctx，于是后面的 storyboard 用 0 重算了一遍每镜时长，
    片长又变成了"什么都不做"的默认值 —— 而且**没有任何报错**。

    所以这里拿 dataclass 的字段名对着 stage01 的回写表验一遍。新增字段
    必须选一个：要么加进 ADOPTED_FIELDS，要么加进 _NOT_ADOPTED 并说明原因。
    """
    import dataclasses

    from ai_drama.stages import stage01_script

    # 这三个是"这一次 run 的身份"，由 Pipeline 持有，不该被 stage01 改写
    not_adopted = {"run_id", "idea", "created_at"}
    fields = {f.name for f in dataclasses.fields(Episode)}
    missing = fields - set(stage01_script.ADOPTED_FIELDS) - not_adopted
    assert not missing, (
        f"这些 Episode 字段没有写回 ctx.episode：{sorted(missing)} —— "
        "下游会读到旧值，而且不会报错"
    )


def test_the_page_shows_target_and_actual_side_by_side(repo_root):
    """只显示"总时长 52.4s"，用户没法知道离目标差多少 —— 两句要并排。"""
    js = (repo_root / "ai_drama" / "web" / "static" / "app.js").read_text(
        encoding="utf-8")
    assert "sb.target_seconds > 0" in js
    assert "目标 ${fmtSec(sb.target_seconds)} · 实际 ${fmtSec(sb.seconds)}" in js
