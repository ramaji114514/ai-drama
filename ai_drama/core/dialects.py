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
    #: 单个输入框的字数上限（网页端常见的硬限制）。
    max_chars: int
    #: 要不要去掉 `【…】` 小标题。
    strip_headings: bool
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


DIALECTS: Dict[str, Dialect] = {
    "generic": Dialect(
        key="generic",
        title="通用（不挑平台）",
        has_negative=True,
        has_last_frame=True,
        max_chars=4000,
        strip_headings=False,
        tip="留着小标题，给看得懂分层的人或 API 用；粘到网页端前先看目标平台那一栏。",
    ),
    "kling": Dialect(
        key="kling",
        title="可灵",
        has_negative=True,
        has_last_frame=True,
        max_chars=2000,
        strip_headings=True,
        tip="首帧图、尾帧图各自上传，视频提示词只描述运动和情绪；负向词有独立框。",
    ),
    "jimeng": Dialect(
        key="jimeng",
        title="即梦",
        has_negative=False,
        has_last_frame=True,
        max_chars=800,
        strip_headings=True,
        tip="没有负向框：负向词已省略，别塞进正向。首尾帧各传一张图。",
    ),
    "seedance": Dialect(
        key="seedance",
        title="Seedance（豆包）",
        has_negative=False,
        has_last_frame=True,
        max_chars=1000,
        strip_headings=True,
        tip="没有负向参数：负向词已省略。时长写在同一句里，不要另起一行。",
    ),
}

DEFAULT_KEY = GENERIC_KEY


def keys() -> Sequence[str]:
    return tuple(DIALECTS)


def catalog() -> List[Dict[str, Any]]:
    """页面上的下拉框：有哪些平台可选、各自一句话该注意什么。"""
    return [{"key": x.key, "title": x.title, "tip": x.tip,
             "has_negative": x.has_negative, "has_last_frame": x.has_last_frame}
            for x in DIALECTS.values()]


def get(key: Optional[str]) -> Dialect:
    """按 key 取方言，认不出来就回退到通用 —— 页面上的旧链接不该 500。"""
    return DIALECTS.get((key or "").strip().lower(), DIALECTS[DEFAULT_KEY])


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

    def put(field_key: str, title: str, hint: str, text: str) -> None:
        text, cut = d.fit(text)
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
    if video:
        put("video", "视频提示词", "粘进视频框，只描述运动和情绪", video)

    neg_img = getattr(shot, "negative_prompt", "") or ""
    neg_vid = getattr(shot, "video_negative", "") or ""
    if d.has_negative:
        put("negative", "负向词", "有独立的负向框就放这里；没有就整段别用",
            _merge_negatives(neg_img, neg_vid))
    elif neg_img or neg_vid:
        out.dropped.append(
            "负向词：这家平台没有负向输入框，已省略 —— "
            "别把它补进正向，正向里的每个名词都会被当成要画的东西。")
    return out


def render_all(ep: Any, shot: Any) -> Dict[str, Any]:
    """一镜 × 所有平台。网页端切换时直接用，不用来回请求。"""
    return {k: render(ep, shot, k).as_dict() for k in keys()}
