"""提示词体检：在把提示词交给用户之前，先按平台规则自己看一遍。

为什么需要这一层：提示词"写得好不好"可以争，"写错了"是能查的。实测和官方文档
反复指出的翻车点，大多不是文笔问题，是几条硬规则没满足：

  * **运镜句放在最后会被当成可选项**（可灵把它当括号里的补充），结果是画面
    自己横移，跟你要的推近毫无关系。所以在视频提示词里运镜必须**先说**。
  * **一镜两个运镜**（"缓慢推近后横移"）等于让模型二选一，多数会两个都不做。
  * **运镜超出片段时长**：三秒里做一次完整橫移再推近，物理上做不完，模型只好
    加速，出来就是"抽帧感"。
  * **人数不写死**：正向里只说"林溪站在门口"，模型可以顺手再画两个人。
  * **有台词却不写开口**：画面里的人闭嘴站着，配音在画外，一眼假。

这一层的输出**不是拒绝**，是提示：`Finding` 列出问题，拼装时能自动修的直接修
（运镜前置、补人数、补开口），修不了的（运镜太长、正文过长）留给报告。

注意：这里**不联网、不烧钱**，全是本地字符串判断。所以它可以跑在每次定稿上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 每种景别的镜头语言：焦段 + 景深 + 光比。
#:
#: 只写景别（"中景"）对模型来说信息量很低 —— 它知道要拍多大，但不知道**用
#: 什么镜头拍**，于是默认给一个又平又亮的画面。焦距决定透视（35mm 有环境感、
#: 85mm 把人从背景里"摘"出来），景深决定背景糊不糊，光比决定这张脸是硬的还是
#: 柔的。三者加起来十来个字，是提示词里性价比最高的一笔。
LENS: Dict[str, str] = {
    "establishing": "24mm 广角，深景深，天光为主",
    "wide": "28mm，深景深，环境光",
    "medium": "35mm，中景深，侧逆光",
    "close": "85mm，浅景深，柔光",
    "extreme_close": "100mm 微距，极浅景深，硬光勾边",
}
DEFAULT_LENS = "35mm，中景深，自然光"

#: 运镜要在几秒内才做得完。数字不是物理定律，是"看起来不赶"的经验下限：
#: 一次横移要让观众看清环境变化，三秒以内只会糊成一片。
MOVE_MIN_SECONDS: Dict[str, float] = {
    "推近": 2.0,
    "推进": 2.0,
    "推近后停顿": 3.0,
    "横移": 4.0,
    "跟随": 3.0,
    "手持": 1.0,
    "固定机位": 0.0,
}

#: 这些词一出现就说明"这一镜有好几个运镜"，模型会二选一。
_MULTI_MOVE = ("然后", "接着", "再", "随后")
_MOVE_WORDS = ("推近", "推进", "拉远", "横移", "平移", "摇", "跟随", "升降",
               "环绕", "旋转", "俯冲", "变焦")

#: 有台词时，视频提示词里必须有"这张嘴在动"的交代。
_SPEAK_HINTS = ("开口", "说话", "口型", "台词", "张嘴", "唇形")

_SENTENCE_END = "。！？；…"


@dataclass
class Finding:
    """一条体检结论。`fixed` 为真表示拼装时已经自动改好了。"""

    code: str
    level: str  # "info" | "warn"
    message: str
    fixed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "level": self.level,
                "message": self.message, "fixed": self.fixed}


def shorten(text: str, budget: int) -> str:
    """按字数预算精简，尽量断在小句边界，不留下半个词。

    超预算时**不是不写**，是写得短一点：观众记住的是"谁在哪儿做了什么"，
    不是第三个形容词。预算内原文不动，避免把手写的节奏改掉。
    """
    text = (text or "").strip()
    if budget <= 0 or len(text) <= budget:
        return text
    cut = text[:budget]
    for mark in ("。", "；", "，", "、", " "):
        pos = cut.rfind(mark)
        if pos >= budget * 0.5:
            return cut[:pos].strip()
    return cut.strip()


def lens_clause(framing: str) -> str:
    """这一镜该用什么镜头拍 —— 十几个字，换来透视和光比的确定性。"""
    return LENS.get((framing or "").strip(), DEFAULT_LENS)


def count_clause(names: Sequence[str]) -> str:
    """把人数写死。

    不写人数时，"林溪站在门口"这种句子模型可以自由加人 —— 而且它经常加，
    尤其画面空的时候。空镜也要明说"画面中没有人"，否则会被塞进路人。
    """
    names = [n for n in names if (n or "").strip()]
    if not names:
        return "画面中没有人"
    if len(names) == 1:
        return f"画面中只有 {names[0]}"
    return "画面中只有 " + " 和 ".join(names)


def speaking_clause(dialogue: str) -> str:
    """有台词就必须让人看见"他在说话"。"""
    if not (dialogue or "").strip():
        return ""
    return "人物开口说话，口型与台词同步"


def move_is_too_fast(move: str, seconds: float) -> Optional[Tuple[str, float]]:
    """这个运镜在这么长的片段里做不做得完。返回 (运镜名, 需要的最短秒数)。"""
    if seconds <= 0:
        return None
    for name, need in MOVE_MIN_SECONDS.items():
        if name in (move or "") and seconds < need:
            return name, need
    return None


def has_several_moves(text: str) -> bool:
    """一句话里塞了几个运镜。"""
    hits = [w for w in _MOVE_WORDS if w in (text or "")]
    joins = [w for w in _MULTI_MOVE if w in (text or "")]
    return len(hits) >= 2 or (hits and joins)


def body_length(text: str) -> int:
    """画面描述的长度，**不含**参考锁定段。

    参考锁定段（`【参考锁定】…`「【无参考图】…」）是必带的身份约束，长度固定
    两百多字，跟"这一镜写得啰嗦不啰嗦"无关。把它算进预算，等于每镜都超 ——
    报警多了就没人看了，那是比不报警更糟。
    """
    lines = [ln for ln in (text or "").splitlines() if not ln.strip().startswith("【")]
    return len("".join(lines).strip())


def looks_too_long(text: str, budget: int) -> bool:
    return body_length(text) > budget


def lint_shot(shot: Any, *, budget: int = 120,
              pinned: bool = False) -> List[Finding]:
    """给一镜做体检。`pinned` 表示提示词是用户手改过的，不动它、只报告。"""
    out: List[Finding] = []
    video = getattr(shot, "video_prompt", "") or ""
    image = getattr(shot, "image_prompt", "") or ""
    seconds = float(getattr(shot, "duration", 0.0) or 0.0)

    if not (getattr(shot, "end_action", "") or "").strip():
        out.append(Finding("no_end_action", "warn",
                           "这一镜没有 end_action，尾帧只能复述首帧。"))
    if looks_too_long(image, budget) and not pinned:
        out.append(Finding("image_too_long", "warn",
                           f"画面描述 {body_length(image)} 字，超过 {budget} 字预算；"
                           "模型会挑着执行，等于把决定权交给它。"))
    if has_several_moves(video):
        out.append(Finding("several_moves", "warn",
                           "视频提示词里有多个运镜，模型多半会二选一。"))
    fast = move_is_too_fast(video, seconds)
    if fast:
        name, need = fast
        out.append(Finding("move_too_fast", "warn",
                           f"「{name}」至少要 {need:.0f} 秒才做得完，"
                           f"这一镜只有 {seconds:.1f} 秒。"))
    dialogue = getattr(shot, "dialogue", "") or ""
    if dialogue.strip() and not any(w in video for w in _SPEAK_HINTS):
        out.append(Finding("no_speech_mime", "warn",
                           "有台词但视频提示词没让人开口，配音会像画外音。"))
    if not (getattr(shot, "negative_prompt", "") or "").strip():
        out.append(Finding("no_negative", "warn",
                           "没有负向词，手和脸是图像模型最先崩的地方。"))
    return out


def summarize(findings: Sequence[Finding]) -> str:
    """一行给人看的汇总，报告里用。"""
    if not findings:
        return "提示词体检通过"
    return f"提示词体检 {len(findings)} 条提醒：" + "；".join(
        f.message for f in findings)
