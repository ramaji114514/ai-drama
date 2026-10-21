"""一致性救命包：参考图给谁、拆镜方案。

这些断言钉的是**取舍**，不是文案：
  * 台词是谁说的 → 那张参考图先给谁；
  * 出镜次数多的优先于次数少的；
  * 一镜两个人时，第三段必须"不用参考图"（否则这个方案没解决问题）；
  * 只用文字描述的那个人，提示词里必须写明"不许套用 @图片1 的长相"。
"""

from __future__ import annotations

from ai_drama.core import consistency
from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.stages import stage02_storyboard as sb


def _ep(chars, shots):
    return Episode(run_id="r", idea="x", style="冷青", characters=list(chars),
                   scenes=[Scene(scene_id="sc01", title="t", shots=list(shots))])


def _c(name, char_id, ref=False):
    return Character(name=name, desc=f"{name}的外貌", char_id=char_id,
                     anchor_prompt=f"{name}的定妆照",
                     ref_image=f"character_{name}.png" if ref else None)


def _shot(*, chars=(), dialogue="", action="她推门进来", end="她站定", framing="close"):
    declared = None if chars is None else (list(chars) or None)
    return Shot(shot_id="s1", scene_id="sc01", order=0, action=action,
                dialogue=dialogue, end_action=end, framing=framing,
                characters=declared)


def test_the_speaker_gets_the_single_reference_slot():
    """林溪说话、灰灰在画里 —— 参考图给林溪，哪怕灰灰在数组里排第一。"""
    lin = _c("林溪", "c1", ref=True)
    cat = _c("灰灰", "c2", ref=True)
    shot = _shot(chars=["灰灰", "林溪"], dialogue="林溪：（低声）谁？")
    ep = _ep([lin, cat], [shot])
    picks = consistency.pick_reference(ep, shot, [cat, lin], max_refs=1)
    assert picks.locked_names == ["林溪"]
    assert picks.unlocked_names == ["灰灰"]


def test_appearance_count_breaks_a_tie_when_nobody_speaks():
    """都没台词时，全片出镜多的那个更值得锁脸。"""
    a = _c("甲", "a", ref=True)
    b = _c("乙", "b", ref=True)
    s0 = _shot(chars=["乙"])
    s1 = _shot(action="乙回头", chars=["乙"])
    s2 = _shot(action="甲抬头", chars=["甲", "乙"])
    ep = _ep([a, b], [s0, s1, s2])
    picks = consistency.pick_reference(ep, s2, [a, b], max_refs=1)
    assert picks.locked_names == ["乙"]


def test_declared_order_is_the_last_tiebreaker_and_nothing_crashes():
    a = _c("甲", "a", ref=True)
    b = _c("乙", "b", ref=True)
    shot = _shot(chars=["乙", "甲"])
    ep = _ep([a, b], [shot])
    assert consistency.rank(ep, shot, [b, a]) == [b, a]


def test_two_reference_slots_means_nobody_is_left_out():
    a = _c("甲", "a", ref=True)
    b = _c("乙", "b", ref=True)
    shot = _shot(chars=["甲", "乙"])
    ep = _ep([a, b], [shot])
    picks = consistency.pick_reference(ep, shot, [a, b], max_refs=2)
    assert picks.unlocked == []
    assert "够用" in picks.reason


def test_an_empty_shot_says_it_needs_no_reference_at_all():
    shot = _shot(chars=[])
    ep = _ep([], [shot])
    picks = consistency.pick_reference(ep, shot, [], max_refs=1)
    assert picks.locked == [] and picks.unlocked == []
    assert "空镜" in picks.reason


def test_the_plan_is_single_closeup_then_reverse_then_a_wide_two_shot():
    a = _c("林溪", "c1", ref=True)
    b = _c("灰灰", "c2", ref=True)
    shot = _shot(chars=["林溪", "灰灰"], dialogue="林溪：谁？")
    ep = _ep([a, b], [shot])
    plan = consistency.split_plan(ep, shot, [a, b], max_refs=1)
    assert [seg.framing for seg in plan] == ["close", "close", "wide"]
    assert plan[0].characters == ["林溪"]
    assert plan[1].characters == ["灰灰"]
    # 第三段如果还要参考图，这个方案就没解决任何问题
    assert plan[2].needs_reference is False
    assert set(plan[2].characters) == {"林溪", "灰灰"}


def test_a_shot_that_fits_does_not_get_a_split_plan():
    a = _c("林溪", "c1", ref=True)
    shot = _shot(chars=["林溪"])
    ep = _ep([a], [shot])
    assert consistency.split_plan(ep, shot, [a], max_refs=1) == []
    assert "不需要拆" in consistency.plan_text([])


def test_the_plan_truncates_an_over_long_action_with_an_ellipsis():
    a = _c("甲", "a", ref=True)
    b = _c("乙", "b", ref=True)
    # 故意不带标点：小句截断够不着，才会走到"整句太长"那条分支
    long = "雨滴顺着玻璃滑落灰猫端坐在窗台正中绿眼凝视着窗外尾巴轻轻卷住前爪"
    shot = _shot(chars=["甲", "乙"], action=long)
    ep = _ep([a, b], [shot])
    plan = consistency.split_plan(ep, shot, [a, b], max_refs=1)
    # 截断要带省略号：不带的话读起来像程序把词切坏了
    assert plan[0].action.endswith("…")
    assert len(plan[0].action) <= 25


def test_explain_names_who_is_locked_who_is_not_and_where_to_go_next():
    a = _c("林溪", "c1", ref=True)
    b = _c("灰灰", "c2", ref=True)
    shot = _shot(chars=["林溪", "灰灰"], dialogue="林溪：谁？")
    ep = _ep([a, b], [shot])
    text = "\n".join(consistency.explain(ep, shot, [a, b], max_refs=1))
    assert "锁脸 林溪" in text and "台词" in text
    assert "锁不住：灰灰" in text
    assert "run.py split" in text


def test_appearance_counts_reads_declared_characters_only():
    a = _c("甲", "a", ref=True)
    s0 = _shot(chars=["甲"])
    s1 = _shot(chars=None)          # 没声明 → 不猜
    ep = _ep([a], [s0, s1])
    # 按稳定身份数，不按名字数：分镜写"甲"、身份是 "a"
    assert consistency.appearance_counts(ep) == {"a": 1}


# —————————————————————————————————————————— 与 stage02 接线


def test_reference_labels_and_the_unlocked_block_agree():
    """点名 @图片1 的人，和「无参考图」那段里的人，必须是不相交的两拨。"""
    lin = _c("林溪", "c1", ref=True)
    cat = _c("灰灰", "c2", ref=True)
    shot = _shot(chars=["灰灰", "林溪"], dialogue="林溪：谁？")
    ep = _ep([lin, cat], [shot])
    labels = sb.reference_labels(ep, shot, max_refs=1)
    unlocked = sb.unlocked_names(ep, shot, max_refs=1)
    assert labels == [("角色", "林溪")]
    assert unlocked == ["灰灰"]
    assert not set(n for _, n in labels) & set(unlocked)


def test_a_character_without_a_portrait_still_gets_the_text_only_note():
    """没有定妆照的人最需要那句"按文字描述生成，别套 @图片1 的脸"。"""
    lin = _c("林溪", "c1", ref=True)
    ghost = _c("阿屿", "c2", ref=False)
    shot = _shot(chars=["林溪", "阿屿"])
    ep = _ep([lin, ghost], [shot])
    assert sb.unlocked_names(ep, shot, max_refs=1) == ["阿屿"]
