"""角色引用：把"这一镜里是谁"从一个字符串变成一个身份。

在 `Character.char_id` 出现之前，判断出镜角色靠**名字子串匹配**
（见 `core/faces.py:matches_character`），三种情况会静默错配：

  * **同名不同人** —— 两个角色都叫「林晚」，按名字查表只留得下一个；
  * **改名** —— 第 5 集把「苏蔓」改成「苏晚」，前面所有引用一起失配；
  * **多重身份** —— 卧底真名「陈默」、化名「林晚」，剧本里两个名字都出现，
    于是"这一镜该拿谁的脸"变成了掷骰子。

`char_id` 是那个不随集数、不随改名、不随别名变化的东西。这一层只做一件事：
**把一个引用解析成一个角色，并如实说出它是怎么解析的**。

为什么引用允许三种写法（id / 名字 / 别名）而不是只允许 id：

  * 老 `project.json` 里只有名字，升级之后那些文件还得能读；
  * 提示词里让模型照抄 id 是为了让**改名不失效**，但它抄错、或者干脆
    写名字，都不该让这一镜静默丢掉参考图 —— 降级要发生，但要说出来。

所以 `resolve()` 返回的不只是角色，还有 `how`：`id` / `name` / `alias` /
`ambiguous` / `unknown`。调用方据此决定"这条降级是否值得报警"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Character

#: 稳定 id 的前缀。和 run / project 的 id 一样带前缀，是为了让人在一堆
#: 字符串里一眼认出"这是个 id，不是名字"。
ID_PREFIX = "char_"

HOW_ID = "id"
HOW_NAME = "name"
HOW_ALIAS = "alias"
HOW_AMBIGUOUS = "ambiguous"
HOW_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Resolution:
    """一次引用的解析结果。"""

    ref: str
    how: str
    character: Optional[Character] = None
    #: 有歧义时的全部候选（写成 `名字(char_id)`，因为两个候选的重名正是问题本身）
    others: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.character is not None


def mint_id(name: str, taken: Iterable[str] = ()) -> str:
    """按名字生成一个稳定 id。

    **刻意不用随机数**：老 `project.json` 里没有 id，要在读取时补上。
    如果补出来的是随机值，那么同一个文件被读两次会得到两套 id，
    而引用它们的镜头就会在两个进程之间对不上。按名字哈希是幂等的 ——
    同一个名字永远得到同一个 id，改名也只改 `name`，不动 id。
    """
    seed = (name or "").strip()
    base = ID_PREFIX + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    used = set(taken)
    if base not in used:
        return base
    # 同名的第二个角色（同名不同人）：加后缀，而不是让它们共用一个身份。
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def ensure_ids(cast: Sequence[Character]) -> int:
    """给还没有 id 的角色补上。返回补了几个。

    对已经在册的角色是幂等的 —— 这是老 `project.json` 的升级路径，
    也是"读一遍就分叉"这一类 bug 的预防措施。
    """
    taken = {c.char_id for c in cast if c.char_id}
    minted = 0
    for c in cast:
        if not c.char_id:
            c.char_id = mint_id(c.name, taken)
            taken.add(c.char_id)
            minted += 1
    return minted


def _indexes(cast: Sequence[Character]) -> Tuple[Dict[str, Character], Dict[str, List[Character]]]:
    """(按 id 查, 按名字与别名查)。

    第二个索引的值是**列表**：同名不同人就是这个列表长度大于 1 的情况，
    把它折成单个角色正是原来那个 bug。
    """
    by_id: Dict[str, Character] = {}
    by_label: Dict[str, List[Character]] = {}
    for c in cast:
        if c.char_id:
            by_id.setdefault(c.char_id, c)
        labels = [c.name, *c.aliases]
        for label in labels:
            label = (label or "").strip()
            if not label:
                continue
            bucket = by_label.setdefault(label, [])
            if c not in bucket:
                bucket.append(c)
    return by_id, by_label


def resolve(ref: str, cast: Sequence[Character]) -> Resolution:
    """把一个引用解析成一个角色。解析不了要说得出是哪一种失败。"""
    ref = (ref or "").strip()
    if not ref:
        return Resolution(ref=ref, how=HOW_UNKNOWN)
    by_id, by_label = _indexes(cast)

    hit = by_id.get(ref)
    if hit is not None:
        return Resolution(ref=ref, how=HOW_ID, character=hit)

    hits = list(by_label.get(ref) or [])
    if not hits:
        # 大小写不敏感的第二遍：`LIN WAN` 与 `Lin Wan` 是同一个人的写法差异，
        # 不是两个人。中文名字不受影响（casefold 是恒等变换）。
        folded = ref.casefold()
        for label, bucket in by_label.items():
            if label.casefold() == folded:
                for c in bucket:
                    if c not in hits:
                        hits.append(c)
    if len(hits) == 1:
        how = HOW_NAME if ref == hits[0].name else HOW_ALIAS
        return Resolution(ref=ref, how=how, character=hits[0])
    if len(hits) > 1:
        return Resolution(
            ref=ref, how=HOW_AMBIGUOUS,
            others=tuple(sorted(f"{c.name}({c.char_id or '无id'})" for c in hits)),
        )
    return Resolution(ref=ref, how=HOW_UNKNOWN)


@dataclass(frozen=True)
class Problem:
    """一个解析不了的引用。`how` 只有 `unknown` 与 `ambiguous` 两种。"""

    ref: str
    how: str
    detail: str


def resolve_all(
    refs: Sequence[str], cast: Sequence[Character]
) -> Tuple[List[Character], Dict[str, int], List[Problem]]:
    """解析一串引用。返回 (命中的角色, 统计, 解析不了的问题)。

    统计的键就是 `how` 的取值 —— 调用方可以把"这一集有多少镜是靠名字
    认出来的"直接印到报告里。降级可以发生，但不能无声无息。
    """
    found: List[Character] = []
    stats = {HOW_ID: 0, HOW_NAME: 0, HOW_ALIAS: 0, HOW_AMBIGUOUS: 0, HOW_UNKNOWN: 0}
    problems: List[Problem] = []
    for ref in refs:
        r = resolve(ref, cast)
        stats[r.how] = stats.get(r.how, 0) + 1
        if r.ok:
            if r.character not in found:
                found.append(r.character)  # type: ignore[arg-type]
        elif r.how == HOW_AMBIGUOUS:
            problems.append(Problem(
                ref=r.ref, how=HOW_AMBIGUOUS,
                detail=(f"「{r.ref}」在卡司里对上了 {len(r.others)} 个角色："
                        f"{'、'.join(r.others)} —— 有歧义就不猜，这一镜锁不了脸"),
            ))
        else:
            problems.append(Problem(
                ref=r.ref, how=HOW_UNKNOWN,
                detail=f"「{r.ref}」不在卡司里 —— 这一镜没有参考图",
            ))
    return found, stats, problems


def id_table(cast: Sequence[Character]) -> str:
    """给提示词看的角色表：`id = 名字（特征）`。

    模型不需要理解 id 是什么，只需要**逐字照抄**。这样第 5 集把这个人的
    名字改掉时，前面引用它的镜头不会一起失配 —— 它们引用的是 id。
    """
    if not cast:
        return ""
    lines = ["【角色表】镜头里的 `characters` 填**下面这行的 id**（逐字照抄，不要自己编）："]
    for c in cast:
        desc = (c.desc or c.anchor_prompt or "").strip().replace("\n", " ")
        if len(desc) > 60:
            desc = desc[:60] + "…"
        alias = f"（别名：{'、'.join(c.aliases)}）" if c.aliases else ""
        lines.append(f"{c.char_id or '无id'} = {c.name}：{desc}{alias}")
    return "\n".join(lines)


__all__ = [
    "HOW_ALIAS",
    "HOW_AMBIGUOUS",
    "HOW_ID",
    "HOW_NAME",
    "HOW_UNKNOWN",
    "ID_PREFIX",
    "Problem",
    "Resolution",
    "ensure_ids",
    "id_table",
    "mint_id",
    "resolve",
    "resolve_all",
]
