"""百炼适配器 —— 只用假 HTTP，一次网络调用都不发生。

为什么这件事值得单写一个文件：这个适配器是**唯一会花钱**的代码。
它跑错一次，代价不是红色的测试报告，是账单。所以凡是能离线钉住的
（错误翻译、重试判定、单价折算、尺寸贴合、超上限拒绝、产物落盘位置），
全都在这里钉住；真正调模型的验证在 tests/test_pipeline.py 的 dryrun 那条路上，
两者分工是"形状用假的测，账目用真的对"。
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from ai_drama.providers import dashscope as ds
from ai_drama.providers import registry
from ai_drama.providers.base import ProviderError

KEY = "AI_DRAMA_DASHSCOPE_API_KEY"


@pytest.fixture
def keyed(monkeypatch):
    """给一个假 key —— 构造函数只检查"有没有"，不会拿它去联网。"""
    monkeypatch.setenv(KEY, "sk-fake-for-tests")
    monkeypatch.delenv("AI_DRAMA_DASHSCOPE_USD_RATE", raising=False)
    return "sk-fake-for-tests"


def _make(kind: str, **kw: Any):
    return registry.create(kind, "dashscope", **kw)


# —————————————————————————————————————————————————————— 注册与"不骗人"


def test_one_name_covers_six_kinds():
    """一个 key 覆盖六种形态 —— 这是选百炼的全部理由，所以要能一眼看到。"""
    avail = registry.available()
    for kind in ("text", "vision", "image", "t2i", "video", "audio"):
        assert "dashscope" in avail[kind], kind


def test_missing_key_fails_loudly_instead_of_faking(keyed, monkeypatch):
    """缺 key 必须报错，**不能**退回假数据。

    假装成功比失败贵：你会以为出了片，其实看的是占位图。
    """
    monkeypatch.delenv(KEY, raising=False)
    for kind in ("text", "image", "video", "audio", "vision", "t2i"):
        with pytest.raises(ProviderError) as err:
            _make(kind)
        assert err.value.retryable is False, kind
        assert KEY in str(err.value), "报错里得写清缺的是哪个环境变量"


def test_health_never_touches_the_network(keyed, monkeypatch):
    """doctor 会调 health()。为了探活而花钱是不可接受的。"""

    def boom(*a, **kw):
        raise AssertionError("health() 不该发网络请求")

    monkeypatch.setattr(ds.urllib.request, "urlopen", boom)
    info = _make("video").health()
    assert info["ok"] is True
    assert info["model"] and info["base_url"]


# —————————————————————————————————————————————————————— 错误翻译


@pytest.mark.parametrize("raw,code,message", [
    # 百炼最常见的一种
    (json.dumps({"error": {"code": "InvalidParameter", "message": "尺寸不支持"}}),
     "InvalidParameter", "尺寸不支持"),
    # 任务轮询失败时是平铺的（没有 error 包一层）
    (json.dumps({"code": "DataInspectionFailed", "message": "内容不合规"}),
     "DataInspectionFailed", "内容不合规"),
    # 网关吐 HTML / 纯文本时不能崩
    ("<html>502 Bad Gateway</html>", "", "<html>502 Bad Gateway</html>"),
    ("", "", ""),
])
def test_parse_error_handles_every_shape_we_have_seen(raw, code, message):
    got_code, got_message = ds._parse_error(raw)
    assert got_code == code
    assert got_message.startswith(message[:20])


def test_throttling_is_retryable_but_bad_parameters_are_not():
    """光看状态码不够：百炼把"可以重试"的错误也放在 400 的 body 里。

    判错的代价是双向的 —— 把 400 当可重试会白烧调用量，
    把 Throttling 当不可重试则会让一次已经付过钱的整集提前失败。
    """
    assert "Throttling" in ds._RETRYABLE_CODES
    assert "Throttling.RateQuota" in ds._RETRYABLE_CODES
    assert "InvalidParameter" not in ds._RETRYABLE_CODES
    assert "DataInspectionFailed" not in ds._RETRYABLE_CODES
    assert 429 in ds._RETRYABLE_HTTP and 503 in ds._RETRYABLE_HTTP
    assert 400 not in ds._RETRYABLE_HTTP and 401 not in ds._RETRYABLE_HTTP


def test_an_http_error_becomes_a_provider_error_with_the_code(keyed, monkeypatch):
    """百炼的 code 必须原样带出来 —— 排障时它就是关键词。"""
    body = json.dumps({"error": {"code": "Throttling.RateQuota",
                                 "message": "Requests rate limit exceeded"}}).encode()

    def raise_429(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, _Bytes(body))

    monkeypatch.setattr(ds.urllib.request, "urlopen", raise_429)
    with pytest.raises(ProviderError) as err:
        _make("text").generate("写个剧本")
    assert "Throttling.RateQuota" in str(err.value)
    assert err.value.retryable is True, "429 是排队挤不上，该重试"

    body400 = json.dumps({"error": {"code": "InvalidParameter",
                                    "message": "size 不支持"}}).encode()

    def raise_400(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, _Bytes(body400))

    monkeypatch.setattr(ds.urllib.request, "urlopen", raise_400)
    with pytest.raises(ProviderError) as err:
        _make("text").generate("写个剧本")
    assert err.value.retryable is False, "参数写错重试多少次都一样"


class _Bytes:
    """urllib 的 HTTPError 想要一个能 read() 的东西。

    `close()` 不是装饰：HTTPError 析构时会去关它收到的那个对象，
    少了这个方法就会在 GC 时抛一个跟测试毫无关系的 AttributeError。
    """

    def __init__(self, blob: bytes):
        self._blob = blob

    def read(self) -> bytes:
        return self._blob

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# —————————————————————————————————————————————————————— 单价与汇率


def test_prices_are_written_in_cny_and_reported_in_usd(keyed, monkeypatch):
    """百炼的价目表是元，这个项目的钱一律是美元（models.py:288）。

    单价在代码里按元写（照官方页抄），出门前折一次 —— 这条测试就是
    在钉"折了，而且只折了一次"，防止哪天忘了折，成本表少报 7 倍。
    """
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_USD_RATE", "7.1")
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_PRICE_PNG", "0.14")
    png = _make("image")
    assert png.price_png == pytest.approx(0.14 / 7.1)

    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_PRICE_I2V_SEC", "0.24")
    video = _make("video")
    assert video.price_second == pytest.approx(0.24 / 7.1)

    # 账目里出现的也必须是美元
    usage = video._video_usage(5.0, ["test"])
    assert usage["money"] == pytest.approx(5.0 * 0.24 / 7.1, rel=1e-6)


def test_the_rate_is_overridable_because_it_actually_moves(keyed, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_USD_RATE", "7.3")
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_PRICE_PNG", "0.14")
    assert _make("image").price_png == pytest.approx(0.14 / 7.3)


@pytest.mark.parametrize("bad", ["abc", "0", "-1", " "])
def test_a_broken_rate_or_price_is_refused_at_construction(keyed, monkeypatch, bad):
    """构造时就拒绝，而不是等算完账再给一个 NaN。

    成本表一旦失真，预算闸门也跟着失准 —— 那正是要花钱的地方。
    """
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_USD_RATE", bad)
    with pytest.raises(ProviderError) as err:
        _make("image")
    assert err.value.retryable is False


def test_a_broken_unit_price_is_refused_too(keyed, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_PRICE_PNG", "一毛四")
    with pytest.raises(ProviderError) as err:
        _make("image")
    assert "AI_DRAMA_DASHSCOPE_PRICE_PNG" in str(err.value)


# —————————————————————————————————————————————————————— 纯函数


@pytest.mark.parametrize("want,expected", [
    ((720, 1280), (720, 1280)),      # 9:16 正好在表里
    ((720, 1279), (720, 1280)),      # 差一个像素也得贴上，否则 InvalidParameter
    ((1920, 1080), (1280, 720)),     # 16:9
    ((1000, 1000), (1024, 1024)),    # 1:1
    ((1080, 1920), (720, 1280)),     # 竖屏
])
def test_sizes_are_snapped_to_a_supported_aspect(want, expected):
    """文生图只认三档尺寸，传别的直接 InvalidParameter。

    按**长宽比**挑最近的一档，不按像素数 —— 后者会把 720x1279 判成
    "离 1024x1024 更近"。
    """
    assert ds._snap_size(want) == expected


def test_only_models_that_accept_duration_are_told_one():
    """wanx2.1-i2v-turbo 不接受 duration，传了**提交即失败**。

    所以这不是优化，是必需项：判错一次就是一次白付的失败调用。
    """
    assert ds._accepts_duration("wan2.5-i2v-preview") is True
    assert ds._accepts_duration("wan2.6-i2v-flash") is True
    assert ds._accepts_duration("wanx2.1-i2v-turbo") is False
    assert ds._accepts_duration("wan2.2-i2v-plus") is False


def test_a_local_image_becomes_a_data_url_with_the_real_bytes(tmp_path):
    """定妆照在本地，百炼的 img_url / base_image_url 都吃 data URL。

    走 data URL 就不需要先把图传到某个公网位置 —— 少一个会过期的中间环节。
    """
    import base64

    png = tmp_path / "kf.png"
    blob = bytes(range(256)) * 4
    png.write_bytes(blob)
    url = ds._data_url(png)
    head, b64 = url.split(",", 1)
    assert head == "data:image/png;base64"
    assert base64.b64decode(b64) == blob


def test_known_voices_are_mapped_and_unknown_ones_pass_through(keyed):
    """项目圣经里写 female_01，百炼那边叫 Cherry。

    认不出来的原样透传，这样也能直接写 "Cherry" 或以后新增的音色名 ——
    映射表是便利，不是白名单。
    """
    audio = _make("audio")
    assert ds._VOICES["female_01"] == "Cherry"
    assert ds._VOICES["male_01"] == "Ethan"
    assert ds._VOICES.get("Cherry", "Cherry") == "Cherry"
    assert audio is not None


# —————————————————————————————————————————————————————— 视频：超上限与落盘


def test_video_refuses_a_duration_over_its_cap_instead_of_truncating(keyed):
    """超上限要**报错**，不能自己截断。

    悄悄给你前 5 秒，后半截的动作会凭空消失，而时间轴上还写着 12 秒。
    正常路径是 stage04 按 caps.max_duration 先拆段，走到这里说明上游漏了。
    """
    video = _make("video")
    with pytest.raises(ProviderError) as err:
        video.submit({"prompt": "x", "duration": 12.0, "image_path": "kf.png"})
    assert err.value.retryable is False
    assert "上限" in str(err.value)


def test_the_portrait_edit_can_be_switched_off_for_the_sake_of_framing(keyed, monkeypatch,
                                                                       tmp_path):
    """一致性和景别在这个接口上只能选一个，所以给了个开关。

    `wanx2.1-imageedit` 只有**一张**输入图，它同时是"这个人的脸"和
    "这个人的构图" —— 拿定妆照当底图，特写会被拉回定妆照的站姿。
    实跑一集见过：medium 和 close 两镜的关键帧几乎一模一样。

    关掉之后走纯文生图：景别听分镜的，脸会重新长。
    """
    # 只要是个真实存在的文件就能当"定妆照"，这里不真的读它的内容
    kf = Path(__file__)
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_IMAGE_USE_PORTRAIT", "0")
    image = _make("image")
    assert image.use_portrait is False
    calls: Dict[str, Any] = {}

    def run_it(provider):
        provider.client.post = lambda p, payload, **kw: (
            calls.update(path=p, payload=payload) or {"output": {"task_id": "t"}})
        provider._wait_image = lambda *a, **kw: "https://oss/x.png"
        provider.client.download = lambda u, p, **kw: Path(p).write_bytes(b"png")
        return provider.png("陈默站在雾里的甲板上", tmp_path / "out.png", ref_image=kf)

    # 关掉之后，即使给了定妆照也走文生图
    usage = run_it(image)
    assert calls["path"] == ds.T2I_PATH
    assert "景别优先" in usage["note"] and "没拿定妆照当底图" in usage["note"]

    # 打开（默认）且定妆照真的在，才走图生图
    monkeypatch.delenv("AI_DRAMA_DASHSCOPE_IMAGE_USE_PORTRAIT", raising=False)
    on = _make("image")
    assert on.use_portrait is True
    run_it(on)
    assert calls["path"] == ds.IMAGE_EDIT_PATH
    assert calls["payload"]["input"]["function"] == "description_edit"
    assert calls["payload"]["input"]["base_image_url"].startswith("data:")


def test_video_requires_a_first_frame(keyed):
    """这是图生视频模型。没有首帧就该说清"该换哪个模型"，而不是发出去等 400。"""
    video = _make("video")
    with pytest.raises(ProviderError) as err:
        video.submit({"prompt": "x", "duration": 5.0})
    assert "image_path" in str(err.value)
    assert "wan2.5-t2v-preview" in str(err.value), "要顺手告诉人家怎么办"


def test_video_poll_writes_the_file_because_stage04_will_not(keyed, tmp_path):
    """**这条钉的是 stage04 的契约。**

    stage04_video 拿到 poll 的第三项就 `Path(info).exists()` 然后搬走，
    它**不会**替你下载。所以 poll 必须把产物落到 dest 再返回本地路径 ——
    返回厂商的 URL 会让整段视频在"找不到文件"里丢掉，而钱已经花了。
    """
    video = _make("video")
    dest = tmp_path / "clip.mp4"
    job = _fake_video_job(video, dest, duration=5.0)

    state, usage, out = video.poll(job, timeout=5.0)
    assert state == "succeeded"
    assert out == str(dest)
    assert Path(out).exists() and Path(out).stat().st_size > 0
    # 按**实际产出**秒数计费，不是按请求的秒数
    assert usage["amount"] == pytest.approx(5.0)
    assert usage["unit"] == "second"
    assert usage["money"] > 0


def test_video_billing_follows_what_the_model_produced_not_what_we_asked(
        keyed, tmp_path, monkeypatch):
    """要 3 秒、模型固定出 5 秒 —— 账单上是 5 秒。

    按请求的 3 秒记会少报四成，成本表一失真，预算闸门就跟着失准。
    """
    video = _make("video")
    dest = tmp_path / "clip.mp4"
    # 假装模型就是出了 5.0 秒（裁切那步在下面单独测）
    monkeypatch.setattr(video, "_probe", lambda p: 5.0)
    monkeypatch.setattr(video, "_trim_to", lambda *a, **kw: False)
    job = _fake_video_job(video, dest, duration=3.0)
    _, usage, _ = video.poll(job, timeout=5.0)
    assert usage["amount"] == pytest.approx(5.0), "账单跟模型实际产出走"


def test_the_trimming_note_says_what_actually_happened(keyed, tmp_path, monkeypatch):
    """裁没裁都要写进 note。

    stage07 按 ffprobe 量出的**实际**时长累加时间轴，这里多 2 秒，
    后面每个镜头的声画都会往后错 2 秒 —— 所以这件事必须留在纸面上。
    """
    video = _make("video")
    dest = tmp_path / "clip.mp4"
    monkeypatch.setattr(video, "_probe", lambda p: 5.0)
    monkeypatch.setattr(video, "_trim_to", lambda *a, **kw: True)
    job = _fake_video_job(video, dest, duration=3.0)
    _, usage, _ = video.poll(job, timeout=5.0)
    assert "裁到 3.00s" in usage["note"]


def test_trimming_is_a_no_op_when_there_is_nothing_to_trim(keyed, tmp_path):
    """时长本来就不超、或没法探测时，不裁也不报错。

    裁切依赖 ffmpeg，而 ffmpeg 是可选的 —— 它不在的时候，
    一次已经付过钱的生成不该因此失败。
    """
    video = _make("video")
    dest = tmp_path / "clip.mp4"
    dest.write_bytes(b"x")
    assert video._trim_to(dest, 0.0) is False, "want<=0 直接跳过"
    assert video._trim_to(dest, 3.0, produced=3.0) is False, "没超就不裁"


def _fake_video_job(video, dest: Path, *, duration: float, url: str = "https://oss/x.mp4") -> str:
    """做一个"提交成功、轮询一次就完成"的假任务。

    只替换 HTTP 那一层，剩下（参数拼装、计费、落盘）全走真代码 ——
    要测的正是那些。
    """
    kf = dest.with_name("kf.png")
    kf.write_bytes(b"\x89PNG fake")
    video.client.post = lambda *a, **kw: {"output": {"task_id": "task-1"}}
    video.client.get = lambda *a, **kw: {
        "output": {"task_status": "SUCCEEDED", "video_url": url}}
    video.client.download = lambda u, p, **kw: Path(p).write_bytes(b"fake-mp4-bytes")
    return video.submit({"prompt": "渡轮靠岸", "duration": duration,
                         "image_path": str(kf), "dest": str(dest)})


# —————————————————————————————————————————————————————— 下载与 TTS


def test_download_refuses_a_zero_byte_product(keyed, tmp_path, monkeypatch):
    """0 字节的产物必须当成失败。

    否则一个空文件会被当成"生成成功"，一路走到成片才炸 ——
    那时候钱已经花了，而且看不出是哪一步的错。
    """
    client = ds._Client("k", "https://example.invalid")

    class _Empty:
        def read(self, n: int = 0) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ds.urllib.request, "urlopen", lambda *a, **kw: _Empty())
    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError) as err:
        client.download("https://oss/x.mp4", dest)
    assert err.value.retryable is True
    assert not dest.exists(), "半成品不能留在 dest 上"
    assert not dest.with_name(dest.name + ".part").exists(), "临时文件也要清掉"


def test_download_leaves_nothing_behind_on_http_error(keyed, tmp_path, monkeypatch):
    client = ds._Client("k", "https://example.invalid")

    def raise_403(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, _Bytes(b""))

    monkeypatch.setattr(ds.urllib.request, "urlopen", raise_403)
    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError) as err:
        client.download("https://oss/x.mp4", dest)
    assert err.value.retryable is False, "403 是签名过期，重试也一样"
    assert not dest.exists()


def test_async_header_is_only_sent_when_asked(keyed, monkeypatch):
    """异步任务是百炼的开关，漏了它会拿到同步的超时，而不是报错。"""
    seen: Dict[str, Optional[str]] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"ok":1}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def capture(req, timeout=None):
        seen["async"] = req.get_header("X-dashscope-async")
        seen["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(ds.urllib.request, "urlopen", capture)
    client = ds._Client("sk-abc", "https://example.invalid")
    client.post("/x", {}, async_=True)
    assert seen["async"] == "enable"
    assert seen["auth"] == "Bearer sk-abc"
    client.post("/x", {})
    assert seen["async"] is None


def test_tts_refuses_empty_text_instead_of_paying_for_silence(keyed, tmp_path):
    """空台词不该调用 TTS：那是付了钱拿回一段静音。"""
    audio = _make("audio")
    with pytest.raises(ProviderError) as err:
        audio.tts("   ", tmp_path / "a.wav")
    assert err.value.retryable is False


def test_tts_bills_the_characters_the_api_reported(keyed, tmp_path):
    """按接口自己报的 `usage.characters` 计费，不按本地 len(text)。

    它会把标点、英文单词按自己的规则折算，用本地字数会跟账单对不上。
    """
    audio = _make("audio")
    calls: Dict[str, Any] = {}

    def fake_post(path, payload, **kw):
        calls["payload"] = payload
        calls["path"] = path
        return {"output": {"audio": {"url": "https://oss/a.wav"}},
                "usage": {"characters": 999}}

    audio.client.post = fake_post
    audio.client.download = lambda u, p, **kw: Path(p).write_bytes(b"wav")
    usage = audio.tts("你终于回来了。", tmp_path / "a.wav", voice="female_01")
    assert calls["payload"]["input"]["voice"] == "Cherry", "female_01 → Cherry"
    assert calls["path"] == "/api/v1/services/aigc/multimodal-generation/generation"
    assert usage["amount"] == 999, "以接口报的字数为准"
    assert usage["stage"] == "audio" and usage["money"] > 0


def test_music_admits_it_is_not_real_bgm(keyed, tmp_path):
    """百炼没有配乐接口。写了占位音轨就必须说清楚。

    含糊过去的后果很具体：用户会以为听到的是"模型觉得合适的配乐"。
    """
    audio = _make("audio")
    usage = audio.music(tmp_path / "bgm.wav", duration=6.0, mood="安静")
    assert "占位" in usage["note"]
    assert "不是真 BGM" in usage["note"]
    assert usage["money"] == 0.0, "占位音轨不该记成花钱"
    assert (tmp_path / "bgm.wav").exists()


# —————————————————————————————————————————————————————— 账目形状


def test_every_usage_entry_carries_what_the_cost_table_needs(keyed, tmp_path):
    """成本表要能按阶段/按 provider 聚合，所以这几把钥匙一个都不能少。"""
    video = _make("video")
    dest = tmp_path / "clip.mp4"
    job = _fake_video_job(video, dest, duration=5.0)
    _, usage, _ = video.poll(job, timeout=5.0)
    for field in ("provider", "stage", "unit", "amount", "money"):
        assert field in usage, field
    assert usage["provider"] == "dashscope"
    assert usage["stage"] == "video"
