"""StageContext — 每个阶段拿到的唯一依赖包。

阶段函数签名统一为：

    def run(ctx: StageContext) -> StageReport

这样任何阶段都可以被单独调用、单独重跑、单独测试（见 tests/）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    #: 质检阶段专用：pass / fail / unchecked。**unchecked 不算通过。**
    #: 放在 StageReport 上而不是只在 qc_report.json 里，是为了让
    #: "这次到底查成没查成"在每一层汇总里都不会被丢掉。
    qc_state: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "money": round(self.money, 6),
            "notes": self.notes,
            "artifacts": self.artifacts,
            "qc_state": self.qc_state,
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
    #: 严格质检：非 PASS 的 run 不被标记为完成。
    #: 默认开 —— "已经交付了但没有证据证明它合格"是最危险的状态。
    strict_qc: bool = True
    #: 质检阶段把结论挂在这里，pipeline 收尾时据此决定状态
    qc_report: Optional[Any] = None
    #: 把字幕烧进画面。默认开 —— 竖屏短剧大量是静音刷的，没有字幕等于没台词。
    #: 关掉不等于没有字幕：SRT 照样写出来，只是不烙在画面上。
    subtitles: bool = True
    #: 这部剧的角色圣经（None = 单集模式，角色由分镜阶段的 LLM 每集重新发明）。
    #: 阶段不该自己去读 project.json：读一次、传进来，才能保证同一次 run 里
    #: 所有阶段看到的是同一份设定。
    project: Optional[Any] = None
    #: 本 run 的画面像素尺寸，由 `Pipeline` 按 `run.meta["aspect"]` 算好传进来。
    #: 出图（stage02/stage03）用它；视频请求用的是 `caps.size`，两者由
    #: Pipeline 从**同一个**画幅推导，所以不会各说一套。
    frame_size: Tuple[int, int] = (720, 1280)

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def save_project(self) -> None:
        """把圣经写回磁盘（新角色入册、定妆照路径更新）。

        没有项目时是 no-op —— 单集模式不该凭空造出一个项目目录来。
        """
        if self.project is not None:
            self.project.save(self.root)

    def heal_shot(self, shot, exc: Exception) -> None:
        """把失败写进 store，**保留现场**，绝不静默吞掉。

        对照参考项目的反模式：出错时返回假数据 -> 你永远不知道它坏过。
        """
        reason = f"{type(exc).__name__}: {exc}"
        self.store.update_shot(shot.shot_id, state="failed", error=reason[:800])


__all__ = ["StageContext", "StageReport", "Ledger", "BudgetExceeded", "ProviderError"]
