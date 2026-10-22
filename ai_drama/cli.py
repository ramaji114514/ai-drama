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
import csv
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .core import assets as assetslib, dialects, media, paths, project
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
from .providers.base import caps_of

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
                # 可选形态被关掉是合法的，但**后果要说清楚** ——
                # "定妆照跳过"、"嘴型没对上"、"画面没人看过"、"没有音效"
                # 都不是能从别处看出来的事。
                why = {
                    "t2i": "定妆照会被跳过，参考锁定会整段失效",
                    "lipsync": "成片里嘴型不会对齐",
                    "vision": "画面不会被任何人看过，质检里那几项只能报「没查成」",
                    "sfx": "成片里只有台词和 BGM，分镜写的音效不会出现",
                }.get(kind, "这一步会被跳过")
                # 打开它的方式各不相同（"能看图的"和"能做音效的"不是一回事），
                # 所以提示也要按形态分开 —— 一句通用提示会把"能看图的 provider"
                # 推荐给音效，那是明摆着指错路。
                hint = {
                    "vision": "设 AI_DRAMA_VISION=<能看图的 provider>",
                    "sfx": "设 AI_DRAMA_SFX=<音效 provider>",
                }.get(kind, f"把 {env_key} 设成 provider 名")
                if raw is None and kind in registry.DEFAULT_OFF_KINDS:
                    # 默认关闭 ≠ 用户关掉了它。说"已关闭"会让人以为是自己干的。
                    print(f"  [ ] {kind:<7} {setting} -> 默认关闭（{why}）；要打开就{hint}")
                else:
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
        aspect=getattr(args, "aspect", "") or "9:16",
        only_shots=tuple(getattr(args, "only_shot", None) or ()),
    )
    stages = args.stages.split(",") if args.stages else None
    target = float(getattr(args, "target", 0.0) or 0.0)
    if target < 0:
        print("--target 不能是负数（0 表示不定目标）")
        return 2
    run = pipe.start(
        args.idea,
        style=args.style or "",
        scenes=args.scenes,
        shots_per_scene=args.shots,
        voice=args.voice,
        target_seconds=target,
    )
    print(f"run_id = {run.run_id}")
    print(f"out    = {run.out_dir}")
    print(f"画幅   = {run.meta.get('aspect')}（{pipe.frame_size[0]}x{pipe.frame_size[1]}）")
    if project_id:
        proj = project.load(root, project_id)
        print(f"project= {project_id}《{proj.title}》"
              f"  第 {run.meta.get('episode_no', '?')} 集"
              f"  在册角色 {len(proj.characters)} 个")
    result = pipe.run(run, stages=stages)
    print_result(result)
    return 0 if result["state"] == "done" else 2


def _one_prompt_bundle(args: argparse.Namespace, notes: str = "") -> Tuple[dict, str]:
    """跑一次 script + storyboard，回 (提示词包, run_id)。"""
    from .core import export

    store = open_store(args)
    root = Path(args.root).resolve() if getattr(args, "root", None) else Path.cwd()
    bundle_providers = registry.build_bundle()
    pipe = Pipeline(
        root, store, bundle_providers,
        budget_usd=args.budget, concurrency=1,
        force=args.force, verbose=False,
        strict_qc=False,
        subtitles=False,
        project_id=(getattr(args, "project", "") or "").strip() or None,
        aspect=getattr(args, "aspect", "") or "9:16",
    )
    run = pipe.start(
        args.idea,
        style=args.style or "",
        scenes=args.scenes,
        shots_per_scene=args.shots,
        voice=args.voice,
        notes=notes,
    )
    # 只跑这两个阶段：提示词在这里就齐了，后面的阶段是花钱的
    result = pipe.run(run, stages=["script", "storyboard"])
    if result.get("state") == "failed":
        raise RuntimeError(str(result.get("error") or "阶段失败"))
    ep = store.get_episode(run.run_id)
    if ep is None:
        raise RuntimeError("没有 episode（脚本阶段没落盘？）")
    data = export.bundle(ep, aspect=str((run.meta or {}).get("aspect") or ""),
                         dialect=getattr(args, "dialect", "") or "")
    return data, run.run_id


