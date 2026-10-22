"""技能装载：先规则筛，再让人（或模型）按 description 挑。

# 为什么需要这一层

这个仓库把「创意判断」放在 `skills/*.md` 里 —— 选题、剧本、分镜、角色、
提示词、质检标准。它们会随品味反复改，写死在代码里就僵了。

但**把所有技能全文塞进上下文**这条路走到 60 集长剧就会断：6 篇中等长度的
文档加起来已经一万多 token，而长剧要的是几十次连续调用。参考项目
（`recon2/aigc-agent/skills/README.md:81-93`）在这里的教训很直接：

    常驻上下文只有 frontmatter 里的 description（每条约 50 token），
    正文只在被选中时才加载（每篇 2–5K token）。

这就是**两级披露**。它带来两个必须认真对待的后果：

  1. **description 是唯一真正影响效果的字段。** 技能是靠它被选中的，
     正文写得再精彩，description 没写清"什么时候该用我"，
     这篇文档就永远不会被翻开。
  2. **冲突必须显式标注。** 两篇技能给出矛盾指导时，让模型自己猜
     是错的 —— 它会挑那个看起来更权威的，而"看起来更权威"和
     "更该生效"经常不是一回事。

# 冲突怎么排序

按作用域的具体程度：`account > content_type > pipeline_stage > global`，
同作用域比 `priority`。排序在注入时**写明谁优先**，不留给模型猜。

# 和 aigc-agent 的差别

那边把正文 pin 进系统区靠的是 capability_budget 分配器；本骨架没有
分配器，也不打算有 —— 这里只做「筛 + 排 + 拼」，把拼好的文本交给调用方
放进它自己的位置。少一层抽象，也少一处会和实际行为脱节的地方
（那边的 `allocation.active.skill_body: 15000` 就是这种：读进来了，
但 `enforce()` 根本没看它）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .miniyaml import ParseError, parse as _parse_yaml

__all__ = [
    "ACTIVE_LIMIT",
    "BODY_BUDGET_TOKENS",
    "SCOPE_ORDER",
    "SkillMeta",
    "Selection",
    "assemble",
    "estimate_tokens",
    "load_body",
    "load_skills",
    "render_index",
    "select",
    "skill_dir",
    "skills",
]

#: 同时激活的技能篇数上限。三篇是「够用且不互相打架」的经验值。
ACTIVE_LIMIT = 3

#: 激活正文的合计 token 上限。超过就得拆文档 —— 一篇讲不透的事，
#: 放到两个 stage 去讲比硬塞进一篇更有效。
BODY_BUDGET_TOKENS = 15_000

#: 作用域的具体程度。数字大 = 更具体 = 冲突时优先。
SCOPE_ORDER: Dict[str, int] = {
    "account": 4,
    "content_type": 3,
    "pipeline_stage": 2,
    "global": 1,
}

VALID_STATUS = ("active", "draft", "placeholder")

#: 加载时直接跳过的文件名（README 是给人看的，`_` 开头的是模板）
_SKIP_NAMES = ("readme.md",)


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    中文一个字大约一个 token，英文一个词大约 1.3 个 token ——
    按字符数除以 1.6 估，对中英混排够用。**只用来决定"要不要拦住你"**，
    不参与任何计费。
    """
    if not text:
        return 0
    return max(1, int(len(text) / 1.6))


def skill_dir(root: Path) -> Path:
    return Path(root) / "skills"


def skills(root: Path) -> List["SkillMeta"]:
    """读一个仓库里的全部技能元数据。"""
    return load_skills(root)


@dataclass
class SkillMeta:
    """一篇技能的**元数据**。正文不在里面 —— 那是两级披露的全部意义。"""

    name: str
    description: str = ""
    scope: str = "global"
    applies_to: Tuple[str, ...] = ()
    stages: Tuple[str, ...] = ()
    priority: int = 50
    version: str = ""
    owner: str = ""
    status: str = "active"
    title: str = ""
    path: Optional[Path] = None
    problems: Tuple[str, ...] = ()

    # ———————————————— 判定
    @property
    def is_active(self) -> bool:
        """`draft` / `placeholder` 一律不加载。

        placeholder 的意义是"我占了这个位置，但还没写" —— 让它进上下文
        等于让模型读到一篇空文档然后自己编。
        """
        return self.status == "active"

    @property
    def specificity(self) -> int:
        return SCOPE_ORDER.get(self.scope, 0)

    def matches(self, *, content_type: str = "", stage: str = "") -> bool:
        """规则预筛：**在进入模型视野之前**就把不相关的挡掉。

        空表示不限。这就是那种"某个键从来没被读到过"的典型位置 ——
        写反了不会报错，只会让某几篇技能永远选不上，或者永远被选上。
        """
        if self.applies_to and content_type and content_type not in self.applies_to:
            return False
        if self.applies_to and not content_type:
            return False
        if self.stages and stage and stage not in self.stages:
            return False
        if self.stages and not stage:
            return False
        return True

    @property
    def sort_key(self) -> Tuple[int, int, str]:
        # 越具体越优先，其次看 priority，最后用名字定序（保证结果可复现）
        return (-self.specificity, -self.priority, self.name)

    def index_line(self) -> str:
        """常驻上下文的那一行。约 50 token —— 这是唯一的长期成本。"""
        return f"- {self.name}: {self.description or '（没写 description，不会被选中）'}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "applies_to": list(self.applies_to),
            "stages": list(self.stages),
            "priority": self.priority,
            "version": self.version,
            "owner": self.owner,
            "status": self.status,
            "title": self.title,
            "path": str(self.path) if self.path else "",
            "problems": list(self.problems),
        }


