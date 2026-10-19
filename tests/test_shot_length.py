"""片长：默认 15 秒，以及它和"跟着台词走"是怎么共存的。

这两条规则容易写成互相打架：
  * 默认片长 15 秒
  * 台词说完还要有气口，所以台词越长镜头越长

正确的合成不是二选一，而是 ``max(默认, 台词需要)``：默认值抬的是**下限**，
不是上限。一句 30 秒的台词不该被剪成 15 秒。
"""

from __future__ import annotations

import pytest

from ai_drama.core.media import (
    DEFAULT_SHOT_SECONDS,
    default_shot_seconds,
    estimate_shot_seconds,
    plan_shot_split,
)
from ai_drama.core.models import Shot


def _shot(framing: str = "medium", dialogue: str = "") -> Shot:
    return Shot(scene_id="sc01", order=0, framing=framing, dialogue=dialogue)


def test_the_default_shot_length_is_fifteen_seconds(monkeypatch):
    """**产品默认值就是 15 秒。**

    测试夹具（tests/conftest.py）把环境变量压到 3 秒好让 dryrun 跑得快，
    所以这里必须先把那个变量摘掉，否则测的是夹具、不是产品。
    """
    monkeypatch.delenv("AI_DRAMA_SHOT_SECONDS", raising=False)
    assert DEFAULT_SHOT_SECONDS == 15.0
    assert default_shot_seconds() == 15.0
    assert estimate_shot_seconds(_shot("close", "……阿屿？")) == 15.0


def test_a_short_line_does_not_shrink_the_shot(monkeypatch):
    monkeypatch.delenv("AI_DRAMA_SHOT_SECONDS", raising=False)
    assert estimate_shot_seconds(_shot("close", "嗯")) == 15.0
    assert estimate_shot_seconds(_shot("extreme_close", "")) == 15.0


def test_a_long_line_still_wins_over_the_default(monkeypatch):
    """台词是硬下限 —— 默认值不能把一句长台词剪断。"""
    monkeypatch.delenv("AI_DRAMA_SHOT_SECONDS", raising=False)
    long_line = "陈默：" + "你回来了" * 40          # 约 32 秒的台词
    got = estimate_shot_seconds(_shot("medium", long_line))
    assert got > 15.0
    assert got == pytest.approx(len(long_line.strip()) / 4.5 + 0.4, abs=0.01)


def test_the_env_var_can_shorten_it(monkeypatch):
    monkeypatch.setenv("AI_DRAMA_SHOT_SECONDS", "6")
    assert default_shot_seconds() == 6.0
    assert estimate_shot_seconds(_shot("close", "嗯")) == 6.0


def test_the_env_var_can_lengthen_it(monkeypatch):
    monkeypatch.setenv("AI_DRAMA_SHOT_SECONDS", "20")
    assert estimate_shot_seconds(_shot("close", "嗯")) == 20.0


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
def test_a_nonsense_env_value_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("AI_DRAMA_SHOT_SECONDS", bad)
    assert default_shot_seconds() == DEFAULT_SHOT_SECONDS


def test_a_fifteen_second_shot_is_one_clip_on_a_thirty_second_model():
    """这是接 Seedance 的全部理由：15 秒**不用拆**。"""
    assert plan_shot_split(15.0, max_duration=30.0) == [15.0]


def test_a_fifteen_second_shot_becomes_three_clips_on_a_five_second_model():
    """换成 5 秒上限的模型，同一条规则会自动拆段 —— 而不是截断。"""
    assert plan_shot_split(15.0, max_duration=5.0) == [5.0, 5.0, 5.0]
