"""技能装载：两级披露、规则预筛、冲突排序。

这一层最容易出的不是崩溃，而是**静默失效**：一篇技能因为
`stage` 拼错、`status` 忘了改、或者作用域写反了，从此再也没被选中过，
而你在怀疑模型。所以下面每一条测的都是"它还会不会被选上"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_drama.core.miniyaml import ParseError
from ai_drama.core.skills import (
    ACTIVE_LIMIT,
    SCOPE_ORDER,
    assemble,
    estimate_tokens,
    load_body,
    load_skills,
    parse_skill,
    render_index,
    select,
)


def _skill(tmp_path: Path, name: str, body: str = "正文", **fm) -> Path:
    """写一篇技能到 `<tmp_path>/skills/<name>.md`。只给非默认的字段。"""
    lines = ["---", f"name: {name}"]
    lines.append(f"description: {fm.pop('description', '这是一段足够长的描述，说明什么时候该用它。')}")
    for key, value in fm.items():
        if key == "stage":
            key = "stage"
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(f"# {name}")
    lines.append("")
    lines.append(body)
    base = tmp_path / "skills"
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{name}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ————————————————————————————————————————---- 解析


def test_frontmatter_is_parsed_and_body_is_excluded(tmp_path: Path):
    p = _skill(tmp_path, "showrunner", body="正文内容\n第二行",
               scope="pipeline_stage", applies_to=["短剧"], stage=["选题"],
               priority=70, owner="me", version="1.2.0")
    meta = parse_skill(p.read_text(encoding="utf-8"), p)
    assert meta.name == "showrunner"
    assert meta.scope == "pipeline_stage"
    assert meta.applies_to == ("短剧",)
    assert meta.stages == ("选题",)
    assert meta.priority == 70
    assert meta.owner == "me"
    assert meta.is_active
    # 正文不在元数据里 —— 这就是两级披露的全部意义
    assert "正文内容" not in str(meta.to_dict())
    assert "正文内容" in load_body(meta)
    assert estimate_tokens(load_body(meta)) > 0


def test_missing_frontmatter_still_loads_but_says_so(tmp_path: Path):
    """老文档（没有 frontmatter）不该直接不能用，但问题要记下来。"""
    p = tmp_path / "old.md"
    p.write_text("# 老文档\n\n没有 frontmatter。\n", encoding="utf-8")
    meta = parse_skill(p.read_text(encoding="utf-8"), p)
    assert meta.name == "old"
    assert meta.scope == "global"
    assert meta.is_active
    assert any("description" in x for x in meta.problems)
    assert any("owner" in x for x in meta.problems)


def test_unknown_frontmatter_key_raises(tmp_path: Path):
    """拼错一个键名会让作用域静默失效，所以必须炸。"""
    p = tmp_path / "x.md"
    p.write_text(
        "---\nname: x\ndescription: 这是一段足够长的描述文字。\nscherr: global\n---\n正文\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError, match="不认识的键"):
        parse_skill(p.read_text(encoding="utf-8"), p)


def test_bad_scope_and_status_raise(tmp_path: Path):
    p = tmp_path / "x.md"
    p.write_text("---\nname: x\ndescription: 这是一段足够长的描述文字。\nscope: 宇宙\n---\n",
                 encoding="utf-8")
    with pytest.raises(ParseError, match="scope"):
        parse_skill(p.read_text(encoding="utf-8"), p)

    p2 = tmp_path / "y.md"
    p2.write_text("---\nname: y\ndescription: 这是一段足够长的描述文字。\nstatus: 写完一半\n---\n",
                  encoding="utf-8")
    with pytest.raises(ParseError, match="status"):
        parse_skill(p2.read_text(encoding="utf-8"), p2)


def test_stage_and_stages_are_both_accepted(tmp_path: Path):
    """单数和复数都认 —— 但拼成别的键就炸。"""
    a = tmp_path / "a.md"
    a.write_text("---\nname: a\ndescription: 这是一段足够长的描述文字。\nstages: [剧本]\n---\n",
                 encoding="utf-8")
    assert parse_skill(a.read_text(encoding="utf-8"), a).stages == ("剧本",)


def test_underscore_and_readme_files_are_skipped(tmp_path: Path):
    _skill(tmp_path, "real")
    _skill(tmp_path, "_template")
    (tmp_path / "README.md").write_text("# 说明\n", encoding="utf-8")
    metas = load_skills(tmp_path)
    assert [m.name for m in metas] == ["real"]


def test_directory_form_is_loaded(tmp_path: Path):
    base = tmp_path / "skills"
    d = base / "drama-script"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: drama-script\ndescription: 微短剧剧本创作，写短剧或做分集大纲时用。\n---\n正文\n",
        encoding="utf-8",
    )
    metas = load_skills(tmp_path)
    assert any(m.name == "drama-script" for m in metas)


# ————————————————————————————————————————---- 预筛与排序


def test_scope_prefilter_happens_before_any_token_is_spent(tmp_path: Path):
    """不匹配的文档在进入模型视野之前就被挡掉，不消耗 token。"""
    rel = tmp_path / "rel.md"
    rel.write_text(
        "---\nname: rel\ndescription: 这是一段足够长的描述文字。\n"
        "applies_to: [短剧]\nstage: [剧本]\n---\n正文\n",
        encoding="utf-8",
    )
    other = tmp_path / "other.md"
    other.write_text(
        "---\nname: other\ndescription: 这是一段足够长的描述文字。\n"
        "applies_to: [短视频]\n---\n正文\n",
        encoding="utf-8",
    )
    metas = [parse_skill(rel.read_text(encoding="utf-8"), rel),
             parse_skill(other.read_text(encoding="utf-8"), other)]

    sel = select(metas, content_type="短剧", stage="剧本")
    assert [m.name for m in sel.chosen] == ["rel"]
    assert [m.name for m in sel.skipped_scope] == ["other"]
    # 被筛掉的要能说清楚为什么 —— 否则"没生效"无从查起
    assert "作用域不匹配" in sel.explain()


def test_no_stage_means_stage_scoped_skills_are_not_loaded(tmp_path: Path):
    """没指定环节时，带 stage 的技能不该被加载。

    否则"我没说我在哪个环节"会变成"把所有环节的技能都塞进来"。
    """
    p = tmp_path / "s.md"
    p.write_text(
        "---\nname: s\ndescription: 这是一段足够长的描述文字。\nstage: [剧本]\n---\n正文\n",
        encoding="utf-8",
    )
    meta = parse_skill(p.read_text(encoding="utf-8"), p)
    assert select([meta], content_type="短剧").chosen == []
    assert select([meta], content_type="短剧", stage="剧本").chosen


def test_inactive_status_is_never_loaded(tmp_path: Path):
    for status in ("draft", "placeholder"):
        p = tmp_path / f"{status}.md"
        p.write_text(
            f"---\nname: {status}\ndescription: 这是一段足够长的描述文字。\n"
            f"status: {status}\n---\n正文\n",
            encoding="utf-8",
        )
        meta = parse_skill(p.read_text(encoding="utf-8"), p)
        sel = select([meta])
        assert sel.chosen == []
        assert [m.name for m in sel.skipped_status] == [status]


def test_more_specific_scope_wins_the_tie(tmp_path: Path):
    """account > content_type > pipeline_stage > global。"""
    assert SCOPE_ORDER["account"] > SCOPE_ORDER["content_type"] > \
        SCOPE_ORDER["pipeline_stage"] > SCOPE_ORDER["global"]

    metas = []
    for i, scope in enumerate(("global", "pipeline_stage", "content_type", "account")):
        p = tmp_path / f"a{i}.md"
        p.write_text(
            f"---\nname: a{i}\ndescription: 这是一段足够长的描述文字。\n"
            f"scope: {scope}\npriority: 50\n---\n正文\n",
            encoding="utf-8",
        )
        metas.append(parse_skill(p.read_text(encoding="utf-8"), p))
    sel = select(metas)
    assert [m.scope for m in sel.chosen] == ["account", "content_type", "pipeline_stage"]


def test_priority_breaks_ties_within_the_same_scope(tmp_path: Path):
    metas = []
    for i, pri in enumerate((10, 90, 50)):
        p = tmp_path / f"p{i}.md"
        p.write_text(
            f"---\nname: p{i}\ndescription: 这是一段足够长的描述文字。\n"
            f"scope: global\npriority: {pri}\n---\n正文\n",
            encoding="utf-8",
        )
        metas.append(parse_skill(p.read_text(encoding="utf-8"), p))
    sel = select(metas)
    assert [m.priority for m in sel.chosen] == [90, 50, 10]


def test_active_limit_is_enforced(tmp_path: Path):
    metas = []
    for i in range(ACTIVE_LIMIT + 2):
        p = tmp_path / f"m{i}.md"
        p.write_text(
            f"---\nname: m{i}\ndescription: 这是一段足够长的描述文字。\n---\n正文\n",
            encoding="utf-8",
        )
        metas.append(parse_skill(p.read_text(encoding="utf-8"), p))
    sel = select(metas)
    assert len(sel.chosen) == ACTIVE_LIMIT
    assert len(sel.skipped_budget) == 2


def test_budget_skips_the_ones_that_would_blow_it(tmp_path: Path):
    small = tmp_path / "small.md"
    small.write_text(
        "---\nname: small\ndescription: 这是一段足够长的描述文字。\npriority: 99\n---\n短\n",
        encoding="utf-8",
    )
    big = tmp_path / "big.md"
    big.write_text(
        "---\nname: big\ndescription: 这是一段足够长的描述文字。\npriority: 10\n---\n"
        + "字" * 20000 + "\n",
        encoding="utf-8",
    )
    metas = [parse_skill(small.read_text(encoding="utf-8"), small),
             parse_skill(big.read_text(encoding="utf-8"), big)]
    sel = select(metas, budget_tokens=200)
    assert [m.name for m in sel.chosen] == ["small"]
    assert [m.name for m in sel.skipped_budget] == ["big"]
    assert "超" in sel.explain()


def test_the_first_skill_loads_even_if_it_is_oversized(tmp_path: Path):
    """一篇超长技能不该把这一轮变成"零技能可用"—— 失败方式是静默的。

    超长的问题由 `skills --check` 报给作者，不在运行时偷偷砍掉指导。
    """
    big = tmp_path / "big.md"
    big.write_text(
        "---\nname: big\ndescription: 这是一段足够长的描述文字。\n---\n"
        + "字" * 20000 + "\n",
        encoding="utf-8",
    )
    meta = parse_skill(big.read_text(encoding="utf-8"), big)
    assert [m.name for m in select([meta], budget_tokens=10).chosen] == ["big"]


# ————————————————————————————————————————---- 渲染


def test_index_carries_only_descriptions(tmp_path: Path):
    """常驻上下文的是索引，不是正文。这是两级披露省下来的东西。"""
    p = tmp_path / "s.md"
    body = "字" * 3000
    p.write_text(
        "---\nname: s\ndescription: 写剧本时用的那份指导文档。\n---\n" + body + "\n",
        encoding="utf-8",
    )
    meta = parse_skill(p.read_text(encoding="utf-8"), p)
    index = render_index([meta])
    assert "s:" in index
    assert "写剧本时用的那份指导文档" in index
    assert body not in index
    assert estimate_tokens(index) < estimate_tokens(body) / 10


def test_index_notes_a_missing_description(tmp_path: Path):
    p = tmp_path / "s.md"
    p.write_text("---\nname: s\n---\n正文\n", encoding="utf-8")
    meta = parse_skill(p.read_text(encoding="utf-8"), p)
    assert "不会被选中" in render_index([meta])


def test_assemble_marks_precedence_only_when_it_can_conflict(tmp_path: Path):
    """两篇作用域不同才写优先声明。每篇都写一遍是噪音。"""
    metas = []
    for i, scope in enumerate(("global", "content_type")):
        p = tmp_path / f"a{i}.md"
        p.write_text(
            f"---\nname: a{i}\ndescription: 这是一段足够长的描述文字。\n"
            f"scope: {scope}\n---\n正文{i}\n",
            encoding="utf-8",
        )
        metas.append(parse_skill(p.read_text(encoding="utf-8"), p))

    text, sel = assemble(metas)
    assert len(sel.chosen) == 2
    # 更具体的那篇不需要声明（它就是赢家），较泛的那篇要写明让位
    assert "以 a1 为准" in text
    assert "以 a0 为准" not in text


def test_assemble_returns_empty_when_nothing_matches(tmp_path: Path):
    p = tmp_path / "s.md"
    p.write_text(
        "---\nname: s\ndescription: 这是一段足够长的描述文字。\n"
        "applies_to: [短视频]\n---\n正文\n",
        encoding="utf-8",
    )
    meta = parse_skill(p.read_text(encoding="utf-8"), p)
    text, sel = assemble([meta], content_type="短剧")
    assert text == ""
    assert sel.skipped_scope


def test_shipped_skills_all_have_frontmatter_and_load():
    """仓库里真正交付的那七篇必须全部能被解析。

    任何人新加一篇忘了写 frontmatter，这条会先炸 —— 而不是等到
    某次跑流水线时才发现"这轮的指导文档怎么少了"。
    """
    repo_root = Path(__file__).resolve().parents[1]
    base = repo_root / "skills"
    if not base.is_dir():
        pytest.skip("仓库里没有 skills/")
    metas = load_skills(repo_root)
    assert len(metas) >= 7
    names = {m.name for m in metas}
    assert {"pipeline", "showrunner", "screenwriter", "storyboard",
            "character-bible", "shot-prompt", "qc"} <= names
    for m in metas:
        assert m.description, f"{m.name} 没有 description，永远不会被选中"
        assert m.owner, f"{m.name} 没写 owner"
    # 常驻索引必须小到可以忽略
    assert estimate_tokens(render_index(metas)) < 600
