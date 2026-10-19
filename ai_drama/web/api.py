"""Web 界面：把磁盘上的东西读成 JSON。

这一层是**纯函数**：给它 store、root、run_id，它返回能直接 `json.dumps` 的
字典。socket 那一层（`app.py`）只管路由和字节。分开的理由和 provider 层一样 ——
"这一条 run 该长什么样"可以离线测，不必起一个真的端口。

两条和别处一致的规矩：

1. **看不见不等于没问题。** 每个文件字段都带 `exists` / `size`，于是界面能显示
   "记录还在，文件没了"，而不是一个裂图。质检的 `unchecked` 同理：它**不是**
   通过，界面里两者绝不能用同一个绿色。
2. **报告说什么就是什么。** 这里不重新计算任何判定：`qc_state` 直接来自
   `qc_report.json`，钱直接来自 costs 表，时间轴直接来自 `timeline.json`。
   界面自己再算一遍就有了第二个真相 —— 而"两个地方各说一套"正是这个项目
   一路在修的病。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from .. import __version__
from ..core import assets as assetslib
from ..core import paths
from ..core import project as projectlib

#: `/media` 允许读出去的目录前缀（相对工作目录）。
#: 刻意是白名单而不是黑名单：这个进程能读到的东西比它该暴露的多得多
#: （`.env`、API key、别的仓库）。
MEDIA_PREFIXES: Tuple[str, ...] = ("runs/", "projects/")

#: 名字里带这些的**一律不给**：数据库本身就是整个项目的账本，
#: 把它挂到浏览器上等于把"只读查看器"变成"下载所有人的运行记录"。
_DENY_SUFFIX = (".sqlite3", ".db", ".sqlite", ".sqlite3-wal", ".sqlite3-shm")


# ————————————————————————————————————————----
# 小工具
# ————————————————————————————————————————----


def file_ref(root: Path, path: Any, *, url: bool = True) -> Optional[Dict[str, Any]]:
    """把一个路径变成"界面上能不能点、点了去哪"。

    `path` 为 None 时返回 None（**不是**返回一个空对象）—— 界面据此区分
    "这一镜还没生成片段"和"生成了但文件丢了"，这两件事的处理方式不一样。
    """
    if not path:
        return None
    p = Path(str(path))
    rel = assetslib.rel_of(root, p)
    try:
        exists = p.is_file()
        size = p.stat().st_size if exists else 0
    except OSError:
        exists, size = False, 0
    out: Dict[str, Any] = {"rel": rel, "name": p.name, "exists": exists, "size": size}
    if url:
        out["url"] = "/media?path=" + quote(rel, safe="")
    return out


def safe_media_path(root: Path, rel: str, *, deny: Sequence[Path] = ()) -> Optional[Path]:
    """把 URL 上的 `path` 变成一个**确实在工作目录里**的文件路径。

    返回 None 就是拒绝，而调用方一律回 404 —— 把"不存在"和"不许看"分开
    等于告诉外面哪些路径是存在的。

    这里挡的是：绝对路径与盘符、任何 `..`、白名单之外的目录、点开头的名字、
    `_work/`（中间产物）、数据库文件，以及符号链接指到工作目录外面的情况
    （`resolve()` 之后再比一次，才是真的挡住了）。
    """
    if not rel:
        return None
    text = str(rel).replace("\\", "/").lstrip("/")
    if not text or ":" in text.split("/")[0]:
        return None
    pure = Path(text)
    if pure.is_absolute() or not pure.parts:
        return None
    if any(part in ("..", ".") or not part for part in pure.parts):
        return None
    if "_work" in pure.parts:
        return None
    if pure.name.startswith("."):
        return None
    if pure.name.endswith(_DENY_SUFFIX) or ".sqlite3-" in pure.name:
        return None
    if not any(text.startswith(pref) for pref in MEDIA_PREFIXES):
        return None

    root_r = Path(root).resolve()
    target = (root_r / pure).resolve()
    if target != root_r and root_r not in target.parents:
        return None
    for denied in deny:
        try:
            if target == Path(denied).resolve():
                return None
        except OSError:
            continue
    return target if target.is_file() else None


def _read_json(path: Path) -> Optional[Any]:
    """读一个 json 文件，读不出来返回 None **不抛**。

    界面是只读的：一个坏掉的 cost_report.json 不该让整页打不开 ——
    它该在那一栏里显示"这个东西读不出来"。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path, *, limit: int = 200000) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


