"""Provider 能力声明与镜头拆分。

这一组测试守的是骨架接真厂商后**第一个会踩的坑**：

    创作侧说"这个镜头 12 秒"，模型说"我一次最多 8 秒"。

如果不处理，你会拿到一段**不报错**的、只演完前 2/3 的视频。
所以这里同时断言三件事：
  1. provider 必须能声明上限（caps）；
  2. 超出上限时是"拆"而不是"截断"；
  3. provider 收到超限请求时要**抛错**，绝不静默截断。
"""

from __future__ import annotations

import pytest

from ai_drama.core.media import estimate_shot_seconds, plan_shot_split
from ai_drama.core.models import Shot
from ai_drama.providers import registry
from ai_drama.providers.base import (
    DEFAULT_CAPS,
    ProviderCaps,
    ProviderError,
    VideoProvider,
    caps_of,
)


def _shot(**kw) -> Shot:
    base = dict(scene_id="sc01", order=0, duration=4.0)
    base.update(kw)
    return Shot(**base)


# ————————————————————————————————————————---- 能力声明


def test_dryrun_declares_a_video_cap():
    """dryrun 也要声明上限 —— 它是你唯一能在花钱之前发现冲突的地方。"""
    bundle = registry.build_bundle()
    caps = caps_of(bundle.video)
    assert caps.max_duration is not None
    assert caps.max_duration > 0
    assert caps.describe()


def test_provider_without_caps_is_unlimited():
    """没声明 caps 的适配器不能因此被当成"上限 0"。

    把上限定低会**静默改变你片子的节奏** —— 比不限制更糟，
    所以这里是 fail-open 而不是 fail-closed。
    """

    class Legacy(VideoProvider):
        name = "legacy"

        def submit(self, request):        # pragma: no cover - 不会被调用
            return "x"

        def poll(self, job_id, *, timeout=300.0):   # pragma: no cover
            return "failed", {}, None

        def fetch(self, job_id, dest):    # pragma: no cover
            return {}

    # 一个"写坏了"的适配器：caps 被赋成了别的东西
    Legacy.caps = None                    # type: ignore[assignment]
    assert caps_of(Legacy()) is DEFAULT_CAPS
    assert caps_of(Legacy()).max_duration is None


def test_caps_reports_provider_size():
    caps = ProviderCaps(width=1080, height=1920, fps=30, max_duration=5.0)
    assert caps.size == (1080, 1920)
    assert "1080x1920" in caps.describe()
    assert "5" in caps.describe()


# ————————————————————————————————————————---- 拆分


def test_shot_within_cap_is_not_split():
    assert plan_shot_split(4.0, max_duration=8.0) == [4.0]


def test_shot_over_cap_is_split_evenly():
    """12 秒 / 上限 8 秒 -> 两段 6 秒，而不是 8+4。

    均分而不是"先塞满"，是因为先塞满会让最后一段只剩 4 秒、
    再极端一点就会切出 0.5 秒的碎片（在成片里就是一道闪光）。
    """
    parts = plan_shot_split(12.0, max_duration=8.0)
    assert len(parts) == 2
    assert parts == [6.0, 6.0]


def test_split_never_loses_time():
    """拆分不能改变总时长 —— 否则成片会比分镜表短，而没人知道为什么。"""
    for total in (1.0, 3.7, 8.0, 8.01, 12.0, 25.4, 60.0):
        parts = plan_shot_split(total, max_duration=8.0)
        assert abs(sum(parts) - total) < 1e-6, (total, parts)


def test_split_respects_the_cap():
    for total in (9.0, 17.5, 40.0):
        parts = plan_shot_split(total, max_duration=8.0)
        assert all(p <= 8.0 + 1e-9 for p in parts), (total, parts)


def test_split_avoids_sub_second_fragments_when_it_can():
    """上限很小时宁可少切一段，也不要生成一串 0.3 秒的碎片。"""
    parts = plan_shot_split(5.0, max_duration=0.4, min_duration=1.0)
    assert all(p >= 1.0 - 1e-9 for p in parts), parts


def test_no_cap_means_no_split():
    assert plan_shot_split(45.0, max_duration=None) == [45.0]


def test_zero_duration_yields_no_parts():
    assert plan_shot_split(0.0, max_duration=8.0) == []


# ————————————————————————————————————————---- 创作侧与能力侧必须分开


def test_estimate_is_not_clamped_by_the_model_cap():
    """estimate_shot_seconds 的默认片长是**创作侧**的量，不是模型能力上限。

    如果这两件事被混在一个数字里，你就再也分不清"这场戏该拍多久"
    和"这个模型能生成多久" —— 换一家模型会莫名其妙改变你的节奏。

    [!] 这条用例的语义在"默认 15 秒"那次改动里变过：
    以前 8 秒是**上限**（再长的台词也被夹到 8 秒），现在是**下限**
    （再短的镜头也至少 15 秒，而长台词可以超过 15 秒 —— 把一句 40 秒
    的话剪成 15 秒等于把话说一半）。所以这里断言的是"台词说了算"，
    以及"要不要拆"仍然只由 provider 的上限决定。
    """
    long_speech = "你" * 200
    d = estimate_shot_seconds(_shot(dialogue=long_speech))
    assert d > 40.0, "40 秒的台词不该被默认片长剪断"
    # 而"要不要拆"由 provider 说了算，与上面那个数字无关：
    # 每段都不超过 3 秒，且总和一分不丢。
    parts = plan_shot_split(d, max_duration=3.0)
    assert all(p <= 3.0 + 1e-6 for p in parts)
    assert abs(sum(parts) - d) < 1e-6


# ————————————————————————————————————————---- provider 必须拒收超限请求


def test_provider_refuses_oversized_request_instead_of_truncating(workspace):
    """超限必须抛错。

    这是本组最重要的一条：静默截断会让成片莫名其妙地短、而且无法归因 ——
    你只会看到"12 秒的镜头出来只有 8 秒"，却不知道是谁吃掉的。
    """
    bundle = registry.build_bundle()
    cap = caps_of(bundle.video).max_duration
    assert cap is not None
    with pytest.raises(ProviderError) as ei:
        bundle.video.submit({
            "prompt": "测试", "duration": cap + 5.0,
            "dest": str(workspace / "too_long.mp4"), "shot_label": "sc01/shot_000",
        })
    assert ei.value.retryable is False      # 重试没有意义，参数就是错的


def test_provider_accepts_request_at_the_cap(workspace):
    bundle = registry.build_bundle()
    cap = caps_of(bundle.video).max_duration
    job = bundle.video.submit({
        "prompt": "测试", "duration": cap,
        "dest": str(workspace / "at_cap.mp4"), "shot_label": "sc01/shot_000",
    })
    assert job.startswith("dryjob_")
