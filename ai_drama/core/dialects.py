"""把同一镜的提示词翻译成各家网页端的"方言"。

为什么要这一层：同一个镜头，粘进可灵、即梦、Seedance 的框里，**该怎么写
并不一样**。差别不在文笔，在字段：

- 有的平台有独立的「负向提示词」框，有的没有。没有的那个，把"不要水印"
  塞进正向框，模型会把"水印"当名词画出来 —— 这正是我们花力气分开写负向
  词的原因，不能在最后一米又混回去。
- 有的平台认"首帧图 + 尾帧图"两个图槽，有的只认首帧；尾帧不支持的平台，
  尾帧提示词只能当"这一段要演到哪"的描述留着，不能当图去用。
- 有的平台会把提示词里的方括号小标题当画面内容。骨架内部用
  `【参考锁定】`「【无参考图】」这类小标题分层，粘进去就成了"画面里出现
  文字" —— 所以要按平台决定保不保留。

能力表是**结构描述，不是 API 参数**：各家网页版改版频繁，这里只保证
"哪个字段有没有、正负向分不分、标题要不要去掉"，不背具体参数名。

新增平台：在 `DIALECTS` 里加一条 `Dialect`，然后补一条测试说明它的
字段差异。**不要**在别处再写一份 if/else。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

KEY = "generic"
GENERIC_KEY = "generic"

_BRACKET_HEAD = re.compile(r"【[^】]{1,16}】")


@dataclass(frozen=True)
class Dialect:
    """一家平台的粘贴方式。"""

    key: str
    title: str
    #: 有没有独立的负向输入框。没有的平台，负向词必须整段丢掉。
    has_negative: bool
    #: 认不认尾帧图（第二个图槽）。
    has_last_frame: bool
    #: 正向输入框的字数上限（查到的网页/API 限制，不是猜的）。
    max_chars: int
    #: 要不要去掉 `【…】` 小标题。
    strip_headings: bool
    #: 这套提示词该用什么语言读。`zh` = 骨架现在写出来的中文；
    #: `en` = 英文平台，中文字段它也能吃，但英文更稳（见 tip）。
    lang: str = "zh"
    #: 负向框的字数上限；0 表示这家**没有**独立的负向框。
    max_negative: int = 0
    #: 运镜怎么写：`sentence` = 自然语言一句；`bracket` = 方括号指令词
    #: （海螺有 15 个固定指令，一次最多 3 个；用自然语言它也认，但方括号更稳）。
    motion: str = "sentence"
    #: 页面上给用户的一句提示：告诉他这几段分别往哪儿粘。
    tip: str = ""

    def clean(self, text: str) -> str:
        """按这家平台的脾气整理一段文字。"""
        if self.strip_headings:
            text = _BRACKET_HEAD.sub("", text or "")
            text = re.sub(r"\n{2,}", "\n", text)
            text = re.sub(r"[ \t]{2,}", " ", text)
        return (text or "").strip()

    def fit(self, text: str) -> Tuple[str, bool]:
        """按字数上限截断（尽量断在句末）。返回 (文字, 是否截过)。"""
        text = self.clean(text)
        if len(text) <= self.max_chars:
            return text, False
        cut = text[: self.max_chars]
        for mark in ("。", "；", "，", "\n"):
            pos = cut.rfind(mark)
            if pos > self.max_chars * 0.6:
                return cut[: pos + 1].strip(), True
        return cut.strip(), True


#: 各家的字数上限都查过官方文档，别改小也别说"差不多"：
#:   可灵 2500（正向/负向各 2500）https://cloud.tencent.cn/document/api/1616/130564
#:   海螺 2000（无负向框，只有 prompt_optimizer 开关）
#:     https://platform.minimax.io/docs/api-reference/video-generation/t2v.md
#:   Veo 3.1 正向 2000 / 负向 500
#:     https://docs.gmicloud.ai/model-quickstarts/video/veo-3-1-generate-001.md
#:   Sora 2 正向 2000（无负向框、只认首帧）
#:     https://docs.gmicloud.ai/model-quickstarts/video/sora-2.md
#:   Runway Gen-4.5 正向 2000（无负向框、首帧+尾帧）
#:     https://docs.magnific.com/api-reference/video/runway-gen-4-5/overview.md
#:   即梦 / Seedance 没查到公开上限，按同代接口的 2000 兜底，宁可早截。
#:
#: 为什么这些数字重要：超一点没事，超一倍模型就开始"挑着执行"，而用户以为
#: 自己写的每一条都被读到了。截断时明说截了（`truncated`），不偷偷砍。
DIALECTS: Dict[str, Dialect] = {
    "generic": Dialect(
        key="generic",
        title="通用（不挑平台）",
        has_negative=True,
        has_last_frame=True,
        max_chars=4000,
        max_negative=4000,
        strip_headings=False,
        tip="留着小标题，给看得懂分层的人或 API 用；粘到网页端前先看目标平台那一栏。",
    ),
    "kling": Dialect(
        key="kling",
        title="可灵",
        has_negative=True,
        has_last_frame=True,
        max_chars=2500,
        max_negative=2500,
        strip_headings=True,
        tip="首帧图、尾帧图各自上传；负向词有独立框（也是 2500 字）。"
            "运镜要写在句子前面，可灵容易把末尾的运镜当可选补充。",
    ),
    "jimeng": Dialect(
        key="jimeng",
        title="即梦",
        has_negative=False,
        has_last_frame=True,
        max_chars=2000,
        strip_headings=True,
        tip="没有负向框：负向词已省略，别塞进正向。首尾帧各传一张图；"
            "官方模板是「主体+动作+场景+风格+运镜」，中文 30–80 字最稳。",
    ),
    "seedance": Dialect(
        key="seedance",
        title="Seedance（豆包）",
        has_negative=False,
        has_last_frame=True,
        max_chars=2000,
        strip_headings=True,
        tip="没有负向参数：负向词已省略。官方四行模板：「画面呈现…」「镜头采用…」"
            "素材引用必须写 @图片1（中文序号），别写 @img1。",
    ),
    "hailuo": Dialect(
        key="hailuo",
        title="海螺（MiniMax）",
        has_negative=False,
        has_last_frame=True,
        max_chars=2000,
        strip_headings=True,
        motion="bracket",
        tip="没有负向框：负向词已省略。运镜用方括号指令词（[Push in] 这类，"
            "一次最多 3 个）；首帧、尾帧各有一个图槽。",
    ),
    # 下面三家是英文平台。字段结构能对齐，但**语言这一层骨架不做**：
    # 翻一遍要再调一次模型，等于给"只拿提示词"的人加一笔钱。所以 tip 里
    # 明说"英文更稳"，把选择权留给用户。
    "runway": Dialect(
        key="runway",
        title="Runway（Gen-4）",
        has_negative=False,
        has_last_frame=True,
        max_chars=2000,
        strip_headings=True,
        lang="en",
        tip="没有负向框（它明确不建议否定措辞，可能反向理解）：负向词已省略。"
            "首帧+尾帧两个图槽；中文它也能吃，但英文更稳。",
    ),
    "veo": Dialect(
        key="veo",
        title="Veo（Google）",
        has_negative=True,
        has_last_frame=True,
        max_chars=2000,
        max_negative=500,
        strip_headings=True,
        lang="en",
        tip="有独立负向框，但只放得下 500 字。首帧+尾帧都支持；"
            "它偏爱「一个镜头一句话」，别堆形容词。",
    ),
    "sora": Dialect(
        key="sora",
        title="Sora",
        has_negative=False,
        has_last_frame=False,
        max_chars=2000,
        strip_headings=True,
        lang="en",
        tip="只认首帧、没有负向框：尾帧和负向词都省了。它吃长句，"
            "把这一段当成一段摄影指导来读，中英文都行。",
    ),
}

#: 海螺的方括号运镜词（官方 15 个固定指令里最常用的几个）。
HAILUO_MOVE = {
    "establishing": "[Push in]",
    "wide": "[Pan right]",
    "medium": "[Tracking shot]",
    "close": "[Push in]",
    "extreme_close": "[Static shot]",
}


DEFAULT_KEY = GENERIC_KEY


def keys() -> Sequence[str]:
    return tuple(DIALECTS)


def catalog() -> List[Dict[str, Any]]:
    """页面上的下拉框：有哪些平台可选、各自一句话该注意什么。"""
    return [{"key": x.key, "title": x.title, "tip": x.tip, "lang": x.lang,
             "has_negative": x.has_negative, "has_last_frame": x.has_last_frame}
            for x in DIALECTS.values()]


def get(key: Optional[str]) -> Dialect:
    """按 key 取方言，认不出来就回退到通用 —— 页面上的旧链接不该 500。"""
    return DIALECTS.get((key or "").strip().lower(), DIALECTS[DEFAULT_KEY])


def _fit_with(text: str, d: Dialect, limit: int) -> Tuple[str, bool]:
    """按任意上限截断（负向框的上限常常和正向不一样，Veo 就是 2000/500）。"""
    text = d.clean(text)
    if limit <= 0 or len(text) <= limit:
        return text, False
    cut = text[:limit]
    for mark in ("。", "；", "，", "\n"):
        pos = cut.rfind(mark)
        if pos > limit * 0.6:
            return cut[: pos + 1].strip(), True
    return cut.strip(), True


@dataclass
class Field:
    """一个待粘贴的框。"""

    key: str
    title: str
    hint: str
    text: str
    truncated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "title": self.title, "hint": self.hint,
                "text": self.text, "truncated": self.truncated}


@dataclass
class Rendered:
    key: str
    title: str
    tip: str
    fields: List[Field] = field(default_factory=list)
    #: 这个平台没有、因此被丢掉的东西，写给用户看，不要偷偷丢
    dropped: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "title": self.title, "tip": self.tip,
                "fields": [f.as_dict() for f in self.fields],
                "dropped": list(self.dropped)}


def _merge_negatives(*parts: str) -> str:
    seen: List[str] = []
    for part in parts:
        for item in re.split(r"[，,、;；]\s*", part or ""):
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
    return "，".join(seen)


def render(ep: Any, shot: Any, key: Optional[str] = None) -> Rendered:
    """把一镜翻成某家平台的几段文字。"""
    d = get(key)
    out = Rendered(key=d.key, title=d.title, tip=d.tip)
    if d.lang == "en" and any((getattr(shot, f, "") or "").strip()
                             for f in ("image_prompt", "video_prompt")):
        # 不替用户翻译，但要告诉他"这不是英文"—— 他可能以为我们翻了
        out.tip += "（当前字段是中文）"

    def put(field_key: str, title: str, hint: str, text: str,
            limit: Optional[int] = None) -> None:
        text, cut = _fit_with(text, d, limit) if limit is not None else d.fit(text)
        if not text:
            return
        out.fields.append(Field(field_key, title, hint, text, cut))

    put("first_frame", "首帧图", "先出这张图，再拿它当视频的首帧",
        getattr(shot, "image_prompt", "") or "")
    if d.has_last_frame:
        put("last_frame", "尾帧图", "这一段演完时的画面，拿它当尾帧",
            getattr(shot, "last_frame", "") or "")
    else:
        end = (getattr(shot, "end_action", "") or "").strip()
        if end:
            out.dropped.append(
                "尾帧图：这家平台只认首帧，尾帧写进了视频提示词里当"
                f"「演到哪儿为止」——{end}")

    video = getattr(shot, "video_prompt", "") or ""
    if video and d.motion == "bracket":
        # 海螺认方括号指令。自然语言那句它也读，但实测不如指令稳，所以把指令
        # 放在最前面，原来的整句留在后面当动作描述。
        mark = HAILUO_MOVE.get(getattr(shot, "framing", ""), "[Tracking shot]")
        video = f"{mark} {video}"
    if video:
        put("video", "视频提示词", "粘进视频框，只描述运动和情绪", video)

    neg_img = getattr(shot, "negative_prompt", "") or ""
    neg_vid = getattr(shot, "video_negative", "") or ""
    if d.has_negative:
        put("negative", "负向词", "有独立的负向框就放这里；没有就整段别用",
            _merge_negatives(neg_img, neg_vid), limit=d.max_negative or d.max_chars)
    elif neg_img or neg_vid:
        out.dropped.append(
            "负向词：这家平台没有负向输入框，已省略 —— "
            "别把它补进正向，正向里的每个名词都会被当成要画的东西。")
    return out


def render_all(ep: Any, shot: Any) -> Dict[str, Any]:
    """一镜 × 所有平台。网页端切换时直接用，不用来回请求。"""
    return {k: render(ep, shot, k).as_dict() for k in keys()}


ANCHOR_HINT = "拿去做「角色参考图」"

#: 定妆照里"删了就不能当参考图"的那几个词。紧凑版只保留含这些词的分句，
#: 剩下的（画风、情绪、环境）整段丢掉 —— 参考图模型看的是长相，
#: 风格句只会跟长相抢权重，这在骨架排序上已经承认过一次了。
ANCHOR_KEEP = ("正面", "纯色", "柔光", "85mm", "五官", "平视")

#: 紧凑版的上限。不是某家平台的硬限制（硬限制都 2000 以上，见 DIALECTS），
#: 而是"再长就开始顾此失彼"的经验线：一张参考图值得写的只有长相和拍法。
ANCHOR_COMPACT = 180


def compact_anchor(text: str, limit: int = ANCHOR_COMPACT) -> str:
    """去掉画风/情绪，只留长相与拍法的那一版。"""
    parts = [x.strip() for x in re.split(r"[，,]", text or "") if x.strip()]
    if not parts:
        return ""
    keep = [parts[0]] + [x for x in parts[1:]
                         if any(k in x for k in ANCHOR_KEEP)]
    out = "，".join(keep)
    if len(out) <= limit:
        return out
    for mark in ("。", "；", "，"):
        pos = out.rfind(mark, 0, limit)
        if pos > limit * 0.6:
            return out[: pos + 1].strip()
    return out[:limit].strip()


def render_anchor(ch: Any, key: Optional[str] = None) -> Rendered:
    """角色的定妆照提示词，按平台字数上限整理。

    定妆照和镜头提示词不是一回事：它只有一段，没有首尾帧之分、没有负向框，
    但**字数上限照样管用** —— 即梦、海螺、Seedance 的图框都是 2000 字，
    而定妆照是唯一会长到两百字以上的那一段（五条规格一条都不能删）。
    截断时同样明说截了，不偷偷砍掉末尾的"五官与发际线完整"——
    那一条恰好是锁脸最要紧的一条，被静默截掉最糟。
    """
    d = get(key)
    out = Rendered(key=d.key, title=d.title, tip=d.tip)
    full = getattr(ch, "anchor_prompt", "") or ""
    text, cut = d.fit(full)
    if text:
        out.fields.append(Field("anchor", "定妆照", ANCHOR_HINT, text, cut))
    # 紧凑版：硬限制管不到，但"再长就顾此失彼"管得到。
    # 通用那栏不给 —— 选通用的意思就是"我要原文，别替我删"。
    if d.key != GENERIC_KEY:
        short = compact_anchor(d.clean(full))
        if short and len(short) < len(text):
            out.fields.append(Field(
                "anchor_compact", "定妆照（紧凑版）",
                f"{len(short)} 字，只留长相和拍法", short))
    if cut:
        out.dropped.append(
            f"定妆照：超过这家平台的 {d.max_chars} 字，已按句号截断。"
            "要完整版就切回「通用」。")
    return out


def anchor_variants(ch: Any) -> Dict[str, Any]:
    """只交出**和原文不一样**的那几家。

    定妆照本来就短的时候，八家全是同一段文字 —— 那就没必要让接口每次
    多发八份。网页端拿不到某家时用回原文即可。
    """
    base = re.sub(r"[ \t]+", " ", (getattr(ch, "anchor_prompt", "") or "").strip())
    out: Dict[str, Any] = {}
    for k in keys():
        r = render_anchor(ch, k)
        text = r.fields[0].text if r.fields else ""
        # 两种情况都算"被改写过"：截断了，或者多出一版紧凑版。
        # 只比 fields[0] 会漏掉后者 —— 那版只加字段、不动原文。
        if text != base or len(r.fields) > 1:
            out[k] = r.as_dict()
    return out
