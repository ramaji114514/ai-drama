"""多张脸谁当准：归拢、定基准、裁决。

# 现实里的样子

角色「阿蛛」的主形象被某家模型的内容护栏拒了，于是换了三家模型各出一张，
产物目录里就躺着三张脸。**谁当准，后面 12 集全跟着走。**

参考项目（`recon2/aigc-agent/src/aigc_agent/domain/drama/faces.py`）
记录的就是这个现场。它的两段排序常量是这块最值钱的东西 ——
因为它们把"我更喜欢哪张"换成了"哪张更有资格"：

    SOURCE_RANK = {"user": 0, "pack": 1, "asset": 2}
    KIND_RANK   = {"角色": 0, "": 1, "服装": 2}

  * `user` 是用户自己钉进来的（他已经表过态），**不该被自动换掉**。
  * `pack` 是参考图包正在用的 —— 那是当前这一版资产库认定的主形象。
  * `asset` 是散落在产物目录里的。摸不清来历，排在最后。

  * 同一来源里再按类别分：**主形象才是那张脸的准绳**。
    服装图哪怕更新也不能当基准 —— 它自己就是照着主形象生的。
  * `""`（摸不清是什么）排中间。

# 什么时候自动定，什么时候必须问人

这条边界是用户定的：**Agent 自动挑，有歧义才问。**

  * 基准是用户钉的 / 参考图包在用的 → 已表过态，不问，直接采信。
  * 基准只是散落产物，但跟它一致的占多数 → 多数派说了算，不问。
  * 否则（没人表过态、又是各自成群） → 停下来问人。

不猜的理由很实际：猜错的代价不是"这张图不好看"，而是**后面 12 集全用错脸**，
而且等你发现的时候已经渲完了。

# 为什么这块是纯逻辑

这里没有一行网络调用、没有一行文件操作。挑基准、判多数、归堆、出问题清单，
全是纯函数。调模型、搬文件、改参考图包在别处（参考项目把它们放在
`functions/drama.py`，本骨架里由 stage 层负责）。

这样分的好处是可以离线测：**"三张脸谁当准"这个判断能被单测覆盖**，
而它恰恰是那种一旦错了就要重渲全片的判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "KIND_RANK",
    "SOURCE_RANK",
    "FaceAudit",
    "FaceCandidate",
    "audit_lines",
    "conflict_question",
    "decide",
    "group_candidates",
    "matches_character",
    "pick_baseline",
]

#: 来源优先级。数字小的优先。
SOURCE_RANK: Dict[str, int] = {"user": 0, "pack": 1, "asset": 2}

#: 同一来源里的类别优先级。数字小的优先。
KIND_RANK: Dict[str, int] = {"角色": 0, "": 1, "服装": 2}

_SOURCE_WHY: Dict[str, str] = {
    "user": "你自己钉过的那张",
    "pack": "参考图包里正在用的主形象",
    "asset": "产物里最新的一张",
}


@dataclass
class FaceCandidate:
    """一张可能当基准的脸。"""

    character: str
    asset_id: str
    summary: str = ""
    url: str = ""
    local: str = ""
    source: str = "asset"
    pack_key: str = ""
    kind: str = ""
    seq: int = 0

    @property
    def rank(self) -> Tuple[int, int, int]:
        # 来源 > 类别 > 新旧（新的在前，所以取负）
        return (
            SOURCE_RANK.get(self.source, 9),
            KIND_RANK.get(self.kind, 9),
            -self.seq,
        )

    def label(self) -> str:
        """给人看的标签。出问题时用户要能一眼认出是哪张。"""
        bits = [_SOURCE_WHY.get(self.source, self.source)]
        if self.kind:
            bits.append(self.kind)
        if self.summary:
            bits.append(self.summary[:24])
        return f"{self.asset_id}（{'·'.join(bits)}）"

    def to_dict(self) -> Dict[str, object]:
        return {
            "character": self.character,
            "asset_id": self.asset_id,
            "summary": self.summary,
            "source": self.source,
            "kind": self.kind,
            "seq": self.seq,
            "rank": list(self.rank),
        }


@dataclass
class FaceAudit:
    """一个角色的面容裁决结果。"""

    character: str
    keeper: Optional[FaceCandidate] = None
    same: List[FaceCandidate] = field(default_factory=list)
    drift: List[FaceCandidate] = field(default_factory=list)
    scores: Dict[str, int] = field(default_factory=dict)
    issues: Dict[str, List[str]] = field(default_factory=dict)
    ambiguous: bool = False
    why: str = ""
    skipped: str = ""

    @property
    def conflicted(self) -> bool:
        return bool(self.drift)

    @property
    def needs_human(self) -> bool:
        return self.ambiguous

    def to_dict(self) -> Dict[str, object]:
        return {
            "character": self.character,
            "keeper": self.keeper.to_dict() if self.keeper else None,
            "same": [c.to_dict() for c in self.same],
            "drift": [c.to_dict() for c in self.drift],
            "scores": dict(self.scores),
            "issues": {k: list(v) for k, v in self.issues.items()},
            "ambiguous": self.ambiguous,
            "why": self.why,
            "skipped": self.skipped,
        }


def pick_baseline(cands: Sequence[FaceCandidate]) -> Tuple[Optional[FaceCandidate], str]:
    """选基准。返回 (候选, 为什么是它)。"""
    live = [c for c in cands if c is not None]
    if not live:
        return None, "这个角色名下没有可用的图"
    best = sorted(live, key=lambda c: c.rank)[0]
    why = _SOURCE_WHY.get(best.source, f"来源 {best.source} 里最新的那张")
    if best.kind == "服装":
        why += "（只有服装图，没有主形象 —— 脸可能不准）"
    return best, why


def decide(
    character: str,
    cands: Sequence[FaceCandidate],
    verdicts: Dict[str, Tuple[bool, int, Sequence[str]]],
    baseline: Optional[FaceCandidate] = None,
    why: str = "",
) -> FaceAudit:
    """裁决一个角色该用哪张脸。

    `verdicts` 形如 `{asset_id: (是否同一人, 分数, 差异说明)}`，
    **基准自己不在里面**（它当然和自己一致）。

    自动定的条件（见模块文档）：基准已表过态 → 采信；
    基准没表过态但跟它一致的占多数 → 多数派说了算；
    其余 → `ambiguous=True`，交给人。
    """
    audit = FaceAudit(character=character)
    live = list(cands)
    if not live:
        audit.skipped = "没有候选图"
        return audit

    keeper = baseline or pick_baseline(live)[0]
    if keeper is None:
        audit.skipped = "没有候选图"
        return audit
    audit.keeper = keeper
    audit.why = why or pick_baseline(live)[1]

    for c in live:
        if c.asset_id == keeper.asset_id:
            continue
        same, score, issues = verdicts.get(c.asset_id, (None, -1, ()))
        if score >= 0:
            audit.scores[c.asset_id] = score
        if same is None:
            # 没判过：不算"另一张脸"，但要留痕 —— 否则它看起来像"已确认同一人"
            audit.issues.setdefault(c.asset_id, []).append("这次没判过（按未确认处理）")
            audit.drift.append(c)
            continue
        if same:
            audit.same.append(c)
        else:
            audit.drift.append(c)
            audit.issues[c.asset_id] = [str(i) for i in issues if str(i).strip()]

    if not audit.drift:
        return audit

    # 基准是用户钉的或参考图包在用的 —— 他已经表过态，直接采信，不问。
    if keeper.source in ("user", "pack"):
        return audit

    # 基准只是散落产物：跟它一致的占多数就认多数派。
    if len(audit.same) + 1 > len(audit.drift):
        return audit

    audit.ambiguous = True
    audit.why = (
        f"「{character}」有 {len(audit.drift)} 张互不一致的脸，"
        f"而且没有一张是你钉过或参考图包在用的 —— 定不了谁是准的"
    )
    return audit


def group_candidates(
    rows: Iterable[FaceCandidate],
    names: Sequence[str],
) -> Dict[str, List[FaceCandidate]]:
    """按角色名归堆，堆内按 rank 排好序（基准就是第一个）。

    只归到 `names` 里有的角色名下 —— 摸不清归属的图不该被算进任何人。
    """
    known = list(names)
    out: Dict[str, List[FaceCandidate]] = {n: [] for n in known}
    for row in rows:
        if row.character in out:
            out[row.character].append(row)
    for name in out:
        out[name].sort(key=lambda c: c.rank)
    return out


def matches_character(text: str, name: str, others: Sequence[str] = ()) -> bool:
    """这个角色的名字有没有出现在 `text` 里。

    **边界处理是重点**：别的角色名更长且包含它时不算。
    例：`朱锦` 与 `朱锦娘` 同时存在，摘要里出现「朱锦娘」不该被算进「朱锦」——
    否则朱锦娘的脸会被拿去当朱锦的基准，而这两个人在剧里是母女。

    这是纯子串匹配的已知薄弱点（本骨架的分镜层也用同样的方法找角色），
    所以至少把"被更长的名字吃掉"这一种明确挡掉。
    """
    if not text or not name:
        return False
    if name not in text:
        return False
    for other in others:
        if other != name and name in other and other in text:
            return False
    return True


def audit_lines(audits: Sequence[FaceAudit]) -> List[str]:
    """给人看的裁决结果。没冲突的一行带过，有冲突的展开。"""
    lines: List[str] = []
    for a in audits:
        if a.skipped:
            lines.append(f"[·] {a.character}：{a.skipped}")
            continue
        if not a.conflicted:
            keeper = a.keeper.asset_id if a.keeper else "?"
            lines.append(f"[OK] {a.character}：只有一张脸（{keeper}），无需裁决")
            continue
        mark = "[?]" if a.ambiguous else "[!]"
        keeper = a.keeper.asset_id if a.keeper else "?"
        lines.append(f"{mark} {a.character}：定 {keeper} 为基准 —— {a.why}")
        for c in a.drift:
            score = a.scores.get(c.asset_id, -1)
            detail = "；".join(a.issues.get(c.asset_id) or [])
            lines.append(f"      另一张脸 {c.label()}（{score} 分）{detail}")
    return lines


def conflict_question(audits: Sequence[FaceAudit]) -> str:
    """歧义时只问定不了的那些，并列出候选让人回资产 id。

    只问定不了的：把没冲突的角色也塞进问题里，用户会在真正需要决定的那条
    上面滑过去。
    """
    blocked = [a for a in audits if a.ambiguous]
    if not blocked:
        return ""
    lines = ["有几张脸定不了谁是准的，需要你指定（回资产 id 即可）："]
    for a in blocked:
        lines.append(f"  「{a.character}」{a.why}。候选：")
        keeper = a.keeper
        if keeper is not None:
            lines.append(f"    - {keeper.label()}  ← 目前暂定")
        for c in a.drift:
            lines.append(f"    - {c.label()}")
    return "\n".join(lines)
