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

**是**：一条能跑通的八阶段流水线。每个阶段独立、可重跑、有落盘、
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

只想跑测试的话再装一个 pytest：`pip install pytest`（测试不需要 API key，
但需要 ffmpeg 的那几个用例会在没装时自动跳过）。

`doctor` 会打印每个 provider 的**实际生效值**（环境变量 → 用哪个），
以及数据库路径和 `skills/` 目录位置。

### 拍多集

一次 run 出一集。要拍一部 60 集的剧，就得让角色跨集保持一致 ——
否则第 2 集的脸就跟第 1 集不一样了，而**报告里一切正常**：

```bash
python run.py project new "深夜外卖" --style "冷色调，手持感"
# -> project_id = proj_xxxxxxxx

python run.py run --project proj_xxxxxxxx --idea "第1集：暴雨夜送到一栋没人的楼"
python run.py run --project proj_xxxxxxxx --idea "第2集：她在地下二层听见自己的名字"
# 第 2 集不会再生成苏蔓的定妆照 —— 直接用第 1 集那张图
```

第一集跑完，角色和定妆照就自动写进了 `projects/<id>/project.json`；
之后每一集都复用同一批文件。要改角色（换发型、改年龄、定音色），
直接改那个 json，或者 `python run.py project set <id> 苏蔓 --voice female_07`。

细节、边界和"为什么不是靠提示词锁住脸"写在
[docs/making-a-series.md](docs/making-a-series.md)。

### 完整命令表

| 命令 | 作用 |
|---|---|
| `doctor` | 环境自检 |
| `providers` | 列出已注册的 provider |
| `models` | 模型目录：能力上限、参考素材字段、默认模型 |
| `rules` | 当前生效的质检规则，以及它来自文件还是内置默认值 |
| `skills` | 技能目录；某一环节实际会加载哪几篇、常驻成本多少 |
| `faces <run_id>` | 谁的脸算准：定妆照多张并存时给出基准与冲突清单 |
| `project new/list/show/set` | 一部剧的**角色圣经**：角色与定妆照跨集复用 |
| `runs` | 历史 run 列表 |
| `run --idea "..."` | 跑一条完整流水线 |
| `resume <run_id>` | 断点续跑（只重跑未完成的） |
| `status <run_id>` | 逐镜进度（含已提交、还没拿回产物的未结算任务） |
| `shots <run_id> [--format md\|json]` | 导出分镜表 |
| `cost <run_id>` | 成本明细（按阶段/按 provider 汇总） |
| `qc <run_id> [--format text\|json]` | 质检报告（返回码 0=通过 2=不通过 **3=没查成**） |

`run` 的常用参数：

```
--idea        一句话想法（必填）
--style       视觉风格
--scenes      场次数（默认 3）
--shots       每场镜头数（默认 4）
--stages      只跑指定阶段，逗号分隔：script,storyboard,frames,video,audio,lipsync,concat,qc
--budget      预算上限（美元），超了抛异常停止。0 = 不限
--concurrency 并发数（默认 3）
--force       忽略缓存全部重跑
--no-strict-qc  质检非 PASS 也照样把 run 标成 done（默认是严格模式）
--project     归到某部剧下：角色与定妆照跨集复用（见下面的「拍多集」）
--no-subtitles  不把字幕烧进画面（SRT 照样写出来，可外挂）
```

---

## 架构

