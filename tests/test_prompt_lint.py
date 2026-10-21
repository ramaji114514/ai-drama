"""提示词体检 + 短版骨架的测试。

这里钉的不是"文笔"，是几条**能被违反**的硬规则：运镜在前、人数写死、
有台词要开口、超预算要报告、平台的负向框大小不一样。
"""

from __future__ import annotations

import pytest

from ai_drama.core import dialects, lint
from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.stages.stage02_storyboard import (
    compose_image_prompt,
    compose_last_frame,
    compose_video_prompt,
)


def _shot(action="林溪弯腰凑近猫", *, dialogue="", end="", duration=5.0,
          framing="medium", image_prompt=""):
    return Shot(
        shot_id="s1", scene_id="sc01", order=1, action=action, dialogue=dialogue,
        framing=framing, duration=duration, end_action=end,
        image_prompt=image_prompt, negative_prompt="",
    )


def _ep(style="冷青主色调"):
    return Episode(run_id="r", idea="x", style=style, characters=[], scenes=[])


# —————————————————————————————————————————— 体检的小工具


def test_shorten_cuts_at_a_clause_boundary_and_leaves_short_text_alone():
    text = "雨滴顺着玻璃滑落，霓虹在积水里碎成一片，林溪抬头看见窗台上有一团灰色"
    got = lint.shorten(text, 20)
    assert len(got) <= 20
    assert not got.endswith("，")
    assert lint.shorten("雨滴顺着玻璃滑落", 40) == "雨滴顺着玻璃滑落"


def test_lens_clause_says_how_it_is_shot_not_just_how_big():
    assert "85mm" in lint.lens_clause("close")
    assert "浅景深" in lint.lens_clause("close")
    # 不认识的景别不能抛，要给一个安全默认
    assert lint.lens_clause("dutch-angle") == lint.DEFAULT_LENS


def test_count_clause_pins_the_number_of_people():
    assert lint.count_clause([]) == "画面中没有人"
    assert lint.count_clause(["林溪"]) == "画面中只有 林溪"
    assert lint.count_clause(["林溪", "灰灰"]) == "画面中只有 林溪 和 灰灰"


def test_speaking_clause_only_when_there_is_a_line():
    assert lint.speaking_clause("") == ""
    assert "口型" in lint.speaking_clause("林溪：谁？")


def test_a_move_that_cannot_finish_in_time_is_flagged():
    assert lint.move_is_too_fast("镜头稳定横移", 2.0) == ("横移", 4.0)
    assert lint.move_is_too_fast("镜头稳定横移", 6.0) is None
    assert lint.move_is_too_fast("固定机位", 2.0) is None


def test_two_moves_in_one_sentence_are_detected():
    assert lint.has_several_moves("镜头推近，然后横移")
    assert lint.has_several_moves("缓慢推近后横移")
    assert not lint.has_several_moves("镜头推近")


def test_lint_reports_the_hard_rules_that_are_broken():
    shot = _shot("雨滴滑落", dialogue="林溪：谁？", duration=2.0)
    shot.video_prompt = "镜头稳定横移，2 秒内匀速完成。雨滴滑落。"
    codes = {f.code for f in lint.lint_shot(shot)}
    assert "no_end_action" in codes          # 没写演到哪儿收
    assert "move_too_fast" in codes          # 两秒做不完一次横移
    assert "no_speech_mime" in codes         # 有台词但没让人开口
    assert "no_negative" in codes            # 没负向词


def test_a_clean_shot_passes_without_noise():
    shot = _shot("林溪抬头", end="林溪站定", duration=8.0, framing="close")
    shot.video_prompt = compose_video_prompt(shot)
    shot.negative_prompt = "畸形手指"
    shot.image_prompt = "短提示词"
    assert [f for f in lint.lint_shot(shot) if f.level == "warn"] == []


