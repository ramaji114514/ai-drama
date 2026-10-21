# ai-drama

[![tests](https://github.com/ramaji114514/ai-drama/actions/workflows/tests.yml/badge.svg)](https://github.com/ramaji114514/ai-drama/actions/workflows/tests.yml)

[中文](README.md) · **English**

**One sentence in, a ready-to-paste prompt set out.**

You say *"working late, I come home and the lock has been changed — the peephole
is covered from the inside"*, and you get:

* **Character sheet prompt** — what this person looks like (feed it to a character-reference image model)
* **First-frame prompt** — what the opening frame of this shot looks like
* **Last-frame prompt** — what the shot looks like at the exact moment it ends (first/last-frame models steer motion with it)
* **Camera prompt** — how the camera moves, and how fast it should finish
* **Negative prompt** — what you *don't* want (broken faces, watermarks, face morphing, flicker…)

Every block is a finished piece you can copy wholesale, paste into Kling / Jimeng /
Sora / Veo / Runway, and generate. Copy them one by one in the page, or export the
whole episode as a single `.txt`.

If you want an actual video, that works too — the same pipeline can drive real
video models from a sentence to an mp4 — but that path is optional. You can take
the prompts and never touch it.

---

## Three-minute start

### 0. What you need

* Python 3.10+ (**no third-party packages** — the standard library is enough)
* FFmpeg (only needed to actually render video; not needed for prompts alone)
* An API key for a model that can write (DeepSeek below; a whole episode costs cents)

### 1. Point it at a model

There is a `.env.example` in the repo. Copy it to `.env` and fill in two lines:

```bash
cp .env.example .env
```

```bash
AI_DRAMA_TEXT=deepseek
AI_DRAMA_DEEPSEEK_API_KEY=sk-your-key
```

You can also run without a key: the default `dryrun` provider is local fake data.
It shows you the shape of the pipeline, but it cannot write a decent sentence —
for real output you need a real model.

> **The key is yours, and it stays on your machine.**
> There is no key in this repository; `sk-your-key` above is a placeholder, and a
> filled-in `.env` is ignored by `.gitignore`. The key the author demos with is a
> local one and is not shared, so don't expect a clone to work out of the box —
> spending a few cents on your own key is more reliable than waiting for someone
> else's. A test guards this: `tests/test_no_secrets.py` scans every git-tracked
> file and fails on anything that looks like a real key.

### 2. One sentence → one prompt set

```bash
# Create a series (characters, style and portraits belong to it — reuse across episodes)
python run.py project new "雨夜旧书店" --style "冷青主色调，高对比胶片质感，窄巷霓虹"

# Run: script + storyboard + portraits + keyframes (no video, prompts only)
python run.py run "深夜加班回家，发现自家门锁被换过" \
  --project proj_xxxxxxxx --scenes 2 --shots 1 --stages script,storyboard,frames
```

It prints where the artifacts went; `episode.json` holds all dialogue and storyboard.

### 3. Open the page, copy prompts

```bash
python run.py web
```

Open <http://127.0.0.1:8765> and click any run on the left, then the storyboard tab:

![Storyboard: prompts for every shot, copy one at a time](docs/images/web-prompts.jpg)

Each shot has **5 prompt blocks**, each with a **Copy** button; at the top,
**Export all prompts** packs the character sheets + every shot's first frame /
last frame / camera / negative into one `.txt`.

The prompt boxes are **editable**: change one, hit **Save**, and it is written back
to `episode.json` and the database. Then **Re-run this shot** re-generates only
that shot's keyframe with the new prompt (just this shot, not the episode).
Text rendered for other platforms is truncated and read-only — switch back to
"generic" to edit, then switch again.

The page binds to localhost only and never creates runs or spends money: the only
write paths are "edit a prompt" and "re-run this shot".

> Prefer no browser, and want a **script or agent** to take the prompts directly?
> `python run.py prompts --idea "one sentence" --json out.json` gives you the same
> thing in one step. To make your agent reach for this tool on its own, install
> [`agent/SKILL.md`](agent/SKILL.md) — details under
> [“For scripts and agents”](#for-scripts-and-agents) below.

---

## What the prompts look like

Episode 1, shot 1 of 《雨夜旧书店》, real output (excerpt):

**First frame**

> 【参考锁定】画面中的人物、服装、场景、道具严格以参考图为准，不得自行更换长相、发型或服装
> 冷青主色调，高对比胶片质感；雨痕在玻璃窗上蜿蜒如泪；书页泛黄卷边，灯光昏黄摇曳
> 角色 林屿：17 岁，男，瘦削安静，黑色短发贴着额头，灰色连帽卫衣

**Last frame** (different from the first: this is the moment the shot *ends*)

> 铜铃停在半空，门缝里伸进来的伞尖滴着水，林屿的手已经按在柜台边缘

**Camera**

> 镜头缓慢推近，越过门框停在林屿的侧脸上，15 秒内匀速完成，不加速也不放慢

**Negative**

> 多余的手指, 畸形的手, 五官错位, 文字, 水印, 字幕, 过曝, 塑料皮肤……
> 变脸, 闪烁, 跳帧, 动作僵住, 鬼影

Four things that usually go wrong are handled literally here:

| Usual failure | How it is handled |
| --- | --- |
| First frame and video use the same sentence — says nothing | First frame covers the *image*, camera covers the *change*; written separately |
| A last frame can't be derived from an action | The script model writes `end_action` separately, or the prompt would be blank |
| “No watermark” inside the positive prompt produces a watermark | Always goes into the separate negative field |
| A 15-second shot that the model finishes in two | The camera prompt says “finish evenly within 15 seconds” |

Prompts are written in Chinese because the primary target platforms are Chinese.
The structure (first frame / last frame / camera / negative) is platform-neutral.

---

## Want the whole thing N seconds long? Ask for a number

```bash
python run.py run "one sentence" --scenes 2 --shots 2 --target 40
```

Target ÷ number of shots = seconds per shot (4 shots over 40 seconds = 10s each).
Give `--target` without `--scenes/--shots` and it splits across the default 3×4.

One thing it **will not** do: cut dialogue short to hit the number. A shot whose
lines take longer keeps the lines, and the report says how far off you landed:

```
定稿 4 镜 / 总时长 52.4s / 定妆照 2 张（目标 40s，多 12.4s）
```

Conversely, if there are so many shots that each would get under 2 seconds, it
tells you the target is impossible instead of quietly handing you a 48-second
edit — 24 shots cannot perform a 30-second scene.

---

## Every platform wants a different shape

The same shot does not use the same input boxes on Kling, Jimeng, Seedance, Hailuo
or Runway: some have a separate negative field, some don't; some accept a last-frame
image, some only a first frame; some will treat `【参考锁定】`-style headings as
picture content and try to draw them. Pick your target in the top right and the
prompts below are rewritten for that platform:

![Switching dialects per platform](docs/images/web-dialects.jpg)

- **Generic**: all five blocks kept, for people or APIs that understand the layering.
- **Kling**: headings stripped, first/last frame images uploaded separately, negatives in its own box.
- **Jimeng**, **Seedance**, **Hailuo**: no negative box, so the negative block is
  **dropped entirely** and the page says why — don't move it into the positive
  field, every noun there is something the model tries to draw. Over-long text is
  cut at a sentence boundary. Hailuo additionally has two image slots, so both
  frames are usable.
- **Runway**, **Veo**, **Sora**: first frame only, so the last-frame image is
  dropped (the last-frame text is merged into the video prompt as “perform up to
  here”); no negative box either. These three are English platforms and **the
  prompts themselves are still Chinese** — the page says so. The skeleton does not
  translate for you, because that would mean another model call and another bill.

The exported `.txt` follows the same choice: pick a platform and the file contains
exactly the blocks that platform wants.

---

## Export the storyboard as CSV, open it in Excel

```bash
python run.py shots run_xxxxxxxx --format csv --out storyboard.csv
```

One row per shot, columns in the order a producer reads a table: scene, shot number,
framing, duration, action, end action, dialogue, cast — the four prompt blocks and
two negative blocks come after. Commas, quotes and newlines inside dialogue are
escaped per CSV rules, so nothing shifts columns; the file starts with a BOM so
Windows doesn't turn it into mojibake.

The storyboard page has an **Export storyboard CSV** button too — same content,
downloaded by the browser.

---

## How it runs

![Eight stages](docs/images/pipeline.png)

One sentence goes in, eight stages run in order, and **every stage writes its
output to disk** — you can stop, look, edit, and re-run only some of them:

| Stage | What it does |
| --- | --- |
| `script` | sentence → scenes, shots, dialogue, characters |
| `storyboard` | composes the four prompts + negatives, fixes shot lengths, makes portraits |
| `frames` | keyframes (first-frame image) |
| `video` | video clips (optional) |
| `audio` | voice-over (optional) |
| `lipsync` | lip sync (optional) |
| `concat` | concat, burn subtitles, final cut (optional) |
| `qc` | self-check: duration, black frames, A/V alignment (optional) |

For prompts only, stop at `storyboard` — everything after it is optional.

---

## Command reference

```bash
python run.py doctor                  # environment check: which model each step uses, keys set?
python run.py providers               # registered providers and their state
python run.py web                     # viewer: storyboard / timeline / QC / cost / assets / film
```

| Command | What it does |
| --- | --- |
| `project new <name>` | create a series (character bible + style, reused across episodes) |
| `run "<one sentence>"` | run the whole pipeline |
| `prompts --idea "<one sentence>"` | prompts only, as JSON for scripts/agents |
| `resume <run_id>` | resume, running only what is missing |
| `status <run_id>` | per-shot progress |
| `shots <run_id>` | export the storyboard (`--format md` / `json` / `csv`) |
| `cost <run_id>` | cost breakdown (by stage / by provider) |
| `assets <run_id>` | asset ledger; `--check` reconciles it |
| `asset <asset_id>` | the lineage of one asset |
| `qc <run_id>` | QC report (0 pass / 2 fail / 3 not checked) |
| `models` | model catalog: capability caps and reference-material fields |
| `skills` | skill catalog: which notes each step actually loads |
| `faces` | when several portraits exist, whose face is authoritative |

Common flags:

```bash
python run.py run "one sentence" \
  --scenes 2 --shots 1 \
  --aspect 9:16 \
  --stages script,storyboard \
  --budget 1.5
```

* `--scenes` / `--shots`: how many scenes, how many shots per scene
* `--target`: desired total length in seconds, split evenly across shots, e.g. `--target 60`.
  When dialogue is longer than a shot's share, **dialogue wins** and the report states the gap
* `--aspect`: `9:16` vertical (default) / `16:9` / `1:1` / `4:5`
* `--stages`: run only these steps; for prompts only, `script,storyboard,frames`
* `--budget`: spend ceiling in USD, stops on the spot when exceeded
* `--force`: ignore caches and redo everything

---

## For scripts and agents

The web page is for humans. To get prompts into **another program**, there are three
routes, easiest first:

### 1. JSON straight from the command line

```bash
python run.py prompts --idea "深夜便利店，店员发现每个客人都买同一件东西" \
  --scenes 2 --shots 2 --json out.json

# without --json it goes to stdout, progress to stderr, so pipes stay clean
python run.py prompts --idea "one sentence" | jq '.scenes[0].shots[0].image_prompt'
```

It runs only “script + storyboard” and **never touches an image model**, so the cost
of an episode comes from the text model alone — usually under a cent. Fields:

| Field | What it is | Where to paste |
| --- | --- | --- |
| `characters[].anchor_prompt` | character sheet | “character reference” / image-to-image |
| `scenes[].shots[].image_prompt` | first frame | “first frame” image slot |
| `scenes[].shots[].last_frame` | last frame | “last frame” image slot |
| `scenes[].shots[].video_prompt` | camera + action + duration | video prompt box |
| `negative_prompt` / `video_negative` | the two negative blocks | negative prompt box (skip if absent) |

Add `--dialect` to pre-arrange everything for one platform's boxes:

```bash
python run.py prompts --idea "one sentence" --dialect jimeng --json out.json
# options: generic (default) / kling / jimeng / seedance / hailuo / runway / veo / sora
```

Each shot then also carries `fields` (the boxes this platform **actually has**,
truncated to its character limit) and `dropped` (fields it lacks, with the reason
spelled out). The original five blocks stay as they were — truncation is lossy, and
you need the original to edit.

### 2. Let an agent call it (skill entry point)

The repo ships an agent note: [`agent/SKILL.md`](agent/SKILL.md). It explains when
to use this tool, how to call it, what the JSON looks like, and the three most
common mistakes when pasting prompts. Drop it into your agent's skill directory and
the agent decides for itself when to reach for it:

```bash
# Claude Code / most local agents
mkdir -p ~/.claude/skills/ai-drama-prompts
cp agent/SKILL.md ~/.claude/skills/ai-drama-prompts/

# tools using the .agents/skills convention (DSH, some CLI agents)
mkdir -p ~/.agents/skills/ai-drama-prompts
cp agent/SKILL.md ~/.agents/skills/ai-drama-prompts/
```

For a single project, put it under that project's `.claude/skills/` or
`.agents/skills/`. Agents with no skill directory (plain chat) work the same way if
you paste the contents of `agent/SKILL.md` into their system prompt or project notes.

Once installed you can just say:

> Write me a 30-second mystery short-drama storyboard whose prompts I can paste into Jimeng.

The agent runs `python run.py prompts` and hands you first frame / last frame /
camera separately, instead of one wall of text.

### 3. Copy from the page

The dumbest and most direct route: `python run.py web`, open the storyboard tab, and
every prompt block has a **Copy** button; **Export all prompts** saves the whole
episode as `.txt`. Good for manually picking a few shots.

---

## Change one line without re-running the episode

Editing dialogue, prompts or a shot's duration all follow one path:

```bash
# 1. open runs/<run_id>/episode.json and change the line you care about
# 2. read it back into the database and re-run only the affected stages
python run.py resume <run_id> --from-json --stages storyboard,frames
```

Keyframes and clips already produced are not redone (unless you pass `--force`), so
money already spent is not spent twice.

---

## You can also render for real (optional)

The default `dryrun` is **local fake rendering**: the whole pipeline runs and you can
see the shape of the output, but frames are placeholder cards and audio is synthetic.
Its purpose is to let you tune the pipeline and the prompts before spending anything.

To render for real, switch the relevant kinds to a real vendor:

```bash
AI_DRAMA_IMAGE=dashscope
AI_DRAMA_VIDEO=dashscope
AI_DRAMA_AUDIO=dashscope
AI_DRAMA_DASHSCOPE_API_KEY=sk-your-key
```

A few annoying problems are already handled:

* **A 15-second shot** when the model only produces 5 seconds at a time is split
  automatically, and each part starts **from the last frame of the previous part** —
  not from the same image again.
* **Subtitles** are burned in and also written as a separate `.srt`.
* **Lip sync** is its own step; the output has no original audio and matches the
  input duration exactly.
* **QC has three states**: pass / fail / not checked. **“Not checked” is not a pass** —
  shipping a batch with unknown problems because a tool was missing is the worse outcome.

---

## Billing: real money and “made-up money” in two columns

![Cost page: real spend kept apart from placeholder estimates](docs/images/web-cost.jpg)

`dryrun`'s price table is invented (“pretend this image costs three cents”) and
nothing leaves your account. So the cost page says them separately: **actual spend**
is what was really charged, **placeholder estimate** sits on its own line tagged
`虚拟`. Merged into one number, someone who only takes prompts would think they were billed.

The budget gate (`--budget`) still counts everything and stops immediately — but the
report keeps them apart.

---

## Plug in your own models

Eight kinds, each owning one part, no vendor name anywhere in the stages:

| Kind | Env var | Available today |
| --- | --- | --- |
| Script | `AI_DRAMA_TEXT` | `deepseek`, `dashscope`, `dryrun` |
| Image | `AI_DRAMA_IMAGE` | `dashscope`, `dryrun` |
| Portrait | `AI_DRAMA_T2I` | `dashscope`, `dryrun` |
| Video | `AI_DRAMA_VIDEO` | `dashscope`, `aihubmix`, `dryrun` |
| Voice | `AI_DRAMA_AUDIO` | `dashscope`, `dryrun` |
| Lip sync | `AI_DRAMA_LIPSYNC` | `dashscope`, `dryrun` |
| Vision | `AI_DRAMA_VISION` | `dashscope`, `dryrun` |
| Sound FX | `AI_DRAMA_SFX` | `dryrun` only |

One env line swaps a vendor; swapping never touches stage code.
`python run.py doctor` tells you who each step will actually use, and whether its key is set.

How much code a new vendor takes: see [docs/adding-a-provider.md](docs/adding-a-provider.md).
Architecture and data flow: see [docs/architecture.md](docs/architecture.md).

---

## Multiple episodes: characters must stay consistent

```bash
# episode 1
python run.py run "雨夜，外卖员把最后一单送到一栋没有门牌的老楼" --project proj_xxxxxxxx

# episode 2: reuses episode 1's characters and portraits, the face doesn't change
python run.py run "第二天，同一个外卖员又来了" --project proj_xxxxxxxx
```

The character bible (`projects/<project_id>/project.json`) stores each character's
description, portrait prompt and voice. Every episode of the same series reads that
one copy — which is the only way an audience still recognizes the same person in
episode three.

---

## Layout

```
ai_drama/
├── core/        state machine, cost accounting, path conventions, subtitles, voices, media helpers
├── providers/   adapters for model vendors (dryrun / dashscope / deepseek / aihubmix)
├── stages/      the eight stages, one file each
├── web/         viewer + prompt editing / per-shot re-run (stdlib http.server + plain JS, zero build)
└── skills/      “knowledge that changes”, like how to phrase prompts — editable on its own
```

```
config/     model catalog, QC rules
docs/       architecture / adding a provider / making a series
runs/       artifacts of every run (not in git)
projects/   character bibles (not in git)
agent/      the agent-facing instructions (SKILL.md)
```

---

## Tests

```bash
python -m pytest
```

Everything runs offline, no network, no money. `--strict-markers` is on; cases marked
`ffmpeg` need FFmpeg installed locally.

Prompts-only subset (no FFmpeg needed):

```bash
python -m pytest -m "not ffmpeg"
python tools/doc_audit.py     # do the commands/paths mentioned in docs actually exist
```

CI runs two suites (see `.github/workflows/tests.yml`): the **offline suite without
ffmpeg** (py3.10 and py3.12 — someone who only takes prompts should never be blocked
by ffmpeg), and the **full suite with ffmpeg** (real concat, real subtitle burn-in).

---

## License

MIT, see [LICENSE](LICENSE).
