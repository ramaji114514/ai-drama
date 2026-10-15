"""极简 YAML —— 够用就停。

**为什么不用 PyYAML。**

这个骨架对外的承诺是「零依赖、零 key 就能跑完整条流水线」。为了读两个
配置文件而引入一个 YAML 解析器，会让「克隆下来就能跑」变成「先装依赖」——
而那正是参考项目们最劝退人的地方。

**为什么不是 JSON。**

配置文件是给人改的。阈值、模型清单这类东西，改的时候需要**在旁边写清楚
为什么是这个数**。JSON 不能写注释，最后就会变成「一个没人敢动的数字」。

**支持的形态**（就这些，多了不认）：

    key: 标量                 # 数字 / true / false / 带引号或不带引号的字符串
    key: []                   # 空列表
    key: [a, b, c]            # 行内列表
    key:                      # 块列表
      - a
      - b
    key:                      # 映射，最多嵌到 MAX_DEPTH 层
      child: 标量
      other:
        grandchild: 标量

**遇到不支持的写法一律抛错，并带行号。**

这条是刻意的。配置解析器最常见的失败方式不是崩溃，而是**静默忽略**：
你改了一个键，程序照跑不误，只是那个键从来没被读到过。于是你花了两个
小时怀疑模型、怀疑网络，就是没怀疑配置文件。宁可启动时就炸。

**一条踩过的坑，记在这里免得再犯。**

第一版用「子项必须比上一行缩进更深」来判断层级，结果**每个正常的列表
从第二项起全部报错** —— 列表项之间是并列关系，缩进相同。层级判断的正确
做法是维护一个容器栈：新的一行只要缩进大于栈顶容器所在行的缩进，就属于
那个容器；小于等于就把它弹出去。并列天然成立，不需要特殊照顾。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

__all__ = ["parse", "ParseError"]

#: 标记"这个键是个容器，但还没遇到第一个子项，类型未知（列表还是映射）"
_PENDING = object()

#: 嵌套深度上限。到顶就报错，而不是悄悄把子项挂到错误的位置。
#: 3 层够用：`models:` -> 模型 id -> 字段。
MAX_DEPTH = 3


class ParseError(ValueError):
    """配置写错了。消息里一定带行号 —— 否则等于没说。"""


def _strip_comment(raw: str) -> str:
    """去掉行尾注释，但不动引号里的 `#`。

    只有「行首的 #」和「前面是空白的 #」才当注释，这样
    `strengths: 画质好#1首选` 里的 `#1` 不会被吃掉，
    而 `max_duration: 8  # 秒` 里的注释能被正常忽略。
    """
    out: List[str] = []
    in_single = in_double = False
    for i, ch in enumerate(raw):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or raw[i - 1] in " \t":
                break
        out.append(ch)
    return "".join(out)


def _split_kv(content: str, lineno: int, source: str) -> Tuple[str, Optional[str]]:
    """把一个缩进行切成 (键, 值文本)。值文本为 None 表示这是个容器键。"""
    if content.startswith("- "):
        raise ParseError(
            f"{source} 第 {lineno} 行：列表项 `{content}` 没有归属的键。\n"
            f"  列表项必须缩进在某个 `键:` 下面。"
        )
    if ":" not in content:
        raise ParseError(
            f"{source} 第 {lineno} 行无法解析：{content!r}\n"
            f"  本文件只支持这几种写法：\n"
            f"    key: 值\n"
            f"    key: [a, b, c]\n"
            f"    key:\n"
            f"      - 列表项\n"
            f"    key:\n"
            f"      child: 值\n"
            f"  多行字符串（| 或 >）、锚点引用、行内映射 {{a: 1}} 都不支持。"
        )
    key, _, value = content.partition(":")
    key = key.strip()
    value = value.strip()
    if not key:
        raise ParseError(f"{source} 第 {lineno} 行：键是空的")
    return key, (value if value else None)


def _scalar(value: str, lineno: int, source: str) -> Any:
    """把一段文本变成 Python 值。

    注意顺序：**先看引号**。带引号的一律当字符串，这样 `version: "2"`
    就是字符串 "2" 而不是整数 2 —— 版本号、模型 id 这类东西一旦被当成
    数字，就会在日志、报告和字典键里长得和别处不一样（`2` vs `"2"`），
    而这类不一致最难查。
    """
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part, lineno, source) for part in inner.split(",")]
    try:
        if "." in v or "e" in low:
            return float(v)
        return int(v)
    except ValueError:
        return v


class _Frame:
    """栈帧：某个容器键下面正在填充的容器。

    容器类型（列表还是映射）在第一行子项到达之前是未知的，
    所以这里延迟创建 —— 不能一看到 `key:` 就假定它是个列表。
    """

    __slots__ = ("indent", "parent", "key", "container", "is_root")

    def __init__(self, indent: int, parent: Optional[Dict[str, Any]], key: Optional[str],
                 *, is_root: bool = False):
        self.indent = indent
        self.parent = parent
        self.key = key
        self.container: Any = None
        self.is_root = is_root

    def ensure(self, want_list: bool) -> Any:
        if self.container is None:
            self.container = [] if want_list else {}
            if self.parent is not None and self.key is not None:
                self.parent[self.key] = self.container
        return self.container


def parse(text: str, *, source: str = "配置文件") -> Dict[str, Any]:
    """解析。`source` 只用于报错信息 —— 请写文件名，别写「配置」。"""
    root: Dict[str, Any] = {}
    root_frame = _Frame(-1, None, None, is_root=True)
    root_frame.container = root
    stack: List[_Frame] = [root_frame]

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        head = line[: len(line) - len(line.lstrip())]
        if "\t" in head:
            raise ParseError(f"{source} 第 {lineno} 行：缩进用空格，不要用制表符")
        indent = len(head)
        content = line.strip()

        # 弹出"这一行已经不在它里面"的容器。
        # 用 <= 而不是 <：同一层级的并列项要回到共同父级再挂上去。
        while len(stack) > 1 and indent <= stack[-1].indent:
            stack.pop()
        frame = stack[-1]
        # 顶层帧没有所属的键，所以只认顶格的行。
        # 少了这一条，`b: 2` 下面随便缩进多少都会挂到顶层 ——
        # 而那个缩进行看起来像是某个键的子项，实际却没有父键。
        if frame.is_root and indent != 0:
            raise ParseError(
                f"{source} 第 {lineno} 行：`{content}` 缩进了，但它上面没有一个 `键:`。"
            )
        if indent <= frame.indent:
            raise ParseError(
                f"{source} 第 {lineno} 行：`{content}` 缩进了，但它上面没有一个 `键:`。"
            )

        if content.startswith("- "):
            container = frame.ensure(want_list=True)
            if not isinstance(container, list):
                raise ParseError(
                    f"{source} 第 {lineno} 行：`{frame.key}` 下面已经是一个映射，"
                    f"不能再出现列表项 `{content}`。"
                )
            container.append(_scalar(content[2:].strip(), lineno, source))
            continue

        container = frame.ensure(want_list=False)
        if not isinstance(container, dict):
            raise ParseError(
                f"{source} 第 {lineno} 行：`{frame.key}` 下面已经是一个列表，"
                f"不能再出现 `{content}`。"
            )
        key, value = _split_kv(content, lineno, source)
        if value is None:
            if len(stack) >= MAX_DEPTH + 1:
                raise ParseError(
                    f"{source} 第 {lineno} 行：`{key}:` 想嵌套到第 {len(stack)} 层，"
                    f"但上限是 {MAX_DEPTH} 层。需要更深的结构请把键名拍平。"
                )
            container[key] = _PENDING
            stack.append(_Frame(indent, container, key))
        else:
            if key in container:
                # 重复键必须报错。静默让后一个覆盖前一个，表现就是
                # "我改了配置但没生效" —— 而且是在几千行配置里找一个
                # 重名的键。刚才给 models.yaml 加 lipsync 条目时就差点
                # 用 `dryrun` 覆盖掉同名的视频条目。
                raise ParseError(
                    f"{source} 第 {lineno} 行：键 `{key}` 重复了。"
                    f"同一层级里后写的会静默盖掉先写的 —— 请合并成一条。"
                )
            container[key] = _scalar(value, lineno, source)

    # 光有键、没有任何子项 -> 空映射（不是 None，省得调用方到处判空）
    _resolve_pending(root)
    return root


def _resolve_pending(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if value is _PENDING:
                node[key] = {}
            else:
                _resolve_pending(value)
    elif isinstance(node, list):
        for item in node:
            _resolve_pending(item)
