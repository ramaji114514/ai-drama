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
from .store import Store


class Pipeline:
    def __init__(self, root: Path, store: Store, bundle, *, budget_usd: float = 0.0,
                 concurrency: int = 3, force: bool = False, verbose: bool = True):
        self.root = Path(root)
        self.store = store
        self.bundle = bundle
        self.budget_usd = budget_usd
        self.concurrency = concurrency
        self.force = force
        self.verbose = verbose

    # ————————————————————————————————————--- 启动

    def start(self, idea: str, **meta: Any) -> Run:
        run_id = new_id("run")
        out_dir = paths.run_dir(self.root, run_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        run = Run(run_id=run_id, idea=idea, state="pending", stage="script",
                  out_dir=str(out_dir), meta=meta)
        self.store.create_run(run)
        return run

    # ————————————————————————————————————--- 执行

    def run(self, run: Run, *, stages: Optional[List[str]] = None) -> Dict[str, Any]:
        from ..stages import BY_NAME

        todo = stages or STAGES
        ledger = Ledger(self.store, run.run_id, self.budget_usd)
        reports: List[StageReport] = []

        episode = self.store.get_episode(run.run_id) or Episode(
            run_id=run.run_id, idea=run.idea
        )

        ctx = StageContext(
            run=run, episode=episode, store=self.store, bundle=self.bundle,
            root=self.root, ledger=ledger, force=self.force,
            concurrency=self.concurrency, verbose=self.verbose,
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
        state = "done" if (final.exists() and final.stat().st_size > 0) else "failed"
        self.store.update_run(
            run.run_id,
            state=state,
            final_video=str(final) if final.exists() else None,
            error=None if state == "done" else "流水线结束但没有产出 final_video",
        )
        if self.verbose:
            print(f"\n=== 完成 ===\n状态: {state}\n成片: {final}\n花费: ${cost['total_money']:.4f}")
        return {
            "run_id": run.run_id,
            "state": state,
            "final_video": str(final) if final.exists() else None,
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
