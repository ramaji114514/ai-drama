"""ai-drama — 一个可替换底座的 AI 短剧 Agent 骨架。

设计要点（也是它和"能跑的 demo"的区别）：

  1. **Provider 全抽象**：换模型不改流水线代码（ai_drama/providers/base.py）
  2. **每一步落盘**：SQLite + episode.json，进程挂了能 resume（ai_drama/core/store.py）
  3. **成本可记账 + 预算闸门**：超预算显式失败，不静默降级（ai_drama/core/context.py）
  4. **音频是一等公民**：台词独立成轨，按镜头对齐（ai_drama/stages/stage05_audio.py）
  5. **确定性归代码，创意归 skill**：拼接/对齐/转码用 ffmpeg，
     选题/剧本/分镜的提示词放 skills/ 里随时改

这里刻意**不做** eager import：core / providers / stages 各自独立，
想要什么显式 import（`from ai_drama.core.models import Shot`），
避免包级循环依赖这种低级但致命的坑。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
