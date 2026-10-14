"""多张脸谁当准。

这一块的判错代价不是"这张图不好看"，而是**后面十几集全用错脸**，
而且等你发现时已经渲完了。所以它全是纯函数、全部可离线测。
"""

from __future__ import annotations

from ai_drama.core.faces import (
    KIND_RANK,
    SOURCE_RANK,
    FaceCandidate,
    audit_lines,
    conflict_question,
    decide,
    group_candidates,
    matches_character,
    pick_baseline,
)


def _c(asset_id: str, **kw) -> FaceCandidate:
    kw.setdefault("character", "阿蛛")
    return FaceCandidate(asset_id=asset_id, **kw)


def _others(*ids: str, **kw) -> list:
    """裁决要看的候选必须**全都在 `cands` 里**。

    `decide()` 遍历的是候选列表，不是 verdicts 的键 —— verdicts 只是给
    每个候选的判定结果。只把基准放进 cands、把别的只写进 verdicts，
    表现就是"所有候选都跟基准一致"（其实一个都没比过）。
    """
    return [_c(i, source="asset", kind="角色", **kw) for i in ids]


# ————————————————————————————————————————---- 排序


def test_rank_order_is_source_then_kind_then_recency():
    assert SOURCE_RANK["user"] < SOURCE_RANK["pack"] < SOURCE_RANK["asset"]
    assert KIND_RANK["角色"] < KIND_RANK[""] < KIND_RANK["服装"]

    # 用户钉的，哪怕很老，也压过参考图包和产物
    assert _c("old_user", source="user", kind="角色", seq=1).rank < \
        _c("new_pack", source="pack", kind="角色", seq=999).rank
    # 同一来源里，主形象压过服装图 —— 服装图自己就是照着主形象生的
    assert _c("face", source="pack", kind="角色", seq=1).rank < \
        _c("outfit", source="pack", kind="服装", seq=999).rank
    # 完全同级时新的在前
    assert _c("new", source="asset", kind="角色", seq=9).rank < \
        _c("old", source="asset", kind="角色", seq=2).rank


def test_pick_baseline_explains_itself():
    keeper, why = pick_baseline([_c("a", source="asset"), _c("u", source="user")])
    assert keeper.asset_id == "u"
    assert "钉" in why


def test_pick_baseline_warns_when_only_outfits_exist():
    """只有服装图时脸是不准的 —— 这一点必须说出来。"""
    keeper, why = pick_baseline([_c("o", source="asset", kind="服装")])
    assert keeper.asset_id == "o"
    assert "服装" in why and "脸可能不准" in why


def test_pick_baseline_on_empty():
    keeper, why = pick_baseline([])
    assert keeper is None and why


def test_label_is_recognizable_by_a_human():
    assert "钉" in _c("as_1", source="user", kind="角色").label()
    assert "as_1" in _c("as_1", source="asset").label()


# ————————————————————————————————————————---- 裁决


def test_user_pinned_baseline_is_never_questioned():
    """用户已经表过态 —— 自动挑，不问。"""
    cands = [_c("u", source="user", kind="角色")] + _others("a1")
    audit = decide("阿蛛", cands, {"a1": (False, 3, ["换脸了"])},
                   baseline=cands[0], why="用户钉的")
    assert audit.keeper.asset_id == "u"
    assert not audit.ambiguous
    assert audit.conflicted           # 有另一张脸，如实报告
    assert audit.drift[0].asset_id == "a1"
    assert audit.issues["a1"] == ["换脸了"]


def test_pack_baseline_is_never_questioned():
    cands = [_c("p", source="pack", kind="角色")] + _others("a1", "a2")
    audit = decide("阿蛛", cands, {"a1": (False, 2, []), "a2": (False, 4, [])},
                   baseline=cands[0], why="参考图包在用的")
    assert not audit.ambiguous


def test_majority_agrees_with_an_unattested_baseline_so_no_question():
    cands = [_c("b", source="asset", kind="角色", seq=9)] + _others("a1", "a2", "a3")
    verdicts = {"a1": (True, 9, []), "a2": (True, 8, []), "a3": (False, 3, [])}
    audit = decide("阿蛛", cands, verdicts, baseline=cands[0], why="产物里最新的")
    assert not audit.ambiguous
    assert len(audit.same) == 2 and len(audit.drift) == 1


def test_split_faces_with_no_attested_baseline_must_ask_the_human():
    """没人表过态、又是各自成群 —— 猜错的代价是后面十几集全用错脸。"""
    cands = [_c("b", source="asset", kind="角色", seq=9)] + _others("a1", "a2")
    verdicts = {"a1": (False, 4, ["脸型不同"]), "a2": (False, 5, ["眼睛不同"])}
    audit = decide("阿蛛", cands, verdicts, baseline=cands[0], why="产物里最新的")
    assert audit.ambiguous
    assert audit.needs_human
    assert "定不了谁是准的" in audit.why


