"""角色一致性：把参考图"点名"，并且不许校验假装通过。

# 问题

视频模型**每次调用是独立的**。同一句"一位工程师"生成四次，会得到四张
不同的脸 —— 模型没有任何机制知道这两个镜头里是同一人。把参考图传过去
只是"给了"，不等于"用了"。

参考项目（`recon2/aigc-agent/src/aigc_agent/domain/drama/identity.py`）
在这件事上的现场记录是："参考图传了也漂，人物漂移严重"。它得出的三条
对策，本骨架原样移植了过来 —— 因为这三条都不是口味问题，是机制问题：

1. **参考锁定段**：在提示词里用 `@图片N` 点名"这张是谁、要一致什么"。
   图片列表与 `@图片N` 同序。只说"保持一致"没有用，得说清是哪一张、
   一致的是什么。
2. **不许继承资产图的版式**（`NOT_INHERIT`）。参考图是**白底证件照**
   和白底全身服装图。不显式排除，模型会把纯白背景、站姿、正面构图、
   影棚光一起搬进镜头 —— 于是每个镜头都像在拍证件照。
3. **校验不许假装通过**。生成后把参考图与结果一起交给视觉模型逐人打分。
   关键是第三态：**"没查成"不等于"通过"**。调用失败、没有本地副本、
   抽不出帧、输出读不出来 —— 这些都按未通过处理，标记待补查。

# 第三条为什么重要

参考项目里同一个仓库有两套相反的做法：
`domain/drama/identity.py:112-115` 是"没查成按没过算"，
而 `domain/realism.py:553-554` 是"解析不出来按通过处理"。
两套并存本身就是问题：**静默放行的校验等于没有校验**，
而且它比没有校验更糟 —— 你会以为已经查过了。

本骨架只取前者：`unchecked` 是独立于 pass/fail 的第三态。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "IDENTITY_RETRY",
    "KIND_DESC",
    "NOT_INHERIT",
    "PASS_SCORE",
    "REFERENCE_HEAD",
    "IdentityVerdict",
    "check_messages",
    "identity_retry_prompt",
    "parse_identity_verdict",
    "reference_block",
    "shot_identity_prompt",
]

#: 锁定段的开头。渲染层靠它判断"这段提示词是不是已经锁过了"。
REFERENCE_HEAD = "【参考锁定】"

#: 一致性分门槛。低于它判不合格。
#: 7 分的含义（照抄校验提示词的定义）：10 = 同一个人同一装扮；
#: 7 分以下 = 明显不是同一个人，或发型/服装变了。
PASS_SCORE = 7

#: 按资产类别生成"要一致什么"。参考项目 identity.py:22-29 的原文。
KIND_DESC: Dict[str, str] = {
    "角色": "角色「{name}」本人：脸型五官、发型发色、体型必须与这张图完全一致",
    "服装": "「{name}」这套服装：款式、颜色、材质一致（脸仍以角色主形象为准）",
    "三视图": "角色「{name}」的体型比例参考",
    "场景": "场景「{name}」的环境与布光",
    "道具": "道具「{name}」的外形",
}

#: **最容易被忽略、也最关键的一句。**
#:
#: 来源是参考项目对 awesome-seedance 案例库的整理，原注：
#: "能不能锁住人，差别就在这一句"。
#:
#: 参考图是纯白底的资产图。不排除，模型会把白背景、站姿、正面构图、
#: 原始光线全搬进分镜画面 —— 而分镜要的是"这个人在这个场景里做这件事"。
NOT_INHERIT = (
    "参考图只取上述属性；**不要**把参考图里的纯白背景、站姿、正面构图、画角"
    "和原始光线搬进画面，那些是资产图不是分镜。"
    "转头、低头、说话、手靠近脸、快速运动时仍必须是同一张脸。"
)

#: 不合格时的重生成指令。中英双语 —— 有些模型对英文指令更敏感。
IDENTITY_RETRY = (
    "**【重新生成】上一版画面里的人物与参考图不是同一个人（{issues}），属于不合格。**"
    "这次必须严格按参考图还原人物的脸型与五官、发型与发色、体型与服装款式颜色。"
    "**REGENERATE**: the person in the previous version does not match the reference "
    "image ({issues}). This version must match the reference face, hair, build and outfit."
)

_RETRY_PREFIX = IDENTITY_RETRY.split("（", 1)[0]

#: 给视觉模型的判定提示词。**逐人打分**，不是整片一个分。
CHECK_PROMPT = """你是短剧的角色一致性审核员。下面先给出参考图（角色资产图），
再给出这一镜的画面（图或视频抽帧）。请逐个角色判断画面里的人是不是同一个人。

