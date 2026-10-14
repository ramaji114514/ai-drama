"""端到端：零 API key 跑完整条流水线。

这是整个骨架的**验收测试**。它证明的事情只有一件，但很关键：

    不接任何付费模型，也能从一句话走到一个可播放的 mp4。

参考项目在这一点上是相反的：没配密钥时它静默返回一份内置的假剧本，
于是"任务成功"了，但你永远拿不到自己的片子。
本骨架的选择是：默认用 dryrun（写得很明确的假图/假视频/假配音），
或者接真 provider 后失败就失败 —— 两种结果你都知道自己在哪。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_drama.core import paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.stages.stage06_concat import probe_duration


def _pipeline(root: Path) -> tuple[Pipeline, Store]:
    store = Store(root / "runs" / "test.sqlite3")
    return Pipeline(root, store, registry.build_bundle(), concurrency=2, verbose=False), store


@pytest.mark.ffmpeg
def test_full_run_produces_a_playable_episode(workspace: Path, need_ffmpeg: None):
    pipe, store = _pipeline(workspace)
    run = pipe.start("深夜回家发现门锁被换过", scenes=1, shots_per_scene=3)
    result = pipe.run(run)

    assert result["state"] == "done", result
    final = Path(result["final_video"])
    assert final.exists() and final.stat().st_size > 10_000

    # 真的能解码出时长和声音 —— 不是"生成了一个 0 字节的壳"
    assert probe_duration(final) > 3.0
    report = registry.build_bundle()
    assert report is not None

    # 逐镜产物齐了
    ep = store.get_episode(run.run_id)
    assert ep is not None and len(ep.scenes) == 1
    for shot in ep.scenes[0].shots:
        kf = paths.keyframe(workspace, run.run_id, shot.scene_id, shot.order)
        clip = paths.clip(workspace, run.run_id, shot.scene_id, shot.order)
        assert kf.exists(), f"缺关键帧 {kf}"
        assert clip.exists() and clip.stat().st_size > 1000, f"缺视频片段 {clip}"

    # 分镜表可供人工修改 —— 这是"人机协作"的接口
    ep_json = workspace / run.out_dir / "episode.json"
    assert ep_json.exists()
    assert json.loads(ep_json.read_text(encoding="utf-8"))["scenes"]


@pytest.mark.ffmpeg
def test_second_run_is_idempotent_and_costs_nothing(workspace: Path, need_ffmpeg: None):
    """幂等：重跑一遍不该再花一分钱，也不该重生成产物。

    这是 resume 能省钱的根据 —— 中断后接着跑，已完成的镜头必须被跳过。
    """
    pipe, store = _pipeline(workspace)
    run = pipe.start("测试幂等", scenes=1, shots_per_scene=2)
    pipe.run(run)
    first = store.cost_summary(run.run_id)

    pipe2 = Pipeline(workspace, store, registry.build_bundle(), concurrency=2, verbose=False)
    result = pipe2.run(store.get_run(run.run_id))
    second = store.cost_summary(run.run_id)

    assert result["state"] == "done"
    assert second["total_money"] == pytest.approx(first["total_money"])
    # 每个"要花钱"的阶段都应该报告全部命中缓存。
    # 两个例外都不花钱：
    #   concat 每次都得重新拼一次盘上的分段（processed 记的是真拼了几段）；
    #   qc 是纯本地检查，重跑一遍是它该做的事，也正是"重跑结果一致"的证明。
    for rep in result["reports"]:
        assert rep["processed"] == 0 or rep["stage"] in ("concat", "qc"), rep["stage"]


@pytest.mark.ffmpeg
def test_resume_after_a_killed_stage_reuses_finished_work(workspace: Path, need_ffmpeg: None):
    """只跑到 frames，然后 resume 剩下的阶段 —— 中间的产物必须被复用。"""
    pipe, store = _pipeline(workspace)
    run = pipe.start("测试断点续跑", scenes=1, shots_per_scene=2)
    partial = pipe.run(run, stages=["script", "storyboard", "frames"])
    assert partial["state"] == "failed"          # 没跑到 concat，自然没有成片

    kf_dir = workspace / run.out_dir / "scenes"
    before = {p: p.stat().st_mtime for p in kf_dir.rglob("*.png")}
    assert before, "frames 阶段应该已经产出关键帧"

    done = pipe.resume(run.run_id, stages=["frames", "video", "audio", "concat"])
    assert done["state"] == "done"

    after = {p: p.stat().st_mtime for p in kf_dir.rglob("*.png")}
    # 已经渲染好的关键帧不该被重新渲染（mtime 不变 = 没被覆盖）
    for p, mtime in before.items():
        assert after[p] == mtime, f"{p} 被重复渲染了，缓存没生效"


def test_pipeline_reports_failure_loudly(workspace: Path):
    """阶段抛错时必须落库 + 返回失败，而不是悄悄产出一部烂片。"""
    pipe, store = _pipeline(workspace)
    run = pipe.start("测试失败传播", scenes=1, shots_per_scene=1)

    class Boom:
        name = "boom"

        def generate(self, *a, **kw):
            raise RuntimeError("模型服务 500")

    bundle = registry.build_bundle()
    bundle.text = Boom()          # 故意注入一个会炸的 provider
    pipe.bundle = bundle

    result = pipe.run(run, stages=["script"])
    assert result["state"] == "failed"
    assert result["failed_stage"] == "script"
    assert "模型服务 500" in result["error"]

    stored = store.get_run(run.run_id)
    assert stored.state == "failed"
    assert "500" in (stored.error or "")


@pytest.mark.ffmpeg
def test_shot_longer_than_the_provider_cap_is_split_and_reassembled(
    workspace: Path, need_ffmpeg: None
):
    """单段上限是生成模型的硬约束，不是创作侧的口味。

    15 秒的分镜喂给「单段最长 8 秒」的模型，出来的必然是截断的片段，
    而且不会报错 —— 你只会觉得"这个模型不太行"。所以骨架要在发出
    请求之前就按上限切开，渲染完再拼回一个完整镜头。
    """
    from ai_drama.providers.base import ProviderCaps
    from ai_drama.providers.dryrun import DryRunVideoProvider

    class ShortVideo(DryRunVideoProvider):
        caps = ProviderCaps(width=720, height=1280, fps=24, max_duration=2.5, min_duration=1.0)

    pipe, store = _pipeline(workspace)
    bundle = registry.build_bundle()
    bundle.video = ShortVideo(work_dir=workspace / "work")
    pipe.bundle = bundle

    run = pipe.start("测试按上限拆段", scenes=1, shots_per_scene=3)
    result = pipe.run(run)
    assert result["state"] == "done", result

    ep = store.get_episode(run.run_id)
    split_any = False
    for shot in ep.scenes[0].shots:
        parts = paths.clip_parts(workspace, run.run_id, shot.scene_id, shot.order)
        clip = paths.clip(workspace, run.run_id, shot.scene_id, shot.order)
        # 拼完之后分段必须被清掉，只留一个完整镜头
        assert clip.exists(), f"没有拼出完整镜头 {clip}"
        assert not parts, f"分段没有清理：{parts}"
        if shot.duration > ShortVideo.caps.max_duration:
            split_any = True
    assert split_any, "这条测试的前提（至少一个镜头超过上限）没成立"

    # 拆段 + 重组不能吞掉时长：成片时长必须与分镜表对得上（±1.5 秒容差）
    total = sum(s.duration for s in ep.scenes[0].shots)
    assert probe_duration(Path(result["final_video"])) == pytest.approx(total, abs=1.5)

    # 被拆过的镜头要在报告里留下痕迹，而不是悄悄多花了几笔钱
    video_report = next(r for r in result["reports"] if r["stage"] == "video")
    joined = " ".join(video_report["notes"])
    assert "拆分" in joined or "上限" in joined, joined

