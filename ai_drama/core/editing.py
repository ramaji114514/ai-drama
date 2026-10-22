"""在网页上直接改一镜的提示词。

以前只有一条路：手改 `episode.json`，再 `resume --from-json`。对"只想把
"镜头缓慢推近"改成"手持跟拍""的人来说，这是两步多余的操作，而且很容易
改错缩进把整个文件弄坏。

这里做两件事，而且**必须一起做**：

1. 改 `episode.json` —— 它是分镜这份数据的正文，人也要能直接读；
2. 把改完的这份回灌进库 —— `resume` 时 Pipeline 是从库里取 episode 的
   （`store.get_episode`），只改文件不改库，重跑会拿旧提示词再算一遍，
   你会以为按钮没生效。

可改的字段是**白名单**，分两层：

* 提示词层（`PROMPT_FIELDS`）：直接改最终发给模型的那几段；
* 分镜层（`STORY_FIELDS`）：改动作 / 台词 / 结束画面 / 景别。这四样是
  **骨架的原料** —— 改完必须用本地骨架重拼提示词、重算时长，否则页面上
  写着新台词、提示词里还是旧动作。重拼不联网、不花钱，所以做成一步。

出镜人（`characters`）仍然不开放：它牵连参考图名额和身份锁定，
在网页上点错一次会静默换掉整镜的脸。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths
from .models import Episode

#: 网页上允许就地编辑的字段。都是"越改越好"的纯文本，不牵连结构。
PROMPT_FIELDS = (
    "image_prompt",      # 首帧
    "last_frame",        # 尾帧
    "video_prompt",      # 运镜/运动
    "negative_prompt",   # 负向·图
    "video_negative",    # 负向·视频
    "end_action",        # 结束画面（尾帧的原料）
)

#: 分镜层可改的字段：骨架的原料。改完要重拼提示词、重算时长。
STORY_FIELDS = ("action", "dialogue", "end_action", "framing")

#: 单段提示词的长度上限。2 万字符已经远超任何厂商的框，再长就是误粘贴。
MAX_CHARS = 20000


class EditError(Exception):
    """改不动的原因，页面直接显示这句话。"""

    def __init__(self, message: str, *, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def _find(data: Dict[str, Any], label: str) -> Dict[str, Any]:
    """按 `sc01/shot_000` 找到那一镜。"""
    for scene in data.get("scenes") or []:
        sid = str(scene.get("scene_id") or "")
        for shot in scene.get("shots") or []:
            order = int(shot.get("order") or 0)
            if f"{sid}/shot_{order:03d}" == label:
                return shot
    raise EditError(f"没有这一镜：{label}", code=404)


def update_prompt(root: Path, run_id: str, label: str, field: str,
                  text: str, *, store: Any = None) -> Dict[str, Any]:
    """把一镜的某个提示词字段改掉，落文件 + 回灌库。返回改完的那一镜。"""
    if field not in PROMPT_FIELDS:
        raise EditError(
            f"这个字段不允许在网页上改：{field}。"
            f"能改的是：{'、'.join(PROMPT_FIELDS)}", code=400)
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_CHARS:
        raise EditError(f"太长了（{len(text)} 字，上限 {MAX_CHARS}）", code=400)

    path = paths.episode_json(root, run_id)
    if not path.exists():
        raise EditError(f"这条 run 还没有 episode.json", code=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise EditError(f"episode.json 不是合法 JSON，先修好它：{exc}", code=409)

    shot = _find(data, label)
    shot[field] = text

    # 先写盘再回灌：写盘失败时库里的还是旧值，两边不会各说一套。
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)

    if store is not None:
        ep = Episode.from_dict(data)
        store.save_episode(ep)
        store.sync_shots(run_id, ep)

    out = dict(shot)
    out["label"] = label
    return out


def _write_episode(path: Path, data: Dict[str, Any]) -> None:
    """先写临时文件再替换：写到一半断电时，旧文件仍然完整。"""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _load_episode(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise EditError("这条 run 还没有 episode.json", code=404)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise EditError(f"episode.json 不是合法 JSON，先修好它：{exc}", code=409)


def _shot_of(ep: Episode, label: str):
    for shot in ep.shots:
        if shot.label == label:
            return shot
    raise EditError(f"没有这一镜：{label}", code=404)


def update_story(root: Path, run_id: str, label: str, fields: Dict[str, str],
                 *, store: Any = None, max_refs: int = 1) -> Dict[str, Any]:
    """改分镜原文，然后用**本地骨架**重拼提示词、重算时长。

    为什么必须重拼：`image_prompt` 是 `style + 角色 + 景别 + action` 拼出来的
    成品，不是手写字段。只改 `action` 不重拼，页面上的动作变了、出图还是旧画面
    —— 这种"看起来生效了"的假成功比报错更难查。

    返回改完的这一镜（含 `lint` 体检结论），页面直接拿来重画。
    """
    from ..stages import stage02_storyboard as sb

    bad = [k for k in fields if k not in STORY_FIELDS]
    if bad:
        raise EditError(
            f"这些字段不在网页可改范围：{'、'.join(bad)}。"
            f"能改的是：{'、'.join(STORY_FIELDS)}", code=400)

    cleaned: Dict[str, str] = {}
    for key, value in fields.items():
        text = (value or "").replace("\r\n", "\n").strip()
        if len(text) > MAX_CHARS:
            raise EditError(f"{key} 太长了（{len(text)} 字，上限 {MAX_CHARS}）", code=400)
        if key == "framing" and text not in sb.FRAMING_PROMPT:
            raise EditError(
                f"没有这个景别：{text}。可选：{'、'.join(sb.FRAMING_PROMPT)}", code=400)
        if key == "action" and not text:
            raise EditError("动作不能为空 —— 空的动作会让这一镜变成空镜（真要空镜就写环境动作）", code=400)
        cleaned[key] = text
    if not cleaned:
        raise EditError("没有要改的字段", code=400)

    path = paths.episode_json(root, run_id)
    data = _load_episode(path)
    raw = _find(data, label)
    raw.update(cleaned)
    _write_episode(path, data)

    ep = Episode.from_dict(data)
    shot = _shot_of(ep, label)
    _compose(ep, shot, max_refs)
    data = ep.to_dict()
    _write_episode(path, data)

    if store is not None:
        store.save_episode(ep)
        store.sync_shots(run_id, ep)

    out = next(dict(sh) for sc in data["scenes"] for sh in sc["shots"]
               if f"{sc['scene_id']}/shot_{int(sh.get('order') or 0):03d}" == label)
    out["label"] = label
    from . import lint as lintlib
    out["lint"] = [f.as_dict() for f in lintlib.lint_shot(shot)]
    return out


def _compose(ep: Episode, shot: Any, max_refs: int) -> None:
    """用本地骨架把这一镜的五段提示词与时长按当前原料重算一遍。

    与 `stage02` 定稿时走的是同一批函数 —— 两处各写一份的结果是"网页上看着
    改了、pipeline 重跑又是另一套"，那种不一致最难查。
    """
    from ..stages import stage02_storyboard as sb
    from . import media

    labels = sb.reference_labels(ep, shot, max_refs)
    unlocked = sb.unlocked_names(ep, shot, max_refs)
    shot.image_prompt = sb.compose_image_prompt(ep, shot, labels, unlocked)
    shot.last_frame = sb.compose_last_frame(ep, shot, labels, unlocked)
    shot.video_prompt = sb.compose_video_prompt(shot)
    shot.negative_prompt = sb.compose_negative_prompt(ep, shot)
    shot.video_negative = sb.compose_video_negative(shot)
    # 台词长短是时长的硬下限（`estimate_shot_seconds` 里写着为什么），
    # 所以改台词必然改时长 —— 不重算的话，页面上的秒数会是撒谎。
    shot.duration = media.estimate_shot_seconds(
        shot, preferred_s=media.preferred_shot_seconds(ep))


def rerun_shots(root: Path, run_id: str, labels: list[str],
                *, store: Any, stages: Optional[list[str]] = None,
                force: bool = True, budget_usd: float = 0.0,
                concurrency: int = 1) -> Dict[str, Any]:
    """只重跑这几镜。同步执行 —— 调用方负责把它放到后台线程里。"""
    from ..providers import registry
    from .pipeline import Pipeline

    pipe = Pipeline(root, store, registry.build_bundle(),
                    budget_usd=budget_usd, concurrency=concurrency,
                    force=force, verbose=False, only_shots=tuple(labels))
    return pipe.resume(run_id, stages=stages or ["frames"])
