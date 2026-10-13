"""StageContext — 每个阶段拿到的唯一依赖包。

阶段函数签名统一为：

    def run(ctx: StageContext) -> StageReport

这样任何阶段都可以被单独调用、单独重跑、单独测试（见 tests/）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import CostEntry, Episode, Run
from .store import Store
from ..providers.base import Bundle, BudgetExceeded, ProviderError


@dataclass
class StageReport:
    stage: str
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    money: float = 0.0
    notes: List[str] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "money": round(self.money, 6),
            "notes": self.notes,
            "artifacts": self.artifacts,
        }


class Ledger:
    """成本记账 + 预算闸门。

    预算超限时**抛异常**，不是静默降级。这是刻意的：
    一次跑了几百块的活儿，你宁可它停下来报警，也不要它偷偷用假数据糊完。
    """

    def __init__(self, store: Store, run_id: str, budget_usd: float = 0.0):
        self.store = store
        self.run_id = run_id
        self.budget_usd = float(budget_usd or 0.0)
        self.spent = 0.0

    def add(self, usage: Dict[str, Any], *, stage: str = "", shot_label: str = "") -> None:
        money = float(usage.get("money") or 0.0)
        entry = CostEntry(
            provider=str(usage.get("provider") or "unknown"),
            stage=str(usage.get("stage") or stage or "unknown"),
            unit=str(usage.get("unit") or "call"),
            amount=float(usage.get("amount") or 0.0),
            money=money,
            run_id=self.run_id,
            shot_label=shot_label or str(usage.get("shot_label") or ""),
        )
        self.store.add_cost(entry)
        self.spent += money
        if self.budget_usd > 0 and self.spent > self.budget_usd:
            raise BudgetExceeded(self.spent, self.budget_usd)

    def summary(self) -> Dict[str, Any]:
        return self.store.cost_summary(self.run_id)


@dataclass
class StageContext:
    run: Run
    episode: Episode
    store: Store
    bundle: Bundle
    root: Path
    ledger: Ledger
    force: bool = False                    # 忽略缓存，全部重跑
    concurrency: int = 3
    verbose: bool = True

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def heal_shot(self, shot, exc: Exception) -> None:
        """把失败写进 store，**保留现场**，绝不静默吞掉。

        对照参考项目的反模式：出错时返回假数据 -> 你永远不知道它坏过。
        """
        reason = f"{type(exc).__name__}: {exc}"
        self.store.update_shot(shot.shot_id, state="failed", error=reason[:800])


__all__ = ["StageContext", "StageReport", "Ledger", "BudgetExceeded", "ProviderError"]
