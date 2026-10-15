"""阶段 8：质检。

流水线的**最后一道闸**，也是上一版骨架里完全缺失的一环。

它检查的是"骨架自己有能力确定的事"，刻意不碰"画得好不好看"：

    * 台词会不会一镜说不完（分中日韩 / 拉丁两套字数上限）
    * 时长的创作侧边界（太短看不清、太长该切）
    * 画面提示词有没有被复制粘贴到多个镜头
    * 一场戏是不是只用了一种景别（全中景 = 幻灯片）
    * 每场结尾有没有钩子（短剧的平台要求）
    * **听觉信息有没有混进画面提示词**（参考项目的概念性错误，见下）
    * 关键帧 / 片段 / 配音是不是真的都在磁盘上
    * 要不要把"嘴型没同步"也当成不通过（`require_lipsync`）

上面这些**不调模型**（`core/qc.py` 是纯函数、离线可测）。另有两条
必须看图才能判的，由 `core/vision_qc.py` 负责，**默认关闭**：

    * 画面里有没有文字（`require_no_text`）
    * 画面里的人像不像定妆照（`require_identity`）

它们的结果并进**同一份** `QcReport` —— 一份报告写"block 0"，另一份文件里
写着"画面里有字"，等于没查。没有配 vision provider 时它们记成
`unchecked`（没查成），**不是通过**。

# 为什么"素材在不在"这一项是 BLOCK

参考项目 Open-AI-Micro-Drama-Generator 的 `script2video.py:97-110` 有一个
镜头索引错位的 bug：首帧失败的镜头被过滤掉后，`enumerate` 让下标重排，
视频被写到错误的镜头上。这类问题的共同点是**不报错**：流水线走完了，
成片也生成了，只是内容不对。所以缺素材必须是 block 级，而不是"记个 note"。

# 为什么"没查成"要单独成一态

`QcState.UNCHECKED` 表示检查器没能拿到结论（文件还没生成、ffprobe 缺失、
检查器自己抛错）。它**不算通过** —— 这正是参考项目 `identity.py:112-115`
的处理方式（"视频段按「没过」算：不进成片、重跑时补查"）。

默认严格模式开：只要不是 PASS，run 就不会被标成 done。
这样"质检没跑成"和"质检通过"在结果里永远不会看起来一样。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Tuple

from ..core import paths
from ..core.context import StageContext, StageReport
from ..core.qc import Finding, QcChecker, QcRules, QcState, Severity, rules_path
from ..core.models import CostEntry, Shot


def _make_asset_checker(ctx: StageContext):
    """返回一个 (shot) -> (是否齐全, 缺失说明) 的回调。

    素材路径**只从 paths 取**，不自己拼字符串 —— 这样质检看到的文件和
    流水线写出的文件必然是同一批（上一版骨架踩过"两处各自拼路径"的坑）。
    """
    def check(shot: Shot) -> Tuple[bool, str]:
        missing: List[str] = []
        kf = paths.keyframe(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        if not (kf.exists() and kf.stat().st_size > 0):
            missing.append(f"关键帧 {kf.name}")
        clip = paths.clip(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
        if not (clip.exists() and clip.stat().st_size > 0):
            missing.append(f"片段 {clip.name}")
        if (shot.dialogue or "").strip():
            voice = paths.voice(ctx.root, ctx.run.run_id, shot.scene_id, shot.order)
            if not (voice.exists() and voice.stat().st_size > 0):
                missing.append(f"配音 {voice.name}")
        return (not missing), "、".join(missing)

    return check


def _vision_checks(ctx: StageContext, report: StageReport, qc: Any,
                   rules: QcRules) -> None:
    """跑需要模型的检查，并把结论并进同一份报告。

    三条刻意的做法：

    1. **结论并进同一份 QcReport，而不是另发一份。** 一份报告里写着
       "block 0 / warn 0"，另一份文件里写着"画面里有字"，等于没查。
       三态汇总（`QcReport.state`）已经处理好了优先级，直接给它 Finding 就行。
    2. **provider 缺席 = 没查成，不是通过。** 这是整个 vision 层唯一重要的事。
    3. **花的钱要记账。** 每次调用都是真金白银，走和别处一样的 ledger。

    关掉时**什么都不说**（不产出 Finding）—— "没开这项检查"和"查了没查成"
    是两码事，后者要拦片子，前者只是没做。这一点和 qc.py 里
    "只有 note 没有 unchecked = 配置里就没开"是同一条规矩。
    """
    want_text = rules.require_no_text
    want_identity = rules.require_identity
    if not (want_text or want_identity):
        return

    vision = getattr(ctx.bundle, "vision", None)
    if vision is None:
        for rule, sev in (("vision-no-text", rules.severity_no_text),
                          ("vision-identity", rules.severity_identity)):
            if (rule == "vision-no-text") != bool(want_text):
                continue
            qc.findings.append(Finding(
                rule=rule, severity=sev, unchecked=True,
                message="要查画面，但没配 vision provider —— 画面没有被任何人看过",
                suggestion="设 AI_DRAMA_VISION=<能看图的 provider>，"
                           "或者把 config/qc_rules.yaml 里这一项关掉",
            ))
        report.notes.append(
            "[!] qc_rules.yaml 里开了画面校验，但 AI_DRAMA_VISION 没配 —— "
            "这两项记成「没查成」（不算通过）"
        )
        return

    from ..core.vision_qc import check_identity, check_no_text

    total_money = 0.0
    calls = 0
    if want_text:
        findings, money, stats = check_no_text(
            vision, ctx.root, ctx.run.run_id, ep_shots(ctx),
            max_checks=rules.vision_max_checks, severity=rules.severity_no_text,
        )
        qc.findings.extend(findings)
        total_money += money
        calls += stats["checked"] + stats["unchecked"]
        report.notes.append(
            f"画面文字检查：{stats['checked']} 镜查过 / {stats['failed']} 镜有字"
            f" / {stats['unchecked']} 镜没查成"
        )
    if want_identity:
        findings, money, stats = check_identity(
            vision, ctx.episode, ctx.root, ctx.run.run_id,
            max_checks=rules.vision_max_checks,
            pass_score=rules.identity_pass_score,
            severity=rules.severity_identity,
        )
        qc.findings.extend(findings)
        total_money += money
        calls += stats["checked"] + stats["unchecked"]
        report.notes.append(
            f"人物一致性检查：{stats['checked']} 镜查过 / {stats['failed']} 镜不像"
            f" / {stats['unchecked']} 镜没查成"
        )

    if total_money:
        # 走和别处一样的账：provider 只报告花了多少，落盘由 store 统一做。
        report.money += total_money
        report.notes.append(f"画面校验花费 ${total_money:.4f}")
        try:
            ctx.store.add_cost(CostEntry(
                run_id=ctx.run.run_id, stage="qc",
                provider=getattr(vision, "name", "vision"),
                unit="call", amount=float(calls), money=total_money,
            ))
        except Exception as exc:                      # noqa: BLE001
            report.notes.append(f"[!] 画面校验的费用没记上：{type(exc).__name__}: {exc}")


def ep_shots(ctx: StageContext) -> List[Shot]:
    return list(ctx.episode.shots)


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="qc")
    ep = ctx.episode

    if not ep.scenes:
        report.notes.append("没有分镜，无法质检")
        report.qc_state = QcState.UNCHECKED.value
        return report

    rules_path_ = rules_path(ctx.root)
    try:
        rules = QcRules.load(rules_path_)
    except Exception as exc:                          # noqa: BLE001
        # 规则文件坏了**不能静默用默认值**跑下去 —— 你会以为按新阈值查过了。
        report.notes.append(f"[!] 规则文件读取失败，退回默认规则：{type(exc).__name__}: {exc}")
        rules = QcRules()
    if rules_path_.exists():
        report.notes.append(f"规则：{rules_path_} (version {rules.version})")
    else:
        report.notes.append(f"规则：内置默认值（未找到 {rules_path_}）")

    checker = QcChecker(
        rules,
        content_type=str(ctx.run.meta.get("content_type") or "短剧"),
        assets=_make_asset_checker(ctx),
    )
    qc = checker.check(ep)

    # ———— 需要**看图**的那两项（默认关，见 core/vision_qc.py）
    _vision_checks(ctx, report, qc, rules)

    ctx.qc_report = qc

    out = paths.qc_report(ctx.root, ctx.run.run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(qc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report.artifacts["qc_report"] = str(out)
    report.qc_state = qc.state.value
    # 质检报告也进台账。它没有上游（不是"由哪件素材变来的"），但它**是**
    # 某一次判断的产物 —— 记下来，"这份报告说的是哪一版成片"才有答案。
    ctx.asset(out, kind="report", meta={"qc_state": qc.state.value,
                                        "findings": len(qc.findings)})

    counts = qc.counts()
    report.processed = qc.shots_total
    report.failed = counts["block"]
    report.notes.append(
        f"质检 {qc.state.value.upper()}：block {counts['block']} / warn {counts['warn']}"
        f" / 未查成 {counts['unchecked']}"
    )

    if ctx.verbose:
        ctx.log("")
        ctx.log(qc.render())

    if qc.state is not QcState.PASS:
        if ctx.strict_qc:
            report.notes.append(
                "[!] 严格质检已开启：非 PASS 的 run 不会被标记为完成"
                "（用 --no-strict-qc 放行，或在 config/qc_rules.yaml 里放宽规则）"
            )
        else:
            report.notes.append(
                f"[!] 质检未通过（{qc.state.value}），但严格模式已关闭 —— "
                f"结果会被放行。要拦住它请去掉 --no-strict-qc。"
            )
    return report
