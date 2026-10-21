---
name: ai-drama-prompts
description: 把一句话变成一套能直接粘进可灵/即梦/Seedance/Sora 的短剧提示词（定妆照、首帧、尾帧、运镜、负向词）。当用户想写分镜提示词、要首尾帧提示词、或者想把一个想法变成可拍的分镜时用。
version: 1.0.0
---

# ai-drama：一句话 → 一套提示词

这个仓库只做一件事：**把一句话拆成一场戏，再把每一镜写成能直接粘进视频模型的提示词**。
不替你出图出片 —— 你拿提示词去可灵、即梦、Seedance 网页版出，用你自己的会员额度。

## 什么时候用

- 用户说"帮我写个短剧/短视频的分镜""要能直接用的提示词""首帧尾帧怎么写"
- 用户只有一个想法，需要拆场、拆镜、定角色长相、写运镜
- 用户想把同一集导出成文本，或者要某个平台（可灵/即梦/Seedance）的写法

不适合：用户要的是**成片**（那要走视频模型 API，是另一件事，见 README 的"可选出片"）。

## 怎么调

先跑一次（只花文本模型的钱，按 token 计，一集通常不到一分钱）：

```bash
git clone https://github.com/ramaji114514/ai-drama && cd ai-drama
echo 'AI_DRAMA_TEXT=deepseek'        >> .env
echo 'AI_DRAMA_DEEPSEEK_API_KEY=sk-你的key' >> .env

python run.py prompts --idea "一句话" --scenes 2 --shots 2 --json out.json
```

不给 `--json` 就把 JSON 打到 stdout，可以直接 `| jq`：

```bash
python run.py prompts --idea "深夜便利店，店员发现每个客人都买同一件东西" | jq '.scenes[0].shots[0].image_prompt'
```

## 拿到的 JSON 长什么样

```jsonc
{
  "version": 1, "run_id": "run_...", "title": "...", "style": "...", "aspect": "9:16",
  "characters": [                       // 先做定妆照，每一镜的首帧都引用同一张脸
    { "char_id": "char_...", "name": "林溪", "desc": "女，28 岁",
      "anchor_prompt": "28-year-old Chinese woman, ...", "ref_image": "" }
  ],
  "scenes": [{
    "scene_id": "sc01", "title": "...", "location": "...", "time_of_day": "深夜",
    "shots": [{
      "label": "sc01/shot_000", "duration": 7.5, "framing": "close",
      "dialogue": "林溪：（低声）灰灰？", "action": "...", "end_action": "...",
      "image_prompt": "...",            // 首帧 / 关键帧
      "last_frame": "...",              // 这一镜结束那一刻（尾帧）
      "video_prompt": "...",            // 运镜 + 动作 + 时长
      "negative_prompt": "...",         // 图的负向词
      "video_negative": "..."           // 视频的负向词
    }]
  }]
}
```

**四段提示词是并列的产物**，不是上下游：首帧粘进"首帧图"、尾帧粘进"尾帧图"、
运镜粘进视频框、两条负向词粘进负向框。哪一段为空就是那个平台不看这个字段。

## 粘的时候注意三件事

1. **首帧和视频框内容不一样**。首帧是静帧描述（构图、光线、服装）；视频框写
   "怎么动、动多久"。把首帧那段原样粘进视频框，模型会重新理解一遍画面。
2. **尾帧写到"下一个动作之前"**。写"他推开门"会让下一镜无处可接；写"他站在
   门口，手还扶在门把上"才能接上。
3. **平台方言不一样**。有的平台没有负向框、有的只吃 800 字、有的把【参考锁定】
   这种小标题当真事去画。网页上有一个"粘到哪个平台"的下拉，选完它会替你删掉
   多余的小标题、截断超长文本，并告诉你"负向词已省略，别塞进正向"。

## 别做的事

- 不要把这些提示词再喂给一个 LLM 让它"优化" —— 里面的【参考锁定】【无参考图】
  是给图模型的指令，再改一遍会把约束改没。
- 不要一次生成几十镜：一集 20 镜以上，模型容易把人物写串。分集。