# ————————————————————————————————————————---- 解析


def _split_frontmatter(text: str, path: Path) -> Tuple[Dict[str, Any], str, str]:
    """切出 (元数据, 正文, 标题)。没有 frontmatter 就返回空元数据。"""
    if not text.startswith("---"):
        body = text
        title = _first_heading(body)
        return {}, body, title
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ParseError(f"{path.name} 的 frontmatter 没有结尾的 `---`")
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    meta = _parse_yaml(raw, source=f"{path.name} frontmatter") if raw.strip() else {}
    if not isinstance(meta, dict):
        raise ParseError(f"{path.name} 的 frontmatter 不是一个映射")
    return meta, body, _first_heading(body)


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _as_tuple(value: Any) -> Tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return (str(value).strip(),)


#: frontmatter 里认识的键。不认识的键报错 —— 拼错一个键名（比如 `stage`
#: 写成 `stages`）会让作用域静默失效，而那正是最难查的一类问题。
_KNOWN_KEYS = {
    "name", "description", "scope", "applies_to", "stage", "stages",
    "priority", "version", "owner", "status",
}


def parse_skill(text: str, path: Path) -> SkillMeta:
    meta, _body, title = _split_frontmatter(text, path)
    unknown = sorted(set(meta) - _KNOWN_KEYS)
    if unknown:
        raise ParseError(
            f"{path.name} 的 frontmatter 里有不认识的键：{', '.join(unknown)}\n"
            f"  认识的键：{', '.join(sorted(_KNOWN_KEYS))}"
        )
    scope = str(meta.get("scope", "global") or "global").strip() or "global"
    problems: List[str] = []
    if scope not in SCOPE_ORDER:
        raise ParseError(
            f"{path.name} 的 scope={scope!r} 不认识；"
            f"只能是 {'/'.join(sorted(SCOPE_ORDER, key=lambda k: -SCOPE_ORDER[k]))}"
        )
    status = str(meta.get("status", "active") or "active").strip() or "active"
    if status not in VALID_STATUS:
        raise ParseError(
            f"{path.name} 的 status={status!r} 不认识；只能是 {'/'.join(VALID_STATUS)}"
        )
    name = str(meta.get("name", "") or "").strip()
    if not name:
        # 没有 name 不算错误（老文档就这么用的），但要能被指出来
        name = path.parent.name if path.name.lower() == "skill.md" else path.stem
        problems.append("frontmatter 里没写 name，按文件名推断")
    description = str(meta.get("description", "") or "").strip()
    if not description:
        problems.append("没写 description —— 它永远不会被选中")
    elif len(description) < 10:
        problems.append(f"description 太短（{len(description)} 字），说明不了什么时候该用它")
    if not str(meta.get("owner", "") or "").strip():
        problems.append("没写 owner，改坏了不知道该找谁")
    try:
        priority = int(meta.get("priority", 50))
    except (TypeError, ValueError):
        raise ParseError(f"{path.name} 的 priority 不是整数：{meta.get('priority')!r}")
    return SkillMeta(
        name=name,
        description=description,
        scope=scope,
        applies_to=_as_tuple(meta.get("applies_to")),
        stages=_as_tuple(meta.get("stage", meta.get("stages"))),
        priority=priority,
        version=str(meta.get("version", "") or ""),
        owner=str(meta.get("owner", "") or ""),
        status=status,
        title=title,
        path=path,
        problems=tuple(problems),
    )


def load_skills(root: Path) -> List[SkillMeta]:
    """扫 `skills/` 下的平铺 `.md` 与 `名字/SKILL.md` 目录形态。

    返回全部（含 draft/placeholder）—— 筛不筛是调用方的事，
    但"哪些没被加载、为什么"必须能查。
    """
    base = skill_dir(root)
    out: List[SkillMeta] = []
    if not base.is_dir():
        return out
    for path in sorted(base.glob("*.md")):
        if path.name.startswith("_") or path.name.lower() in _SKIP_NAMES:
            continue
        out.append(parse_skill(path.read_text(encoding="utf-8"), path))
    for path in sorted(base.glob("*/SKILL.md")):
        if path.parent.name.startswith("_"):
            continue
        out.append(parse_skill(path.read_text(encoding="utf-8"), path))
    return out


def load_body(meta: SkillMeta) -> str:
    """取正文（去掉 frontmatter）。**这是唯一会花大钱的一步。**"""
    if meta.path is None:
        return ""
    _meta, body, _title = _split_frontmatter(meta.path.read_text(encoding="utf-8"), meta.path)
    return body.strip()