# ————————————————————————————————————————----
# 总体
# ————————————————————————————————————————----


def meta_payload(root: Path, db: Path) -> Dict[str, Any]:
    """页面抬头：版本、工作目录、数据库，以及八个形态各自会用哪个 provider。

    形态那一栏值得放在最上面：这个项目默认关掉了两个形态（vision / sfx），
    而"报告里少了一整块校验"这件事在 CLI 里要专门去 `doctor` 才看得到。
    """
    from ..providers import registry

    kinds: List[Dict[str, Any]] = []
    for kind in ("text", "image", "video", "audio", "t2i", "lipsync", "vision", "sfx"):
        name = registry.effective_name(kind)
        kinds.append({
            "kind": kind,
            "env": registry.KIND_ENV.get(kind, ""),
            "provider": name,
            "off": not name,
            "optional": kind in registry.OPTIONAL_KINDS,
        })
    return {
        "app": "ai-drama",
        "version": __version__,
        "root": str(Path(root).resolve()),
        "db": str(Path(db).resolve()),
        "kinds": kinds,
    }


def runs_payload(root: Path, store: Any, *, limit: int = 50) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for run in store.list_runs(limit=limit):
        meta = run.meta or {}
        try:
            money = float(store.cost_summary(run.run_id).get("total_money") or 0.0)
        except Exception:                       # noqa: BLE001
            money = 0.0
        ep = store.get_episode(run.run_id)
        out.append({
            "run_id": run.run_id,
            "state": run.state,
            "stage": run.stage,
            "idea": run.idea,
            "title": (ep.title if ep is not None else "") or "",
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "error": run.error,
            "project_id": meta.get("project_id", ""),
            "episode_no": meta.get("episode_no", None),
            "aspect": meta.get("aspect", ""),
            "money": round(money, 6),
            "final_video": file_ref(root, run.final_video) if run.final_video else None,
        })
    return out


def _shot_payload(root: Path, run_id: str, scene_id: str, shot: Any,
                  live: Dict[str, Any]) -> Dict[str, Any]:
    order = int(getattr(shot, "order", 0) or 0)

    def conv(fn, *extra) -> Optional[Dict[str, Any]]:
        return file_ref(root, fn(root, run_id, scene_id, order, *extra))

    sfx = []
    for i, cue in enumerate(getattr(shot, "sfx", []) or []):
        sfx.append({"cue": cue, "file": conv(paths.sfx, i)})

    return {
        "shot_id": shot.shot_id,
        "label": shot.label,
        "order": order,
        "scene_id": scene_id,
        "framing": getattr(shot, "framing", ""),
        "action": getattr(shot, "action", ""),
        "dialogue": getattr(shot, "dialogue", ""),
        # 提示词也带出去：用户要拿去可灵/即梦这类网页端用，所以在页面上要能看、
        # 能改、能一键复制。image_prompt 是首帧，last_frame 是尾帧，video_prompt
        # 是运镜+动作。end_action 单独带出去，因为它是模型的原始意图，
        # last_frame 是骨架拼出来的结果 —— 两者都摆出来，用户才看得出哪一步变了。
        "image_prompt": getattr(shot, "image_prompt", ""),
        "last_frame": getattr(shot, "last_frame", ""),
        "video_prompt": getattr(shot, "video_prompt", ""),
        "end_action": getattr(shot, "end_action", ""),
        "duration": float(getattr(shot, "duration", 0.0) or 0.0),
        "characters": list(getattr(shot, "characters", None) or []),
        "declared": getattr(shot, "characters", None) is not None,
        "sfx": sfx,
        "state": live.get("state") or getattr(shot, "state", ""),
        "error": live.get("error") or getattr(shot, "error", None),
        "keyframe": conv(paths.keyframe),
        "clip": conv(paths.clip),
        "synced": conv(paths.synced),
        "audio": conv(paths.voice),
    }


