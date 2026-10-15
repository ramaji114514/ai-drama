"""质检：确定性检查，不调模型。

这个模块的存在本身是一条设计结论：

    **质检不是可选增强，是流水线的必要环节。**

来源是一次真实的提示词工程失败（参考项目 aigc-agent 的
`domain/realism.py` 文件头记录）：

    在提示词里写了"毛孔、雀斑"，出来的还是磨皮脸。
    也就是说 —— **光在提示词里写清楚是没用的**。
    最后是靠"提示词硬约束 + 描述清洗 + 生成后校验不合格重生成"三道一起上才解决。

于是骨架把"校验"做成流水线里的一个阶段（stage07_qc），而不是留给你自觉。

# 三条判定，不是两条

最重要的设计是 `QcState` 的第三态：

    PASS       查了，通过
    FAIL       查了，不通过
    UNCHECKED  **没查成**（文件还没生成 / 读不出来 / 检查器自己出错）

把 UNCHECKED 单独列出来，是因为它是**最容易被误当成"通过"的状态**：
一个空目录、一个读不出来的时长、一个没装的 ffprobe，都很容易被写成
"没有发现问题，继续"。而"没有发现问题"和"没去查"是两件完全不同的事。

本模块对 UNCHECKED 的处理是：**按未通过对待**（必须显式出现在报告里、
默认拦住"done"），只有你明确关掉严格模式才放行。
参考项目里 `identity.py:112-115` 对同一问题的注释写得很清楚：
    "视频段按「没过」算：不进成片、重跑时补查"
而同一个仓库的 `realism.py:553-554` 却写着"解析不出来按通过处理" ——
两套策略并存说明作者自己也在摇摆。骨架选前者，并且**只选一套**。

# 规则为什么必须限定作用域

参考项目 `domain/compliance/__init__.py:115-118` 的血泪注释：

    广告法极限词是给营销文案的：打在剧本台词上，"最强""绝对""第一"
    全判 block，实测**模型花了三个会话、四十多元把 60 集重写一遍**去删这些词。

所以每条规则都带 `applies_to`（内容形态）与 `stage`（生效阶段），
在进入模型视野**之前**就被筛掉，不消耗任何 token，也不会误伤别的形态。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields as _dc_fields
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ————————————————————————————————————————----
# 三态判定
# ————————————————————————————————————————----


class QcState(str, Enum):
    """质检结论。注意它是三态 —— 第三态是这个模块最重要的东西。"""

    PASS = "pass"
    FAIL = "fail"
    UNCHECKED = "unchecked"

    @property
    def ok(self) -> bool:
        return self is QcState.PASS

    def __str__(self) -> str:                      # 方便直接 f-string
        return self.value


class Severity(str, Enum):
    """问题的严重度。只有 block 会让整体判 fail。"""

    BLOCK = "block"     # 必须处理，拦住"done"
    WARN = "warn"       # 报出来给人看，不拦
    INFO = "info"       # 仅供参考


_SEVERITY_MARK = {Severity.BLOCK: "[X]", Severity.WARN: "[!]", Severity.INFO: "[i]"}
_SEVERITY_ORDER = {Severity.BLOCK: 0, Severity.WARN: 1, Severity.INFO: 2}


@dataclass
class Finding:
    """一条质检结论。

    `unchecked=True` 时表示**这条没查成**，此时 severity 没有意义 ——
    不要把它当成"没问题"。`where` 是定位（镜头 / 文件 / 规则名），
    `detail` 是给人看的解释，`suggestion` 是改法（不是自动改）。
    """

    rule: str
    severity: Severity
    message: str
    where: str = ""
    detail: str = ""
    suggestion: str = ""
    unchecked: bool = False

    def render(self) -> str:
        if self.unchecked:
            mark = "[?]"
        else:
            mark = _SEVERITY_MARK.get(self.severity, "[?]")
        loc = f"（{self.where}）" if self.where else ""
        line = f"{mark} {self.rule}{loc}：{self.message}"
        if self.detail:
            line += f"\n      {self.detail}"
        if self.suggestion:
            line += f"\n      -> {self.suggestion}"
        return line

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "message": self.message,
            "where": self.where,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "unchecked": self.unchecked,
        }


# ————————————————————————————————————————----
# 规则
# ————————————————————————————————————————----


@dataclass
class QcRules:
    """质检规则。

    默认值就是一套能跑的规则，`config/qc_rules.yaml` 存在时覆盖它。
    **规则是数据不是代码** —— 调阈值不该改 Python。
    """

    version: str = "1"
    #: 这条规则管哪些内容形态。空 = 全部。
    applies_to: Tuple[str, ...] = ()
    #: 这条规则在哪些阶段生效。空 = 全部。
    stages: Tuple[str, ...] = ()

    #: 单镜台词上限（中日韩字符 / 拉丁字符分开算 —— 一个中文字的信息量
    #: 大约是一个英文单词的三分之一，用同一个数字量两种语言必错一边）
    max_dialogue_cjk: int = 60
    max_dialogue_latin: int = 180
    severity_dialogue: Severity = Severity.WARN

    #: 单镜时长区间（创作侧约束，provider 的能力上限另由 ProviderCaps 管）
    min_shot_seconds: float = 1.0
    max_shot_seconds: float = 15.0
    severity_duration: Severity = Severity.BLOCK

    #: 画面提示词重复：同一段 action/prompt 出现在多个镜头里
    duplicate_min_chars: int = 12
    duplicate_ratio: float = 0.6
    severity_duplicate: Severity = Severity.WARN

    #: 每场至少用几种景别（全是中景 = 幻灯片）
    min_distinct_framings: int = 3
    severity_framing: Severity = Severity.INFO

    #: 每场最后一镜要留钩子（短剧的平台要求：前 15 秒给高潮、集尾留悬念）
    require_scene_hook: bool = True
    severity_hook: Severity = Severity.WARN

    #: 画面提示词里不许出现听觉信息（台词/音效/旁白必须走单独字段）
    forbidden_in_visual: Tuple[str, ...] = (
        "旁白", "画外音", "音效", "配乐", "BGM", "字幕",
        "narration", "voiceover", "sound effect", "subtitle",
    )
    severity_auditory: Severity = Severity.BLOCK

    #: 素材完整性
    require_assets: bool = True
    severity_assets: Severity = Severity.BLOCK

    #: 有台词的镜头必须真的做过口型同步。
    #: **默认关**：口型同步是可选增强（`AI_DRAMA_LIPSYNC=none` 时整个阶段会被
    #: 跳过），默认打开会让所有没配 provider 的人一上来就 fail。
    #: 打开它之后，"嘴型没对上"就不再只是一条 note，而是拦住 done。
    require_lipsync: bool = False
    severity_lipsync: Severity = Severity.BLOCK
    #: 有台词但没同步的镜头超过这个比例就报（配合 require_lipsync 用）。
    #: 想严格到"一个都不能少"就把 require_lipsync 打开、这里设 0。
    lipsync_missing_ratio: float = 0.0

    # ———— 下面两条要**调模型看图**，所以默认关（见 core/vision_qc.py）
    #
    # 关掉不等于"没这项要求"，而是"没人看过画面，我不假装看过"：
    # 打开后 provider 缺席 / 调用失败一律变成 unchecked（没查成），
    # 而 unchecked 在严格模式下**不算通过**。

    #: 画面里不许出现文字（招牌/门牌/水印/字幕）。
    #: 值得单独一项：模型很爱在招牌和屏幕上顺手写字，而它写的中文基本都是错的 ——
    #: 一部剧里出现三个错别字招牌，观众立刻知道这是 AI 生成的。
    #: 分工是「生成的画面里不许有字，字全部后期烧」（见 stage07_concat）。
    require_no_text: bool = False
    severity_no_text: Severity = Severity.BLOCK

    #: 画面里的人要和角色的定妆照一致（把关键帧和定妆照一起发给视觉模型）。
    #: 判定逻辑与三态语义早就写好了（core/identity.py），缺的一直是
    #: "真能看图的那一层" —— 现在由 vision provider 补上。
    require_identity: bool = False
    severity_identity: Severity = Severity.BLOCK
    #: 一致性打分门槛（0-10）。低于它按不通过处理。
    identity_pass_score: int = 7
    #: 最多查几个镜头（0 = 全查）。每查一镜就是一次调用，所以要有个闸门；
    #: 抽样是**等距**的，不是取前 N 个 —— 取前 N 个会把检查范围永远钉在开头几场，
    #: 而后半部恰恰是最容易漂的地方。
    vision_max_checks: int = 0

    @classmethod
    def load(cls, path: Optional[Path]) -> "QcRules":
        """从 YAML 读规则；文件不存在或读不动就退回默认值（并保留原因）。

        刻意**不引入 PyYAML**：骨架的承诺是零依赖可跑，而这里真正需要的
        只有"标量 + 字符串列表"两种形态。复杂 YAML 特性一律不支持，
        遇到不认识的写法会**抛错而不是静默忽略** —— 静默忽略配置
        是这类"改了参数没生效"事故的根源。
        """
        if path is None or not Path(path).exists():
            return cls()
        raw = _mini_yaml(Path(path).read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)          # type: ignore[attr-defined]
        kwargs: Dict[str, Any] = {}
        for key, value in raw.items():
            if key not in known:
                raise ValueError(
                    f"qc_rules.yaml 里有不认识的键 '{key}'。"
                    f"已知键：{sorted(known)}。"
                    f"（宁可报错也不静默忽略 —— 否则你会以为参数生效了）"
                )
            kwargs[key] = value
        for sev_key in ("severity_dialogue", "severity_duration", "severity_duplicate",
                        "severity_framing", "severity_hook", "severity_auditory",
                        "severity_assets", "severity_lipsync",
                        "severity_no_text", "severity_identity"):
            if sev_key in kwargs and not isinstance(kwargs[sev_key], Severity):
                kwargs[sev_key] = Severity(str(kwargs[sev_key]))
        for tup_key in ("applies_to", "stages", "forbidden_in_visual"):
            if tup_key in kwargs:
                v = kwargs[tup_key]
                kwargs[tup_key] = tuple(v) if isinstance(v, (list, tuple)) else (str(v),)
        # 版本号必须当字符串：YAML 里写 '2' 或 2 都得是 "2"。
        # 否则 `rules_version: 2` 和 `rules_version: "2"` 会在报告里长得不一样，
        # 而报告的意义恰恰是"这份结论是按哪一版规则得出的"。
        if "version" in kwargs:
            kwargs["version"] = str(kwargs["version"])
        return cls(**kwargs)

    def matches(self, *, content_type: str = "", stage: str = "") -> bool:
        """规则作用域匹配。空表示不限。"""
        if self.applies_to and content_type and content_type not in self.applies_to:
            return False
        if self.stages and stage and stage not in self.stages:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        """全部字段，一个不漏。

        刻意**手写字段名而不是遍历 dataclass**是错的 —— 一开始这里就是手写的，
        结果新加的三个 severity_* 和 forbidden_in_visual 全都从输出里消失了，
        而 `rules` 命令的全部意义就是让你看见"现在生效的到底是什么"。
        所以这里遍历字段，新增字段自动出现。
        """
        out: Dict[str, Any] = {}
        for f in _dc_fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Severity):
                out[f.name] = value.value
            elif isinstance(value, tuple):
                out[f.name] = list(value)
            else:
                out[f.name] = value
        return out


# ————————————————————————————————————————----
# 报告
# ————————————————————————————————————————----


@dataclass
class QcReport:
    """一次质检的完整结论。"""

    run_id: str = ""
    content_type: str = "短剧"
    stage: str = "qc"
    checked_at: str = ""
    rules_version: str = "1"
    findings: List[Finding] = field(default_factory=list)
    #: 检查器自己跑不起来的原因（例如 ffprobe 缺失）。非空即整体 UNCHECKED。
    blocked_reason: str = ""
    #: 素材检查是否**真的**执行了。false 时所有素材结论都是 UNCHECKED。
    assets_checked: bool = True
    #: 统计用的输入规模
    shots_total: int = 0

    # ———— 汇总

    @property
    def blocks(self) -> List[Finding]:
        return [f for f in self.findings
                if not f.unchecked and f.severity is Severity.BLOCK]

    @property
    def warns(self) -> List[Finding]:
        return [f for f in self.findings
                if not f.unchecked and f.severity is Severity.WARN]

    @property
    def unchecked(self) -> List[Finding]:
        return [f for f in self.findings if f.unchecked]

    @property
    def state(self) -> QcState:
        """整体判定。

        顺序很重要：**先看有没有确定性的失败，再看有没有没查成的。**
        反过来会让"没查成"掩盖掉确定的坏消息。
        """
        if self.blocked_reason:
            return QcState.UNCHECKED
        if self.blocks:
            return QcState.FAIL
        if self.unchecked:
            return QcState.UNCHECKED
        return QcState.PASS

    @property
    def passed(self) -> bool:
        """只有 PASS 才算通过。UNCHECKED **不算通过**。"""
        return self.state is QcState.PASS

    def counts(self) -> Dict[str, int]:
        return {
            "block": len(self.blocks),
            "warn": len(self.warns),
            "unchecked": len(self.unchecked),
            "info": len([f for f in self.findings
                         if not f.unchecked and f.severity is Severity.INFO]),
        }

    def render(self, *, limit: int = 40) -> str:
        c = self.counts()
        head = (
            f"质检 {self.state.value.upper()} — {self.shots_total} 个镜头；"
            f"block {c['block']} / warn {c['warn']} / 未查成 {c['unchecked']}"
        )
        lines = [head]
        if self.blocked_reason:
            lines.append(f"[?] 检查器未能运行：{self.blocked_reason}")
        ordered = sorted(
            self.findings,
            key=lambda f: (0 if f.unchecked else _SEVERITY_ORDER.get(f.severity, 3)),
        )
        for f in ordered[:limit]:
            lines.append(f.render())
        if len(ordered) > limit:
            lines.append(f"... 还有 {len(ordered) - limit} 条，见 qc_report.json")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "content_type": self.content_type,
            "stage": self.stage,
            "checked_at": self.checked_at,
            "rules_version": self.rules_version,
            "state": self.state.value,
            "passed": self.passed,
            "counts": self.counts(),
            "blocked_reason": self.blocked_reason,
            "assets_checked": self.assets_checked,
            "shots_total": self.shots_total,
            "findings": [f.to_dict() for f in self.findings],
        }


# ————————————————————————————————————————----
# 检查器
# ————————————————————————————————————————----


_CJK_RANGES = (
    ("\u4e00", "\u9fff"),    # CJK 统一表意
    ("\u3040", "\u30ff"),    # 假名
    ("\uac00", "\ud7af"),    # 谚文
)


def has_cjk(text: str) -> bool:
    return any(lo <= ch <= hi for ch in text for lo, hi in _CJK_RANGES)


def _visual_text(shot) -> str:
    """一个镜头里所有"会被送进画面模型"的文字。

    台词（dialogue）**刻意不在里面** —— 它走 TTS，不该进画面提示词。
    """
    return " ".join(x for x in (
        getattr(shot, "action", "") or "",
        getattr(shot, "image_prompt", "") or "",
        getattr(shot, "video_prompt", "") or "",
    ) if x).strip()


class QcChecker:
    """按规则检查一部片子。

    构造时传入一个 `assets` 回调（可选）用来检查素材是否落盘：

        assets(shot) -> (是否存在, 缺失说明)

    不传就表示"素材没查成"，所有素材结论都是 UNCHECKED ——
    这正是我们要的：**没查成必须说出来，不能默认通过。**
    """

    def __init__(self, rules: Optional[QcRules] = None, *,
                 content_type: str = "短剧",
                 assets: Optional[Any] = None,
                 runtime: Optional[Any] = None):
        self.rules = rules or QcRules()
        self.content_type = content_type
        self.assets = assets
        self.runtime = runtime                 # 提供 probe_duration 等能力，可为 None

    # ———— 单项检查

    def _check_dialogue(self, shots: Sequence[Any]) -> List[Finding]:
        r = self.rules
        out: List[Finding] = []
        for s in shots:
            text = (getattr(s, "dialogue", "") or "").strip()
            if not text:
                continue
            limit = r.max_dialogue_cjk if has_cjk(text) else r.max_dialogue_latin
            if len(text) > limit:
                out.append(Finding(
                    rule="dialogue-length", severity=r.severity_dialogue,
                    message=f"台词 {len(text)} 字，超过 {limit} 字",
                    where=_where(s),
                    detail=text[:60] + ("..." if len(text) > 60 else ""),
                    suggestion="拆成两个镜头，或压缩台词 —— 一镜说太多话会来不及演",
                ))
        return out

    def _check_duration(self, shots: Sequence[Any]) -> List[Finding]:
        r = self.rules
        out: List[Finding] = []
        for s in shots:
            d = float(getattr(s, "duration", 0.0) or 0.0)
            if d <= 0:
                out.append(Finding(
                    rule="shot-duration", severity=r.severity_duration,
                    message="镜头时长为 0 或未设置", where=_where(s),
                    suggestion="检查分镜表里的 duration 字段",
                ))
            elif d < r.min_shot_seconds:
                out.append(Finding(
                    rule="shot-duration", severity=r.severity_duration,
                    message=f"镜头只有 {d:.2f}s，短于下限 {r.min_shot_seconds:g}s",
                    where=_where(s), suggestion="太短看不清，合并到相邻镜头",
                ))
            elif d > r.max_shot_seconds:
                out.append(Finding(
                    rule="shot-duration", severity=r.severity_duration,
                    message=f"镜头 {d:.2f}s，长于 {r.max_shot_seconds:g}s",
                    where=_where(s),
                    suggestion="拆成多个镜头；短剧的节奏靠切，不靠长镜头",
                ))
        return out

    def _check_duplicate(self, shots: Sequence[Any]) -> List[Finding]:
        r = self.rules
        seen: Dict[str, List[str]] = {}
        for s in shots:
            text = _visual_text(s)
            key = " ".join(text.split())
            if len(key) >= r.duplicate_min_chars:
                seen.setdefault(key, []).append(_where(s))
        out: List[Finding] = []
        dup_total = sum(len(v) - 1 for v in seen.values() if len(v) > 1)
        if not len(shots):
            return out
        if dup_total / max(1, len(shots)) >= r.duplicate_ratio and seen:
            out.append(Finding(
                rule="duplicate-visual", severity=r.severity_duplicate,
                message=f"{dup_total} 个镜头的画面提示词与别的镜头完全相同",
                where="整片",
                detail="；".join(
                    ", ".join(v) for v in seen.values() if len(v) > 1
                )[:400],
                suggestion="每个镜头应有自己的机位与动作 —— 重复的提示词会生成重复的画面",
            ))
        return out

    def _check_framing(self, scenes: Sequence[Any]) -> List[Finding]:
        r = self.rules
        out: List[Finding] = []
        for sc in scenes:
            shots = list(getattr(sc, "shots", []) or [])
            if len(shots) < r.min_distinct_framings:
                continue
            kinds = {str(getattr(s, "framing", "") or "") for s in shots}
            kinds.discard("")
            if len(kinds) < r.min_distinct_framings:
                out.append(Finding(
                    rule="framing-diversity", severity=r.severity_framing,
                    message=f"这一场只用了 {len(kinds)} 种景别（建议至少 "
                            f"{r.min_distinct_framings} 种）",
                    where=getattr(sc, "scene_id", "?"),
                    detail=f"出现过的景别：{', '.join(sorted(kinds)) or '（空）'}",
                    suggestion="全中景会像幻灯片；远景交代空间、特写交代情绪",
                ))
        return out

    def _check_hook(self, scenes: Sequence[Any]) -> List[Finding]:
        r = self.rules
        if not r.require_scene_hook:
            return []
        out: List[Finding] = []
        for sc in scenes:
            shots = list(getattr(sc, "shots", []) or [])
            if not shots:
                continue
            last = shots[-1]
            text = " ".join(x for x in (
                (getattr(last, "action", "") or ""),
                (getattr(last, "dialogue", "") or ""),
            ) if x).strip()
            if len(text) < 6:
                out.append(Finding(
                    rule="scene-hook", severity=r.severity_hook,
                    message="这一场的最后一镜没有可识别的钩子内容",
                    where=getattr(sc, "scene_id", "?"),
                    detail=f"最后一镜 {_where(last)} 的动作/台词：{text or '（空）'}",
                    suggestion="短剧每场结尾要留悬念，否则观众不会点下一集",
                ))
        return out

    def _check_auditory(self, shots: Sequence[Any]) -> List[Finding]:
        r = self.rules
        out: List[Finding] = []
        for s in shots:
            visual = _visual_text(s)
            low = visual.lower()
            hit = [w for w in r.forbidden_in_visual if w and w.lower() in low]
            if hit:
                out.append(Finding(
                    rule="auditory-in-visual", severity=r.severity_auditory,
                    message=f"画面提示词里出现了听觉信息：{', '.join(hit)}",
                    where=_where(s), detail=visual[:120],
                    suggestion="台词/音效/旁白必须放在 dialogue 字段，"
                               "它们进画面模型只会污染生成结果",
                ))
        return out

    def _check_lipsync(self, shots: Sequence[Any]) -> List[Finding]:
        """有台词的镜头是不是真的对过嘴。

        默认不查（`require_lipsync` 关着）—— 口型同步是可选增强，
        没配 provider 时整个阶段会被跳过，默认打开会让所有人都 fail。

        打开之后判断的是**产物在不在**，而不是"provider 报没报成功"：
        报告可以是假的，磁盘上一份 `shot_XXX_synced.mp4` 假不了。
        """
        r = self.rules
        if not r.require_lipsync:
            return []
        talking = [s for s in shots if (getattr(s, "dialogue", "") or "").strip()]
        if not talking:
            return []
        missing: List[str] = []
        for s in talking:
            raw = getattr(s, "synced", None)
            ok = False
            if raw:
                try:
                    p = Path(raw)
                    ok = p.exists() and p.stat().st_size > 0
                except OSError:
                    ok = False
            if not ok:
                missing.append(_where(s))
        if len(missing) / len(talking) <= r.lipsync_missing_ratio:
            return []
        return [Finding(
            rule="lipsync-present", severity=r.severity_lipsync,
            message=f"{len(missing)}/{len(talking)} 个有台词的镜头没有口型同步产物",
            where="整片",
            detail="，".join(missing[:20]),
            suggestion="接上 lip-sync provider（AI_DRAMA_LIPSYNC）后跑 "
                       "`run.py resume <run_id> --stages lipsync,concat`；"
                       "或者把 config/qc_rules.yaml 的 require_lipsync 关掉",
        )]

    def _check_assets(self, shots: Sequence[Any]) -> Tuple[List[Finding], bool]:
        """素材完整性。返回值第二项是"到底查成了没有"。"""
        if self.assets is None:
            return ([Finding(
                rule="assets-present", severity=Severity.BLOCK, unchecked=True,
                message="素材未检查（没有可用的检查器）",
                where="整片",
                suggestion="跑完整流水线后再质检，或显式关闭 require_assets",
            )], False)

        out: List[Finding] = []
        checked = True
        for s in shots:
            try:
                ok, missing = self.assets(s)
            except Exception as exc:                   # noqa: BLE001
                checked = False
                out.append(Finding(
                    rule="assets-present", severity=Severity.BLOCK, unchecked=True,
                    message=f"素材检查抛错：{type(exc).__name__}: {exc}",
                    where=_where(s),
                ))
                continue
            if not ok:
                out.append(Finding(
                    rule="assets-present", severity=self.rules.severity_assets,
                    message="素材不完整", where=_where(s), detail=str(missing),
                    suggestion="补齐后再拼接；缺镜拼出来的成片会静默变短",
                ))
        return out, checked

    # ———— 入口

    def check(self, episode) -> QcReport:
        from .models import now_iso

        report = QcReport(
            run_id=str(getattr(episode, "run_id", "") or ""),
            content_type=self.content_type,
            checked_at=now_iso(),
            rules_version=self.rules.version,
        )
        shots = list(getattr(episode, "shots", []) or [])
        scenes = list(getattr(episode, "scenes", []) or [])
        report.shots_total = len(shots)

        if not shots:
            report.blocked_reason = "这部片子没有任何镜头，质检无从谈起"
            return report

        try:
            report.findings += self._check_dialogue(shots)
            report.findings += self._check_duration(shots)
            report.findings += self._check_duplicate(shots)
            report.findings += self._check_framing(scenes)
            report.findings += self._check_hook(scenes)
            report.findings += self._check_auditory(shots)
            report.findings += self._check_lipsync(shots)
            if self.rules.require_assets:
                asset_findings, checked = self._check_assets(shots)
                report.findings += asset_findings
                report.assets_checked = checked
        except Exception as exc:                       # noqa: BLE001
            # 检查器自己崩了 —— 这**不是通过**，是没查成。
            report.blocked_reason = f"检查器抛错：{type(exc).__name__}: {exc}"
        return report


def _where(shot) -> str:
    label = getattr(shot, "label", None)
    if label:
        return str(label)
    return f"{getattr(shot, 'scene_id', '?')}/shot_{int(getattr(shot, 'order', 0)):03d}"


# ————————————————————————————————————————----
# 极简 YAML
# ————————————————————————————————————————----
#
# 解析器本体在 core/miniyaml.py。规则文件和模型目录都要读配置，
# 两处各写一份解析器迟早会分叉（一边认 `[]`、另一边不认），
# 而分叉的表现就是"这个文件改了有用、那个文件改了没用"。
# 这里只留一个薄封装，把 source 固定成本文件名，让报错说得清是哪个文件。


def _mini_yaml(text: str) -> Dict[str, Any]:
    from .miniyaml import parse

    return parse(text, source="qc_rules.yaml")


# ————————————————————————————————————————----
# 便捷入口
# ————————————————————————————————————————----


def rules_path(root: Path) -> Path:
    """规则文件的位置。和 paths.rules_file() 是同一处，这里只是便捷转发。"""
    from .paths import rules_file

    return rules_file(root)


def default_rules(root: Optional[Path] = None) -> QcRules:
    p = rules_path(root) if root else None
    if p and p.exists():
        return QcRules.load(p)
    return QcRules()


__all__ = [
    "Finding", "QcChecker", "QcReport", "QcRules", "QcState", "Severity",
    "default_rules", "has_cjk", "rules_path",
]