```
ai-drama/
├── run.py                      入口
├── config/
│   ├── models.yaml             模型目录：能力上限 / 参考素材字段 / 单价
│   └── qc_rules.yaml           质检规则（数据不是代码，改这里不用改 Python）
├── ai_drama/
│   ├── cli.py                  命令行
│   ├── core/
│   │   ├── models.py           Shot / Scene / Episode / Character / Run / CostEntry
│   │   ├── store.py            SQLite 落盘 + 成本记账 + 未结算任务台账
│   │   ├── pipeline.py         八阶段编排 + 质检闸门
│   │   ├── context.py          StageContext + Ledger（成本闸门）
│   │   ├── paths.py            产物路径约定（dryrun 与真厂商落在同一处）
│   │   ├── media.py            ffmpeg 封装 + 单段时长拆分 + 字幕（SRT/ASS）
│   │   ├── catalog.py          模型目录：caps / ref_fields / 默认模型
│   │   ├── miniyaml.py         零依赖 YAML 子集（配置需要能写注释）
│   │   ├── qc.py               确定性质检 + 三态判定（pass/fail/**unchecked**）
│   │   ├── identity.py         参考锁定 + 一致性判定解析（纯函数）
│   │   ├── faces.py            多张脸谁当准（纯函数，可离线测）
│   │   ├── vision_qc.py        要**看图**的质检（默认关，见设计决定 14）
│   │   ├── skills.py           技能两级披露：预筛 → 选择 → 注入
│   │   └── project.py          角色圣经：跨集复用角色 / 定妆照 / 音色
│   ├── providers/
│   │   ├── base.py             七个 ABC + 能力声明 + 重试 + 定价表  ← 全项目最重要
│   │   ├── registry.py         注册表 + build_bundle() 依赖注入点
│   │   ├── dryrun.py           本地假渲染（零成本跑通全流程）
│   │   └── _template.py        接真厂商时照这个补
│   └── stages/
│       ├── stage01_script.py      想法 → 分场分镜表
│       ├── stage02_storyboard.py  定妆照 + 参考锁定 + 提示词 + 时长核算
│       ├── stage03_frames.py      关键帧（并发）
│       ├── stage04_video.py       镜头视频（并发 + 拆段 + 独立重试）
│       ├── stage05_audio.py       配音 + BGM
│       ├── stage06_lipsync.py     口型同步（只做有台词的镜头）
│       ├── stage07_concat.py      拼接 + 混音 + 烧字幕（先写时间轴）
│       └── stage08_qc.py          质检（确定性 + 可选的画面校验）
├── skills/                     创意层：提示词与判断标准（见 skills/README.md）
├── docs/
│   ├── architecture.md         设计决定与硬约束
│   └── making-a-series.md      拍 60 集：角色圣经怎么用
│   └── adding-a-provider.md    换模型的那几十行
├── tests/                      测试
├── projects/<project_id>/      跨集共用的东西（一部 60 集的片子只有一个）
│   ├── project.json            角色圣经 —— 人手可改，改完下一集生效
│   └── portraits/<角色名>.png   定妆照：复用同一张是脸不漂的唯一硬保证
└── runs/<run_id>/              每次运行的产物
    ├── episode.json            分镜定稿 —— 人手可改，改完直接 resume
    ├── character_<名>.png      定妆照
    ├── timeline.json           每个镜头在成片里的起止秒数（字幕与对齐共用）
    ├── subtitles.srt           字幕交付物（标准 SRT，可直接上传平台）
    ├── subtitles.ass           烧录用的 ASS（带样式，字号是真实像素）
    ├── qc_report.json          质检报告
    ├── scenes/sc01/
    │   ├── shot_000_keyframe.png
    │   ├── shot_000_clip.mp4
    │   ├── shot_000_audio.wav
    │   └── scene_video.mp4
    ├── final_video.mp4
    └── cost_report.json
```

### 十四条设计决定（以及为什么）

**1. `submit + poll`，而不是一个阻塞的 `generate()`**
视频模型动辄排队几分钟。阻塞式接口没法做并发、没法做超时、
更没法在进程重启后恢复。提交拿 id、轮询查状态，是唯一正确的形态。
（见 `ai_drama/providers/base.py` 的 `VideoProvider`。）

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

**6. 能力上限是数据，不是我的猜测**
"这个模型一次能生成几秒"写在 `config/models.yaml` 里，不在代码里。
15 秒的分镜喂给一个上限 8 秒的模型，它**不会报错** —— 它会按 15 秒的
信息量去编排画面，然后给你一段被截断的片子。所以超限的镜头由
`plan_shot_split()` 均分拆段、逐段渲染后拼接，并且：
一个镜头中途失败要**删掉已经渲好的分段**（半套分段拼出来的时长是错的）。
参考素材走哪个请求字段同样在配置里 —— 字段名写错时接口通常不报错、
只是静默忽略，这正是"参考图传了也漂"最常见的隐藏原因。

