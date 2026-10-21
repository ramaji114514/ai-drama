"""提示词包（`ai_drama/core/export.py` + `run.py prompts`）。

包是给**程序**吃的，所以这里钉的是"键有没有、值对不对"，
不钉排版 —— 排版属于网页上那份给人看的 .txt。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_drama.core import export
from ai_drama.core.models import Character, Episode, Scene, Shot


def _episode() -> Episode:
    ep = Episode(run_id="run_x", idea="一句话", title="窗台上的等待")
    ep.style = "冷青主色调"
    ep.characters = [
        Character(char_id="c1", name="林溪", desc="女，28 岁",
                  anchor_prompt="28-year-old Chinese woman"),
        Character(char_id="c2", name="灰灰", desc="灰猫", anchor_prompt=""),
    ]
    shot = Shot.from_dict({
        "scene_id": "sc01", "order": 0, "action": "雨滴顺着玻璃滑落",
        "end_action": "猫把爪子收回去", "dialogue": "林溪：（低声）灰灰？",
        "characters": ["林溪"], "duration": 7.5, "framing": "close",
        "image_prompt": "冷青，雨滴顺着玻璃滑落，close",
        "last_frame": "冷青，猫把爪子收回去，close",
        "video_prompt": "雨滴顺着玻璃滑落。轻微手持跟随，7 秒内匀速完成。",
        "negative_prompt": "多余的手指，水印", "video_negative": "闪烁，变脸",
    })
    ep.scenes = [Scene(scene_id="sc01", title="窗台上", location="公寓",
                       time_of_day="深夜", shots=[shot])]
    return ep


def test_the_bundle_has_the_four_prompts_side_by_side():
    data = export.bundle(_episode())
    shot = data["scenes"][0]["shots"][0]
    for key in ("image_prompt", "last_frame", "video_prompt",
                "negative_prompt", "video_negative"):
        assert shot[key], key
    assert shot["label"] == "sc01/shot_000"
    assert shot["duration"] == 7.5


def test_characters_live_at_the_top_level_not_copied_into_every_shot():
    """定妆照不挂在某一镜上：复制进 shot 迟早会不一致。"""
    data = export.bundle(_episode())
    assert [c["name"] for c in data["characters"]] == ["林溪", "灰灰"]
    assert data["characters"][0]["anchor_prompt"]
    assert "anchor_prompt" not in data["scenes"][0]["shots"][0]


def test_the_bundle_carries_no_money():
    """提示词包不带钱：用包的人关心的是要粘什么。"""
    raw = json.dumps(export.bundle(_episode()), ensure_ascii=False).lower()
    for word in ("cost", "money", "price", "usd"):
        assert word not in raw


def test_aspect_comes_from_the_caller_and_may_be_empty():
    assert export.bundle(_episode(), aspect="9:16")["aspect"] == "9:16"
    assert export.bundle(_episode())["aspect"] == ""


def test_missing_fields_do_not_blow_up():
    ep = Episode(run_id="run_y", idea="x")
    ep.scenes = [Scene(scene_id="sc01", shots=[
        Shot.from_dict({"scene_id": "sc01", "order": 0})])]
    data = export.bundle(ep)
    assert data["characters"] == []
    shot = data["scenes"][0]["shots"][0]
    assert shot["image_prompt"] == "" and shot["negative_prompt"] == ""


def test_the_cli_offers_prompts_and_defaults_to_stdout():
    from ai_drama import cli

    parser = cli.build_parser()
    args = parser.parse_args(["prompts", "--idea", "一句话"])
    assert args.func is cli.cmd_prompts
    assert args.json == ""            # 空 = 打到 stdout
    assert (args.scenes, args.shots) == (2, 2)


def test_prompts_only_runs_the_two_text_stages():
    """出图/出片是花钱的；这条命令一个 provider 都不该多调。"""
    from ai_drama import cli

    src = Path(cli.__file__).read_text(encoding="utf-8")
    at = src.index("def cmd_prompts")
    body = src[at: src.index("def import_episode_json")]
    assert 'stages=["script", "storyboard"]' in body
    assert "verbose=False" in body
