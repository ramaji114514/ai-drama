"""百炼适配器 —— 只用假 HTTP，一次网络调用都不发生。

为什么这件事值得单写一个文件：这个适配器是**唯一会花钱**的代码。
它跑错一次，代价不是红色的测试报告，是账单。所以凡是能离线钉住的
（错误翻译、重试判定、单价折算、尺寸贴合、超上限拒绝、产物落盘位置），
全都在这里钉住；真正调模型的验证在 tests/test_pipeline.py 的 dryrun 那条路上，
两者分工是"形状用假的测，账目用真的对"。
"""

from __future__ import annotations

import http.client
import json
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def test_one_name_covers_every_kind():
    """一个 key 覆盖七种形态 —— 这是选百炼的全部理由，所以要能一眼看到。"""
    avail = registry.available()
    for kind in ("text", "vision", "image", "t2i", "video", "audio", "lipsync"):
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


class _FakeOpener:
    """顶替 `build_opener()` 的产物，只实现 `download()` 真正用到的那一个方法。

    `download()` 会按「先直连、后系统代理」的顺序试出口，所以测试要控制的
    是**出口列表**（`_Client._openers`），而不是 `urllib.request.urlopen`
    —— 后者现在只服务 API 调用那几条路。
    """

    def __init__(self, behave):
        self._behave = behave
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        result = self._behave(req, timeout)
        if isinstance(result, Exception):
            raise result
        return result


class _Blob:
    """能一段段 read() 的假响应，用来验下载。"""

    def __init__(self, blob: bytes):
        self._blob = blob

    def read(self, n: int = -1) -> bytes:
        if not self._blob:
            return b""
        chunk, self._blob = self._blob[:n], self._blob[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mount_openers(monkeypatch, client, *behaviours):
    """把 client 的出口换成给定的假 opener，返回它们（按顺序）。

    名字照抄生产代码里的叫法（"直连" / "系统代理"），测试读起来才对得上。
    """
    names = ["直连", "系统代理"]
    made = [_FakeOpener(fn) for fn in behaviours]
    monkeypatch.setattr(client, "_openers", lambda: list(zip(names, made)))
    return made


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
    _mount_openers(monkeypatch, client,
                   lambda req, timeout=None: _Blob(b""),
                   lambda req, timeout=None: _Blob(b""))

    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError) as err:
        client.download("https://oss/x.mp4", dest)
    assert err.value.retryable is True
    assert not dest.exists(), "半成品不能留在 dest 上"
    assert not dest.with_name(dest.name + ".part").exists(), "临时文件也要清掉"


def test_download_leaves_nothing_behind_on_http_error(keyed, tmp_path, monkeypatch):
    client = ds._Client("k", "https://example.invalid")

    def raise_403(req, timeout=None):
        return urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, _Bytes(b""))

    _mount_openers(monkeypatch, client, raise_403, raise_403)
    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError) as err:
        client.download("https://oss/x.mp4", dest)
    assert err.value.retryable is False, "403 是签名过期，重试也一样"
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_goes_direct_before_reaching_for_a_proxy(keyed, tmp_path, monkeypatch):
    """先直连 —— 这是被人亲眼看到的一条真事，不是洁癖。

    百炼给的产物是带签名的 OSS 直链，本身不需要代理。而 Windows 上的
    系统代理设置会被 urllib **自动**读进来（`getproxies()` 读注册表），
    curl 不读 —— 于是出现过"同一条地址 curl 秒下、urllib 稳定超时"，
    看着像"百炼给了一个坏地址"。

    所以：第一个出口必须是直连，而且直连通了就不该再去碰代理。
    """
    client = ds._Client("k", "https://example.invalid")
    direct, proxied = _mount_openers(
        monkeypatch, client,
        lambda req, timeout=None: _Blob(b"real-bytes"),
        lambda req, timeout=None: _Blob(b"should-not-be-used"))

    dest = tmp_path / "out.mp4"
    client.download("https://oss/x.mp4", dest)
    assert dest.read_bytes() == b"real-bytes"
    assert direct.calls == 1
    assert proxied.calls == 0, "直连已经拿到了，就别再绕代理"


