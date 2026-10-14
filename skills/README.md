# skills —— 创意判断层

这个目录里放的是**提示词与判断标准**，不是代码。

## 为什么和代码分开

一句话：**按"会不会变"切。**

| 层 | 放什么 | 特征 |
|---|---|---|
| `ai_drama/`（骨架） | 状态机、断点续跑、成本记账、ffmpeg 拼接/转码/字幕烧录、并发调度 | 确定性流程，不该让 LLM 每次重新发明 |
| `skills/`（本目录） | 选题、剧本、分镜设计、角色设定、镜头提示词、质检标准 | 创意判断，随品味迭代，写死在代码里会僵化 |

两者通过**结构化产物**对接：skill 产出 JSON，骨架消费 JSON。
中间那份 `runs/<run_id>/episode.json` 就是契约 —— 你可以手改它然后 `resume`，
骨架会照着你改过的分镜继续跑。

## 两级披露：为什么正文不常驻上下文

一篇技能的**元数据**（frontmatter）是常驻的，**正文**只在被选中时才加载。

| | 成本 | 什么时候进上下文 |
|---|---|---|
| `description`（一行） | 约 50 token / 篇 | 永远在 —— 模型靠它决定要不要读这篇 |
| 正文 | 2–5K token / 篇 | 只有被选中时 |

七篇技能的正文加起来一万多 token。做一集 45 秒的短片还好，
做 60 集长剧就是几十次连续调用 —— 全塞进去，窗口会被指导文档吃光，
而真正该被记住的前情反而挤没了。

**所以 `description` 是唯一真正影响效果的字段。** 一篇技能正文写得再精彩，
`description` 没说清"什么时候该用我"，它就永远不会被翻开。

写法：说**什么情况下用它**，不要说**它是什么**。
一句话，20–40 字，含触发场景和动作。

| | 例子 |
|---|---|
| 反例 | `抖音方法论` / `文案规范` |
| 正例 | `把一句话想法变成能抓着人的竖屏短剧选题，做选题和定总基调时用。` |

## frontmatter 契约

```markdown
---
name: showrunner                    # 必填。文件名可以不同，引用用这个
description: 把一句话想法变成能抓着人的竖屏短剧选题，做选题和定总基调时用。
scope: pipeline_stage               # account / content_type / pipeline_stage / global
applies_to: [短剧]                   # 内容形态。空 = 不限
stage: [选题]                        # 流水线环节。空 = 不限
priority: 70                        # 0-100，同作用域下数字大的优先
version: 1.0.0
owner: ramaji114514                 # 改坏了该找谁
status: active                      # active / draft / placeholder
---
```

规则说明：

- **`scope` 决定冲突时谁优先**：`account > content_type > pipeline_stage > global`。
  越具体越优先。注入时骨架会**写明哪条优先**，不留给模型猜 ——
  两篇指导矛盾时，模型会挑"看起来更权威"的那篇，而那不是"更该生效"的那篇。
- **`status` 不是 active 就不加载。** `placeholder` 的意义是"占了这个位置但还没写"，
  让它进上下文等于让模型读一篇空文档然后自己编。
- **不认识的键会报错**，不静默忽略。`stage` 写成 `stages`（或反过来）会让作用域
  失效，而失效的表现是"这几篇技能好像从来没生效过"——查起来极费劲。
  两种写法都认，但拼错成别的键就直接炸。
- **`applies_to`/`stage` 是规则预筛**：不匹配的文档在进入模型视野之前就被挡掉，
  不消耗任何 token。

## 目录

| 文件 | 环节 | scope | 产出什么 |
|---|---|---|---|
| `00-pipeline.md` | 总纲 | global | 十二环节流水线、四个真正的难点、责任划分 |
| `01-showrunner.md` | 选题 | pipeline_stage/选题 | 一句话 → 卖点 / 钩子 / 前三秒 / 标题 |
| `02-screenwriter.md` | 剧本 | pipeline_stage/剧本 | 想法 → 分场分镜表（`episode.json` 的 `scenes`） |
| `03-storyboard.md` | 分镜 | pipeline_stage/分镜 | 景别 / 机位 / 表演节拍 / 时长 |
| `04-character-bible.md` | 角色一致性 | **content_type** | 角色卡（身份词块，跨镜头复用） |
| `05-shot-prompt.md` | 镜头提示词 | pipeline_stage/提示词 | 每镜的图像提示词 + 视频提示词 |
| `06-qc.md` | 质检 | pipeline_stage/质检 | 一致性 / 节奏 / 音画对齐的验收清单 |

## 用法

**人用**：写新剧本前读 `00-pipeline.md`，然后按 `01 → 02` 往下走。

**agent 用**：不要全读。先看骨架给的目录：

```powershell
python run.py skills                          # 目录：每篇一行 description
python run.py skills --stage 剧本              # 这一环节会加载哪几篇，以及为什么
python run.py skills --show screenwriter      # 真要写的时候再取正文
```

**改进 prompt**：直接改这里的 md 文件。改完重跑 `python run.py run --force`
就能看到效果 —— 这就是把创意层从代码里拆出来的意义。

## 写新技能

1. 文件名 = `name` + `.md`，kebab-case 用英文（便于引用），中文写在正文里。
   以 `_` 开头的文件不会被加载（模板用）。
2. 先写 `description`，再写正文。`description` 决定它会不会被选中。
3. 单篇正文控制在 2–5K token（约 1500–3500 中文字）。写不下就拆成两篇、
   用 `stage` 区分 —— 一篇讲不透的事，放到两个环节去讲比硬塞进一篇更有效。
4. 体检：`python run.py skills --check` 会报出没写 owner、description 太短、
   正文超长的篇目。
