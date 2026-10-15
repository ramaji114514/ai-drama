"""画面校验：让模型**真的看一眼**已经做出来的东西。

和 `core/qc.py` 的分工是刻意的，不是历史遗留：

    core/qc.py          确定性检查（时长、对白长度、重复、钩子…）——
                        **不调模型**，纯函数，离线可测
    core/vision_qc.py   需要**看图**的检查（脸一不一致、画面里有没有字）——
                        每次调用都花钱，默认关闭

把模型塞进 `QcChecker` 会让那个模块失去它唯一的好处：不用任何 API key
就能测"输出乱码时算不算通过"。所以这一层单独放，产出的是 `qc.Finding`，
交给 `QcReport` 统一汇总 —— 三态语义（**没查成 ≠ 通过**）在那一层已经写好
并且被测试钉住了，这里不再发明一套。

# 三条铁律

1. **看不见不等于没问题。** provider 没配 / 调用失败 / 输出读不出来，
   一律产出 `unchecked=True` 的 Finding。严格模式下 unchecked 不算通过 ——
   这是整个检查层的意义所在，写成"通过"就等于没做检查。
2. **默认关闭。** 每查一镜就是一次调用。60 集 × 每集 20 镜 = 1200 次，
   而其中绝大多数镜头本来就不会有问题。默认打开的结果是所有人第一天
   就把整个质检关掉。
3. **只报不改。** 判定不触发自动重生成。一次误判就重渲一镜是真金白银，
   而"重渲之后还是不对"的下场是把钱花在同一个问题上 —— 改法是给人看，
   由人决定，见 docs/architecture.md。
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .identity import CHECK_PROMPT, PASS_SCORE, parse_identity_verdict
from .qc import Finding, Severity

#: 发给模型的图**先缩到这么宽**。参考项目的实测数字：512 宽的一帧约 145KB
#: base64，而这里要判断的是"是不是同一张脸 / 有没有字"这种粗粒度的事，
#: 原图 720x1280 只是白花 token。
FRAME_MAX_WIDTH = 512


# ————————————————————————————————————————----
# 图片 -> data URL
# ————————————————————————————————————————----


def image_data_url(path: Path, *, max_width: int = FRAME_MAX_WIDTH) -> str:
    """把本地图片变成 `data:image/jpeg;base64,...`。

    缩图失败（Pillow 缺失、图坏了）时**退回原图不缩放**，而不是抛错：
    少一次缩放只是多花点 token，而这个函数抛错会让整项检查变成 unchecked ——
    那是把"看不出问题"错报成"没查成"。
    """
    raw = Path(path).read_bytes()
    mime = "image/png"
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        if img.width > max_width:
            ratio = max_width / float(img.width)
            img = img.resize((max_width, max(1, int(img.height * ratio))))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        raw, mime = buf.getvalue(), "image/jpeg"
    except Exception:                                 # noqa: BLE001
        pass
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _call(vision: Any, messages: List[Dict[str, Any]], *, scene: str = "") -> Tuple[str, Dict[str, Any]]:
    return vision.chat(messages, scene=scene)


# ————————————————————————————————————————----
# 检查一：画面里有没有字
# ————————————————————————————————————————----

NO_TEXT_PROMPT = """判断这张短剧画面里**有没有可见的文字**。

文字包括：招牌、门牌、路牌、书本封面、手机屏幕、字幕、水印、
以及任何像字或字母的图形符号。

不算文字：纯色块、光影、模糊到无法辨认的纹理。

只输出 JSON，不要解释：
{"has_text": true, "found": ["门牌上写着 302"], "confidence": 0.9}

