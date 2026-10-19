"""aihubmix 适配器的离线单测。

一条网络调用都没有 —— 全部 monkeypatch 掉 ``_Client``。
真接口上的三个待核实点（data URL 收不收、产物地址在哪个字段、
按 token 计费的真实单价）不在这里断言，它们只能靠一次真调用回答。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ai_drama.providers import ProviderError, available
from ai_drama.providers.aihubmix import (
    MAX_SECONDS,
    MIN_SECONDS,
    AiHubMixVideo,
    _aspect_of,
    _resolution_of,
)


@pytest.fixture(autouse=True)
def no_ambient_key(monkeypatch):
    monkeypatch.delenv("AI_DRAMA_AIHUBMIX_API_KEY", raising=False)
    monkeypatch.delenv("AI_DRAMA_AIHUBMIX_VIDEO_MODEL", raising=False)
    monkeypatch.delenv("AI_DRAMA_AIHUBMIX_USD_PER_SECOND", raising=False)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("AI_DRAMA_AIHUBMIX_API_KEY", "test-key")
    return "test-key"


class _Client:
    """假的 HTTP 客户端：记下每一次请求，按脚本返回。"""

    def __init__(self, posts: Optional[List[Dict[str, Any]]] = None,
                 gets: Optional[List[Dict[str, Any]]] = None,
                 downloads: Optional[List[Optional[str]]] = None,
                 base_url: str = "https://aihubmix.com/ai/v1"):
        self.posts: List[Tuple[str, Dict[str, Any]]] = []
        self.gets: List[str] = []
        self.downloads: List[Tuple[str, Path]] = []
        self._posts = list(posts or [])
        self._gets = list(gets or [])
        self._downloads = list(downloads or [])
        self.base_url = base_url

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                *, timeout: Optional[float] = None) -> Dict[str, Any]:
        if method == "POST":
            self.posts.append((path, payload or {}))
            return self._posts.pop(0) if self._posts else {"id": "t"}
        self.gets.append(path)
        return self._gets.pop(0) if self._gets else {"status": "pending"}

    def download(self, url: str, dest: Path, *, timeout: float = 600.0) -> Path:
        self.downloads.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-mp4")
        return dest


def _provider(client: _Client, *, per_second: str = "") -> AiHubMixVideo:
    if per_second:
        os.environ["AI_DRAMA_AIHUBMIX_USD_PER_SECOND"] = per_second
    return AiHubMixVideo(client=client)


# ———————————————————————————————————————————————————————— 注册与 key

def test_the_adapter_registers_itself():
    assert "aihubmix" in available("video")["video"]


def test_no_key_is_a_loud_error_not_a_silent_fake(monkeypatch):
    with pytest.raises(ProviderError) as boom:
        AiHubMixVideo()
    assert "AI_DRAMA_AIHUBMIX_API_KEY" in str(boom.value)
    assert boom.value.retryable is False
    assert "静默降级" in str(boom.value)


def test_the_model_can_be_switched_by_env(keyed, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_AIHUBMIX_VIDEO_MODEL", "some-other-model")
    assert AiHubMixVideo(client=_Client()).model == "some-other-model"


# ———————————————————————————————————————————————————————— 纯函数

@pytest.mark.parametrize("size,want", [
    ((720, 1280), "9:16"),
    ((1280, 720), "16:9"),
    ((1024, 1024), "1:1"),
    ((720, 960), "3:4"),
    ((840, 360), "21:9"),
])
def test_aspect_ratio_snaps_to_the_nearest_enum(size, want):
    assert _aspect_of(size) == want


@pytest.mark.parametrize("size,want", [
    ((480, 832), "480p"),
    ((720, 1280), "720p"),
    ((1080, 1920), "1080p"),
])
def test_resolution_follows_the_short_side(size, want):
    assert _resolution_of(size) == want


# ———————————————————————————————————————————————————————— 提交

def test_the_payload_turns_audio_generation_off(keyed):
    client = _Client()
    prov = _provider(client)
    prov.submit({"prompt": "雾中的渡轮", "duration": 15, "size": (720, 1280),
                 "dest": str(Path("out.mp4")), "shot_label": "sc01/shot_000"})
    path, payload = client.posts[0]
    assert path == "/videos"
    assert payload["generate_audio"] is False, "自带音轨会和 stage07 铺的台词叠两遍"
    assert payload["model"] == "doubao-seedance-2-5-260628"


def test_the_first_frame_goes_into_frame_images_not_a_bare_image(keyed, tmp_path):
    kf = tmp_path / "kf.png"
    kf.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    client = _Client()
    prov = _provider(client)
    prov.submit({"prompt": "起雾", "duration": 10, "size": (720, 1280),
                 "image_path": str(kf), "dest": str(tmp_path / "o.mp4")})
    payload = client.posts[0][1]
    assert "image" not in payload, "字段名写错不会报错，只会被安静地忽略"
    frame = payload["frame_images"][0]
    assert frame["frame_type"] == "first_frame"
    assert frame["image_url"]["url"].startswith("data:image/png;base64,")


def test_duration_is_rounded_to_a_whole_second(keyed):
    client = _Client()
    prov = _provider(client)
    prov.submit({"prompt": "x", "duration": 7.6, "size": (720, 1280),
                 "dest": str(Path("o.mp4"))})
    assert client.posts[0][1]["duration"] == 8


def test_duration_below_the_floor_is_lifted_not_rejected(keyed):
    client = _Client()
    prov = _provider(client)
    prov.submit({"prompt": "x", "duration": 1.0, "size": (720, 1280),
                 "dest": str(Path("o.mp4"))})
    assert client.posts[0][1]["duration"] == int(MIN_SECONDS)


def test_a_duration_over_the_cap_is_refused_not_truncated(keyed):
    prov = _provider(_Client())
    with pytest.raises(ProviderError) as boom:
        prov.submit({"prompt": "x", "duration": MAX_SECONDS + 5, "size": (720, 1280),
                     "dest": str(Path("o.mp4")), "shot_label": "sc01/shot_009"})
    assert boom.value.retryable is False
    assert "plan_shot_split" in str(boom.value)


def test_no_prompt_and_no_first_frame_is_refused(keyed):
    prov = _provider(_Client())
    with pytest.raises(ProviderError) as boom:
        prov.submit({"prompt": "   ", "duration": 8, "size": (720, 1280),
                     "dest": str(Path("o.mp4"))})
    assert "无从生成" in str(boom.value)


def test_a_submit_without_an_id_is_refused(keyed):
    prov = _provider(_Client(posts=[{"status": "queued"}]))
    with pytest.raises(ProviderError) as boom:
        prov.submit({"prompt": "x", "duration": 8, "size": (720, 1280),
                     "dest": str(Path("o.mp4"))})
    assert "没拿到任务 id" in str(boom.value)


# ———————————————————————————————————————————————————————— 轮询

def test_pending_maps_to_running(keyed):
    prov = _provider(_Client(gets=[{"id": "t", "status": "in_progress"}]))
    state, usage, out = prov.poll("t")
    assert state == "running" and usage == {} and out is None


def test_completed_downloads_into_dest_and_returns_that_path(keyed, tmp_path, monkeypatch):
    dest = tmp_path / "clip.mp4"
    client = _Client(gets=[{
        "id": "t", "status": "completed",
        "output": [{"url": "https://cdn.example/x.mp4"}],
    }])
    prov = _provider(client, per_second="0.5")
    prov.submit({"prompt": "x", "duration": 10, "size": (720, 1280), "dest": str(dest)})
    monkeypatch.setattr(prov, "_probe", lambda p: None)

    state, usage, out = prov.poll("t")

    assert state == "succeeded"
    assert out == str(dest), "stage04 拿这个字符串直接 Path(info).exists()"
    assert dest.exists()
    assert client.downloads[0][0] == "https://cdn.example/x.mp4"
    assert usage["amount"] == 10.0
    assert usage["money"] == pytest.approx(5.0)


def test_the_download_falls_back_to_the_content_endpoint(keyed, tmp_path, monkeypatch):
    dest = tmp_path / "clip.mp4"
    client = _Client(gets=[{"id": "t", "status": "completed"}])
    prov = _provider(client)
    prov.submit({"prompt": "x", "duration": 8, "size": (720, 1280), "dest": str(dest)})
    monkeypatch.setattr(prov, "_probe", lambda p: None)

    prov.poll("t")

    assert client.downloads[0][0].endswith("/videos/t/content")


def test_the_bill_follows_the_measured_length_not_the_request(keyed, tmp_path, monkeypatch):
    dest = tmp_path / "clip.mp4"
    client = _Client(gets=[{"id": "t", "status": "completed"}])
    prov = _provider(client, per_second="1.0")
    prov.submit({"prompt": "x", "duration": 8, "size": (720, 1280), "dest": str(dest)})
    monkeypatch.setattr(prov, "_probe", lambda p: 9.4)

    _, usage, _ = prov.poll("t")

    assert usage["amount"] == pytest.approx(9.4)
    assert usage["money"] == pytest.approx(9.4)
    assert "要 8s" in usage["note"] and "9.4s" in usage["note"]


def test_an_unpriced_service_records_zero_and_says_so(keyed, tmp_path, monkeypatch):
    dest = tmp_path / "clip.mp4"
    client = _Client(gets=[{"id": "t", "status": "completed"}])
    prov = _provider(client)
    prov.submit({"prompt": "x", "duration": 8, "size": (720, 1280), "dest": str(dest)})
    monkeypatch.setattr(prov, "_probe", lambda p: 8.0)

    _, usage, _ = prov.poll("t")

    assert usage["money"] == 0.0
    assert "未配每秒单价" in usage["note"]


def test_a_failed_task_raises_instead_of_returning_a_bad_file(keyed):
    prov = _provider(_Client(gets=[{
        "id": "t", "status": "failed",
        "error": {"message": "content moderation rejected"},
    }]))
    with pytest.raises(ProviderError) as boom:
        prov.poll("t")
    assert "content moderation rejected" in str(boom.value)


def test_cancelled_counts_as_failed(keyed):
    prov = _provider(_Client(gets=[{"id": "t", "status": "cancelled"}]))
    with pytest.raises(ProviderError):
        prov.poll("t")


# ———————————————————————————————————————————————————————— 价格配置

def test_a_bad_per_second_price_is_refused(keyed, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_AIHUBMIX_USD_PER_SECOND", "cheap")
    with pytest.raises(ProviderError) as boom:
        AiHubMixVideo(client=_Client())
    assert "不是数字" in str(boom.value)


def test_a_negative_price_is_refused(keyed, monkeypatch):
    monkeypatch.setenv("AI_DRAMA_AIHUBMIX_USD_PER_SECOND", "-1")
    with pytest.raises(ProviderError):
        AiHubMixVideo(client=_Client())
