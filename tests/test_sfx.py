"""音效：三种声音各有各的位置。

这一组测试守的是几件很容易做错、而做错了**不会报错**的事：

  * 音效被写进了画面提示词（质检那条 `forbidden_in_visual` 拦的是这个）
  * 分镜写了 40 条音效，而没有任何 provider —— 成片里一声都没有，报告里也不写
  * 音效和台词各算一遍时间轴（字幕当初就是这么飘掉的）
  * 音效和台词一样响，把台词盖掉（观众唯一会立刻发现的问题）
  * dryrun 合成的占位声假装自己真的是雨声

所以这里既测"洗数据"，也测"盘上真的有声音" —— 光看文件生成了，
和"字幕文件生成了但画面上没有字"是同一类错觉。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from ai_drama.core import media, paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.dryrun import DryRunSfxProvider
from ai_drama.stages import stage01_script


# ————————————————————————————————————————----
# 洗分镜给的数据
# ————————————————————————————————————————----


def test_cues_are_cleaned_and_deduped():
    got = stage01_script._cues("门被撞开, 雨声，门被撞开")
    assert got == ["门被撞开", "雨声"]


def test_music_is_not_an_effect():
    """模型会把 BGM 写进 sfx。

    放进去的后果不是报错，而是**每一镜都去合成一段"弦乐"** —— 成本翻倍，
    而且听起来像有人在旁边一直按琴键。
    """
    assert stage01_script._cues(["紧张的低音弦乐", "风声", "背景音乐", "心跳"]) == \
        ["风声", "心跳"]


def test_a_sentence_is_not_an_effect():
    """一句话的描述是镜头说明写错了地方，不是音效。"""
    long_one = "雨水顺着铁皮棚的边缘成串地砸下来并溅起细密的水雾打在镜头上"
    assert stage01_script._cues([long_one, "雨声"]) == ["雨声"]


def test_non_strings_are_dropped():
    assert stage01_script._cues([{"a": 1}, None, "脚步", 42]) == ["脚步"]


# ————————————————————————————————————————----
# dryrun 音效：真出声，但自己说清楚那是什么
# ————————————————————————————————————————----


def test_the_placeholder_is_deterministic_but_not_identical():
    """同一个 cue 每次同一种声音（可写回归测试），不同 cue 听上去不一样
    （这样混音时能听出"哪一镜有音效"）。"""
    a1 = DryRunSfxProvider._shape("门被撞开", 0.5, 22050)
    a2 = DryRunSfxProvider._shape("门被撞开", 0.5, 22050)
    b = DryRunSfxProvider._shape("雨声", 0.5, 22050)
    assert a1 == a2
    assert a1 != b


def test_the_placeholder_actually_makes_sound(tmp_path: Path):
    """占位声也得是真波形 —— 一个全零的文件会让"混音接线对不对"无从验证。"""
    import struct
    import wave

    dest = tmp_path / "sfx.wav"
    DryRunSfxProvider().make("门被撞开", dest, duration=0.6)
    with wave.open(str(dest), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        frames = w.readframes(w.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    assert max(abs(s) for s in samples) > 3000, "占位声几乎是静音的"
    assert max(abs(s) for s in samples) < 32767, "削顶了"


def test_the_placeholder_says_it_is_a_placeholder(tmp_path: Path):
    """一个假装是雨声的占位音比一段明显的噪声更糟 ——
    后者你一听就知道没配音效，前者会让人以为音效配好了。"""
    usage = DryRunSfxProvider().make("雨声", tmp_path / "s.wav", duration=0.5)
    assert usage["provider"] == "dryrun-sfx"
    assert "占位声" in usage["note"]
    assert "不是它描述的那个声音" in usage["note"]


def test_sfx_is_off_by_default_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv("AI_DRAMA_SFX", raising=False)
    assert registry.effective_name("sfx") == ""
    assert "sfx" in registry.DEFAULT_OFF_KINDS
    assert "sfx" in registry.OPTIONAL_KINDS
    monkeypatch.setenv("AI_DRAMA_SFX", "none")
    assert registry.build_bundle().sfx is None


# ————————————————————————————————————————----
# 阶段：没有 provider 时必须出声
# ————————————————————————————————————————----


def _pipe(workspace, *, sfx=True, **kw) -> Pipeline:
    store = Store(workspace / "runs" / "t.sqlite3")
    bundle = registry.build_bundle()
    # sfx **默认是关的**（`registry.DEFAULT_OFF_KINDS`），所以测试里必须显式
    # 装上或卸掉它 —— 靠环境变量会让这些用例在别人的机器上莫名其妙地过或不过。
    bundle.sfx = DryRunSfxProvider() if sfx else None
    return Pipeline(workspace, store, bundle, verbose=False, concurrency=2, **kw)


def _cue_count(pipe: Pipeline, run) -> int:
    ep = pipe.store.get_episode(run.run_id)
    return sum(len(s.sfx) for s in ep.shots)


@pytest.mark.ffmpeg
def test_declared_effects_without_a_provider_are_reported(workspace, need_ffmpeg):
    """"分镜写了音效但没有 provider"必须是报告里的一句话。

    成片里少了所有声音，是那种"一切正常"的缺失 —— 不写出来就没人会发现。
    """
    pipe = _pipe(workspace, sfx=False)
    run = pipe.start("暴雨夜的外卖单", scenes=1, shots_per_scene=4)
    result = pipe.run(run, stages=["script", "audio"])

    assert _cue_count(pipe, run) > 0, "dryrun 分镜本来就该写出音效"
    audio = next(r for r in result["reports"] if r["stage"] == "audio")
    joined = " ".join(audio["notes"])
    assert "没有配 sfx provider" in joined, audio["notes"]
    assert not list(Path(workspace).rglob("*_sfx*.wav")), "没配 provider 却产出了文件"


@pytest.mark.ffmpeg
def test_effects_are_written_one_file_per_cue(workspace, need_ffmpeg):
    pipe = _pipe(workspace)
    run = pipe.start("暴雨夜的外卖单", scenes=1, shots_per_scene=4)
    pipe.run(run, stages=["script", "audio"])

    ep = pipe.store.get_episode(run.run_id)
    expected = 0
    for shot in ep.shots:
        for idx, _cue in enumerate(shot.sfx):
            p = paths.sfx(workspace, run.run_id, shot.scene_id, shot.order, idx)
            assert p.exists() and p.stat().st_size > 0, f"缺 {p}"
            expected += 1
    assert expected > 0

    # 幂等：再跑一次直接把现成的文件复用，不该再合成一遍
    reports = pipe.run(run, stages=["audio"])["reports"]
    audio = next(r for r in reports if r["stage"] == "audio")
    assert "音效" in " ".join(audio["notes"])


@pytest.mark.ffmpeg
def test_rerunning_audio_does_not_regenerate_effects(workspace, need_ffmpeg):
    """重跑一次不该再花一次钱 —— 和台词、关键帧一样的缓存规矩。"""
    pipe = _pipe(workspace)
    run = pipe.start("暴雨夜的外卖单", scenes=1, shots_per_scene=3)
    pipe.run(run, stages=["script", "audio"])

    seen: list = []
    real_make = DryRunSfxProvider.make

    def spy(self, cue, dest, **kw):
        seen.append(cue)
        return real_make(self, cue, dest, **kw)

    DryRunSfxProvider.make = spy             # type: ignore[method-assign]
    try:
        pipe.run(run, stages=["audio"])
    finally:
        DryRunSfxProvider.make = real_make   # type: ignore[method-assign]
    assert seen == [], f"重跑又合成了一遍：{seen}"


# ————————————————————————————————————————----
# 混音：真的进了成片，而且不盖住台词
# ————————————————————————————————————————----


def _mean_volume_db(video: Path, start: float, end: float) -> float:
    """量一段窗口的平均响度（dB，越接近 0 越响）。"""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(max(0.05, end - start)),
         "-i", str(video), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore").stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
    assert m, out[-500:]
    return float(m.group(1))


@pytest.mark.ffmpeg
def test_the_effect_is_actually_in_the_film(workspace, need_ffmpeg):
    """**盘上有文件**和**成片里听得到**是两件事。

    和字幕那条像素级断言同一个道理：字幕文件生成了不代表画面上有字。
    这里量的是第一镜（dryrun 给它安排了"远处城市的低频嗡鸣"）那一秒的
    音频能量 —— 开关音效两次跑，响的必须是有音效那次。
    """
    off = _pipe(workspace, sfx=False)
    run_off = off.start("暴雨夜的外卖单", scenes=1, shots_per_scene=4)
    off.run(run_off, stages=["script", "frames", "video", "audio", "concat"])

    on = _pipe(workspace)
    run_on = on.start("暴雨夜的外卖单", scenes=1, shots_per_scene=4)
    on.run(run_on, stages=["script", "frames", "video", "audio", "concat"])

    ep = on.store.get_episode(run_on.run_id)
    assert ep.shots[0].sfx, "第一镜本该有音效，测试前提不成立"

    quiet = _mean_volume_db(paths.episode_video(workspace, run_off.run_id), 0.2, 1.0)
    loud = _mean_volume_db(paths.episode_video(workspace, run_on.run_id), 0.2, 1.0)
    assert loud > quiet + 3, f"音效没有进成片（关 {quiet}dB / 开 {loud}dB）"


@pytest.mark.ffmpeg
def test_effects_alone_are_still_mixed(workspace, tmp_path, need_ffmpeg):
    """没有台词、只有音效时也要走混音。

    原来那段代码是 `if not segments: 直接和 BGM 混（或原样拷贝）` ——
    加了 effets 之后这里写错就会**静默丢掉所有音效**，而那正是
    "没有人会报错"的那类 bug。
    """
    from PIL import Image

    img = workspace / "f.png"
    Image.new("RGB", (128, 128), (10, 20, 40)).save(img)
    clip = media.image_to_clip(img, workspace / "c.mp4", duration=1.0, size=(128, 128))

    fx = tmp_path / "hit.wav"
    DryRunSfxProvider().make("门被撞开", fx, duration=0.8)

    out = media.mix_voice_and_music(
        clip, [], workspace / "mixed.mp4", effects=[(fx, 0.1)])
    assert _mean_volume_db(out, 0.15, 0.6) > -40


@pytest.mark.ffmpeg
def test_the_timeline_records_the_effects(workspace, need_ffmpeg):
    """时间轴是排查对齐问题的第一份文件 —— 音效也得在里面。"""
    import json

    pipe = _pipe(workspace)
    run = pipe.start("暴雨夜的外卖单", scenes=1, shots_per_scene=4)
    pipe.run(run)
    rows = json.loads(
        paths.timeline(workspace, run.run_id).read_text(encoding="utf-8"))
    assert any(r.get("sfx") for r in rows), rows

    # 而且它和台词用的是**同一个** start（同一份累加结果）
    ep = pipe.store.get_episode(run.run_id)
    assert len(rows) == len(ep.shots)