def test_download_falls_back_to_the_system_proxy(keyed, tmp_path, monkeypatch):
    """直连不通还要留着系统代理这条路。

    有些内网只有代理能出门；把代理整个扔掉，那些环境下就一个文件都下不下来。
    """
    client = ds._Client("k", "https://example.invalid")
    direct, proxied = _mount_openers(
        monkeypatch, client,
        lambda req, timeout=None: TimeoutError("timed out"),
        lambda req, timeout=None: _Blob(b"via-proxy"))

    dest = tmp_path / "out.mp4"
    client.download("https://oss/x.mp4", dest)
    assert dest.read_bytes() == b"via-proxy"
    assert direct.calls == 1 and proxied.calls == 1


def test_download_cleans_up_when_every_exit_fails(keyed, tmp_path, monkeypatch):
    """两条路都不通时报的是这个模块的错，且不留垃圾。"""
    client = ds._Client("k", "https://example.invalid")
    _mount_openers(monkeypatch, client,
                   lambda req, timeout=None: TimeoutError("timed out"),
                   lambda req, timeout=None: ConnectionResetError("reset"))

    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError) as err:
        client.download("https://oss/x.mp4", dest, attempts=1)
    assert err.value.retryable is True
    msg = str(err.value)
    assert "直连" in msg and "系统代理" in msg, f"两条出口都要出现在报错里：{msg}"
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_a_disconnect_is_reported_as_a_provider_error(keyed, tmp_path, monkeypatch):
    """`RemoteDisconnected` 是 ConnectionResetError，不是 URLError。

    不兜这一层，调用方收到的是一个来自 http.client 的堆栈，
    而它和"百炼出错了"完全看不出关系。
    """
    client = ds._Client("k", "https://example.invalid")
    _mount_openers(monkeypatch, client,
                   lambda req, timeout=None: http.client.RemoteDisconnected("gone"),
                   lambda req, timeout=None: http.client.RemoteDisconnected("gone"))

    with pytest.raises(ProviderError) as err:
        client.download("https://oss/x.mp4", tmp_path / "out.mp4", attempts=1)
    assert "RemoteDisconnected" in str(err.value)


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


# —————————————————————————————————————————————————————— 口型同步（VideoRetalk）


class _FakeMedia:
    """假的 core.media：记下被调过哪些 ffmpeg 命令，不真跑。

    口型同步这条路要跑三次 ffmpeg（对音轨、去音轨、对时长），真跑一遍
    要几十秒。这里只关心"我们**让它**做了什么"，所以把 subprocess 换掉，
    顺便按调用意图伪造出产物文件，让后续的 exists() / 时长校验能继续走。
    """

    def __init__(self, produced: Dict[str, Optional[float]]):
        self.produced = produced          # 输出文件名 -> 模拟量出的时长
        self.calls: List[List[str]] = []

    def find_ffmpeg(self) -> str:
        return "ffmpeg"

    def find_ffprobe(self) -> str:
        return "ffprobe"

    def try_probe_duration(self, path) -> Optional[float]:
        return self.produced.get(Path(path).name)

    def run(self, cmd, *, timeout: int = 900, cwd=None):
        args = [str(c) for c in cmd]
        self.calls.append(args)
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"#fake media")
        # 造出来的东西多长：测试写死了就听测试的，没写就按命令里的 -t 推。
        # （真实 ffmpeg 里长度由参数决定，这里只是把那件事补上。）
        if out.name not in self.produced:
            self.produced[out.name] = self._implied_duration(args)

        class _Done:
            returncode = 0
            stdout = b""
            stderr = b""

        return _Done()

    @staticmethod
    def _implied_duration(args: List[str]) -> Optional[float]:
        if "-t" in args:
            return float(args[args.index("-t") + 1])
        return None


