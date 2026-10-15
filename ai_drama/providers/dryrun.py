"""dryrun providers — 零成本、零 API key、能端到端出片。

这是整个骨架的"假厂商"，但它**不是 mock**：

  * 它真的产出一个可以播放的 mp4（关键帧是渲染出来的真图，片段是真编码的）。
  * 它的成本账是**按真实定价表算出来的数字**，所以预算逻辑可以被真实验证。
  * 它是**确定性**的：同一个 idea 永远产出分镜一致的结果，
    方便你写回归测试、对比两次重跑有没有变。

它的用途是让你在**还没决定用哪家模型之前**就能验证骨架本身：
镜头表合不合理、节奏对不对、成本记账准不准、断点续跑能不能恢复。
接真厂商时只换 provider，这一层原样保留当测试夹具。

[!]️ 反模式对照：参考项目（microdrama）在缺少密钥时**静默返回一份写死的假剧本**，
表现为"任务成功但永远出同一部片"。这里刻意相反 — dryrun 必须被**显式选择**
（AI_DRAMA_TEXT=dryrun），且每一层输出都带 [dryrun] 标记，绝不伪装成真结果。
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media
from ..core.models import Character, Episode, Scene, Shot
from .base import (
    PRICING,
    AudioProvider,
    ImageProvider,
    LipSyncProvider,
    ProviderCaps,
    ProviderError,
    T2IProvider,
    TextProvider,
    VideoProvider,
    estimate_tokens,
)
from .registry import register


# ————————————————————————————————————————----
# 确定性随机源
# ————————————————————————————————————————----


def _seed_of(text: str) -> int:
    return int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)


def _pick(pool: List[str], seed: int, offset: int = 0) -> str:
    return pool[(seed + offset * 7919) % len(pool)]


def _extract_idea(prompt: str) -> str:
    """从 stage01 拼好的指令里抠出「想法：xxx」那一行。

    指令是固定格式（见 stages/stage01_script.py 的 build_prompt），
    所以这里做一个很窄的解析；解析不到就返回空串，让调用方兜底。
    """
    for line in (prompt or "").splitlines():
        line = line.strip()
        if line.startswith("想法："):
            value = line[len("想法："):].strip()
            # 风格要求跟在想法后面（"想法：xxx\\n画面风格要求：yyy"），这里只取第一段
            return value.split("\n")[0].strip()
    return ""


STYLES = [
    ("都市悬疑", "冷调夜戏、霓虹反光、浅景深、手持微晃、电影感青橙色调"),
    ("古装权谋", "暖调烛光、丝绸质感、低饱和、端庄运镜、水墨留白"),
    ("职场现实", "自然光、干净构图、中景为主、克制表演、低对比"),
    ("科幻惊悚", "高对比、冷蓝主光、空间纵深构图、缓慢推轨、雾面质感"),
]

LOCATIONS = ["老城区出租屋", "写字楼地下车库", "深夜便利店", "医院走廊", "跨江大桥桥底"]
TIMES = ["深夜", "清晨", "黄昏", "雨天下午"]

ARCHETYPES = [
    ("林晚", "女", "28岁", "外表冷静、内心焦躁的调查记者，习惯用沉默观察别人"),
    ("陈默", "男", "34岁", "沉默寡言的技术员，袖口总沾着机油，说话从不超过两句"),
    ("苏蔓", "女", "31岁", "强势的项目负责人，笑的时候眼睛不笑"),
]

CLOSERS = [
    "手机在口袋里震了一下。",
    "窗外有车灯扫过天花板。",
    "远处传来一声很轻的响动。",
    "桌上那杯水，还在晃。",
]

CAMERA_MOVES = {
    "establishing": "缓慢推轨推进",
    "wide": "稳定横移",
    "medium": "轻微手持跟随",
    "close": "固定机位微推",
    "extreme_close": "极缓推进后停顿",
}


def _second_archetype(seed: int, hero_name: str) -> tuple:
    """挑第二个角色，**保证名字不与主角重名**。

    [!] 这件事必须在这里做。曾经两个角色同名（都由同一个种子下标选出），
    后果是三重的、而且全都不报错：
      1. 定妆照写到同一个文件，第二个角色静默复用第一个的脸；
      2. 参考锁定段里 `@图片1 = 角色「林晚」` 指不出是哪一个林晚；
      3. 分镜里"林晚"这个名字同时命中两个角色，`character_by_name` 只留一个。
    真厂商生成的剧本同样可能出现重名，所以 core/faces 那层还要能裁决
    （朱锦 / 朱锦娘 的包含关系就是这么处理的）。
    """
    names = [a[0] for a in ARCHETYPES]
    idx = (seed // 11 + 1) % len(ARCHETYPES)
    for _ in range(len(ARCHETYPES)):
        if ARCHETYPES[idx][0] != hero_name:
            return ARCHETYPES[idx]
        idx = (idx + 1) % len(ARCHETYPES)
    # 整个表里只有一个名字时无解 —— 退化成带序号的别名，至少不撞车
    return (f"{names[0]}B",) + ARCHETYPES[0][1:]


# ————————————————————————————————————————----
# 文本 provider：规则化的编剧引擎
# ————————————————————————————————————————----


@register("dryrun", "text")
class DryRunTextProvider(TextProvider):
    """把一句 idea 展开成结构化分镜。纯规则，无网络，确定性。"""

    name = "dryrun-text"

    SYSTEM = (
        "你是短剧编剧。输出严格 JSON。"
        "镜头提示词里绝对不允许出现台词、音效、音乐、旁白等听觉信息 — "
        "画面提示词只描述看得见的东西。对白单独放在 dialogue 字段里。"
    )

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 4000,
                 scene: str = "", scene_count: int = 3,
                 shots_per_scene: int = 4) -> Tuple[str, Dict[str, Any]]:
        """prompt 是 stages/stage01_script.py 拼好的完整指令。

        这里要从指令里把「想法：」那一行抠出来当片名种子 ——
        直接用整段 prompt 当 idea 的话，片名会变成
        "把一个想法展开成一部竖屏短剧的分镜表"，一眼就是机器填的。
        """
        idea = _extract_idea(prompt) or (prompt or "").strip() or "一个陌生来电"
        seed = _seed_of(idea)
        payload = self._build(idea, seed, int(scene_count or 3), int(shots_per_scene or 4))
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        in_tok = estimate_tokens(self.SYSTEM + idea)
        out_tok = estimate_tokens(text)
        money = (
            in_tok / 1000 * PRICING["llm_per_1k_token"]["in"]
            + out_tok / 1000 * PRICING["llm_per_1k_token"]["out"]
        )
        return text, {
            "unit": "token",
            "amount": float(in_tok + out_tok),
            "money": round(money, 6),
            "provider": self.name,
            "stage": scene or "text",
            "note": "[dryrun] 规则引擎生成，非真实模型输出",
        }

    # ————————————————————————————————----- 内部逻辑

    def _build(self, idea: str, seed: int, scene_count: int = 3,
               shots_per_scene: int = 4) -> Dict[str, Any]:
        style_name, style_desc = STYLES[seed % len(STYLES)]
        hero_name, hero_sex, hero_age, hero_desc = ARCHETYPES[(seed // 7) % len(ARCHETYPES)]
        second = _second_archetype(seed, hero_name)
        location = _pick(LOCATIONS, seed, 2)
        time_of_day = _pick(TIMES, seed, 3)

        characters = [
            {
                "name": hero_name,
                "desc": f"{hero_sex}，{hero_age}。{hero_desc}",
                "anchor_prompt": (
                    f"{style_name}风格人物定妆照，{hero_sex}性{hero_age}角色，{hero_desc}，"
                    f"半身，正面，中性表情，纯色背景，{style_desc}"
                ),
            },
            {
                "name": second[0],
                "desc": f"{second[1]}，{second[2]}。{second[3]}",
                "anchor_prompt": (
                    f"{style_name}风格人物定妆照，{second[1]}性{second[2]}角色，{second[3]}，"
                    f"半身，正面，中性表情，纯色背景，{style_desc}"
                ),
            },
        ]

        scene_count = max(1, min(int(scene_count or 3), 8))
        shots_per_scene = max(1, min(int(shots_per_scene or 4), 12))
        closers = [CLOSERS[(seed + i * 13) % len(CLOSERS)] for i in range(scene_count)]
        scenes: List[Dict[str, Any]] = []

        for si in range(scene_count):
            sid = f"sc{si + 1:02d}"
            (n1, n2, n3) = f"{hero_name}", second[0], "旁观者"
            template = ["引子：异常出现", "推进：确认与对峙", "爆发：逼到墙角"]
            scene_title = template[si] if si < len(template) else f"第 {si + 1} 场：继续逼近"
            one_line = [
                f"{hero_name}在一次寻常的日常里，撞见一个不该出现的细节。",
                f"{hero_name}开始追问，{n2}的态度反常。",
                f"真相被揭开一角，{hero_name}必须当场做出选择。",
            ][si] if si < 3 else f"事态继续升级，{hero_name}又失去了一个可以信任的人。"
            shots: List[Dict[str, Any]] = []
            pool = self._scene_shots(si, hero_name, second[0], location, closers[si])
            for oi in range(shots_per_scene):
                framing, action, line, on_screen = pool[oi % len(pool)]
                if oi >= len(pool):
                    # 要求的格数比模板多：复用景别但把动作标注成后续节拍，
                    # 避免出现"两镜一模一样的画面提示词"。
                    action = f"{action}（第 {oi + 1} 个节拍，机位与表演需与前镜区分）"
                shots.append({
                    "order": oi,
                    "framing": framing,
                    "action": action,
                    "dialogue": line,
                    "characters": list(on_screen),
                    "location": location if (si != scene_count - 1 or oi < shots_per_scene - 1) else "桥底-车边",
                })
            scenes.append({
                "scene_id": sid,
                "title": scene_title,
                "summary": one_line,
                "location": location,
                "time_of_day": time_of_day,
                "shots": shots,
            })

        return {
            "title": f"{idea[:12]}",
            "logline": f"{idea}。{style_name}质感，{scene_count} 场戏，悬念收尾。",
            "style": style_desc,
            "characters": characters,
            "scenes": scenes,
            "_dryrun": True,
        }

    def _scene_shots(self, si: int, hero: str, other: str, location: str,
                     closer: str) -> List[Tuple[str, str, str, List[str]]]:
        """返回 (景别, 动作, 台词, **画面里看得见的角色**)。

        第四个元素是 `characters` 字段 —— 骨架按它决定拿谁的定妆照锁脸。
        dryrun 是"零配置也能跑通"的参考实现，所以它要把**完整的契约**演出来，
        而不只是把 JSON 填满：空镜是空数组、单人镜只有一个人、
        群戏两个人都在里面。忽略这个字段也能跑，但那一镜就会退化成
        靠名字子串猜（报告里会写出来）。
        """
        if si == 0:
            return [
                ("establishing", f"{location}空镜，环境建立，光线偏暗", "", []),
                ("medium", f"{hero}独自停留，动作细微，像在确认什么", "", [hero]),
                ("close", f"{hero}的视线突然定住，瞳孔收缩", f"{hero}：（低声）不对。", [hero]),
                ("extreme_close", closer, "", [hero]),
            ]
        if si == 1:
            return [
                ("wide", f"{hero}与{other}隔着一张桌子对坐，空间关系紧张", "", [hero, other]),
                ("medium", f"{other}低头回避视线，手指敲桌面", "", [other]),
                ("close", f"{hero}前倾逼问，语气压得很低",
                 f"{hero}：你到底在瞒我什么？", [hero]),
                ("medium", f"{other}终于抬头，表情松动",
                 f"{other}：……知道的人，都走了。", [other]),
            ]
        return [
            ("wide", f"{location}，两人距离被骤然拉开，冷风灌进画面", "", [hero, other]),
            ("medium", f"{hero}后退半步，意识到危险逼近", "", [hero]),
            ("close", f"{hero}做出决定的瞬间，呼吸稳定下来",
             f"{hero}：那就从我开始。", [hero]),
            ("establishing", f"镜头拉远，人影变小，环境吞没一切。{closer}", "", []),
        ]


# ————————————————————————————————————————----
# 出图 provider
# ————————————————————————————————————————----

WINDOW_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

# 每个场景一个主色，方便你一眼看出镜头切换是否发生在正确的场景边界
SCENE_COLORS = {
    "sc01": ((14, 26, 48), (58, 92, 140)),
    "sc02": ((30, 14, 38), (120, 62, 120)),
    "sc03": ((44, 16, 18), (168, 74, 60)),
}


def _load_font(size: int):
    from PIL import ImageFont

    for p in WINDOW_FONTS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


@register("dryrun", "image")
class DryRunImageProvider(ImageProvider):
    """用 PIL 渲染一张"分镜卡"：渐变底 + 场景色带 + 文字覆盖。

    它不是占位符 — 它是**可读的分镜可视化**，让你不接任何模型就能判断
    镜头表是否合理、台词是否会撑爆时长。
    """

    name = "dryrun-image"

    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            ref_image: Optional[Path] = None, shot_label: str = "",
            scene: str = "") -> Dict[str, Any]:
        from PIL import Image, ImageDraw

        w, h = size
        path.parent.mkdir(parents=True, exist_ok=True)
        scene_key = shot_label.split("/")[0] if "/" in shot_label else "sc01"
        top, bottom = SCENE_COLORS.get(scene_key, ((20, 22, 30), (70, 80, 100)))

        img = Image.new("RGB", (w, h), top)
        d = ImageDraw.Draw(img)

        # 垂直渐变
        for y in range(h):
            t = y / max(1, h - 1)
            d.line(
                [(0, y), (w, y)],
                fill=(
                    int(top[0] + (bottom[0] - top[0]) * t),
                    int(top[1] + (bottom[1] - top[1]) * t),
                    int(top[2] + (bottom[2] - top[2]) * t),
                ),
            )

        # 顶部场景色带
        d.rectangle([0, 0, w, 10], fill=bottom)
        # 取景框
        d.rectangle([24, 24, w - 24, h - 24], outline=(255, 255, 255, 40), width=2)

        f_label = _load_font(46)
        f_body = _load_font(30)
        f_small = _load_font(22)

        d.text((44, 52), shot_label or "shot", font=f_label, fill=(255, 255, 255))
        d.text((44, 112), f"framing: {scene or '-'}", font=f_small, fill=(200, 210, 230))

        # 提示词正文（自动折行）
        body = prompt.strip()
        lines: List[str] = []
        for para in body.split("\n"):
            if not para.strip():
                lines.append("")
                continue
            # 中文按字符宽度折行
            width_chars = max(10, (w - 100) // 30)
            lines.extend(textwrap.wrap(para, width=width_chars) or [""])

        y = h // 2 - 60
        for ln in lines[:18]:
            d.text((50, y), ln, font=f_body, fill=(236, 240, 248))
            y += 42

        d.text((44, h - 74), "[dryrun] 非真实模型生成 - 仅用于验证分镜与节奏",
               font=f_small, fill=(150, 160, 180))
        img.save(path, "PNG")

        money = PRICING["image_per_png"].get(f"{w}x{h}", 0.03)
        return {
            "unit": "image",
            "amount": 1.0,
            "money": round(money, 6),
            "provider": self.name,
            "stage": "frames",
            "shot_label": shot_label,
            "note": "[dryrun] 本地渲染，未调用真实出图模型",
        }


@register("dryrun", "t2i")
class DryRunT2IProvider(T2IProvider):
    """定妆照：同样本地渲染，但用不同色系区分。"""

    name = "dryrun-t2i"

    def png(self, prompt: str, path: Path, *, size: Tuple[int, int] = (720, 1280),
            shot_label: str = "") -> Dict[str, Any]:
        prov = DryRunImageProvider()
        usage = prov.png(prompt, path, size=size, shot_label=shot_label or "portrait",
                         scene="portrait")
        usage["stage"] = "storyboard"
        usage["provider"] = self.name
        return usage


# ————————————————————————————————————————----
# 视频 provider
# ————————————————————————————————————————----


@register("dryrun", "video")
class DryRunVideoProvider(VideoProvider):
    """真正的 submit -> poll -> fetch 三段式，只是出片在本地。

    刻意保留异步形态：这样 stages/video.py 里的并发编排、超时、重试逻辑
    全都能被真实验证，换成真厂商时不用重写调用方。

    ``max_duration`` 默认给 8 秒 —— 这不是本地能力，而是**照着主流视频模型的
    公开上限填的**（veo3.1 系列 8 秒、sora-2 12 秒）。dryrun 故意保留这条限制，
    是为了让"创作侧要 12 秒、模型只给 8 秒"这个冲突在你还没接真厂商时就暴露出来，
    而不是等付了钱才发现成片被截断。
    """

    name = "dryrun-video"
    caps = ProviderCaps(
        width=720, height=1280, fps=24,
        max_duration=8.0, min_duration=1.0,
        max_ref_images=1,
        notes="本地推帧编码；上限照主流视频模型填，用来暴露时长冲突",
    )

    def __init__(self, work_dir: Optional[Path] = None, image_provider: Optional[ImageProvider] = None):
        self.work_dir = Path(work_dir) if work_dir else Path.cwd() / "runs" / "_dryrun_jobs"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.image_provider = image_provider or DryRunImageProvider()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def submit(self, request: Dict[str, Any]) -> str:
        # 契约检查：调用方给的时长必须在 caps 之内。
        # 这里**抛错而不是截断** —— 截断会让成片节奏莫名其妙且无法归因。
        duration = float(request.get("duration") or 4.0)
        cap = self.caps.max_duration
        if cap is not None and duration > cap + 1e-6:
            raise ProviderError(
                f"duration {duration:.2f}s 超过 {self.name} 的单段上限 {cap:.2f}s"
                f"（镜头 {request.get('shot_label', '?')}）。"
                f"拆分应该发生在上游 stage 里，provider 不做静默截断。",
                retryable=False,
                provider=self.name,
            )
        job_id = "dryjob_" + hashlib.md5(
            json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        self._jobs[job_id] = {"request": request, "done": False}
        return job_id

    def poll(self, job_id: str, *, timeout: float = 300.0) -> Tuple[str, Dict[str, Any], Optional[str]]:
        job = self._jobs.get(job_id)
        if job is None:
            return "failed", {}, None
        req = job["request"]
        dest = Path(req["dest"])
        duration = float(req.get("duration") or 4.0)
        size = req.get("size") or (720, 1280)

        if not job["done"]:
            # 生成关键帧（如果调用方已经给了首帧就复用）
            frame = req.get("image_path")
            if not frame or not Path(frame).exists():
                frame = dest.with_suffix(".frame.png")
                self.image_provider.png(
                    req.get("prompt", ""), Path(frame), size=size,
                    shot_label=req.get("shot_label", ""),
                )
            clip = media.image_to_clip(Path(frame), dest, duration=duration, size=size)
            job.update(done=True, clip=str(clip))

        money = duration * PRICING["video_per_second"]["720p"]
        usage = {
            "unit": "second",
            "amount": round(duration, 3),
            "money": round(money, 6),
            "provider": self.name,
            "stage": "video",
            "shot_label": req.get("shot_label", ""),
            "note": "[dryrun] 本地推帧编码，未调用真实视频模型",
        }
        return "succeeded", usage, job["clip"]

    def fetch(self, job_id: str, dest: Path) -> Dict[str, Any]:
        src = self._jobs.get(job_id, {}).get("clip")
        if src and Path(src).exists() and Path(src) != dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            Path(src).replace(dest)
        return {"unit": "file", "amount": 1.0, "money": 0.0,
                "provider": self.name, "stage": "video"}


# ————————————————————————————————————————----
# 音频 provider
# ————————————————————————————————————————----


@register("dryrun", "audio")
class DryRunAudioProvider(AudioProvider):
    """占位音轨：形状正确（时长/采样率/单声道），内容是**一声短促的音**。

    这样"音画对齐"和"拼接"逻辑可以被真实跑通，而不需要先解决 TTS。
    接真 TTS 时只换这一个类。

    台词**故意做成有声的**：一声 196 Hz 的短音，你放成片时能直接听出每句台词
    落在时间轴的哪个位置 —— 这是骨架里唯一能验证"音画对齐对不对"的手段
    （静音的占位音轨把这件事变成了看不见的）。BGM 仍然是静音：一段循环的哔声
    没有验证价值，只会盖住台词。
    """

    name = "dryrun-audio"

    def __init__(self, silent: bool = False):
        self.silent = silent

    def _duration_of(self, text: str) -> float:
        # 中文按 4.5 字/秒读，和 core/media.estimate_shot_seconds 口径一致
        return max(1.0, len(text.strip()) / 4.5 + 0.35) if text.strip() else 1.0

    def tts(self, text: str, path: Path, *, voice: str = "female_01",
            speed: float = 1.0, shot_label: str = "") -> Dict[str, Any]:
        dur = self._duration_of(text) / max(0.5, speed)
        media.write_wav(path, seconds=dur, freq=196.0, silent=self.silent or not text.strip())
        money = len(text) / 1000 * PRICING["tts_per_1k_char"]["default"]
        return {
            "unit": "char",
            "amount": float(len(text)),
            "money": round(money, 6),
            "provider": self.name,
            "stage": "audio",
            "shot_label": shot_label,
            "note": "[dryrun] 占位音轨（一声短音，不是真语音；用来听台词落在哪）",
        }

    def music(self, path: Path, *, duration: float, mood: str = "") -> Dict[str, Any]:
        media.write_wav(path, seconds=duration, freq=130.0, silent=True)
        minutes = duration / 60.0
        return {
            "unit": "minute",
            "amount": round(minutes, 4),
            "money": round(minutes * PRICING["music_per_minute"]["default"], 6),
            "provider": self.name,
            "stage": "audio",
            "note": "[dryrun] 静音占位 BGM",
        }


@register("dryrun", "lipsync")
class DryRunLipSyncProvider(LipSyncProvider):
    """占位口型同步：**不做任何视觉伪造**，只把片段重编码成契约要求的形态。

    真厂商做的事（按音素重绘嘴型）这里一件都没做。它产出的是"格式正确的错东西"，
    存在的意义是让 stage06 的接线、产物路径、时长契约、失败重跑逻辑都能被真实
    跑通 —— 而不是让成片看起来对上了嘴。

    **刻意没做可视化**（画个波形条、印个"已同步"）。试过：波形在低音量素材上
    画出来就是一条看不见的线（`showwaves` 对 ±1199/32767 的正弦只出 251 个
    非黑像素），而配 `apad` 凑长度会让 ffmpeg 永不终止。更重要的是，一个
    看起来"同步过了"的假标记比什么都不做更糟 —— 它会让人不再去看报告里的
    `[dryrun]` 字样。
    """

    name = "dryrun-lipsync"

    #: 时长容差：重编码前后的差超过这个值就报错。
    DURATION_TOLERANCE = 0.12

    def sync(self, video: Path, audio: Optional[Path], dest: Path, *,
             shot_label: str = "", scene: str = "") -> Dict[str, Any]:
        ff = media.require_ffmpeg()
        dest.parent.mkdir(parents=True, exist_ok=True)

        before = media.try_probe_duration(video)
        if before is None:
            raise ProviderError(
                f"量不出输入片段的时长，没法保证同步后时长不变：{video}",
                retryable=False, provider=self.name,
            )

        # 直接写 dest：原子替换由 stage06 负责（它会先给一个临时路径，
        # 量完时长对得上才挪成正式产物）。provider 再套一层临时文件没有意义。
        media.run([
            ff, "-y", "-i", str(video),
            "-an",                       # 契约 1：输出不带音轨（音轨由 concat 统一铺）
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(dest),
        ])
        after = media.try_probe_duration(dest)
        if after is None or abs(after - before) > self.DURATION_TOLERANCE:
            # 骨架那边还会再验一次；这里先炸掉，省得把坏产物交出去。
            dest.unlink(missing_ok=True)
            raise ProviderError(
                f"重编码后时长对不上（{before:.3f}s -> {after!r}s）",
                retryable=False, provider=self.name,
            )

        heard = "有配音，但**没有**做音素对齐" if audio and Path(audio).exists() else "这一镜本来就没有台词"
        return {
            "unit": "second",
            "amount": round(before, 3),
            "money": round(before * PRICING["lipsync_per_second"]["default"], 6),
            "provider": self.name,
            "stage": "lipsync",
            "shot_label": shot_label,
            "note": f"[dryrun] 没有真的改嘴型（{heard}）：只做了无音轨重编码，时长保持 {before:.3f}s",
        }