def _variant_notes(k: int, total: int) -> str:
    """第 k 个变体的额外要求。

    变体不是"重跑一遍碰运气" —— 那句"换一个不同的开场"是**唯一**让两次生成
    真正分开的输入。不写它，同一个模型给同一个想法会给出高度相似的骨架。
    """
    if total <= 1:
        return ""
    return (f"这是同一个想法的第 {k} 个变体（共 {total} 个）。"
            "请换一个**不同的开场方式**和**不同的结尾落点**，"
            "人物关系与核心冲突保持不变；避免和第 1 个变体雷同的镜头调度。")


def _compare(rows: List[Tuple[int, str, dict]]) -> str:
    """一行一版：镜数、总时长、片名。挑变体靠这个表，不靠翻 JSON。"""
    lines = ["变体  镜数  总时长  片名"]
    for k, run_id, data in rows:
        shots = sum(len(sc["shots"]) for sc in data["scenes"])
        secs = sum(float(sh.get("duration") or 0) for sc in data["scenes"]
                   for sh in sc["shots"])
        lines.append(f"  {k}    {shots:>2}   {secs:>5.1f}s  {data.get('title') or ''}"
                     f"  ({run_id})")
    return "\n".join(lines)


def cmd_prompts(args: argparse.Namespace) -> int:
    """一句话 → 提示词包。只跑 script + storyboard，一台图模型都不碰。

    存在的理由是**给别的程序用**：agent 要的是一段能 `json.load` 的文本，
    不是一张带小标题的漂亮页面。所以这条命令默认把 JSON 打到 stdout，
    进度写 stderr —— 管道不会因为"阶段报告"混进来而解不开。

    出图/出片是另一回事：这里一个 provider 调用（除文本模型外）都不会发生，
    定妆照和关键帧全是占位。想要的图在 `run.py resume <id> --stages frames`。
    """
    load_env()
    variants = max(1, int(getattr(args, "variants", 1) or 1))
    rows: List[Tuple[int, str, dict]] = []
    try:
        for k in range(1, variants + 1):
            data, run_id = _one_prompt_bundle(args, _variant_notes(k, variants))
            rows.append((k, run_id, data))
    except RuntimeError as exc:
        print(f"没跑成:{exc}", file=sys.stderr)
        return 2

    out = getattr(args, "json", "") or ""
    if variants == 1:
        data = rows[0][2]
        if out and out != "-":
            dest = Path(out)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
            shots = sum(len(sc["shots"]) for sc in data["scenes"])
            print(f"拿到 {len(data['characters'])} 个角色、{len(data['scenes'])} 场 "
                  f"{shots} 镜的提示词 → {dest}", file=sys.stderr)
            print(f"run {rows[0][1]}", file=sys.stderr)
        else:
            # stdout 只有 JSON：管道那头 `| jq .` 直接能用
            print(json.dumps(data, ensure_ascii=True, indent=2))
            print(f"run {rows[0][1]}", file=sys.stderr)
        return 0

    # 多版：默认把对照表写 stderr、把 N 份包写成 JSON 数组打到 stdout。
    # 想拆成文件就给 --json 一个路径，会写成 <名字>.v1.json / .v2.json …
    print(_compare(rows), file=sys.stderr)
    payload = [data for _, _, data in rows]
    if out and out != "-":
        dest = Path(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        for k, run_id, data in rows:
            one = dest.with_name(f"{dest.stem}.v{k}{dest.suffix or '.json'}")
            one.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
            print(f"变体 {k} → {one}（run {run_id}）", file=sys.stderr)
    else:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """stdio 上跑 MCP：一行一个 JSON-RPC。日志一律走 stderr。"""
    from . import mcp_server

    return mcp_server.serve()


def import_episode_json(root: Path, store: Any, run_id: str) -> Optional[int]:
    """把 run 目录里的 `episode.json` 读回库。返回镜头数，文件不存在返回 None。

    `episode.json` 一直是"只写不读"的产物，所以文档里那句"直接改 episode.json
    再只重跑受影响阶段"其实一直做不到 —— 改完 resume，库里还是旧的那份。
    这个函数把它补上。

    做成**显式**（`resume --from-json`）而不是默认行为，是因为
    `run.py shots` 之类的命令改的是库，每次都让文件盖回去会把人刚改的东西冲掉。
    """
    from .core.models import Episode          # 局部导入：只有这里有需要

    ep_json = paths.episode_json(root, run_id)
    if not ep_json.exists():
        return None
    ep = Episode.from_dict(json.loads(ep_json.read_text(encoding="utf-8")))
    if not ep.run_id:
        ep.run_id = run_id
    store.save_episode(ep)
    store.sync_shots(run_id, ep)
    return len(ep.shots)


def cmd_resume(args: argparse.Namespace) -> int:
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    if getattr(args, "from_json", False):
        shots = import_episode_json(root, store, args.run_id)
        if shots is None:
            print(f"没有 episode.json：{paths.episode_json(root, args.run_id)}")
            return 1
        print(f"已按 episode.json 回灌：{shots} 镜")
    pipe = Pipeline(
        root, store, registry.build_bundle(),
        budget_usd=args.budget, concurrency=args.concurrency,
        force=args.force, verbose=True,
        strict_qc=not getattr(args, "no_strict_qc", False),
        subtitles=(not getattr(args, "no_subtitles", False)) and _subtitles_wanted(),
        aspect=getattr(args, "aspect", "") or "9:16",
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
        voice = proj.voice_for(c, "（未指定，用 --voice）")
        alias = f"  别名 {'、'.join(c.aliases)}" if c.aliases else ""
        print(f"  {c.name:<12} {c.char_id or '无id':<16} {shot:<34} 音色 {voice}{alias}")
        if args.verbose:
            # 自动定下来的音色，"凭什么"也要能看见 —— 觉得不对才知道该改哪条
            note = proj.voice_notes.get(c.char_id or "") or proj.voice_notes.get(c.name)
            if note:
                print(f"      音色依据：{note}")
            if c.desc:
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
    canon = proj.character_by_ref(args.name)
    if canon is None:
        print(f"角色不在圣经里（也不是任何角色的别名）：{args.name}")
        print("在册角色：" + "、".join(f"{c.name}({c.char_id or '无id'})" for c in proj.characters))
        return 1
    if args.desc is not None:
        canon.desc = args.desc
    if args.anchor_prompt is not None:
        canon.anchor_prompt = args.anchor_prompt
    if args.voice is not None:
        # 键优先写 id：改了名字之后音色还跟着这个人走
        key = canon.char_id or canon.name
        proj.voice_map[key] = args.voice
        # 人手填的就不该再留一条"自动推断"的笔记 —— 否则文件在撒谎
        proj.voice_notes.pop(key, None)
        proj.voice_notes.pop(canon.name, None)
    p = proj.save(root)
    print(f"已更新 {canon.name}（{canon.char_id or '无id'}）-> {p}")
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
    # 画幅也要在这里露一面：它是"成片形状"的唯一记录处，而老 run 根本没有这个
    # 字段（那时写死在两个调用点里）。不显示的话，"为什么这条片是竖的"
    # 只能靠翻代码回答。
    run_aspect = (run.meta or {}).get("aspect") or "9:16（老 run 没有记录，按默认）"
    print(f"画幅   {run_aspect}")
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


#: 分镜表的列。顺序就是制片那边看表的顺序：先认哪一镜，再看拍什么，
#: 最后才是提示词 —— 提示词是给做图的人用的，不该抢在最前面。
SHOT_CSV_COLUMNS = ("场次", "镜号", "景别", "时长秒", "画面", "结束画面", "台词",
                    "出镜", "首帧提示词", "尾帧提示词", "视频提示词",
                    "负向词图", "负向词视频")


def shots_csv(ep: Any) -> str:
    """整集分镜表 -> CSV 文本（不含 BOM，调用方决定要不要加）。

    用标准库 `csv` 而不是自己拼逗号：台词里本来就有逗号和引号，
    手工拼出来的表格在某些行会错列 —— 而**错列的表格没人会去核对**，
    它就这么被拿去开会了。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SHOT_CSV_COLUMNS)
    for sc in ep.scenes:
        for sh in sc.shots:
            writer.writerow([
                # 表述给人看：镜号从 1 数。库里 episode.json 仍是 0 基，
                # 想让两者一致的人可以去看 json —— 表格是拿去开会的
                sc.scene_id, sh.order + 1, sh.framing, f"{sh.duration:.2f}",
                sh.action, sh.end_action, sh.dialogue,
                "、".join(sh.characters or []),
                sh.image_prompt, sh.last_frame, sh.video_prompt,
                sh.negative_prompt, sh.video_negative,
            ])
    return buf.getvalue()


def cmd_split(args: argparse.Namespace) -> int:
    """这镜该不该拆、怎么拆。

    只出方案，**不改** episode.json：拆镜要新建 shot、重排 order、决定哪一段
    用哪张参考图，任何一步由脚本替你做都可能把已经调好的分镜搅乱。所以这里
    只回答一个问题："一镜两个人在我只有一张参考图的时候，怎么拍。"
    """
    from .core import consistency
    from .stages.stage02_storyboard import characters_on_screen

    load_env()
    store = open_store(args)
    ep = store.get_episode(args.run_id)
    if ep is None:
        print(f"没有分镜记录：{args.run_id}（先跑 script 和 storyboard）")
        return 1
    # 上限必须问 provider，不能写死 1：换一家支持多张参考图的模型，这套方案
    # 就该自动变成"不用拆"。
    bundle = registry.build_bundle()
    max_refs = int(getattr(caps_of(bundle.image), "max_ref_images", 1) or 1)

    targets = [sh for sh in ep.shots if not args.shot or sh.label == args.shot]
    if args.shot and not targets:
        print(f"没有这个镜头：{args.shot}")
        return 1
    plans = []
    for shot in targets:
        on_screen = characters_on_screen(ep, shot)
        plan = consistency.split_plan(ep, shot, on_screen, max_refs)
        if not plan and not args.all:
            continue
        plans.append((shot, on_screen, plan))

    if args.json:
        print_json([{"shot": shot.label,
                     "characters": [c.name for c in on_screen],
                     "max_ref_images": max_refs,
                     "plan": [seg.as_dict() for seg in plan]}
                    for shot, on_screen, plan in plans])
        return 0
    if not plans:
        print(f"没有需要拆的镜头（单镜最多 {max_refs} 张参考图，出镜人数都没超）")
        return 0
    for shot, on_screen, plan in plans:
        print(f"== {shot.label} · {shot.framing} · {shot.duration:.1f}s · "
              f"出镜 {'、'.join(c.name for c in on_screen)}")
        for line in consistency.explain(ep, shot, on_screen, max_refs):
            print(f"   {line}")
        print(consistency.plan_text(plan))
        print()
    print("拆完把新的分镜写进 episode.json（或让剧本模型重写这一场），")
    print("再 resume --stages storyboard,frames 就能出图 —— 这一步不替你做。")
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
    if args.format == "csv":
        text = shots_csv(ep)
        # Excel 只认带 BOM 的 UTF-8：不带的话中文全是乱码，
        # 而这个文件的第一用途就是被人双击打开
        data = "\ufeff" + text
        if args.out:
            Path(args.out).write_text(data, encoding="utf-8", newline="")
            print(f"已写出 {args.out}（{len(ep.shots)} 镜，Excel 可直接打开）")
        else:
            sys.stdout.write(data)
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


def cmd_assets(args: argparse.Namespace) -> int:
    """资产台账：这次 run 产出了什么、都是谁生成的。

    `--check` 是这一层的重点：台账和磁盘**对不上就要报**。两种情况分开报，
    因为它们说明的是完全不同的事 ——
      * `磁盘上有、台账里没有`：有产物绕过了台账，于是"这东西哪来的"从此没答案；
      * `台账里有、磁盘上没有`：记录在说谎，按它找文件会找到空气。
    它**只报不改**（不自动补记录）：自动补上只会把"漏记账的产出点"永远藏起来。
    """
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    rows = store.list_assets(args.run_id or "", kind=args.kind or "")
    if not rows:
        where = args.run_id or "任何 run"
        print(f"台账里没有 {where} 的资产记录。")
        if not args.run_id:
            print("（老 run 没有台账：这一层是后加的，重跑一次才有。）")
        return 1 if args.run_id else 0

    if args.check:
        return _assets_check(args, store, root, rows)

    if args.json:
        print_json([r.to_row() for r in rows])
        return 0

    if args.run_id:
        summary = store.asset_summary(args.run_id)
        print(f"run {args.run_id}  资产 {summary['total']} 件 / "
              f"{summary['bytes'] / 1024 / 1024:.1f} MB / 血缘边 {summary['edges']} 条")
        for kind, info in summary["by_kind"].items():
            print(f"  {kind:<10} {info['count']:>4} 件  {info['bytes'] / 1024:>9.1f} KB"
                  f"   {assetslib.KIND_DESC.get(kind, '')}")
        print("")
    for row in rows:
        print(f"  {row.asset_id}  {row.describe()}")
    return 0


def _assets_check(args: argparse.Namespace, store, root: Path, rows: List[Any]) -> int:
    """对账。返回码：0 = 一致，2 = 有差异（和 qc 的分法一致，脚本里能直接用）。"""
    runs = sorted({r.run_id for r in rows if r.run_id})
    bad = 0
    for run_id in runs:
        drift = assetslib.reconcile(
            root, run_id, store.list_assets(run_id),
            paths.run_dir(root, run_id),
        )
        head = f"run {run_id}：{drift.summary()}"
        if drift.ok:
            print(f"[OK] {head}")
            continue
        bad += 1
        print(f"[X]  {head}")
        if drift.missing:
            print(f"     台账里有、磁盘上不在（{len(drift.missing)} 个）：")
            for rel in drift.missing[:20]:
                print(f"       - {rel}")
            if len(drift.missing) > 20:
                print(f"       …还有 {len(drift.missing) - 20} 个")
        if drift.unrecorded:
            print(f"     磁盘上有、台账里没有（{len(drift.unrecorded)} 个）：")
            for rel in drift.unrecorded[:20]:
                print(f"       - {rel}")
            if len(drift.unrecorded) > 20:
                print(f"       …还有 {len(drift.unrecorded) - 20} 个")
    if not bad:
        print("\n台账与磁盘一致。")
        return 0
    print(f"\n{bad} 个 run 的台账和磁盘对不上 —— 这正是这一层存在的意义。")
    return 2


def cmd_asset(args: argparse.Namespace) -> int:
    """一件资产的前因后果。

    往上追是"它是由什么变来的"（定妆照 → 关键帧 → 片段 → 成片），
    往下追是"它被用在了哪里"。两个方向都要能看 ——
    只看上游回答不了"我改这张定妆照会波及多少东西"。
    """
    load_env()
    store = open_store(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    item = store.get_asset(args.asset_id)
    if item is None:
        print(f"台账里没有这个 asset_id：{args.asset_id}")
        print("提示：`python run.py assets [run_id]` 可以看到 id 列表。")
        return 1

    parents = assetslib.parents_of(store.asset_edges(item.asset_id), item.asset_id)
    children = assetslib.children_of(store.asset_edges(item.asset_id), item.asset_id)

    if args.json:
        print_json({
            "asset": item.to_row(),
            "parents": parents,
            "children": children,
        })
        return 0

    print(f"{item.asset_id}  {item.kind}")
    print(f"  路径      {item.rel}")
    path = assetslib.resolve(root, item.rel)
    exists = path.exists()
    print(f"  磁盘      {'在' if exists else '**不在**（记录还在，文件没了）'}")
    if item.size:
        print(f"  大小      {item.size} B")
    if item.sha256:
        print(f"  sha256    {item.sha256[:16]}…")
    if item.stage:
        print(f"  阶段      {item.stage}")
    if item.provider or item.model:
        print(f"  provider  {item.provider or '(未记录)'}"
              f"{'  model ' + item.model if item.model else ''}")
    if item.prompt:
        brief = item.prompt.replace("\n", " ")
        print(f"  提示词    {brief[:120]}{'…' if len(brief) > 120 else ''}")
        print(f"  提示词指纹 {item.prompt_hash}")
    if item.meta:
        print(f"  备注      {json.dumps(item.meta, ensure_ascii=False)}")

    # 同一个内容出现在别处吗（同一张图被两个 run 用过 / 复制过）
    if item.sha256:
        twins = store.assets_sharing_content(item.sha256, exclude=item.asset_id)
        if twins:
            print(f"  同内容的还有 {len(twins)} 条记录：")
            for t in twins[:5]:
                print(f"    {t.asset_id}  {t.run_id}  {t.rel}")

    if parents:
        print("  上游（它是由什么变来的）：")
        for pid, rel in parents:
            p = store.get_asset(pid)
            label = p.describe() if p else f"{pid}（记录已不在）"
            print(f"    <- [{rel}] {label}")
    if children:
        print("  下游（它被用在了哪里）：")
        for cid, rel in children:
            c = store.get_asset(cid)
            label = c.describe() if c else f"{cid}（记录已不在）"
            print(f"    -> [{rel}] {label}")
    if not parents and not children:
        print("  没有任何血缘边（它是这一层的起点，或者记它的时候上游还没进台账）")

    if args.tree:
        tree = assetslib.lineage(
            {a.asset_id: a for a in store.list_assets(item.run_id)},
            store.asset_edges(), item.asset_id,
        )
        if len(tree) <= 1:
            print("\n  没有更早的来路（它本来就是起点，或者记它的时候上游还没进台账）")
        else:
            print("\n  来路（缩进一层 = 更早一步）：")
            for node in tree:
                if node.depth == 0:
                    print(f"    {node.asset.describe()}")
                else:
                    tail = "（同上，这里不再展开）" if node.repeat else ""
                    print(f"    {'  ' * node.depth}<- [{node.rel}] "
                          f"{node.asset.describe()}{tail}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """只读 Web 界面。

    刻意**没有**"在网页上开始生成"这个功能：花钱的那条路只留一条
    （`run` / `resume`），它带着预算闸门、断点续跑和 job_id 台账。
    网页再加一个入口，这些就得重新想一遍 —— 谁按的、按了几次、上一次那个
    还在跑吗。这里补的是"能看见"，不是"能跑"。
    """
    from .web.app import serve

    load_env()
    root = _root_of(args)
    db = Path(args.db) if getattr(args, "db", None) else None
    return serve(
        root, db,
        host=args.host, port=int(args.port),
        limit=int(args.limit), quiet=not args.verbose,
        allow_remote=bool(args.allow_remote), open_browser=bool(args.open),
    )


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
        spend = f"花费 ${rep['money']:.4f}"
        sim = float(rep.get("simulated") or 0.0)
        if sim > 0:
            # dryrun 的价是编的。不标出来，只产提示词的人会以为这一跑真花了钱。
            spend += f"（其中 ${sim:.4f} 为占位估算）"
        print(
            f"  {rep['stage']:<11} 处理 {rep['processed']:<4} 跳过 {rep['skipped']:<4} "
            f"失败 {rep['failed']:<3} {spend}"
        )
        for note in rep.get("notes", []):
            print(f"      - {note}")
    cost = result.get("cost") or {}
    if cost:
        real = float(cost.get("real_money") or 0.0)
        sim = float(cost.get("simulated_money") or 0.0)
        if sim > 0:
            print(f"\n实际花费 ${real:.4f}（另有 ${sim:.4f} 为占位估算，没有真的扣费）")
        else:
            print(f"\n总计花费 ${real:.4f}")
    final = result.get("final_video")
    # 路径是 Pipeline 里的"预期位置"，不保证文件真在 —— 只跑了
    # script+storyboard 时它照样存在这个字段。打印一个不存在的文件比不打印更糟。
    if final and Path(final).exists():
        print(f"成片：{final}")
    elif not result.get("failed_stage"):
        # 提示词模式：一个阶段都没失败，只是这次没请求出片。说清楚，
        # 否则"没有成片"看着就像出了事。
        print("本次没有跑出片阶段 —— 分镜和提示词到这儿就算做完了。")


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
    sp.add_argument("--target", type=float, default=0.0, metavar="SECONDS",
                    help="整部目标时长（秒），均摊到每个镜头。例：--target 60。"
                         "台词读不完的镜头会突破上限并在报告里写出差值")
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
    sp.add_argument("--aspect", default="", metavar="W:H",
                    help="画幅：9:16（默认，竖屏）/ 16:9 / 1:1 / 4:5，也可写 1280x720。"
                         "模型不支持时当场报错，不会偷偷按竖屏渲")
    add_common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("prompts", help="一句话 → 提示词包（JSON；只跑剧本+分镜）")
    sp.add_argument("--idea", required=True, help="一句话想法")
    sp.add_argument("--style", default="", help="视觉风格（可选）")
    sp.add_argument("--scenes", type=int, default=2, help="场次数（默认 2）")
    sp.add_argument("--shots", type=int, default=2, help="每场镜头数（默认 2）")
    sp.add_argument("--voice", default="female_01", help="配音音色（不影响提示词）")
    sp.add_argument("--project", default="", metavar="PROJECT_ID",
                    help="归到某部剧下，复用角色与定妆照")
    sp.add_argument("--json", default="", metavar="FILE",
                    help="写到文件；不给就打到 stdout（`-` 同 stdout）")
    sp.add_argument("--dialect", default="", choices=[""] + list(dialects.keys()),
                    metavar="PLATFORM",
                    help="按某家网页端的框整理一遍（"
                         + "/".join(dialects.keys()) + "）；不给就是通用写法")
    sp.add_argument("--variants", type=int, default=1, metavar="N",
                    help="一次出 N 个不同调度的变体，挑一个（默认 1）")
    sp.add_argument("--budget", type=float, default=0.0)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--aspect", default="", metavar="W:H")
    add_common(sp)
    sp.set_defaults(func=cmd_prompts)

    sp = sub.add_parser("mcp", help="MCP 服务器（stdio）：让 agent 直接调提示词")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("resume", help="断点续跑")
    sp.add_argument("run_id")
    sp.add_argument("--stages", help="只跑指定阶段，逗号分隔")
    sp.add_argument("--budget", type=float, default=0.0)
    sp.add_argument("--concurrency", type=int, default=3)
    sp.add_argument("--force", action="store_true")
    sp.add_argument(
        "--only-shot", action="append", default=[], metavar="sc01/shot_000",
        help="只重跑这几镜（可重复）。网页上「重跑这一镜」用的就是它",
    )
    sp.add_argument(
        "--from-json", action="store_true", dest="from_json",
        help="先把 run 目录里的 episode.json 读回库再跑：手改过台词/时长/景别之后用它",
    )
    sp.add_argument("--no-strict-qc", action="store_true")
    sp.add_argument("--no-subtitles", action="store_true")
    sp.add_argument("--aspect", default="", metavar="W:H",
                    help="一般不用写：画幅记在 run 上，resume 会沿用原样。"
                         "只有老 run（这个字段还不存在时建的）才需要在这里补")
    add_common(sp)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("status", help="逐镜进度")
    sp.add_argument("run_id")
    add_common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("split", help="一镜两个人怎么办：拆镜方案（只出方案，不改分镜）")
    sp.add_argument("run_id")
    sp.add_argument("--shot", help="只看某一镜（如 sc01/shot_000）；不给就看全部超员的")
    sp.add_argument("--all", action="store_true", help="连不需要拆的镜头也列出来")
    sp.add_argument("--json", action="store_true", help="机器可读")
    add_common(sp)
    sp.set_defaults(func=cmd_split)

    sp = sub.add_parser("shots", help="导出分镜表")
    sp.add_argument("run_id")
    sp.add_argument("--format", choices=["md", "json", "csv"], default="md",
                    help="md（默认）/ json（原始）/ csv（表格，Excel 双击能开）")
    sp.add_argument("--out", help="输出文件（不给就打到 stdout）")
    add_common(sp)
    sp.set_defaults(func=cmd_shots)

    sp = sub.add_parser("cost", help="成本明细")
    sp.add_argument("run_id")
    add_common(sp)
    sp.set_defaults(func=cmd_cost)

    sp = sub.add_parser("assets", help="资产台账：产出了什么、谁生成的（--check 对账）")
    sp.add_argument("run_id", nargs="?", default="",
                    help="不给就列出全部 run 的资产")
    sp.add_argument("--kind", help="只看某一类：" + " / ".join(assetslib.KINDS))
    sp.add_argument("--check", action="store_true",
                    help="把台账和磁盘比一遍（0=一致 2=对不上），只报不改")
    sp.add_argument("--json", action="store_true")
    add_common(sp)
    sp.set_defaults(func=cmd_assets)

    sp = sub.add_parser("asset", help="一件资产的前因后果（血缘）")
    sp.add_argument("asset_id")
    sp.add_argument("--tree", action="store_true", help="一路追到根")
    sp.add_argument("--json", action="store_true")
    add_common(sp)
    sp.set_defaults(func=cmd_asset)

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

    sp = sub.add_parser("web", help="只读 Web 界面：分镜 / 时间轴 / 质检 / 成本 / 血缘")
    add_common(sp)
    sp.add_argument("--host", default="127.0.0.1",
                    help="绑定的地址（默认 127.0.0.1；别的地址要 --allow-remote）")
    sp.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    sp.add_argument("--open", action="store_true", help="起来后自动打开浏览器")
    sp.add_argument("--allow-remote", action="store_true",
                    help="允许绑到非回环地址 —— 同一网络的人都能看到这些片子")
    sp.add_argument("--limit", type=int, default=50, help="侧栏列多少条 run")
    sp.add_argument("-v", "--verbose", action="store_true", help="打印每个请求")
    sp.set_defaults(func=cmd_web)

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