def _lipsync_harness(keyed, tmp_path, monkeypatch, *, clip_seconds: float = 3.0,
                     produced_seconds: float = 3.0):
    """把一条 sync() 要碰的外部世界全换成假的，返回 (provider, 记录, video, audio)。

    只留下被测的那段逻辑：上传两个文件 → 提交异步任务 → 轮询 → 下载 →
    去音轨对齐时长 → 记账。
    """
    video = tmp_path / "shot_000_clip.mp4"
    video.write_bytes(b"#picture")
    audio = tmp_path / "shot_000_voice.wav"
    audio.write_bytes(b"#voice")

    # raw.mp4 是"接口给的产物"，normalize 之后再变成 dest
    media = _FakeMedia({"shot_000_clip.mp4": clip_seconds,
                        "raw.mp4": produced_seconds})
    prov = _make("lipsync")
    monkeypatch.setattr(prov, "_media", lambda: media)

    seen: Dict[str, Any] = {"uploads": [], "posts": [], "gets": []}

    def fake_upload(path, *, model, timeout=900.0):
        seen["uploads"].append((Path(path).name, model))
        return f"oss://dashscope-instant/fake/{Path(path).name}"

    def fake_post(path, payload, **kw):
        seen["posts"].append((path, payload, kw))
        return {"output": {"task_id": "task-1"}}

    def fake_get(path, *, query=None, timeout=None, label=""):
        seen["gets"].append(path)
        return {"output": {"task_status": "SUCCEEDED", "video_url": "https://oss/x.mp4"}}

    def fake_download(url, dest, *, timeout=300.0):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"#downloaded")

    prov.client.upload = fake_upload
    prov.client.post = fake_post
    prov.client.get = fake_get
    prov.client.download = fake_download
    return prov, seen, media, video, audio


def test_lipsync_is_registered_and_defaults_to_videoretalk(keyed):
    prov = _make("lipsync")
    assert prov.kind == "lipsync"
    assert prov.default_model == "videoretalk"
    assert prov.model_env == "AI_DRAMA_DASHSCOPE_LIPSYNC_MODEL"
    assert prov.model == "videoretalk"


def test_the_usd_price_never_goes_through_the_exchange_rate(keyed, monkeypatch):
    """口型同步的官方报价是**美元**（$0.011469/秒），不能再折一次汇率。

    其它形态的价目表是元，走 `_price`；这个走 `_price_usd`。两套入口分家的
    理由：把美元价也塞进"元÷汇率"，汇率先行就把它的含义悄悄改掉了。
    """
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_USD_RATE", "7.1")
    prov = _make("lipsync")
    assert prov.price_second == pytest.approx(0.011469), "不该被 7.1 除"

    # 汇率动了，美元价不动 —— 这正是"本来就是美元"的含义
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_USD_RATE", "7.3")
    assert _make("lipsync").price_second == pytest.approx(0.011469)

    # 要改成别的美元价就设 _PRICE_USD_
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_PRICE_USD_LIPSYNC", "0.02")
    assert _make("lipsync").price_second == pytest.approx(0.02)


def test_a_broken_usd_price_is_refused_at_construction(keyed, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_DASHSCOPE_PRICE_USD_LIPSYNC", "免费")
    with pytest.raises(ProviderError) as err:
        _make("lipsync")
    assert err.value.retryable is False
    assert "AI_DRAMA_DASHSCOPE_PRICE_USD_LIPSYNC" in str(err.value)


def test_the_oss_resolve_header_is_sent_with_the_task(keyed, monkeypatch):
    """用 `oss://` 地址调模型时**必须**带这个头，否则服务端不认这个协议。

    漏了它的症状是 400「提供的 URL 无效」，跟"文件没上传成功"看起来一模一样，
    极容易往错的方向查一整天。
    """
    seen: Dict[str, Optional[str]] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"ok":1}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def capture(req, timeout=None):
        seen["oss"] = req.get_header("X-dashscope-ossresourceresolve")
        return _Resp()

    monkeypatch.setattr(ds.urllib.request, "urlopen", capture)
    client = ds._Client("sk-abc", "https://example.invalid")
    client.post("/x", {}, async_=True, oss=True)
    assert seen["oss"] == "enable"
    client.post("/x", {})
    assert seen["oss"] is None, "不传 oss 地址时不该带这个头"


