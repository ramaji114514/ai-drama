"""流水线编排。

这里只有一件事：**按顺序跑阶段，每一步都落盘，失败就停**。

刻意不做的事：
  * 不做 DAG、不引 LangGraph/CrewAI。六个阶段是线性的，
    真正的复杂度在于"每一步可恢复"，而不在于编排花样。
  * 不吞异常。任何阶段抛错，run 状态立刻写成 failed 并保留栈信息。
  * 不自动降级。素材缺失时宁可停下来让你知道，也不要产出一部你看不出来的废片。
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .context import Ledger, StageContext, StageReport
from .models import STAGES, Episode, Run, new_id
from .project import Project, load as load_project
from .store import Store


class Pipeline:
    def __init__(self, root: Path, store: Store, bundle, *, budget_usd: float = 0.0,
                 concurrency: int = 3, force: bool = False, verbose: bool = True,
                 strict_qc: bool = True, subtitles: bool = True,
                 project_id: Optional[str] = None):
        self.root = Path(root)
        self.store = store
        self.bundle = bundle
        self.budget_usd = budget_usd
        self.concurrency = concurrency
        self.force = force
        self.verbose = verbose
        self.strict_qc = strict_qc
        self.subtitles = subtitles
        #: 这一步跑的是哪部剧（None = 单集模式，角色每集重新生成）。
        self.project_id = project_id

    # ————————————————————————————————————--- 启动

    def start(self, idea: str, project_id: Optional[str] = None, **meta: Any) -> Run:
        pid = project_id or self.project_id
        if pid:
            meta["project_id"] = pid
            # 集号在**建 run 之前**算：episode_no() 数的是库里已有的 run，
            # 建完再算会把自己也算进去，于是第 1 集显示成"第 2 集"。
            meta.setdefault("episode_no", self.episode_no(pid))
        run_id = new_id("run")
        out_dir = paths.run_dir(self.root, run_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        run = Run(run_id=run_id, idea=idea, state="pending", stage="script",
                  out_dir=str(out_dir), meta=meta)
        self.store.create_run(run)
        return run

    def episode_no(self, project_id: str) -> int:
        """这部剧已经跑过几次 -> 这一次是第几集。

        从 runs 表数出来而不是在 project.json 里记一个计数器：
        计数器会跟真实 run 列表不一致（失败重跑、手工删 run），
        而"第几集"只是个显示用的编号，不值得为它引入一个会失真的状态。
        """
        n = 0
        for r in self.store.list_runs():
            if (r.meta or {}).get("project_id") == project_id:
                n += 1
        return n + 1

    # ————————————————————————————————————--- 执行

    def run(self, run: Run, *, stages: Optional[List[str]] = None) -> Dict[str, Any]:
        from ..stages import BY_NAME

        todo = stages or STAGES
        ledger = Ledger(self.store, run.run_id, self.budget_usd)
        reports: List[StageReport] = []

        episode = self.store.get_episode(run.run_id) or Episode(
            run_id=run.run_id, idea=run.idea
        )

        # 这部剧的角色圣经。**读坏了要让整条流水线停下**，不能静默降级成
        # "每集重新造角色" —— 那样第 2 集就换脸，而且要跑到成片才看得出来。
        # 没有文件（单集模式）是正常用法，load() 返回 None。
        pid = (run.meta or {}).get("project_id") or self.project_id
        project: Optional[Project] = None
        if pid:
            project = load_project(self.root, pid)
            if project is None:
                raise FileNotFoundError(
                    f"project 不存在：{pid}。"
                    f"（{paths.project_json(self.root, pid)} 没找到）"
                    f"要么先 `run.py project new`，要么去掉 --project。"
                )

        ctx = StageContext(
            run=run, episode=episode, store=self.store, bundle=self.bundle,
            root=self.root, ledger=ledger, force=self.force,
            concurrency=self.concurrency, verbose=self.verbose,
            strict_qc=self.strict_qc, subtitles=self.subtitles,
            project=project,
        )

        self.store.update_run(run.run_id, state="running")

        for stage in todo:
            fn = BY_NAME.get(stage)
            if fn is None:
                raise KeyError(f"unknown stage: {stage}")
            self.store.update_run(run.run_id, stage=stage)
            ctx.log(f"\n=== [{stage}] ===")
            try:
                rep = fn(ctx)
            except Exception as exc:                      # noqa: BLE001
                tb = traceback.format_exc()
                self.store.update_run(
                    run.run_id, state="failed", error=f"{type(exc).__name__}: {exc}",
                )
                if self.verbose:
                    print(f"[{stage}] [X] 阶段失败：{type(exc).__name__}: {exc}")
                    print(tb)
                return {
                    "run_id": run.run_id,
                    "state": "failed",
                    "failed_stage": stage,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": tb,
                    "reports": [r.to_dict() for r in reports],
                    "cost": ledger.summary(),
                }
            reports.append(rep)
            # 每跑完一个阶段就把 episode 刷回数据库，
            # 这样进程被 Ctrl-C 掉之后 resume 仍然能拿到最新分镜
            if ctx.episode.scenes:
                self.store.save_episode(ctx.episode)
                self.store.sync_shots(run.run_id, ctx.episode)

        final = paths.episode_video(self.root, run.run_id)
        cost = ledger.summary()
        (paths.cost_report(self.root, run.run_id)).write_text(
            json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ———————————————————————— 质检闸门
        # 三态：pass / fail / unchecked。**unchecked 不通过** ——
        # "没查成"和"查过没问题"在最终状态里绝对不能长得一样，
        # 否则一次 ffprobe 缺失就能让整批片子带着未知问题被当成合格品交付。
        has_video = final.exists() and final.stat().st_size > 0
        qc_state = ctx.qc_report.state.value if ctx.qc_report is not None else ""
        qc_ok = (qc_state == "pass") or not qc_state
        blocked = self.strict_qc and has_video and not qc_ok

        if blocked:
            state = "failed"
            error = (
                f"质检未通过（{qc_state}），严格模式已开启。"
                f"详见 {paths.qc_report(self.root, run.run_id)}"
            )
        else:
            state = "done" if has_video else "failed"
            error = None if has_video else "流水线结束但没有产出 final_video"
            if has_video and not qc_ok:
                error = f"已放行，但质检结论是 {qc_state}（严格模式已关闭）"

        self.store.update_run(
            run.run_id,
            state=state,
            final_video=str(final) if final.exists() else None,
            error=error,
        )
        if self.verbose:
            print(f"\n=== 完成 ===\n状态: {state}\n成片: {final}\n花费: ${cost['total_money']:.4f}")
            if qc_state:
                print(f"质检: {qc_state.upper()}")
            if error:
                print(f"说明: {error}")
        return {
            "run_id": run.run_id,
            "state": state,
            "final_video": str(final) if final.exists() else None,
            "qc_state": qc_state,
            "error": error,
            "reports": [r.to_dict() for r in reports],
            "cost": cost,
        }

    # ————————————————————————————————————--- 续跑

    def resume(self, run_id: str, *, stages: Optional[List[str]] = None) -> Dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        run.state = "running"
        return self.run(run, stages=stages)
