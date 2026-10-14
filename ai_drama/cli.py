"""命令行入口。

    python run.py doctor                     环境自检（第一个该跑的命令）
    python run.py run --idea "..."           跑完整条流水线
    python run.py resume <run_id>            断点续跑
    python run.py status <run_id>            看每个镜头卡在哪一步
    python run.py shots <run_id>             导出分镜表（Markdown，可交给人工改）
    python run.py cost <run_id>              成本明细
    python run.py qc <run_id>                看质检报告（三态：pass / fail / unchecked）
    python run.py rules                      看当前质检规则与生效来源
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
from .core.qc import QcRules, rules_path
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
        strict_qc=not getattr(args, "no_strict_qc", False),
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
        strict_qc=not getattr(args, "no_strict_qc", False),
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


def cmd_qc(args: argparse.Namespace) -> int:
    """看质检报告。

    刻意**不返回 0 就直接说"通过"**：三态要分开返回码，
    这样在 CI 或脚本里 `python run.py qc <id> || echo 有问题` 才是有意义的。
        0 = pass / 2 = fail / 3 = unchecked（没查成，不等于通过）
    """
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    run = store.get_run(args.run_id)
    if run is None:
        print(f"run not found: {args.run_id}")
        return 1
    path = paths.qc_report(root, run.run_id)
    if not path.exists():
        print(f"还没有质检报告：{path}")
        print("跑一次 `python run.py resume <run_id> --stages qc` 就有了。")
        return 3
    if args.format == "json":
        print_json(json.loads(path.read_text(encoding="utf-8")))
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"run    {data.get('run_id')}")
    print(f"状态   {str(data.get('state', '?')).upper()}"
          f"   (pass 才算通过；unchecked 表示没查成)")
    print(f"镜头   {data.get('shots_total')} 个")
    print(f"统计   {data.get('counts')}")
    if data.get("blocked_reason"):
        print(f"原因   {data['blocked_reason']}")
    print("")
    for f in data.get("findings", []):
        mark = "[?]" if f.get("unchecked") else {"block": "[X]", "warn": "[!]", "info": "[i]"}.get(f.get("severity"), "[?]")
        loc = f"（{f['where']}）" if f.get("where") else ""
        print(f"{mark} {f['rule']}{loc}：{f['message']}")
        if f.get("detail"):
            print(f"      {f['detail']}")
        if f.get("suggestion"):
            print(f"      -> {f['suggestion']}")
    state = str(data.get("state") or "")
    return {"pass": 0, "fail": 2, "unchecked": 3}.get(state, 1)


def cmd_rules(args: argparse.Namespace) -> int:
    """看当前生效的质检规则，以及它是从哪来的。

    这条命令存在的理由：**"改了配置没生效"是最难发现的一类问题**。
    把来源直接打出来，你就不用猜了。
    """
    load_env()
    root = Path(args.root).resolve() if args.root else Path.cwd()
    path = rules_path(root)
    print(f"规则文件  {path}")
    if path.exists():
        print("来源      文件（config/qc_rules.yaml）")
    else:
        print("来源      内置默认值（没有 config/qc_rules.yaml）")
    try:
        rules = QcRules.load(path)
    except Exception as exc:                          # noqa: BLE001
        print(f"\n[X] 规则文件读不了：{type(exc).__name__}: {exc}")
        print("    修好它，或删掉这个文件退回默认值 —— 不要放着不管：")
        print("    骨架宁可报错也不静默用默认值跑，因为你以为改过的阈值其实没生效。")
        return 2
    print_json(rules.to_dict())
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """看模型目录，以及**骨架此刻真正读到的是哪一份**。

    和 `rules` 同样的理由：目录读不到时会退回内置的那份（只有 dryrun），
    而内置目录里没有你的模型、也就没有单段时长上限 —— 于是镜头不会被拆、
    接上真厂商时就会拿到被截断的片段。所以"我在用哪份目录"必须看得见。
    """
    from .core.catalog import load_catalog

    load_env()
    root = Path(args.root).resolve() if args.root else Path.cwd()
    catalog = load_catalog(root)
    path = catalog_path_show(root)
    print(f"目录文件  {path}")
    if catalog.problem:
        print(f"[!] {catalog.problem}")
        print("    正在用内置目录。它只保证 dryrun 能跑，不含你的模型能力上限。")
    else:
        print(f"来源      文件（config/models.yaml），共 {len(catalog.models)} 条")

    print("\n参考素材字段（写错了接口不报错，只是安静地忽略）：")
    rf = catalog.ref_fields
    for name, value in rf.to_dict().items():
        print(f"  {name:<13} {value or '(关)'}")
    print(f"  实际送出的图片字段：{', '.join(rf.image_field_names())}")

    print("\n形态默认：")
    for kind in ("text", "image", "video", "audio", "t2i"):
        entry = catalog.default_model(kind)
        if entry is None:
            continue
        mark = "" if catalog.defaults.get(kind) else "（目录没配，取最便宜的）"
        print(f"  {kind:<6} {entry.id} {mark}")

    kind = getattr(args, "kind", None)
    print(f"\n模型清单{('（' + kind + '）') if kind else ''}：")
    lines = catalog.credit_lines(kind or "")
    if not lines:
        print("  （没有）")
    for line in lines:
        print(f"  {line}")

    if getattr(args, "json", False):
        print()
        print_json(catalog.to_dict())
    return 0


def catalog_path_show(root: Path) -> Path:
    from .core.catalog import catalog_path

    return catalog_path(root)


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
    sp.add_argument("--no-strict-qc", action="store_true",
                    help="质检非 PASS 也放行（默认严格：unchecked/fail 都不算完成）")
    add_common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resume", help="断点续跑")
    sp.add_argument("run_id")
    sp.add_argument("--stages", help="只跑指定阶段，逗号分隔")
    sp.add_argument("--budget", type=float, default=0.0)
    sp.add_argument("--concurrency", type=int, default=3)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-strict-qc", action="store_true")
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

    sp = sub.add_parser("qc", help="质检报告（0=通过 2=不通过 3=没查成）")
    sp.add_argument("run_id")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    add_common(sp)
    sp.set_defaults(func=cmd_qc)

    sp = sub.add_parser("rules", help="看当前质检规则与生效来源")
    add_common(sp)
    sp.set_defaults(func=cmd_rules)

    sp = sub.add_parser("models", help="看模型目录、能力上限与参考素材字段")
    add_common(sp)
    sp.add_argument("--kind", help="只看某一形态：text / image / video / audio / t2i")
    sp.add_argument("--json", action="store_true", help="附完整目录 JSON")
    sp.set_defaults(func=cmd_models)

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
