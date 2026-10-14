# 接一个真厂商

骨架承诺的一件事：**换模型不用改流水线**。这条承诺的完整含义是"只改一个
文件、几十行代码"，而不是"有个抽象层所以理论上可以换"。这份文档就是那几十行。

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
