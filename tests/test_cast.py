"""角色稳定 id：让"这一镜里是谁"不再由名字决定。

这一组守的是三种**不会报错的错配**，它们此前的表现都是"成片里出现一张
陌生的脸"，而报告里一切正常：

  1. **同名不同人** —— 两个角色都叫「林晚」，按名字查表只留得下一个。
     谁的脸被锁上，取决于字典的写法。这一组要求：**认不出来就说认不出来**，
     宁可这一镜没有参考图，也不要给错脸。
  2. **改名** —— 第 5 集把「苏蔓」改成「苏晚」。按名字匹配时，前面引用
     "苏蔓"的镜头会一起失联。有了 id 之后，引用的是身份，不是叫法。
  3. **多重身份** —— 卧底真名「陈默」、化名「林晚」。故事里两个名字都出现，
     而"这一镜是谁"必须有一个答案。

最要紧的一条是 `test_a_rename_does_not_break_earlier_shots`：
它同时钉住"改名之后旧名字还认得出"和"锁的是同一张定妆照"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from ai_drama.core import cast, paths, project as projlib
from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.dryrun import DryRunT2IProvider
from ai_drama.stages.stage02_storyboard import (
    characters_by_text,
    characters_in_shot,
    resolve_declared,
)


def _char(name: str, *, cid: str = "", aliases=(), desc: str = "") -> Character:
    return Character(name=name, char_id=cid, aliases=list(aliases),
                     desc=desc or f"{name}的外貌", ref_image=f"{name}.png")


def _shot(action: str, refs=None) -> Shot:
    return Shot(scene_id="sc01", order=1, action=action,
                characters=None if refs is None else list(refs))


def _ep(chars, shots=()) -> Episode:
    return Episode(run_id="r", idea="x", characters=list(chars),
                   scenes=[Scene(scene_id="sc01", title="t", summary="s",
                                 location="l", time_of_day="夜", shots=list(shots))])


def _pipe(root: Path, project_id: Optional[str] = None) -> tuple[Pipeline, Store]:
    store = Store(root / "runs" / "test.sqlite3")
    return Pipeline(root, store, registry.build_bundle(), concurrency=2,
                    verbose=False, project_id=project_id), store


# ————————————————————————————————————————----
# id 本身：可推导、幂等、不撞车
# ————————————————————————————————————————----


def test_an_id_is_derived_from_the_name_not_drawn_at_random():
    """同名的角色必须每次都拿到同一个 id。

    老 `project.json` 里没有 id，要在读取时补。如果补出来的是随机值，
    同一个文件被读两次就得到两套 id —— 而引用它们的镜头在两个进程之间
    会对不上。可推导是这里的全部意义。
    """
    assert cast.mint_id("苏蔓") == cast.mint_id("苏蔓")
    assert cast.mint_id("苏蔓") != cast.mint_id("苏晚")
    assert cast.mint_id("苏蔓").startswith(cast.ID_PREFIX)


def test_two_people_with_the_same_name_get_two_ids():
    """同名不同人：他们必须是**两个身份**，而不是共享一个。"""
    a = cast.mint_id("林晚")
    b = cast.mint_id("林晚", taken=[a])
    assert a != b
    assert b.startswith(a)          # 同源，但加了后缀区分


def test_filling_in_ids_is_idempotent():
    chars = [_char("苏蔓"), _char("林晚", cid="char_keepme")]
    assert cast.ensure_ids(chars) == 1              # 只补了一个
    assert chars[1].char_id == "char_keepme"        # 已有的不动
    first = chars[0].char_id
    assert cast.ensure_ids(chars) == 0              # 再来一次什么都不做
    assert chars[0].char_id == first


def test_an_old_episode_json_without_ids_still_works():
    """向后兼容：旧 episode.json 里只有名字。"""
    ep = _ep([_char("苏蔓"), _char("林晚")])
    cast.ensure_ids(ep.characters)
    shot = _shot("苏蔓站在雨里", refs=["苏蔓"])
    assert [c.name for c in characters_in_shot(ep, shot)] == ["苏蔓"]


# ————————————————————————————————————————----
# 解析：三种写法，和"认不出来"
# ————————————————————————————————————————----


def test_id_name_and_alias_all_point_at_the_same_person():
    me = _char("陈默", cid="char_chenmo", aliases=["林晚"])
    r_id = cast.resolve("char_chenmo", [me])
    r_name = cast.resolve("陈默", [me])
    r_alias = cast.resolve("林晚", [me])
    assert r_id.how == cast.HOW_ID
    assert r_name.how == cast.HOW_NAME
    assert r_alias.how == cast.HOW_ALIAS
    assert r_id.character is r_name.character is r_alias.character is me


def test_a_name_that_fits_two_people_is_not_guessed():
    """两个人都叫「林晚」，只写名字时**不猜**。

    给错脸比不给脸难发现得多：不给脸至少参考图是空的，报告里看得见。
    """
    a = _char("林晚", cid="char_1", desc="短发")
    b = _char("林晚", cid="char_2", desc="长发")
    r = cast.resolve("林晚", [a, b])
    assert r.how == cast.HOW_AMBIGUOUS
    assert r.character is None
    assert len(r.others) == 2
    # 但 id 是明确的
    assert cast.resolve("char_2", [a, b]).character is b


def test_an_alias_that_fits_two_people_is_also_ambiguous():
    """化名与真名撞车时同样不猜 —— 卧底戏里这是常态，不是边角情况。"""
    spy = _char("陈默", cid="char_1", aliases=["林晚"])
    other = _char("林晚", cid="char_2")
    assert cast.resolve("林晚", [spy, other]).how == cast.HOW_AMBIGUOUS
    assert cast.resolve("char_1", [spy, other]).character is spy


def test_case_only_differences_are_the_same_person():
    """`LIN WAN` 与 `Lin Wan` 是一个人的两种写法，不是两个人。"""
    me = _char("LIN WAN", cid="char_1")
    assert cast.resolve("Lin Wan", [me]).character is me


def test_the_stats_say_how_the_reference_was_understood():
    me = _char("陈默", cid="char_1", aliases=["林晚"])
    found, stats, problems = cast.resolve_all(
        ["char_1", "陈默", "林晚", "苏曼"], [me]
    )
    assert [c.name for c in found] == ["陈默"]       # 同一个人只算一次
    assert stats[cast.HOW_ID] == 1
    assert stats[cast.HOW_NAME] == 1
    assert stats[cast.HOW_ALIAS] == 1
    assert stats[cast.HOW_UNKNOWN] == 1
    assert len(problems) == 1 and problems[0].how == cast.HOW_UNKNOWN
    assert "苏曼" in problems[0].detail


def test_the_prompt_table_hands_out_the_ids():
    """提示词里的角色表：模型不需要理解 id，只需要逐字照抄。"""
    table = cast.id_table([_char("苏蔓", cid="char_a1", aliases=["蔓姐"], desc="女，31岁")])
    assert "char_a1 = 苏蔓" in table
    assert "蔓姐" in table
    assert "逐字照抄" in table


# ————————————————————————————————————————----
# 分镜层：认不出来要说出来
# ————————————————————————————————————————----


def test_the_stage_reports_a_clashing_name_instead_of_picking_one():
    a = _char("林晚", cid="char_1")
    b = _char("林晚", cid="char_2")
    ep = _ep([a, b])
    shot = _shot("林晚推门进来", refs=["林晚"])
    found, unknown, ambiguous = resolve_declared(ep, shot)
    assert found == []                       # 不给脸
    assert unknown == []
    assert ambiguous == ["林晚"]             # 但要说出来
    assert characters_in_shot(ep, shot) == []


def test_a_declared_id_locks_the_face_even_with_a_clashing_name():
    a = _char("林晚", cid="char_1")
    b = _char("林晚", cid="char_2")
    ep = _ep([a, b])
    shot = _shot("林晚推门进来", refs=["char_2"])
    assert [c.char_id for c in characters_in_shot(ep, shot)] == ["char_2"]


def test_guessing_by_text_also_knows_aliases():
    """退路（没声明时按文本猜）也要认得别名，否则卧底戏整个漏掉。"""
    me = _char("陈默", cid="char_1", aliases=["林晚"])
    ep = _ep([me])
    assert [c.name for c in characters_by_text(ep, _shot("林晚低头点烟"))] == ["陈默"]


def test_the_character_round_trips_through_json():
    me = _char("陈默", cid="char_1", aliases=["林晚", "阿默"])
    back = Character.from_dict(json.loads(json.dumps(me.to_dict(), ensure_ascii=False)))
    assert back.char_id == "char_1"
    assert back.aliases == ["林晚", "阿默"]
    assert back.labels() == ["陈默", "林晚", "阿默"]


def test_a_hand_edited_alias_written_as_one_line_still_reads():
    """手改项目文件时把 aliases 写成字符串，不该让整个项目读不出来。"""
    back = Character.from_dict({"name": "陈默", "aliases": "林晚"})
    assert back.aliases == ["林晚"]


# ————————————————————————————————————————----
# 圣经合并：id 定身份
# ————————————————————————————————————————----


def test_merge_matches_by_id_so_a_new_name_becomes_an_alias():
    """模型照抄了 id、但换了名字 -> 身份不变，新名字记成别名。

    这正是"改名"在数据上的样子：**id 不动，名字只是当下的叫法**。
    """
    proj = projlib.new_project("深夜外卖")
    proj.characters.append(_char("苏蔓", cid="char_sm"))
    incoming = [Character(name="苏晚", char_id="char_sm", desc="模型重写的")]
    merged, stats = proj.merge_cast(incoming)
    assert [c.name for c in merged] == ["苏蔓"]            # 圣经的名字为准
    assert merged[0].char_id == "char_sm"
    assert "苏晚" in merged[0].aliases
    assert stats["renamed"] == 1
    assert stats["new"] == 0                              # 没有变成第 2 个人
    # 别名写回了圣经，下一集按旧名字也认得出
    assert "苏晚" in proj.characters[0].aliases


def test_merge_matches_an_old_name_after_the_bible_was_renamed():
    """制片在第 3 集把「苏蔓」改名成「苏晚」，模型还写着旧名字。"""
    proj = projlib.new_project("深夜外卖")
    proj.characters.append(_char("苏晚", cid="char_sm", aliases=["苏蔓"]))
    merged, stats = proj.merge_cast([Character(name="苏蔓", desc="旧名字")])
    assert [c.name for c in merged] == ["苏晚"]
    assert stats["new"] == 0


def test_two_people_with_the_same_name_stay_two_people_in_the_bible():
    proj = projlib.new_project("替身")
    proj.characters.append(_char("林晚", cid="char_1", desc="短发"))
    proj.characters.append(_char("林晚", cid="char_2", desc="长发"))
    merged, stats = proj.merge_cast([])
    assert [c.char_id for c in merged] == ["char_1", "char_2"]
    assert stats["locked"] == 2


def test_a_brand_new_character_gets_an_id_on_the_way_in():
    proj = projlib.new_project("深夜外卖")
    merged, stats = proj.merge_cast([Character(name="陈默", desc="刚登场")])
    assert stats["new"] == 1
    assert merged[0].char_id.startswith(cast.ID_PREFIX)
    assert proj.characters[0].char_id == merged[0].char_id      # 圣经里也发了 id


def test_voice_follows_the_identity_not_the_name():
    """音色挂在身份上：改了名字不该换嗓子。"""
    proj = projlib.new_project("深夜外卖")
    proj.characters.append(_char("苏蔓", cid="char_sm", aliases=["蔓姐"]))
    proj.voice_map["char_sm"] = "female_07"
    assert proj.voice_for("苏蔓") == "female_07"
    assert proj.voice_for("蔓姐") == "female_07"
    assert proj.voice_for(proj.characters[0]) == "female_07"
    assert proj.voice_for("不认识的人", "female_01") == "female_01"


def test_a_voice_map_keyed_by_name_still_works():
    """老项目文件的音色表是按名字存的，不能因为没填 id 就换嗓子。"""
    proj = projlib.new_project("深夜外卖")
    proj.characters.append(_char("苏蔓", cid="char_sm"))
    proj.voice_map["苏蔓"] = "female_07"
    assert proj.voice_for("苏蔓") == "female_07"


@pytest.mark.ffmpeg
def test_a_real_run_reports_a_clashing_declaration(workspace: Path,
                                                   need_ffmpeg: None):
    """端到端：两个角色同名时，报告里必须有一句"不猜"。

    单元测试已经钉住了 `resolve_declared` 的行为；这一条钉的是**用户看得见的
    那一行** —— 一个只在函数返回值里成立、却从不写进报告的保护措施，等于没有。
    """
    proj = projlib.new_project("替身")
    proj.characters.append(_char("苏晚", cid="char_1", desc="短发"))
    proj.characters.append(_char("苏晚", cid="char_2", desc="长发"))
    proj.save(workspace)

    pipe, store = _pipe(workspace, proj.project_id)
    pipe.bundle.t2i = DryRunT2IProvider()
    run = pipe.start("苏晚推开门，屋里站着另一个苏晚", scenes=1, shots_per_scene=3)
    assert "failed_stage" not in pipe.run(run, stages=["script", "storyboard"])

    # 让某一个镜头**声明**那个有歧义的名字（模型真会这么干）
    ep = store.get_episode(run.run_id)
    assert len([c for c in ep.characters if c.name == "苏晚"]) == 2
    ep.shots[0].characters = ["苏晚"]
    ep.shots[0].image_prompt = ""          # 逼 stage02 重算这一镜
    store.save_episode(ep)

    result = pipe.resume(run.run_id, stages=["storyboard"])
    notes = next(r for r in result["reports"] if r["stage"] == "storyboard")["notes"]
    text = " ".join(notes)
    assert "对上了多个角色" in text, f"同名冲突没有报出来：{notes}"
    assert "不猜" in text
    # 有歧义的那一镜**不给脸**：宁可没有参考图，也不要给错脸
    after = store.get_episode(run.run_id)
    assert characters_in_shot(after, after.shots[0]) == []


# ————————————————————————————————————————----
# 端到端：改名不打断前几集
# ————————————————————————————————————————----


class _CountingT2I(DryRunT2IProvider):
    """数一数这一集真正生成了几张定妆照（文件名）。"""

    name = "counting-t2i"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def png(self, prompt: str, dest: Path, **kw):
        self.calls.append(Path(dest).name)
        return super().png(prompt, dest, **kw)


@pytest.mark.ffmpeg
def test_renaming_does_not_regenerate_the_portrait(workspace: Path,
                                                   need_ffmpeg: None):
    """改名之后第 2 集**不许重新生成定妆照**。

    这正是这一层要修的那类错：定妆照的文件名曾经是按**名字**推出来的
    （`portraits/林晚.png`），于是把角色改名成「苏晚」之后，第 2 集按新名字
    找不到文件、**重新生成一张脸** —— 而报告里还写着"沿用项目里的"。
    圣经里的 `ref_image` 才是权威：文件叫什么名字无关紧要。
    """
    proj = projlib.new_project("深夜外卖")
    proj.characters.append(_char("林晚", cid="char_lw"))
    proj.save(workspace)

    t2i = _CountingT2I()
    pipe, store = _pipe(workspace, proj.project_id)
    pipe.bundle.t2i = t2i
    run1 = pipe.start("暴雨夜，林晚把最后一单送上楼", scenes=1, shots_per_scene=2)
    assert "failed_stage" not in pipe.run(run1, stages=["script", "storyboard"])

    bible = projlib.load(workspace, proj.project_id)
    assert bible is not None
    face1 = bible.characters[0].ref_image
    assert face1 and Path(face1).exists()

    # 改名（旧名字留在别名里 —— 这就是改名该有的写法）
    old_name = bible.characters[0].name
    bible.characters[0].aliases.append(old_name)
    bible.characters[0].name = "苏晚"
    bible.save(workspace)

    marks = len(t2i.calls)
    run2 = pipe.start("第二天，苏晚发现那个地址是她自己家", scenes=1, shots_per_scene=2)
    assert "failed_stage" not in pipe.run(run2, stages=["script", "storyboard"])

    ep2 = store.get_episode(run2.run_id)
    c = ep2.character_by_ref("苏晚")
    assert c is not None
    assert c.ref_image == face1                    # 还是同一张文件
    assert Path(c.ref_image).exists()
    # 这一集没有为她重新生成任何一张定妆照
    assert old_name not in t2i.calls[marks:]
    assert "苏晚.png" not in t2i.calls[marks:]



def test_a_rename_does_not_break_earlier_shots(workspace: Path):
    """一整条：第 1 集建册 -> 改名 -> 第 2 集引用旧名字仍然锁到同一张脸。"""
    proj = projlib.new_project("深夜外卖")
    proj.characters.append(_char("苏蔓", cid="char_sm"))
    proj.save(workspace)

    # 第 1 集：生成定妆照并记进圣经
    t2i = DryRunT2IProvider()
    pipe, _store = _pipe(workspace, proj.project_id)
    pipe.bundle.t2i = t2i
    r1 = pipe.run(pipe.start("暴雨夜，苏蔓把最后一单送上楼", scenes=1, shots_per_scene=2),
                  stages=["script", "storyboard"])
    assert "failed_stage" not in r1

    bible = projlib.load(workspace, proj.project_id)
    assert bible is not None
    face1 = bible.characters[0].ref_image
    assert face1 and Path(face1).exists()

    # 制片改名：圣经里改名字，旧名字留在别名里（这就是"改名"该有的样子）
    bible.characters[0].aliases.append(bible.characters[0].name)
    bible.characters[0].name = "苏晚"
    bible.save(workspace)

    # 第 2 集：一个镜头引用旧名字，一个引用 id —— 都该锁到同一张脸
    ep = _ep([Character(name="苏晚", char_id="char_sm", aliases=["苏蔓"],
                        ref_image=face1)])
    shot_old = _shot("苏蔓站在雨里", refs=["苏蔓"])
    shot_id = _shot("苏晚抬头", refs=["char_sm"])
    assert [c.name for c in characters_in_shot(ep, shot_old)] == ["苏晚"]
    assert [c.name for c in characters_in_shot(ep, shot_id)] == ["苏晚"]
    assert characters_in_shot(ep, shot_old)[0].ref_image == face1


def test_the_next_episode_reuses_the_same_id_across_a_real_run(workspace: Path):
    """跨集：第 2 集的角色表里，苏蔓还是同一个 char_id 和同一张图。"""
    proj = projlib.new_project("深夜外卖")
    proj.characters.append(_char("苏蔓", cid="char_sm"))
    proj.save(workspace)

    pipe, store = _pipe(workspace, proj.project_id)
    pipe.bundle.t2i = DryRunT2IProvider()
    run1 = pipe.start("暴雨夜，苏蔓把最后一单送上楼", scenes=1, shots_per_scene=2)
    r1 = pipe.run(run1, stages=["script", "storyboard"])
    assert "failed_stage" not in r1
    first = store.get_episode(run1.run_id)
    assert first.character_by_ref("苏蔓") is not None
    assert first.character_by_ref("苏蔓").char_id == "char_sm"

    run2 = pipe.start("第二天，苏蔓发现订单地址是她自己家", scenes=1, shots_per_scene=2)
    r2 = pipe.run(run2, stages=["script", "storyboard"])
    assert "failed_stage" not in r2
    second = store.get_episode(run2.run_id)
    a = first.character_by_ref("苏蔓")
    b = second.character_by_ref("苏蔓")
    assert a.char_id == b.char_id == "char_sm"
    assert a.ref_image == b.ref_image          # 同一张定妆照文件
    assert Path(b.ref_image).exists()
