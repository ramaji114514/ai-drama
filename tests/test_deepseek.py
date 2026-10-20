"""DeepSeek 适配器 —— 只用假 HTTP，一次网络调用都不发生。

这一层是**唯一会花钱**的代码，所以凡是能离线钉住的（错误翻译、重试判定、
计费口径、请求体形状）全在这里钉住。真调模型的验证另说，且只用一条。
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Dict, List

import pytest

from ai_drama.providers import deepseek as ds
from ai_drama.providers import registry
from ai_drama.providers.base import ProviderError

KEY = "AI_DRAMA_DEEPSEEK_API_KEY"

REPLY = {
    "id": "chatcmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"title":"雨夜"}'},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
              "prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 800},
}


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(KEY, "sk-fake-for-tests")
    for k in ("AI_DRAMA_DEEPSEEK_MODEL", "AI_DRAMA_DEEPSEEK_BASE_URL",
              "AI_DRAMA_DEEPSEEK_PRICE_IN", "AI_DRAMA_DEEPSEEK_PRICE_OUT",
              "AI_DRAMA_DEEPSEEK_PRICE_CACHE"):
        monkeypatch.delenv(k, raising=False)
    return "sk-fake-for-tests"


class _Resp:
    def __init__(self, payload: Any):
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: Dict[str, Any] | None = None):
    return urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions", code, "boom", {},  # type: ignore[arg-type]
        io.BytesIO(json.dumps(body or {"error": {"message": "boom"}}).encode()))


def _say(monkeypatch, payload: Any, seen: Dict[str, Any] | None = None):
    def fake_urlopen(req, timeout=None):
        if seen is not None:
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(payload)
    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)


# ————————————————————————————————————————————————————— 注册与"不骗人"


def test_it_registers_as_the_text_kind_only():
    """只接管写剧本这一件事 —— 别的形态仍然归各家自己的适配器。"""
    avail = registry.available()
    assert "deepseek" in avail["text"]
    for kind in ("image", "t2i", "video", "audio", "lipsync", "vision"):
        assert "deepseek" not in avail[kind], kind


def test_missing_key_fails_loudly_instead_of_faking(keyed, monkeypatch):
    """缺 key 必须报错，不能悄悄退回假数据：假装成功比失败贵得多。"""
    monkeypatch.delenv(KEY, raising=False)
    with pytest.raises(ProviderError) as err:
        registry.create("text", "deepseek")
    assert err.value.retryable is False
    assert KEY in str(err.value), "报错里得写清缺的是哪个环境变量"


def test_health_never_touches_the_network(keyed, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("health() 不该发网络请求")

    monkeypatch.setattr(ds.urllib.request, "urlopen", boom)
    info = registry.create("text", "deepseek").health()
    assert info["ok"] is True
    assert info["model"] == "deepseek-chat"


# ————————————————————————————————————————————————————— 请求形状


def test_it_sends_the_openai_compatible_request_it_promises(keyed, monkeypatch):
    seen: Dict[str, Any] = {}
    _say(monkeypatch, REPLY, seen)
    text, _ = registry.create("text", "deepseek").generate(
        "写个短剧", system="短剧编剧引擎")
    assert text == '{"title":"雨夜"}'
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == "Bearer sk-fake-for-tests"
    assert seen["body"]["model"] == "deepseek-chat"
    assert seen["body"]["stream"] is False, "流式会让这里拿不到完整 JSON"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "短剧编剧引擎"},
        {"role": "user", "content": "写个短剧"},
    ]


def test_generate_accepts_every_keyword_its_caller_passes(keyed, monkeypatch):
    """`stage01_script.py:143` 一定会传 scene_count / shots_per_scene。

    base.py 的抽象签名里没有它们 —— 照着 ABC 写就必炸。这条是钉那个坑。
    """
    _say(monkeypatch, REPLY)
    text, usage = registry.create("text", "deepseek").generate(
        "p", system="s", max_tokens=1234, scene="script",
        scene_count=3, shots_per_scene=2)
    assert text and usage["stage"] == "script"


def test_base_url_and_model_are_configurable(keyed, monkeypatch):
    """厂商换域名 / 换模型名不该改代码。"""
    monkeypatch.setenv("AI_DRAMA_DEEPSEEK_BASE_URL", "https://example.invalid/v1/")
    monkeypatch.setenv("AI_DRAMA_DEEPSEEK_MODEL", "deepseek-reasoner")
    seen: Dict[str, Any] = {}
    _say(monkeypatch, REPLY, seen)
    client = registry.create("text", "deepseek")
    client.generate("p")
    assert seen["url"] == "https://example.invalid/v1/chat/completions"
    assert seen["body"]["model"] == "deepseek-reasoner"


# ————————————————————————————————————————————————————— 计费


def test_billing_splits_cache_hits_from_misses(keyed, monkeypatch):
    """写剧本时 system 和角色圣经反复出现，缓存命中率不低。

    全按未命中价算会把账记贵 —— 而这是唯一会花钱的一层，账必须准。
    """
    _say(monkeypatch, REPLY)
    _, usage = registry.create("text", "deepseek").generate("p")
    assert usage["tokens_in"] == 1000
    assert usage["tokens_out"] == 500
    assert usage["tokens_cached"] == 200
    assert usage["unit"] == "token"
    assert usage["provider"] == "deepseek"
    expect = (200 / 1000 * 0.00007) + (800 / 1000 * 0.00027) + (500 / 1000 * 0.0011)
    assert usage["money"] == pytest.approx(round(expect, 6))


def test_a_cache_hit_really_is_cheaper_than_a_miss(keyed, monkeypatch):
    """同一份用量，命中越多钱越少 —— 拆分真的生效了，不只是记了个数。"""
    def run(hit: int) -> float:
        payload = json.loads(json.dumps(REPLY))
        payload["usage"]["prompt_cache_hit_tokens"] = hit
        payload["usage"]["prompt_cache_miss_tokens"] = 1000 - hit
        _say(monkeypatch, payload)
        _, usage = registry.create("text", "deepseek").generate("p")
        return usage["money"]

    assert run(1000) < run(0)


def test_missing_cache_fields_are_billed_as_plain_input(keyed, monkeypatch):
    """没有缓存字段时宁可记贵，不要记便宜。"""
    payload = json.loads(json.dumps(REPLY))
    payload["usage"].pop("prompt_cache_hit_tokens")
    payload["usage"].pop("prompt_cache_miss_tokens")
    _say(monkeypatch, payload)
    _, usage = registry.create("text", "deepseek").generate("p")
    assert usage["tokens_cached"] == 0
    expect = (1000 / 1000 * 0.00027) + (500 / 1000 * 0.0011)
    assert usage["money"] == pytest.approx(round(expect, 6))


def test_prices_are_overridable_and_bad_values_are_refused(keyed, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_DEEPSEEK_PRICE_OUT", "0.0022")
    assert registry.create("text", "deepseek").price_out == pytest.approx(0.0022)
    monkeypatch.setenv("AI_DRAMA_DEEPSEEK_PRICE_OUT", "2.2e-3元")
    with pytest.raises(ProviderError) as err:
        registry.create("text", "deepseek")
    assert "不是数字" in str(err.value)


# ————————————————————————————————————————————————————— 错误翻译与重试


def test_a_bad_key_is_not_retried(keyed, monkeypatch):
    """401/402 重试只是浪费时间；余额不足时还会刷屏。"""
    def fake(req, timeout=None):
        raise _http_error(401, {"error": {"message": "invalid api key"}})

    monkeypatch.setattr(ds.urllib.request, "urlopen", fake)
    with pytest.raises(ProviderError) as err:
        registry.create("text", "deepseek").generate("p")
    assert err.value.retryable is False
    assert "401" in str(err.value)
    assert "invalid api key" in str(err.value), "厂商原话要带出来，别自己编"


def test_rate_limit_is_retried_and_then_succeeds(keyed, monkeypatch):
    """429 是常态（尤其一次跑整集），重试一次就好了 —— 不该让整集挂掉。"""
    calls: List[int] = []

    def fake(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, {"error": {"message": "rate limit"}})
        return _Resp(REPLY)

    monkeypatch.setattr(ds.urllib.request, "urlopen", fake)
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    text, _ = registry.create("text", "deepseek").generate("p")
    assert text and len(calls) == 2


def test_network_error_is_marked_retryable(keyed, monkeypatch):
    def fake(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ds.urllib.request, "urlopen", fake)
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    with pytest.raises(ProviderError) as err:
        registry.create("text", "deepseek").generate("p")
    assert err.value.retryable is True
    assert "网络错误" in str(err.value)


def test_non_json_body_is_a_retryable_error(keyed, monkeypatch):
    """网关抽风时会返回一段 HTML，别让 json 的报错伪装成代码 bug。"""
    _say(monkeypatch, b"<html>502 Bad Gateway</html>")
    with pytest.raises(ProviderError) as err:
        registry.create("text", "deepseek").generate("p")
    assert "不是 JSON" in str(err.value)


def test_empty_choices_is_an_error_not_an_empty_script(keyed, monkeypatch):
    """空剧本会被后面的阶段当成"这一集什么都没写"，不如在这儿炸。"""
    _say(monkeypatch, {"choices": [], "usage": {}})
    with pytest.raises(ProviderError) as err:
        registry.create("text", "deepseek").generate("p")
    assert err.value.retryable is True
    assert "choices" in str(err.value)


def test_reasoner_spending_its_whole_budget_on_thinking_says_so(keyed, monkeypatch):
    """正文空、思考非空 = max_tokens 被思考吃完了。

    这条线索比一句"返回空"有用得多：照着它去调预算，而不是反复重试。
    """
    _say(monkeypatch, {"choices": [{"message": {
        "content": "", "reasoning_content": "先想想第 1 场怎么开…"}}], "usage": {}})
    with pytest.raises(ProviderError) as err:
        registry.create("text", "deepseek").generate("p")
    assert err.value.retryable is False
    assert "max_tokens" in str(err.value)
