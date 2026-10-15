"""模型目录 —— 「哪个模型能干什么」是数据。

这个模块存在的理由，是为了让「换一家模型」这件事**不改代码**。

三件事被搬进了 `config/models.yaml`：

1. **能力上限**（`max_duration` / `resolutions` / `aspect_ratios`）。
   最要命的是单段时长上限。分镜表说「这个镜头 12 秒」，而模型一次只能出
   8 秒 —— 不处理的话你会拿到一段**不报错的**、只演完前 2/3 的视频。
   参考项目在自己代码注释里记过这个坑：写死 15 秒而 veo3.1 上限 8 秒时，
   「模型会按 15 秒的信息量去编排画面，生成出来必然是截断的」。
   所以上限必须来自模型，而它必须是配置 —— 否则换家模型就得改代码。

2. **参考素材走哪个请求字段**（`ref_fields`）。
   这条最容易吃暗亏：**字段名写错，接口不报错，只是安静地忽略**。
   于是你传了参考图、模型没收到、出图漂了，而你会以为是「这个模型
   一致性不行」。不同厂商/型号的字段名不一样，所以它必须是配置。

3. **档次与擅长什么**（`tier` / `cost` / `strengths`）。
   让「这个活得用贵的吗」变成一次可读的查表，而不是口口相传的经验。

价格字段刻意留空。填了就得维护，不填只是退回「按次数拦」这种更保守的
算法 —— 骨架不该假装知道你的单价。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields as _dc_fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .miniyaml import ParseError, parse

__all__ = [
    "ModelCatalog", "ModelEntry", "RefFields",
    "catalog_path", "load_catalog", "default_catalog",
]

#: 相对档次。挑模型时的主要依据。
TIERS = ("quality", "balanced", "fast")

#: 形态。
KINDS = ("text", "image", "video", "audio", "t2i", "lipsync", "vision")


def catalog_path(root: Path) -> Path:
    return Path(root) / "config" / "models.yaml"


# ————————————————————————————————————————----


@dataclass(frozen=True)
class RefFields:
    """参考素材在请求里叫什么。

    留成配置是因为**字段名不对接口不报错**。聚合服务尤其如此：
    你把参考图放在 `image_urls`，服务读的是 `image`，于是它安静地
    生成了一张没有参考的图 —— 看起来就是「一致性不行」。
    """

    image: str = "image"
    image_alias: str = "image_urls"
    video: str = "video_urls"
    audio: str = "audio_urls"

    def image_field_names(self) -> Tuple[str, ...]:
        """要送几个字段名。去重、去掉空串。

        两个都送不会出错，只会多一点点请求体积 —— 比"零参考"划算得多。
        """
        out: List[str] = []
        for name in (self.image, self.image_alias):
            if name and name not in out:
                out.append(name)
        return tuple(out)

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in _dc_fields(self)}


@dataclass(frozen=True)
class ModelEntry:
    """一个模型（或一个 provider 的一种能力）。"""

    id: str
    kind: str = "video"
    #: 由哪个适配器实现 —— 对应 providers/ 里 `@register(name, kind)` 的 name。
    #: 留空表示"还没接"，目录里先记着能力。
    provider: str = ""
    tier: str = "balanced"
    cost: int = 3
    #: 单价。留空 = 只能按次数拦，不能按金额拦。**照控制台单价填，别猜。**
    price: Optional[float] = None
    price_per_second: Optional[float] = None
    #: 单次生成的最长/最短秒数。这是硬上限，不是建议值。
    max_duration: Optional[float] = None
    min_duration: Optional[float] = None
    resolutions: Tuple[str, ...] = ()
    aspect_ratios: Tuple[str, ...] = ()
    supports_image_input: bool = False
    #: 一句话进上下文，供挑选模型时判断。写具体点才有用。
    strengths: str = ""
    notes: str = ""

    @property
    def credit(self) -> str:
        """一行摘要，给人和给模型看都够短。"""
        bits = [f"{self.id}[{self.kind}/{self.tier}/cost{self.cost}]"]
        if self.max_duration is not None:
            bits.append(f"单段≤{_num(self.max_duration)}s")
        if self.aspect_ratios:
            bits.append("/".join(self.aspect_ratios))
        if self.strengths:
            bits.append(self.strengths)
        return " ".join(bits)

    def caps(self, *, width: int = 720, height: int = 1280, fps: int = 24):
        """构造成 ProviderCaps，供 stage 决定"要不要拆镜头"。

        默认 720x1280 是竖屏 —— 短剧的主战场。横屏项目在这里传自己的尺寸。
        """
        from ..providers.base import ProviderCaps

        return ProviderCaps(
            width=width,
            height=height,
            fps=fps,
            max_duration=self.max_duration,
            min_duration=self.min_duration or 1.0,
            aspect_ratios=self.aspect_ratios or ("9:16",),
            supports_image_input=self.supports_image_input,
            notes=f"{self.id}（{self.tier}）" + (f"：{self.strengths}" if self.strengths else ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in _dc_fields(self):
            value = getattr(self, f.name)
            out[f.name] = list(value) if isinstance(value, tuple) else value
        return out


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


# ————————————————————————————————————————----


@dataclass
class ModelCatalog:
    """整份目录。"""

    version: str = "1"
    ref_fields: RefFields = field(default_factory=RefFields)
    models: Dict[str, ModelEntry] = field(default_factory=dict)
    defaults: Dict[str, str] = field(default_factory=dict)
    #: 目录文件读不到时的原因。有值 = 正在用内置目录，`models` 命令会显示它。
    problem: str = ""

    # ———————— 查询

    def get(self, model_id: str) -> Optional[ModelEntry]:
        return self.models.get(model_id)

    def by_kind(self, kind: str) -> List[ModelEntry]:
        return [m for m in self.models.values() if m.kind == kind]

    def by_provider(self, provider: str) -> List[ModelEntry]:
        return [m for m in self.models.values() if m.provider and m.provider == provider]

    def default_model(self, kind: str) -> Optional[ModelEntry]:
        """这个形态默认用哪个模型。没配就挑本形态里最便宜的。"""
        model_id = self.defaults.get(kind)
        if model_id and model_id in self.models:
            return self.models[model_id]
        candidates = self.by_kind(kind)
        if not candidates:
            return None
        return sorted(candidates, key=lambda m: (m.cost, m.id))[0]

    def caps_for_model(self, model_id: str, **size):
        entry = self.get(model_id)
        return entry.caps(**size) if entry else None

    def caps_for_provider(self, provider: str, **size):
        """按适配器名找能力声明。

        适配器自己声明了 caps 就不用走这里；这里是在它**没**声明时兜底 ——
        「注册表里有个叫 veo3.1-quality 的适配器，目录里记着它单段 8 秒」，
        比让流水线猜要强。
        """
        found = self.by_provider(provider)
        if not found:
            return None
        return sorted(found, key=lambda m: (m.cost, m.id))[0].caps(**size)

    def credit_lines(self, kind: str = "") -> List[str]:
        pool = self.models.values() if not kind else self.by_kind(kind)
        return [m.credit for m in sorted(pool, key=lambda m: (m.kind, m.cost, m.id))]

    # ———————— 装载

    @classmethod
    def load(cls, path: Optional[Path]) -> "ModelCatalog":
        """读目录。读不动就退回内置目录，但**把原因记下来**。

        这里和质检规则的处理一致：不静默降级。用内置目录跑出来的结果
        与用你的目录跑出来的结果可能不一样（比如单段上限 8 秒 vs 12 秒），
        所以"我在用哪份目录"必须能被看见。
        """
        if path is None or not Path(path).exists():
            cat = cls.builtin()
            cat.problem = f"未找到 {path}，正在用内置目录"
            return cat
        try:
            raw = parse(Path(path).read_text(encoding="utf-8"), source=Path(path).name)
        except (ParseError, OSError) as exc:
            cat = cls.builtin()
            cat.problem = f"{type(exc).__name__}: {exc}"
            return cat
        return cls._from_raw(raw)

    @classmethod
    def _from_raw(cls, raw: Dict[str, Any]) -> "ModelCatalog":
        ref_raw = raw.get("ref_fields") or {}
        known_ref = {f.name for f in _dc_fields(RefFields)}
        ref = RefFields(**{k: str(v) for k, v in ref_raw.items() if k in known_ref})

        defaults = {str(k): str(v) for k, v in (raw.get("defaults") or {}).items()}

        models_raw = raw.get("models") or {}
        known = {f.name for f in _dc_fields(ModelEntry)} - {"id"}
        models: Dict[str, ModelEntry] = {}
        for model_id, body in models_raw.items():
            if not isinstance(body, dict):
                raise ParseError(
                    f"models.yaml：模型 '{model_id}' 下面应该是若干 `键: 值`，"
                    f"实际读到了 {type(body).__name__}"
                )
            unknown = set(body) - known
            if unknown:
                # 同质检规则：不认识的键报错，不静默忽略。
                raise ParseError(
                    f"models.yaml：模型 '{model_id}' 里有不认识的字段 {sorted(unknown)}。"
                    f"可用字段：{sorted(known)}"
                )
            kwargs: Dict[str, Any] = {"id": str(model_id)}
            for key, value in body.items():
                if key in ("resolutions", "aspect_ratios"):
                    kwargs[key] = tuple(_as_list(value))
                else:
                    kwargs[key] = value
            entry = ModelEntry(**kwargs)
            if entry.kind not in KINDS:
                raise ParseError(
                    f"models.yaml：'{model_id}' 的 kind='{entry.kind}' 不是已知形态 {KINDS}"
                )
            if entry.tier not in TIERS:
                raise ParseError(
                    f"models.yaml：'{model_id}' 的 tier='{entry.tier}' 不是已知档次 {TIERS}"
                )
            models[entry.id] = entry

        version = raw.get("version", "1")
        return cls(
            version=str(version) if version is not None else "1",
            ref_fields=ref,
            models=models,
            defaults=defaults,
        )

    @classmethod
    def builtin(cls) -> "ModelCatalog":
        """内置目录：只保证 dryrun 能跑。

        刻意很小。内置的东西一旦丰富起来，就会变成"另一份要维护的配置"，
        而它和文件里的那份迟早不一致。
        """
        return cls(
            version="1",
            ref_fields=RefFields(),
            defaults={"video": "dryrun"},
            models={
                "dryrun": ModelEntry(
                    id="dryrun", kind="video", provider="dryrun", tier="fast", cost=1,
                    max_duration=8.0, resolutions=("720p",),
                    aspect_ratios=("9:16", "16:9", "1:1"),
                    strengths="本地假渲染，不出网、不花钱、无 key",
                ),
            },
            problem="内置目录（只保证 dryrun 可用）",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "problem": self.problem,
            "ref_fields": self.ref_fields.to_dict(),
            "defaults": dict(self.defaults),
            "models": {k: v.to_dict() for k, v in self.models.items()},
        }


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


# ————————————————————————————————————————----
# 便捷入口
# ————————————————————————————————————————----


def load_catalog(root: Optional[Path] = None) -> ModelCatalog:
    """从 <root>/config/models.yaml 读；给 None 就只要内置目录。"""
    if root is None:
        return ModelCatalog.builtin()
    return ModelCatalog.load(catalog_path(root))


def default_catalog() -> ModelCatalog:
    """按当前工作目录找目录文件。CLI 用这个。"""
    return load_catalog(Path.cwd())
