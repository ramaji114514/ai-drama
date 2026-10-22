"""五段提示词的**模板本身** —— 抄来的那几条经验，得钉住。

这些约束来自社区里被反复验证的模板（角色参考图、首尾帧、运镜、负向词），
共同点是：**少一条不会报错**，只是出来的图/片子差一截。所以只能靠断言。

- 定妆照是参考图，不是海报：正面、平光、纯背景、无遮挡
- 首帧要给机位高度，不然默认一张"眼睛在画面正中"的证件照
- 尾帧要说是定格，不然模型给你一帧带运动模糊的画面
- 运镜要带速度，15 秒和 4 秒不是同一种语言
- 负向词要覆盖"技术上没坏但一眼廉价"那一组
"""

from __future__ import annotations

from ai_drama.core import lint
from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.stages.stage02_storyboard import (
    ANCHOR_SPEC,
    compose_anchor_prompt,
    compose_image_prompt,
    compose_last_frame,
    compose_negative_prompt,
    compose_video_negative,
    compose_video_prompt,
)


def _shot(action: str = "她推开门走进来", **kw) -> Shot:
    return Shot(scene_id="sc01", order=1, action=action, **kw)


def _ep(chars=(), style: str = "") -> Episode:
    return Episode(
        run_id="r", idea="x", style=style, characters=list(chars),
        scenes=[Scene(scene_id="sc01", title="t", summary="s",
                      location="l", time_of_day="夜", shots=[])],
    )


def _char(name: str = "林晚", desc: str = "30 岁，齐耳短发，眼角有疤") -> Character:
    return Character(name=name, desc=desc, ref_image="ref.png")


# ————————————————————————————————————————---- 定妆照（角色参考图）


def test_the_anchor_prompt_is_a_reference_sheet_not_a_poster():
    """定妆照的用途是当参考图 —— 好看但侧逆光的脸，拿去锁脸一无所获。"""
    ep = _ep(style="冷青色调，胶片质感")
    text = compose_anchor_prompt(ep, _char())

    assert text.startswith("30 岁，齐耳短发"), "长相必须排第一，风格只会挤掉它"
    for must in ("正面胸像", "纯色浅灰背景", "均匀柔光", "85mm"):
        assert must in text, must
    assert "画风：冷青色调，胶片质感" in text
    assert text.index("画风") > text.index("正面胸像"), "风格放最后，不要抢身份"


def test_the_anchor_prompt_survives_an_empty_style():
    text = compose_anchor_prompt(_ep(), _char())
    assert "画风" not in text
    assert "正面胸像" in text
    assert "，，" not in text and not text.endswith("，")


def test_the_anchor_spec_keeps_the_four_things_that_make_it_reusable():
    """四条缺一条，参考图就退化成一张普通的人像。"""
    joined = "，".join(ANCHOR_SPEC)
    for must in ("正面", "无遮挡", "均匀柔光", "细节清晰"):
        assert must in joined, must


# ————————————————————————————————————————---- 首帧 / 尾帧


def test_the_image_prompt_says_where_the_camera_stands():
    """只写景别不写机位高度，模型给一张平视证件照。"""
    close = compose_image_prompt(_ep([_char()]), _shot(framing="close"))
    wide = compose_image_prompt(_ep([_char()]), _shot(framing="wide"))

    assert lint.height_clause("close") in close
    assert lint.height_clause("wide") in wide
    assert close != wide


def test_an_unknown_framing_still_gets_a_camera_height():
    assert lint.height_clause("什么景别都不写") == lint.DEFAULT_HEIGHT


def test_the_last_frame_is_marked_as_a_frozen_instant():
    """首尾帧模型会照着两张图补中间过程 —— 尾帧糊了，中间就全糊。"""
    shot = _shot(end_action="他已经站在柜台前，手还扶在门把上")
    last = compose_last_frame(_ep([_char()]), shot)

    assert last.startswith("同一镜头的最后一帧")
    assert "没有运动模糊" in last
    assert "他已经站在柜台前" in last
    # 首帧不该有这些 —— 有就说明我把两句写反了
    assert "最后一帧" not in compose_image_prompt(_ep([_char()]), shot)


# ————————————————————————————————————————---- 运镜


def test_the_video_prompt_states_how_long_the_move_takes():
    """不写节奏，模型在 15 秒里前两秒演完，剩下十三秒杵着。"""
    long_take = compose_video_prompt(_shot(duration=15.0))
    assert "15 秒内匀速完成" in long_take
    assert "不加速也不放慢" in long_take
    # 运镜句在最前面（可灵把末尾那句当可选补充）
    assert long_take.index("手持跟随") < long_take.index("推开门")


# ————————————————————————————————————————---- 负向词


def test_the_negative_prompt_covers_the_cheap_look():
    """"技术上没坏但一眼廉价"那一组：塑料皮肤、CG 感、过饱和、鬼影。"""
    neg = compose_negative_prompt(_ep([_char()]), _shot("林晚推开门走进来"))
    for must in ("塑料皮肤", "过度 CG 渲染", "过饱和", "鬼影"):
        assert must in neg, must
    assert "畸形手指" in neg, "画面里有人，解剖组必须写"


def test_an_empty_shot_does_not_get_anatomy_negatives():
    """空镜里写"畸形手指"，模型会试着在画面里补一个人。"""
    ep = _ep()
    empty = Shot(scene_id="sc01", order=1, action="雨落在空荡的街道上")
    neg = compose_negative_prompt(ep, empty)
    assert "畸形手指" not in neg
    assert "塑料皮肤" in neg


def test_the_video_negative_is_about_time_not_anatomy():
    neg = compose_video_negative(_shot())
    for must in ("画面闪烁", "跳帧", "动作僵住不动"):
        assert must in neg, must
    assert "畸形手指" not in neg
    assert "塑料皮肤" in neg
