# ai-drama

**AI 短剧流水线骨架** —— 可替换底座 + 可恢复流水线 + 成本可记账。

一句话：**厂商会换，流水线不该改。**

```bash
# 零 API key、零配置，直接跑通整条流水线
python run.py run --idea "深夜加班回家，发现自家门锁被换过，猫眼从里面被贴住了"
```

跑完你会拿到一个真的 `final_video.mp4`（竖屏 720×1280、带音轨、可播放），
以及一份精确到每个镜头的成本账单。

---

## 这是什么 / 不是什么

**是**：一条能跑通的六阶段流水线。每个阶段独立、可重跑、有落盘、
有缓存、有成本记账、有断点续跑。创意层（提示词）和骨架层（流程）分离。

**不是**：一个"接上就能出片"的成品。默认的 `dryrun` provider 只是
**本地假渲染** —— 它证明流程是通的、账单是准的、断点是真的能续的，
但它画出来的不是电影。

想看到真画面，把任一 provider 换成真厂商即可（见下面「换厂商」一节）。

---

## 快速开始

### 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.10 | 只用了标准库 + Pillow |
| Pillow | 任意 | 画占位图。`pip install pillow` |
| ffmpeg | 任意 | 拼接、转码、混音、烧字幕。**必须在 PATH 里** |

可选：中文字体。Windows 用系统自带的 `msyh.ttc`，Linux/macOS 会自动找
`Noto Sans CJK` / `PingFang`。找不到字体不会崩，只是占位图上的字变成方块。

### 三步

```bash
pip install pillow

python run.py doctor      # 自检：python / ffmpeg / 字体 / provider 配置
python run.py run --idea "你的想法"
```

`doctor` 会打印每个 provider 的**实际生效值**（环境变量 → 用哪个），
以及数据库路径和 `skills/` 目录位置。

### 完整命令表

| 命令 | 作用 |
|---|---|
| `doctor` | 环境自检 |
| `providers` | 列出已注册的 provider |
| `runs` | 历史 run 列表 |
| `run --idea "..."` | 跑一条完整流水线 |
| `resume <run_id>` | 断点续跑（只重跑未完成的） |
| `status <run_id>` | 逐镜进度 |
| `shots <run_id> [--format md\|json]` | 导出分镜表 |
| `cost <run_id>` | 成本明细（按阶段/按 provider 汇总） |

`run` 的常用参数：

```
--idea        一句话想法（必填）
--style       视觉风格
--scenes      场次数（默认 3）
--shots       每场镜头数（默认 4）
--stages      只跑指定阶段，逗号分隔：script,storyboard,frames,video,audio,concat
--budget      预算上限（美元），超了抛异常停止。0 = 不限
--concurrency 并发数（默认 3）
--force       忽略缓存全部重跑
```

---

## 架构

```
ai-drama/
├── run.py                      入口
├── ai_drama/
│   ├── cli.py                  命令行
│   ├── core/
│   │   ├── models.py           Shot / Scene / Episode / Character / Run / CostEntry
│   │   ├── store.py            SQLite 落盘 + 成本记账（断点续跑的基础）
│   │   ├── pipeline.py         六阶段编排
│   │   ├── context.py          StageContext + Ledger（成本闸门）
│   │   ├── paths.py            产物路径约定（dryrun 与真厂商落在同一处）
│   │   └── media.py            ffmpeg 封装：探测/拼接/缩放/混音/烧字幕
│   ├── providers/
│   │   ├── base.py             五个 ABC + 重试 + 定价表  ← 全项目最重要
│   │   ├── registry.py         注册表 + build_bundle() 依赖注入点
│   │   ├── dryrun.py           本地假渲染（零成本跑通全流程）
│   │   └── _template.py        接真厂商时照这个补
│   └── stages/
│       ├── stage01_script.py      想法 → 分场分镜表
│       ├── stage02_storyboard.py  定妆照 + 提示词 + 时长核算
│       ├── stage03_frames.py      关键帧（并发）
│       ├── stage04_video.py       镜头视频（并发 + 独立重试）
│       ├── stage05_audio.py       配音 + BGM
│       └── stage06_concat.py      拼接 + 混音 + 字幕
├── skills/                     创意层：提示词与判断标准（见 skills/README.md）
├── tests/                      测试
└── runs/<run_id>/              每次运行的产物
    ├── episode.json            分镜定稿 —— 人手可改，改完直接 resume
    ├── character_<名>.png      定妆照
    ├── scenes/sc01/
    │   ├── shot_000_keyframe.png
    │   ├── shot_000_clip.mp4
    │   ├── shot_000_audio.wav
    │   └── scene_video.mp4
    ├── final_video.mp4
    └── cost_report.json
```

