"""命令行入口。

    python run.py doctor                     环境自检（第一个该跑的命令）
    python run.py run --idea "..."           跑完整条流水线
    python run.py resume <run_id>            断点续跑
    python run.py status <run_id>            看每个镜头卡在哪一步
    python run.py shots <run_id>             导出分镜表（Markdown，可交给人工改）
    python run.py cost <run_id>              成本明细
    python run.py runs                       历史记录
    python run.py providers                  当前注册了哪些 provider

设计原则：**所有命令都必须能在没有任何 API key 的情况下跑通**，
因为骨架的职责是先把"流程"这件事做对，模型随时可以换。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .core import media, paths
from .core.models import STAGES, Run
from .core.pipeline import Pipeline
from .core.store import Store, default_db_path
from .providers import registry

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"


def setup_console() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认是 GBK，直接 print 中文没问题，
    但一旦带上 [OK]/[X]/- 这类字符就会 UnicodeEncodeError 崩掉 ---
    这种崩法极难排查（看起来像"命令跑不了"）。骨架有中文注释和中文输出，
    所以这里统一处理，让它在任何终端都不炸。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:                        # noqa: BLE001
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
        except Exception:                        # noqa: BLE001
            pass


# ————————————————————————————————————————----
# 工具
# ————————————————————————————————————————----


def load_env() -> None:
    """读 .env（如果装了 python-dotenv）；没装就退化成手写解析。

    刻意不把 python-dotenv 设成硬依赖 — 骨架要能零依赖启动。
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_file)
        return
    except Exception:                       # noqa: BLE001
        pass
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def open_store(args: argparse.Namespace) -> Store:
    db = Path(args.db) if getattr(args, "db", None) else default_db_path()
    return Store(db)


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ————————————————————————————————————————----
# 命令
# ————————————————————————————————————————----


