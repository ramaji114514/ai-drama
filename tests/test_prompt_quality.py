"""提示词本身的质量 —— 项目唯一的产物，所以它值得单独一层测试。

这里钉住的四件事，任何一条坏掉都不会报错：模型照常出图，只是出得不对。
所以只能靠断言，不能靠"看一眼觉得还行"。
"""

from __future__ import annotations

from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.stages.stage02_storyboard import (
    FRAMING_PROMPT,
    compose_image_prompt,
    compose_last_frame,
    compose_negative_prompt,
    compose_video_negative,
    compose_video_prompt,
)


def _shot(action: str, **kw) -> Shot:
    return Shot(scene_id="sc01", order=1, action=action, **kw)


def _ep(chars, **kw) -> Episode:
    return Episode(run_id="r", idea="x", characters=list(chars),
                   scenes=[Scene(scene_id="sc01", title="t", summary="s",
                                 location="l", time_of_day="夜", shots=[])],
                   **kw)


def _char(name: str = "林晚") -> Character:
    return Character(name=name, desc=f"{name}的外貌", ref_image="ref.png")


# ————————————————————————————————————————---- 负向提示词


def test_the_negative_prompt_covers_hands_and_watermarks():
    """手和版面是重出成本最高的两类事故，负向里必须有。"""
    neg = compose_negative_prompt(_ep([_char()]), _shot("林晚推开门"))
    assert "畸形手指" in neg
    assert "水印" in neg
    assert "字幕" in neg


def test_negatives_never_leak_into_the_positive_prompt():
    """把"不要水印"写进正向，等于把"水印"这个词递给模型。"""
    ep = _ep([_char()])
    positive = compose_image_prompt(ep, _shot("林晚推开门"))
    for word in ("水印", "畸形手指", "低分辨率"):
        assert word not in positive


def test_an_empty_shot_does_not_mention_hands():
    """空镜里本来没有人。写"畸形手指"只会把模型往人上引。"""
    neg = compose_negative_prompt(_ep([]), _shot("雨滴砸在玻璃窗上"))
    assert "手指" not in neg
    assert "水印" in neg, "版面类与画面里有没有人无关，仍然要在"


def test_the_video_negative_is_about_time_not_anatomy():
    """视频坏在时间维度：变脸、闪烁、跳帧。这些词在静态图里毫无意义。"""
    neg = compose_video_negative(_shot("林晚推开门"))
    assert "变脸" in neg
    assert "闪烁" in neg
    assert "畸形手指" not in neg


# ————————————————————————————————————————---- 景别不重复


def test_the_framing_is_not_said_twice():
    """action 开头已经写了景别，骨架再补一句会让模型把两个景别都装进画面。"""
    shot = _shot("俯拍特写：雨滴顺着玻璃往下爬")
    prompt = compose_image_prompt(_ep([]), shot)
    assert FRAMING_PROMPT["medium"] not in prompt


def test_a_framing_only_in_the_tail_still_gets_the_skeleton_line():
    """景别词只是偶尔出现在 action 末尾时不算"已经说了" —— 那是在描述，不是取景。"""
    shot = _shot("林晚站在门口，很久没有动，直到镜头给她一个特写")
    prompt = compose_image_prompt(_ep([]), shot)
    assert FRAMING_PROMPT["medium"] in prompt


def test_the_last_frame_also_skips_a_duplicated_framing():
    shot = _shot("林晚推开门", end_action="特写：他的眼睛慢慢睁大")
    prompt = compose_last_frame(_ep([]), shot)
    assert FRAMING_PROMPT["medium"] not in prompt
    assert "眼睛慢慢睁大" in prompt


# ————————————————————————————————————————---- 镜头知道自己有多长


def test_the_video_prompt_says_how_long_it_has():
    """不写节奏，模型会在长镜头里前两秒演完、然后杵着不动。"""
    prompt = compose_video_prompt(_shot("林晚推开门", duration=15.0))
    assert "15 秒内匀速完成" in prompt


def test_even_a_sub_second_shot_gets_a_pace():
    prompt = compose_video_prompt(_shot("林晚推开门", duration=0.4))
    assert "匀速完成" in prompt
    assert "0 秒" not in prompt


# ————————————————————————————————————————---- 拼接不留双标点


def test_a_style_that_ends_with_a_full_stop_does_not_leave_a_double_punctuation():
    """真实输出里出现过 "……猫的凝视。，角色 灰灰：……"。

    模型写的 style 大多自带句号，而拼接处加的是逗号。图生图不会为标点报错，
    它只会把这一坨当成要画的内容 —— 白占注意力，还显得这工具不讲究。
    """
    ep = Episode(run_id="r", idea="x", style="冷蓝与暖橙对比色调，雨夜潮湿质感。",
                 characters=[], scenes=[])
    prompt = compose_image_prompt(ep, _shot("灰猫端坐在窗台正中。"))
    assert "。，" not in prompt
    assert "。。" not in prompt
    assert "雨夜潮湿质感，中景，人物腰部以上，灰猫端坐" in prompt


def test_the_video_prompt_does_not_double_its_full_stop():
    prompt = compose_video_prompt(_shot("林溪弯腰凑近猫，眉头微皱。", duration=15.0))
    assert "。。" not in prompt
    assert prompt.startswith("林溪弯腰凑近猫，眉头微皱。轻微手持跟随")


def test_a_part_that_is_only_punctuation_is_dropped_not_joined():
    """模型偶尔把整段写成"…"或"。"，拼进去就是一个没有内容的逗号。"""
    ep = Episode(run_id="r", idea="x", style="。", characters=[], scenes=[])
    prompt = compose_image_prompt(ep, _shot("雨滴顺着玻璃滑落"))
    assert not prompt.startswith("，")
    assert "，，" not in prompt
