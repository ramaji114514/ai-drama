"""给角色挑音色：只看描述，而且说清楚凭什么。

这不是"猜脸"那种猜 —— 音色错了，观众一耳朵就听出来（一个苍老的男人用稚气
的女声，画面再好也出戏）。而流水线原来的做法是**整部剧共用同一个默认音色**：
`--voice` 没传就是 `female_01`，传了就是所有人一个嗓子。角色圣经里明明有
`voice_map`，却从来没有谁去填过它 —— 于是"声音和形象不匹配"这件事
**永远不会有任何一层报出来**。

所以这里按角色的描述做一次**可解释**的判断：

- 判断结果写进项目圣经的 `voice_map`（看得见、改得动、下一集沿用）；
- 判断依据写进 `voice_notes`，人翻文件时能看到"它是凭什么挑的"；
- **判断不出来就不猜**：返回 `None`，由调用方报"这个角色没法定音色，
  用的是全局默认" —— 宁可让人看见一次默认值，也不要悄悄给错嗓子。

音色名是**通用的**（`male_02` 这种），具体落到哪个人声由 provider 决定
（见 `ai_drama/providers/dashscope.py` 的 `_VOICES`）。换一家厂商不用改这里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: 通用音色名 -> (性别, 年龄段)。provider 负责把它映射成自己的具体人声。
#: 只分三段：听觉上"少年 / 成年 / 老年"是分得出来的，再细就是硬凑了。
PALETTE: Dict[str, Tuple[str, str]] = {
    "female_01": ("female", "young"),
    "female_02": ("female", "adult"),
    "female_03": ("female", "old"),
    "male_01": ("male", "young"),
    "male_02": ("male", "adult"),
    "male_03": ("male", "old"),
}

#: 年龄段由小到大。判断年龄时用来比较。
_AGE_ORDER = ("young", "adult", "old")

# 光看 `desc` 常常不够 —— 它是中文短句（"陈默，42 岁，摆渡人"），而
# `anchor_prompt` 是模型写的英文（"Chinese man, 42 years old, weathered face"），
# 两边的线索互补。所以两个都查，中英文都认。
_FEMALE = (
    "女", "女孩", "少女", "姑娘", "母亲", "妈妈", "阿姨", "奶奶", "姐姐",
    "妹妹", "老婆", "妻子", "女儿", "嫂子", "婶", "婆婆", "妇人",
)
_MALE = (
    "男", "男孩", "少年", "父亲", "爸爸", "叔叔", "大爷", "爷爷", "哥哥",
    "弟弟", "老公", "丈夫", "儿子", "小伙", "汉子", "老头", "老汉", "大叔",
)
_FEMALE_EN = re.compile(
    r"\b(female|woman|women|girl|lady|mother|mom|she|her|daughter|sister|wife)\b",
    re.I,
)
_MALE_EN = re.compile(
    r"\b(male|man|men|boy|gentleman|father|dad|he|him|his|son|brother|husband)\b",
    re.I,
)
_OLD = (
    "年迈", "苍老", "花白", "白发", "皱纹", "上了年纪", "饱经风霜", "风霜",
    "老人", "老者", "老翁", "老汉", "老头", "老太", "老妪",
    "爷爷", "奶奶", "大爷", "阿婆",
)
_OLD_EN = re.compile(
    r"\b(old|older|elderly|aged|ageing|aging|grey|gray|wrinkl\w*|weathered)\b", re.I
)
_YOUNG = ("少年", "少女", "孩子", "儿童", "小孩", "童", "学生", "小伙", "少年感")
_YOUNG_EN = re.compile(
    r"\b(young|teen|teenage|teenager|boy|girl|child|kid|adolescent)\b", re.I
)
_ADULT = ("中年", "青年", "壮年", "成年")
_ADULT_EN = re.compile(r"\b(adult|middle-aged|mid-30s|mid-40s)\b", re.I)

#: 明确写出来的年龄优先于形容词 —— `42 years old` 比 `weathered face` 硬。
_AGE_NUM_CN = re.compile(r"(\d{1,2})\s*岁")
_AGE_NUM_EN = re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\s*old", re.I)
#: 中文描述里常写「六十岁」而不是「60岁」—— 不认这一种，一个六十岁的人
#: 就会被当成成年人，等于白写。
_AGE_CN_WORDS = re.compile(r"([一二三四五六七八九十]{1,3})\s*岁")
_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_number(text: str) -> Optional[int]:
    """「六十」「十七」「十」-> 60 / 17 / 10。只处理 1~99，够用了。"""
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = (_CN_DIGITS.get(head, 1) if head else 1) * 10
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens + ones
    return _CN_DIGITS.get(text)


def _band(years: int) -> str:
    if years < 21:
        return "young"
    if years < 50:
        return "adult"
    return "old"



@dataclass(frozen=True)
class Suggestion:
    """一个"看着像"的音色，以及凭什么这么看。"""

    voice: str
    why: str


def _gender(text: str) -> Tuple[Optional[str], List[str]]:
    """返回 (性别, 命中的线索)。两边都命中就是看不懂，返回 None。"""
    hits: Dict[str, List[str]] = {"female": [], "male": []}
    for word in _FEMALE:
        if word in text:
            hits["female"].append(word)
    for word in _MALE:
        if word in text:
            hits["male"].append(word)
    for regex, sex in ((_FEMALE_EN, "female"), (_MALE_EN, "male")):
        for m in regex.finditer(text):
            hits[sex].append(m.group(0))
    female, male = hits["female"], hits["male"]
    if female and not male:
        return "female", female
    if male and not female:
        return "male", male
    # 两边都有（"她父亲" 这种）或都没有 —— 都不猜
    return None, female + male


def _age(text: str) -> Tuple[Optional[str], Optional[str]]:
    """返回 (年龄段, 凭据)。认不出返回 (None, None)。"""
    for regex in (_AGE_NUM_CN, _AGE_NUM_EN):
        m = regex.search(text)
        if m:
            return _band(int(m.group(1))), m.group(0)
    m = _AGE_CN_WORDS.search(text)
    if m:
        years = _cn_number(m.group(1))
        if years is not None:
            return _band(years), m.group(0)
    for words, band in ((_OLD, "old"), (_YOUNG, "young"), (_ADULT, "adult")):
        for word in words:
            if word in text:
                return band, word
    for regex, band in ((_OLD_EN, "old"), (_YOUNG_EN, "young"), (_ADULT_EN, "adult")):
        m = regex.search(text)
        if m:
            return band, m.group(0)
    return None, None


def suggest(desc: str = "", anchor_prompt: str = "") -> Optional[Suggestion]:
    """看描述猜一个音色。猜不出来返回 None（**不猜**比猜错好）。"""
    text = f"{desc}\n{anchor_prompt}".strip()
    if not text:
        return None

    sex, sex_hits = _gender(text)
    if sex is None:
        return None

    band, age_hit = _age(text)
    if band is None:
        # 性别看得出、年龄看不出：成年人是最不容易出错的默认（少年和老年
        # 都有明显的外形线索，没有线索就说明画像里没写）。
        band = "adult"

    for name, (v_sex, v_band) in PALETTE.items():
        if v_sex == sex and v_band == band:
            why = f"{'女' if sex == 'female' else '男'}声"
            if age_hit:
                why += f"（凭「{age_hit}」判为{band}）"
            else:
                why += "（描述里没写年龄，按成年处理）"
            return Suggestion(voice=name, why=why)
    return None


def unresolved_reason(desc: str = "", anchor_prompt: str = "") -> str:
    """猜不出来时说清楚是缺哪条线索 —— 报错要能指导人动手改。"""
    text = f"{desc}\n{anchor_prompt}".strip()
    if not text:
        return "角色描述是空的"
    sex, hits = _gender(text)
    if sex is None:
        if hits:
            return "描述里男女线索都有（" + "、".join(dict.fromkeys(hits)) + "），不敢定"
        return "描述里看不出性别"
    return "调色板里没有对应的音色（这不该发生）"
