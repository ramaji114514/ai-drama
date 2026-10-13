"""数据模型与时间估算。

这一层看着无聊，但它决定了整条流水线的"长度预算"：
镜头时长估错，后面配音、对齐、成片全都会飘。
"""

from __future__ import annotations

from ai_drama.core.media import estimate_shot_seconds
from ai_drama.core.models import Episode, Scene, Shot, STAGES


def _shot(**kw) -> Shot:
    base = dict(scene_id="sc01", order=0)
    base.update(kw)
    return Shot(**base)


def test_no_dialogue_uses_action_length():
    """没有台词 -> 按动作长短估。画面时长不能短到看不清发生了什么。"""
    short = estimate_shot_seconds(_shot(action="抬眼"))
    long = estimate_shot_seconds(
        _shot(action="她慢慢推开那扇贴着封条的门，门后是一片被翻得乱七八糟的客厅")
    )
    assert 1.0 <= short <= long <= 8.0


def test_dialogue_dominates_duration():
    """有台词 -> 时长由台词的语速决定，不能被 action 的短长盖过去。

    这是短剧最实际的一条约束：镜头必须留够把话说完的时间，
    否则配音会被硬切，或者画面在台词说完前就跳走了。
    """
    one = estimate_shot_seconds(_shot(action="特写", dialogue="你是谁"))
    many = estimate_shot_seconds(
        _shot(action="特写", dialogue="你到底是谁，为什么会拿着我家的钥匙站在这里，告诉我")
    )
    assert many > one
    assert many >= len("你到底是谁，为什么会拿着我家的钥匙站在这里，告诉我") / 9.0


def test_episode_duration_is_sum_of_shots():
    ep = Episode(run_id="r", idea="i")
    sc = Scene(scene_id="sc01")
    sc.shots.append(_shot(duration=3.0))
    sc.shots.append(_shot(order=1, duration=4.5))
    ep.scenes.append(sc)
    assert ep.total_duration == 7.5


def test_episode_roundtrip_preserves_everything():
    """episode.json 是唯一的真相源，序列化必须无损。

    丢一个字段 = 人工改完 JSON 再 resume 时改动被吃掉 —— 那 resume 就失去意义了。
    """
    ep = Episode(run_id="r1", idea="想法", title="片名", logline="梗概", style="赛博")
    sc = Scene(scene_id="sc01", title="场一", location="楼道", time_of_day="深夜")
    sc.shots.append(
        _shot(order=0, action="推门", dialogue="有人吗", framing="close", duration=4.0)
    )
    ep.scenes.append(sc)

    back = Episode.from_dict(ep.to_dict())
    assert back.title == ep.title
    assert back.style == ep.style
    assert back.total_duration == ep.total_duration
    assert back.scenes[0].location == "楼道"
    assert back.scenes[0].shots[0].dialogue == "有人吗"
    assert back.scenes[0].shots[0].framing == "close"


def test_stage_order_is_the_contract():
    """阶段顺序就是流水线的契约，改它等于改产品。

    单独断言一次，避免有人"顺手"调换 05 和 06 —— 那样语音就永远对不上画面。
    """
    assert STAGES == ["script", "storyboard", "frames", "video", "audio", "concat"]
