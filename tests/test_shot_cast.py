"""出镜角色由分镜**显式给出**，而不是靠名字子串猜。

这一层要防的失败和 test_storyboard.py 是同一族，但更隐蔽：

    骨架从镜头文本里找角色名的子串，来判断"这一镜该拿谁的定妆照锁脸"。

猜错的后果不是报错，是**成片里出现一张陌生的脸**：

  * `朱锦` 会匹配到 `朱锦娘`（core/faces.py 里为此写了特判）；
  * 多角色同框时"谁的脸算基准"立刻失效 —— 而参考图上限通常只有 1 张；
  * 改个名字、写个外号、模型换一种措辞，匹配就静默失效。

所以现在由分镜阶段的模型显式输出 `shot.characters = [name, ...]`：
**锁脸跟着声明走，告警跟着并集走**。这两句是这一组的全部要点，
分开看都很自然，合起来才是"既锁得对、又不会因为模型少写就闭嘴"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_drama.core import paths
from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.dryrun import DryRunTextProvider
from ai_drama.stages.stage01_script import _names
from ai_drama.stages.stage02_storyboard import (
    characters_by_text,
    characters_in_shot,
    characters_on_screen,
    declared_characters,
)
from ai_drama.stages.stage03_frames import character_in_shot


def _shot(action: str, cast=None, **kw) -> Shot:
    """`cast=None` 表示**没声明**（characters=None），`cast=[]` 表示声明空镜。

    这个区分本身就是被测的东西，所以助手不能把两者都变成 `[]`。
    """
    return Shot(scene_id="sc01", order=1, action=action,
                characters=None if cast is None else list(cast), **kw)


def _ep(chars, shots=()) -> Episode:
    return Episode(run_id="r", idea="x", characters=list(chars),
                   scenes=[Scene(scene_id="sc01", title="t", summary="s",
                                 location="l", time_of_day="夜", shots=list(shots))])


def _char(name: str, ref: str = "ref.png") -> Character:
    return Character(name=name, desc=f"{name}的外貌", ref_image=ref)


# ————————————————————————————————————————---- 声明优先


def test_the_declared_cast_beats_whatever_the_text_says():
    """文本里出现了两个名字，但分镜说这一镜只有林晚 —— 以声明为准。

    "这张戏是谁的"应当由剧本决定，不由字符串查找决定。
    """
    ep = _ep([_char("林晚"), _char("陈默")])
    shot = _shot("林晚推开门，陈默抬头看她", cast=["林晚"])

    assert [c.name for c in characters_in_shot(ep, shot)] == ["林晚"]
    assert character_in_shot(ep, shot).name == "林晚"


def test_the_declaration_order_is_kept():
    """声明的顺序 = 谁更重要 = 谁的脸当基准。"""
    ep = _ep([_char("陈默"), _char("林晚")])
    assert [c.name for c in characters_in_shot(ep, _shot("对峙", cast=["林晚", "陈默"]))] \
        == ["林晚", "陈默"]
    assert [c.name for c in characters_in_shot(ep, _shot("对峙", cast=["陈默", "林晚"]))] \
        == ["陈默", "林晚"]


def test_an_empty_declaration_is_respected():
    """空数组 = 空镜。不许退回去猜 —— 那会让"空镜"这个声明失去意义。"""
    ep = _ep([_char("林晚")])
    shot = _shot("走廊空镜，林晚的房门紧闭", cast=[])
    # 猜的话会命中"林晚"（文本里有），但空镜就是空镜
    assert [c.name for c in characters_by_text(ep, shot)] == ["林晚"]
    assert characters_in_shot(ep, shot) == []
    assert character_in_shot(ep, shot) is None


def test_without_a_declaration_it_falls_back_to_the_name_guess():
    """旧 episode.json（没有 characters 字段）不能因此锁不住脸。"""
    ep = _ep([_char("林晚"), _char("陈默")])
    shot = _shot("林晚推开门，陈默抬头看她")          # 没有声明
    assert [c.name for c in characters_in_shot(ep, shot)] == ["林晚", "陈默"]


def test_an_unknown_name_is_reported_instead_of_silently_dropped():
    """名字写错一个字（苏曼 / 苏蔓），这一镜就锁不住脸，而它不会报错。"""
    ep = _ep([_char("苏蔓")])
    shot = _shot("苏曼站在雨里", cast=["苏曼"])
    found, unknown = declared_characters(ep, shot)
    assert found == []
    assert unknown == ["苏曼"]
    assert characters_in_shot(ep, shot) == []


# ————————————————————————————————————————---- 告警跟着并集


def test_crowding_is_computed_from_the_union_not_the_declaration():
    """模型少声明一个人，不能让"锁不住第二张脸"这条告警跟着消失。

    只用声明的话，这条告警就变成"只要模型不写就永远不报警"——
    比没有还糟，因为它训练人相信"没告警就是没问题"。
    """
    ep = _ep([_char("林晚"), _char("陈默")])
    shot = _shot("林晚与陈默对视", cast=["林晚"])      # 漏了陈默

    assert len(characters_in_shot(ep, shot)) == 1      # 锁脸只看声明
    assert len(characters_on_screen(ep, shot)) == 2    # 告警看并集
    assert {c.name for c in characters_on_screen(ep, shot)} == {"林晚", "陈默"}


def test_the_union_does_not_duplicate_the_declared_cast():
    ep = _ep([_char("林晚"), _char("陈默")])
    shot = _shot("林晚与陈默对视", cast=["林晚", "陈默"])
    assert [c.name for c in characters_on_screen(ep, shot)] == ["林晚", "陈默"]


# ————————————————————————————————————————---- 清洗模型给的字段


def test_the_names_field_survives_what_models_actually_return():
    """模型有时给逗号分隔的字符串，有时给 null，有时重复，有时给个 dict。"""
    assert _names(["苏蔓", "林晚"]) == ["苏蔓", "林晚"]
    assert _names("苏蔓，林晚、陈默") == ["苏蔓", "林晚", "陈默"]
    assert _names(["苏蔓", "苏蔓", " 苏蔓 "]) == ["苏蔓"]
    assert _names(None) == []
    assert _names("") == []
    assert _names([{"name": "苏蔓"}, 3, None, "林晚"]) == ["林晚"]
    assert _names(["「苏蔓」"]) == ["苏蔓"]


# ————————————————————————————————————————---- dryrun 要把完整契约演出来


def test_the_dryrun_text_provider_declares_the_cast_on_every_shot():
    """dryrun 是"零配置也能跑通"的参考实现，它得把字段填全。

    不填的后果不是报错：所有镜头都会退化成"靠名字猜"，于是报告里
    满屏"靠猜"的告警 —— 而真正该被注意的那一镜就淹没在里面了。
    """
    payload = DryRunTextProvider()._build("暴雨夜的外卖单", 1, 2, 4)
    shots = [s for sc in payload["scenes"] for s in sc["shots"]]
    assert shots
    for s in shots:
        assert "characters" in s, f"{s['order']} 镜没有 characters 字段"
        assert isinstance(s["characters"], list)
    # 至少有一个空镜（声明为空）和一个双人镜（声明了两个人）
    assert any(s["characters"] == [] for s in shots)
    assert any(len(s["characters"]) == 2 for s in shots)


@pytest.mark.ffmpeg
def test_an_old_run_without_the_field_still_locks_faces_and_says_it_guessed(
        workspace: Path, need_ffmpeg: None):
    """升级路径：`characters` 字段是后加的，已经跑过的 run 里没有它。

    那时候不能一个人都不锁（那会让所有旧 run 一旦 resume 就换脸），
    要退回名字子串猜 —— 但**必须说出来**，否则用户会把"能跑"当成"猜得对"。

    注意要清的是**库里那份 payload**：resume 时 `ctx.episode` 来自
    `store.get_episode()`（运行时的真相），episode.json 只是剧本层的可编辑副本，
    由 stage01 负责读回来。只改文件是测不到的。
    """
    store = Store(workspace / "runs" / "test.sqlite3")
    pipe = Pipeline(workspace, store, registry.build_bundle(),
                    concurrency=2, verbose=False)
    run = pipe.start("暴雨夜最后一个外卖单", scenes=1, shots_per_scene=4)
    pipe.run(run, stages=["script", "storyboard"])

    # —— 退回"这个字段还不存在的年代"
    ep = store.get_episode(run.run_id)
    for shot in ep.shots:
        shot.characters = None
    store.save_episode(ep)
    ep_json = paths.episode_json(workspace, run.run_id)
    ep_json.write_text(json.dumps(ep.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")

    result = pipe.resume(run.run_id, stages=["storyboard"])
    storyboard = next(r for r in result["reports"] if r["stage"] == "storyboard")
    notes = " ".join(storyboard["notes"])
    assert "靠名字子串猜" in notes, f"退回猜的路径没有报出来：{notes}"
    assert notes.count("[!]") >= 1

    # 而且脸还是锁上了（不能因为没声明就一个人都不锁）
    after = store.get_episode(run.run_id)
    locked = [s for s in after.shots if "@图片1" in (s.image_prompt or "")]
    assert locked, "旧 run 退回猜之后一个参考锁定段都没有 —— 等于每个镜头都换脸"


@pytest.mark.ffmpeg
def test_a_real_run_writes_the_cast_into_episode_json(workspace: Path,
                                                      need_ffmpeg: None):
    """字段要能一路走到 episode.json —— 手改它再 resume 是既定用法。"""
    store = Store(workspace / "runs" / "test.sqlite3")
    pipe = Pipeline(workspace, store, registry.build_bundle(),
                    concurrency=2, verbose=False)
    run = pipe.start("暴雨夜最后一个外卖单", scenes=1, shots_per_scene=4)
    # 只跑两个阶段：流水线结尾会因为"没有成片"把 state 记成 failed，
    # 那是既定行为（见 pipeline.py 的质检闸门），这里关心的是有没有阶段抛错。
    result = pipe.run(run, stages=["script", "storyboard"])
    assert "failed_stage" not in result, result

    ep = json.loads(paths.episode_json(workspace, run.run_id).read_text(encoding="utf-8"))
    names = {c["name"] for c in ep["characters"]}
    shots = [s for sc in ep["scenes"] for s in sc["shots"]]
    assert any(s["characters"] for s in shots), "没有任何镜头声明出镜角色"
    for s in shots:
        for n in s["characters"]:
            assert n in names, f"{s['order']} 镜声明了不在卡司里的 {n}"

    storyboard = next(r for r in result["reports"] if r["stage"] == "storyboard")
    notes = " ".join(storyboard["notes"])
    assert "由分镜显式给出" in notes
    assert "靠名字子串猜" not in notes, (
        "dryrun 已经声明了出镜角色，不该再退化成靠猜"
    )
