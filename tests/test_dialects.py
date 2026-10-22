"""同一个镜头，粘到可灵、即梦、Seedance 里，该怎么写并不一样。

差别不在文笔，在**字段**：有没有负向框、认不认尾帧图、`【…】`小标题会不会
被当成画面内容。搞错任何一项，用户拿到的就是把"不要水印"画成水印的那类结果。

所以这一层只做一件事：把同一镜翻成各家网页端的几段文字，并且在
"这家没有这个字段"时**明说丢了什么**，不偷偷丢。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_drama.core import dialects                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _shot(**kw) -> SimpleNamespace:
    base = dict(
        image_prompt="【参考锁定】人物必须与图一致\n冷青主色调，少年站在门内",
        last_frame="【参考锁定】同一张脸\n他转过身，伞还在滴水",
        video_prompt="他推开门。镜头缓推，15 秒内匀速完成，不加速也不放慢。",
        negative_prompt="多余的手指，畸变，水印",
        video_negative="变脸，闪烁，跳帧",
        end_action="他转过身，伞还在滴水",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_generic_keeps_the_headings_and_the_negatives():
    out = dialects.render(None, _shot(), "generic")
    texts = {f.key: f.text for f in out.fields}
    assert "【参考锁定】" in texts["first_frame"]
    assert texts["negative"]
    assert not out.dropped


def test_kling_gets_a_negative_box_but_no_bracket_headings():
    """可灵会把方括号小标题当画面内容 —— 粘进去就成了"画面里有字"。"""
    out = dialects.render(None, _shot(), "kling")
    texts = {f.key: f.text for f in out.fields}
    for value in texts.values():
        assert "【" not in value and "】" not in value
    assert "少年站在门内" in texts["first_frame"]
    assert texts["negative"]
    assert "负向词" not in " ".join(out.dropped)


def test_jimeng_has_no_negative_box_and_says_so():
    out = dialects.render(None, _shot(), "jimeng")
    keys = [f.key for f in out.fields]
    assert "negative" not in keys
    assert any("负向词" in why for why in out.dropped)


def test_seedance_has_no_negative_either():
    out = dialects.render(None, _shot(), "seedance")
    assert "negative" not in [f.key for f in out.fields]
    assert any("负向" in why for why in out.dropped)


def test_a_platform_without_tail_frame_moves_it_into_the_motion_text():
    """只认首帧的平台，尾帧信息不能凭空消失 —— 得留在视频提示词里。"""
    only_first = dialects.Dialect(
        key="only_first", title="只看首帧", has_negative=False,
        has_last_frame=False, max_chars=4000, strip_headings=True)
    dialects.DIALECTS["only_first"] = only_first
    try:
        out = dialects.render(None, _shot(), "only_first")
    finally:
        dialects.DIALECTS.pop("only_first", None)
    assert "last_frame" not in [f.key for f in out.fields]
    assert any("尾帧图" in why and "伞还在滴水" in why for why in out.dropped)


def test_long_text_is_cut_at_a_sentence_end_and_flagged():
    long = "。".join(["他把伞收起来"] * 400)
    out = dialects.render(None, _shot(image_prompt=long), "jimeng")
    first = [f for f in out.fields if f.key == "first_frame"][0]
    assert first.truncated
    assert len(first.text) <= dialects.get("jimeng").max_chars


def test_an_unknown_platform_falls_back_to_generic():
    """旧链接、手改的 hash 不该把页面打成 500。"""
    assert dialects.get("nope").key == "generic"
    assert dialects.get("").key == "generic"


def test_every_platform_is_reachable_from_the_page():
    page = (ROOT / "ai_drama" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function dialectOf" in page
    assert 'id="dialect"' in page
    assert "renderTab()" in page
    assert "mine.dropped" in page


def test_the_export_follows_the_selected_platform():
    page = (ROOT / "ai_drama" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    export = page.split("function exportPrompts")[1].split("function ")[0]
    assert "sh.dialects" in export
    assert "state.dialect" in export
    assert "f.title" in export


def test_render_all_covers_every_platform():
    got = dialects.render_all(None, _shot())
    assert set(got) == set(dialects.keys())
    assert got["kling"]["title"] == "可灵"
    assert got["jimeng"]["fields"]


def test_hailuo_takes_both_frames_but_has_no_negative_box():
    """海螺：首尾帧都认，但没有负向框 —— 两件事互不相关，别绑在一起。"""
    d = dialects.get("hailuo")
    assert d.has_last_frame is True
    assert d.has_negative is False
    shot = _shot()
    got = dialects.render(None, shot, "hailuo")
    keys = {f.key for f in got.fields}
    assert "last_frame" in keys, "海螺认尾帧，别把尾帧也丢掉"
    assert "negative" not in keys
    assert any("负向词" in x for x in got.dropped)


def test_platforms_without_a_last_frame_slot_say_where_the_tail_went():
    """Sora 只认首帧：尾帧不能当图给，要写清楚去哪了。

    Runway Gen-4.5 与 Veo 都有尾帧槽，所以它们**不该**出现在这条里 ——
    能力表是查官方文档得到的，不是按"洋平台大概都只给首帧"猜的。
    """
    for key in ("sora",):
        got = dialects.render(None, _shot(), key)
        assert "last_frame" not in {f.key for f in got.fields}, key
        assert any("尾帧" in x for x in got.dropped), key


def test_only_the_platforms_that_really_have_a_negative_box_get_one():
    """有负向框的：通用/可灵/Veo。其余一律丢掉并写明原因。"""
    shot = _shot()
    for key in ("generic", "kling", "veo"):
        got = dialects.render(None, shot, key)
        assert "negative" in {f.key for f in got.fields}, key
    for key in ("jimeng", "seedance", "hailuo", "runway", "sora"):
        got = dialects.render(None, shot, key)
        assert "negative" not in {f.key for f in got.fields}, key
        assert any("负向" in x for x in got.dropped), key


def test_english_platforms_admit_the_prompt_is_still_chinese():
    """不替用户翻译，但也不能让他以为我们翻了。"""
    got = dialects.render(None, _shot(), "veo")
    assert "中文" in got.tip


def test_every_dialect_is_reachable_from_the_catalog():
    from ai_drama.core import dialects as mod

    cat = {c["key"]: c for c in mod.catalog()}
    assert set(cat) == set(mod.keys())
    for key, item in cat.items():
        assert item["title"], key
        assert item["tip"], key  # 下拉框旁边那句话不能是空的
        assert item["lang"] in ("zh", "en"), key


def test_the_four_platforms_people_actually_use_are_all_here():
    """这条是产品承诺：可灵、即梦、豆包、海螺 —— 四个中文入口一个都不能少。"""
    for key in ("kling", "jimeng", "seedance", "hailuo"):
        assert key in dialects.keys(), key


# ---- 定妆照也走方言（它只有一段，但字数上限照样管） ------------------------

class _Char:
    def __init__(self, anchor):
        self.name = "林溪"
        self.anchor_prompt = anchor


def _long_anchor(n=400):
    # 五条规格 + 一段长相 + 画风，真实长度大约两百字；这里放大到确定超限
    return "角色长相描述。" * n


def test_a_short_anchor_is_left_alone():
    ch = _Char("28岁女性，齐肩黑发，深灰色风衣")
    r = dialects.render_anchor(ch, "jimeng")
    assert r.fields[0].text == ch.anchor_prompt
    assert r.fields[0].truncated is False
    assert r.dropped == []


def test_an_overlong_anchor_is_cut_at_the_platform_limit_and_says_so():
    ch = _Char(_long_anchor())
    r = dialects.render_anchor(ch, "jimeng")
    assert r.fields[0].truncated is True
    assert len(r.fields[0].text) <= dialects.get("jimeng").max_chars
    # 截了就明说，不偷偷砍掉末尾"五官与发际线完整"那一条
    assert any("定妆照" in x and "截断" in x for x in r.dropped)


def test_the_generic_platform_keeps_the_full_anchor():
    ch = _Char(_long_anchor(6))  # 600 字左右，通用是 4000
    assert dialects.render_anchor(ch, "generic").fields[0].truncated is False


def test_anchor_variants_only_lists_the_platforms_that_changed():
    # 只有一句、没有可删的东西：所有平台都一样，不必多发
    one_line = _Char("一句话定妆照")
    assert dialects.anchor_variants(one_line) == {}

    # 有画风句可以删时，非通用平台都多出一版紧凑版（哪怕没被截断）
    with_style = _Char("28岁女性，齐肩黑发，冷青色调，高对比胶片质感，"
                       "正面胸像，平视机位，纯色浅灰背景，均匀柔光")
    v = dialects.anchor_variants(with_style)
    assert "generic" not in v
    assert set(v) == set(dialects.keys()) - {"generic"}

    long = _Char(_long_anchor())
    variants = dialects.anchor_variants(long)
    assert "jimeng" in variants and "veo" in variants
    assert len(variants["jimeng"]["fields"]) == 2
    # 通用 4000 字，600 字不会被改 —— 没必要让接口多发一份
    assert "generic" not in variants


def test_the_compact_anchor_drops_the_style_sentence():
    """风格句跟长相抢权重，紧凑版把它整段删掉。"""
    ch = _Char("28岁女性，齐肩黑发，深灰色风衣，冷青色调，高对比胶片质感，"
               "正面胸像，平视机位，纯色浅灰背景，均匀柔光，85mm 浅景深，"
               "五官对称、发际线与耳廓完整")
    fields = {f.key: f for f in dialects.render_anchor(ch, "jimeng").fields}
    assert "anchor_compact" in fields
    short = fields["anchor_compact"].text
    assert "冷青色调" not in short
    assert "正面胸像" in short and "85mm" in short
    assert len(short) < len(fields["anchor"].text)


def test_the_generic_platform_gets_no_compact_version():
    ch = _Char("28岁女性，齐肩黑发，冷青色调，正面胸像，纯色背景，均匀柔光")
    keys = [f.key for f in dialects.render_anchor(ch, "generic").fields]
    assert keys == ["anchor"]