def render_index(metas: Sequence[SkillMeta], *, only_active: bool = True) -> str:
    """常驻上下文的那份目录。

    只放 name + description。每条约 50 token —— 这是两级披露省下来的东西：
    六篇技能的正文是一万多 token，目录是三百多。
    """
    rows = [m for m in metas if m.is_active or not only_active]
    rows = sorted(rows, key=lambda m: m.sort_key)
    if not rows:
        return "（没有可用的技能）"
    return "\n".join(m.index_line() for m in rows)


# ————————————————————————————————————————---- 选择


@dataclass
class Selection:
    """一次装载的结果。**把筛掉的也留着** —— "为什么不生效"要能回答。"""

    chosen: List[SkillMeta] = field(default_factory=list)
    skipped_scope: List[SkillMeta] = field(default_factory=list)
    skipped_status: List[SkillMeta] = field(default_factory=list)
    skipped_budget: List[SkillMeta] = field(default_factory=list)
    index_text: str = ""
    body_tokens: int = 0

    def explain(self) -> str:
        lines = []
        for m in self.chosen:
            lines.append(f"  [载] {m.name}（{m.scope}，priority {m.priority}）")
        for m in self.skipped_scope:
            lines.append(f"  [过] {m.name}：作用域不匹配这次的内容形态/环节")
        for m in self.skipped_status:
            lines.append(f"  [过] {m.name}：status={m.status}")
        for m in self.skipped_budget:
            lines.append(
                f"  [过] {m.name}：正文 {estimate_tokens(load_body(m))} token，"
                f"加进来会超 {BODY_BUDGET_TOKENS} 的激活预算"
            )
        return "\n".join(lines) or "  （没有技能）"


def select(
    metas: Sequence[SkillMeta],
    *,
    content_type: str = "",
    stage: str = "",
    limit: int = ACTIVE_LIMIT,
    budget_tokens: int = BODY_BUDGET_TOKENS,
) -> Selection:
    """规则预筛 → 按具体程度排序 → 按预算装载正文。

    **这一步不看 description。** 本骨架没有"让模型读描述自己挑"的循环
    （那是 agent 运行时的事），所以这里只做确定性的一半：
    规则筛 + 排序 + 预算。给模型挑的那一半，交给调用方 —— 它拿到的
    `index_text` 就是给模型看的候选清单。
    """
    sel = Selection()
    sel.index_text = render_index(metas)

    candidates: List[SkillMeta] = []
    for m in metas:
        if not m.is_active:
            sel.skipped_status.append(m)
            continue
        if not m.matches(content_type=content_type, stage=stage):
            sel.skipped_scope.append(m)
            continue
        candidates.append(m)

    candidates.sort(key=lambda m: m.sort_key)
    spent = 0
    for m in candidates:
        if len(sel.chosen) >= limit:
            sel.skipped_budget.append(m)
            continue
        cost = estimate_tokens(load_body(m))
        # 第一篇无论多大都要装 —— 否则一篇超长的技能会把这一轮变成
        # "零技能可用"，而失败方式是完全静默的（模型只是少了指导）。
        # 超长的问题由 `skills --check` 报出来给人看，不在运行时偷偷砍掉指导。
        if sel.chosen and spent + cost > budget_tokens:
            sel.skipped_budget.append(m)
            continue
        sel.chosen.append(m)
        spent += cost
    sel.body_tokens = spent
    return sel


def assemble(
    metas: Sequence[SkillMeta],
    *,
    content_type: str = "",
    stage: str = "",
    limit: int = ACTIVE_LIMIT,
    budget_tokens: int = BODY_BUDGET_TOKENS,
) -> Tuple[str, Selection]:
    """拼出可直接塞进提示词的文本。

    排序在注入时**写明谁优先**：两篇技能指导冲突时，让模型自己猜
    它是会挑"看起来更权威"的那篇，而不是"更该生效"的那篇。
    """
    sel = select(metas, content_type=content_type, stage=stage,
                 limit=limit, budget_tokens=budget_tokens)
    if not sel.chosen:
        return "", sel

    blocks: List[str] = []
    for i, m in enumerate(sel.chosen, start=1):
        header = f"【技能 {i}/{len(sel.chosen)} · {m.name}】"
        if m.title:
            header += f"（{m.title}）"
        note = _precedence_note(sel.chosen, i - 1)
        body = load_body(m).strip()
        blocks.append(f"{header}\n{note}\n{body}" if note else f"{header}\n{body}")
    return "\n\n".join(blocks), sel


def _precedence_note(chosen: List[SkillMeta], idx: int) -> str:
    """只有真的可能冲突时才写那句优先声明。

    每篇都写一遍"我是第几优先"是噪音，模型会把这段当模板忽略掉。
    """
    me = chosen[idx]
    more_specific = [m for j, m in enumerate(chosen) if j != idx and m.specificity > me.specificity]
    if not more_specific:
        return ""
    names = "、".join(m.name for m in more_specific)
    return (
        f"（作用域冲突时以 {names} 为准：{more_specific[0].scope} 比 {me.scope} 更具体）"
    )