**7. 质检是流水线的必要环节，不是可选增强**
参考项目的复盘原话是："提示词里写了毛孔雀斑，出来还是磨皮脸" ——
光在提示词里写清楚没用，必须**提示词硬约束 + 描述清洗 + 生成后校验**
三件事一起上。所以质检有独立阶段、独立报告、独立返回码，
而且**结论是三态**：`pass` / `fail` / **`unchecked`（没查成）**。
第三态最关键：它最容易被默认值、空列表和异常捕获悄悄变成"通过"，
一旦如此，整条质检链就只是装饰。骨架里的处理是 —— "没查成"按未通过算，
重跑时补查，`IdentityVerdict.__post_init__` 强制 `unchecked ⇒ passed=False`。

**8. 参考图要在提示词里点名**
只把图传给模型**不等于**它会用。生成模型每次调用都是独立的，
它没有任何机制知道这一镜和上一镜是同一个人（"她"、"同一个女人"
这种指代对它毫无意义）。所以 `core/identity.py` 把参考图写成
`@图片1 = 角色「X」本人…` 并入提示词开头，并显式排除资产图版式 ——
参考图是白底证件照，不说清楚，模型会把白背景、站姿、正面构图、
影棚光一起搬进分镜画面。

**9. 契约由骨架验，不靠适配器自觉**
口型同步（`stage06_lipsync.py`）有两条硬契约：输出不能带音轨（否则每句台词
说两遍）、输出时长必须等于输入片段（否则 `stage07_concat` 按片段实际时长
累加的音画时间轴会整体错位，而且不报错）。这种事**不能写在文档里就算了** ——
骨架先让 provider 写到临时文件，用 ffprobe 量完对得上才原子替换成正式产物。
`tests/test_lipsync.py` 里有两个故意写坏的 provider 来证明它真的会拒收。
同一个理由，`config/qc_rules.yaml` 里不认识的键会**报错**而不是忽略，
`config/models.yaml` 里重复的键也是 —— "改了配置没生效"是最难发现的一类问题。

**10. 字幕的时间轴必须和音画对齐共用一份累加结果**
字幕飘掉几乎永远是"字幕自己又算了一遍时间"。所以 `stage07_concat.py` 在按
ffprobe 实际时长累加、决定每句台词落在哪一秒的**同一个循环**里生成字幕，
并把这份时间轴写成 `timeline.json` —— 字幕飘了的时候，第一件事就是打开它
核对每个镜头的起止秒数跟你以为的是不是一致。

另一个坑是字幕的**字号不是像素**：libass 读 SRT 时会假定脚本坐标系是
384x288，于是 `force_style` 里的 FontSize / MarginV 都按 (视频高/288) 放大
（实测 720x1280 下 FontSize=22 渲染出约 90px 的字，直接糊在画面底部；
加 `original_size=` 也没用，它说的是"这份字幕按多大尺寸排的"，不改变坐标系）。
所以本骨架**自己生成 ASS** 并把 `PlayResX/PlayResY` 设成视频真实尺寸，
字号与边距就是真正的像素，同一套比例参数在竖屏横屏上都成立。

还有一条：**烧录不等于字幕**。SRT 一律写出来当交付物（多数平台要的是字幕轨，
不是烙在画面上的字），烧不烧由 `--no-subtitles` / `AI_DRAMA_SUBTITLES` 决定。
最后，`tests/test_subtitles.py` 里有一条**像素级**断言 ——
"字幕文件生成了"完全不代表画面上有字：滤镜写错、路径没找到、字体缺字，
ffmpeg 全都不会报错，它只是什么都不画。

**11. 付费提交的任务，id 必须在付钱的那一刻就落盘**
视频生成是整条链最贵的一环，而"提交成功"到"拿到文件"之间隔着几分钟排队。
进程在这个窗口里死掉（Ctrl-C、断电、容器被回收）是完全正常的事：

```
提交成功 -> 排队 -> [进程没了] -> resume -> 又提交一次 -> 又付一次钱
```

而厂商那边其实已经有一份在跑了。第一版骨架就是这个行为 —— job_id 只活在
`_render_shot()` 的局部变量里。现在 `store.shot_jobs()` 按拆段序号记着
"已提交、还没结算"的任务：提交成功后**第一件事是落库**，然后才去轮询；
`resume` 先认领记录里的任务再决定要不要重新提交。

