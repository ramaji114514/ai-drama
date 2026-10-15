"""资产台账 —— 每个产物是谁生成的、又是由什么变来的。

一次 run 会在 `runs/<id>/` 下散落上百个文件（定妆照、关键帧、片段、分段、
配音、音效、BGM、字幕、成片……），而 `StageReport.artifacts` 只是一份**当次
内存里的**路径字典：换一个进程问"这张关键帧是哪条提示词、拿哪张定妆照生成的"，
答案是"没人记得"。这个模块把那份记忆落成台账。

它做三件事：

1. 给每个产物一个**幂等**的 id —— 同一次 run 里同一个路径永远得到同一个 id。
   阶段是会重跑的；重跑一次就多一条记录的话，台账第二天就没人信了。这和
   `core/cast.py` 里"id 由名字推导、不抽随机数"是同一条理由。
2. 记下它的来路：阶段、provider、模型、提示词（及其哈希）、大小、sha256。
3. 记下**血缘**：定妆照 → 关键帧 → 片段 → 同步片段 → 成片。

边界（刻意的）：

* **不搬、不改、不删文件**。台账只描述磁盘上已经发生的事。产物被谁删掉了，
  是 `reconcile()` 的活儿 —— 它负责把"记录里有、磁盘上没有"大声报出来。
* **不假装记得**。没有 provider/提示词的产物（比如 ffmpeg 拼出来的成片），
  那些字段就是空的；空着比填一个猜测值强。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: 产物的种类。加新种类是安全的（老台账不认识的行仍能读出来），
#: 但**改已有种类的名字**会让历史记录变成另一种东西 —— 别改。
KINDS: Tuple[str, ...] = (
    "portrait",
    "keyframe",
    "clip",
    "voice",
    "sfx",
    "bgm",
    "synced",
    "subtitle",
    "timeline",
    "scene",
    "final",
    "report",
    "cost",
    "episode",
    "other",
)

KIND_DESC: Dict[str, str] = {
    "portrait": "定妆照：角色的脸，跨集复用",
    "keyframe": "关键帧：这一镜的起始画面",
    "clip": "片段：单镜的视频",
    "voice": "台词配音",
    "sfx": "音效",
    "bgm": "背景音乐（全片一条）",
    "synced": "口型同步后的片段",
    "subtitle": "字幕文件（srt / ass）",
    "timeline": "时间轴：字幕/配音/音效共用的那一份",
    "scene": "场次拼接：这一场所有镜头拼起来的中间产物",
    "final": "成片",
    "report": "报告类产物（质检报告等）",
    "cost": "成本明细",
    "episode": "剧本 JSON",
    "other": "没归类的产物",
}

#: 血缘边的三种含义。parent -> child。
EDGE_RELS: Tuple[str, ...] = ("ref", "source", "derive")

EDGE_DESC: Dict[str, str] = {
    "ref": "作为参考图被用上（定妆照 → 关键帧）",
    "source": "作为素材被合成进去（关键帧 → 片段，片段 → 成片）",
    "derive": "同一件东西的下一步形态（分段 → 片段，srt → ass）",
}

ID_PREFIX = "as_"

#: 提示词存进台账时的截断长度。哈希永远按**完整**文本算 ——
#: 截断只影响人读，不影响"两条记录是不是同一条提示词"。
PROMPT_MAX = 4000


def asset_id(run_id: str, rel: str) -> str:
    """由 (run_id, 相对路径) 推导出的稳定 id。

    幂等是刻意的：`resume` 重跑一个阶段时，同一个文件必须落到同一条记录上，
    否则台账会被同一件东西的多条记录撑爆，而"这件东西有几个版本"就再也答不出来。
    """
    digest = hashlib.sha1(f"{run_id}\n{rel}".encode("utf-8")).hexdigest()
    return f"{ID_PREFIX}{digest[:12]}"


def prompt_hash(text: str) -> str:
    """提示词的指纹。同一段提示词生成的两个产物，哈希相同。"""
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def rel_of(root: Path, path: Path) -> str:
    """把产物路径存成**相对 workspace** 的形式。

    相对路径让台账跟着整个工作目录一起搬走还能对上；路径在工作目录之外时
    老实存绝对路径 —— 硬算一个 `../..` 的相对路径只会更难读。
    """
    root = Path(root).resolve()
    p = Path(path)
    try:
        resolved = p.resolve()
    except OSError:            # 路径过长/盘符失效等：宁可存原样，也别抛
        return str(p)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def resolve(root: Path, rel: str) -> Path:
    """`rel_of()` 的逆运算。"""
    p = Path(rel)
    return p if p.is_absolute() else Path(root) / p


def digest(path: Path) -> Tuple[int, str]:
    """(字节数, sha256)。文件不在时返回 (0, "")，不抛 —— 对账要用这个结果。"""
    p = Path(path)
    try:
        size = p.stat().st_size
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return size, h.hexdigest()
    except OSError:
        return 0, ""


@dataclass
class Asset:
    """台账里的一行。"""

    asset_id: str
    run_id: str
    kind: str
    rel: str
    stage: str = ""
    size: int = 0
    sha256: str = ""
    provider: str = ""
    model: str = ""
    shot_id: str = ""
    prompt_hash: str = ""
    prompt: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_tracked_file(self) -> bool:
        return bool(self.rel)

    def to_row(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "kind": self.kind,
            "rel": self.rel,
            "size": int(self.size or 0),
            "sha256": self.sha256 or "",
            "provider": self.provider or "",
            "model": self.model or "",
            "shot_id": self.shot_id or "",
            "prompt_hash": self.prompt_hash or "",
            "prompt": self.prompt or "",
            "meta": json.dumps(self.meta or {}, ensure_ascii=False),
        }

    @classmethod
    def from_row(cls, row: Any) -> "Asset":
        get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k])
        raw_meta = get("meta") or "{}"
        try:
            meta = json.loads(raw_meta)
        except (TypeError, ValueError):
            meta = {}
        return cls(
            asset_id=get("asset_id") or "",
            run_id=get("run_id") or "",
            kind=get("kind") or "other",
            rel=get("rel") or "",
            stage=get("stage") or "",
            size=int(get("size") or 0),
            sha256=get("sha256") or "",
            provider=get("provider") or "",
            model=get("model") or "",
            shot_id=get("shot_id") or "",
            prompt_hash=get("prompt_hash") or "",
            prompt=get("prompt") or "",
            meta=meta if isinstance(meta, dict) else {},
        )

    def describe(self) -> str:
        bits = [f"{self.kind:<9}", self.rel or "(无路径)"]
        if self.provider:
            bits.append(f"provider={self.provider}")
        if self.model:
            bits.append(f"model={self.model}")
        if self.size:
            bits.append(f"{self.size}B")
        return "  ".join(bits)


def make_asset(
    run_id: str,
    root: Path,
    path: Path,
    *,
    kind: str = "other",
    stage: str = "",
    provider: str = "",
    model: str = "",
    shot_id: str = "",
    prompt: str = "",
    meta: Optional[Dict[str, Any]] = None,
    with_digest: bool = True,
) -> Asset:
    """拼出一行台账。**不写库**（写库是 Store 的事），只负责把事实收集齐。"""
    rel = rel_of(root, path)
    size, sha = digest(path) if with_digest else (0, "")
    text = prompt or ""
    return Asset(
        asset_id=asset_id(run_id, rel),
        run_id=run_id,
        kind=kind if kind in KINDS else "other",
        rel=rel,
        stage=stage,
        size=size,
        sha256=sha,
        provider=provider or "",
        model=model or "",
        shot_id=shot_id or "",
        prompt_hash=prompt_hash(text),
        prompt=text[:PROMPT_MAX],
        meta=dict(meta or {}),
    )


# ———————————————————————————————————————————————— 血缘


@dataclass
class LineageNode:
    """血缘树上的一个节点。"""

    depth: int
    rel: str                 # 走到这个节点的那条边（根部为空）
    asset: Asset
    via: str = ""            # 边的含义说明
    repeat: bool = False     # 这个节点在上面已经展开过，这里只标一下


def parents_of(edges: Iterable[Any], asset_id_: str) -> List[Tuple[str, str]]:
    """(parent_id, rel) —— 谁变成了它。"""
    out: List[Tuple[str, str]] = []
    for e in edges:
        get = e.get if hasattr(e, "get") else (lambda k, d=None: e[k])
        if (get("child_id") or "") == asset_id_:
            out.append((get("parent_id") or "", get("rel") or "derive"))
    return out


def children_of(edges: Iterable[Any], asset_id_: str) -> List[Tuple[str, str]]:
    """(child_id, rel) —— 它变成了谁。"""
    out: List[Tuple[str, str]] = []
    for e in edges:
        get = e.get if hasattr(e, "get") else (lambda k, d=None: e[k])
        if (get("parent_id") or "") == asset_id_:
            out.append((get("child_id") or "", get("rel") or "derive"))
    return out


def lineage(
    assets: Dict[str, Asset],
    edges: Sequence[Any],
    asset_id_: str,
    *,
    max_depth: int = 12,
) -> List[LineageNode]:
    """沿"谁变成了它"往上走，返回一棵**以它自己为根**的树（先序）。

    成片那条链最长：定妆照 → 关键帧 → 片段 → 同步片段 → 成片，五层。
    但成片有**七个**直接上游 —— 只跟第一条会把"它还用了哪些东西"藏起来，
    于是 `--tree` 的输出看着像一条链、实际是棵树。

    去重是全局的（同一张定妆照被两镜用上时只展开一次）。一场戏六十个镜头
    全展开的话能打出几万行，而"这张脸被用过"说一次就够了 —— 但**反复出现
    的那几次仍然会打印成叶子**（`repeat=True`），否则这就成了另一种谎：
    看着像只有第一镜用了那张定妆照。
    """
    root = assets.get(asset_id_)
    if root is None:
        return []
    out: List[LineageNode] = [LineageNode(depth=0, rel="", asset=root, via="")]
    seen = {asset_id_}

    def walk(current: str, depth: int) -> None:
        if depth >= max_depth:
            return
        for parent_id, rel in parents_of(edges, current):
            node = assets.get(parent_id)
            if node is None:
                continue
            if parent_id in seen:
                out.append(LineageNode(depth=depth + 1, rel=rel, asset=node,
                                       via=EDGE_DESC.get(rel, ""), repeat=True))
                continue
            seen.add(parent_id)
            out.append(LineageNode(depth=depth + 1, rel=rel, asset=node,
                                   via=EDGE_DESC.get(rel, "")))
            walk(parent_id, depth + 1)

    walk(asset_id_, 0)
    return out


# ———————————————————————————————————————————————— 对账


@dataclass
class Drift:
    """台账和磁盘对不上的地方。两种都要报，理由不同：

    * `unrecorded`：磁盘上有的东西台账里没有 —— 说明有产物**绕过了台账**，
      于是"这件东西是谁生成的"从此答不出来。多半是新增了产出点忘了记账。
    * `missing`：台账里有的东西磁盘上没有 —— 说明**记录在说谎**。可能是被手工
      删了，也可能是 resume 之前清理过。用户按台账找文件会找到空气。
    """

    unrecorded: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    checked: int = 0
    recorded: int = 0

    @property
    def ok(self) -> bool:
        return not self.unrecorded and not self.missing

    def summary(self) -> str:
        if self.ok:
            return f"台账与磁盘一致（{self.recorded} 条记录 / 检查 {self.checked} 个文件）"
        bits = []
        if self.missing:
            bits.append(f"{len(self.missing)} 条记录的文件已经不在磁盘上")
        if self.unrecorded:
            bits.append(f"{len(self.unrecorded)} 个文件没有进台账")
        return "；".join(bits)


#: 这些文件天生不进台账：SQLite 的 WAL/SHM、临时文件、以及"目录"本身。
_IGNORE_SUFFIX = (".sqlite3-wal", ".sqlite3-shm", ".part", ".tmp", ".db-journal")
#: 后缀在中间的临时文件 —— 口型同步写的是 `shot_001_synced.part.mp4`，
#: 那个 `.part` 不在结尾。原子替换之后它就没了；崩在半路才会留一个。
#: 它是**写到一半的东西**，不是产物，记进去只会制造假差异。
_IGNORE_INFIX = (".part.", ".tmp.")
_IGNORE_NAMES = ("ai_drama.sqlite3",)

#: 中间产物：拆段渲染的 `shot_001_p1_clip.mp4` 与 `_work/` 下的拼接临时文件。
#: 它们**不进台账**，理由是它们的存在本来就不稳定 —— 成片拼好就删掉，崩在半路
#: 就留着。记进台账只会制造"这批文件永远对不上账"的假差异，而真正该被注意的
#: 差异会被淹掉。花了多少钱在 costs 表里，丢不了。
_IGNORE_RE = re.compile(r"(^|/)_work/|_p\d+_clip\.mp4$")


def scan_run_dir(run_dir: Path, *, ignore_junk: bool = True) -> List[str]:
    """列出 run 目录下所有**像产物**的文件（相对 run 目录的 posix 路径）。"""
    run_dir = Path(run_dir)
    if not run_dir.exists():
        return []
    out: List[str] = []
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file():
            continue
        name = p.name
        if ignore_junk and (name in _IGNORE_NAMES
                            or name.endswith(_IGNORE_SUFFIX)
                            or any(m in name for m in _IGNORE_INFIX)
                            or name.startswith(".")):
            continue
        rel = p.relative_to(run_dir).as_posix()
        if ignore_junk and _IGNORE_RE.search(rel):
            continue
        out.append(rel)
    return out


def reconcile(root: Path, run_id: str, assets: Sequence[Asset],
              run_dir: Path, *, ignore_junk: bool = True) -> Drift:
    """把台账和磁盘比一遍。**只报不改** —— 自动补记录等于掩盖漏记账的产出点。"""
    run_dir = Path(run_dir)
    root = Path(root)
    recorded: Dict[str, Asset] = {}
    for a in assets:
        if not a.rel:
            continue
        p = resolve(root, a.rel)
        try:
            key = p.relative_to(run_dir).as_posix()
        except ValueError:
            continue          # 不在这个 run 目录里（比如项目定妆照）：不参与对账
        recorded[key] = a

    on_disk = set(scan_run_dir(run_dir, ignore_junk=ignore_junk))
    unrecorded = sorted(on_disk - set(recorded))
    missing = []
    for key, a in sorted(recorded.items()):
        if key not in on_disk:
            missing.append(key)
    drift = Drift(
        unrecorded=unrecorded,
        missing=missing,
        checked=len(on_disk),
        recorded=len(recorded),
    )
    return drift


__all__ = [
    "Asset",
    "Drift",
    "EDGE_DESC",
    "EDGE_RELS",
    "KINDS",
    "KIND_DESC",
    "LineageNode",
    "PROMPT_MAX",
    "asset_id",
    "children_of",
    "digest",
    "lineage",
    "make_asset",
    "parents_of",
    "prompt_hash",
    "reconcile",
    "rel_of",
    "resolve",
    "scan_run_dir",
]
