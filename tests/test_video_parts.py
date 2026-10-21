"""一个镜头拆成几段之后，提示词要跟着拆。

以前的做法是：整镜写「15 秒内匀速完成」，拆成 3 段之后**原样**发给模型，
只把请求里的 duration 改成 5 秒。等于要求模型用 5 秒演完 15 秒的信息量 ——
它只能演得又快又乱，或者演到一半开始循环。这种错误不会报错，
只会让每一镜都看起来"节奏很奇怪"，而且每一段都真的付了钱。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_drama.stages.stage04_video import _part_prompt      # noqa: E402


def _shot(prompt: str = "", action: str = "她推开门") -> SimpleNamespace:
    return SimpleNamespace(video_prompt=prompt, action=action)


FULL = "她推开门。镜头缓推，15 秒内匀速完成，不加速也不放慢。"


def test_single_part_keeps_the_prompt_untouched():
    """不拆段时一个字都不该改 —— 那是要导出给用户粘进可灵的。"""
    assert _part_prompt(_shot(FULL), 1, 1, 15.0) == FULL


def test_the_whole_shot_pace_is_stripped_from_every_part():
    """整镜的"15 秒内完成"必须摘掉，否则每段都在被要求演完整镜。"""
    for part in (1, 2, 3):
        text = _part_prompt(_shot(FULL), part, 3, 5.0)
        assert "15 秒" not in text
        assert "5 秒" in text
        assert "匀速完成，不加速也不放慢" not in text


def test_each_part_says_where_it_starts_and_stops():
    first = _part_prompt(_shot(FULL), 1, 3, 5.0)
    middle = _part_prompt(_shot(FULL), 2, 3, 5.0)
    last = _part_prompt(_shot(FULL), 3, 3, 5.0)

    assert "第 1/3 段" in first and "不要在这一段里收尾" in first
    assert "第 2/3 段" in middle and "不要重复已经演过的开头" in middle
    assert "第 3/3 段" in last and "完成收尾" in last


def test_the_action_and_camera_move_survive_the_rewrite():
    """摘掉的是时长交代，不是动作和运镜 —— 那是这一镜的全部内容。"""
    text = _part_prompt(_shot(FULL), 2, 3, 5.0)
    assert text.startswith("她推开门")
    assert "镜头缓推" in text


def test_it_falls_back_to_the_action_when_there_is_no_video_prompt():
    text = _part_prompt(_shot("", action="雨砸在玻璃上"), 2, 2, 4.0)
    assert "雨砸在玻璃上" in text
    assert "第 2/2 段" in text


def test_seconds_are_rounded_to_a_whole_number():
    """给模型看的秒数不需要小数 —— 「4.7 秒」只会让它更糊涂。"""
    text = _part_prompt(_shot(FULL), 1, 2, 4.7)
    assert "5 秒" in text