这里有一个必须区分开的语义：**"厂商说失败"和"我们等不下去"不是一回事**。
前者是终局（记录要清掉，否则这一镜会被永远认领到一个死任务上、再也渲不出来），
后者说明任务多半还在跑（记录必须留住，丢掉就等于重复付费）。`_poll_one()`
因此返回三态 `done` / `failed` / `timeout`，而不是把后两者都塞进 `failed`。
`tests/test_job_ledger.py` 里用 `KeyboardInterrupt`（`_poll_one` 只吞 `Exception`，
所以这一下真能穿出去）模拟"进程在等待中被 Ctrl-C"，然后把"resume 不再 submit"
钉死。CLI 的 `status` 会把未结算任务列出来 —— 否则用户只会觉得
"怎么又扣了一次钱"。

**12. 跨集一致性靠"复用同一个文件"，不靠"提示词写得像"**
默认一次 run 出一集，角色由分镜阶段的 LLM **每集重新发明**。这对一部 60 集的
竖屏短剧是致命的：

```
第 1 集 -> LLM 造出"苏蔓，31 岁，短发" -> 定妆照 A -> 全片用 A
第 2 集 -> LLM 再造一个"苏蔓，30 岁，大波浪" -> 定妆照 B -> 全片用 B
```

两集各自内部是自洽的（每集都复用了自己那张定妆照），**集与集之间不是** ——
所以没有任何一步会报警，只有观众会发现"换人了"。而且集内那套一致性工具
（`core/faces.py`、`core/identity.py`）全是集内的，跨集用不上。

修法是 `core/project.py` + `projects/<id>/project.json` 这份**角色圣经**：
在册角色的名字、外貌描述、定妆照路径都记在里面；每一集都带 `--project <id>`，
于是第 2 集开始**直接复用第 1 集那张定妆照文件** —— 不重新生成，所以既不
花钱也不可能漂。提示词里的锁定（设计决定 6）是第二道防线，不是第一道。

圣经是**文件不是数据库**，和 `episode.json` 一个待遇：制片说"苏蔓改成短发"，
改那个 json 就行，不需要动代码、也不需要重跑第 1 集。新角色可以正常登场
（第 12 集冒出个新反派是正常创作），这一集生成定妆照后**自动入册** ——
圣经是随剧集生长的，不是一开始就要写全。

合并那一步是"改不坏"的兜底：在册角色**一律以圣经为准**，模型这一集把苏蔓
写成什么样都不算数；模型漏写某个在册角色时，他也会照样进本集卡司（漏写是
模型最常见的失误，而漏一次这一集就锁不住脸）。`--project` 打错字会**当场停下**，
绝不静默降级成"每集重新造角色"。

已知边界：目前只包住**角色**。场景/地点、同一角色不同集换装、以及按**名字**
匹配角色（同名不同人、改名、卧底身份会错配）都还没解决 —— 要做扎实得给每个
角色一个稳定 id，让分镜阶段输出 id 而不是名字。

**13. 谁在画面里，由剧本说，不由字符串查找说**
判断"这一镜该拿谁的定妆照锁脸"，第一版是**猜**的：在 `action` + `dialogue` 里
找角色名的子串。这有三个不出声的失败：

```
action: "朱锦娘推开门"        -> "朱锦" 也匹配上了（core/faces.py 为此写了特判）
action: "林晚与陈默隔桌对坐"   -> 谁的脸当基准？参考图上限通常只有 1 张
action: "苏曼站在雨里"        -> 名字写错一个字，一个人都匹配不到
```

三种情况**都不会报错**，只会让人在成片里看到一张陌生的脸。所以现在由分镜阶段的
模型显式输出 `Shot.characters = [name, ...]`，顺序即"谁更重要"。

两个容易写错的细节：

* **`None`（没声明）和 `[]`（声明画面里没人）必须分开。** 合并成一种，要么
  空镜被迫锁一张不该出现的脸，要么所有旧 `episode.json` 都被当成空镜、一个人
  都不锁。旧 run 走 `None` 这条退路（退回猜），并在报告里**写明它猜了** ——
  否则用户会把"能跑"当成"猜得对"。