def cmd_doctor(args: argparse.Namespace) -> int:
    load_env()
    print(f"ai-drama {__version__}")
    print(f"python  {sys.version.split()[0]}  ({sys.executable})")

    ok = True

    # ffmpeg / ffprobe
    ff = media.find_ffmpeg()
    fp = media.find_ffprobe()
    print(f"ffmpeg  {ff or '[X] 未找到'}")
    print(f"ffprobe {fp or '[X] 未找到'}")
    if not ff:
        ok = False
        print("        -> Windows: winget install Gyan.FFmpeg（装完需要重开终端）")

    # Pillow
    try:
        import PIL

        print(f"Pillow  {PIL.__version__}")
    except Exception:                        # noqa: BLE001
        ok = False
        print("Pillow  [X] 未安装（dryrun 出图需要）  -> pip install pillow")

    # 字体（dryrun 出图要把中文渲染出来）
    fonts = [p for p in __import__("ai_drama.providers.dryrun", fromlist=["WINDOW_FONTS"]).WINDOW_FONTS
             if Path(p).exists()]
    print(f"字体    {len(fonts)} 个可用" + (f"  例：{fonts[0]}" if fonts else "  [!] 没找到中文字体，dryrun 出图会退化成默认位图字体"))

    # provider 配置
    print("\nprovider 配置（环境变量 -> 实际使用）:")
    for kind, env_key, default in [
        ("text", "AI_DRAMA_TEXT", "dryrun"),
        ("image", "AI_DRAMA_IMAGE", "dryrun"),
        ("video", "AI_DRAMA_VIDEO", "dryrun"),
        ("audio", "AI_DRAMA_AUDIO", "dryrun"),
        ("t2i", "AI_DRAMA_T2I", "（未配置，定妆照会跳过）"),
    ]:
        name = os.environ.get(env_key, default)
        mark = "[OK]" if (kind == "t2i" and name.startswith("（")) or name in registry._REGISTRY[kind] else "[X]"
        print(f"  {mark} {kind:<6} {env_key}={name}")

    print(f"\n数据库  {default_db_path()}")
    print(f"skills  {SKILLS_DIR}  ({'存在' if SKILLS_DIR.exists() else '缺失'})")

    if not ok:
        print("\n有阻断项未通过 — 先解决上面 [X] 的条目。")
        return 1
    print("\n环境自检通过。现在可以直接：python run.py run --idea \"你的想法\"")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    load_env()
    print_json(registry.available())
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    runs = store.list_runs(limit=args.limit)
    if not runs:
        print("还没有任何 run。先跑：python run.py run --idea \"...\"")
        return 0
    for r in runs:
        final = r.final_video or "-"
        print(f"{r.run_id}  {r.state:<9} stage={r.stage:<10} {r.created_at}  {r.idea[:30]}")
        if args.verbose:
            print(f"     out : {r.out_dir}")
            print(f"     final: {final}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    bundle = registry.build_bundle()
    pipe = Pipeline(
        root, store, bundle,
        budget_usd=args.budget, concurrency=args.concurrency,
        force=args.force, verbose=True,
    )
    stages = args.stages.split(",") if args.stages else None
    run = pipe.start(
        args.idea,
        style=args.style or "",
        scenes=args.scenes,
        shots_per_scene=args.shots,
        voice=args.voice,
    )
    print(f"run_id = {run.run_id}")
    print(f"out    = {run.out_dir}")
    result = pipe.run(run, stages=stages)
    print_result(result)
    return 0 if result["state"] == "done" else 2


def cmd_resume(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    pipe = Pipeline(
        root, store, registry.build_bundle(),
        budget_usd=args.budget, concurrency=args.concurrency,
        force=args.force, verbose=True,
    )
    stages = args.stages.split(",") if args.stages else None
    result = pipe.resume(args.run_id, stages=stages)
    print_result(result)
    return 0 if result["state"] == "done" else 2


def cmd_status(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    run = store.get_run(args.run_id)
    if run is None:
        print(f"run not found: {args.run_id}")
        return 1
    shots = store.list_shots(args.run_id)
    counts: Dict[str, int] = {}
    for s in shots:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    total = sum(float(s["duration"] or 0) for s in shots)

    print(f"run    {run.run_id}")
    print(f"state  {run.state}   stage={run.stage}")
    print(f"idea   {run.idea}")
    print(f"out    {run.out_dir}")
    print(f"final  {run.final_video or '-'}")
    if run.error:
        print(f"error  {run.error}")
    print(f"镜头   {len(shots)} 个 / 名义时长 {total:.1f}s")
    print(f"状态   " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("\n逐镜进度：")
    for s in shots:
        flags = []
        flags.append("KF" if s["keyframe"] else "--")
        flags.append("VID" if s["video"] else "---")
        flags.append("AUD" if s["audio"] else "---")
        print(f"  {s['label']:<16} {s['state']:<8} {' '.join(flags)}  {s['error'] or ''}")
    return 0


def cmd_shots(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    ep = store.get_episode(args.run_id)
    if ep is None:
        print(f"没有分镜记录：{args.run_id}")
        return 1
    if args.format == "json":
        print_json(ep.to_dict())
        return 0

    lines: List[str] = []
    lines.append(f"# {ep.title}\n")
    lines.append(f"> {ep.logline}\n")
    lines.append(f"- **风格**：{ep.style}")
    lines.append(f"- **总时长**：{ep.total_duration}s（{len(ep.shots)} 镜）")
    lines.append(f"- **角色**：{', '.join(c.name for c in ep.characters) or '-'}\n")
    for sc in ep.scenes:
        lines.append(f"## {sc.scene_id} {sc.title}")
        lines.append(f"*{sc.location} - {sc.time_of_day}* — {sc.summary}\n")
        lines.append("| # | 景别 | 时长 | 画面 | 台词 |")
        lines.append("|---|------|------|------|------|")
        for sh in sc.shots:
            dlg = sh.dialogue.replace("|", "\\|") or "—"
            act = sh.action.replace("|", "\\|")
            lines.append(f"| {sh.order} | {sh.framing} | {sh.duration}s | {act} | {dlg} |")
        lines.append("")
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写出 {args.out}")
    else:
        print(text)
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    print_json(store.cost_summary(args.run_id))
    return 0


def print_result(result: Dict[str, Any]) -> None:
    print("\n--- 阶段报告 ---")
    for rep in result.get("reports", []):
        print(
            f"  {rep['stage']:<11} 处理 {rep['processed']:<4} 跳过 {rep['skipped']:<4} "
            f"失败 {rep['failed']:<3} 花费 ${rep['money']:.4f}"
        )
        for note in rep.get("notes", []):
            print(f"      - {note}")
    cost = result.get("cost") or {}
    if cost:
        print(f"\n总计花费 ${cost.get('total_money', 0):.4f}")
    if result.get("final_video"):
        print(f"成片：{result['final_video']}")


# ————————————————————————————————————————----
# 参数解析
# ————————————————————————————————————————----


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-drama",
        description="AI 短剧骨架：可替换底座 + 可恢复流水线 + 成本可记账",
    )
    p.add_argument("--version", action="version", version=f"ai-drama {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", help="SQLite 路径（默认 ./runs/ai_drama.sqlite3）")
        sp.add_argument("--root", help="产物根目录（默认当前目录）")

    sp = sub.add_parser("doctor", help="环境自检")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("providers", help="列出已注册的 provider")
    sp.set_defaults(func=cmd_providers)

    sp = sub.add_parser("runs", help="历史 run")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("-v", "--verbose", action="store_true")
    add_common(sp)
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("run", help="跑一条完整流水线")
    sp.add_argument("--idea", required=True, help="一句话想法")
    sp.add_argument("--style", default="", help="视觉风格（可选）")
    sp.add_argument("--scenes", type=int, default=3, help="场次数（默认 3）")
    sp.add_argument("--shots", type=int, default=4, help="每场镜头数（默认 4）")
    sp.add_argument("--voice", default="female_01", help="配音音色")
    sp.add_argument("--stages", help=f"只跑指定阶段，逗号分隔：{','.join(STAGES)}")
    sp.add_argument("--budget", type=float, default=0.0, help="预算上限（美元），0 表示不限")
    sp.add_argument("--concurrency", type=int, default=3)
    sp.add_argument("--force", action="store_true", help="忽略缓存全部重跑")
    add_common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resume", help="断点续跑")
    sp.add_argument("run_id")
    sp.add_argument("--stages", help="只跑指定阶段，逗号分隔")
    sp.add_argument("--budget", type=float, default=0.0)
    sp.add_argument("--concurrency", type=int, default=3)
    sp.add_argument("--force", action="store_true")
    add_common(sp)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("status", help="逐镜进度")
    sp.add_argument("run_id")
    add_common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("shots", help="导出分镜表")
    sp.add_argument("run_id")
    sp.add_argument("--format", choices=["md", "json"], default="md")
    sp.add_argument("--out", help="输出文件（不给就打到 stdout）")
    add_common(sp)
    sp.set_defaults(func=cmd_shots)

    sp = sub.add_parser("cost", help="成本明细")
    sp.add_argument("run_id")
    add_common(sp)
    sp.set_defaults(func=cmd_cost)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。用 `python run.py resume <run_id>` 可以从断点继续。")
        return 130
    except Exception as exc:                 # noqa: BLE001
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("AI_DRAMA_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
