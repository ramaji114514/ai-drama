"""体检里那几条"不致命但会咬人"的小 bug —— 每条一个回归。

这些的共同点：出错时**不报错**，只是结果悄悄不对 (提示词还是旧的、已付的钱
被删了、报错写进了看不见的容器)。所以每条都钉住那个具体的错误形态。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_drama.core.models import Episode, Shot
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.stages import stage01_script, stage02_storyboard


# ————————————————————————————————————————----
# 旧 json / 手写 json
# ————————————————————————————————————————----


def test_a_shot_missing_keys_gets_defaults_not_none():
    """老 episode.json 里没有 negative_prompt：不能变成 None。

    变成 None 之后，拼提示词时炸的是 `NoneType + str`，那个报错指不到
    "少了一个键"这个真正的原因。
    """
    shot = Shot.from_dict({"scene_id": "sc01", "order": 0})
    assert shot.negative_prompt == ""
    assert shot.video_negative == ""
    assert shot.sfx == []
    assert shot.framing == "medium"
    assert shot.duration == 4.0


def test_an_explicit_null_is_treated_as_missing():
    shot = Shot.from_dict({"scene_id": "sc01", "order": 1, "sfx": None,
                           "negative_prompt": None})
    assert shot.sfx == [] and shot.negative_prompt == ""


def test_a_stringly_typed_order_and_duration_are_coerced():
    shot = Shot.from_dict({"scene_id": "sc01", "order": "2", "duration": "6.5"})
    assert shot.order == 2 and shot.duration == 6.5

    bad = Shot.from_dict({"scene_id": "sc01", "order": "第一", "duration": None})
    assert bad.order == 0 and bad.duration == 4.0


def test_the_model_writing_a_non_numeric_order_does_not_kill_the_script():
    """`order` 写成 "1st" 时用数组位置顶上 —— 不该让整集停在这一行。"""
    from random import random  # noqa: F401  （只是为了让这段像别处的测试）

    assert stage01_script._order_of({"order": 3}, 9) == 3
    assert stage01_script._order_of({"order": "2"}, 9) == 2
    assert stage01_script._order_of({"order": "1st"}, 9) == 9
    assert stage01_script._order_of({}, 9) == 9


# ————————————————————————————————————————----
# 景别识别
# ————————————————————————————————————————----


def test_framing_written_at_the_start_counts():
    assert stage02_storyboard._action_already_names_framing("俯拍全景：雨水砸在招牌上")
    assert stage02_storyboard._action_already_names_framing("特写，他抬眼")


def test_framing_mentioned_later_is_not_a_directive():
    """景别出现在动作中段，是动作的一部分，不是取景指令 —— 该补还得补。"""
    assert not stage02_storyboard._action_already_names_framing(
        "他把伞收起来，抬头看着招牌，镜头缓缓推成特写")


# ————————————————————————————————————————----
# 没有定妆照的角色
# ————————————————————————————————————————----


def test_a_character_without_a_portrait_is_declared_not_dropped():
    """没定妆照的人**最需要**那句"按文字描述生成"。

    旧实现只筛 `c.ref_image`，等于把根本锁不了脸的人从提示词里整个漏掉。
    """
    ep = Episode(run_id="r", idea="x")
    ep.characters = [
        type("C", (), {"name": "林溪", "desc": "女", "ref_image": "runs/a.png",
                       "char_id": "c1", "aliases": []})(),
        type("C", (), {"name": "灰灰", "desc": "猫", "ref_image": None,
                       "char_id": "c2", "aliases": []})(),
    ]
    shot = Shot.from_dict({"scene_id": "sc01", "order": 0,
                           "characters": ["林溪", "灰灰"]})
    ep.scenes = [type("S", (), {"scene_id": "sc01", "shots": [shot]})()]
    assert stage02_storyboard.unlocked_names(ep, shot, max_refs=1) == ["灰灰"]


# ————————————————————————————————————————----
# 提示词过期
# ————————————————————————————————————————----


def test_editing_the_action_makes_the_prompts_stale():
    shot = Shot.from_dict({
        "scene_id": "sc01", "order": 0,
        "action": "雨滴顺着玻璃滑落",
        "image_prompt": "冷青色调，雨滴顺着玻璃滑落，中景",
    })
    assert not stage02_storyboard._prompts_look_stale(shot)
    shot.action = "她推门走进来，肩上全是雨"
    assert stage02_storyboard._prompts_look_stale(shot)


def test_a_hand_edited_prompt_is_not_treated_as_stale():
    """网页上改过的提示词，只要动作原文还在里面，就不该被下一轮覆盖。"""
    shot = Shot.from_dict({
        "scene_id": "sc01", "order": 0,
        "action": "雨滴顺着玻璃滑落",
        "image_prompt": "雨滴顺着玻璃滑落，逆光，浅景深，35mm",
    })
    assert not stage02_storyboard._prompts_look_stale(shot)


def test_an_empty_prompt_is_stale():
    assert stage02_storyboard._prompts_look_stale(
        Shot.from_dict({"scene_id": "sc01", "order": 0}))


# ————————————————————————————————————————----
# 失败了不要把已经付过钱的分段删掉
# ————————————————————————————————————————----


def test_only_the_parts_after_the_failed_one_are_cleaned(workspace: Path):
    from ai_drama.core import paths
    from ai_drama.stages import stage04_video

    scene = paths.scene_dir(workspace, "run_x", "sc01")
    scene.mkdir(parents=True, exist_ok=True)
    made = []
    for idx in (1, 2, 3):
        p = scene / f"shot_000_p{idx}_clip.mp4"
        p.write_bytes(b"x" * 8)
        made.append(p)

    class Ctx:
        root = workspace

        class run:                       # noqa: N801  像 StageContext 那样用
            run_id = "run_x"

    shot = Shot.from_dict({"scene_id": "sc01", "order": 0})
    stage04_video._cleanup_parts(Ctx(), shot, after=2)
    assert made[0].exists() and made[1].exists()
    assert not made[2].exists()          # 第 3 段是过期尝试留下的，删掉


# ————————————————————————————————————————----
# 网页：报错要看得见 / Range 只读那一段
# ————————————————————————————————————————----


def test_the_error_card_unhides_the_content_region(workspace: Path):
    """进页面失败时 #content 还是 hidden —— 把错误卡写进隐藏容器等于什么都不显示。"""
    js = (Path(__file__).resolve().parents[1] / "ai_drama" / "web" / "static"
          / "app.js").read_text(encoding="utf-8")
    at = js.index("async function getJSON")
    body = js[at: at + 900]
    assert "$('#empty').hidden = true" in body
    assert "$('#content').hidden = false" in body


