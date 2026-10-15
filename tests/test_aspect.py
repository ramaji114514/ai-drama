"""画幅：一个数字，两个调用点，零个报错机会。

这条功能的全部难点不在于"支持 16:9"，而在于**别让两个地方各算一遍尺寸**：

    stage03_frames  用 ctx.frame_size 出图
    stage04_video   把 caps.size 送进请求体
    dryrun 的图生视频 拿请求里的 size 去 image_to_clip()
    字幕             从**成片**量尺寸（所以它自动跟着走）

只要有一处写死 720x1280，出来的片子就会在某一步换尺寸，而**没有任何一步
会报错**——ffmpeg 会老老实实帮你拼出一个尺寸不一致的片子。
所以这里既测"能出横屏"，也测"两处同源"。
"""

from __future__ import annotations

import pytest

from ai_drama.core import media, paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.base import ProviderCaps, VideoProvider


def test_aspects_are_named_and_they_are_pixels():
    assert media.frame_size("9:16") == (720, 1280)
    assert media.frame_size("16:9") == (1280, 720)
    assert media.frame_size("1:1") == (720, 720)


def test_a_pixel_size_is_accepted_too():
    """写 `1280x720` 和写 `16:9` 是同一个意思 —— 两种都很自然。"""
    assert media.normalize_aspect("1280x720") == "16:9"
    assert media.normalize_aspect("1280×720") == "16:9"


def test_an_unknown_aspect_raises_instead_of_falling_back():
    """猜错画幅要等到看成片才发现 —— 所以这里必须当场炸。"""
    for bad in ("16:10", "竖屏", "9/16", ""):
        with pytest.raises(ValueError):
            media.normalize_aspect(bad)


class _PickAVideo(VideoProvider):
    """只声明支持竖屏的假 provider，用来验"不支持就停下"。

    三个抽象方法都写成"不该被调用到"：这条测试只关心 `Pipeline` 在**花任何钱
    之前**就把不支持的画幅拦下来。
    """

    name = "only-vertical"

    def __init__(self, caps: ProviderCaps) -> None:
        self.caps = caps

    def submit(self, request):                 # pragma: no cover - 用不到
        raise NotImplementedError

    def poll(self, job_id, *, timeout: float = 300.0):   # pragma: no cover
        raise NotImplementedError

    def fetch(self, job_id, dest):             # pragma: no cover - 用不到
        raise NotImplementedError


def _pipe(workspace, **kw) -> Pipeline:
    store = Store(workspace / "runs" / "t.sqlite3")
    bundle = registry.build_bundle()
    return Pipeline(workspace, store, bundle, verbose=False, **kw)


def test_the_size_lands_on_the_video_provider_caps(workspace):
    """stage04 送的是 `caps.size` —— 所以画幅必须落到 caps 上，不是别处。"""
    pipe = _pipe(workspace, aspect="16:9")
    assert pipe.frame_size == (1280, 720)
    assert pipe.bundle.video.caps.size == (1280, 720)
    assert pipe.bundle.video.caps.width == 1280


def test_a_provider_that_cannot_do_it_says_so_up_front(workspace):
    """provider 声明支持哪些画幅，就只做哪些。

    悄悄按竖屏渲一部横屏片，比直接报错糟得多 —— 后者只是今天跑不成，
    前者是钱花了、片子也"成功"了、要等人看出来。
    """
    store = Store(workspace / "runs" / "t.sqlite3")
    bundle = registry.build_bundle()
    bundle.video = _PickAVideo(ProviderCaps(aspect_ratios=("9:16",)))
    with pytest.raises(ValueError) as ei:
        Pipeline(workspace, store, bundle, verbose=False, aspect="16:9")
    assert "16:9" in str(ei.value) and "only-vertical" in str(ei.value)

    # 它支持的画幅当然要能用
    Pipeline(workspace, store, bundle, verbose=False, aspect="9:16")


@pytest.mark.ffmpeg
def test_every_step_of_a_landscape_run_is_landscape(workspace, need_ffmpeg):
    """真正的验收：出图、片段、成片**三处尺寸一致**。

    只钉住其中一处是没有意义的 —— 漂移正是发生在两处之间。
    """
    pipe = _pipe(workspace, aspect="16:9")
    run = pipe.start("末班地铁上捡到一部还在响的手机", scenes=1, shots_per_scene=2)
    result = pipe.run(run)
    assert result["state"] == "done", result

    assert pipe.frame_size == (1280, 720)
    ep = pipe.store.get_episode(run.run_id)
    for shot in ep.shots:
        kf = paths.keyframe(workspace, run.run_id, shot.scene_id, shot.order)
        assert media.probe_size(kf) == (1280, 720), f"关键帧尺寸漂了：{kf}"
        clip = paths.clip(workspace, run.run_id, shot.scene_id, shot.order)
        assert media.probe_size(clip) == (1280, 720), f"片段尺寸漂了：{clip}"
    assert media.probe_size(paths.episode_video(workspace, run.run_id)) == (1280, 720)

    # 字幕是按**成片**量出来的尺寸排的，所以横屏下它自动跟着走
    assert (run.meta or {}).get("aspect") == "16:9"


@pytest.mark.ffmpeg
def test_a_resume_keeps_the_runs_own_aspect(workspace, need_ffmpeg):
    """画幅是**这条片子的属性**，不是这次命令行的属性。

    补渲一个镜头时得到一块不同尺寸的画面，是那种"跑完了、片子也生成了、
    只是内容不对"的失败 —— 没有一步会报错。
    """
    pipe = _pipe(workspace, aspect="1:1")
    run = pipe.start("末班地铁上捡到一部还在响的手机", scenes=1, shots_per_scene=2)
    pipe.run(run, stages=["script", "storyboard", "frames"])

    # 换一个**默认竖屏**的 Pipeline 来 resume：结果必须是方形
    other = _pipe(workspace, aspect="9:16")
    assert other.frame_size == (720, 1280)
    other.resume(run.run_id, stages=["frames"])

    assert other.frame_size == (720, 720), "resume 用了命令行的画幅而不是 run 的"
    ep = other.store.get_episode(run.run_id)
    for shot in ep.shots:
        kf = paths.keyframe(workspace, run.run_id, shot.scene_id, shot.order)
        assert media.probe_size(kf) == (720, 720)
