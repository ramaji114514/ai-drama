"""尾帧（`end_action`）到底有没有被写出来。

尾帧提示词唯一的来源就是 `end_action`。缺了它，stage02 只能拿 `action` 顶替 ——
首尾两帧同文，粘进图生图出来是同一张图。**这不会报错**：成片照样出，
只是"续拍"这条链的第二半安静地断了。所以只能靠断言。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_drama.core.models import Episode, Scene, Shot
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.stages.stage02_storyboard import (
    compose_image_prompt,
    compose_last_frame,
)
from ai_drama.stages.stage01_script import _fill_end_actions


def _usage(stage: str = "script") -> dict:
    return {"provider": "fake", "stage": stage, "unit": "call",
            "amount": 1, "money": 0.002}


def _payload(shots_extra: dict = None) -> dict:
    shot = {"order": 0, "framing": "medium", "action": "他推开门走进来",
            "dialogue": "林晚：你回来了。", "characters": ["林晚"],
            "location": "旧书店"}
    shot.update(shots_extra or {})
    return {"title": "雨夜", "logline": "x", "style": "冷调",
            "characters": [{"name": "林晚", "desc": "25岁女性"}],
            "scenes": [{"scene_id": "sc01", "title": "门", "summary": "s",
                        "location": "旧书店", "time_of_day": "夜",
                        "shots": [shot]}]}


class LazyText:
    """第一遍漏掉 end_action 的模型。真实模型漏这个字段就是这样。"""

    name = "lazy"

    def __init__(self, fills: bool = True):
        self.fills = fills
        self.calls = []

    def generate(self, prompt, *, system="", scene="", **kw):
        self.calls.append(scene)
        if scene == "script-end_action":
            if not self.fills:
                return json.dumps({"end_actions": []}), _usage()
            return json.dumps({"end_actions": [
                {"scene_id": "sc01", "order": 0,
                 "end_action": "他已经站在柜台前，手还扶在门把上"}]}), _usage()
        return json.dumps(_payload()), _usage()


class CompleteText(LazyText):
    """一次就给全的模型 —— 不该触发补写调用。"""

    def generate(self, prompt, *, system="", scene="", **kw):
        self.calls.append(scene)
        return json.dumps(_payload(
            {"end_action": "他已经站在柜台前，手还扶在门把上"})), _usage()


def _pipe(root: Path, text) -> tuple:
    store = Store(root / "runs" / "test.sqlite3")
    pipe = Pipeline(root, store, registry.build_bundle(), concurrency=2, verbose=False)
    bundle = registry.build_bundle()
    bundle.text = text
    pipe.bundle = bundle
    return pipe, store


def test_a_lazy_model_gets_one_extra_cheap_call(workspace: Path):
    text = LazyText()
    pipe, _ = _pipe(workspace, text)
    run = pipe.start("深夜加班回家", scenes=1, shots_per_scene=1)
    # 只跑到 script：没有 final_video，流水线会如实报 failed —— 这里不关心那个，
    # 只关心分镜有没有把 end_action 补上。
    pipe.run(run, stages=["script"])

    assert text.calls == ["script", "script-end_action"], "漏了就应该补问一次，且只补一次"
    data = json.loads((workspace / "runs" / run.run_id / "episode.json")
                      .read_text(encoding="utf-8"))
    shot = data["scenes"][0]["shots"][0]
    assert shot["end_action"] == "他已经站在柜台前，手还扶在门把上"


def test_a_complete_model_is_not_asked_twice(workspace: Path):
    text = CompleteText()
    pipe, _ = _pipe(workspace, text)
    run = pipe.start("深夜加班回家", scenes=1, shots_per_scene=1)
    pipe.run(run, stages=["script"])
    assert text.calls == ["script"], "一次就写全了，不该再花一次调用"


def test_a_model_that_still_refuses_does_not_break_the_run(workspace: Path):
    """补不上不能把整集搞崩 —— 留下来的那句告警在 stage02 里。"""
    pipe, _ = _pipe(workspace, LazyText(fills=False))
    run = pipe.start("深夜加班回家", scenes=1, shots_per_scene=1)
    pipe.run(run, stages=["script"])
    data = json.loads((workspace / "runs" / run.run_id / "episode.json")
                      .read_text(encoding="utf-8"))
    assert data["scenes"][0]["shots"][0]["end_action"] == ""


# ————————————————————————————————————————---- 尾帧真的不一样了


def _ep(end_action: str) -> Episode:
    shot = Shot(scene_id="sc01", order=0, action="他推开门走进来",
                end_action=end_action, framing="medium")
    return Episode(run_id="r", idea="x", style="冷调",
                   characters=[],
                   scenes=[Scene(scene_id="sc01", title="t", summary="s",
                                 location="旧书店", time_of_day="夜", shots=[shot])])


def test_the_tail_frame_stops_being_a_copy_of_the_head_frame():
    ep = _ep("他已经站在柜台前，手还扶在门把上")
    shot = ep.shots[0]
    head = compose_image_prompt(ep, shot, [])
    tail = compose_last_frame(ep, shot, [])
    assert head != tail
    assert "手还扶在门把上" in tail
    assert "推开门走进来" not in tail


def test_without_an_end_action_it_falls_back_but_says_so():
    """老项目没有这个字段 —— 退回 action 是**已知的降级**，不是等价。"""
    ep = _ep("")
    shot = ep.shots[0]
    # 退化时动作描述还是首帧那一句（降级的代价），但尾帧会被明确标成
    # "最后一帧的定格"——以前两者逐字相同，模型以为要两张一样的图。
    img = compose_image_prompt(ep, shot, [])
    last = compose_last_frame(ep, shot, [])
    assert shot.action in last and shot.action in img
    assert last != img
    assert "最后一帧" in last


def test_the_repair_fills_by_scene_and_order_not_by_position():
    """模型回来的数组顺序不可信 —— 得按 (scene_id, order) 对号入座。"""
    shots = [Shot(scene_id="sc01", order=0, action="A"),
             Shot(scene_id="sc02", order=0, action="B")]

    class Ctx:
        class bundle:
            class text:
                name = "fake"

                @staticmethod
                def generate(prompt, **kw):
                    return json.dumps({"end_actions": [
                        {"scene_id": "sc02", "order": 0, "end_action": "B 的结束"},
                        {"scene_id": "sc01", "order": 0, "end_action": "A 的结束"},
                    ]}), _usage()

        class ledger:
            @staticmethod
            def add(*a, **kw):
                pass

        @staticmethod
        def log(msg):
            pass

    got, money = _fill_end_actions(Ctx(), shots)
    assert got == 2
    assert shots[0].end_action == "A 的结束"
    assert shots[1].end_action == "B 的结束"
    assert money == 0.002