def test_tie_is_not_a_majority():
    """一致的数量没有严格过半时不算多数派。"""
    cands = [_c("b", source="asset", kind="角色")] + _others("a1", "a2")

    # 1 张一致 + 基准自身 = 2，对面 1 张 —— 2 > 1 成立，所以不算歧义
    audit = decide("阿蛛", cands, {"a1": (True, 9, []), "a2": (False, 3, [])},
                   baseline=cands[0], why="x")
    assert not audit.ambiguous

    # 两张都不一致 —— 1（基准自己）> 2 不成立
    audit2 = decide("阿蛛", cands, {"a1": (False, 9, []), "a2": (False, 3, [])},
                    baseline=cands[0], why="x")
    assert audit2.ambiguous


def test_an_unjudged_candidate_is_reported_not_silently_same():
    """没判过 ≠ 同一人。不记这一笔，它看起来就像已经确认过了。"""
    cands = [_c("b", source="asset", kind="角色")] + _others("a1")
    audit = decide("阿蛛", cands, {}, baseline=cands[0], why="x")
    assert all(c.asset_id != "a1" for c in audit.same)
    assert any("没判过" in " ".join(v) for v in audit.issues.values())


def test_no_candidates_is_skipped_not_failed():
    audit = decide("阿蛛", [], {}, baseline=None, why="")
    assert audit.skipped
    assert audit.keeper is None
    assert not audit.ambiguous


def test_the_baseline_itself_is_not_scored_against_itself():
    cands = [_c("b", source="user")]
    audit = decide("阿蛛", cands, {"b": (False, 0, [])}, baseline=cands[0], why="x")
    assert audit.same == [] and audit.drift == []


# ————————————————————————————————————————---- 归堆与名字匹配


def test_group_candidates_ignores_unknown_names_and_sorts():
    rows = [
        _c("x", character="阿蛛", source="asset", kind="角色", seq=1),
        _c("y", character="阿蛛", source="user", kind="角色", seq=5),
        _c("z", character="路人甲"),
    ]
    grouped = group_candidates(rows, ["阿蛛"])
    assert set(grouped) == {"阿蛛"}
    assert [c.asset_id for c in grouped["阿蛛"]] == ["y", "x"]


def test_matches_character_respects_a_longer_name():
    """`朱锦` 与 `朱锦娘` 同时在剧里 —— 不能把娘的脸当成锦的基准。"""
    assert matches_character("朱锦站在门口", "朱锦", ["朱锦", "朱锦娘"])
    assert not matches_character("朱锦娘站在门口", "朱锦", ["朱锦", "朱锦娘"])
    assert matches_character("朱锦娘站在门口", "朱锦娘", ["朱锦", "朱锦娘"])


def test_matches_character_is_substring_based_for_better_or_worse():
    """纯子串匹配是已知的薄弱点，至少把行为固定下来。"""
    assert matches_character("林晚推门", "林晚", [])
    assert not matches_character("林晚推门", "陆离", [])
    assert not matches_character("", "林晚", [])
    assert not matches_character("林晚推门", "", [])


# ————————————————————————————————————————---- 给人看的输出


def test_audit_lines_are_one_line_when_there_is_no_conflict():
    keep = _c("only", source="user")
    audit = decide("阿蛛", [keep], {}, baseline=keep, why="用户钉的")
    lines = audit_lines([audit])
    assert len(lines) == 1 and lines[0].startswith("[OK]")


def test_audit_lines_mark_ambiguity_differently_from_a_conflict():
    cands = [_c("b", source="asset", kind="角色", seq=9)] + _others("a1", "a2")
    ok = decide("阿蛛", cands, {"a1": (False, 4, ["脸型不同"]), "a2": (True, 8, [])},
                baseline=cands[0], why="x")
    unsure = decide("阿蛛", cands,
                    {"a1": (False, 4, []), "a2": (False, 5, [])},
                    baseline=cands[0], why="x")
    assert audit_lines([ok])[0].startswith("[!]")
    assert audit_lines([unsure])[0].startswith("[?]")


def test_audit_lines_report_a_skip():
    assert audit_lines([decide("阿蛛", [], {}, None, "")])[0].startswith("[·]")


def test_conflict_question_only_asks_about_the_undecidable_ones():
    """把没冲突的角色也塞进问题里，用户会在真正要决定的那条上滑过去。"""
    cands_ok = [_c("u", source="user", character="已定")] + [_c("x", character="已定")]
    ok = decide("已定", cands_ok, {"x": (False, 3, [])}, baseline=cands_ok[0], why="用户钉的")

    cands_bad = [_c("b", source="asset", character="定不了", seq=9)] + \
        [_c("a1", character="定不了"), _c("a2", character="定不了")]
    bad = decide("定不了", cands_bad,
                 {"a1": (False, 4, []), "a2": (False, 5, [])},
                 baseline=cands_bad[0], why="x")

    question = conflict_question([ok, bad])
    assert "定不了" in question
    assert "已定" not in question
    assert "a1" in question and "a2" in question
    assert conflict_question([ok]) == ""
