"""MCP 服务器：让 Claude Code / Cursor 这类 agent 直接拿到提示词。

走的是 MCP 的 stdio 传输 —— 一行一个 JSON-RPC 2.0 消息，**stdout 只有协议**，
任何 log 都得写 stderr。这不是洁癖：stdout 混进一行阶段报告，客户端解析就断了。

为什么零依赖手写而不是装 `mcp` 包：这个仓库的卖点之一是不装任何东西就能跑，
为了一个只用到 4 个方法的协议引入 SDK（及其传递依赖），代价不成比例。协议本身
只有 initialize / tools/list / tools/call 三件事，两百行足够。

工具设计的一条原则：**不悄悄花钱**。`write_prompts` 会调文本模型（写剧本），
但一个图/视频 provider 都不碰；点出片得用户自己去 `python run.py resume`。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .core import export
from .core import lint as lintlib
from .core import paths
from .core.pipeline import Pipeline
from .core.store import Store, default_db_path
from .providers import registry

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ai-drama"


# ——————————————————————————————————————————— 工具


def _db_of(args: Dict[str, Any]) -> Path:
    """库路径：显式给的优先，否则跟随工作目录（和 CLI 的 `default_db_path` 一致）。"""
    raw = str(args.get("db") or "").strip()
    return Path(raw).resolve() if raw else default_db_path()


def _bundle(args: Dict[str, Any], root: Path) -> Dict[str, Any]:
    """跑 script + storyboard，返回提示词包（和 `python run.py prompts` 同源）。"""
    idea = str(args.get("idea") or "").strip()
    if not idea:
        raise ValueError("缺 idea：一句话剧情")

    db = _db_of(args)
    store = Store(db)
    try:
        pipe = Pipeline(
            root, store, registry.build_bundle(),
            budget_usd=float(args.get("budget") or 0.0), concurrency=1,
            force=False, verbose=False, strict_qc=False, subtitles=False,
            aspect=str(args.get("aspect") or "9:16"),
        )
        run = pipe.start(
            idea,
            style=str(args.get("style") or ""),
            scenes=int(args.get("scenes") or 3),
            shots_per_scene=int(args.get("shots_per_scene") or 4),
            target_seconds=float(args.get("target_seconds") or 0.0),
        )
        result = pipe.run(run, stages=["script", "storyboard"])
        if result.get("state") == "failed":
            raise RuntimeError(str(result.get("error") or "阶段失败，见 stderr"))
        ep = store.get_episode(run.run_id)
        if ep is None:
            raise RuntimeError("没有 episode：脚本阶段没落盘")
        return export.bundle(
            ep,
            aspect=str((run.meta or {}).get("aspect") or ""),
            dialect=str(args.get("dialect") or ""),
        )
    finally:
        store.close()


def _lint(args: Dict[str, Any], root: Path) -> Dict[str, Any]:
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("缺 run_id")
    db = _db_of(args)
    store = Store(db)
    try:
        ep = store.get_episode(run_id)
        if ep is None:
            raise ValueError(f"没有这条 run 的 episode：{run_id}")
        budget = int(args.get("budget_chars") or 220)
        shots: List[Dict[str, Any]] = []
        for shot in ep.shots:
            found = lintlib.lint_shot(shot, budget=budget)
            shots.append({
                "label": shot.label,
                "seconds": round(float(shot.duration or 0.0), 3),
                "issues": [{"code": f.code, "level": f.level, "message": f.message}
                           for f in found],
            })
        total = sum(len(s["issues"]) for s in shots)
        return {"run_id": run_id, "title": ep.title or "", "issues": total,
                "shots": shots}
    finally:
        store.close()


def _runs(args: Dict[str, Any], root: Path) -> Dict[str, Any]:
    db = _db_of(args)
    store = Store(db)
    try:
        limit = int(args.get("limit") or 10)
        rows = []
        for run in store.list_runs()[:limit]:
            ep = store.get_episode(run.run_id)
            rows.append({
                "run_id": run.run_id,
                "title": (ep.title if ep else "") or run.idea[:24],
                "state": run.state,
                "stage": run.stage,
                "seconds": round(float(ep.total_duration or 0.0), 3) if ep else 0.0,
            })
        return {"runs": rows}
    finally:
        store.close()


_TOOLS: Dict[str, Callable[[Dict[str, Any], Path], Any]] = {
    "write_prompts": _bundle,
    "lint_prompts": _lint,
    "list_runs": _runs,
}

_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "write_prompts": {
        "type": "object",
        "properties": {
            "idea": {"type": "string", "description": "一句话剧情"},
            "scenes": {"type": "integer", "default": 3},
            "shots_per_scene": {"type": "integer", "default": 4},
            "target_seconds": {"type": "number", "default": 0,
                               "description": "整部目标时长，0 表示不定"},
            "style": {"type": "string", "description": "整体画面风格"},
            "dialect": {"type": "string",
                        "description": "平台方言：generic/kling/jimeng/seedance/…"},
            "aspect": {"type": "string", "default": "9:16"},
            "budget": {"type": "number", "default": 0},
            "root": {"type": "string", "description": "工作目录，默认当前目录"},
            "db": {"type": "string"},
        },
        "required": ["idea"],
    },
    "lint_prompts": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "budget_chars": {"type": "integer", "default": 220},
            "root": {"type": "string"},
            "db": {"type": "string"},
        },
        "required": ["run_id"],
    },
    "list_runs": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 10},
            "root": {"type": "string"},
            "db": {"type": "string"},
        },
    },
}

_DESCRIPTIONS = {
    "write_prompts": "一句话剧情 → 定妆照 / 首帧 / 尾帧 / 运镜 / 负向词。只调文本模型，不出图。",
    "lint_prompts": "体检一条 run 的提示词：太长、运镜太快、有台词没开口、缺负向词。",
    "list_runs": "列出最近的 run（标题、状态、总时长）。",
}


def _root_of(args: Dict[str, Any]) -> Path:
    raw = str(args.get("root") or "").strip()
    return Path(raw).resolve() if raw else Path.cwd()


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行一个工具，返回 MCP 的 content 数组。异常翻成 isError。"""
    fn = _TOOLS.get(name)
    if fn is None:
        raise ValueError(f"没有这个工具：{name}")
    # 工具里踩到的 print 一律改道 stderr：stdout 只留给协议本身
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = fn(args, _root_of(args))
    except Exception as exc:  # noqa: BLE001 - 工具失败要回给客户端，不能把服务打死
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True}
    finally:
        sink.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


