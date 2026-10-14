"""质检：三态判定 + 规则作用域。

这一组测试守的是**最容易骗过自己的一类问题**：

    "没查成"被当成"通过"。

一个空的产物目录、一个读不出来的时长、一个压根没接上的检查器，
都会被顺手写成"没有发现问题，继续"。所以 `unchecked` 是单独一态，
且**默认拦住 done**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_drama.core.models import Episode, Scene, Shot
from ai_drama.core.qc import (
    Finding,
    QcChecker,
    QcReport,
    QcRules,
    QcState,
    Severity,
    _mini_yaml,
    has_cjk,
    rules_path,
)


def _ep(*shots: Shot) -> Episode:
    ep = Episode(run_id="r1", idea="测试")
    sc = Scene(scene_id="sc01", title="场一")
    for s in shots:
        sc.shots.append(s)
    ep.scenes.append(sc)
    return ep


def _shot(order: int = 0, **kw) -> Shot:
    base = dict(scene_id="sc01", order=order, action="推门而入", duration=4.0)
    base.update(kw)
    return Shot(**base)


NO_ASSETS = lambda shot: (True, "")          # noqa: E731  测试里显式表示"不查素材"


# ————————————————————————————————————————---- 三态


def test_pass_requires_no_findings_at_all():
    ep = _ep(
        _shot(0, framing="establishing", action="空镜建立环境，风声灌进来", dialogue=""),
        _shot(1, framing="medium", action="她回头看向门口的方向", dialogue="谁在那"),
        _shot(2, framing="close", action="门缝里出现一只手的影子", dialogue=""),
    )
    rep = QcChecker(assets=NO_ASSETS).check(ep)
    assert rep.state is QcState.PASS
    assert rep.passed is True


def test_unchecked_is_not_pass():
    """**本文件最重要的一条。**

    没有素材检查器 => 素材结论是"没查成"，不是"通过"。
    这样"还没查"和"查过没问题"在结果里永远不会长得一样。
    """
    ep = _ep(_shot(0))
    rep = QcChecker(assets=None).check(ep)      # 没给检查器 = 查不成
    assert rep.state is QcState.UNCHECKED
    assert rep.passed is False
    assert rep.assets_checked is False
    assert rep.unchecked, "必须显式列出没查成的项"


def test_checker_crash_is_unchecked_not_pass():
    """检查器自己抛错时，结论是"没查成"，绝不是"没问题"。"""
    def boom(shot):
        raise RuntimeError("磁盘掉了")

    ep = _ep(_shot(0))
    rep = QcChecker(assets=boom).check(ep)
    assert rep.state is QcState.UNCHECKED
    assert rep.assets_checked is False
    assert any("磁盘掉了" in f.message for f in rep.unchecked)


def test_fail_beats_unchecked():
    """确定的坏消息不能被"没查成"盖住 —— 汇总顺序很重要。"""
    ep = _ep(_shot(0, duration=99.0))          # 时长越界 = BLOCK
    rep = QcChecker(assets=None).check(ep)
    assert rep.state is QcState.FAIL
    assert rep.blocks


def test_empty_episode_is_unchecked():
    ep = Episode(run_id="r", idea="空")
    rep = QcChecker(assets=NO_ASSETS).check(ep)
    assert rep.state is QcState.UNCHECKED
    assert "镜头" in rep.blocked_reason


def test_report_roundtrip_is_serializable():
    ep = _ep(_shot(0, duration=99.0))
    rep = QcChecker(assets=None).check(ep)
    d = rep.to_dict()
    assert d["state"] in {"pass", "fail", "unchecked"}
    assert d["passed"] is False
    assert isinstance(d["findings"], list)
    assert d["findings"][0]["severity"] in {"block", "warn", "info"}


# ————————————————————————————————————————---- 单项规则


def test_long_dialogue_is_flagged_but_does_not_block():
    """台词太长是创作问题，报出来给人看，但别拦住整条流水线。"""
    ep = _ep(_shot(0, dialogue="你" * 200))
    rep = QcChecker(assets=NO_ASSETS).check(ep)
    assert any(f.rule == "dialogue-length" for f in rep.findings)
    assert not rep.blocks


def test_cjk_and_latin_have_separate_limits():
    """一个中文字 ≈ 一个英文单词的三分之一，用同一个阈值量两种语言必错一边。"""
    assert has_cjk("你好") is True
    assert has_cjk("hello") is False

    rules = QcRules(max_dialogue_cjk=5, max_dialogue_latin=100)
    cjk = QcChecker(rules, assets=NO_ASSETS).check(_ep(_shot(0, dialogue="你好世界啊喂")))
    latin = QcChecker(rules, assets=NO_ASSETS).check(_ep(_shot(0, dialogue="hello")))

    assert any(f.rule == "dialogue-length" for f in cjk.findings)
    assert not any(f.rule == "dialogue-length" for f in latin.findings)


def test_duration_bounds_block():
    rules = QcRules(min_shot_seconds=1.0, max_shot_seconds=10.0)
    short = QcChecker(rules, assets=NO_ASSETS).check(_ep(_shot(0, duration=0.5)))
    long = QcChecker(rules, assets=NO_ASSETS).check(_ep(_shot(0, duration=20.0)))
    assert short.blocks and long.blocks


def test_auditory_info_in_visual_prompt_blocks():
    """参考项目的概念性错误：把台词/音效写进画面提示词。

    它会把听觉信息喂给视频模型，污染生成结果，而且是 block 级 ——
    因为这类提示词是**批量**写坏的，不拦住会整片都脏。
    """
    ep = _ep(_shot(0, action="她抬头看向窗外，配乐渐强"))
    rep = QcChecker(assets=NO_ASSETS).check(ep)
    assert rep.state is QcState.FAIL
    assert any(f.rule == "auditory-in-visual" for f in rep.blocks)


def test_identical_visual_prompts_are_flagged():
    same = "她在楼梯拐角停住脚步回头张望"
    ep = _ep(_shot(0, action=same), _shot(1, action=same), _shot(2, action=same))
    rep = QcChecker(assets=NO_ASSETS).check(ep)
    assert any(f.rule == "duplicate-visual" for f in rep.findings)


def test_single_framing_scene_is_reported():
    ep = _ep(*[_shot(i, framing="medium") for i in range(4)])
    rep = QcChecker(assets=NO_ASSETS).check(ep)
    assert any(f.rule == "framing-diversity" for f in rep.findings)


def test_scene_hook_check():
    rules = QcRules(require_scene_hook=True)
    ep = _ep(_shot(0, action="她走进房间", dialogue=""))
    ep.scenes[0].shots[-1].action = ""
    rep = QcChecker(rules, assets=NO_ASSETS).check(ep)
    assert any(f.rule == "scene-hook" for f in rep.findings)


# ————————————————————————————————————————---- 规则作用域（血泪教训）


def test_rule_scope_lets_you_exclude_a_content_type():
    """参考项目的血案：广告法极限词规则打在短剧台词上，
    模型花三个会话、四十多元把 60 集重写一遍去删「最强」「绝对」。

    所以规则必须能限定作用域。
    """
    drama_only = QcRules(applies_to=("短剧",))
    assert drama_only.matches(content_type="短剧") is True
    assert drama_only.matches(content_type="广告") is False
    # 空作用域 = 全部生效（默认值）
    assert QcRules().matches(content_type="广告") is True


def test_rules_load_from_yaml_roundtrip(tmp_path: Path):
    p = tmp_path / "qc_rules.yaml"
    p.write_text(
        "version: '2'\n"
        "max_dialogue_cjk: 12\n"
        "min_shot_seconds: 1.5\n"
        "severity_duration: warn\n"
        "applies_to:\n"
        "  - 短剧\n"
        "  - 短视频\n"
        "forbidden_in_visual:\n"
        "  - 旁白\n",
        encoding="utf-8",
    )
    rules = QcRules.load(p)
    assert rules.version == "2"
    assert rules.max_dialogue_cjk == 12
    assert rules.min_shot_seconds == 1.5
    assert rules.severity_duration is Severity.WARN
    assert rules.applies_to == ("短剧", "短视频")
    assert rules.forbidden_in_visual == ("旁白",)


def test_unknown_rule_key_raises_instead_of_being_ignored(tmp_path: Path):
    """改了配置没生效 = 最难发现的一类问题。所以不认识的键要报错。"""
    p = tmp_path / "qc_rules.yaml"
    p.write_text("max_dialogue_cjk: 10\nmax_dialgoue_latin: 99\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_dialgoue_latin"):
        QcRules.load(p)


def test_missing_rules_file_falls_back_to_defaults(tmp_path: Path):
    rules = QcRules.load(tmp_path / "nope.yaml")
    assert rules.max_dialogue_cjk > 0


def test_mini_yaml_rejects_unsupported_syntax():
    with pytest.raises(ValueError):
        _mini_yaml("这是没有冒号的一行")
    with pytest.raises(ValueError):
        _mini_yaml("  - 一个没有归属的列表项\n")


def test_empty_bracket_list_is_an_empty_scope_not_a_string():
    """`applies_to: []` 必须是空作用域（= 全部生效）。

    如果它被解析成字符串 "[]"，作用域会变成"只对名叫 [] 的内容形态生效" ——
    所有规则静默失效，而配置文件看起来完全正常。
    """
    parsed = _mini_yaml("applies_to: []\n")
    assert parsed == {"applies_to": []}
    rules = QcRules(applies_to=tuple(parsed["applies_to"]))
    assert rules.matches(content_type="短剧") is True
    assert rules.matches(content_type="广告") is True


def test_shipped_rules_file_loads_and_is_scope_free(tmp_path: Path):
    """仓库里真正交付的那份 qc_rules.yaml 必须能被解析。

    这条测试的作用是：任何人改了 config/qc_rules.yaml 的写法而解析器不支持，
    CI 会先炸，而不是等到某次出片时才发现规则根本没生效。
    """
    from ai_drama.core.qc import rules_path as _rp

    repo_root = Path(__file__).resolve().parents[1]
    path = _rp(repo_root)
    if not path.exists():                     # 允许有人删掉它，走内置默认值
        pytest.skip("仓库里没有 config/qc_rules.yaml")
    rules = QcRules.load(path)
    assert rules.applies_to == ()
    assert rules.stages == ()
    assert rules.version == "1"
    assert rules.severity_duration is Severity.BLOCK
    assert rules.severity_auditory is Severity.BLOCK
    assert rules.severity_dialogue is Severity.WARN
    assert "旁白" in rules.forbidden_in_visual
    assert rules.max_shot_seconds > rules.min_shot_seconds


def test_rules_path_points_at_config_dir(tmp_path: Path):
    assert rules_path(tmp_path) == tmp_path / "config" / "qc_rules.yaml"


# ————————————————————————————————————————---- 渲染


def test_render_mentions_unchecked_explicitly():
    ep = _ep(_shot(0))
    text = QcChecker(assets=None).check(ep).render()
    assert "UNCHECKED" in text
    assert "未查成" in text


def test_finding_render_marks_unchecked_differently():
    f = Finding(rule="x", severity=Severity.BLOCK, message="m", unchecked=True)
    assert "[?]" in f.render()
    f2 = Finding(rule="x", severity=Severity.BLOCK, message="m")
    assert "[X]" in f2.render()