* **锁脸跟着声明走，告警跟着并集走。** 拥挤告警（出镜人数 > 参考图上限）必须
  按"声明 ∪ 文本推断"算：模型少声明一个人时，如果告警也跟着变少，那它就变成
  "只要模型不写就永远不报警"——比没有还糟，因为它训练人相信"没告警就是没问题"。

`characters` 里点了一个不在卡司里的名字（写错字），这一镜就没有参考图 ——
报告里会把那个名字原样列出来。

余下的边界：仍然按**名字**匹配角色。同名不同人、改名、多重身份（卧底）还是会
错配，要做扎实得给角色一个稳定 id。

**14. 看不见，不等于没问题**
质检的第三态（`UNCHECKED`）在接上视觉模型之前只覆盖"文件还没生成"这类情况。
接了之后它立刻变成整个检查层的主轴：

```
没配 AI_DRAMA_VISION        -> 没查成（不是通过）
调用失败 / 超时             -> 没查成
模型输出读不出来            -> 没查成
模型说"我看不清这张图"      -> 没查成
```

三种情况有一个共同点：**你并不知道画面到底有没有问题**。把它们写成"通过"，
成片就会带着从没被检查过的镜头交付，而且报告里连痕迹都没有。

所以这一层有两个和别处**刻意相反**的默认值：

* **provider 默认关闭。** 另外六种形态没配就是 dryrun（零配置也能出片），
  vision 没配就是**没有**。一个"假装看过了"的默认检查器比没有检查器更糟 ——
  它会把「没查成」洗成「通过了」。连显式配 `dryrun` 也只得到「没查成」：
  `DryRunVisionProvider` 直接抛错并说明"画面没有被任何人看过"。
* **两项检查默认关。** 每查一镜就是一次带图的调用，60 集 × 每集 20 镜 = 1200 次。
  默认打开的结果是所有人第一天就把整个质检关掉。

另外两条：判定逻辑**一行都没有重写**（提示词用 `core/identity.CHECK_PROMPT`、
解析用 `parse_identity_verdict`，provider 只做"消息进、文本出"，
于是最要紧的几个用例不需要任何 API key 就能测）；结论并进**同一份** `QcReport`
而不是另发一份文件 —— 一份报告写"block 0"、另一份写"画面里有字"，等于没查。

它**只报不改**：一次误判就重渲一镜是真金白银，而"重渲之后还是不对"的下场是
把钱花在同一个问题上。重跑由你决定。

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
AI_DRAMA_T2I=dryrun       # 定妆照。写 none 才跳过（跳过 = 参考锁定整段失效）
AI_DRAMA_LIPSYNC=dryrun   # 口型同步。none = 不做（成片嘴型不对齐，报告里会说）
# AI_DRAMA_VISION=...     # 画面校验（第七种形态）。**默认关**，见下
```

`AI_DRAMA_VISION` 和上面六个别扭地不一样：**它没设就是关的**。
上面六个是"生产"形态，默认 dryrun 让零配置也能出片；它是"检查"形态，
而一个"假装看过了"的默认检查器比没有检查器更糟 —— 它会把"没查成"洗成
"通过了"。配上 `dryrun` 也一样：那一档会直接抛错，结果是「没查成」，
不会给你一个漂亮的满分。它能支撑的两项检查（画面上有没有字、
人像不像定妆照）在 `config/qc_rules.yaml` 里也默认关，因为每查一镜
就是一次调用。

另外三个不是 provider 的开关：

```bash
AI_DRAMA_SUBTITLES=0          # 不把字幕烧进画面（SRT 照样写出来）
AI_DRAMA_SUBTITLE_FONT=...    # 烧字幕用哪个字体（写字体名，不是路径）
AI_DRAMA_DB=...               # SQLite 落在哪
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

四个必须遵守的约定：
- 错误统一抛 `ProviderError(msg, retryable=True/False)`。
  网络抖动/429/5xx 标 `retryable=True`；参数非法/内容违规标 `False`。
  重试逻辑骨架已经写好了（指数退避 + 尊重 `retryable`）。
- `usage` 必须如实报告 `{"unit": "second", "amount": 4.0, "money": 0.32}`。
  定价基准在 `base.py` 的 `PRICING`，接真厂商时按实际改。
  模型的能力上限（单段几秒、支持几种分辨率）写进 `config/models.yaml`，
  不要硬编码在适配器里 —— 见「四条约定」第 4 条。