`has_text` 为 false 时 `found` 留空数组。看不清就如实说：
{"has_text": null, "found": [], "confidence": 0.0}
"""


def parse_no_text_verdict(text: str) -> Tuple[Optional[bool], List[str], str]:
    """解析"有没有字"的回答。返回 (has_text, found, note)。

    `has_text=None` 表示**读不出来**（不是"没有字"）。这一条是这块的全部要点：
    把读不出来当成没有字，等于这个检查白做。
    """
    raw = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    if not raw:
        return None, [], "视觉模型没有返回内容"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, [], "视觉模型的输出不是合法 JSON"
    if not isinstance(data, dict) or "has_text" not in data:
        return None, [], "视觉模型的输出里没有 has_text 字段"
    found = [str(x) for x in (data.get("found") or []) if str(x).strip()]
    value = data.get("has_text")
    if value is None:
        return None, found, "模型说它看不清这张图"
    return bool(value), found, ""


# ————————————————————————————————————————----
# 两个检查的共同外壳
# ————————————————————————————————————————----


def _pick(shots: Sequence[Any], max_checks: int) -> List[Any]:
    """挑要检查的镜头。

    `max_checks <= 0` 表示全查。超过上限时**等距抽**而不是取前 N 个：
    取前 N 个会把检查范围永远钉在开头几场，而后半部恰恰是最容易漂的地方。
    """
    items = list(shots)
    if max_checks <= 0 or len(items) <= max_checks:
        return items
    step = len(items) / float(max_checks)
    return [items[min(len(items) - 1, int(i * step))] for i in range(max_checks)]


def _keyframe_of(run_root: Path, run_id: str, shot: Any) -> Optional[Path]:
    from . import paths

    p = paths.keyframe(run_root, run_id, shot.scene_id, shot.order)
    return p if p.exists() and p.stat().st_size > 0 else None


def check_no_text(vision: Any, run_root: Path, run_id: str, shots: Sequence[Any], *,
                  max_checks: int = 0,
                  severity: Severity = Severity.BLOCK
                  ) -> Tuple[List[Finding], float, Dict[str, Any]]:
    """逐镜问一句"画面里有没有字"。

    为什么值得单独一项：**生成的画面里不许出现字，字全部后期烧**（见
    stage07_concat 的字幕）。模型很爱在招牌、门牌、屏幕上"顺手写点字"，
    而它写出来的中文基本都是错的 —— 一部剧里出现三个错别字招牌，
    观众立刻知道这是 AI 生成的。这件事在人眼抽查里几乎发现不了。
    """
    findings: List[Finding] = []
    money = 0.0
    checked = failed = unchecked = 0
    examples: List[str] = []

    for shot in _pick(shots, max_checks):
        img = _keyframe_of(run_root, run_id, shot)
        if img is None:
            unchecked += 1
            findings.append(Finding(
                rule="vision-no-text", severity=severity, where=shot.label,
                message="没有关键帧可查", unchecked=True,
                suggestion="先跑 frames 阶段，或者把这一项关掉",
            ))
            continue
        try:
            text, usage = _call(vision, [{"role": "user", "content": [
                {"type": "text", "text": NO_TEXT_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": image_data_url(img), "detail": "low"}},
            ]}], scene=shot.label)
            money += float((usage or {}).get("money") or 0.0)
        except Exception as exc:                      # noqa: BLE001
            unchecked += 1
            findings.append(Finding(
                rule="vision-no-text", severity=severity, where=shot.label,
                message=f"这一镜没查成：{type(exc).__name__}: {exc}", unchecked=True,
            ))
            continue

        has_text, found, note = parse_no_text_verdict(text)
        if has_text is None:
            unchecked += 1
            findings.append(Finding(
                rule="vision-no-text", severity=severity, where=shot.label,
                message=f"这一镜没查成：{note}", unchecked=True,
                detail=(text or "")[:200],
            ))
            continue
        checked += 1
        if has_text:
            failed += 1
            detail = "；".join(found) if found else "（模型没说是哪几个字）"
            examples.append(f"{shot.label}：{detail}")
            findings.append(Finding(
                rule="vision-no-text", severity=severity, where=shot.label,
                message="画面里出现了文字",
                detail=detail,
                suggestion="把这一镜的提示词里「不要出现文字/招牌/门牌」写死，"
                           "或者重渲这一镜；台词一律由字幕层烧",
            ))

    if failed:
        pass          # 逐镜的 block 已经足够说明问题
    elif unchecked and not checked:
        pass          # 一条都没查成：整体是"没查成"，下面不再补通过结论
    else:
        findings.append(Finding(
            rule="vision-no-text", severity=Severity.INFO, where="全片",
            message=f"{checked} 镜查过，画面里都没有文字"
                    + (f"（{unchecked} 镜没查成）" if unchecked else ""),
        ))
    return findings, money, {
        "checked": checked, "failed": failed, "unchecked": unchecked,
        "examples": examples,
    }


def check_identity(vision: Any, ep: Any, run_root: Path, run_id: str, *,
                   max_checks: int = 0,
                   pass_score: int = PASS_SCORE,
                   severity: Severity = Severity.BLOCK
                   ) -> Tuple[List[Finding], float, Dict[str, Any]]:
    """把关键帧和角色的定妆照一起发给视觉模型，问"是不是同一个人"。

    判定逻辑**一行都没有重写**：提示词用 `core/identity.CHECK_PROMPT`，
    解析用 `parse_identity_verdict`（含逐角色最低分门槛、`unchecked` 不变式）。
    这一层只负责取图、发出去、把结论翻译成 Finding。

    参考图用 `detail: high`（要看清五官），关键帧用 `detail: low`
    （判的是粗粒度的是不是同一张脸）—— 这个取舍写在 `identity.check_messages`
    的 docstring 里，和参考项目的实测数字一起。
    """
    from .identity import check_messages
    from ..stages.stage02_storyboard import characters_in_shot

    findings: List[Finding] = []
    money = 0.0
    checked = failed = unchecked = 0
    scores: List[int] = []
    bad: List[str] = []

    candidates = []
    for shot in ep.shots:
        img = _keyframe_of(run_root, run_id, shot)
        if img is None:
            continue
        cast = [c for c in characters_in_shot(ep, shot)
                if c.ref_image and Path(c.ref_image).exists()]
        if cast:
            candidates.append((shot, img, cast[:2]))

    for shot, img, cast in _pick(candidates, max_checks):
        refs = [(("角色", c.name, image_data_url(Path(c.ref_image)))) for c in cast]
        try:
            messages = check_messages(refs, [image_data_url(img, max_width=640)])
            text, usage = _call(vision, messages, scene=shot.label)
            money += float((usage or {}).get("money") or 0.0)
        except Exception as exc:                      # noqa: BLE001
            unchecked += 1
            findings.append(Finding(
                rule="vision-identity", severity=severity, where=shot.label,
                message=f"这一镜没查成：{type(exc).__name__}: {exc}", unchecked=True,
            ))
            continue

        verdict = parse_identity_verdict(text, pass_score=pass_score)
        if verdict.unchecked:
            unchecked += 1
            findings.append(Finding(
                rule="vision-identity", severity=severity, where=shot.label,
                message=f"这一镜没查成：{verdict.note}", unchecked=True,
                detail=(text or "")[:200],
            ))
            continue
        checked += 1
        if verdict.score >= 0:
            scores.append(verdict.score)
        if not verdict.passed:
            failed += 1
            detail = "；".join(verdict.issues) or f"分数 {verdict.score} 低于 {pass_score}"
            bad.append(f"{shot.label}（{verdict.score} 分）：{detail}")
            findings.append(Finding(
                rule="vision-identity", severity=severity, where=shot.label,
                message=f"画面里的人不像定妆照（{verdict.score} 分）",
                detail=detail,
                suggestion="看这一镜的参考锁定段有没有点名参考图；"
                           "必要时重跑 frames/video。若是模型误判，"
                           "把 pass_score 调低或关掉 require_identity",
            ))

    if failed:
        pass
    elif unchecked and not checked:
        pass
    elif checked:
        avg = round(sum(scores) / len(scores), 1) if scores else -1
        findings.append(Finding(
            rule="vision-identity", severity=Severity.INFO, where="全片",
            message=f"{checked} 镜查过，人物与定妆照一致（平均 {avg} 分）"
                    + (f"（{unchecked} 镜没查成）" if unchecked else ""),
        ))
    return findings, money, {
        "checked": checked, "failed": failed, "unchecked": unchecked,
        "examples": bad, "scores": scores,
    }


__all__ = [
    "check_identity", "check_no_text", "image_data_url",
    "parse_no_text_verdict", "NO_TEXT_PROMPT", "FRAME_MAX_WIDTH",
]
