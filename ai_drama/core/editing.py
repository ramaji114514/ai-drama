"""在网页上直接改一镜的提示词。

以前只有一条路：手改 `episode.json`，再 `resume --from-json`。对"只想把
"镜头缓慢推近"改成"手持跟拍""的人来说，这是两步多余的操作，而且很容易
改错缩进把整个文件弄坏。

这里做两件事，而且**必须一起做**：

1. 改 `episode.json` —— 它是分镜这份数据的正文，人也要能直接读；
2. 把改完的这份回灌进库 —— `resume` 时 Pipeline 是从库里取 episode 的
   （`store.get_episode`），只改文件不改库，重跑会拿旧提示词再算一遍，
   你会以为按钮没生效。

可改的字段是**白名单**，只放提示词相关的几项：
动作、台词、出镜人这些属于分镜结构，改了要牵连景别/参考图/时长，
不是"改个措辞"能在网页上安全完成的 —— 那些仍然走 `--from-json`。
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
