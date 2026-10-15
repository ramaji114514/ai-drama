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
from .core import media, paths, project
from .core.catalog import KINDS
from .core.faces import (
    FaceAudit,
    FaceCandidate,
    audit_lines,
    conflict_question,
    decide,
    pick_baseline,
)
from .core.models import Episode, STAGES, Run
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

    # provider 配置。
    #
    # 这里**必须**问 registry 要结论，不能自己再写一份默认值表：之前这一份把
    # t2i 的默认值写成"未配置，定妆照会跳过"，而 build_bundle 早就默认 dryrun
    # 了 —— doctor 说一套、流水线做另一套，正是本项目要消灭的那类失败。
    print("\nprovider 配置（环境变量 -> 实际使用）:")
    for kind in registry.KIND_ENV:
        env_key = registry.kind_env(kind)
        raw = os.environ.get(env_key)
        setting = f"{env_key} 未设置" if raw is None else f"{env_key}={raw}"
        name = registry.effective_name(kind)
        optional = kind in registry.OPTIONAL_KINDS
        if not name:
            if optional:
                # 可选形态被显式关掉是合法的，但**后果要说清楚** ——
                # "定妆照跳过"和"嘴型没对上"都不是能从别处看出来的事。
                why = {
                    "t2i": "定妆照会被跳过，参考锁定会整段失效",
                    "lipsync": "成片里嘴型不会对齐",
                }.get(kind, "这一步会被跳过")
                print(f"  [!] {kind:<7} {setting} -> 已关闭（可选形态）  —— {why}")
            else:
                ok = False
                print(f"  [X] {kind:<7} {setting} -> 已关闭 —— 但这个形态是必须的")
            continue
        if name in registry._REGISTRY[kind]:
            extra = "（默认值；关掉它写 none）" if optional and raw is None else ""
            print(f"  [OK] {kind:<7} {setting} -> {name}{extra}")
        else:
            ok = False
            avail = ", ".join(sorted(registry._REGISTRY[kind])) or "（一个都没有）"
            print(f"  [X] {kind:<7} {setting} -> 没注册过这个名字。可用：{avail}")

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


def _subtitles_wanted() -> bool:
    """字幕默认开；`AI_DRAMA_SUBTITLES=0/none/off/false/no` 关掉。

    和 `--no-subtitles` 的关系是"两者都能关，谁都不能强行开" ——
    环境变量适合"我这个项目就是不要字幕"，命令行开关适合"这一条别烧"。
    """
    raw = (os.environ.get("AI_DRAMA_SUBTITLES", "") or "").strip().lower()
    return raw not in ("0", "none", "off", "false", "no")


