"""字幕：从剧本行到画面上的那行字。

这一组测试防的是同一类事故：**文档说有、代码其实没有**。
本骨架的 README 与 media.py 的 docstring 从第一天起就写着"烧字幕"，
而真正实现它之前，全仓连一句 subtitles 都没有 —— 也就是说一部竖屏短剧
交付出去，观众看到的是没有台词的画面，而报告里一切正常。

所以这里的断言分三层：
  1. 文本处理（剧本行 -> 该显示的那句话）
  2. 文件格式（SRT 能被别的工具读；ASS 的坐标系是真实画布尺寸）
  3. **真的烧进去了**（像素级：字幕出现后画面下方才有亮像素）

第三层是刻意的：前两层全过了，成片里依然可能一个字都没有
（滤镜写错、路径没找到、字体缺字都会被 ffmpeg 静默容忍）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_drama.core import media, paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry


def _pipeline(root: Path, *, subtitles: bool = True) -> tuple[Pipeline, Store]:
    store = Store(root / "runs" / "test.sqlite3")
    return Pipeline(root, store, registry.build_bundle(), concurrency=2,
                    verbose=False, subtitles=subtitles), store


# —————————————————————————————————————————————— 1. 文本

def test_screenplay_line_loses_its_speaker_and_stage_direction():
    assert media.speech_text("苏蔓：（低声）不对。") == "不对。"
    assert media.speech_text("陈默：我到了。") == "我到了。"
    assert media.speech_text("LIN WAN: Who are you?") == "Who are you?"
    assert media.speech_text("（画外音）别回头") == "别回头"


def test_a_line_that_is_all_stage_direction_is_left_alone():
    """去完什么都不剩时退回原文 —— 显示一句带提示的台词，总比显示空白强。"""
    assert media.speech_text("（只有提示）") == "（只有提示）"
    assert media.speech_text("") == ""


def test_plain_text_survives_untouched():
    assert media.speech_text("没有前缀的台词") == "没有前缀的台词"


# —————————————————————————————————————————————— 2. 文件格式

def test_srt_is_readable_by_other_tools(tmp_path: Path):
    p = media.build_srt([(0.0, 1.5, "第一句"), (6.5, 9.125, "第二句")],
                        tmp_path / "sub.srt")
    text = p.read_text(encoding="utf-8")
    assert text.startswith("1\n00:00:00,000 --> 00:00:01,500\n第一句\n")
    assert "2\n00:00:06,500 --> 00:00:09,125\n第二句\n" in text
    assert "75" not in text.split("-->")[0].split("\n")[-1]


def test_srt_timestamps_round_to_milliseconds():
    assert media._srt_time(0) == "00:00:00,000"
    assert media._srt_time(61.9999) == "00:01:02,000"
    assert media._srt_time(3661.5) == "01:01:01,500"
    assert media._srt_time(-3) == "00:00:00,000"


def test_ass_declares_the_real_canvas_so_font_size_is_pixels(tmp_path: Path):
    """libass 读 SRT 时假定坐标系是 384x288，FontSize 就不是像素了。

    自己写 ASS 并把 PlayRes 设成真实画布，字号才可预测 ——
    这是踩过一次的坑，测试把它钉住。
    """
    p = media.build_ass([(0.0, 1.0, "一句话")], tmp_path / "sub.ass",
                        width=1080, height=1920, font="TestFont")
    text = p.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in text
    assert "PlayResY: 1920" in text
    style = [ln for ln in text.splitlines() if ln.startswith("Style: Default,")][0]
    fields = style.split("Default,", 1)[1].split(",")
    assert fields[0] == "TestFont"
    assert int(fields[1]) == round(1920 * 0.042)
    # 字段顺序：0 Fontname, 1 Fontsize, ..., 17 MarginL, 18 MarginR, 19 MarginV, 20 Encoding
    # MarginV 也必须是像素级换算的结果，不是一个写死的数
    assert int(fields[19]) == round(1920 * 0.075)
    assert int(fields[17]) == round(1080 * 0.083)


def test_ass_escapes_what_libass_would_treat_as_syntax(tmp_path: Path):
    """`{}` 是特效块、`\\N` 是换行 —— 台词里出现它们，轻则样式乱、重则整句消失。"""
    p = media.build_ass([(0.0, 1.0, "{\\an8}别动")], tmp_path / "sub.ass",
                        width=720, height=1280)
    line = [ln for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.startswith("Dialogue:")][0]
    body = line.rsplit(",,", 1)[1]
    assert "{" not in body and "}" not in body
    assert "\\a" not in body


def test_subtitle_font_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("AI_DRAMA_SUBTITLE_FONT", "我的字体")
    assert media.subtitle_font() == "我的字体"
    monkeypatch.delenv("AI_DRAMA_SUBTITLE_FONT")
    # 没设时给一个真实存在的字体名，而不是空串（空串会让 libass 用默认字体，
    # 中文就变成一排方块）
    assert media.subtitle_font()


# —————————————————————————————————————————————— 3. 真的烧进去了

@pytest.mark.ffmpeg
def test_probe_size_reads_the_real_frame(workspace: Path, need_ffmpeg: None):
    pipe, _ = _pipeline(workspace)
    run = pipe.start("量一下画布尺寸", scenes=1, shots_per_scene=2)
    result = pipe.run(run)
    assert result["state"] == "done", result
    size = media.probe_size(Path(result["final_video"]))
    assert size is not None
    width, height = size
    assert width > 0 and height > 0


@pytest.mark.ffmpeg
def test_the_subtitle_is_actually_burned_into_the_picture(workspace: Path,
                                                          need_ffmpeg: None):
    """像素级断言：字幕那一秒的画面下方有亮像素，没有台词的镜头没有。

    为什么不信"文件生成了"：滤镜写错、SRT 路径没找到、字体缺字，
    ffmpeg 全都不会报错，它只是**什么都不画**。
    """
    from PIL import Image

    pipe, _ = _pipeline(workspace)
    run = pipe.start("暴雨夜最后一单", scenes=1, shots_per_scene=3)
    result = pipe.run(run)
    assert result["state"] == "done", result

    final = Path(result["final_video"])
    srt = paths.subtitles(workspace, run.run_id)
    ass = paths.subtitle_ass(workspace, run.run_id)
    assert srt.exists() and "-->" in srt.read_text(encoding="utf-8")
    assert ass.exists(), "开了字幕却没有 ASS —— 烧录这一步被跳过了"

    import json
    rows = json.loads(paths.timeline(workspace, run.run_id).read_text(encoding="utf-8"))
    with_lines = [r for r in rows if r["dialogue"]]
    assert with_lines, "这个片子里没有一句台词，测不了烧字幕"
    row = with_lines[0]

    def bright_bottom(sample_at: float) -> int:
        frame = workspace / "frame.png"
        media.run([media.require_ffmpeg(), "-y", "-loglevel", "error",
                   "-ss", str(sample_at), "-i", str(final),
                   "-frames:v", "1", str(frame)])
        img = Image.open(frame).convert("L")
        w, h = img.size
        band = img.crop((0, int(h * 0.80), w, h))
        # 用 tobytes() 而不是 getdata()：后者在 Pillow 12 起被弃用，
        # 而这个仓库把 DeprecationWarning 当错误
        return sum(1 for px in band.tobytes() if px > 200)

    # 台词镜：字幕在画面下方，白字加黑描边
    cue = bright_bottom(float(row["start"]) + float(row["duration"]) / 2)
    # 没有台词的第一镜：同一位置应该干干净净（dryrun 的画面是深色卡片）
    silent = bright_bottom(0.2)
    assert cue > silent, f"台词镜亮像素 {cue} 不比无台词镜 {silent} 多，字幕没烧上"


@pytest.mark.ffmpeg
def test_turning_subtitles_off_still_leaves_a_usable_srt(workspace: Path,
                                                         need_ffmpeg: None):
    """不烧 != 没有字幕。SRT 照样写出来，好让剪辑软件外挂上去。"""
    pipe, _ = _pipeline(workspace, subtitles=False)
    run = pipe.start("不要烙在画面上", scenes=1, shots_per_scene=3)
    result = pipe.run(run)
    assert result["state"] == "done", result
    assert paths.subtitles(workspace, run.run_id).exists()
    assert not paths.subtitle_ass(workspace, run.run_id).exists()
    report = [r for r in result["reports"] if r["stage"] == "concat"][0]
    assert any("未烧录" in n for n in report["notes"]), report["notes"]
