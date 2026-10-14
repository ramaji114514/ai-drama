"""Provider 抽象层 —— 全项目最该被测的一层。

为什么：骨架的全部价值就是"换厂商不用改代码"。
这个承诺如果不能被测试证明，它就只是一句 README 上的口号。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_drama.providers import registry
from ai_drama.providers.base import (
    AudioProvider,
    BudgetExceeded,
    ImageProvider,
    ProviderError,
    T2IProvider,
    TextProvider,
    VideoProvider,
    call_with_retry,
)


def test_default_is_dryrun_so_the_skeleton_runs_with_zero_keys():
    """默认必须零配置可跑。否则新人第一步就卡死，骨架等于没交付。"""
    bundle = registry.build_bundle()
    assert bundle.text.name.startswith("dryrun")
    assert bundle.video.name.startswith("dryrun")


def test_every_kind_including_t2i_defaults_to_dryrun(monkeypatch):
    """五个形态一个都不能少。

    这条是防回归的：`t2i` 曾经默认是空字符串，于是 `doctor` 打印
    "未配置，定妆照会跳过"、而流水线其实在跑定妆照 —— 报告和现实各说一套。
    """
    for kind in registry.KIND_ENV:
        monkeypatch.delenv(registry.kind_env(kind), raising=False)
        assert registry.effective_name(kind) == "dryrun", kind
    assert registry.build_bundle().t2i is not None


def test_t2i_can_be_turned_off_explicitly(monkeypatch):
    """关掉定妆照要能关掉，而且要关得干净。"""
    for off in ("none", "off", "NONE", "0", "false", " "):
        monkeypatch.setenv("AI_DRAMA_T2I", off)
        assert registry.effective_name("t2i") == "", off
        assert registry.build_bundle().t2i is None, off


def test_an_empty_required_provider_says_what_to_do(monkeypatch):
    """`AI_DRAMA_TEXT=` 这种手滑要给出人话，而不是 KeyError('')。"""
    monkeypatch.setenv("AI_DRAMA_TEXT", "")
    with pytest.raises(ValueError, match="AI_DRAMA_TEXT"):
        registry.build_bundle()


def test_unknown_provider_fails_loudly():
    """写错名字必须炸，不能悄悄退回默认 —— 那会让你以为在用真模型。"""
    with pytest.raises(KeyError):
        registry.create("video", "no-such-vendor")


def test_all_registered_providers_satisfy_the_interfaces():
    """注册表里的每个 provider 都必须真的是对应抽象的子类。

    duck typing 在这里不够：接口漂了（比如 submit 少个参数）
    只会在跑了几十块钱之后才暴露。
    """
    kinds = {
        "text": TextProvider,
        "image": ImageProvider,
        "video": VideoProvider,
        "audio": AudioProvider,
        "t2i": T2IProvider,
    }
    for kind, base in kinds.items():
        for name in registry.available()[kind]:
            obj = registry.create(kind, name)
            assert isinstance(obj, base), f"{kind}/{name} 不是 {base.__name__}"


def test_video_provider_submit_poll_contract(workspace: Path):
    """submit 拿 id、poll 查状态 —— 换了任何厂商这条都不能变。"""
    bundle = registry.build_bundle()
    job = bundle.video.submit({"prompt": "测试", "duration": 3.0, "dest": str(workspace / "a.mp4")})
    assert isinstance(job, str) and job
    state, usage, out = bundle.video.poll(job, timeout=60)
    assert state in {"succeeded", "failed", "running"}
    assert isinstance(usage, dict)


def test_image_provider_writes_a_real_file(workspace: Path):
    """出图要真的落盘且非空 —— 只返回路径不写文件是典型的假实现。"""
    bundle = registry.build_bundle()
    dest = workspace / "kf.png"
    bundle.image.png("一个空荡荡的楼道", dest, size=(320, 568))
    assert dest.exists() and dest.stat().st_size > 1000


class _Flaky:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError("429 too many requests", retryable=True)
        return "ok"


def test_retry_recovers_from_transient_errors():
    flaky = _Flaky(fail_times=2)
    assert call_with_retry(flaky, attempts=3, base_delay=0.01) == "ok"
    assert flaky.calls == 3


def test_retry_gives_up_and_raises_the_last_error():
    flaky = _Flaky(fail_times=99)
    with pytest.raises(ProviderError):
        call_with_retry(flaky, attempts=3, base_delay=0.01)
    assert flaky.calls == 3


def test_non_retryable_errors_are_not_retried():
    """参数非法/内容违规重试只是烧钱。必须一次就抛。"""
    calls = {"n": 0}

    def bad(*a, **kw):
        calls["n"] += 1
        raise ProviderError("invalid prompt", retryable=False)

    with pytest.raises(ProviderError):
        call_with_retry(bad, attempts=5, base_delay=0.01)
    assert calls["n"] == 1


def test_budget_exceeded_is_a_provider_error_and_not_retryable():
    exc = BudgetExceeded(spent=1.5, budget=1.0)
    assert isinstance(exc, ProviderError)
    assert exc.retryable is False