def test_upload_builds_a_multipart_body_with_the_file_last(keyed, tmp_path, monkeypatch):
    """表单域的顺序有硬要求：`file` 必须是最后一个。

    OSS 的表单上传按顺序解析，file 后面还有域的话那些域会被当成文件内容。
    这里直接读回我们拼出来的 body 来钉住顺序。
    """
    blob = tmp_path / "clip.mp4"
    blob.write_bytes(b"PICTURE-BYTES")

    sent: Dict[str, Any] = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent["method"] = req.get_method()
        sent["ctype"] = req.get_header("Content-type")
        sent["length"] = req.get_header("Content-length")
        sent["body"] = req.data.read()
        return _Resp()

    prov = _make("lipsync")
    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)
    prov.client.get = lambda path, **kw: {"data": {
        "upload_host": "https://oss-upload.invalid", "upload_dir": "dashscope-instant/x",
        "oss_access_key_id": "LTAfake", "signature": "sig", "policy": "pol",
        "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true",
        "max_file_size_mb": "100",
    }}
    url = prov.client.upload(blob, model="videoretalk")

    assert sent["method"] == "POST"
    assert sent["ctype"].startswith("multipart/form-data; boundary=")
    body = sent["body"]
    assert b'name="OSSAccessKeyId"' in body
    assert b"PICTURE-BYTES" in body
    # file 之后只剩收尾的分隔线，没有别的表单域
    tail = body.split(b"PICTURE-BYTES", 1)[1]
    assert b'name="' not in tail, "file 必须是最后一个表单域"
    assert tail.strip().endswith(b"--")
    assert int(sent["length"]) == len(body), "Content-Length 得和真发的字节数对上"
    assert url.startswith("oss://dashscope-instant/x/")
    assert url.endswith(".mp4")


def test_the_content_length_survives_being_attached_to_a_file_body(keyed, tmp_path,
                                                                   monkeypatch):
    """`req.data = <文件对象>` 会**顺手删掉已经设好的 Content-Length**。

    这是 CPython 的一个有意为之的行为（bpo-16464：换了 body，长度就不作数了）。
    踩中的症状很隐蔽：请求退化成 chunked 传输，而 OSS 的表单上传不吃 chunked，
    回一个跟长度毫无关系的错。所以这条测试钉的是"设序"—— 长度必须在 body 之后设。
    """
    blob = tmp_path / "clip.mp4"
    blob.write_bytes(b"X")

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen: Dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):
        seen["length"] = req.get_header("Content-length")
        seen["ctype"] = req.get_header("Content-type")
        seen["body_len"] = len(req.data.read())
        return _Resp()

    prov = _make("lipsync")
    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)
    prov.client.get = lambda path, **kw: {"data": {
        "upload_host": "https://u.invalid", "upload_dir": "d/x",
        "oss_access_key_id": "L", "signature": "s", "policy": "p"}}
    prov.client.upload(blob, model="videoretalk")

    assert seen["length"] is not None, "少了 Content-Length 就会退化成 chunked"
    assert int(seen["length"]) == seen["body_len"], "长度得和真发的字节数一致"


