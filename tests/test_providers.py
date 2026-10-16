"""Provider 抽象层 —— 全项目最该被测的一层。

为什么：骨架的全部价值就是"换厂商不用改代码"。
这个承诺如果不能被测试证明，它就只是一句 README 上的口号。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, List

import pytest

from ai_drama.providers import registry
from ai_drama.providers.base import (
    AudioProvider,
    BudgetExceeded,
    ImageProvider,
    LipSyncProvider,
    ProviderError,
    T2IProvider,
    TextProvider,
    VideoProvider,
    VisionProvider,
    call_with_retry,
)


def test_default_is_dryrun_so_the_skeleton_runs_with_zero_keys():
    """默认必须零配置可跑。否则新人第一步就卡死，骨架等于没交付。"""
    bundle = registry.build_bundle()
    assert bundle.text.name.startswith("dryrun")
    assert bundle.video.name.startswith("dryrun")


def test_defaults_are_dryrun_except_the_kinds_that_must_stay_off(monkeypatch):
    """每个形态没设环境变量时用什么，必须**只有一处**说了算。

    这条是防回归的：`t2i` 曾经默认是空字符串，于是 `doctor` 打印
    "未配置，定妆照会跳过"、而流水线其实在跑定妆照 —— 报告和现实各说一套。

    现在多一条分界线：**能生产的**形态默认 dryrun（零 key 也要能出片），
    **做检查的**形态（vision）默认关闭。理由见下面那条测试。
    """
    for kind in registry.KIND_ENV:
        monkeypatch.delenv(registry.kind_env(kind), raising=False)
        if kind in registry.DEFAULT_OFF_KINDS:
            assert registry.effective_name(kind) == "", kind
        else:
            assert registry.effective_name(kind) == "dryrun", kind

    bundle = registry.build_bundle()
    assert bundle.t2i is not None
    assert bundle.vision is None


def test_vision_is_off_by_default_and_never_fakes_a_verdict(monkeypatch):
    """视觉是**检查**形态，它的默认值必须是"关掉"，不能是 dryrun。

    一个"假装看过了"的默认检查器比没有检查器更糟：它会把"没查成"洗成
    "通过"，而 unchecked 与 pass 的区别正是质检那一层的全部意义。
    显式配上 dryrun 也只允许得到"没查成" —— DryRunVisionProvider 直接抛。
    """
    monkeypatch.delenv("AI_DRAMA_VISION", raising=False)
    assert registry.effective_name("vision") == ""
    assert registry.build_bundle().vision is None

    monkeypatch.setenv("AI_DRAMA_VISION", "dryrun")
    vision = registry.build_bundle().vision
    assert vision is not None
    assert vision.health()["ok"] is False, "dryrun 视觉不能自称可用"
    with pytest.raises(ProviderError):
        vision.chat([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}}]}])


def test_vision_can_be_turned_off_explicitly(monkeypatch):
    for off in ("none", "off", "NONE", "0", "false", " "):
        monkeypatch.setenv("AI_DRAMA_VISION", off)
        assert registry.effective_name("vision") == "", off
        assert registry.build_bundle().vision is None, off


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


def _create_with_dummy_key(kind: str, name: str, monkeypatch):
    """真厂商的构造函数会检查 key，缺了直接抛 —— 这是对的。

    但"检查接口有没有漂"不该被一个 key 挡住，也不该为此花钱。
    所以这里从报错里把**它要的那个环境变量名**读出来，塞个假值再试一次：
    接口对不上（少参数、方法名错）照样会炸，而网络调用一次都不会发生。
    """
    try:
        return registry.create(kind, name)
    except ProviderError as exc:
        missing = re.findall(r"AI_DRAMA_[A-Z0-9_]+", str(exc))
        if not missing:
            raise
        for env in missing:
            monkeypatch.setenv(env, "dummy-key-for-interface-check")
        return registry.create(kind, name)


def test_all_registered_providers_satisfy_the_interfaces(monkeypatch):
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
        "vision": VisionProvider,
        "lipsync": LipSyncProvider,
    }
    for kind, base in kinds.items():
        for name in registry.available()[kind]:
            obj = _create_with_dummy_key(kind, name, monkeypatch)
            assert isinstance(obj, base), f"{kind}/{name} 不是 {base.__name__}"


def test_providers_accept_every_keyword_their_callers_pass(monkeypatch):
    """真实现必须收得下 dryrun 收得下的全部关键字。

    这条踩过：`stage01_script.py:138` 会传 `scene_count=`，
    而 `base.TextProvider.generate()` 的抽象签名里**没有**这个参数 ——
    照着 ABC 写适配器，第一次真跑就 `TypeError`，一分钱产出都没有。
    ABC 是骨架自己写的，它漏了参数，所以"实现了 ABC"并不足以保证能跑。

    判据取 dryrun：它是默认实现，也是唯一被端到端跑通过的实现，
    调用方传什么都以它为准。
    """
    for kind in ("text", "image", "video", "audio", "t2i", "vision", "lipsync"):
        ours = _create_with_dummy_key(kind, "dashscope", monkeypatch)
        ref = registry.create(kind, "dryrun")
        for meth in _public_methods(ref):
            mine = getattr(ours, meth, None)
            assert mine is not None, f"dashscope 缺方法 {kind}.{meth}"
            # 只比"dryrun 能收的、真实现也得能收"这一个方向：
            # 真实现多几个自己的旋钮（比如 timeout）是允许的。
            allowed = set(inspect.signature(_impl_of(ref, meth)).parameters)
            missing = allowed - set(inspect.signature(_impl_of(ours, meth)).parameters)
            assert not missing, (
                f"{kind}.dashscope.{meth} 少了参数 {sorted(missing)}；"
                f"调用方按 dryrun 的签名传参，少一个就是 TypeError"
            )


def _impl_of(obj: Any, meth: str):
    """取到方法本身（不绑定实例），这样签名里不会多一个 self。"""
    return inspect.unwrap(getattr(type(obj), meth))


def _public_methods(obj: Any) -> List[str]:
    """provider 的公开方法 = 非下划线、可调用、且不是 health()。"""
    out = []
    for name in dir(type(obj)):
        if name.startswith("_") or name == "health":
            continue
        if not callable(getattr(type(obj), name)):
            continue
        out.append(name)
    return out


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
