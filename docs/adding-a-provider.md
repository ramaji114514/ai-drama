# 接一个真厂商

骨架承诺的一件事：**换模型不用改流水线**。这条承诺的完整含义是"只改一个
文件、几十行代码"，而不是"有个抽象层所以理论上可以换"。这份文档就是那几十行。

六种形态：`text`（写剧本）/ `image`（关键帧）/ `video`（镜头片段）/
`audio`（配音与 BGM）/ `t2i`（定妆照，可关）/ `lipsync`（口型同步，可关）。
每种形态一个环境变量，见下面各节。

## 三步

**1. 复制模板**

```powershell
copy ai_drama\providers\_template.py ai_drama\providers\your_vendor.py
```

**2. 实现 ABC，注册**

```python
from .base import ProviderCaps, ProviderError, VideoProvider, VideoSubmitRequest
from .registry import register


@register("your_vendor", "video")          # ← 第二个参数是形态
class YourVendorVideo(VideoProvider):
    name = "your_vendor"

    # 声明你的模型**真正的**上限。写错了不会报错，只会让成片被静默截断。
    caps = ProviderCaps(width=720, height=1280, fps=24, max_duration=8.0,
                        aspect_ratios=("9:16", "16:9"))

    def __init__(self, api_key: str = "", base_url: str = ""):
        self.api_key = api_key or os.environ.get("AI_DRAMA_YOUR_VENDOR_API_KEY", "")
        ...
```

**3. 指向它**

```powershell
$env:AI_DRAMA_VIDEO="your_vendor"
python run.py run --idea "..."
```

## 四个必须守住的约定

### ① 错误要分类，别一律吞掉

```python
ProviderError(msg, retryable=True)    # 网络抖动 / 429 / 5xx —— 骨架会退避重试
ProviderError(msg, retryable=False)   # 参数非法 / 内容违规 / 余额不足 —— 立刻抛出
```

把"余额不足"标成可重试，你会用真金白银换三次同样的失败。

### ② `usage` 必须如实报告

```python
{"provider": "your_vendor", "stage": "video", "unit": "second",
 "amount": 8.0, "money": 0.64}
```

成本报表、预算闸门、断点续跑的"已付过费不重复付"全都建立在这个字典上。
**报少了就是漏记，报假了就是账本不可信** —— 而这个骨架存在的理由之一
就是让"这一集花了多少钱"是个能回答的问题。

### ③ 能力上限来自你的声明，不是我的猜测

`caps.max_duration` 决定单镜要不要拆段（`core/media.plan_shot_split`）。
15 秒的分镜喂给一个上限 8 秒的模型，它不会报错 —— 它会按 15 秒的信息量
去编排画面，然后给你一段被截断的片子。所以：

* 你的适配器声明了 `caps`，就以你的为准；
* 没声明，骨架会去 `config/models.yaml` 找同名条目补上；
* 两边都没有，**按"不限制"处理**（fail-open），并在报告里说明。

### ④ 参考图要在提示词里点名

只把图传给模型**不等于**它会用。`core/identity.reference_block()` 生成
`@图片N = 角色「X」本人…` 那段，并且显式排除资产图版式（白背景、站姿、
正面构图）。参考图列表的顺序必须与提示词里的编号一致 —— 错一位模型不会
报错，它只会很认真地照着错的那张图渲染。

## 口型同步（第六种形态）

前四种形态是"生成"，`t2i` 是"只出图"，`lipsync` 是**已经出片之后**才做的一步：
输入一段画面 + 一段配音，输出一段对好嘴的画面。

```python
from .base import LipSyncProvider, ProviderError
from .registry import register


@register("your_vendor", "lipsync")
class YourVendorLipSync(LipSyncProvider):
    name = "your_vendor"

    def sync(self, video, audio, dest, *, shot_label="", scene=""):
        # 调你的 API，把结果写到 dest
        ...
        return {"provider": self.name, "stage": "lipsync", "unit": "second",
                "amount": 4.0, "money": 0.08}
```

```powershell
$env:AI_DRAMA_LIPSYNC="your_vendor"
```

两条契约**不是建议**，骨架会在 stage06 里逐条验：

| 契约 | 破了会怎样 | 怎么破的 |
|---|---|---|
| 输出**不带音轨** | 成片里每句台词说两遍（一遍原声、一遍对齐后的） | 忘了 `-an`，或厂商返回里自带音频 |
| 输出**时长 == 输入** | 后面**所有**镜头的声音整体错位，而且不报错 | 模型按音频长度重新裁/拉，或你顺手做了 `-shortest` |

