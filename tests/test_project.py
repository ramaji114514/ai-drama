"""角色圣经：让第 40 集的脸还是第 1 集那张。

这一组测试守的是一个**结构性**问题，不是某个函数的行为：

    默认一次 run 出一集，角色由分镜阶段的 LLM 每集重新发明。
    第 1 集的"苏蔓"和第 2 集的"苏蔓"可以是两个人 —— 而两集各自内部
    都是自洽的（每集都复用了自己那张定妆照），所以**没有任何一步会报警**。
    只有观众会发现"换人了"。

真正的一致性保证不是"提示词写得像"，而是**每一集复用同一个文件**。
所以这里最要紧的一条是 `test_the_same_portrait_file_is_reused_next_episode`：
它同时钉住"没重复花钱"和"图是同一张"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ai_drama.core import paths, project
from ai_drama.core.models import Character
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.providers.dryrun import DryRunAudioProvider, DryRunT2IProvider


def _pipe(root: Path, project_id: Optional[str] = None, **kw) -> tuple[Pipeline, Store]:
    store = Store(root / "runs" / "test.sqlite3")
    bundle = registry.build_bundle()
    for k, v in kw.items():
        setattr(bundle, k, v)
    return Pipeline(root, store, bundle, concurrency=2, verbose=False,
                    project_id=project_id), store


# ————————————————————————————————————————————----
# 圣经本身
# ————————————————————————————————————————————----


def test_the_bible_is_a_plain_file_you_can_edit(workspace: Path):
    """圣经是文件不是数据库 —— 制片说"苏蔓改成短发"，改这个 json 就行。"""
    proj = project.new_project("深夜外卖", style="冷色调")
    proj.characters.append(Character(name="苏蔓", desc="女，31岁，短发"))

    p = proj.save(workspace)
    assert p == paths.project_json(workspace, proj.project_id)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["characters"][0]["name"] == "苏蔓"
    assert data["style"] == "冷色调"

    back = project.load(workspace, proj.project_id)
    assert back is not None
    assert [c.name for c in back.characters] == ["苏蔓"]
    assert back.characters[0].desc == "女，31岁，短发"


def test_a_missing_project_is_none_but_a_broken_one_is_loud(workspace: Path):
    """没有项目 = 正常（单集模式）；项目读坏了 = 必须抛。

    读坏了静默返回一个空项目，等于把一部 60 集的片子悄悄变成"每集重新投胎" ——
    而且要跑到第 2 集成片才看得出来。
    """
    assert project.load(workspace, "proj_nope") is None

    pid = "proj_broken"
    p = paths.project_json(workspace, pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        project.load(workspace, pid)

    # 坏项目不该把 list 打挂，但必须显示出来
    rows = project.list_projects_lenient(workspace)
    assert any(proj is None and "proj_broken" in err for proj, err in rows)


def test_scripture_block_puts_the_cast_in_front_of_the_model():
    proj = project.new_project("深夜外卖")
    proj.characters.append(Character(name="苏蔓", desc="女，31岁，强势的项目负责人",
                                     ref_image="X:/p/苏蔓.png"))
    proj.characters.append(Character(name="林晚", desc="女，28岁，调查记者"))

    block = proj.scripture_block()
    assert "苏蔓" in block and "林晚" in block
    assert "必须沿用" in block
    # 已有定妆照的角色要额外标一句，否则模型会重新描写外貌
    assert "定妆照已锁定" in block


def test_merge_cast_locks_the_appearance_the_model_tries_to_change():
    """核心单测：模型把苏蔓写成了另一个人，合并后必须还是圣经里的那个。"""
    proj = project.new_project("深夜外卖")
    proj.characters.append(Character(name="苏蔓", desc="女，31岁，短发",
                                     anchor_prompt="短发职业女性定妆照"))

    cast, stats = proj.merge_cast([
        Character(name="苏蔓", desc="女，22岁，大波浪长发",
                  anchor_prompt="长发少女定妆照"),
        Character(name="陈默", desc="男，34岁，技术员"),
    ])

    by = {c.name: c for c in cast}
    assert by["苏蔓"].desc == "女，31岁，短发", "模型改掉了在册角色的外貌"
    assert by["苏蔓"].anchor_prompt == "短发职业女性定妆照"
    assert by["陈默"].desc == "男，34岁，技术员"
    assert stats["new"] == 1
    # 新角色立刻入册，下一集就是"在册角色"
    assert proj.character_by_name("陈默") is not None


def test_a_registered_character_enters_the_episode_even_if_the_model_forgets_it():
    """模型漏写是最常见的失误。漏一次就让这个角色这一集锁不住脸，代价太大。"""
    proj = project.new_project("深夜外卖")
    proj.characters.append(Character(name="苏蔓", desc="女，31岁"))
    proj.characters.append(Character(name="林晚", desc="女，28岁"))

    cast, stats = proj.merge_cast([])          # 模型一个角色都没列
    assert [c.name for c in cast] == ["苏蔓", "林晚"]
    assert stats["locked"] == 2


# ————————————————————————————————————————————----
# 端到端：跨集复用
# ————————————————————————————————————————————----


class _CountingT2I(DryRunT2IProvider):
    """数一数这一集真正生成了几张定妆照。"""

    name = "counting-t2i"

    def __init__(self) -> None:
        super().__init__()
        self.calls: List[str] = []

    def png(self, prompt: str, dest: Path, **kw: Any):
        self.calls.append(Path(dest).name)
        return super().png(prompt, dest, **kw)


@pytest.mark.ffmpeg
def test_the_same_portrait_file_is_reused_next_episode(workspace: Path,
                                                       need_ffmpeg: None):
    """这一条同时钉住两件事：**没重复花钱** 和 **图是同一张**。

    第一件是成本，第二件才是一致性。只钉住成本是不够的 —— 一个"每次重新
    生成但缓存命中"的实现同样省钱，却不能保证脸不漂。
    """
    proj = project.new_project("深夜外卖", style="冷色调")
    proj.save(workspace)
    t2i = _CountingT2I()
    pipe, store = _pipe(workspace, proj.project_id, t2i=t2i)

    run1 = pipe.start("第1集：暴雨夜送到一栋没人的楼", scenes=1, shots_per_scene=3)
    r1 = pipe.run(run1, stages=["script", "storyboard"])
    ep1 = store.get_episode(run1.run_id)
    assert ep1.characters, "第一集没生成任何角色"
    faces1 = {c.name: c.ref_image for c in ep1.characters}
    shots_first = len(t2i.calls)
    assert shots_first == len(ep1.characters)

    # 定妆照必须落在**项目**目录里，不是 run 目录 —— 否则下一集找不到它
    for name, ref in faces1.items():
        assert ref, f"{name} 没有定妆照"
        assert Path(ref) == paths.project_portrait(workspace, proj.project_id, name), (
            f"{name} 的定妆照没进项目目录：{ref}"
        )
        assert Path(ref).exists()

    # —— 第 2 集
    run2 = pipe.start("第2集：她在地下二层听见自己的名字", scenes=1, shots_per_scene=3)
    pipe.run(run2, stages=["script", "storyboard"])
    ep2 = store.get_episode(run2.run_id)
    faces2 = {c.name: c.ref_image for c in ep2.characters}

    # 在册角色：**一个都不许重新生成**。
    # 注意不能断言"第 2 集零新图" —— 第 2 集冒出个新角色（陈默）是正常创作，
    # 他当然要有一张新定妆照。要钉住的是"老角色没有重画"。
    regenerated = t2i.calls[shots_first:]
    reused_names = {Path(p).name for p in faces1.values()}
    assert not (set(regenerated) & reused_names), (
        f"第 2 集把在册角色的定妆照重新画了：{sorted(set(regenerated) & reused_names)} —— "
        f"复用同一个文件是跨集一致性的唯一硬保证，不是省钱的可选项"
    )
    for name, ref in faces1.items():
        assert faces2.get(name) == ref, (
            f"{name} 第 2 集换了参考图：{faces2.get(name)} != {ref}"
        )

    # 新角色入册并且也拿到了一张定妆照 —— 圣经是随剧集生长的
    bible = project.load(workspace, proj.project_id)
    assert bible is not None
    grown = [n for n in faces2 if n not in faces1]
    for name in grown:
        assert Path(faces2[name]).exists(), f"新角色 {name} 的定妆照不在磁盘上"
        assert bible.character_by_name(name) is not None, f"新角色 {name} 没写进圣经"

    # 圣经里的外貌描述不能被第 2 集的模型改写
    first = ep1.characters[0]
    assert bible.character_by_name(first.name).desc == first.desc


@pytest.mark.ffmpeg
def test_editing_the_bible_changes_the_next_episode(workspace: Path,
                                                    need_ffmpeg: None):
    """圣经改了，下一集就跟着改 —— 否则"人手可改"是假的。"""
    proj = project.new_project("深夜外卖")
    proj.characters.append(Character(name="苏蔓", desc="女，31岁，短发",
                                     anchor_prompt="短发定妆照"))
    t2i = _CountingT2I()
    pipe, store = _pipe(workspace, proj.project_id, t2i=t2i)
    pid = proj.project_id
    proj.save(workspace)

    run1 = pipe.start("第1集", scenes=1, shots_per_scene=2)
    pipe.run(run1, stages=["script", "storyboard"])
    assert store.get_episode(run1.run_id).character_by_name("苏蔓").desc == "女，31岁，短发"

    # 制片改口径：改文件，不动代码
    data = json.loads(paths.project_json(workspace, pid).read_text(encoding="utf-8"))
    for c in data["characters"]:
        if c["name"] == "苏蔓":
            c["desc"] = "女，31岁，齐耳短发，左手有疤"
    paths.project_json(workspace, pid).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    run2 = pipe.start("第2集", scenes=1, shots_per_scene=2)
    pipe.run(run2, stages=["script", "storyboard"])
    assert store.get_episode(run2.run_id).character_by_name("苏蔓").desc == \
        "女，31岁，齐耳短发，左手有疤", "改了圣经但下一集没生效"


def test_running_against_a_missing_project_stops_instead_of_degrading(workspace: Path):
    """--project 打错字，绝不能静默降级成"每集重新造角色"。"""
    pipe, _ = _pipe(workspace, "proj_typo")
    run = pipe.start("随便一集", scenes=1, shots_per_scene=1)
    with pytest.raises(FileNotFoundError) as ei:
        pipe.run(run, stages=["script"])
    assert "proj_typo" in str(ei.value)


@pytest.mark.ffmpeg
def test_episode_numbers_come_from_the_run_list_not_a_counter(workspace: Path,
                                                              need_ffmpeg: None):
    """集号只是显示用，但"第 1 集显示成第 2 集"是用户一眼能看见的错。"""
    proj = project.new_project("深夜外卖")
    proj.save(workspace)
    pipe, _ = _pipe(workspace, proj.project_id)
    r1 = pipe.start("第一集", scenes=1, shots_per_scene=2)
    r2 = pipe.start("第二集", scenes=1, shots_per_scene=2)
    assert r1.meta["episode_no"] == 1
    assert r2.meta["episode_no"] == 2
    # 单集模式不带任何项目标记
    plain, _ = _pipe(workspace)
    assert "project_id" not in plain.start("单集", scenes=1, shots_per_scene=1).meta


# ————————————————————————————————————————————----
# 音色
# ————————————————————————————————————————————----


class _RecordingAudio(DryRunAudioProvider):
    name = "recording-audio"

    def __init__(self) -> None:
        super().__init__()
        self.voices: List[str] = []

    def tts(self, text: str, dest: Path, **kw: Any):
        self.voices.append(str(kw.get("voice")))
        return super().tts(text, dest, **kw)


@pytest.mark.ffmpeg
def test_each_character_keeps_their_own_voice(workspace: Path, need_ffmpeg: None):
    """声音比脸更容易被记住 —— 第 3 集换了音色会被直接说成"配音换人了"。"""
    proj = project.new_project("深夜外卖")
    proj.characters.append(Character(name="苏蔓", desc="女，31岁"))
    proj.voice_map["苏蔓"] = "female_02"
    proj.save(workspace)

    audio = _RecordingAudio()
    pipe, store = _pipe(workspace, proj.project_id, audio=audio)
    run = pipe.start("第1集", scenes=1, shots_per_scene=3)
    pipe.run(run, stages=["script", "audio"])

    ep = store.get_episode(run.run_id)
    expect = []
    for shot in ep.shots:
        if not shot.dialogue.strip():
            continue
        speaker = shot.dialogue.split("：")[0].strip()
        expect.append("female_02" if speaker == "苏蔓" else "female_01")
    assert audio.voices == expect or sorted(audio.voices) == sorted(expect), (
        f"音色没按角色走：{audio.voices}"
    )


def test_speaker_of_only_matches_the_speaker_prefix_pattern():
    from ai_drama.core.media import speaker_of

    assert speaker_of("苏蔓：（低声）不对。") == "苏蔓"
    assert speaker_of("LIN WAN: Who are you?") == "LIN WAN"
    # 没有说话人前缀 = 空串，退回全局音色，而不是把半句话当人名
    assert speaker_of("没有前缀的台词。") == ""
    assert speaker_of("") == ""
