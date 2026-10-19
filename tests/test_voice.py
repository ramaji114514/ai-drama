"""音色推断：只看描述、说清楚凭什么、猜不出来就不猜。

这一组测试守的是用户真提过的那个问题 ——「苍老的男人却用的是稚气的声音」。
根因不是模型不会配音，是 `voice_map` 从来没人填，于是全剧共用一个默认嗓子，
而**没有任何一层会报出来**。
"""

from __future__ import annotations

from ai_drama.core import voice
from ai_drama.core.models import Character
from ai_drama.core.project import Project


# ———————————————————————————————————————————— 单纯看描述

def test_a_man_of_forty_two_does_not_get_a_child_voice():
    """用户报的原话。42 岁的男人必须是成年男声，不能是任何女声/童声。"""
    got = voice.suggest("42岁，男，沉默寡言，左眉骨有旧疤")
    assert got is not None
    assert got.voice == "male_02"
    assert voice.PALETTE[got.voice] == ("male", "adult")
    assert "42岁" in got.why          # 凭什么，要能看见


def test_age_comes_from_the_english_anchor_prompt_when_the_desc_is_silent():
    """`desc` 是中文短句、`anchor_prompt` 是英文长句，两边互补。"""
    got = voice.suggest("摆渡人，沉默寡言", "Chinese man, 42 years old, weathered face")
    assert got is not None
    assert got.voice == "male_02"
    assert "42 years old" in got.why


def test_gender_can_come_from_the_english_anchor_prompt():
    got = voice.suggest("调查记者，习惯用沉默观察别人", "a young woman, short hair")
    assert got is not None
    assert voice.PALETTE[got.voice] == ("female", "young")


def test_an_old_man_gets_an_old_voice():
    got = voice.suggest("男，年迈，白发，饱经风霜")
    assert got is not None
    assert got.voice == "male_03"
    assert voice.PALETTE[got.voice] == ("male", "old")


def test_a_teenager_gets_a_young_voice():
    got = voice.suggest("17岁，男，瘦削清秀")
    assert got is not None
    assert got.voice == "male_01"


def test_a_word_containing_an_age_hint_does_not_make_a_wife_old():
    """「老婆」里有「老」，但它不是年龄线索 —— 这是这条规则最容易写错的地方。"""
    got = voice.suggest("女，老婆，温柔")
    assert got is not None
    assert got.voice == "female_02"


def test_no_gender_means_no_guess():
    """看不出性别就不猜。猜错比不猜更糟：错的嗓子会一路跟到成片。"""
    assert voice.suggest("沉默寡言的人，袖口总沾着机油") is None


def test_both_genders_mentioned_is_not_guessed():
    """同时出现两性线索（「男人，但身量像个女孩」）就不敢定。"""
    assert voice.suggest("男人，但身量像个女孩") is None


def test_a_chinese_numeral_age_is_read():
    """中文描述常写「六十岁」而不是「60岁」，不认就得白读一个老人。"""
    got = voice.suggest("男，六十岁，在码头守了四十年")
    assert got is not None
    assert got.voice == "male_03"
    assert "六十岁" in got.why


def test_an_empty_description_is_not_guessed():
    assert voice.suggest() is None


def test_the_reason_for_not_guessing_is_actionable():
    """定不下来时得说清楚缺哪条线索，人才能去补。"""
    assert "性别" in voice.unresolved_reason("沉默寡言的人")
    assert "空的" in voice.unresolved_reason()


def test_the_palette_covers_both_genders_and_three_bands():
    """六个通用音色 = 两性 × 三段年龄。少一格就会有角色落不进来。"""
    combos = set(voice.PALETTE.values())
    assert combos == {
        (sex, band)
        for sex in ("female", "male")
        for band in ("young", "adult", "old")
    }


# ———————————————————————————————————————————— 落到项目圣经

def _cast(*chars: Character) -> list:
    return list(chars)


def test_the_project_fills_voices_once_and_remembers_why():
    proj = Project(project_id="proj_test")
    cast = _cast(
        Character(name="陈默", char_id="char_a", desc="42岁，男，沉默寡言"),
        Character(name="少年阿屿", char_id="char_b", desc="14岁，男，瘦小苍白"),
    )
    filled, unresolved = proj.ensure_voices(cast)
    assert [name for name, _, _ in filled] == ["陈默", "少年阿屿"]
    assert proj.voice_map == {"char_a": "male_02", "char_b": "male_01"}
    assert proj.voice_notes["char_a"].startswith("男声")
    assert unresolved == []
    # 第二次什么都不做（已经定了，不会每集重摇一次骰子）
    again, _ = proj.ensure_voices(cast)
    assert again == []


def test_a_hand_written_voice_is_never_overwritten():
    """人手填的值（哪怕看起来不对）一律不动 —— 那是人的决定。"""
    proj = Project(project_id="proj_test")
    proj.voice_map["char_a"] = "male_03"
    filled, _ = proj.ensure_voices(_cast(Character(name="陈默", char_id="char_a", desc="42岁，男")))
    assert filled == []
    assert proj.voice_map["char_a"] == "male_03"


def test_a_voice_stored_under_the_name_also_counts():
    """老项目文件按名字存的表，也得认 —— 否则会给他换一个嗓子。"""
    proj = Project(project_id="proj_test")
    proj.voice_map["陈默"] = "male_01"
    filled, _ = proj.ensure_voices(_cast(Character(name="陈默", char_id="char_a", desc="42岁，男")))
    assert filled == []
    assert proj.voice_for(Character(name="陈默", char_id="char_a")) == "male_01"


def test_a_character_without_a_gender_is_reported_not_defaulted():
    """定不出来就报出来，**不硬塞一个默认值**进去。"""
    proj = Project(project_id="proj_test")
    filled, unresolved = proj.ensure_voices(
        _cast(Character(name="无名氏", char_id="char_x", desc="袖口总沾着机油"))
    )
    assert filled == []
    assert unresolved and unresolved[0][0] == "无名氏"
    assert "性别" in unresolved[0][1]
    assert proj.voice_map == {}


def test_the_notes_survive_a_round_trip():
    """写进文件、读回来，依据还在 —— 不然下一集人就没法查它凭什么。"""
    proj = Project(project_id="proj_test")
    proj.ensure_voices(_cast(Character(name="陈默", char_id="char_a", desc="42岁，男")))
    back = Project.from_dict(proj.to_dict())
    assert back.voice_map == proj.voice_map
    assert back.voice_notes == proj.voice_notes


def test_an_old_project_file_without_notes_still_loads():
    """`voice_notes` 是新加的字段，老文件没有它，读进来不能炸。"""
    proj = Project.from_dict(
        {
            "project_id": "proj_old",
            "characters": [{"name": "陈默", "char_id": "char_a"}],
            "voice_map": {"char_a": "male_02"},
        }
    )
    assert proj.voice_notes == {}
    assert proj.voice_for(Character(name="陈默", char_id="char_a")) == "male_02"