def test_upload_gives_every_file_a_unique_key(keyed, tmp_path, monkeypatch):
    """`x-oss-forbid-overwrite=true`：同名传第二次会被拒。

    所以 key 里的文件名必须每次都不一样 —— 否则同一集里第二个镜头
    就会撞上第一个镜头的名字，报一个跟"重名"毫无关系的错。
    """
    blob = tmp_path / "clip.mp4"
    blob.write_bytes(b"X")

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    prov = _make("lipsync")
    monkeypatch.setattr(ds.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    prov.client.get = lambda path, **kw: {"data": {
        "upload_host": "https://oss-upload.invalid", "upload_dir": "d/x",
        "oss_access_key_id": "LTAfake", "signature": "s", "policy": "p"}}
    first = prov.client.upload(blob, model="videoretalk")
    second = prov.client.upload(blob, model="videoretalk")
    assert first != second
    assert first.startswith("oss://d/x/clip-") and first.endswith(".mp4")


def test_upload_refuses_a_file_over_the_policy_limit(keyed, tmp_path, monkeypatch):
    """凭证里写着这次能传多大，超了本地就拦，别等 OSS 回一个 403。"""
    blob = tmp_path / "clip.mp4"
    blob.write_bytes(b"x" * 2048)

    prov = _make("lipsync")
    monkeypatch.setattr(ds.urllib.request, "urlopen",
                        lambda req, timeout=None: pytest.fail("不该真的去传"))
    prov.client.get = lambda path, **kw: {"data": {
        "upload_host": "https://oss-upload.invalid", "upload_dir": "d/x",
        "oss_access_key_id": "L", "signature": "s", "policy": "p",
        "max_file_size_mb": "0.001"}}
    with pytest.raises(ProviderError) as err:
        prov.client.upload(blob, model="videoretalk")
    assert err.value.retryable is False
    assert "videoretalk" in str(err.value)


def test_lipsync_uploads_both_files_against_the_same_model(keyed, tmp_path, monkeypatch):
    """文件是**和模型绑定**的：上传时报的 model 必须和调用时一模一样。

    对不上时报"文件不存在"，不说为什么 —— 所以这里把这一致性钉死。
    """
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch)
    prov.sync(video, audio, tmp_path / "out.mp4", shot_label="sc01/shot_000")
    names = [name for name, _ in seen["uploads"]]
    assert len(names) == 2, "画面和音轨都要传"
    assert any(n.endswith(".mp4") for n in names) and any(n.endswith(".wav") for n in names)
    assert [m for _, m in seen["uploads"]] == ["videoretalk", "videoretalk"]


def test_lipsync_asks_for_the_same_length_back(keyed, tmp_path, monkeypatch):
    """`video_extension=False` 是刻意的：让它出息和输入一样长的片子。

    开着的话它会自己把画面拉长去就音轨 —— 那就直接违反契约 2 了。
    """
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch)
    prov.sync(video, audio, tmp_path / "out.mp4")
    path, payload, kw = seen["posts"][0]
    assert path == ds.LIPSYNC_PATH
    assert kw.get("async_") is True, "这是个异步任务"
    assert kw.get("oss") is True, "输入是 oss:// 地址，必须带那个头"
    assert payload["parameters"]["video_extension"] is False
    assert payload["input"]["video_url"].startswith("oss://")
    assert payload["input"]["audio_url"].startswith("oss://")


def test_the_dubbing_is_fitted_to_the_picture_before_uploading(keyed, tmp_path,
                                                               monkeypatch):
    """音轨先补/裁到和画面一样长，再上传。

    为什么非做不可：`video_extension=False` 时产物取 min(画面, 音轨)。
    配音比画面短，产物就跟着短 —— 那就撞上契约 2 了。所以补静音（apad）
    并且显式 `-t` 卡到画面长度。
    """
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch,
                                                       clip_seconds=3.0)
    prov.sync(video, audio, tmp_path / "out.mp4")
    fit = [args for args in media.calls if "apad" in args]
    assert len(fit) == 1, "音轨要被处理过一次"
    assert "-t" in fit[0]
    assert fit[0][fit[0].index("-t") + 1] == "3.000", "卡到画面的秒数"


def test_lipsync_strips_the_audio_track(keyed, tmp_path, monkeypatch):
    """契约 1：出去的东西不带音轨。

    留着的话成片里每句台词都会说两遍（一遍原声一遍对齐后的）。
    """
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch)
    prov.sync(video, audio, tmp_path / "out.mp4")
    final = media.calls[-1]
    assert "-an" in final, f"最后一步要带 -an：{final}"


def test_lipsync_trims_a_longer_product_back_to_the_input(keyed, tmp_path, monkeypatch):
    """接口多给了几秒就裁掉 —— stage07 按实际时长累加时间轴。"""
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch,
                                                       clip_seconds=3.0,
                                                       produced_seconds=5.0)
    usage = prov.sync(video, audio, tmp_path / "out.mp4")
    assert "-t" in media.calls[-1]
    assert media.calls[-1][media.calls[-1].index("-t") + 1] == "3.000"
    assert "裁回" in usage["note"]


