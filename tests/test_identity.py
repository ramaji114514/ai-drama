"""角色一致性：参考锁定、重生成、以及"没查成 ≠ 通过"。

这里每一条测的都是**会静默出错**的地方：锁定段编号错一位、重生成指令
被叠了两遍、校验输出解析失败被当成通过。这三种错都不会报异常，
只会让你在成片里看到一张陌生的脸。
"""

from __future__ import annotations

import json

from ai_drama.core.identity import (
    IDENTITY_RETRY,
    NOT_INHERIT,
    PASS_SCORE,
    REFERENCE_HEAD,
    check_messages,
    identity_retry_prompt,
    parse_identity_verdict,
    reference_block,
    shot_identity_prompt,
)


# ————————————————————————————————————————---- 参考锁定段


def test_reference_block_numbers_match_the_image_order():
    """`@图片N` 必须与传给模型的图片列表同序。

    错一位不会报错 —— 模型会照着错的那张图渲，而且渲得很准。
    """
    block = reference_block([("角色", "林晚"), ("服装", "林晚·战袍"), ("场景", "天台")])
    assert "@图片1 = 角色「林晚」本人" in block
    assert "@图片2 = 「林晚·战袍」这套服装" in block
    assert "@图片3 = 场景「天台」" in block
    assert block.index("@图片1") < block.index("@图片2") < block.index("@图片3")


def test_reference_block_demands_identity_and_excludes_asset_layout():
    block = reference_block([("角色", "陆离")])
    assert block.startswith(REFERENCE_HEAD)
    assert "不得自行更换长相" in block
    # 这一句是"能不能锁住人"的关键 —— 少了它，白底证件照的版式会被搬进分镜
    assert NOT_INHERIT in block
    assert "纯白背景" in block and "站姿" in block


def test_reference_block_is_empty_without_labels():
    assert reference_block([]) == ""
    assert shot_identity_prompt("一镜的动作", []) == "一镜的动作"


def test_lock_block_goes_to_the_front_of_the_prompt():
    """硬约束放尾部时模型会忽略它 —— 参考项目的复盘原话是"脸没听这套"。"""
    out = shot_identity_prompt("林晚推开门", [("角色", "林晚")])
    assert out.startswith(REFERENCE_HEAD)
    assert out.endswith("林晚推开门")


# ————————————————————————————————————————---- 重生成


def test_retry_prompt_puts_the_specific_difference_first():
    """"发型变短了"比"要一致"有用 —— 但前提是它进了提示词。"""
    out = identity_retry_prompt("林晚推开门", ["林晚的发型从长发变成短发"])
    assert out.startswith("**【重新生成】")
    assert "发型从长发变成短发" in out
    assert out.endswith("林晚推开门")


def test_retry_prompt_is_idempotent():
    """重试两次不该把同一段指令叠两遍 —— 模型会把它当噪音忽略掉。"""
    once = identity_retry_prompt("林晚推开门", ["脸型变了"])
    twice = identity_retry_prompt(once, ["脸型变了"])
    assert once == twice
    assert once.count(IDENTITY_RETRY.split("（", 1)[0]) == 1


def test_retry_prompt_survives_missing_issues():
    out = identity_retry_prompt("推开门", [])
    assert "长相或服装变了" in out


# ————————————————————————————————————————---- 判定解析


def _verdict_obj(**kw) -> str:
    return json.dumps({"pass": True, "score": 8, "issues": [], "characters": {}, **kw},
                      ensure_ascii=False)


def test_clean_json_parses():
    v = parse_identity_verdict(_verdict_obj(characters={"陆离": 9}))
    assert v.state == "pass"
    assert v.score == 8
    assert v.characters == {"陆离": 9}
    assert not v.unchecked


def test_code_fence_is_stripped():
    v = parse_identity_verdict("```json\n" + _verdict_obj(score=9) + "\n```")
    assert v.state == "pass" and v.score == 9


def test_low_score_fails_with_issues():
    v = parse_identity_verdict(_verdict_obj(score=4, issues=["陆离的发型变了"]))
    assert v.state == "fail"
    assert not v.passed
    assert v.issues == ["陆离的发型变了"]
    assert "陆离的发型变了" in v.render()


def test_per_character_minimum_wins_over_the_overall_score():
    """主角漂了但配角很准 —— 平均分会放过它，最低分不会。"""
    v = parse_identity_verdict(_verdict_obj(score=9, characters={"主角": 4, "配角": 9}))
    assert v.state == "fail", "逐角色最低分门槛没生效"


def test_score_exactly_at_the_threshold_passes():
    v = parse_identity_verdict(_verdict_obj(score=PASS_SCORE, characters={"A": PASS_SCORE}))
    assert v.state == "pass"


def test_garbage_output_is_unchecked_not_pass():
    """这是整套设计里最重要的一条。"""
    for bad in ("", "   ", "模型拒绝回答", "[1, 2, 3]"):
        v = parse_identity_verdict(bad)
        assert v.unchecked, f"{bad!r} 被当成了查过"
        assert v.state == "unchecked"
        assert not v.passed
        assert v.note
        assert not v.retryable, "没查成不该去重渲 —— 那是花钱掩盖网络问题"


def test_truncated_json_is_unchecked():
    v = parse_identity_verdict('{"pass": true, "score": 8')
    assert v.unchecked


def test_missing_score_falls_back_to_the_pass_field():
    """走到这里说明调用成功、输出也是 JSON —— 只是模型没按格式给分。

    那是格式偏差，不是"没查成"。校验器把生成链路整个挂掉更糟。
    """
    v = parse_identity_verdict('{"pass": false, "issues": ["不像"]}')
    assert v.state == "fail"
    assert not v.unchecked
    assert "pass 字段" in v.note


def test_no_score_and_no_pass_field_passes_with_a_note():
    v = parse_identity_verdict('{"characters": {"A": 9}}')
    assert v.state == "pass"
    assert v.note and "复核" in v.note


def test_unchecked_and_fail_render_differently():
    assert "未查成" in parse_identity_verdict("").render()
    assert "不通过" in parse_identity_verdict(_verdict_obj(score=2)).render()
    assert "通过" in parse_identity_verdict(_verdict_obj(score=9)).render()


def test_verdict_roundtrips_to_dict():
    d = parse_identity_verdict(_verdict_obj(score=3, issues=["x"])).to_dict()
    assert d["state"] == "fail" and d["issues"] == ["x"]


# ————————————————————————————————————————---- 校验消息


def test_check_messages_put_references_before_targets():
    msgs = check_messages(
        [("角色", "林晚", "https://x/ref.png")],
        ["https://x/a.jpg", "https://x/b.jpg"],
        is_video=True,
    )
    content = msgs[0]["content"]
    kinds = [c["type"] for c in content]
    assert kinds.count("image_url") == 3
    first_ref = next(i for i, c in enumerate(content) if c["type"] == "image_url")
    texts = [c.get("text", "") for c in content]
    assert any("参考图：角色（林晚）" in t for t in texts)
    assert first_ref < next(i for i, t in enumerate(texts) if "视频抽帧 1/2" in t)


def test_reference_images_are_high_detail_and_frames_are_low():
    """参考图要看清五官；抽帧一多 high 会把 token 直接爆掉。"""
    msgs = check_messages([("角色", "A", "r")], ["t1", "t2"])
    details = [c["image_url"]["detail"] for c in msgs[0]["content"]
               if c["type"] == "image_url"]
    assert details == ["high", "low", "low"]
