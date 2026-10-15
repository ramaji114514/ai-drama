"""画面校验：**看不见不等于没问题**。

这一组测试守的是一条很容易被写反的规矩：

    没配视觉 provider / 调用失败 / 输出读不出来 —— 这三种情况都**不是通过**。

写反的代价不会当场显形：报告上印着"一致性通过（9 分）"，而实际上
**没有任何东西看过画面**。所以这里几乎每条测试都在断言"它没有说通过"。

另一个重点是 provider 的接口刻意做得很笨（消息进、文本出）：
判定逻辑留在 `core/identity.py` 和 `core/vision_qc.py` 里，
于是下面最要紧的几个用例**不需要任何 API key**，一个假的 provider 就够了。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ai_drama.core import paths, vision_qc
from ai_drama.core.identity import parse_identity_verdict
from ai_drama.core.models import Character, Episode, Scene, Shot
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.qc import QcRules, QcState, Severity
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.base import ProviderError, VisionProvider
from ai_drama.providers.dryrun import DryRunVisionProvider


class _ScriptedVision(VisionProvider):
    """一个只会念台词的视觉 provider。`replies` 里放它每次要"说"的话。"""

    name = "scripted-vision"

    def __init__(self, replies: List[Any]) -> None:
        self.replies = list(replies)
        self.calls: List[Dict[str, Any]] = []

    def chat(self, messages, *, scene: str = "") -> Tuple[str, Dict[str, Any]]:
        self.calls.append({"scene": scene, "messages": messages})
        if not self.replies:
            raise ProviderError("没有更多预设回答了", retryable=False)
        nxt = self.replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt, {"unit": "call", "amount": 1, "money": 0.003,
                     "provider": self.name, "stage": "qc"}


# ————————————————————————————————————————----
# 解析：读不出来 ≠ 没有字
# ————————————————————————————————————————----


def test_no_text_verdict_keeps_three_states():
    assert vision_qc.parse_no_text_verdict('{"has_text": true, "found": ["门牌 302"]}') \
        == (True, ["门牌 302"], "")
    assert vision_qc.parse_no_text_verdict('{"has_text": false, "found": []}') \
        == (False, [], "")

    # 这三种都必须是 None（读不出来），不是 False（"没有字"）
    for bad in ("", "乱七八糟", '{"found": []}', "```json\n\n```"):
        has_text, _found, note = vision_qc.parse_no_text_verdict(bad)
        assert has_text is None, f"{bad!r} 被当成了「没有字」"
        assert note, "读不出来必须说明原因"

    # 模型说它看不清 -> 也是读不出来
    assert vision_qc.parse_no_text_verdict('{"has_text": null}')[0] is None


def test_a_fenced_json_answer_is_still_read():
    """模型爱把 JSON 包在 ``` 里，这不该让整项检查变成"没查成"。"""
    has_text, found, _ = vision_qc.parse_no_text_verdict(
        '```json\n{"has_text": true, "found": ["水印"]}\n```')
    assert (has_text, found) == (True, ["水印"])


# ————————————————————————————————————————----
# 图片 -> data URL
# ————————————————————————————————————————----


def test_images_are_shrunk_before_being_sent(workspace: Path):
    """原图 720x1280 直接发是白花 token —— 判的是"是不是同一张脸"这种粗事。"""
    from PIL import Image

    src = workspace / "big.png"
    Image.new("RGB", (720, 1280), (20, 30, 60)).save(src)
    url = vision_qc.image_data_url(src)
    assert url.startswith("data:image/jpeg;base64,")

    import base64
    import io

    raw = base64.b64decode(url.split(",", 1)[1])
    img = Image.open(io.BytesIO(raw))
    assert img.width == vision_qc.FRAME_MAX_WIDTH
    assert img.height < 1280, "高度要按比例跟着缩"
    assert url.startswith("data:image/jpeg;base64,"), "缩过之后是 JPEG（体积更小）"
    # 注意：**不要**断言"比原图字节少"。纯色 PNG 本来就压得极小，
    # 而同一个纯色缩成 JPEG 反而更大 —— 那样断言会随素材而 flaky。
    # 该钉的是"尺寸被限制住了"，不是"字节数一定更小"。


def test_a_broken_image_falls_back_instead_of_reporting_unchecked(workspace: Path):
    """缩图失败要退回原图，而不是抛。

    "少缩一次图"只是多花点 token；抛出去会让整项检查变成"没查成" ——
    那是把"看不出问题"错报成"没查成"。两件事的代价差得远。
    """
    src = workspace / "not-really.png"
    src.write_bytes(b"this is not a png at all")
    url = vision_qc.image_data_url(src)
    assert url.startswith("data:image/png;base64,")


# ————————————————————————————————————————----
# dryrun 视觉：它看不见，而且如实说出来
# ————————————————————————————————————————----


def test_the_dryrun_vision_provider_refuses_to_pretend():
    """这是整个 vision 层最重要的一条。

    做一个"假装看过了"的 dryrun 在技术上太容易了（返回 `{"pass": true,
    "score": 9}` 就行），而且会让流水线一路绿灯 —— 那正是本项目反复在防的事。
    """
    v = DryRunVisionProvider()
    assert v.health()["ok"] is False
    with pytest.raises(ProviderError) as ei:
        v.chat([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}])
    assert "没查成" in str(ei.value)


# ————————————————————————————————————————----
# 逐镜检查：三种状态各来一遍
# ————————————————————————————————————————----


def _ep_with_shots(n: int = 3) -> Episode:
    shots = [Shot(scene_id="sc01", order=i, action=f"动作{i}",
                  dialogue=f"林晚：第{i}句。", characters=["林晚"]) for i in range(n)]
    return Episode(run_id="r1", idea="x",
                   characters=[Character(name="林晚", desc="女", ref_image="ref.png")],
                   scenes=[Scene(scene_id="sc01", title="t", summary="s",
                                 location="l", time_of_day="夜", shots=shots)])


def _with_keyframes(workspace: Path, ep: Episode) -> Path:
    """把 ctx.root / run_id 那一层需要的关键帧造出来。"""
    from PIL import Image

    for shot in ep.shots:
        p = paths.keyframe(workspace, ep.run_id, shot.scene_id, shot.order)
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (200, 360), (10, 20, 40)).save(p)
    return workspace


def test_a_shot_with_text_blocks_and_a_clean_one_passes(workspace: Path):
    ep = _ep_with_shots(2)
    _with_keyframes(workspace, ep)
    vision = _ScriptedVision([
        '{"has_text": true, "found": ["门牌上写着 302"]}',
        '{"has_text": false}',
    ])

    findings, money, stats = vision_qc.check_no_text(
        vision, workspace, ep.run_id, ep.shots, severity=Severity.BLOCK)

    blocks = [f for f in findings if not f.unchecked and f.severity is Severity.BLOCK]
    assert len(blocks) == 1
    assert "门牌上写着 302" in blocks[0].detail
    assert stats == {"checked": 2, "failed": 1, "unchecked": 0,
                     "examples": ["sc01/shot_000：门牌上写着 302"]}
    assert money == pytest.approx(0.006)


def test_an_unreadable_answer_is_unchecked_not_pass(workspace: Path):
    """模型答非所问时，这一镜**没查成** —— 不是"没有字"。"""
    ep = _ep_with_shots(1)
    _with_keyframes(workspace, ep)
    vision = _ScriptedVision(["我不太确定，可能吧"])

    findings, _money, stats = vision_qc.check_no_text(
        vision, workspace, ep.run_id, ep.shots, severity=Severity.BLOCK)

    assert stats["unchecked"] == 1 and stats["checked"] == 0
    assert all(f.unchecked for f in findings if f.rule == "vision-no-text")
    assert not any(not f.unchecked and f.severity is Severity.BLOCK for f in findings), \
        "读不出来被当成了不通过或通过"


def test_a_provider_that_throws_is_unchecked_not_failed(workspace: Path):
    ep = _ep_with_shots(1)
    _with_keyframes(workspace, ep)
    vision = _ScriptedVision([ProviderError("网络断了", retryable=True)])

    findings, _money, stats = vision_qc.check_no_text(
        vision, workspace, ep.run_id, ep.shots, severity=Severity.BLOCK)

    assert stats["unchecked"] == 1
    assert "网络断了" in " ".join(f.message for f in findings)


def test_a_missing_keyframe_is_unchecked_not_pass(workspace: Path):
    ep = _ep_with_shots(1)                     # 故意不造关键帧
    vision = _ScriptedVision(['{"has_text": false}'])
    findings, _money, stats = vision_qc.check_no_text(
        vision, workspace, ep.run_id, ep.shots, severity=Severity.BLOCK)
    assert stats["unchecked"] == 1
    assert vision.calls == [], "没有图可查时不该还去调一次模型"


def test_sampling_is_evenly_spread_not_taken_from_the_front(workspace: Path):
    """取前 N 个会把检查范围永远钉在开头几场，而后半部最容易漂。"""
    ep = _ep_with_shots(10)
    _with_keyframes(workspace, ep)
    vision = _ScriptedVision(['{"has_text": false}'] * 3)
    _f, _m, stats = vision_qc.check_no_text(
        vision, workspace, ep.run_id, ep.shots, max_checks=3)
    assert stats["checked"] == 3
    picked = [c["scene"] for c in vision.calls]
    orders = [int(s.split("_")[-1]) for s in picked]
    assert orders[0] == 0 and orders[-1] >= 6, f"抽样只取了前面几个：{picked}"


# ————————————————————————————————————————----
# 一致性：判定逻辑复用 identity.py，不在别处重写一遍
# ————————————————————————————————————————----


def test_identity_uses_the_existing_verdict_logic(workspace: Path):
    """同一条回答，走 vision_qc 和直接走 parse_identity_verdict 结论必须一致。

    如果哪天有人在 vision_qc 里另写一套解析，这条会立刻炸。
    """
    ep = _ep_with_shots(1)
    _with_keyframes(workspace, ep)
    face = workspace / "ref.png"
    from PIL import Image

    Image.new("RGB", (200, 360), (200, 180, 160)).save(face)
    ep.characters[0].ref_image = str(face)

    answer = json.dumps({"pass": True, "score": 9, "issues": [],
                         "characters": {"林晚": 9}})
    vision = _ScriptedVision([answer])
    findings, money, stats = vision_qc.check_identity(
        vision, ep, workspace, ep.run_id, pass_score=7)

    assert parse_identity_verdict(answer, pass_score=7).passed is True
    assert stats["checked"] == 1 and stats["failed"] == 0
    assert any(f.severity is Severity.INFO for f in findings)
    assert money > 0
    # 定妆照要按 detail=high 发（要看清五官），关键帧按 low
    content = vision.calls[0]["messages"][0]["content"]
    details = [p.get("image_url", {}).get("detail") for p in content
               if isinstance(p, dict) and p.get("type") == "image_url"]
    assert details[0] == "high" and details[-1] == "low", details


def test_identity_below_the_score_blocks_with_the_issues(workspace: Path):
    ep = _ep_with_shots(1)
    _with_keyframes(workspace, ep)
    face = workspace / "ref.png"
    from PIL import Image

    Image.new("RGB", (200, 360), (200, 180, 160)).save(face)
    ep.characters[0].ref_image = str(face)

    vision = _ScriptedVision([json.dumps({
        "pass": False, "score": 3,
        "issues": ["林晚的发型从长发变成短发"], "characters": {"林晚": 3}})])
    findings, _m, stats = vision_qc.check_identity(
        vision, ep, workspace, ep.run_id, pass_score=7)

    assert stats["failed"] == 1
    block = next(f for f in findings if f.severity is Severity.BLOCK)
    assert block.where == "sc01/shot_000"
    assert "发型" in block.detail
    assert block.suggestion, "报出来还要给改法"


def test_identity_is_unchecked_when_the_vision_provider_is_missing(workspace: Path):
    ep = _ep_with_shots(1)
    _with_keyframes(workspace, ep)
    ep.characters[0].ref_image = ""
    findings, _m, stats = vision_qc.check_identity(
        None, ep, workspace, ep.run_id)          # 传 None 会炸 —— 不该走到那
    assert stats["checked"] == 0


# ————————————————————————————————————————----
# 端到端：并进同一份 QcReport
# ————————————————————————————————————————----


def _rules(**kw) -> QcRules:
    base = dict(require_no_text=True, require_identity=False)
    base.update(kw)
    return QcRules(**base)


@pytest.mark.ffmpeg
def test_vision_findings_land_in_the_same_qc_report(workspace: Path,
                                                    need_ffmpeg: None):
    """一份报告写"block 0"、另一份文件写"画面里有字"，等于没查。

    走完整条流水线（而不是直接调 stage08），因为要验的正是"并进去"这件事 ——
    包括它能不能影响 run 的最终状态、钱有没有记上账。
    """
    (workspace / "config").mkdir(exist_ok=True)
    (workspace / "config" / "qc_rules.yaml").write_text(
        "version: '1'\nrequire_no_text: true\nseverity_no_text: block\n"
        "vision_max_checks: 2\n", encoding="utf-8")

    store = Store(workspace / "runs" / "test.sqlite3")
    vision = _ScriptedVision(['{"has_text": true, "found": ["招牌上的错别字"]}'] * 8)
    bundle = registry.build_bundle()
    bundle.vision = vision
    pipe = Pipeline(workspace, store, bundle, concurrency=2, verbose=False)
    run = pipe.start("暴雨夜的外卖单", scenes=1, shots_per_scene=3)
    result = pipe.run(run)

    assert result["qc_state"] == QcState.FAIL.value, result["qc_state"]
    qc_stage = next(r for r in result["reports"] if r["stage"] == "qc")
    assert any("画面文字检查" in n for n in qc_stage["notes"]), qc_stage["notes"]
    assert qc_stage["money"] > 0, "画面校验花的钱没记进阶段报告"

    # 钱要真的落到账本里（不只是在报告里印了一行）
    summary = store.cost_summary(run.run_id)
    assert any("scripted-vision" in str(k) for k in summary["by_provider"]), summary

    # 结论要能挡住 done：严格模式下非 PASS 不算完成
    assert result["state"] != "done", "画面里有字却把 run 标成了完成"


@pytest.mark.ffmpeg
def test_vision_off_but_rule_on_is_unchecked_not_pass(workspace: Path,
                                                      need_ffmpeg: None):
    """规则开着、provider 没配 —— 这是最容易写成"通过"的一种情况。

    严格模式下它必须挡住 done：你要求查画面，而没有任何东西看过画面。
    """
    (workspace / "config").mkdir(exist_ok=True)
    (workspace / "config" / "qc_rules.yaml").write_text(
        "version: '1'\nrequire_no_text: true\nseverity_no_text: block\n",
        encoding="utf-8")

    store = Store(workspace / "runs" / "test.sqlite3")
    bundle = registry.build_bundle()
    bundle.vision = None
    pipe = Pipeline(workspace, store, bundle, concurrency=2, verbose=False)
    run = pipe.start("暴雨夜的外卖单", scenes=1, shots_per_scene=3)
    result = pipe.run(run)

    assert result["qc_state"] == QcState.UNCHECKED.value, result["qc_state"]
    assert result["state"] != "done"
    qc_stage = next(r for r in result["reports"] if r["stage"] == "qc")
    assert any("AI_DRAMA_VISION 没配" in str(n) for n in qc_stage["notes"]), \
        qc_stage["notes"]
    # 结论也要能在报告里读到：不是"没问题"，是"没人看过"
    rep = json.loads((workspace / "runs" / run.run_id / "qc_report.json")
                     .read_text(encoding="utf-8"))
    mine = [f for f in rep["findings"] if f["rule"] == "vision-no-text"]
    assert mine and all(f["unchecked"] for f in mine), rep["findings"]


def test_rules_default_to_off_so_nobody_is_surprised_by_the_bill():
    """两条画面校验默认关：每查一镜就是一次调用，1200 次不是小数。"""
    r = QcRules()
    assert r.require_no_text is False
    assert r.require_identity is False
    assert r.vision_max_checks == 0
