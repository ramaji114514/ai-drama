---
name: storyboard
description: 给每一镜定景别机位与表演节拍，把剧本翻译成画面时用。
scope: pipeline_stage
applies_to: [短剧]
stage: [分镜]
priority: 65
version: 1.0.0
owner: ramaji114514
status: active
---
# 03 · 分镜（Storyboard）

## 你要做的事

给每一镜定**景别、机位、表演节拍**。这是把"剧本"翻译成"画面"的一步。

## 景别词表（只用这些，骨架会翻译成模型提示词）

| 代号 | 含义 | 什么时候用 |
|---|---|---|
| `establishing` | 大远景 / 环境建立 | 场次开头，交代空间 |
| `wide` | 全景 | 两人关系、空间张力 |
| `medium` | 中景 | 对话主力景别 |
| `close` | 特写 | 情绪、关键道具、反转 |
| `extreme_close` | 大特写 | 瞳孔、手、屏幕、极强压迫 |
| `over_shoulder` | 过肩 | 对峙、窥视 |
| `pov` | 主观视角 | 让观众代入 |

**节奏口诀**：一场内景别不要重复三次以上，
`establishing → medium → close → extreme_close` 这种递进是最稳的。

## 机位与运动（写在 `action` 里，不要单独字段）

`action` 字段 = 画面内容 + 摄影机行为 + 光线。示例：

```
"老城区出租屋空镜，环境建立，光线偏暗，窗帘被风吹动，镜头轻微横移"
"林晚独自站在门口，背对镜头，手停在把手上不动，浅景深，冷调侧光"
"林晚的视线突然定住，瞳孔收缩，手持微晃，霓虹反光扫过脸颊"
```

三要素齐全的标准写法：**主体动作 + 机位/运动 + 光线质感**。

## 节拍检查

一镜一个节拍。检验方法：**把 `action` 念出来，如果需要停顿换气才能念完，那就是两镜。**

## 输出契约

分镜是**对已有 `scenes` 的补充**，不新建结构。给每个 shot 补齐：

```json
{"order": 0, "framing": "medium", "action": "...", "duration_sec": 3.0,
 "location": "老城区出租屋", "dialogue": ""}
```

如果是先有剧本再分镜，输出完整的 `scenes` 数组（结构同 `02-screenwriter.md`）。
如果是给现有分镜做复核，只输出**改动的镜头**：

```json
{"changes": [{"scene_id": "sc01", "order": 2, "framing": "extreme_close",
              "reason": "这一镜的情绪需要更压迫的景别"}]}
```

## 常见错误

| 错误 | 为什么错 | 改法 |
|---|---|---|
| 所有镜头都是 `medium` | 没有视觉节奏，观众疲劳 | 按景别递进排 |
| `action` 写"她很难过" | 抽象情绪，图像模型无法落地 | 写可见的：眼眶红、手在抖、背对镜头 |
| 一镜里有运镜又有两个动作 | 视频模型只学得会一个 | 拆成两镜 |
| 镜头里出现未登记的角色 | 拿不到参考图，一致性崩 | 要么登记进 `characters`，要么别写 |
| `duration_sec` 不随内容变化 | 全部 3 秒 = 机械感 | 空镜短、台词镜长 |

## 提示词模板

```
你是分镜师。给下面这场戏排镜头。

场次：{scene_json}
可用景别：establishing / wide / medium / close / extreme_close / over_shoulder / pov

要求：
- 景别要递进，同一场里不要重复三次以上
- action 必须包含：主体可见动作 + 机位/运动 + 光线质感
- 一镜一个节拍，不要把两个动作塞进一镜
- 有台词的镜头 duration_sec 按 字数/5 + 1 秒 估

只输出 JSON，结构见 skills/03-storyboard.md 的「输出契约」。
```