def storyboard_payload(root: Path, store: Any, run_id: str, ep: Any) -> Dict[str, Any]:
    live = {s["shot_id"]: s for s in store.list_shots(run_id)}
    scenes: List[Dict[str, Any]] = []
    for scene in ep.scenes:
        scenes.append({
            "scene_id": scene.scene_id,
            "title": scene.title,
            "location": scene.location,
            "time_of_day": scene.time_of_day,
            "summary": scene.summary,
            "shots": [
                _shot_payload(root, run_id, scene.scene_id, sh, live.get(sh.shot_id, {}))
                for sh in scene.shots
            ],
        })
    return {
        "title": ep.title,
        "logline": ep.logline,
        "style": ep.style,
        "idea": ep.idea,
        "created_at": ep.created_at,
        "characters": [
            {
                "char_id": c.char_id,
                "name": c.name,
                "aliases": list(c.aliases or []),
                "desc": c.desc,
                "anchor_prompt": c.anchor_prompt,
                "portrait": file_ref(root, c.ref_image) if c.ref_image else None,
            }
            for c in ep.characters
        ],
        "scenes": scenes,
        "shots_total": sum(len(s["shots"]) for s in scenes),
        "seconds": round(float(ep.total_duration or 0.0), 3),
    }


def run_payload(root: Path, store: Any, run_id: str) -> Optional[Dict[str, Any]]:
    run = store.get_run(run_id)
    if run is None:
        return None
    ep = store.get_episode(run_id)
    meta = run.meta or {}

    qc = _read_json(paths.qc_report(root, run_id))
    timeline = _read_json(paths.timeline(root, run_id))
    cost = None
    try:
        cost = store.cost_summary(run_id)
    except Exception:                           # noqa: BLE001
        cost = None

    return {
        "run": {
            "run_id": run.run_id,
            "state": run.state,
            "stage": run.stage,
            "idea": run.idea,
            "error": run.error,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "project_id": meta.get("project_id", ""),
            "episode_no": meta.get("episode_no", None),
            "aspect": meta.get("aspect", ""),
        },
        "storyboard": storyboard_payload(root, store, run_id, ep) if ep is not None else None,
        "cost": cost,
        "qc": qc,
        "timeline": timeline,
        "subtitles": {
            "srt": _read_text(paths.subtitles(root, run_id)),
            "file": file_ref(root, paths.subtitles(root, run_id)),
        },
        "assets": assets_summary(root, store, run_id),
        "files": {
            "final_video": file_ref(root, paths.episode_video(root, run_id)),
            "episode_json": file_ref(root, paths.episode_json(root, run_id)),
            "episode_json_data": _read_json(paths.episode_json(root, run_id)),
            "timeline": file_ref(root, paths.timeline(root, run_id)),
            "qc_report": file_ref(root, paths.qc_report(root, run_id)),
            "cost_report": file_ref(root, paths.cost_report(root, run_id)),
            "bgm": file_ref(root, paths.bgm(root, run_id)),
        },
    }


# ————————————————————————————————————————----
# 资产与血缘
# ————————————————————————————————————————----


def _asset_payload(a: Any, root: Path) -> Dict[str, Any]:
    ref = file_ref(root, assetslib.resolve(root, a.rel))
    return {
        "asset_id": a.asset_id,
        "run_id": a.run_id,
        "kind": a.kind,
        "rel": a.rel,
        "stage": a.stage,
        "size": a.size,
        "sha256": (a.sha256 or "")[:16],
        "provider": a.provider,
        "model": a.model,
        "shot_id": a.shot_id,
        "prompt_hash": a.prompt_hash,
        "prompt": a.prompt,
        "meta": a.meta,
        "file": ref,
    }