# ——————————————————————————————————————————— 协议


def handle(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """一条 JSON-RPC 请求 → 一条响应。通知（没有 id）返回 None。"""
    method = request.get("method") or ""
    rid = request.get("id")
    params = request.get("params") or {}

    if rid is None:                    # notifications/initialized 之类的通知
        return None

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })

    if method == "tools/list":
        return _ok(rid, {"tools": [
            {"name": name,
             "description": _DESCRIPTIONS.get(name, ""),
             "inputSchema": _SCHEMAS.get(name, {"type": "object"})}
            for name in _TOOLS
        ]})

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            return _ok(rid, call_tool(name, args))
        except Exception as exc:  # noqa: BLE001
            return _err(rid, -32602, f"{type(exc).__name__}: {exc}")

    if method == "ping":
        return _ok(rid, {})

    return _err(rid, -32601, f"不支持的方法：{method}")


def _ok(rid: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None) -> int:
    """读一行、答一行。读到 EOF 就退出。"""
    # Windows 上 Python 的 stdio 默认跟随系统代码页（cp936），而 MCP 走 UTF-8。
    # 不显式改，中文 idea 一进来就变成代理对，json.dumps 直接炸。
    for stream in (sys.stdin, sys.stdout):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")
    src = stdin or sys.stdin
    dst = stdout or sys.stdout
    for line in src:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            dst.write(json.dumps(_err(None, -32700, f"JSON 解不开：{exc}")) + "\n")
            dst.flush()
            continue
        response = handle(request)
        if response is not None:
            dst.write(json.dumps(response, ensure_ascii=False) + "\n")
            dst.flush()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return serve()
