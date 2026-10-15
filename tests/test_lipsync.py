"""口型同步（阶段 6）的测试。

这里最要紧的一组断言不是"dryrun 能不能出一个文件"，而是
**骨架有没有拦住一个坏掉的适配器**：

  真厂商的返回是黑盒，适配器作者很容易写出"时长变了"或"带上了音轨"的实现，
  而这两种错都不会报异常 —— 一个让全片声音错位，一个让每句台词说两遍。
  契约如果只写在文档里，就等于没有。所以下面用几个**故意写坏的 provider**
  来验证骨架这一侧真的会拒收。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_drama.core import media, paths
from ai_drama.core.context import Ledger, StageContext
from ai_drama.core.models import Episode, Run, Scene, Shot
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.qc import QcChecker, QcRules, QcState
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.base import LipSyncProvider
from ai_drama.stages import stage06_lipsync as lipsync_stage

# ———————————————————————————————————————————————— 构造

def _shot(order: int, *, dialogue: str = "你到底是谁", scene_id: str = "sc01") -> Shot:
    return Shot(scene_id=scene_id, order=order, action=f"动作 {order}", dialogue=dialogue)


def _episode(*shots: Shot) -> Episode:
    ep = Episode(run_id="run_test", idea="测试", title="测试")
    ep.scenes = [Scene(scene_id="sc01", title="第一场", summary="", shots=list(shots))]
    return ep


def _ctx(tmp_path: Path, ep: Episode, provider, *, force: bool = False,
         drop_lipsync: bool = False) -> StageContext:
    store = Store(tmp_path / "runs" / "t.db")
    run = Run(run_id="run_test", idea="测试", out_dir=str(tmp_path))
    store.create_run(run)
    store.save_episode(ep)
    store.sync_shots("run_test", ep)
    bundle = registry.build_bundle(lipsync=provider)
    if drop_lipsync:
        # 注意 build_bundle(lipsync=None) 不等于"关掉" —— overrides 里的 None
        # 意思是"没有覆盖"，于是又回到环境变量/dryrun 默认值。
        # 要测"没配 provider"这一条，得在 bundle 上直接抹掉。
        bundle.lipsync = None
    return StageContext(
        run=run, episode=ep, store=store, bundle=bundle, root=tmp_path,
        ledger=Ledger(store, "run_test"), force=force, concurrency=2, verbose=False,
    )


def _make_clip(tmp_path: Path, ep: Episode, shot: Shot, *, seconds: float = 2.0,
               need_ffmpeg=None) -> Path:
    """造一个真的能解码的片段，让 ffprobe 量得出时长。"""
    img = tmp_path / f"k{shot.order}.png"
    from PIL import Image

    Image.new("RGB", (64, 96), (30, 40, 90)).save(img)
    dest = paths.clip(tmp_path, ep.run_id, shot.scene_id, shot.order)
    media.image_to_clip(img, dest, duration=seconds, size=(64, 96))
    return dest


class _Spy(LipSyncProvider):
    """记录每次调用，产出无音轨、时长不变的副本。"""

    name = "spy"

    def __init__(self, *, seconds: float | None = None, write: bool = True):
        self.calls: list[str] = []
        self.seconds = seconds          # 非 None 时故意改时长（用它测契约）
        self.write = write

    def sync(self, video, audio, dest, *, shot_label="", scene=""):
        self.calls.append(shot_label)
        if not self.write:
            return {"unit": "second", "amount": 0.0, "money": 0.0, "provider": self.name}
        from PIL import Image

        img = video.parent / "spy.png"
        Image.new("RGB", (64, 96), (10, 10, 10)).save(img)
        media.image_to_clip(img, dest, duration=self.seconds or 2.0, size=(64, 96))
        return {"unit": "second", "amount": 2.0, "money": 0.01,
                "provider": self.name, "stage": "lipsync", "shot_label": shot_label}


# ———————————————————————————————————————————————— 契约与注册

def test_lipsync_is_a_provider_kind_of_its_own():
    """它不能是 VideoProvider 的子类：输入输出都是片段，跟"生成"是两件事。"""
    from ai_drama.providers.base import VideoProvider

    assert "lipsync" in registry.available()
    assert not issubclass(LipSyncProvider, VideoProvider)
    assert not issubclass(VideoProvider, LipSyncProvider)


@pytest.mark.ffmpeg
def test_dryrun_reports_that_it_did_not_sync(tmp_path, need_ffmpeg):
    """dryrun 必须自己说清楚它没改嘴型 —— 报告是唯一能说这话的地方。"""
    ep = _episode(_shot(0))
    clip = _make_clip(tmp_path, ep, _shot(0))
    voice = tmp_path / "v.wav"
    media.write_wav(voice, seconds=2.0, freq=200.0, silent=True)

    provider = registry.build_bundle().lipsync
    assert provider.name == "dryrun-lipsync"
    usage = provider.sync(clip, voice, tmp_path / "out.mp4", shot_label="sc01/shot_000")

    assert usage["provider"] == "dryrun-lipsync"
    assert usage["stage"] == "lipsync"
    assert "没有真的改嘴型" in usage["note"]


def test_lipsync_can_be_turned_off_explicitly(monkeypatch):
    monkeypatch.setenv("AI_DRAMA_LIPSYNC", "none")
    assert registry.build_bundle().lipsync is None


def test_default_is_dryrun(monkeypatch):
    monkeypatch.delenv("AI_DRAMA_LIPSYNC", raising=False)
    assert registry.build_bundle().lipsync.name == "dryrun-lipsync"


# ———————————————————————————————————————————————— 阶段行为

@pytest.mark.ffmpeg
def test_only_shots_with_dialogue_are_sent(tmp_path, need_ffmpeg):
    ep = _episode(_shot(0), _shot(1), _shot(2, dialogue=""))
    for s in ep.shots:
        _make_clip(tmp_path, ep, s)
    spy = _Spy()
    report = lipsync_stage.run(_ctx(tmp_path, ep, spy))

    # 顺序不确定（阶段是并发的），比集合不比列表
    assert sorted(spy.calls) == ["sc01/shot_000", "sc01/shot_001"]
    assert report.processed == 2
    assert "没有可对的嘴" not in " ".join(report.notes)   # 还有两镜要同步


@pytest.mark.ffmpeg
def test_dryrun_output_has_no_audio_track_and_keeps_duration(tmp_path, need_ffmpeg):
    ep = _episode(_shot(0))
    _make_clip(tmp_path, ep, _shot(0), seconds=2.0)
    report = lipsync_stage.run(_ctx(tmp_path, ep, registry.build_bundle().lipsync))

    assert report.processed == 1
    out = paths.synced(tmp_path, ep.run_id, "sc01", 0)
    assert out.exists() and out.stat().st_size > 1000
    assert abs(media.probe_duration(out) - 2.0) < 0.12
    # 契约 1：不能带音轨（带了的话成片里每句台词说两遍）
    assert media.find_ffprobe() is not None
    import subprocess

    txt = subprocess.run(
        [media.find_ffprobe(), "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    ).stdout.decode()
    assert "audio" not in txt
    assert "video" in txt


@pytest.mark.ffmpeg
def test_a_provider_that_changes_the_duration_is_rejected(tmp_path, need_ffmpeg):
    """契约 2 的核心测试：坏适配器不能被放行。

    时长变了会让后面所有镜头的声音错位，而且错得很安静 ——
    所以这件事必须由**骨架**拦，不能指望每个适配器都记得。
    """
    ep = _episode(_shot(0))
    _make_clip(tmp_path, ep, _shot(0), seconds=2.0)
    spy = _Spy(seconds=4.0)                       # 故意产出 4 秒
    report = lipsync_stage.run(_ctx(tmp_path, ep, spy))

    assert spy.calls == ["sc01/shot_000"]         # 确实调了
    assert report.processed == 0
    assert report.failed == 1
    assert any("时长" in n for n in report.notes)
    # 关键：坏产物**不能**留在正式位置上
    assert not paths.synced(tmp_path, ep.run_id, "sc01", 0).exists()


@pytest.mark.ffmpeg
def test_a_provider_that_writes_nothing_is_rejected(tmp_path, need_ffmpeg):
    ep = _episode(_shot(0))
    _make_clip(tmp_path, ep, _shot(0))
    report = lipsync_stage.run(_ctx(tmp_path, ep, _Spy(write=False)))

    assert report.failed == 1
    assert any("没有写出文件" in n for n in report.notes)
    assert not paths.synced(tmp_path, ep.run_id, "sc01", 0).exists()


@pytest.mark.ffmpeg
def test_second_run_skips_and_does_not_call_the_provider_again(tmp_path, need_ffmpeg):
    ep = _episode(_shot(0))
    _make_clip(tmp_path, ep, _shot(0))
    spy = _Spy()
    ctx = _ctx(tmp_path, ep, spy)

    lipsync_stage.run(ctx)
    assert len(spy.calls) == 1
    report = lipsync_stage.run(ctx)

    assert report.skipped == 1 and report.processed == 0
    assert len(spy.calls) == 1                  # 没有重复付费
    # 跳过时也要把路径记回镜头 —— 否则质检读到的 shot.synced 是空的
    assert ep.shots[0].synced and Path(ep.shots[0].synced).exists()


@pytest.mark.ffmpeg
def test_missing_provider_is_loud_not_silent(tmp_path, need_ffmpeg):
    """没配 provider 是合法的，但必须说出来。"""
    ep = _episode(_shot(0))
    _make_clip(tmp_path, ep, _shot(0))
    report = lipsync_stage.run(_ctx(tmp_path, ep, None, drop_lipsync=True))

    assert report.skipped == 1
    joined = " ".join(report.notes)
    assert "[!]" in joined and "AI_DRAMA_LIPSYNC" in joined
    assert "嘴型没有对齐" in joined


# ———————————————————————————————————————————————— 拼接与质检

@pytest.mark.ffmpeg
def test_concat_prefers_the_synced_clip(tmp_path, need_ffmpeg):
    """对完嘴的片段必须真的被用上 —— 否则整个阶段白做。"""
    from ai_drama.stages.stage07_concat import _clip_for

    ep = _episode(_shot(0))
    clip = _make_clip(tmp_path, ep, _shot(0))
    ctx = _ctx(tmp_path, ep, _Spy())
    assert _clip_for(ctx, ep.shots[0]) == clip

    synced = paths.synced(tmp_path, ep.run_id, "sc01", 0)
    synced.write_bytes(b"x" * 100)
    assert _clip_for(ctx, ep.shots[0]) == synced


def test_qc_requires_a_synced_file_only_when_asked(tmp_path):
    ep = _episode(_shot(0))

    # 默认不查：口型同步是可选增强，默认打开会让没接 provider 的人全 fail
    assert not QcRules().require_lipsync
    report = QcChecker(QcRules(), assets=lambda s: (True, "")).check(ep)
    assert not [f for f in report.findings if f.rule == "lipsync-present"]

    # 打开之后：没产物就是 block
    rules = QcRules(require_lipsync=True)
    report = QcChecker(rules, assets=lambda s: (True, "")).check(ep)
    hits = [f for f in report.findings if f.rule == "lipsync-present"]
    assert len(hits) == 1 and hits[0].severity.value == "block"
    assert report.state is QcState.FAIL

    # 产物在磁盘上就通过
    out = tmp_path / "shot_000_synced.mp4"
    out.write_bytes(b"x" * 100)
    ep.shots[0].synced = str(out)
    report = QcChecker(rules, assets=lambda s: (True, "")).check(ep)
    assert not [f for f in report.findings if f.rule == "lipsync-present"]
    assert report.state is QcState.PASS


def test_qc_ignores_shots_without_dialogue(tmp_path):
    """空镜本来就不需要同步，不该被算成缺失。"""
    ep = _episode(_shot(0, dialogue=""))
    report = QcChecker(QcRules(require_lipsync=True),
                       assets=lambda s: (True, "")).check(ep)
    assert not [f for f in report.findings if f.rule == "lipsync-present"]


# ———————————————————————————————————————————————— 端到端

@pytest.mark.ffmpeg
def test_full_pipeline_runs_the_lipsync_stage(workspace, need_ffmpeg):
    pipe = Pipeline(workspace, Store(workspace / "runs" / "t.sqlite3"),
                    registry.build_bundle(), concurrency=2, verbose=False)
    run = pipe.start("深夜加班回家发现门锁被换过", scenes=1, shots_per_scene=3)
    result = pipe.run(run)

    assert result["state"] == "done", result
    stages = {r["stage"]: r for r in result["reports"]}
    assert "lipsync" in stages
    # dryrun 下它必须自陈"没有真的改嘴型"
    assert any("没有真的改嘴型" in n for n in stages["lipsync"]["notes"])

    ep = pipe.store.get_episode(run.run_id)
    talking = [s for s in ep.shots if s.dialogue.strip()]
    assert talking, "dryrun 应该至少产出一句台词"
    for shot in talking:
        assert shot.synced, f"{shot.label} 没有同步产物"
        assert Path(shot.synced).exists()
