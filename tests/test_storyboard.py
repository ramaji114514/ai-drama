"""参考图到底有没有被"点名"。

这一层要防的失败很隐蔽：**参考图传给了模型，但提示词里一个字都没提它**。
模型不会报错，它只是按自己的理解换一张脸 —— 而且每一次生成调用都是
独立的，所以第二镜的脸和第一镜毫无关系。成片看起来"只是有点不像"，
但那不是随机波动，是链路断了。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_drama.core import paths
from ai_drama.core.identity import NOT_INHERIT, REFERENCE_HEAD
from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.stages.stage02_storyboard import (
    characters_in_shot,
    compose_image_prompt,
    reference_labels,
)
from ai_drama.stages.stage03_frames import character_in_shot


def _shot(action: str, **kw) -> Shot:
    return Shot(scene_id="sc01", order=1, action=action, **kw)


def _ep(chars, shots) -> Episode:
    return Episode(run_id="r", idea="x", characters=list(chars),
                   scenes=[Scene(scene_id="sc01", title="t", summary="s",
                                 location="l", time_of_day="夜", shots=list(shots))])


def _char(name: str, ref: str = "ref.png") -> Character:
    return Character(name=name, desc=f"{name}的外貌", ref_image=ref)


# ————————————————————————————————————————---- 谁在镜头里


def test_characters_are_ordered_by_where_they_appear_not_by_the_roster():
    """参考锁定段的 `@图片N` 要对应真正传出去的图，所以顺序得由剧本决定。"""
    ep = _ep([_char("陈默"), _char("林晚")], [])
    shot = _shot("林晚推开门，陈默抬头看她")
    assert [c.name for c in characters_in_shot(ep, shot)] == ["林晚", "陈默"]
    assert character_in_shot(ep, shot).name == "林晚"


def test_a_longer_name_containing_this_one_does_not_count():
    """`朱锦` 与 `朱锦娘` 同在剧里时，娘出现在画面里不该让锦也被锁上。"""
    ep = _ep([_char("朱锦"), _char("朱锦娘")], [])
    shot = _shot("朱锦娘推开门，屋里只有她一个人")
    assert [c.name for c in characters_in_shot(ep, shot)] == ["朱锦娘"]


def test_empty_shot_mentions_nobody():
    ep = _ep([_char("林晚")], [])
    assert characters_in_shot(ep, _shot("跨江大桥桥底空镜，环境建立")) == []


# ————————————————————————————————————————---- 点名谁


def test_only_characters_with_a_reference_image_are_named():
    """没有参考图却在提示词里写 `@图片1` —— 模型会照它想象的图渲染。"""
    ep = _ep([_char("林晚", ref=""), _char("陈默")], [])
    shot = _shot("林晚看着陈默")
    assert reference_labels(ep, shot, 2) == [("角色", "陈默")]


def test_reference_labels_respect_the_provider_limit():
    ep = _ep([_char("林晚"), _char("陈默")], [])
    shot = _shot("林晚看着陈默")
    assert reference_labels(ep, shot, 1) == [("角色", "林晚")]
    assert reference_labels(ep, shot, 2) == [("角色", "林晚"), ("角色", "陈默")]
    assert reference_labels(ep, shot, 0) == []


# ————————————————————————————————————————---- 拼提示词


def test_the_lock_goes_to_the_front_and_excludes_the_asset_layout():
    ep = _ep([_char("林晚")], [])
    shot = _shot("林晚推开门")
    prompt = compose_image_prompt(ep, shot, [("角色", "林晚")])
    assert prompt.startswith(REFERENCE_HEAD)
    assert "@图片1 = 角色「林晚」本人" in prompt
    assert NOT_INHERIT in prompt
    # 顺序变了：主体 → 动作 → 镜头 → 人数 → 风格，所以动作不再压尾
    assert "林晚推开门" in prompt
    assert "画面中只有 林晚" in prompt


def test_without_a_reference_image_the_prompt_has_no_lock_at_all():
    ep = _ep([_char("林晚", ref="")], [])
    prompt = compose_image_prompt(ep, _shot("林晚推开门"), [])
    assert REFERENCE_HEAD not in prompt
    assert "@图片1" not in prompt


# ————————————————————————————————————————---- 端到端


def _pipeline(root: Path) -> Pipeline:
    store = Store(root / "runs" / "test.sqlite3")
    return Pipeline(root, store, registry.build_bundle(), concurrency=2, verbose=False)


@pytest.mark.ffmpeg
def test_a_real_run_names_the_reference_image_and_renders_a_portrait(
        workspace: Path, need_ffmpeg: None):
    """验收：定妆照真的被渲出来，而且提示词里点了它的名。"""
    pipe = _pipeline(workspace)
    run = pipe.start("深夜回家发现门锁被换过", scenes=1, shots_per_scene=3)
    result = pipe.run(run)
    assert result["state"] == "done", result

    ep = json.loads(paths.episode_json(workspace, run.run_id).read_text(encoding="utf-8"))
    names = [c["name"] for c in ep["characters"]]
    assert len(names) == len(set(names)), f"角色重名：{names}"

    # 定妆照必须真的落盘（t2i 默认走 dryrun，不该被静默跳过）
    for ch in ep["characters"]:
        assert ch["ref_image"], f"{ch['name']} 没有参考图，参考锁定段会静默失效"
        assert Path(ch["ref_image"]).exists()

    locked = [s for scene in ep["scenes"] for s in scene["shots"]
              if s["image_prompt"].startswith(REFERENCE_HEAD)]
    assert locked, "没有任何镜头写进参考锁定段 —— 参考图等于没传"
    for s in locked:
        assert "@图片1" in s["image_prompt"]
        # 听觉信息永远不许进画面提示词
        assert "台词" not in s["video_prompt"]


@pytest.mark.ffmpeg
def test_a_crowded_shot_is_reported_not_silently_locked(workspace: Path, need_ffmpeg: None):
    """出镜人数超过参考图上限时，只能锁住前 N 个 —— 剩下的必须报出来。

    这一条不报的话，"两个人同框"的镜头里第二个人每次都换脸，
    而报告里一切正常。
    """
    pipe = _pipeline(workspace)
    run = pipe.start("两个人对峙", scenes=1, shots_per_scene=4)
    result = pipe.run(run)

    storyboard = next(r for r in result["reports"] if r["stage"] == "storyboard")
    notes = " ".join(storyboard["notes"])
    assert "参考锁定" in notes

    ep = json.loads(paths.episode_json(workspace, run.run_id).read_text(encoding="utf-8"))
    names = [c["name"] for c in ep["characters"]]
    crowded = 0
    for scene in ep["scenes"]:
        for s in scene["shots"]:
            if sum(1 for n in names if n and n in s["action"]) > 1:
                crowded += 1
    if crowded:
        assert "参考图上限" in notes, (
            f"有 {crowded} 个镜头出镜人数超过上限，却没有上报"
        )


def _fake_ctx(t2i: str, image: str):
    """只要 bundle 上那两个 provider 的 .name —— 这个判断不该依赖别的东西。"""

    class _P:
        def __init__(self, name: str):
            self.name = name

    return SimpleNamespace(bundle=SimpleNamespace(
        t2i=_P(t2i) if t2i else None, image=_P(image) if image else None))


def test_a_dryrun_portrait_fed_to_a_real_image_model_is_shouted_about():
    """占位定妆照喂给真图像模型 —— 这是**会花钱**的错误，不是"配置不全"。

    占位定妆照是一张写着提示词的卡片。真图像模型会老老实实把那张卡片当底图去改，
    于是每一帧都长着"文字卡片脸"，而且看起来像是模型正经出的图：
    钱花了，脸也没锁住，报告里却一切正常。
    """
    from ai_drama.stages.stage02_storyboard import _dryrun_portrait_feeding_a_real_model as bad

    assert bad(_fake_ctx("dryrun-t2i", "dashscope"), 2), "正是要拦的那种组合"
    # 全 dryrun 跑一遍是完全正常的用法 —— 每个新人第一次都这么跑
    assert not bad(_fake_ctx("dryrun-t2i", "dryrun-image"), 2)
    # 定妆照本来就是真模型出的，参考图是真的
    assert not bad(_fake_ctx("dashscope", "dashscope"), 2)
    # 这一集压根没有定妆照（关掉了 t2i），没什么可喂的
    assert not bad(_fake_ctx("dryrun-t2i", "dashscope"), 0)
    # 形态没配全的时候不该因为 None 崩掉
    assert not bad(_fake_ctx("dryrun-t2i", ""), 2)
    assert not bad(_fake_ctx("", "dashscope"), 2)


@pytest.mark.ffmpeg
def test_a_plain_dryrun_run_does_not_get_that_warning(workspace: Path, need_ffmpeg: None):
    """默认配置（全 dryrun）下不许出现这条警告。

    假警报会训练人忽略所有 [!]，那比没有告警更糟。
    """
    pipe = _pipeline(workspace)
    run = pipe.start("默认配置跑一遍", scenes=1, shots_per_scene=2)
    result = pipe.run(run)
    storyboard = next(r for r in result["reports"] if r["stage"] == "storyboard")
    notes = " ".join(storyboard["notes"])
    assert "占位定妆照" not in notes