时长这一条骨架是这么验的：先让你写到临时文件，ffprobe 量出来对得上（容差 0.12s）
才原子替换成正式产物。对不上就这一镜算失败、临时文件删掉 ——
你能单独重跑这一镜，但错误进不了成片。

**没有配音的镜头不会被送来**（空镜、纯动作没有可对的嘴），所以实现里
不必处理 `audio is None` 之外的奇怪情况。

没配 provider 不算错误：流水线照跑，但阶段报告里会有一条 `[!]`，
明确写出"N 个有台词的镜头被跳过、成片里嘴型没有对齐"。
要让这件事变成硬门槛，打开 `config/qc_rules.yaml` 的 `require_lipsync`。

### ⑤ `poll` 要能被另一次进程调用

视频是唯一一个"提交出去、等几分钟才回来"的形态，而进程在这几分钟里死掉是
完全正常的事（Ctrl-C、断电、容器被回收）。所以骨架会把提交成功的 job_id
**立刻落库**，`resume` 时先拿它去认领，而不是重新提交 —— 否则每崩一次就
重复付一次钱，而厂商那边其实已经有一份在跑了。

这让 `poll` 多了一层约定：

```python
def poll(self, job_id, *, timeout=300.0):
    r = self._get(f"/v1/video/{job_id}")
    if r.status_code == 404:
        # **查无此人要返回终局失败，不要抛异常**
        return ("failed", {}, "job not found")
    if r.json()["status"] == "failed":
        return ("failed", {}, r.json().get("reason", ""))
    if not r.json().get("output"):
        return ("running", {}, None)
    return ("succeeded", self._usage(r.json()), None)
```

两条：

1. `poll` 必须能被一个**从没提交过这个任务**的进程调用（只要厂商侧还能按 id
   查状态就行）。把状态藏在进程内存里的实现，会让 resume 退化成重复付费。
2. **查无此人 ≠ 还在跑**。任务过期、被清理、id 根本不存在时返回 `failed`
   这种终局状态 —— 抛异常会被上层的容错轮询吞掉并一直重试到超时，于是骨架
   把"任务没了"误判成"还在排队"，这一镜就永远认领不到也渲不出来。

`tests/test_job_ledger.py` 里有三条测试钉住这套行为（含用 `KeyboardInterrupt`
模拟进程被打断），照它们写就行。

### ⑥ 文本 provider 要给出镜角色

分镜阶段的文本 provider 在每个镜头的 JSON 里都要带一个 `characters` 数组，
列出**画面里真的看得见**的角色名（空镜是空数组）：

```json
{"order": 2, "framing": "close", "action": "苏蔓的视线突然定住",
 "dialogue": "苏蔓：（低声）不对。", "characters": ["苏蔓"], "location": "楼道"}
```

骨架按它决定拿谁的定妆照去锁脸。**不写不会报错** —— 那一镜会退化成"在
`action` 里找角色名的子串"，而猜错的表现是成片里出现一张陌生的脸，
只有观众会发现。`providers/dryrun.py` 的 `_scene_shots()` 是填全了这个字段的
参考实现。

两条细节：`characters` 为空数组表示"这一镜是空镜"（谁都不锁），
字段缺失/null 表示"没声明"（骨架退回猜）—— 这两者不一样，别用 null 表示空镜。
名字要和顶层 `characters` 数组里的**逐字一致**；对不上时那一镜拿不到参考图，
报告里会把不认识的名字列出来。

## 请求字段是配置，不是常量

各家把参考素材放在哪个字段完全不一致（`image` / `image_urls` / `ref_images`…），
而**字段名写错时接口通常不报错，只是忽略**。所以它在 `config/models.yaml`
里：

```yaml
ref_fields:
  image: image            # 参考图走哪个字段
  image_alias: image_urls # 再按另一个名字送一份，防"零参考"
  video: video_urls
  audio: audio_urls
```

## 自检

```powershell
python run.py providers     # 你的 provider 出现了吗
python run.py models        # 能力上限补对了吗
python run.py doctor        # 环境齐全吗
```

接上之后先跑一小段看看方向对不对，别一上来就跑全剧：

```powershell
python run.py run --idea "..." --scenes 1 --shots 2
```
