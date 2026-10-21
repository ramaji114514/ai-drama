"""阶段 2：分镜定稿。

这是**确定性骨架**和**创意判断**之间的接缝，也是整个项目最该由你来掌控的一层：

  * 时长：由 core/media.estimate_shot_seconds 用公式算（台词长度 + 景别），
    不是让模型随口给一个数字。你要调节奏，改公式参数，全片一起变。
  * 提示词：把"风格 + 角色外观 + 景别 + 地点 + 动作"拼成画面提示词；
    运动单独拼成视频提示词。
  * **听觉信息绝不进画面提示词** — 参考项目把 audio_desc 直接拼进了
    video prompt（script2video.py:190），这是概念性错误：
    你没法让画面模型"表演出"台词，只会污染画面。
  * **参考锁定**：把参考图**在提示词里点名**（`@图片1 = 角色「X」本人…`）
    并显式排除资产图版式。只把图传给模型不等于它会用 —— aigc-agent 的
    现场记录是"参考图传了也漂"。锁定段由 core/identity 生成。
  * 定妆照：为每个角色生成一张参考图，后续所有镜头复用同一张，
    这是**最低成本的跨镜头一致性方案**。

创意侧的提示词模板在 skills/storyboard.md，本文件只做拼装与校验。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..core import cast, paths
from ..core.context import StageContext, StageReport
from ..core.faces import matches_character
from ..core.identity import UNLOCKED_HEAD, shot_identity_prompt
from ..core.media import estimate_shot_seconds, preferred_shot_seconds
from ..core.models import Character, Shot
from ..providers.base import caps_of

# 景别 -> 画面提示词片段
FRAMING_PROMPT = {
    "establishing": "远景定场，环境为主，人物很小",
    "wide": "全景，人物全身与环境关系清晰",
    "medium": "中景，人物腰部以上",
    "close": "特写，面部与情绪为主",
    "extreme_close": "极特写，局部细节充满画面",
}

CAMERA_MOVE = {
    "establishing": "镜头缓慢推进",
    "wide": "镜头稳定横移",
    "medium": "轻微手持跟随",
    "close": "固定机位，极缓推近",
    "extreme_close": "极缓推近后停顿",
}

NEGATIVE = "文字, 字幕, 水印, 畸形手指, 多手多脚, 面部崩坏, 低分辨率, 模糊"

# 负向词分三类，缺一不可：
#   1. 解剖 —— 手和脸是图像模型最先崩的地方，也是观众第一眼看的地方
#   2. 版面 —— 文字/水印/字幕一旦出现，这一张就废了，只能重新出
#   3. 画质 —— 过曝、糊、塑料皮肤，属于"看着不对但说不出哪儿不对"
_NEG_ANATOMY = ("畸形手指", "多余手指", "多手多脚", "肢体重复", "五官错位",
                "面部崩坏", "皮肤塑料感")
_NEG_LAYOUT = ("文字", "字幕", "水印", "logo", "签名", "边框", "分屏")
_NEG_QUALITY = ("低分辨率", "模糊", "过曝", "噪点", "色块")
#: 视频专属。坏在**时间维度**上，静态图里根本不会有这几类事故，
#: 混进图片负向里就只是噪声。
_NEG_MOTION = ("人物变脸", "五官漂移", "肢体扭曲", "多余人物", "画面闪烁",
               "跳帧", "镜头穿模", "背景突变", "动作僵住不动")

#: action 自己开头就写了景别时，骨架不要再补一句：
#: "极特写，局部细节充满画面，俯拍特写：……" 这种重复会让模型试着把
#: 两个景别都装进画面（实测出来是画中画）。
_FRAMING_WORDS = ("特写", "近景", "中景", "全景", "远景", "俯拍", "仰拍", "航拍")


def _prompts_look_stale(shot: Shot) -> bool:
    """提示词还是不是按照现在的 action 写出来的。

    以前只判 `not shot.image_prompt`：一旦拼过一次就永远不再拼，于是手改
    `episode.json` 里的动作/景别再 resume，提示词还是旧的 —— 页面显示新动作、
    出图还是老画面，这是最难查的那种"看起来生效了"。

    判据很保守：只认"现在的动作文字没有出现在提示词里"。网页上改过的提示词
    （哪怕改得和动作无关）不会被这条判据掀掉，因为动作原文还在里面。
    """
    if not (shot.image_prompt or "").strip():
        return True
    head = _tidy(shot.action or "")
    if not head:
        return False
    probe = head[:12]
    return probe not in _tidy(shot.image_prompt)


def _action_already_names_framing(action: str) -> bool:
    """action 开头那一小句是不是已经交代了景别。

    看的是**第一个小句**（逗号/句号/冒号之前），不是固定字数：
    "俯拍全景：雨水在霓虹招牌上炸开" 里的景别在寸步之内，而
    "他把伞收起来，抬头看着招牌，镜头缓缓推成特写" 里的"特写"是动作的
    一部分、不是取景指令 —— 固定取前 16 字两头都会判错，短句漏、长句冤。
    """
    head = (action or "").strip()
    for sep in ("，", ",", "。", "；", ";", "：", ":"):
        head = head.split(sep)[0]
    return any(w in head for w in _FRAMING_WORDS)


def _join_neg(groups) -> str:
    out: List[str] = []
    for group in groups:
        for word in group:
            if word not in out:
                out.append(word)
    return "、".join(out)


def compose_negative_prompt(ep, shot: Shot) -> str:
    """首帧/关键帧的负向提示词。

    为什么单独成段而不是拼进正向：正向提示词里每个名词都会被当成"要画的东西"，
    模型只认名词、不太认否定。所以"不要水印"写进正向，经常换回来一个水印。
    可灵、即梦、Sora、Veo 都有独立的负向输入框，这段就是喂那个框的。
    """
    # 空镜不放解剖类负向：画面里本来没有人，写"畸形手指"反而把模型往人上引。
    if characters_on_screen(ep, shot):
        return _join_neg([_NEG_ANATOMY, _NEG_LAYOUT, _NEG_QUALITY])
    return _join_neg([_NEG_LAYOUT, _NEG_QUALITY])


def compose_video_negative(shot: Shot) -> str:
    """视频的负向提示词 —— 只放"动起来才会坏"的那几种，加上版面。

    时长越长，变脸和闪烁越容易发生，所以它跟 `shot.duration` 不是无关的：
    这里不按时长加词（模型不认"几秒内别变脸"），但重跑时长时这段不必动。
    """
    return _join_neg([_NEG_MOTION, _NEG_LAYOUT])



def characters_by_text(ep, shot: Shot) -> List[Character]:
    """**猜**这一镜有谁：在镜头文本里找角色名（或别名）的子串。

    [!] 这是已知的薄弱点（参考项目就是这么做的，多角色场景立即失效），
    现在只作为**退路**用：

      * 分镜阶段显式给了 `shot.characters` -> 用那个，见 declared_characters()
      * 没给（旧 episode.json、或模型漏了这个字段）-> 才退到这里，
        stage02 会把"靠猜"的镜数报出来，不会让它悄悄发生。
    """
    blob = f"{shot.action} {shot.dialogue}"
    labels: List[str] = []
    for c in ep.characters:
        for lbl in c.labels():
            if lbl not in labels:
                labels.append(lbl)
    hits = []
    for c in ep.characters:
        where = [blob.find(lbl) for lbl in c.labels() if matches_character(blob, lbl, labels)]
        if where:
            hits.append((min(where), c))
    return [c for _, c in sorted(hits, key=lambda t: t[0])]


def resolve_declared(ep, shot: Shot):
    """解析 `shot.characters`：返回 (命中的角色, 不认识的, 有歧义的)。

    引用可以是**角色 id、名字或别名** —— 项目模式下模型抄的是角色表里的 id
    （所以改名不会让引用失联），但它写名字、写化名、甚至抄错，也都不该让
    这一镜静默丢掉参考图。降级会发生，代价是这里要把话说出来。

    `characters is None`（没声明）和 `[]`（声明"画面里没有人"）是两回事，
    只有前者才返回空并让调用方退回去猜。
    """
    if shot.characters is None:
        return [], [], []
    found, _stats, problems = cast.resolve_all(shot.characters, ep.characters)
    # 返回**原始引用**而不是整句话：调用方要拼自己的报告文案
    # （详细的 `Problem.detail` 在 core.cast 里，需要时再取）。
    unknown = [p.ref for p in problems if p.how == cast.HOW_UNKNOWN]
    ambiguous = [p.ref for p in problems if p.how == cast.HOW_AMBIGUOUS]
    return found, unknown, ambiguous


def declared_characters(ep, shot: Shot):
    """老签名：返回 (命中的角色, 解析不了的说明)。"""
    found, unknown, ambiguous = resolve_declared(ep, shot)
    return found, unknown + ambiguous


def characters_in_shot(ep, shot: Shot) -> List[Character]:
    """找出镜头里出现的**全部**角色，按"谁更重要"排序。

    三处刻意设计：

    1. **显式声明优先。** 分镜阶段给了 `shot.characters` 就用它，顺序即模型
       排定的重要性 —— "这张戏是谁的"应当由剧本决定，不由字符串查找决定。
       声明为空数组时结果是空的：**空镜就是空镜**，不许退回去猜，
       否则"我声明了画面里没人"这句话就没有意义了。
    2. 真的没声明（旧 episode.json、模型漏字段）才退回名字子串匹配，
       并且复用 `core.faces.matches_character`，它处理了 `朱锦` 与 `朱锦娘`
       这种包含关系（否则娘的脸会当成锦的基准）。stage02 会把"靠猜"的镜数报出来。
    3. 返回值里只含**在册角色**：模型点了一个不在卡司里的名字，那一镜就没有
       参考图，但这件事会在 stage02 的报告里被点名，不会静默。

    引用的解析走 `core.cast`：**按身份（char_id）认人**，名字与别名只是这个
    身份的叫法。于是同名不同人不会被折成一个人，改名也不会让前几集失联。
    """
    if shot.characters is not None:
        return declared_characters(ep, shot)[0]
    return characters_by_text(ep, shot)


def characters_on_screen(ep, shot: Shot) -> List[Character]:
    """**可能**出现在画面里的所有角色：声明与文本推断的并集。

    为什么要并集而不是只用声明：`max_ref_images` 的拥挤告警必须建立在
    "这一镜到底有几个人"上。模型少声明一个人（很常见），如果告警也跟着变少，
    那这条告警就变成了"只要模型不写就永远不报警"——比没有还糟。
    """
    out = list(characters_in_shot(ep, shot))
    for c in characters_by_text(ep, shot):
        if c not in out:
            out.append(c)
    return out


def character_in_shot(ep, shot: Shot) -> Optional[Character]:
    """镜头里的**第一个**角色 —— 也就是关键帧拿谁的参考图。"""
    found = characters_in_shot(ep, shot)
    return found[0] if found else None


def reference_labels(ep, shot: Shot, max_refs: int = 1) -> List:
    """这一镜要在提示词里点名哪些参考图。

    **必须与 stage03 真正传给模型的图片同序同数**：多写一个 `@图片2`
    而只传了一张图，模型不会报错 —— 它会以为第二张就是它想象的任何东西。
    上限取 provider 自报的 `caps.max_ref_images`（见 core/catalog）。
    """
    found = [c for c in characters_in_shot(ep, shot) if c.ref_image]
    return [("角色", c.name) for c in found[:max(0, max_refs)]]


#: 拼接前要先剥掉的句尾标点。**这是真实输出里暴露出来的问题**：
#: 模型写的 style/action 大多自带句号，直接 `"，".join(...)` 会拼出
#: "……强调窗框与猫的凝视。，角色 灰灰：……" 这种双标点。图生图模型
#: 不会为标点报错，它只会把这一坨当成要画的内容去理解 —— 画面里没人在意
#: 逗号，但"句号+逗号"这种异常输入会白占注意力。所以先剥、后统一补。
_TAIL_PUNCT = "。．.，,、；;：:！!？?… \t\r\n"


def _tidy(text: str) -> str:
    """去掉首尾空白与句尾标点，留给拼接处统一收口。"""
    return (text or "").strip().rstrip(_TAIL_PUNCT).strip()


def _join_parts(parts) -> str:
    """用中文逗号把若干段接成一句，剥掉每段自带的句尾标点。

    空段（模型没给 style、没有角色锚点）直接丢掉，免得拼出"，特写，……"。
    """
    return "，".join(p for p in (_tidy(x) for x in parts) if p)


def unlocked_names(ep, shot: Shot, max_refs: int = 1) -> List[str]:
    """出镜、但**拿不到**参考图的角色名 —— 提示词里要当成"文字描述出来的角色"。

    provider 只吃 N 张参考图，第 N+1 个人照样站在画面里。不给模特图又不声明，
    模型唯一的线索是"这里有个有名字的人"，它会现造一张脸，而且每个镜头造得
    都不一样 —— 成片里看起来像人物漂移，其实是没告诉他这人长什么样。
    """
    chars = characters_in_shot(ep, shot)
    locked = [c for c in chars if c.ref_image][: max(0, max_refs)]
    locked_names_ = {id(c) for c in locked}
    # **没有定妆照的角色也要算进来**：它才是最需要"文字描述"声明的那个人。
    # 只筛 `c.ref_image` 等于把"根本锁不了脸的人"从提示词里整个漏掉，
    # 而模型这时连一个"这个人长什么样"的线索都没有。
    return [c.name for c in chars if id(c) not in locked_names_]


def compose_image_prompt(ep, shot: Shot, labels=(), unlocked=()) -> str:
    parts = [ep.style or ""]
    for ch in characters_in_shot(ep, shot):
        parts.append(f"角色 {ch.name}：{ch.desc}")
    # 景别是"这一镜要怎么取景"，action 里往往已经写过了（"俯拍特写：……"）。
    # 两句都留，模型会以为要同时满足两个景别。
    if not _action_already_names_framing(shot.action):
        parts.append(FRAMING_PROMPT.get(shot.framing, "中景"))
    parts.append(shot.action)
    body = _join_parts(parts)
    return shot_identity_prompt(body, labels, unlocked)


def compose_last_frame(ep, shot: Shot, labels=(), unlocked=()) -> str:
    """尾帧提示词 —— 这一镜**结束那一刻**的画面。

    与 compose_image_prompt 对称（同样的风格、角色锚点、景别），只把中间那句
    从 action 换成 end_action。**没有 end_action 就退回 action**，因为老项目
    和没照新 schema 写的模型都不会给这个字段；退回会让尾帧变成首帧的复述，
    所以这里不假装它等价 —— 调用方会把"退化了"记进报告。
    """
    end = shot.end_action or shot.action
    parts = [ep.style or ""]
    for ch in characters_in_shot(ep, shot):
        parts.append(f"角色 {ch.name}：{ch.desc}")
    if not _action_already_names_framing(end):
        parts.append(FRAMING_PROMPT.get(shot.framing, "中景"))
    parts.append(end)
    body = _join_parts(parts)
    return shot_identity_prompt(body, labels, unlocked)


def compose_video_prompt(shot: Shot) -> str:
    """运动提示词 — 只描述摄影机和画面元素的运动，不含任何听觉信息。

    这里**不加参考锁定段**，是有意的：视频 provider 收到的是这一镜的
    关键帧（`request.image_path`），而关键帧的构图正是我们要的画面本身。
    把资产图那套 "不要把白背景和站姿搬进来" 的排除条款套上去，
    只会让模型去改掉一个本来就对的构图。

    什么时候该加：如果哪天视频侧改成拿**角色资产图**做参考，
    就要用 identity.reference_block，并且注意顺序与图片列表对齐。

    为什么要写"匀速完成"：不写节奏，模型会在长镜头里自己发挥 —— 前 2 秒
    演完整段动作，剩下 13 秒人物在画面里呆着；或者反过来拖到最后才动。
    这不是提示词写得不够美，是它不知道这段有多长。
    """
    move = CAMERA_MOVE.get(shot.framing, "镜头缓推")
    seconds = float(shot.duration or 0.0)
    if seconds >= 1:
        pace = f"{seconds:.0f} 秒内匀速完成，不加速也不放慢"
    else:
        pace = "匀速完成，不加速也不放慢"
    return f"{_tidy(shot.action)}。{move}，{pace}。"


def _dryrun_portrait_feeding_a_real_model(ctx: StageContext, portraits_in_play: int) -> bool:
    """有没有"占位定妆照喂给真图像模型"这回事。

    要同时满足三条才算：这一集确实牵涉到定妆照、定妆照是 dryrun 出的、
    而关键帧那一步换成了真模型。单看任何一条都不该报警
    （全 dryrun 跑通一遍是完全正常的用法）。
    """
    if not portraits_in_play:
        return False
    t2i = ctx.bundle.t2i
    image = ctx.bundle.image
    if t2i is None or image is None:
        return False
    # 用名字前缀认"这是不是占位实现"，跟 registry 的 `dryrun-<kind>` 命名一致
    # （stage06 认 dryrun-lipsync 是同一套约定）。
    return t2i.name.startswith("dryrun") and not image.name.startswith("dryrun")


def run(ctx: StageContext) -> StageReport:
    report = StageReport(stage="storyboard")
    ep = ctx.episode
    if not ep.scenes:
        report.notes.append("没有分镜可定稿（先跑 script 阶段）")
        return report

    # ————————————————————————————————-- 定妆照
    #
    # 顺序很重要：参考图得先存在，提示词才能**点名**它。反过来写的话，
    # 第一次跑时 ch.ref_image 全是空的，参考锁定段会静默地一个字都不写 ——
    # 而那正是"参考图传了也漂"的成因。
    #
    # 有项目时，定妆照存在**项目**目录并跨集复用同一个文件。这才是跨集
    # 一致性的硬保证：第 40 集的脸和第 1 集是同一张图，而不是"提示词写得像"。
    pid = ctx.project.project_id if ctx.project is not None else ""
    portraits = 0
    reused = 0                     # 项目里已有、这一集直接复用的
    warned_no_t2i = False          # 只在第一次缺 provider 时提示，别每个角色刷一行
    for ch in ep.characters:
        if not ch.anchor_prompt:
            ch.anchor_prompt = f"{ep.style}，人物定妆照，{ch.desc}"
        run_level = paths.portrait(ctx.root, ctx.run.run_id, ch.name)
        canon = paths.project_portrait(ctx.root, pid, ch.name) if pid else None

        # 圣经里记着的那张脸在哪。**改名字之后文件名不会跟着改**，所以不能只按
        # 名字推路径：那样"改名"就等于"换脸"（第 2 集重新生成一张，而报告里
        # 还写着"沿用项目里的"）。这也正是这条 id 线索要修的同一类错。
        known = Path(ch.ref_image) if ch.ref_image else None
        if known is not None and not (known.is_file() and known.stat().st_size > 0):
            known = None
        have = canon if (canon is not None and canon.exists()
                         and canon.stat().st_size > 0) else None

        # 1) 这个人的脸已经有了 -> 直接复用。**不重新生成**：既不花钱，也不可能漂。
        picked = known or have
        if picked is not None and not ctx.force:
            ch.ref_image = str(picked)
            if ctx.project is not None:
                # 按身份记，这样别名/改名都不会让这条记录走丢
                ctx.project.remember_portrait(ch.char_id or ch.name, str(picked))
            # 也记进资产台账：**"这件东西是哪一次 run 造的"和"哪些 run 用过它"
            # 是两个问题**。复用时同样记一条（不填 provider —— 这一集确实没生成
            # 它，填个厂商名等于编），这样反查"第 2 集用的这张脸出自哪里"有答案。
            ctx.asset(picked, kind="portrait", via="t2i",
                      meta={"character": ch.name, "char_id": ch.char_id or "",
                            "reused": True})
            reused += 1
            continue

        # 2) 单集模式的退路：本 run 里已经生成过
        if canon is None and run_level.exists() and not ctx.force:
            ch.ref_image = str(run_level)
            ctx.asset(run_level, kind="portrait", via="t2i",
                      meta={"character": ch.name, "char_id": ch.char_id or "",
                            "reused": True})
            report.skipped += 1
            continue

        if not ctx.bundle.t2i:
            if not warned_no_t2i:
                warned_no_t2i = True
                report.notes.append(
                    "未配置 t2i provider（AI_DRAMA_T2I），已跳过定妆照。"
                    "接真图像模型时建议单独配一个，定妆照和人像一致性关系很大。"
                )
            continue
        # 项目模式下**不拿 run 级的那张顶替**：那一张可能是"项目建立之前"
        # 由模型自己写的外貌生成的，脸未必对。宁可付一次生成费，也不要把
        # 一张可能是错的脸固化进圣经 —— 固化之后会一路错到第 100 集。
        dest = canon if canon is not None else run_level
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            usage = ctx.bundle.t2i.png(ch.anchor_prompt, dest, size=ctx.frame_size,
                                       shot_label=f"portrait/{ch.name}")
            ctx.ledger.add(usage, stage="storyboard")
            report.add_usage(usage)
            ch.ref_image = str(dest)
            if ctx.project is not None:
                ctx.project.remember_portrait(ch.char_id or ch.name, str(dest))
            ctx.asset(dest, kind="portrait", via="t2i", prompt=ch.anchor_prompt,
                      meta={"character": ch.name, "char_id": ch.char_id or "",
                            "reused": False})
            portraits += 1
        except Exception as exc:                      # noqa: BLE001
            # 定妆照失败不应该让整片停摆，但必须留痕
            report.notes.append(f"定妆照失败 {ch.name}: {type(exc).__name__}: {exc}")
            report.failed += 1

    if ctx.project is not None:
        ctx.save_project()

    # ————————————————————————————————-- 提示词与时长
    max_refs = int(getattr(caps_of(ctx.bundle.image), "max_ref_images", 1) or 1)
    locked = 0
    crowded = 0
    changed = 0
    declared = 0                 # 出镜角色由分镜显式给出的镜头数
    guessed = 0                  # 靠名字子串猜的（旧 episode.json / 模型漏字段）
    by_name = 0                  # 声明里靠名字/别名认出来的引用数（降级，要报）
    unknown: List[str] = []      # 声明了但不在卡司里的引用
    ambiguous: List[str] = []    # 声明了对上多个角色的引用（同名不同人）
    preferred = preferred_shot_seconds(ep)
    for shot in ep.shots:
        labels = reference_labels(ep, shot, max_refs)
        if labels:
            locked += 1
        if shot.characters is not None:
            declared += 1
            _found, bad, clash = resolve_declared(ep, shot)
            unknown.extend(n for n in bad if n not in unknown)
            ambiguous.extend(n for n in clash if n not in ambiguous)
            # 引用本可以写 id，写名字也能认出来 —— 但那是降级，报出来。
            _f, stats, _p = cast.resolve_all(shot.characters, ep.characters)
            by_name += stats[cast.HOW_NAME] + stats[cast.HOW_ALIAS]
        elif characters_by_text(ep, shot):
            # 只有真的猜出人来了才算"靠猜" —— 空镜本来就该是空数组
            guessed += 1
        # 出镜人数超过 provider 能吃的参考图数时，多出来的人**锁不住**。
        # 这不会报错，只会让人在成片里看到一张陌生的脸 —— 所以要说出来。
        # 这里用**并集**（声明 ∪ 文本推断）：模型少声明一个人不能让这条告警
        # 跟着消失，否则它就变成"只要模型不写就永远不报警"——比没有还糟。
        if len(characters_on_screen(ep, shot)) > max_refs:
            crowded += 1
        if _prompts_look_stale(shot) or ctx.force:
            unlocked = unlocked_names(ep, shot, max_refs)
            shot.image_prompt = compose_image_prompt(ep, shot, labels, unlocked)
            shot.video_prompt = compose_video_prompt(shot)
            shot.last_frame = compose_last_frame(ep, shot, labels, unlocked)
            shot.negative_prompt = compose_negative_prompt(ep, shot)
            shot.video_negative = compose_video_negative(shot)
            changed += 1
        # 时长统一在这里复核：模型给的值不可信，公式给的值才可复现。
        # preferred 跟着整部目标走（均摊到每镜），没定目标就是默认片长。
        refined = estimate_shot_seconds(shot, preferred_s=preferred)
        if abs(refined - shot.duration) > 0.01:
            shot.duration = refined
            changed += 1

    ctx.store.save_episode(ep)
    ctx.store.sync_shots(ctx.run.run_id, ep)
    ep_json = paths.episode_json(ctx.root, ctx.run.run_id)
    ep_json.write_text(json.dumps(ep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    report.processed = changed
    report.artifacts["episode"] = str(ep_json)
    head = f"定稿 {len(ep.shots)} 镜 / 总时长 {ep.total_duration}s / 定妆照 {portraits} 张"
    if ep.target_seconds > 0:
        gap = ep.total_duration - ep.target_seconds
        # 说清差在哪：均摊是愿望，台词是硬下限（读不完的话不能剪掉一半）
        if abs(gap) < 0.05:
            head += f"（目标 {ep.target_seconds:g}s，正好）"
        else:
            head += (f"（目标 {ep.target_seconds:g}s，"
                     f"{'多' if gap > 0 else '少'} {abs(gap):.1f}s）")
    if reused:
        head += f"（另有 {reused} 张沿用项目里的，未重复生成）"
    lock = f"参考锁定 {locked}/{len(ep.shots)} 镜（provider 单镜最多 {max_refs} 张参考图）"
    report.notes.append(head)
    report.notes.append(lock)
    if ep.target_seconds > 0 and preferred < 2.0:
        report.notes.append(
            f"[!] 目标 {ep.target_seconds:g}s ÷ {len(ep.shots)} 镜 = {preferred:.2f} 秒/镜，"
            "低于 2 秒下限 —— 再短的镜头说话都来不及。实际会明显超过目标。"
            "要么减镜数（--scenes/--shots），要么把目标调大。"
        )
    if ep.target_seconds > 0 and ep.total_duration > ep.target_seconds * 1.2 + 0.5:
        report.notes.append(
            f"[!] 实际 {ep.total_duration}s 超目标 {ep.target_seconds:g}s 约 "
            f"{(ep.total_duration - ep.target_seconds) / ep.target_seconds:.0%}："
            "台词长度是硬下限，超出来的部分只能靠删台词或减镜头消化。"
        )
    if _dryrun_portrait_feeding_a_real_model(ctx, portraits + reused):
        # 这条不是"配置不全"的提醒，是一条**会花钱的错误**：
        # 占位定妆照是个写着提示词的卡片，真图像模型会老老实实把那张卡片当底图去改，
        # 于是每一帧都长着一张"文字卡片脸"，而且看起来像是模型出的图 ——
        # 钱花了，脸也没锁住，报告里却一切正常。
        report.notes.append(
            "[!] 定妆照用的是 dryrun 占位图，而关键帧要用真模型生成 —— "
            "真模型会把那张写着提示词的卡片当底图去改，出来的每一帧都带着卡片的样子，"
            "脸也锁不住。花这个钱之前先把 AI_DRAMA_T2I 也配上，"
            "或者删掉项目里 portraits/ 下已有的占位图重跑。"
        )
    if unknown:
        # 引用对不上角色表，这一镜就锁不住脸 —— 而它不会报错
        shown = "、".join(unknown[:4]) + ("…" if len(unknown) > 4 else "")
        report.notes.append(
            f"[!] 分镜声明的出镜角色里有 {len(unknown)} 个不在卡司里：{shown}。"
            "这些镜头拿不到参考图，脸锁不住 —— 多半是名字没写对（对不上角色表）"
        )
    if ambiguous:
        # 同名不同人：**不猜**，宁可这一镜没有参考图，也不要给错脸
        shown = "、".join(ambiguous[:4]) + ("…" if len(ambiguous) > 4 else "")
        report.notes.append(
            f"[!] 分镜声明的出镜角色里有 {len(ambiguous)} 个对上了多个角色，"
            f"已按「不猜」处理（这些引用不锁脸）：{shown}。"
            "把镜头里的引用改成角色表里的 id 就能定下来 —— 名字对身份不是一对一"
        )
    if declared or guessed:
        cast_note = f"出镜角色：{declared} 镜由分镜显式给出"
        if by_name:
            cast_note += f"（其中 {by_name} 个引用是名字/别名，不是 id）"
        if guessed:
            cast_note += f"，{guessed} 镜靠名字子串猜（旧 episode.json 或模型漏了 characters 字段）"
            report.notes.append(
                f"[!] {cast_note}。猜的方式在多角色同框时会错，"
                "重跑 script 阶段可以补上显式声明"
            )
        else:
            report.notes.append(cast_note)
    if crowded:
        report.notes.append(
            f"[!] {crowded} 个镜头出镜人数超过参考图上限（{max_refs}），"
            f"多出来的人锁不住脸。提示词里已写明「{UNLOCKED_HEAD}」那段："
            "ta 靠文字描述生成、不许套用 @图片1 的长相。"
            "要真正锁住脸，只能拆成单人镜头，或换支持更多参考图的模型"
        )
    # 尾帧退化成首帧的复述时必须说出来。不说的话，用户拿去做首尾帧工具，
    # 两端会是同一个画面 —— 那不是"效果差一点"，是这一镜根本没动。
    no_end = sum(1 for s in ep.shots if not (s.end_action or "").strip())
    if no_end:
        report.notes.append(
            f"[!] 尾帧：{no_end}/{len(ep.shots)} 镜没有 end_action（模型没写这个字段），"
            "尾帧只能退化成这一镜起始画面的复述 —— 首尾两帧会是同一个画面，"
            "等于没有尾帧。重跑 script 阶段可以让模型补上"
        )
    ctx.log(f"[storyboard] [OK] {head}")
    ctx.log(f"[storyboard] {lock}")
    for note in report.notes:
        if note.startswith("[!]"):
            ctx.log(f"[storyboard] {note}")
    return report