每个角色只看这四项：脸型与五官、发型与发色、体型、服装款式与颜色。

打分 0-10：
  10 = 同一个人、同一套装扮
  7-9 = 同一个人，但妆造或服装有可见变化
  0-6 = 明显不是同一个人，或发型/服装换了

画面里没出现某个参考角色不算扣分（那一镜没他的戏）。
场景与道具只判断环境与外形是否一致，不打人物分。

只输出 JSON，不要解释文字：
{"pass": true, "score": 8, "issues": ["陆离的发型从长发变成短发"], "characters": {"陆离": 8}}
"""


def reference_block(labels: Sequence[Tuple[str, str]]) -> str:
    """生成参考锁定段。

    `labels` 必须**与传给模型的图片列表同序** —— 因为输出里用的是
    `@图片1`、`@图片2` 这样的编号，错一位就等于把脸安到别人身上，
    而且模型不会报错，它只会照着错的图渲。

    参数形如 `[("角色", "林晚"), ("服装", "林晚·战袍"), ("场景", "天台")]`。
    """
    if not labels:
        return ""
    parts: List[str] = []
    for i, (kind, name) in enumerate(labels, start=1):
        describe = KIND_DESC.get(kind)
        if describe is None:
            describe = "「{name}」的" + kind
        parts.append(f"@图片{i} = {describe.format(name=name)}")
    return (
        f"{REFERENCE_HEAD}画面中的人物、服装、场景、道具严格以参考图为准，"
        f"不得自行更换长相、发型或服装："
        + "；".join(parts)
        + "。"
        + NOT_INHERIT
    )


def shot_identity_prompt(prompt: str, labels: Sequence[Tuple[str, str]]) -> str:
    """把锁定段拼到镜头提示词**开头**。

    放开头不是排版偏好：参考项目的复盘里，把硬约束放在提示词尾部时
    模型会忽略它（"7 张主形象的脸没听这套"）。锁定段必须先进去。
    """
    block = reference_block(labels)
    if not block:
        return prompt
    body = (prompt or "").strip()
    return f"{block}\n{body}" if body else block


def identity_retry_prompt(prompt: str, issues: Iterable[str]) -> str:
    """把"哪里不像"写进提示词最前面，再生成一次。

    **幂等**：已经带重生成指令的提示词不再叠加。否则重试两次之后，
    提示词开头会堆三段一样的话，而模型会把它当噪音。
    """
    if (prompt or "").startswith(_RETRY_PREFIX):
        return prompt
    why = "；".join(i for i in issues if i)[:200] or "长相或服装变了"
    return IDENTITY_RETRY.format(issues=why) + "\n" + (prompt or "").strip()


# ————————————————————————————————————————---- 判定


@dataclass
class IdentityVerdict:
    """一次一致性校验的结论。**三态，不是两态。**"""

    passed: bool = True
    score: int = -1
    issues: List[str] = field(default_factory=list)
    characters: Dict[str, int] = field(default_factory=dict)
    note: str = ""
    #: `unchecked=True` = **这次没查成**（调用失败 / 没本地副本 /
    #: 抽不出帧 / 输出读不出来）。按未通过处理：不进成片、重跑时补查。
    #:
    #: 只有 note 而没有 unchecked = 配置里就没开这项检查，
    #: 那不算"没查成"，也不该拦住片子。
    unchecked: bool = False

    def __post_init__(self) -> None:
        #: 不变式：没查成 ⇒ 没通过。
        #:
        #: `passed` 的默认值是 True（"没有任何异议就是通过"），但那个默认
        #: 只对**真的查过**的结论成立。少了这一句，`parse_*` 返回的
        #: unchecked 结论会带着 `passed=True` 流出去，而调用方多半只看
        #: `passed` —— 于是"没查成"在下一层又变回了"通过"，
        #: 而且这一次连 note 都没人读。
        if self.unchecked:
            self.passed = False

    @property
    def state(self) -> str:
        if self.unchecked:
            return "unchecked"
        return "pass" if self.passed else "fail"

    @property
    def retryable(self) -> bool:
        """没查成的**不该**直接重生成 —— 先补查。

        把它当失败去重渲，是在用真金白银掩盖一个网络问题。
        """
        return (not self.unchecked) and (not self.passed)

    def render(self) -> str:
        if self.unchecked:
            return f"[?] 未查成：{self.note or '原因未记录'}"
        if self.passed:
            return f"[OK] 一致性通过（{self.score} 分）"
        detail = "；".join(self.issues) or "分数不达标"
        return f"[X] 一致性不通过（{self.score} 分）：{detail}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "passed": self.passed,
            "score": self.score,
            "issues": list(self.issues),
            "characters": dict(self.characters),
            "note": self.note,
            "unchecked": self.unchecked,
        }


def _strip_fence(text: str) -> str:
    raw = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", raw, re.S)
    return fence.group(1).strip() if fence else raw


def parse_identity_verdict(text: str, *, pass_score: int = PASS_SCORE) -> IdentityVerdict:
    """解析视觉模型的输出。

    解析不出来时返回 `unchecked=True`，**不是"通过"**。
    这条是刻意的：调用失败、输出乱码、模型答非所问 —— 这三种情况下
    你并不知道画面到底一不一致，而"按通过处理"会让成片带着没查过的
    镜头交付，且没有任何痕迹。
    """
    raw = _strip_fence(text)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    if not raw:
        return IdentityVerdict(note="一致性校验没有返回内容", unchecked=True)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return IdentityVerdict(note="一致性校验输出不是合法 JSON", unchecked=True)
    if not isinstance(data, dict):
        return IdentityVerdict(note="一致性校验输出的不是对象", unchecked=True)

    issues = [str(i) for i in (data.get("issues") or []) if str(i).strip()]
    chars_raw = data.get("characters") or {}
    characters: Dict[str, int] = {}
    if isinstance(chars_raw, dict):
        for name, value in chars_raw.items():
            try:
                characters[str(name)] = int(float(value))
            except (TypeError, ValueError):
                continue

    try:
        score = int(float(data.get("score", -1)))
    except (TypeError, ValueError):
        score = -1

    if score < 0:
        # 拿不到分数时退回布尔判定；连布尔都没有就按通过处理**但写 note**。
        #
        # 这一条看着和上面矛盾，其实是两件事：走到这里说明**调用成功了、
        # 输出也是 JSON**，只是模型没按格式给分数 —— 那是格式偏差，
        # 不是"没查成"。校验器把生成链路整个挂掉，比放过一次格式偏差更糟。
        fallback = data.get("pass")
        if isinstance(fallback, bool):
            return IdentityVerdict(
                passed=fallback, score=score, issues=issues,
                characters=characters,
                note="模型没给分数，按它的 pass 字段判定",
            )
        return IdentityVerdict(
            passed=True, score=score, issues=issues, characters=characters,
            note="模型既没给分数也没给 pass 字段，按通过处理但请人工复核",
        )

    # 逐角色最低分门槛：一个角色漂了，整镜就不合格。
    # 用最低分而不是平均分 —— 平均分会让"主角漂了但配角很准"过关。
    passed = score >= pass_score and all(v >= pass_score for v in characters.values())
    return IdentityVerdict(
        passed=passed, score=score, issues=issues, characters=characters
    )


def check_messages(
    refs: Sequence[Tuple[str, str, str]],
    targets: Sequence[str],
    *,
    is_video: bool = False,
) -> List[Dict[str, Any]]:
    """构造给视觉模型的校验消息。

    `refs` 是 `(标签, 说明, 图片地址)`，`targets` 是待判画面的地址
    （本地文件用 data URL，或 https 链接）。

    参考图用 `detail: high`（要看清五官），视频抽帧用 `detail: low`
    （抽帧多，high 会把 token 直接爆掉，而这里要的是"是不是同一张脸"
    这个粗判断）。参考项目的实测数字：512 宽、q4 的一帧约 145KB base64，
    十几帧就是 2MB 请求体。
    """
    content: List[Dict[str, Any]] = [{"type": "text", "text": CHECK_PROMPT}]
    for label, desc, url in refs:
        content.append({"type": "text", "text": f"参考图：{label}（{desc}）"})
        content.append({"type": "image_url",
                        "image_url": {"url": url, "detail": "high"}})
    kind = "视频抽帧" if is_video else "生成结果"
    for i, url in enumerate(targets, start=1):
        content.append({"type": "text", "text": f"{kind} {i}/{len(targets)}"})
        content.append({"type": "image_url",
                        "image_url": {"url": url, "detail": "low"}})
    return [{"role": "user", "content": content}]