def assets_summary(root: Path, store: Any, run_id: str) -> Dict[str, Any]:
    summary = store.asset_summary(run_id)
    return {
        "total": summary.get("total", 0),
        "bytes": summary.get("bytes", 0),
        "edges": summary.get("edges", 0),
        "by_kind": summary.get("by_kind", {}),
    }


def assets_payload(root: Path, store: Any, run_id: str) -> Dict[str, Any]:
    rows = store.list_assets(run_id)
    edges = [e for e in store.asset_edges() if getattr(e, "parent_id", "") in
             {r.asset_id for r in rows}]
    return {
        "summary": assets_summary(root, store, run_id),
        "assets": [_asset_payload(a, root) for a in rows],
        "edges": [
            {"parent": e.parent_id, "child": e.child_id, "rel": e.rel} for e in edges
        ],
    }


def asset_payload(root: Path, store: Any, asset_id: str) -> Optional[Dict[str, Any]]:
    a = store.get_asset(asset_id)
    if a is None:
        return None
    edges = store.asset_edges()
    rows = {r.asset_id: r for r in store.list_assets(a.run_id)}
    tree = [
        {
            "depth": n.depth,
            "rel": n.rel,
            "via": n.via,
            "repeat": n.repeat,
            "asset_id": n.asset.asset_id,
            "kind": n.asset.kind,
            "label": Path(n.asset.rel).name,
        }
        for n in assetslib.lineage(rows, edges, asset_id)
    ]
    return {
        "asset": _asset_payload(a, root),
        "upstream": [
            {"rel": rel, "asset": _asset_payload(row, root)}
            for pid, rel in assetslib.parents_of(edges, asset_id)
            if (row := rows.get(pid)) is not None
        ],
        "downstream": [
            {"rel": rel, "asset": _asset_payload(row, root)}
            for pid, rel in assetslib.children_of(edges, asset_id)
            if (row := rows.get(pid)) is not None
        ],
        "sharing": [
            {"asset_id": s.asset_id, "kind": s.kind, "rel": s.rel}
            for s in store.assets_sharing_content(a.sha256, exclude=asset_id)
        ],
        "tree": tree,
    }


# ————————————————————————————————————————----
# 项目（角色圣经）
# ————————————————————————————————————————----


def projects_payload(root: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for proj, err in projectlib.list_projects_lenient(root):
        if proj is None:
            out.append({"_error": err, "project_id": "", "title": ""})
            continue
        out.append({
            "project_id": proj.project_id,
            "title": proj.title,
            "logline": proj.logline,
            "style": proj.style,
            "characters": len(proj.characters),
            "updated_at": proj.updated_at,
            "_error": err,
        })
    return out


def project_payload(root: Path, project_id: str) -> Optional[Dict[str, Any]]:
    proj = projectlib.load(root, project_id)
    if proj is None:
        return None
    return {
        "project_id": proj.project_id,
        "title": proj.title,
        "logline": proj.logline,
        "style": proj.style,
        "created_at": proj.created_at,
        "updated_at": proj.updated_at,
        "characters": [
            {
                "char_id": c.char_id,
                "name": c.name,
                "aliases": list(c.aliases or []),
                "desc": c.desc,
                "anchor_prompt": c.anchor_prompt,
                "voice": proj.voice_for(c, ""),
                "portrait": file_ref(root, c.ref_image) if c.ref_image else None,
            }
            for c in proj.characters
        ],
    }


__all__ = [
    "MEDIA_PREFIXES",
    "asset_payload",
    "assets_payload",
    "assets_summary",
    "file_ref",
    "meta_payload",
    "project_payload",
    "projects_payload",
    "run_payload",
    "runs_payload",
    "safe_media_path",
    "storyboard_payload",
]