def cmd_run(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    bundle = registry.build_bundle()
    project_id = (getattr(args, "project", "") or "").strip()
    if project_id and project.load(root, project_id) is None:
        print(f"project 不存在：{project_id}")
        print(f"（找的是 {paths.project_json(root, project_id)}）")
        print("先建一个：python run.py project new \"剧名\"")
        return 1
    pipe = Pipeline(
        root, store, bundle,
        budget_usd=args.budget, concurrency=args.concurrency,
        force=args.force, verbose=True,
        strict_qc=not getattr(args, "no_strict_qc", False),
        subtitles=(not getattr(args, "no_subtitles", False)) and _subtitles_wanted(),
        project_id=project_id or None,
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
    if project_id:
        proj = project.load(root, project_id)
        print(f"project= {project_id}《{proj.title}》"
              f"  第 {run.meta.get('episode_no', '?')} 集"
              f"  在册角色 {len(proj.characters)} 个")
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
        subtitles=(not getattr(args, "no_subtitles", False)) and _subtitles_wanted(),
    )
    stages = args.stages.split(",") if args.stages else None
    result = pipe.resume(args.run_id, stages=stages)
    print_result(result)
    return 0 if result["state"] == "done" else 2


def _root_of(args: argparse.Namespace) -> Path:
    return Path(args.root).resolve() if args.root else Path.cwd()


def cmd_project(args: argparse.Namespace) -> int:
    """`project` 后面没跟子命令时的用法提示。

    不用 `parser.print_help()`：那个闭包会在 build_parser 里被后面的
    `sp = sub.add_parser(...)` 覆盖掉，打出来的是**另一个**命令的帮助。
    """
    print("用法：run.py project <子命令>")
    print()
    print("  new <剧名>            建一部剧（不花钱，只写一个 json）")
    print("  list                  列出所有剧、各自的集数与角色数")
    print("  show <project_id>     看角色圣经与这部剧下的每一集")
    print("  set <project_id> <角色> [--voice/--desc/--anchor-prompt]")
    print()
    print("为什么需要它：默认一次 run 出一集，角色由模型每集重新发明 ——")
    print("第 2 集的脸就跟第 1 集不一样了。把每一集都挂在同一个 --project 下，")
    print("角色外观与定妆照就会跨集复用同一份文件。")
    return 1


def cmd_project_new(args: argparse.Namespace) -> int:
    """建一部剧。这一步**不花钱**，只是把角色圣经的文件放好。"""
    load_env()
    root = _root_of(args)
    proj = project.new_project(args.title, logline=args.logline or "", style=args.style or "")
    p = proj.save(root)
    print(f"project_id = {proj.project_id}")
    print(f"title      = {proj.title}")
    print(f"file       = {p}")
    print()
    print("接下来每一集都带上它，角色与定妆照就会跨集复用：")
    print(f"  python run.py run --project {proj.project_id} --idea \"第 1 集的想法\"")
    print("角色圣经可以直接手改（加角色、改外貌、定音色），改完下一集生效。")
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    load_env()
    root = _root_of(args)
    store = open_store(args)
    runs = store.list_runs()
    rows = project.list_projects_lenient(root)
    if not rows:
        print(f"还没有任何项目（{paths.projects_root(root)} 是空的）")
        print("建一个：python run.py project new \"剧名\"")
        return 0
    print(f"{'project_id':<24} {'集数':>4} {'角色':>4}  标题")
    for proj, err in rows:
        if proj is None:
            print(f"{'?':<24} {'':>4} {'':>4}  [坏] {err}")
            continue
        n = sum(1 for r in runs if (r.meta or {}).get("project_id") == proj.project_id)
        print(f"{proj.project_id:<24} {n:>4} {len(proj.characters):>4}  {proj.title}")
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    load_env()
    root = _root_of(args)
    store = open_store(args)
    proj = project.load(root, args.project_id)
    if proj is None:
        print(f"project 不存在：{args.project_id}")
        return 1

    print(f"project_id {proj.project_id}")
    print(f"title      {proj.title}")
    if proj.logline:
        print(f"logline    {proj.logline}")
    if proj.style:
        print(f"style      {proj.style}")
    print(f"file       {paths.project_json(root, proj.project_id)}")

    print(f"\n在册角色 {len(proj.characters)} 个：")
    for c in proj.characters:
        # 定妆照是否**真的在磁盘上**：圣经里写着路径、文件没了，是这里最该
        # 被抓出来的一种坏 —— 它会让下一集静默地重新生成一张脸。
        if not c.ref_image:
            shot = "无定妆照"
        elif Path(c.ref_image).exists():
            shot = "定妆照 OK"
        else:
            shot = f"[!] 定妆照丢了：{c.ref_image}"
        voice = proj.voice_map.get(c.name, "（未指定，用 --voice）")
        print(f"  {c.name:<12} {shot:<34} 音色 {voice}")
        if args.verbose and c.desc:
            print(f"      {c.desc}")

    episodes = [r for r in store.list_runs()
                if (r.meta or {}).get("project_id") == proj.project_id]
    episodes.sort(key=lambda r: r.created_at)
    print(f"\n这部剧下的 run {len(episodes)} 个（按时间）：")
    for i, r in enumerate(episodes, start=1):
        mark = "成片" if r.final_video else r.state
        print(f"  第 {i:>2} 集  {r.run_id}  {mark:<8} {r.idea[:32]}")
    return 0


def cmd_project_set(args: argparse.Namespace) -> int:
    """给一个角色写定音色 / 改外貌描述。手改 project.json 也行，这里图个方便。"""
    load_env()
    root = _root_of(args)
    proj = project.load(root, args.project_id)
    if proj is None:
        print(f"project 不存在：{args.project_id}")
        return 1
    canon = proj.character_by_name(args.name)
    if canon is None:
        print(f"角色不在圣经里：{args.name}")
        print("在册角色：" + "、".join(c.name for c in proj.characters))
        return 1
    if args.desc is not None:
        canon.desc = args.desc
    if args.anchor_prompt is not None:
        canon.anchor_prompt = args.anchor_prompt
    if args.voice is not None:
        proj.voice_map[canon.name] = args.voice
    p = proj.save(root)
    print(f"已更新 {canon.name} -> {p}")
    if args.anchor_prompt is not None:
        print("[!] 改了 anchor_prompt 但**没有重新生成定妆照**："
              "这一集的参考图还是旧的那张。要换脸请删掉 "
              f"{paths.project_portrait(root, proj.project_id, canon.name)} 再跑。")
    return 0


def _outstanding_jobs(shot_row: Dict[str, Any]) -> Dict[str, str]:
    """这一镜有没有"已提交、还没拿到产物"的任务（job_ids 在库里是 JSON 文本）。"""
    raw = shot_row.get("job_ids")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


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
    run_pid = (run.meta or {}).get("project_id", "")
    if run_pid:
        # 一集跑完之后最该知道的就是"这是哪部剧的第几集" ——
        # 不然面对 60 个 run_id 你分不清哪个是第几集。
        print(f"project {run_pid}  第 {run.meta.get('episode_no', '?')} 集")
    print(f"out    {run.out_dir}")
    print(f"final  {run.final_video or '-'}")
    if run.error:
        print(f"error  {run.error}")
    print(f"镜头   {len(shots)} 个 / 名义时长 {total:.1f}s")
    print(f"状态   " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # 已付费但还没取回的任务：这是"钱花出去了、东西还没到手"的唯一可见处。
    # 不说出来，用户只会觉得"怎么又扣了一次钱"。
    outstanding = [(s["label"], jobs) for s in shots
                   if (jobs := _outstanding_jobs(s))]
    if outstanding:
        print(f"\n未结算任务   {sum(len(j) for _, j in outstanding)} 个"
              f"（已提交、还没拿到产物；resume 会先认领它们，不会重复付费）")
        for label, jobs in outstanding:
            for part, job in sorted(jobs.items()):
                print(f"  {label:<16} p{part}  {job}")

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


def cmd_faces(args: argparse.Namespace) -> int:
    """谁的脸算准。

    这一层解决的是真实现场里最难堪的一件事：某个角色的主形象被模型的内容
    护栏拒了，你换三家模型各出一张，于是产物目录里躺着三张脸 ——
    **谁当准，后面十几集全跟着走**，而且等你发现时已经渲完了。

    本命令做两件事，并且**明确区分它做不到的事**：
      1. 归拢候选（这个角色名下所有定妆照），排出基准（来源 > 类别 > 新旧）
      2. 报告冲突：除了基准还有别的脸，如实列出来
    它**不做**两两判定 —— 那需要视觉模型，没配就不编造结论，
    而不是把"没查成"印成"通过"。
    """
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    run = store.get_run(args.run_id)
    if run is None:
        print(f"run not found: {args.run_id}")
        return 1
    ep_json = paths.episode_json(root, run.run_id)
    if not ep_json.exists():
        print(f"还没有分镜：{ep_json}")
        return 1
    ep = Episode.from_dict(json.loads(ep_json.read_text(encoding="utf-8")))

    audits = []
    run_pid = (run.meta or {}).get("project_id", "")
    for ch in ep.characters:
        files = paths.portrait_variants(root, run.run_id, ch.name, run_pid)
        if not files:
            audits.append(FaceAudit(
                character=ch.name,
                skipped=f"run 目录（和项目 {run_pid or '—'} 的 portraits）里都没有这个角色的定妆照",
            ))
            continue
        # 新者在前：同来源同类别时，"最新的一张"才是默认基准
        files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
        used = Path(ch.ref_image).name if ch.ref_image else ""
        cands = []
        for i, p in enumerate(files):
            # 流水线正在用的那张 = "参考图包里在用的"，它是已经定下来的选择
            source = "pack" if p.name == used else "asset"
            cands.append(FaceCandidate(
                character=ch.name, asset_id=p.name, summary=p.name,
                local=str(p), source=source, kind="角色", seq=len(files) - i,
            ))
        keeper, why = pick_baseline(cands)
        audits.append(decide(ch.name, cands, {}, baseline=keeper, why=why))

    if args.json:
        print_json([a.to_dict() for a in audits])
        return 0

    for line in audit_lines(audits):
        print(line)
    question = conflict_question(audits)
    if question:
        print("")
        print(question)
    if any(a.conflicted for a in audits):
        print("")
        print("注意：这里只给出基准与冲突清单，**没有**做两两判定。")
        print("      判定需要视觉模型（参考图 + 候选图一起交给它逐人打分），")
        print("      现在没有配置，所以这些候选一律按「未确认」看待 ——")
        print("      不是「已经确认是同一个人」。")
    return 0


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
    for kind in KINDS:
        entry = catalog.default_model(kind)
        if entry is None:
            continue
        mark = "" if catalog.defaults.get(kind) else "（目录没配，取最便宜的）"
        print(f"  {kind:<7} {entry.id} {mark}")

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


def cmd_skills(args: argparse.Namespace) -> int:
    """技能目录 / 装载预览 / 正文查看。

    这个命令存在的理由是**两级披露的第二级需要一个人工入口**：
    平时常驻上下文的只有 description，那"这一环节到底会加载哪几篇、
    为什么另外几篇没加载"就必须能查 —— 否则一篇技能静默失效，
    你只会觉得"模型今天不太行"。
    """
    from .core.skills import (
        ACTIVE_LIMIT,
        BODY_BUDGET_TOKENS,
        assemble,
        estimate_tokens,
        load_body,
        load_skills,
        render_index,
        select,
    )

    load_env()
    root = Path(args.root).resolve() if args.root else Path.cwd()
    metas = load_skills(root)
    if not metas:
        print(f"[!] {root / 'skills'} 下没有技能")
        return 1

    if args.show:
        hit = next((m for m in metas if m.name == args.show), None)
        if hit is None:
            names = ", ".join(m.name for m in metas)
            print(f"[X] 没有名叫 {args.show!r} 的技能。现有：{names}")
            return 1
        body = load_body(hit)
        print(f"# {hit.name}（{hit.scope}，priority {hit.priority}）")
        print(f"# {hit.description}")
        print(f"# 正文约 {estimate_tokens(body)} token")
        print()
        print(body)
        return 0

    if args.check:
        bad = 0
        for m in metas:
            body_tokens = estimate_tokens(load_body(m))
            issues = list(m.problems)
            if body_tokens > 5000:
                issues.append(f"正文 {body_tokens} token，超过单篇 5000 的建议值 —— 建议拆篇")
            if issues:
                bad += 1
                print(f"[!] {m.name}")
                for issue in issues:
                    print(f"      {issue}")
        if not bad:
            print(f"[OK] {len(metas)} 篇全过：都有 owner、description 够长、正文不超长")
        else:
            print(f"\n{bad} / {len(metas)} 篇需要处理")
        return 0 if not bad else 2

    stage = getattr(args, "stage", "") or ""
    content_type = getattr(args, "content_type", "") or ""
    print(f"技能目录  {root / 'skills'}（{len(metas)} 篇）")
    print(f"激活上限  同时 ≤{ACTIVE_LIMIT} 篇，正文合计 ≤{BODY_BUDGET_TOKENS} token")
    print(f"常驻成本  约 {estimate_tokens(render_index(metas))} token（只有 description）\n")

    if not stage and not content_type:
        print("全部技能（常驻上下文的就是这些行）：")
        for m in sorted(metas, key=lambda x: x.sort_key):
            flag = "" if m.is_active else f"  [{m.status} 不加载]"
            print(f"  {m.scope:<16} p{m.priority:<3} {m.index_line()}{flag}")
        print("\n用 --stage / --content-type 看某一环节实际会加载哪几篇。")
        if getattr(args, "json", False):
            print()
            print_json([m.to_dict() for m in metas])
        return 0

    sel = select(metas, content_type=content_type, stage=stage)
    print(f"这一环节（内容形态={content_type or '不限'} / 环节={stage or '不限'}）：")
    print(sel.explain())
    print(f"\n正文合计约 {sel.body_tokens} token")
    text, _ = assemble(metas, content_type=content_type, stage=stage)
    if getattr(args, "render", False) and text:
        print("\n" + "=" * 70)
        print(text)
    elif text:
        print("（加 --render 可打印拼好的注入文本）")
    if getattr(args, "json", False):
        print()
        print_json({
            "chosen": [m.to_dict() for m in sel.chosen],
            "skipped_scope": [m.name for m in sel.skipped_scope],
            "skipped_status": [m.name for m in sel.skipped_status],
            "skipped_budget": [m.name for m in sel.skipped_budget],
            "body_tokens": sel.body_tokens,
        })
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

    sp = sub.add_parser("project", help="一部剧的角色圣经：跨集复用角色与定妆照")
    psub = sp.add_subparsers(dest="project_cmd")

    p1 = psub.add_parser("new", help="建一部剧（不花钱）")
    p1.add_argument("title")
    p1.add_argument("--logline", default="")
    p1.add_argument("--style", default="")
    add_common(p1)
    p1.set_defaults(func=cmd_project_new)

    p2 = psub.add_parser("list", help="列出所有剧")
    add_common(p2)
    p2.set_defaults(func=cmd_project_list)

    p3 = psub.add_parser("show", help="看角色圣经与这部剧下的每一集")
    p3.add_argument("project_id")
    p3.add_argument("-v", "--verbose", action="store_true", help="连角色描述一起打出来")
    add_common(p3)
    p3.set_defaults(func=cmd_project_show)

    p4 = psub.add_parser("set", help="改一个在册角色的描述 / 定妆照提示词 / 音色")
    p4.add_argument("project_id")
    p4.add_argument("name")
    p4.add_argument("--desc")
    p4.add_argument("--anchor-prompt")
    p4.add_argument("--voice")
    add_common(p4)
    p4.set_defaults(func=cmd_project_set)

    sp.set_defaults(func=cmd_project)

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
    sp.add_argument("--project", default="", metavar="PROJECT_ID",
                    help="归到某部剧下：角色与定妆照跨集复用（见 project new）")
    sp.add_argument("--stages", help=f"只跑指定阶段，逗号分隔：{','.join(STAGES)}")
    sp.add_argument("--budget", type=float, default=0.0, help="预算上限（美元），0 表示不限")
    sp.add_argument("--concurrency", type=int, default=3)
    sp.add_argument("--force", action="store_true", help="忽略缓存全部重跑")
    sp.add_argument("--no-strict-qc", action="store_true",
                    help="质检非 PASS 也放行（默认严格：unchecked/fail 都不算完成）")
    sp.add_argument("--no-subtitles", action="store_true",
                    help="不把字幕烧进画面（SRT 照样写出来，可外挂）")
    add_common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resume", help="断点续跑")
    sp.add_argument("run_id")
    sp.add_argument("--stages", help="只跑指定阶段，逗号分隔")
    sp.add_argument("--budget", type=float, default=0.0)
    sp.add_argument("--concurrency", type=int, default=3)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-strict-qc", action="store_true")
    sp.add_argument("--no-subtitles", action="store_true")
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
    sp.add_argument("--kind", help="只看某一形态：" + " / ".join(KINDS))
    sp.add_argument("--json", action="store_true", help="附完整目录 JSON")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("skills", help="看技能目录，以及某一环节实际会加载哪几篇")
    add_common(sp)
    sp.add_argument("--stage", help="流水线环节（选题 / 剧本 / 分镜 / 角色 / 提示词 / 质检）")
    sp.add_argument("--content-type", help="内容形态（短剧 / 短视频 / 图文 …）")
    sp.add_argument("--show", help="打印某一篇的正文")
    sp.add_argument("--check", action="store_true", help="体检：owner / description / 正文长度")
    sp.add_argument("--render", action="store_true", help="打印拼好的注入文本")
    sp.add_argument("--json", action="store_true", help="附元数据 JSON")
    sp.set_defaults(func=cmd_skills)

    sp = sub.add_parser("faces", help="谁的脸算准：定妆照多张并存时给出基准与冲突")
    sp.add_argument("run_id")
    add_common(sp)
    sp.add_argument("--json", action="store_true", help="附结构化 JSON")
    sp.set_defaults(func=cmd_faces)

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
