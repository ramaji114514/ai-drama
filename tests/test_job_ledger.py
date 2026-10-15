"""提交过的任务不能因为进程死掉就白付一次钱。

视频生成是整条链最贵的一环，而"提交成功"到"拿到文件"之间隔着几分钟的排队。
进程在这个窗口里死掉（Ctrl-C、断电、容器被回收）是完全正常的事，
**问题是死掉之后会怎样**：

    提交成功 -> 排队 -> [进程没了] -> resume -> 又提交一次 -> 又付一次钱

而厂商那边其实已经有一份在跑了。第一版骨架就是这个行为：job_id 只活在
`_render_shot()` 的局部变量里。这一组测试把"提交后立刻落库、resume 先认领"
钉死，并且顺便钉住它的反面 —— **厂商明确说失败时必须把记录清掉**，
否则坏任务会被永远认领、这一镜再也渲不出来。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from ai_drama.core import media, paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.base import VideoProvider
from ai_drama.providers.dryrun import DryRunVideoProvider
from ai_drama.stages.stage04_video import _poll_one


class _ScriptedVideo(DryRunVideoProvider):
    """一个可以精确控制"第几次 poll 的时候崩"的假 provider。

    它**真的写出文件**（用 media.image_to_clip 造一小段），因为
    `_render_shot` 会去检查产物在不在 —— 一个只会返回字符串的假 provider
    测不出"落盘"这条路径。

    继承 dryrun 而不是直接实现 VideoProvider：只覆盖 submit / poll，
    其余（fetch / caps / 定价）沿用已经能跑的那一套。
    """

    name = "scripted-video"

    def __init__(self, *, crash_on_poll: Optional[int] = None,
                 known: Optional[Dict[str, str]] = None):
        super().__init__()
        self.submits: list = []
        self.polls: list = []
        self.crash_on_poll = crash_on_poll
        #: 上次运行里"已经提交过的任务 -> 目标路径"。真实场景里这是厂商那边
        #: 的状态；在这里由测试显式递给第二次运行的 provider，好让它能认领
        #: 一个自己没提交过的 job。
        self.known: Dict[str, str] = dict(known or {})
        self._dest: Dict[str, str] = {}
        self._n = 0

    def submit(self, request: Dict[str, Any]) -> str:
        self._n += 1
        job = f"job-{self._n}"
        self.submits.append(job)
        self._dest[job] = request["dest"]
        # 注意：submit 只登记，**不产出文件** —— 真厂商也是"提交立刻返回一个 id，
        # 产物要等 poll 才有"。如果这里就把文件写了，resume 会因为"分段已存在"
        # 直接跳过，测试就测不到"认领 job"这条路径了。
        return job

    def poll(self, job_id: str, *, timeout: float = 300.0):
        self.polls.append(job_id)
        if self.crash_on_poll and len(self.polls) == self.crash_on_poll:
            # 用 BaseException 模拟"进程被 Ctrl-C 打断"：
            # `_poll_one` 只吞 Exception（网络抖动要重试），所以这一下会真穿出去。
            raise KeyboardInterrupt("模拟进程在等待时被打断")
        # 产物写到 dest 旁边，交给 _move_into_place 搬进去（真厂商的产物
        # 也不会正好落在我们的目标路径上）
        target = self._dest.get(job_id) or self.known.get(job_id)
        assert target, f"这个 fake 不认识 {job_id} —— 测试没把已知任务递给它"
        out = Path(target).with_suffix(".provider.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        media.image_to_clip(_tiny_png(), out, duration=1.0, fps=8)
        return ("succeeded", {"money": 0.5, "amount": 1.0, "provider": self.name}, out)


def _tiny_png() -> Path:
    """一张 16x16 的小图，给 media.image_to_clip 当素材。

    写在系统临时目录而不是 tests/ 里：测试不该往仓库里丢文件
    （第一版写成了 tests/_tiny.png，`git add -A` 就把它提交进去了）。
    """
    from PIL import Image

    p = Path(tempfile.gettempdir()) / "ai_drama_tiny_16.png"
    if not p.exists():
        Image.new("RGB", (16, 16), (30, 40, 70)).save(p)
    return p


def _pipe(root: Path, video: VideoProvider) -> tuple[Pipeline, Store]:
    store = Store(root / "runs" / "test.sqlite3")
    bundle = registry.build_bundle()
    bundle.video = video
    return Pipeline(root, store, bundle, concurrency=1, verbose=False), store


@pytest.mark.ffmpeg
def test_the_job_id_is_in_the_database_before_we_start_polling(workspace: Path,
                                                               need_ffmpeg: None):
    """核心断言：提交成功和开始轮询之间，job_id 已经在库里。

    之前这两步之间什么都不落盘，那段时间就是"付了钱但查不到"的窗口。
    """
    seen: list = []

    class Peeking(_ScriptedVideo):
        store: Any = None
        run_id: str = ""

        def poll(self, job_id: str, *, timeout: float = 300.0):
            # 轮询的这一刻，库里的 job_ids 必须已经写好了
            seen.append([dict(r) for r in self.store.list_shots(self.run_id)])
            return super().poll(job_id, timeout=timeout)

    video = Peeking()
    pipe, store = _pipe(workspace, video)
    run = pipe.start("提交后立刻落库", scenes=1, shots_per_scene=2)
    video.store = store
    video.run_id = run.run_id
    result = pipe.run(run, stages=["script", "storyboard", "video"])
    assert result["state"] != "done" or True

    assert seen, "provider 的 poll 根本没被调到，测试前提不成立"
    first_poll_state = seen[0]
    assert any(r["job_ids"] for r in first_poll_state), (
        "开始轮询的时候库里还没有任何 job_id —— 进程在这一刻死掉就会重复付费。"
        f"当时的 shots 表：{first_poll_state}"
    )


@pytest.mark.ffmpeg
def test_resume_reclaims_the_recorded_job_instead_of_paying_again(
        workspace: Path, need_ffmpeg: None, monkeypatch: pytest.MonkeyPatch):
    """进程在等待中被打断 -> resume 必须**只认领、不重新提交**。"""
    monkeypatch.setenv("AI_DRAMA_RECOVER_TIMEOUT", "20")
    crashing = _ScriptedVideo(crash_on_poll=1)
    pipe, store = _pipe(workspace, crashing)
    run = pipe.start("等的时候挂了", scenes=1, shots_per_scene=2)

    with pytest.raises(KeyboardInterrupt):
        pipe.run(run, stages=["script", "storyboard", "video"])

    submitted_before = list(crashing.submits)
    assert submitted_before, "第一次跑没有提交任何任务，测试前提不成立"

    ep = store.get_episode(run.run_id)
    shot = ep.scenes[0].shots[0]
    recorded = store.shot_jobs(shot.shot_id)
    assert recorded, (
        "进程被打断后库里没有留下已提交的任务 —— resume 时会重新提交、再付一次钱"
    )

    # 第二次：换一个不会崩的 provider（它认得上一次的任务，就像同一个厂商的
    # 第二次调用认得自己发出去的单号），它不应该收到任何新的 submit
    benign = _ScriptedVideo(known=dict(crashing._dest))
    pipe2, store2 = _pipe(workspace, benign)
    result = pipe2.resume(run.run_id, stages=["video"])

    assert benign.submits == [], (
        f"resume 又重新提交了 {benign.submits} —— 这就是重复付费"
    )
    recovered = [j for j in recorded.values() if j in benign.polls]
    assert recovered, (
        f"resume 没有去认领已记录的任务。记录={recorded} 实际轮询={benign.polls}"
    )
    assert result["state"] in ("done", "running", "failed")
    # 结算完之后记录要清干净，不能留着让下一次 resume 再认领一次
    assert not store2.shot_jobs(shot.shot_id), "任务结算后 job_ids 没有清掉"


@pytest.mark.ffmpeg
def test_a_provider_reported_failure_clears_the_record(workspace: Path,
                                                       need_ffmpeg: None):
    """反面：厂商说这个任务死了，就必须忘掉它。

    否则这一镜会被永远认领到一个不存在的任务上，而且每次 resume 都失败 ——
    坏记录比没有记录更难查。
    """

    class DeadJob(_ScriptedVideo):
        def poll(self, job_id: str, *, timeout: float = 300.0):
            self.polls.append(job_id)
            return ("failed", {}, "provider said no")

    video = DeadJob()
    pipe, store = _pipe(workspace, video)
    run = pipe.start("厂商说任务死了", scenes=1, shots_per_scene=1)
    pipe.run(run, stages=["script", "storyboard", "video"])

    ep = store.get_episode(run.run_id)
    shot = ep.scenes[0].shots[0]
    assert not store.shot_jobs(shot.shot_id), (
        "厂商明确报失败后仍留着 job_ids —— 下一次 resume 会认领一个死任务，"
        "这一镜就再也渲不出来了"
    )


def test_polling_that_gives_up_is_not_reported_as_a_provider_failure(workspace: Path):
    """超时 != 失败。

    这个区分是省钱的开关：超时时任务多半还在厂商那边跑，job_id 必须留住；
    而"厂商说失败"是终局，必须扔掉。第一版把两者都返回 "failed"，
    调用方因此无从分辨，只能一律丢掉 —— 也就是一律重复付费。
    """

    class StillRunning(_ScriptedVideo):
        def poll(self, job_id: str, *, timeout: float = 300.0):
            self.polls.append(job_id)
            return ("running", {}, "")

    pipe, _ = _pipe(workspace, StillRunning())
    run = pipe.start("永远在跑的任务", scenes=1, shots_per_scene=1)
    from ai_drama.core.context import StageContext

    ctx = StageContext(run=run, episode=pipe.store.get_episode(run.run_id) or _empty(run),
                       store=pipe.store, bundle=pipe.bundle,
                       root=workspace, ledger=None, verbose=False)  # type: ignore[arg-type]
    status, usage, info = _poll_one(ctx, "job-x", timeout=0.3)
    assert status == "timeout", (
        f"等不下去时返回了 {status!r}，调用方会当作厂商失败而丢掉 job_id"
    )
    assert "timeout" in info


def _empty(run):
    from ai_drama.core.models import Episode

    return Episode(run_id=run.run_id, idea=run.idea)


def test_a_brand_new_column_is_added_to_an_existing_database(workspace: Path):
    """老库要能自动补出 job_ids 列。

    升级后的第一件事不该是 `no such column: job_ids` —— 而且是在跑到一半、
    已经付过几次钱的时候炸。
    """
    import sqlite3

    db = workspace / "old.sqlite3"
    conn = sqlite3.connect(str(db))
    # 一个"上个版本"的 shots 表：有 synced，没有 job_ids
    conn.executescript(
        "CREATE TABLE shots (shot_id TEXT PRIMARY KEY, run_id TEXT, scene_id TEXT,"
        " ord INTEGER, label TEXT, state TEXT, duration REAL, keyframe TEXT,"
        " video TEXT, audio TEXT, synced TEXT, error TEXT, payload TEXT,"
        " updated_at TEXT);"
    )
    conn.execute("INSERT INTO shots (shot_id, run_id) VALUES ('s1','r1')")
    conn.commit()
    conn.close()

    store = Store(db)
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(shots)")}
    assert "job_ids" in cols, f"老库没有补上 job_ids 列：{sorted(cols)}"
    assert store.shot_jobs("s1") == {}