### 五条设计决定（以及为什么）

**1. `submit + poll`，而不是一个阻塞的 `generate()`**
视频模型动辄排队几分钟。阻塞式接口没法做并发、没法做超时、
更没法在进程重启后恢复。提交拿 id、轮询查状态，是唯一正确的形态。
（见 `ai_drama/providers/base.py:119` 的 `VideoProvider`。）

**2. 每次调用都返回 `(返回值, usage)`**
provider 自己**不记账**，它只报告"我花了多少"，由 `Store` 统一落盘。
这样成本统计永远和实际调用一一对应，不会漏记也不会重记。

**3. 失败就可见，绝不静默降级**
参考实现里最常见的反模式是：出错时返回一段写死的假数据 ↓
于是你"跑通了"，但永远得到同一部片，而且不知道它坏过。
本骨架的选择是：失败写进数据库（保留现场）、预算超限**抛异常**、
没有 provider 就**跳过**并明确告诉你跳过了。（见 `core/context.py` 的 `heal_shot`。）

**4. 缓存 + 断点续跑是基建，不是优化**
一条 60 秒的片子按真厂商价格量级要 5–20 美元。
跑到第 30 个镜头炸了，必须能只重跑那一个。
每个镜头的产物落盘 + 状态写库，`resume` 时只碰未完成的。

**5. 路径集中在一处**
`core/paths.py` 是唯一的产物路径约定。dryrun 和真厂商落在同一处，
否则你切到真模型之后会得到两套目录结构，前一次的缓存全部失效。

---

## 换厂商：只改一行

`stages/` 里的代码**从不认识任何厂商**。它们只调用抽象接口：

```python
# 阶段代码长这样 —— 没有厂商名
usage = ctx.bundle.image.png(prompt, path, ref_image=portrait)
job = ctx.bundle.video.submit({"prompt": vp, "image_path": frame, "duration": 3.0})
state, usage, out = ctx.bundle.video.poll(job)
```

切换靠**环境变量选 provider 名**：

```bash
AI_DRAMA_TEXT=dryrun      # 也可以是真厂商名，见 providers/
AI_DRAMA_IMAGE=dryrun
AI_DRAMA_VIDEO=dryrun
AI_DRAMA_AUDIO=dryrun
AI_DRAMA_T2I=             # 留空 = 跳过定妆照
```

### 接一个真厂商要写多少

打开 `ai_drama/providers/_template.py`，照着手写一个类，
然后在你自己的模块里 import 它（注册是装饰器一行）：

```python
@register("my-vendor", "video")
class MyVideo(VideoProvider):
    name = "my-vendor"

    def submit(self, request):  ...   # 调一次 HTTP，返回 job id
    def poll(self, job_id, *, timeout=300.0):  ...  # 轮询直到完成
    def fetch(self, job_id, dest):  ...  # 下载到 dest
```

大约 30–60 行。**不用碰 pipeline、不用碰 stages、不用碰 store。**

三个必须遵守的约定：
- 错误统一抛 `ProviderError(msg, retryable=True/False)`。
  网络抖动/429/5xx 标 `retryable=True`；参数非法/内容违规标 `False`。
  重试逻辑骨架已经写好了（指数退避 + 尊重 `retryable`）。
- `usage` 必须如实报告 `{"unit": "second", "amount": 4.0, "money": 0.32}`。
  定价基准在 `base.py:209` 的 `PRICING`，接真厂商时按实际改。
- 密钥从环境变量读，**不要写进代码**。命名约定：
  `AI_DRAMA_<PROVIDER>_API_KEY` / `AI_DRAMA_<PROVIDER>_BASE_URL`。
  见 `.env.example`。

---

## 骨架 vs skills：按"会不会变"切

