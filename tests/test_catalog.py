"""模型目录与配置解析器。

守两件事：

  1. **能力上限来自数据。** 单段时长上限如果不是从目录读的，接上真厂商时
     超长的镜头会被静默截断 —— 你只会看到"12 秒的镜头出来只有 8 秒"。
  2. **配置写错要炸，不要静默忽略。** 解析器最常见的失败方式不是崩溃，
     而是某个键从来没被读到过，而你还在怀疑模型和网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_drama.core.catalog import (
    ModelCatalog,
    ModelEntry,
    RefFields,
    catalog_path,
    load_catalog,
)
from ai_drama.core.miniyaml import MAX_DEPTH, ParseError, parse


# ————————————————————————————————————————---- 解析器


def test_parse_scalars_lists_and_nested_maps():
    text = (
        "version: \"1\"\n"
        "count: 3\n"
        "ratio: 0.6\n"
        "on: true\n"
        "off: false\n"
        "empty: []\n"
        "inline: [a, b, c]\n"
        "block:\n"
        "  - x\n"
        "  - y\n"
        "outer:\n"
        "  inner:\n"
        "    deep: 5\n"
    )
    out = parse(text, source="t.yaml")
    assert out["version"] == "1"          # 带引号 -> 字符串，不是 int 1
    assert out["count"] == 3
    assert out["ratio"] == 0.6
    assert out["on"] is True
    assert out["off"] is False
    assert out["empty"] == []
    assert out["inline"] == ["a", "b", "c"]
    assert out["block"] == ["x", "y"]
    assert out["outer"]["inner"]["deep"] == 5


def test_quoted_numbers_stay_strings():
    """版本号、ID 这类东西不能被解析成数字。

    一旦变成数字，`2` 和 `"2"` 会在报告、字典键、日志里长得不一样，
    而这类不一致查起来最费劲。
    """
    assert parse('v: "2"\n')["v"] == "2"
    assert parse("v: 2\n")["v"] == 2


def test_sibling_list_items_share_an_indent_level():
    """列表项之间是并列的 —— 第一版解析器在这里把每个列表都判错了。

    当时的规则是"子项必须比上一行更深"，于是列表的第二个 `- ` 起全部报错。
    现在用的是容器栈：缩进大于栈顶就属于它，小于等于就弹出。
    """
    out = parse("a:\n  - 1\n  - 2\n  - 3\nb: 9\n", source="t.yaml")
    assert out["a"] == [1, 2, 3]
    assert out["b"] == 9


def test_comment_markers_inside_values_survive():
    """不带空格的井号是值的一部分；前面有空白的井号起注释作用。"""
    out = parse("strengths: 画质最好#1首选\n", source="t.yaml")
    assert out["strengths"] == "画质最好#1首选"

    out2 = parse("max_duration: 8  # 秒\n# 整行注释\n", source="t.yaml")
    assert out2["max_duration"] == 8


def test_unsupported_syntax_raises_with_line_number():
    with pytest.raises(ParseError) as ei:
        parse("第一行: 1\n这是没有冒号的一行\n", source="t.yaml")
    assert "第 2 行" in str(ei.value)

    with pytest.raises(ParseError) as ei2:
        parse("  - 孤儿列表项\n", source="t.yaml")
    assert "第 1 行" in str(ei2.value)

    with pytest.raises(ParseError) as ei3:
        parse("a: 1\nb: 2\n  缩进错地方: 3\n", source="t.yaml")
    assert "第 3 行" in str(ei3.value)


def test_depth_limit_is_enforced_not_silently_flattened():
    lines = ["l0:"]
    for depth in range(1, MAX_DEPTH + 2):
        lines.append("  " * depth + f"l{depth}:")
    with pytest.raises(ParseError, match="嵌套"):
        parse("\n".join(lines) + "\n", source="t.yaml")


# ————————————————————————————————————————---- 参考素材字段


def test_ref_fields_sends_both_image_names():
    """两个字段名都送，防"零参考"。

    有些聚合服务会安静地丢掉别名 —— 于是你传了参考图、模型没收到、
    出图漂了，而你会以为是"这个模型一致性不行"。
    """
    rf = RefFields()
    assert rf.image_field_names() == ("image", "image_urls")


def test_ref_fields_alias_can_be_turned_off():
    rf = RefFields(image_alias="")
    assert rf.image_field_names() == ("image",)


def test_ref_fields_dedupes_identical_names():
    rf = RefFields(image="image", image_alias="image")
    assert rf.image_field_names() == ("image",)


# ————————————————————————————————————————---- 目录


def _write_catalog(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    p = cfg / "models.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_catalog_falls_back_but_says_so(tmp_path: Path):
    """退回内置目录必须留痕。

    内置目录里没有你的模型，也就没有单段时长上限 —— 照着它跑，
    超长镜头不会被拆，接上真厂商就会拿到被截断的片段。
    所以"我在用哪份目录"不能是静默的。
    """
    cat = load_catalog(tmp_path)
    assert cat.problem
    assert cat.default_model("video").id == "dryrun"


def test_catalog_roundtrip_with_caps(tmp_path: Path):
    p = _write_catalog(tmp_path, (
        "version: \"2\"\n"
        "ref_fields:\n"
        "  image: img\n"
        "  image_alias: \"\"\n"
        "defaults:\n"
        "  video: m1\n"
        "models:\n"
        "  m1:\n"
        "    kind: video\n"
        "    provider: acme\n"
        "    tier: quality\n"
        "    cost: 5\n"
        "    max_duration: 6\n"
        "    resolutions: [1080p]\n"
        "    aspect_ratios: [9:16]\n"
        "    supports_image_input: true\n"
        "    strengths: 便宜好用\n"
    ))
    cat = ModelCatalog.load(p)
    assert cat.problem == ""
    assert cat.version == "2"
    assert cat.ref_fields.image_field_names() == ("img",)
    entry = cat.get("m1")
    assert entry.tier == "quality"
    assert entry.max_duration == 6
    assert entry.resolutions == ("1080p",)
    caps = cat.caps_for_model("m1", width=1080, height=1920, fps=30)
    assert caps.max_duration == 6
    assert caps.size == (1080, 1920)
    assert "6" in caps.describe()
    assert "便宜好用" in caps.describe()
    assert cat.caps_for_provider("acme") is not None
    assert cat.default_model("video").id == "m1"


def test_unknown_model_field_raises(tmp_path: Path):
    """同质检规则：不认识的键报错，不静默忽略。"""
    p = _write_catalog(tmp_path, (
        "models:\n"
        "  m1:\n"
        "    kind: video\n"
        "    max_duraton: 6\n"      # 少了一个 i
    ))
    with pytest.raises(ParseError, match="max_duraton"):
        ModelCatalog.load(p)


def test_unknown_kind_and_tier_raise(tmp_path: Path):
    p = _write_catalog(tmp_path, "models:\n  m1:\n    kind: 全息投影\n")
    with pytest.raises(ParseError, match="kind"):
        ModelCatalog.load(p)
    p2 = _write_catalog(tmp_path, "models:\n  m1:\n    kind: video\n    tier: 超值\n")
    with pytest.raises(ParseError, match="tier"):
        ModelCatalog.load(p2)


def test_default_model_prefers_configured_then_cheapest():
    cat = ModelCatalog(models={
        "expensive": ModelEntry(id="expensive", kind="video", cost=5),
        "cheap": ModelEntry(id="cheap", kind="video", cost=1),
    })
    assert cat.default_model("video").id == "cheap"       # 没配就取最便宜的
    cat.defaults["video"] = "expensive"
    assert cat.default_model("video").id == "expensive"   # 配了就听配置的


def test_catalog_path_is_under_config():
    assert catalog_path(Path("repo")) == Path("repo") / "config" / "models.yaml"


def test_bundle_gets_caps_from_the_catalog(tmp_path: Path, monkeypatch):
    """注册表兜底：适配器没声明上限时，用目录里同名条目的上限。

    这是"忘了写"和"故意不限制"之间的区别 —— 前者会让超长镜头被静默截断。
    """
    from ai_drama.core.catalog import ModelCatalog
    from ai_drama.providers import registry
    from ai_drama.providers.base import DEFAULT_CAPS, VideoProvider, caps_of

    class AcmeVideo(VideoProvider):
        name = "acme"

        def submit(self, request):          # pragma: no cover
            return "j"

        def poll(self, job_id, *, timeout=300.0):    # pragma: no cover
            return "failed", {}, None

        def fetch(self, job_id, dest):      # pragma: no cover
            return {}

    registry._REGISTRY["video"]["acme"] = AcmeVideo
    try:
        assert caps_of(AcmeVideo()) is DEFAULT_CAPS     # 自己什么都没声明
        cat = ModelCatalog(models={
            "acme-v1": ModelEntry(id="acme-v1", kind="video", provider="acme", max_duration=5.0),
        })
        bundle = registry.build_bundle(video=AcmeVideo(), catalog=cat)
        assert caps_of(bundle.video).max_duration == 5.0
    finally:
        registry._REGISTRY["video"].pop("acme", None)


def test_shipped_catalog_parses_and_declares_video_caps():
    """仓库里真正交付的那份 models.yaml 必须能解析。

    任何人改了它的写法而解析器不支持，这条会先炸 ——
    而不是等到某次出片时才发现能力上限根本没读进来。
    """
    repo_root = Path(__file__).resolve().parents[1]
    p = catalog_path(repo_root)
    if not p.exists():
        pytest.skip("仓库里没有 config/models.yaml")
    cat = ModelCatalog.load(p)
    assert cat.problem == ""
    assert cat.get("dryrun") is not None
    assert cat.get("dryrun-short").max_duration == 4
    # 至少要有一条视频模型声明了上限，否则"拆分"这条路径永远走不到
    assert any(m.max_duration for m in cat.by_kind("video"))
    assert cat.ref_fields.image_field_names()
    # 默认视频模型必须是能在零 key 下跑起来的那条
    assert cat.default_model("video").provider == "dryrun"
