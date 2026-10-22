"""把一集导出成机器能吃的提示词包（给 agent / 别的程序用）。

网页上的「导出全部提示词」是给**人**看的一整篇文本（带小标题、带"拿去做
什么"的说明）。这个模块产出的是**同一个东西的结构化版本**：键名稳定、
不含排版、可以直接 `json.load` 进另一个程序。

两边的取舍不一样，所以不合并：
  * 人的那份要能读，字段顺序、标题、空行都算内容；
  * 程序的那份只要键和值，多一个空行都是噪音。

刻意不带的钱：提示词包里没有 cost 字段。用这个包的人（agent、创作者）
关心的是"要粘什么"，钱在 `run.py cost` 里看 —— 混进来只会让人以为
生成这套提示词本身要按条计费。
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import dialects
from .models import Episode, Shot

VERSION = 1


def _shot(shot: Shot, dialect: str = "") -> Dict[str, Any]:
    data = {
        "label": shot.label,
        "scene_id": shot.scene_id,
        "order": shot.order,
        "duration": round(float(shot.duration or 0.0), 2),
        "framing": shot.framing,
        "characters": list(shot.characters or []),
        "dialogue": shot.dialogue or "",
        "action": shot.action or "",
        "end_action": shot.end_action or "",
        # 四段提示词是并列的产物，不是上下游
        "image_prompt": shot.image_prompt or "",
        "last_frame": shot.last_frame or "",
        "video_prompt": shot.video_prompt or "",
        "negative_prompt": shot.negative_prompt or "",
        "video_negative": shot.video_negative or "",
    }
    if dialect:
        # 选了平台就多给一份"按这家的框整理过"的版本。**原样五段照样留着**：
        # 截断是有损的，用户想改还得有原文可改。
        rendered = dialects.render(None, shot, dialect)
        data["dialect"] = rendered.key
        data["dialect_tip"] = rendered.tip
        data["fields"] = [f.as_dict() for f in rendered.fields]
        data["dropped"] = list(rendered.dropped)
    return data


def _character(char: Any, dialect: str = "") -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "char_id": getattr(char, "char_id", ""),
        "name": getattr(char, "name", ""),
        "desc": getattr(char, "desc", ""),
        # 定妆照提示词：拿去图模型里做「角色参考图」，再喂回每一镜的首帧
        "anchor_prompt": getattr(char, "anchor_prompt", "") or "",
        "ref_image": getattr(char, "ref_image", "") or "",
    }
    if dialect:
        # 定妆照是唯一会长到两百字以上的一段（五条规格一条都不能删），
        # 即梦/海螺/Seedance 的图框都是 2000 字 —— 按平台截，并明说截了。
        rendered = dialects.render_anchor(char, dialect)
        data["dialect"] = rendered.key
        full = next((f for f in rendered.fields if f.key == "anchor"), None)
        compact = next((f for f in rendered.fields if f.key == "anchor_compact"),
                       None)
        data["anchor_text"] = full.text if full else ""
        data["truncated"] = bool(full and full.truncated)
        # 紧凑版只在平台需要时出现（通用不给）
        if compact:
            data["anchor_compact"] = compact.text
            data["anchor_compact_hint"] = compact.hint
        data["dropped"] = list(rendered.dropped)
    return data


def bundle(ep: Episode, *, aspect: str = "", dialect: str = "") -> Dict[str, Any]:
    """整集的提示词包：角色在前，场次/镜头在后。

    角色单独提到顶层，是因为**它们不挂在某一镜上** —— 定妆照要先做，
    每一镜的首帧都要引用同一张脸。放进 shot 里就得复制 N 份，
    而复制的那 N 份迟早会不一致。
    """
    scenes: List[Dict[str, Any]] = []
    for scene in ep.scenes:
        scenes.append({
            "scene_id": scene.scene_id,
            "title": scene.title or "",
            "location": scene.location or "",
            "time_of_day": scene.time_of_day or "",
            "shots": [_shot(s, dialect) for s in scene.shots],
        })
    return {
        "version": VERSION,
        "run_id": ep.run_id,
        "title": ep.title or "",
        "logline": ep.logline or "",
        "style": getattr(ep, "style", "") or "",
        # 画幅记在 Run.meta 里，Episode 上没有这个字段 —— 由调用方传进来，
        # 缺了就是空串（宁可空着，也不要在这猜一个 9:16）
        "aspect": aspect,
        "dialect": dialects.get(dialect).key if dialect else "",
        "characters": [_character(c, dialect) for c in ep.characters],
        "scenes": scenes,
    }