| | 放什么 | 为什么 |
|---|---|---|
| **骨架**（`ai_drama/`） | 状态机、断点续跑、成本记账、ffmpeg 拼接/转码/字幕、并发调度 | 确定性流程，不该让 LLM 每次重新发明 |
| **skills**（`skills/`） | 选题、剧本、分镜、角色卡、镜头提示词、质检标准 | 创意判断，随品味迭代，写死在代码里会僵化 |

两者通过**结构化产物**对接：skill 产出 JSON，骨架消费 JSON。

`runs/<run_id>/episode.json` 就是这个契约。你可以**手改它，然后 `resume`**：

```bash
# 觉得第 3 个镜头时长不对，直接改
notepad runs/run_xxxxxxxx/scenes/../episode.json
# 只重跑受影响阶段，前面的缓存和已花的钱都保住
python run.py resume run_xxxxxxxx
```

---

## 与同类项目的差别

有两个热度很高的开源实现值得我们对照（都读过代码，这里只做定位，不评价人）：

| | storyforge | Open-AI-Micro-Drama-Generator | **本项目** |
|---|---|---|---|
| 定位 | 生产级、自托管全家桶 | 能出片的最小演示 | 可替换骨架 |
| 依赖 | 需自建 ComfyUI / CosyVoice / Wan2.1 等一整套模型服务 | 一家聚合 API（单 key） | 零依赖可跑，厂商即插即用 |
| 音轨 | 有 | **无**（对白被当画面提示词喂给视频模型） | 有（TTS + BGM + 混音） |
| 持久化 | Postgres + Redis + Qdrant + MinIO | **无**（状态在进程内存里，中间产物只有 mp4） | SQLite，逐镜落盘 |
| 断点续跑 | 有 | 无 | 有 |
| 成本记账 | 有 | 无 | 有（精确到镜 + 预算闸门） |
| 失败处理 | — | 静默返回假数据/占位视频 | 失败可见，不降级 |

结论：**都不是"拿来当底座"的好选择**。
storyforge 工程完备但要求你先把一整套模型服务跑起来；
microdrama 跑起来便宜但缺音轨、缺持久化，而且出错会假装成功。
本项目把两边真正有价值的部分（依赖抽象的形态、分镜的写法、
可恢复编排的必要性）重新组织成一个**你今天就能跑起来**的骨架。

---

## 测试

```bash
python -m pytest -q
```

覆盖范围（26 个用例）：

| 文件 | 测什么 |
|---|---|
| `test_models.py` | 时长推导（台词主导）、Episode 序列化往返、**阶段顺序即契约** |
| `test_store.py` | SQLite 惰性建库、run 生命周期、逐镜落盘、`sync_shots` 幂等、成本按 provider/阶段聚合、**预算闸门抛异常而不是降级** |
| `test_providers.py` | 默认是 dryrun（零 key 可跑）、未知 provider 明确报错、五个 ABC 的接口一致性、`submit→poll→fetch` 契约、**重试能恢复**、**retryable=False 不重试**、预算异常不可重试 |
| `test_pipeline.py` | 全流程产出可播放成片、**第二次跑零成本且幂等**、**中断后 resume 只补缺的**、失败会被大声报出来 |

`tests/conftest.py` 里有 `need_ffmpeg` 夹具：没装 ffmpeg 时相关用例会跳过
而不是失败 —— 环境缺失不该被误报成代码缺陷。

---

## 路线图

- [x] 六阶段可恢复流水线 + 成本记账 + 预算闸门
- [x] dryrun provider（零成本端到端）
- [ ] **口型同步**（第八环节，当前缺失）——
      需要先有人脸关键点或音频驱动的模型，属于 provider 层的新抽象
- [ ] **角色一致性自动化**：定妆照 → 关键帧的面部相似度校验，
      现在只有机制没有校验（见 `skills/04-character-bible.md`）
- [ ] **质检报告**：`skills/06-qc.md` 的清单目前是文档，没有代码实现
- [ ] 音效层（现在只有台词和 BGM）
- [ ] 竖屏外的其他画幅（横屏 / 方形）
- [ ] Web 界面（现在只有 CLI）

## 许可

MIT，见 [LICENSE](LICENSE)。