def test_lipsync_pads_a_shorter_product_with_a_frozen_frame(keyed, tmp_path, monkeypatch):
    """接口给短了就用定格补上 —— 长度不许变，画面怎么补是我们的事。"""
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch,
                                                       clip_seconds=3.0,
                                                       produced_seconds=2.0)
    usage = prov.sync(video, audio, tmp_path / "out.mp4")
    assert any("tpad" in a for a in media.calls[-1])
    assert "补" in usage["note"]


def test_lipsync_refuses_to_hand_back_a_clip_of_the_wrong_length(keyed, tmp_path,
                                                                 monkeypatch):
    """对不上的时候**宁可失败**，也不交一个会让后面所有镜头错位的产物。

    stage06 会把这一镜记成失败 —— 可以单独重跑，比静默错位强。
    """
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch,
                                                       clip_seconds=3.0,
                                                       produced_seconds=2.0)
    # 把"补时长"那条路堵掉，模拟 ffmpeg 没能把长度拉回来
    media.produced["out.mp4"] = 2.0
    original = media.run

    def blind(cmd, *, timeout=900, cwd=None):
        original(cmd, timeout=timeout, cwd=cwd)
        media.produced["out.mp4"] = 2.0

    media.run = blind
    with pytest.raises(ProviderError) as err:
        prov.sync(video, audio, tmp_path / "out.mp4")
    assert "时长" in str(err.value)
    assert err.value.retryable is False, "重试也是同一个结果，别烧钱"


def test_lipsync_refuses_a_clip_outside_the_documented_window(keyed, tmp_path,
                                                              monkeypatch):
    """文档写死 2s < 时长 < 120s。越界本地就拦 —— 发过去也是 400。"""
    for seconds in (1.5, 130.0):
        prov, seen, media, video, audio = _lipsync_harness(
            keyed, tmp_path, monkeypatch, clip_seconds=seconds)
        with pytest.raises(ProviderError) as err:
            prov.sync(video, audio, tmp_path / f"out{seconds}.mp4")
        assert err.value.retryable is False
        assert not seen["uploads"], "本地拦下的不该产生任何上传"
        assert "120" in str(err.value) or "2" in str(err.value)


def test_lipsync_without_dubbing_says_so_instead_of_pretending(keyed, tmp_path,
                                                              monkeypatch):
    """没有配音文件时允许直接重编码过去 —— 但必须如实说没同步过。

    含糊过去的后果：报告里这一镜看着是"已同步"，成片里嘴一动不动。
    """
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch)
    usage = prov.sync(video, audio=None, dest=tmp_path / "out.mp4")
    assert usage["money"] == 0.0, "什么都没做就不该记账"
    assert "没有做口型同步" in usage["note"]
    assert not seen["uploads"], "没音轨可对，不该上传"
    assert (tmp_path / "out.mp4").exists()


def test_lipsync_bills_the_seconds_it_produced(keyed, tmp_path, monkeypatch):
    """按产出片段的秒数计费 —— 那是账单的依据。"""
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch,
                                                       clip_seconds=3.0,
                                                       produced_seconds=3.0)
    usage = prov.sync(video, audio, tmp_path / "out.mp4", shot_label="sc01/shot_000")
    assert usage["provider"] == "dashscope"
    assert usage["stage"] == "lipsync"
    assert usage["unit"] == "second"
    assert usage["amount"] == pytest.approx(3.0)
    assert usage["money"] == pytest.approx(3.0 * 0.011469, rel=1e-6)
    assert usage["shot_label"] == "sc01/shot_000"
    assert "videoretalk" in usage["note"]


def test_lipsync_cleans_up_its_work_directory(keyed, tmp_path, monkeypatch):
    """上传的表单体、下载的原片、对好的音轨都是中间物，不该留在产物旁边。

    留着的后果很具体：`runs/` 会被几十 MB 的 `.raw.mp4` 撑起来，
    而资产库会把它们当成本次 run 的产物一起登记。
    """
    dest = tmp_path / "out.mp4"
    prov, seen, media, video, audio = _lipsync_harness(keyed, tmp_path, monkeypatch)
    prov.sync(video, audio, dest)
    assert dest.exists()
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name not in {"shot_000_clip.mp4", "shot_000_voice.wav", "out.mp4"}]
    assert leftovers == [], f"留了中间物：{leftovers}"
