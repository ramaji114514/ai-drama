"""一致性救命包：一张参考图给谁、这镜该不该拆、锁不住的脸怎么办。

已确认的约束（不是我们挑的，是接口事实）：

  * 主力图像模型单次只吃 **1 张**参考图（`ProviderCaps.max_ref_images`），
    视频模型接着也只吃 1 张首帧。一镜两个人，就必然有一个人的脸靠文字描述。
  * 参考图能锁"脸"，锁不住"关系" —— 两个人怎么站、谁看谁，它管不了。
  * 同一个人的脸在段落之间漂移，观众最先看出来的是**眼睛和发型**，
    所以文字描述要把这两样写死（`Character.desc` 里已经要求）。

于是有一个策略问题：**那一张参考图给谁？** 以前是"按声明顺序取第一个"，
等于把"这场戏是谁的"交给剧本 JSON 里数组的先后。这里的排序规则是：

  1. 这一镜**说话的人**优先 —— 台词的承载者，脸被看的时间最长；
  2. 全局**出镜次数多**的优先 —— 同一次运行里出现得越多，锁他收益越大；
  3. 都相同时按声明顺序 —— 剧本的顺序至少表达了一点"重要性"。

拆镜建议用的是电影里的老办法：**单人 → 反打 → 双人全景**。前两段各锁一张脸，
第三段用全/远景，人物在画面里小到"脸不是重点"，两张脸都不需要参考图也能对上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class Picks:
    """这一镜谁拿到了参考图、谁没有。"""

    locked: List[Any] = field(default_factory=list)
    unlocked: List[Any] = field(default_factory=list)
    reason: str = ""

    @property
    def locked_names(self) -> List[str]:
        return [getattr(c, "name", "") for c in self.locked]

    @property
    def unlocked_names(self) -> List[str]:
        return [getattr(c, "name", "") for c in self.unlocked]


@dataclass
class Segment:
    """拆镜后的一段。只给建议，不替你改 episode.json。"""

    order: int
    framing: str
    action: str
    characters: List[str]
    needs_reference: bool
    why: str

    def as_dict(self) -> Dict[str, Any]:
        return {"order": self.order, "framing": self.framing,
                "action": self.action, "characters": list(self.characters),
                "needs_reference": self.needs_reference, "why": self.why}


def _speaks(shot: Any, name: str) -> bool:
    """这一镜的台词是不是这个人说的。

    台词约定是「名字：内容」（也可以带括号动作）。没有冒号的老数据就认不出，
    这时不猜、按"没说话"处理 —— 宁可把参考图给错人，也不要凭一个字就给。
    """
    line = (getattr(shot, "dialogue", "") or "").strip()
    if not line or "：" not in line and ":" not in line:
        return False
    head = line.split("：")[0].split(":")[0]
    return name and name in head


def _id_for(ep: Any, ref: str) -> str:
    """把分镜里写的名字/别名换成稳定身份。

    不换会静默算错：分镜写的是"乙"，`char_id` 是 "b"，
    `counts.get(char_id)` 永远取到 0 —— 于是"出镜次数"这一项等于没有，
    参考图又退回按数组顺序给了。
    """
    for c in getattr(ep, "characters", []) or []:
        labels = c.labels() if hasattr(c, "labels") else [getattr(c, "name", "")]
        if ref in labels:
            return getattr(c, "char_id", "") or ref
    return ref


def appearance_counts(ep: Any) -> Dict[str, int]:
    """全片每个角色出镜多少次，按 `char_id` 记 —— 收益跟次数成正比。"""
    counts: Dict[str, int] = {}
    for scene in getattr(ep, "scenes", []) or []:
        for shot in getattr(scene, "shots", []) or []:
            ids = {_id_for(ep, str(ref)) for ref in getattr(shot, "characters", []) or []}
            for cid in ids:
                counts[cid] = counts.get(cid, 0) + 1
    return counts


def rank(ep: Any, shot: Any, on_screen: Sequence[Any]) -> List[Any]:
    """把这一镜的人按"参考图给谁最划算"排序。"""
    counts = appearance_counts(ep)
    order = {getattr(c, "char_id", ""): i for i, c in enumerate(on_screen)}
    return sorted(
        on_screen,
        key=lambda c: (
            0 if _speaks(shot, getattr(c, "name", "")) else 1,   # 说话的优先
            -counts.get(getattr(c, "char_id", ""), 0),           # 出现多的优先
            order.get(getattr(c, "char_id", ""), 99),            # 声明顺序兜底
        ),
    )


def pick_reference(ep: Any, shot: Any, on_screen: Sequence[Any],
                   max_refs: int = 1) -> Picks:
    """按优先级把有限的参考图名额分下去。"""
    ranked = rank(ep, shot, on_screen)
    limit = max(0, int(max_refs))
    locked = ranked[:limit]
    unlocked = ranked[limit:]
    if not on_screen:
        return Picks([], [], "空镜不需要参考图")
    if not unlocked:
        return Picks(locked, [], f"这一镜 {len(locked)} 人，参考图够用")
    who = "、".join(getattr(c, "name", "") for c in locked) or "没人"
    return Picks(locked, unlocked,
                 f"参考图给了 {who}（单镜最多 {limit} 张），"
                 f"其余 {len(unlocked)} 人按文字描述生成")


def explain(ep: Any, shot: Any, on_screen: Sequence[Any],
            max_refs: int = 1) -> List[str]:
    """给报告用的人话解释，一条一句。"""
    picks = pick_reference(ep, shot, on_screen, max_refs)
    lines = [picks.reason] if picks.reason else []
    for c in picks.locked:
        why = "这一镜的台词是 ta 说的" if _speaks(shot, getattr(c, "name", "")) \
            else "出镜次数最多的那个"
        lines.append(f"锁脸 {getattr(c, 'name', '')}：{why}")
    if picks.unlocked:
        lines.append(
            "锁不住：" + "、".join(picks.unlocked_names) +
            " —— 靠文字描述（眼睛、发型、服装写死），别指望和别的镜头完全一致")
        lines.append("要两张脸都锁住：`python run.py split <run_id> --shot "
                     f"{getattr(shot, 'label', '') or '<镜头>'}` 看拆镜方案")
    return lines


def _first_clause(text: str, limit: int = 24) -> str:
    """取第一小句当这一段的动作 —— 拆镜后每段只演一件小事。

    被截断时补省略号：不补的话"绿眼凝视窗"看起来像一个错句，
    读到的人会以为程序把词切坏了。
    """
    text = (text or "").strip()
    for sep in ("。", "；", ";", "，", ","):
        if sep in text:
            head = text.split(sep)[0]
            if head:
                text = head
                break
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，。、 ")+ "…"


def split_plan(ep: Any, shot: Any, on_screen: Sequence[Any],
               max_refs: int = 1) -> List[Segment]:
    """把一镜超员的戏拆成"单人 → 反打 → 双人全景"。

    为什么第三段用全景：参考图锁的是脸，脸在画面里越小，模型越不需要它 ——
    全景里两个人各占一小块，靠服装和发型就能认人，这时"两张脸"不是关键。
    这就是在 1 张参考图的约束下还能拍两个人的办法。
    """
    if len(on_screen) <= max(1, max_refs):
        return []
    ranked = rank(ep, shot, on_screen)
    primary = ranked[0]
    others = ranked[1:]
    out: List[Segment] = []
    action = getattr(shot, "action", "") or ""
    end = getattr(shot, "end_action", "") or ""
    out.append(Segment(
        1, "close", _first_clause(action), [getattr(primary, "name", "")], True,
        f"单人特写：把参考图给 {getattr(primary, 'name', '')}，这一镜的脸最要紧"))
    if others:
        second = others[0]
        out.append(Segment(
            2, "close", _first_clause(end or action), [getattr(second, "name", "")], True,
            f"反打单人：换成 {getattr(second, 'name', '')}，参考图改给 ta，"
            "和上一段剪在一起就是对话"))
    everyone = "、".join(getattr(c, "name", "") for c in ranked)
    out.append(Segment(
        3, "wide", f"{everyone}在同一个空间里，交代位置关系", 
        [getattr(c, "name", "") for c in ranked], False,
        "双人全景：人物小、脸不是重点，不给参考图也不会被看出来"))
    return out


def plan_text(plan: Sequence[Segment]) -> str:
    """拆镜方案打成给人看的几行。"""
    if not plan:
        return "这一镜不需要拆：出镜人数没有超过参考图上限。"
    lines = []
    for seg in plan:
        need = "要参考图" if seg.needs_reference else "不用参考图"
        lines.append(f"{seg.order}. [{seg.framing}] {seg.action} "
                     f"（{'、'.join(seg.characters)}；{need}）")
        lines.append(f"   {seg.why}")
    return "\n".join(lines)
