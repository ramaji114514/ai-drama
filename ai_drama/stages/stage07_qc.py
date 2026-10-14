"""阶段 7：质检（确定性，不调模型）。

流水线的**最后一道闸**，也是上一版骨架里完全缺失的一环。

它检查的是"骨架自己有能力确定的事"，刻意不碰"画得好不好看"：

    * 台词会不会一镜说不完（分中日韩 / 拉丁两套字数上限）
    * 时长的创作侧边界（太短看不清、太长该切）
    * 画面提示词有没有被复制粘贴到多个镜头
    * 一场戏是不是只用了一种景别（全中景 = 幻灯片）
    * 每场结尾有没有钩子（短剧的平台要求）
    * **听觉信息有没有混进画面提示词**（参考项目的概念性错误，见下）
    * 关键帧 / 片段 / 配音是不是真的都在磁盘上

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
from ..core.models import Shot


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
    ctx.qc_report = qc

    out = paths.qc_report(ctx.root, ctx.run.run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(qc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report.artifacts["qc_report"] = str(out)
    report.qc_state = qc.state.value

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