def test_summarize_is_one_line_even_with_many_findings():
    shot = _shot(dialogue="林溪：谁？", duration=1.0)
    shot.video_prompt = "推近，然后横移，1 秒内匀速完成。走。"
    line = lint.summarize(lint.lint_shot(shot))
    assert "\n" not in line
    assert "体检" in line


# —————————————————————————————————————————— 短版骨架


def test_the_frame_prompt_carries_lens_and_headcount(monkeypatch):
    monkeypatch.delenv("AI_DRAMA_PROMPT_DETAIL", raising=False)
    ep = _ep()
    got = compose_image_prompt(ep, _shot())
    assert "35mm" in got                      # 焦段
    assert "画面中只有 林溪" not in got        # 没声明角色时不该凭空点名
    assert "画面中没有人" in got              # 空镜也要写死人数


def test_the_short_version_trims_the_style_but_keeps_the_action(monkeypatch):
    monkeypatch.delenv("AI_DRAMA_PROMPT_DETAIL", raising=False)
    long_style = "冷青主色调，高对比胶片质感，手持微晃，雨痕蜿蜒，书页泛黄卷边，灯光昏黄"
    got = compose_image_prompt(_ep(long_style), _shot("林溪弯腰凑近猫"))
    assert "林溪弯腰凑近猫" in got
    assert len(got.split("角色")[-1]) < len(long_style) + 40


def test_the_full_version_is_still_one_env_var_away(monkeypatch):
    monkeypatch.setenv("AI_DRAMA_PROMPT_DETAIL", "full")
    long_style = "冷青主色调，高对比胶片质感，手持微晃，雨痕蜿蜒，书页泛黄卷边"
    got = compose_image_prompt(_ep(long_style), _shot())
    assert long_style in got


def test_the_video_prompt_puts_the_camera_first_and_says_where_it_stops():
    got = compose_video_prompt(_shot("林溪走近窗台", end="林溪停在窗台前", duration=6.0))
    assert got.startswith("轻微手持跟随")
    assert got.index("林溪走近窗台") > got.index("轻微手持跟随")
    assert "最后停在：林溪停在窗台前" in got


def test_a_shot_with_a_line_tells_the_model_to_move_its_mouth():
    got = compose_video_prompt(_shot(dialogue="林溪：谁？", duration=6.0))
    assert "口型" in got


def test_the_tail_frame_also_pins_the_headcount_and_the_lens():
    got = compose_last_frame(_ep(), _shot(end="林溪站定"))
    assert "林溪站定" in got
    assert "画面中没有人" in got


# —————————————————————————————————————————— 平台能力矩阵


def test_the_limits_are_the_ones_in_the_docs():
    assert dialects.DIALECTS["kling"].max_chars == 2500
    assert dialects.DIALECTS["veo"].max_negative == 500
    assert dialects.DIALECTS["jimeng"].max_negative == 0


def test_the_negative_box_uses_its_own_limit_not_the_positive_one():
    shot = _shot()
    shot.negative_prompt = "、".join(f"毛病{i}" for i in range(400))
    got = dialects.render(None, shot, "veo")
    neg = [f for f in got.fields if f.key == "negative"][0]
    assert len(neg.text) <= 500
    assert neg.truncated


def test_hailuo_gets_a_bracket_move_instead_of_a_sentence():
    shot = _shot(framing="close")
    shot.video_prompt = "他抬头看窗外。"
    got = dialects.render(None, shot, "hailuo")
    video = [f for f in got.fields if f.key == "video"][0]
    assert video.text.startswith("[Push in]")


def test_the_identity_block_does_not_count_against_the_budget():
    """参考锁定段两百多字、必带；把它算进预算等于每镜都报警。"""
    block = "【参考锁定】画面中的人物、服装、场景、道具严格以参考图为准，不得自行更换长相。\n"
    body = "角色 林溪：28岁女性，长发微乱，特写，85mm，画面中只有 林溪"
    assert lint.body_length(block + body) == len(body)
    assert lint.looks_too_long(block + body, budget=len(body)) is False
    assert lint.looks_too_long(block + body + "多" * 30, budget=len(body)) is True