def test_a_range_request_reads_only_that_slice(workspace: Path):
    """拖一次进度条发一个 Range 请求，不该把整个成片读进内存。"""
    import threading
    import urllib.request

    from ai_drama.web.app import make_server

    run_dir = workspace / "runs" / "run_range"
    run_dir.mkdir(parents=True)
    payload = bytes(range(256)) * 8          # 2KB
    (run_dir / "final_video.mp4").write_bytes(payload)

    httpd, app = make_server(workspace, workspace / "runs" / "t.sqlite3",
                             port=0, quiet=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        req = urllib.request.Request(
            base + "/media?path=runs/run_range/final_video.mp4",
            headers={"Range": "bytes=100-199"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            assert r.status == 206
        assert body == payload[100:200]
        assert r.headers["Content-Range"] == "bytes 100-199/2048"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_the_range_response_is_sliced_not_stored_whole(monkeypatch):
    """直接掐实现：`_file` 不许再调 `read_bytes()`。"""
    from ai_drama.web import app as webapp

    def boom(self, *a, **k):                 # noqa: ANN001
        raise AssertionError("Range 请求不该把整个文件读进内存")

    monkeypatch.setattr(Path, "read_bytes", boom, raising=False)
    src = (Path(webapp.__file__).read_text(encoding="utf-8"))
    start = src.index("def _file(")
    end = src.index("def _static(")
    assert "read_bytes()" not in src[start:end]
    assert 'target.open("rb")' in src[start:end]