- 密钥从环境变量读，**不要写进代码**。命名约定：
  `AI_DRAMA_<PROVIDER>_API_KEY` / `AI_DRAMA_<PROVIDER>_BASE_URL`。
  见 `.env.example`。

**声明能力上限，别猜**。
给 provider 类加一行 `caps = ProviderCaps(max_duration=8.0, ...)`，
或者写进 `config/models.yaml`；骨架会据此拆段，而不是把 15 秒喂给
一个上限 8 秒的模型然后拿到一段被截断的片子。
更完整的清单见 [docs/adding-a-provider.md](docs/adding-a-provider.md)。

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
pip install pytest
python -m pytest
```

（`pytest.ini` 里已经配好 `testpaths = tests`，在仓库根目录直接敲 `python -m pytest`
就行。加 `-q` 会只剩进度点、看不到 `230 passed` 那行汇总。）

覆盖范围（230 个用例）：

| 文件 | 测什么 |
|---|---|
| `test_models.py` | 时长推导（台词主导）、Episode 序列化往返、**阶段顺序即契约** |
| `test_store.py` | SQLite 惰性建库、run 生命周期、逐镜落盘、`sync_shots` 幂等、成本按 provider/阶段聚合、**预算闸门抛异常而不是降级** |
| `test_providers.py` | 默认是 dryrun（零 key 可跑）、未知 provider 明确报错、**vision 例外：默认关闭且 dryrun 拒绝假装看过**、七种形态的接口一致性、`submit→poll→fetch` 契约、**重试能恢复**、**retryable=False 不重试**、预算异常不可重试 |
| `test_pipeline.py` | 全流程产出可播放成片、**第二次跑零成本且幂等**、**中断后 resume 只补缺的**、失败会被大声报出来 |
| `test_caps.py` | 能力声明缺失时**不限制**（fail-open）、超上限**均分拆段**、总和一秒不丢、provider **拒绝**超限请求而不是静默截断 |
| `test_catalog.py` | 零依赖 YAML 子集（引号内 `#`、并列列表、**重复键报错**、缩进错误带行号报错）、模型目录往返、**不认识的字段报错**、目录缺失时降级但说明原因 |
| `test_qc.py` | **三态判定**（没查成 ≠ 通过、检查器自身崩溃也算没查成）、时长/对白/重复/景别/钩子规则、**规则作用域**、不认识的配置键抛错 |
| `test_skills.py` | frontmatter 契约、作用域预筛、正文超预算不静默砍、冲突时写明谁优先、七篇自带技能全部可加载 |
| `test_identity.py` | 参考锁定段的编号与顺序、`NOT_INHERIT` 排除资产图版式、重生成提示**幂等**、判定解析（最低分门槛、`unchecked` 不说成通过） |
| `test_faces.py` | 基准排序（用户钉的 > 参考图包 > 散落产物）、自动定的三个条件、**定不了就停下来问人**、`朱锦`/`朱锦娘` 的名字包含边界 |
| `test_storyboard.py` | 出镜角色按出现位置排序、只对有参考图的角色点名、锁定段在提示词开头、**拥挤镜头被报出来而不是假装锁住** |
| `test_cli.py` | `doctor` 报的 provider **就是流水线真会用的那个**、配错名字当场报阻断、`.env` 真的会被读、`--help` 里一个子命令都不少 |
| `test_lipsync.py` | 只送有台词的镜头、输出**无音轨**且时长不变、**两个故意写坏的 provider 被骨架拒收**、已有产物不重复付费、没配 provider 时大声报出来、拼接优先用同步后的片段、`require_lipsync` 开关 |
| `test_vision_qc.py` | **「看不见」和「没问题」不是一回事**：provider 缺席 / 调用抛错 / 输出读不出来一律记「没查成」而**不是通过**；dryrun 视觉拒绝假装看过；缩图失败退回原图（不把「看不出问题」错报成「没查成」）；抽样**等距**而不是取前 N；一致性判定复用 `identity.parse_identity_verdict`（同一条回答两处结论必须一致）；结论并进**同一份** QcReport 且能挡住 done；钱要落到账本里 |
| `test_shot_cast.py` | 显式声明压过名字匹配、声明顺序即重要性、**空镜（`[]`）与没声明（`null`）是两回事**、告警按**声明∪文本**的并集算（模型少写一个人不能把告警弄没）、写错名字要报出来、旧 run 退回猜但**说出来它猜了** |
| `test_project.py` | 圣经是**可手改的文件**、模型改不掉在册角色的外貌、模型漏写角色也照样入卡司、**第 2 集复用同一个定妆照文件**（同时钉住没重复花钱和图是同一张）、改圣经下一集生效、项目打错字**大声停下**而不是降级成每集重造 |
| `test_job_ledger.py` | 提交后**开始轮询之前** job_id 已在库里、resume 只认领不重新提交（用 Ctrl-C 模拟进程被打断）、厂商报失败时记录被清掉、超时与失败被区分开、老库自动补列 |
| `test_subtitles.py` | 剧本行去说话人与表演提示、SRT 时间戳进位、**ASS 的 PlayRes 就是画布尺寸**（字号才是像素）、`{}`/`\` 被转义、**像素级断言字幕真的烧进了画面**、关掉烧录仍留下可用的 SRT |

`tests/conftest.py` 里有 `need_ffmpeg` 夹具：没装 ffmpeg 时相关用例会跳过
而不是失败 —— 环境缺失不该被误报成代码缺陷。

---

## 路线图

- [x] 八阶段可恢复流水线 + 成本记账 + 预算闸门
- [x] dryrun provider（零成本端到端）
- [x] **能力上限来自声明**：`ProviderCaps` + `config/models.yaml`，超限镜头拆段渲染
- [x] **质检是必要环节**：`stages/stage08_qc.py` + `config/qc_rules.yaml`，
      三态判定（通过 / 不通过 / **没查成**），严格模式下非 PASS 的 run 不算完成
- [x] **参考图在提示词里点名**：`core/identity.py` 的参考锁定段 + 排除资产图版式
- [x] **多张脸谁当准**：`core/faces.py` + `python run.py faces`（纯函数层，可离线测）
- [x] **角色圣经（project 层）**：`core/project.py` + `projects/<id>/project.json`。
      在册角色的外观与定妆照跨集复用——第 2 集开始**直接复用第 1 集那张图**，
      既不花钱也不可能漂；新角色自动入册（见设计决定 12）
- [x] **已付费任务的台账**：`stage04_video.py` 在提交成功后立刻把 job_id 落库，
      `resume` 先认领已付费的任务再决定要不要重新提交（见设计决定 11）
- [x] **字幕**：`stages/stage07_concat.py` 在算音画对齐的同一个循环里生成字幕，
      烧录走自写的 ASS（字号是真实像素），同时留下 `timeline.json` 与 `subtitles.srt`
- [x] **口型同步**：`stages/stage06_lipsync.py` + `LipSyncProvider`（第六种形态）。
      骨架强校验两条契约（无音轨、时长不变），坏适配器会被拒收而不是悄悄毁掉时间轴。
      默认 `dryrun` 只做无音轨重编码并自陈"没有真的改嘴型"；要真出效果按
      [adding-a-provider.md](docs/adding-a-provider.md) 接一家（sync.so / latentsync 之类）
- [x] **一致性校验接上视觉模型**：`core/vision_qc.py` + `VisionProvider`（第七种形态）。
      两项检查都默认**关**（每查一镜就是一次调用）：画面里有没有字、人像不像定妆照。
      判定逻辑复用 `core/identity.py`，结论并进同一份 `QcReport`。
      **没配 provider 时记「没查成」而不是「通过」**——这是这一层的全部意义（见设计决定 14）
- [x] **出镜角色由模型显式给出**：`Shot.characters`（见设计决定 13）。
      名字子串匹配降级为退路，并且会报出来
- [ ] **角色稳定 id**：`Shot.characters` 目前仍按**名字**匹配角色，
      同名不同人、改名、卧底身份还是会错配
- [ ] 音效层（现在只有台词和 BGM）
- [ ] 竖屏外的其他画幅（横屏 / 方形）
- [ ] Web 界面（现在只有 CLI）

## 许可

MIT，见 [LICENSE](LICENSE)。
