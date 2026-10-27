/* i18n.js —— 界面英文版。
 *
 * 为什么是"渲染之后再翻"，而不是给每个字符串包一层 t()：
 *   1. 页面上的文字一半是 JS 拼出来的 HTML 片段（`<span>第 3 镜</span>`），
 *      一半是服务端给的句子（方言提示、体检结论）—— 服务端那边没有语言概念；
 *   2. 真正的内容（提示词、台词、剧本 JSON）**也是中文**，它们躺在
 *      <textarea> / <pre> / <code> 里，那些地方一个字都不能翻；
 *   3. 这个页面没有构建步骤，多一层"渲染后遍历文本节点"比改 200 个调用点小得多。
 * 代价：字典是按"整句"命中的，动态拼出来的句子要靠 rules 兜底 —— 漏了就还是中文，
 * 不会翻错。加新文案时记得同时加一条字典。
 */

const I18N = (() => {
  "use strict";

  const STORE_KEY = "ai-drama-lang";
  // 这些容器里是内容，不是界面
  const SKIP = new Set(["PRE", "TEXTAREA", "CODE", "SCRIPT", "STYLE", "KBD", "SAMP"]);

  /* 整句字典：左边是界面上的中文原文，右边是英文。
   * 键必须和文本节点里的内容**一字不差**（含标点、全角括号）。 */
  const EN = {
    // 顶栏 / 侧栏
    "本机查看器 · 可改提示词": "local viewer · prompts editable",
    "这部剧": "This drama",
    "历史 run": "Past runs",
    "还没有 run。": "No runs yet.",
    "还没有项目。": "No projects yet.",
    "左边挑一条 run": "Pick a run on the left",
    "个角色": "characters",
    "第": "Ep.",
    "集 ·": "·",
    "虚拟": "simulated",
    "还没有": "empty",
    "没有项目？": "No project?",
    "一次 run 一集就是单集模式（角色每集重新发明）": "One run = one episode (characters are reinvented per episode)",
    "· 数据库": "· database",
    "连不上": "can't connect",
    "读不出来": "can't read",
    "页面上一片空白": "the page came up blank",

    // 页签
    "分镜": "Storyboard",
    "时间轴": "Timeline",
    "质检": "QC",
    "成本": "Cost",
    "资产": "Assets",
    "成片": "Film",

    // 分镜页
    "导出全部提示词": "Export all prompts",
    "导出分镜表 CSV": "Export shots CSV",
    "粘贴到": "Paste into",
    "通用（不挑平台）": "Generic (any platform)",
    "整集的定妆照 + 每镜首帧/尾帧/运镜，一键存成 .txt":
      "Portraits + per-shot keyframe / last frame / camera moves, saved as one .txt",
    "卡司（这一集用到的脸）": "Cast (faces used in this episode)",
    "定妆照": "Portrait",
    "定妆照（紧凑版）": "Portrait (compact)",
    "定妆照不在": "portrait missing",
    "没有定妆照": "no portrait",
    "（这一集没有定妆照提示词）": "(this episode has no portrait prompt)",
    "拿去做「角色参考图」": "for a character reference image",
    "拿去做「首帧/参考图」": "for a first frame / reference image",
    "拿去做「尾帧/结束帧」——空的话说明这一镜模型没写结束画面":
      "for a last frame / end frame — empty means the model wrote no end action",
    "拿去做「画面描述/运镜」": "for the motion / camera description",
    "首帧": "First frame",
    "尾帧": "Last frame",
    "运镜": "Camera",
    "负向·图": "Negative (image)",
    "负向·视频": "Negative (video)",
    "负向·图（不要什么）": "Negative (image) — what to avoid",
    "负向·视频（别动坏）": "Negative (video) — don't break the motion",
    "粘到「负向提示词」框": "negative prompt box",
    "视频专用的那一组：变脸、闪烁、跳帧": "video-only list: face swap, flicker, dropped frames",
    "复制": "Copy",
    "保存": "Save",
    "保存失败": "Save failed",
    "已复制": "Copied",
    "已存": "Saved",
    "已存进 episode.json 和库。画面要变，点这一镜上的「重跑这一镜」。":
      "Saved to episode.json and the database. To change the picture, hit “Re-run this shot”.",
    "没存上": "Not saved",
    "没存上：": "Not saved: ",
    "没跑起来：": "Didn't start: ",
    "请求失败（": "Request failed (",
    "存…": "Saving…",
    "重跑这一镜": "Re-run this shot",
    "只重跑这一镜：用现在的提示词重出关键帧": "Re-run just this shot: regenerate the keyframe from the current prompt",
    "已重拼，体检": "Rebuilt, lint",
    "已保存并重拼提示词": "Saved and rebuild the prompts",
    "可一键复制": "copy per field",
    "可以直接改": "editable",
    "提示词（": "Prompts (",
    "出镜": "On screen",
    "出镜：声明为空镜": "On screen: declared empty",
    "结束画面": "Ends on",
    "结束画面：": "Ends on: ",
    "台词": "Dialogue",
    "台词：": "Dialogue: ",
    "景别": "Framing",
    "动作": "Action",
    "这一镜演什么": "What happens in this shot",
    "改了会换镜头语言": "changing this changes the shot language",
    "尾帧的原料，空着会退回首帧动作": "raw material for the last frame; empty falls back to the action",
    "「名字：内容」；台词长短决定时长": "“name: line”; line length sets the duration",
    "改分镜原文（保存后自动重拼提示词）": "Edit the storyboard (prompts are rebuilt on save)",
    "保存并重拼": "Save & rebuild",
    "体检": "Lint",
    "体检通过": "Lint passed",
    "看看是哪几条": "see what they are",
    "体检结果": "Lint notes",
    "关键帧文件没了": "keyframe file is gone",
    "文件没了": "file is gone",
    "记录还在，文件没了": "record remains, file is gone",
    "记录在，文件没了": "record present, file gone",
    "文件没了（": "file gone (",
    "待跑": "pending",
    "完成": "done",
    "进行中": "running",
    "失败": "failed",
    "跳过": "skipped",
    "通过": "pass",
    "不通过": "fail",
    "没查成": "not checked",
    "未知": "unknown",
    "目标": "Goal",
    "目标/实际": "goal / actual",
    "总时长": "Total",
    "总长": "Total",
    "多": "over",
    "少": "under",
    "（同上，这里不再展开）": "(same as above)",
    "一次": "one",

    // 成本页
    "实际花费": "Actually spent",
    "调用次数": "API calls",
    "按阶段": "By stage",
    "按 provider": "By provider",
    "明细": "Details",
    "阶段": "stage",
    "钱": "money",
    "占比": "share",
    "单位": "unit",
    "用量": "usage",
    "镜头": "shot",
    "没有成本记录。": "No cost records.",
    "只显示前 300 条。": "Showing the first 300 only.",

    // 时间轴
    "字幕文件": "Subtitle file",
    "字幕（SRT）": "Subtitles (SRT)",
    "音效": "SFX",
    "时间": "time",
    "时长": "duration",
    "时长秒": "seconds",
    "分镜表": "Shots",
    "还没有 subtitles.srt。": "No subtitles.srt yet.",
    "（拼接阶段跑完才有）。": "(available once concat has run)",
    "拼接阶段还没跑完": "concat hasn't finished yet",

    // 质检
    "阻断": "blocked",
    "警告": "warnings",
    "发现": "findings",
    "检查的镜头": "shots checked",
    "检查的资产": "assets checked",
    "还没有质检报告。": "No QC report yet.",
    "没有任何发现。": "Nothing found.",
    "规则来自": "rules from",
    "可补跑。": "to re-run it.",
    "字幕自己又算了一遍时间": "subtitles recomputed their own timing",

    // 资产
    "资产": "Asset",
    "体积": "Size",
    "血缘边": "Lineage edges",
    "文件": "File",
    "大小": "Size",
    "种类": "Kind",
    "产物": "artifact",
    "其他产物": "other artifacts",
    "上游（它是由什么变来的）": "Upstream (what it was made from)",
    "下游（用到了它的东西）": "Downstream (what uses it)",
    "血缘链": "Lineage chain",
    "这一条 run 没有资产台账。": "This run has no asset ledger.",
    "台账是后来才加的 —— 重跑一次就有。": "The ledger came later — re-run once to get one.",

    // 项目页
    "角色圣经": "Character bible",
    "这部戏还没有在册角色。": "No characters on file for this drama.",
    "角色": "Characters",
    "更新": "updated",
    "创建": "created",
    "风格": "Style",
    "为": "for",

    // 成片
    "下载成片": "Download film",
    "还没有成片": "No film yet",
    "这一条 run 还没有分镜（": "This run has no storyboard yet (",

    // 杂项
    "无": "none",
    "无 id": "no id",
    "没有": "none",
    "条": "items",
    "件": "items",
    "段": "fields",
    "镜": "shots",
    "（可以手改，改完": "(you can edit it by hand; after saving",
    "这份文件可以直接手改（改台词、改时长），": "This file can be edited by hand (dialogue, duration),",
    "然后": "then",
    "，可直接手改（改完下一集生效）。定妆照复用的是同一个文件 ——":
      ". Edit it by hand and it applies from the next episode. Portraits reuse the same file —",
    "跨集一致性的硬保证在这里，不在提示词里。":
      "that's where cross-episode consistency is enforced, not in the prompt.",
    "台账记的是\"这东西是谁生成的、从什么变来的\"。":
      "The ledger records who produced each artifact and what it came from.",
    "点一行看它的上游和血缘。": "Click a row to see its upstream and lineage.",
    "字幕、音画对齐、音效共用**同一份**累加时间轴 —— 字幕飘掉":
      "Subtitles, A/V sync and SFX all share **one** accumulated timeline — drifting subtitles",
    "几乎永远是\"字幕自己又算了一遍时间\"。":
      "almost always mean the subtitles recomputed the timing themselves.",
    "本地工具不用防": "local tool, nothing to defend against",
    "按阶段/按 provider": "by stage / by provider",
    "什么都没显示": "nothing rendered",
    "看终端里的报错": "check the error in your terminal",
    "给谁写的": "written for",
    "被改写过的那几家": "providers that rewrote it",
    "要粘进去的那几段": "the fields you paste",
    "结论": "verdict",
    "起点": "origin",
    "场次": "Scene",
    "镜号": "shot",
    "段数": "fields",
    "集": "",
    "画幅": "aspect",
    "可灵": "Kling",
    "即梦": "Jimeng",
    "Seedance（豆包）": "Seedance (Doubao)",
    "海螺（MiniMax）": "Hailuo (MiniMax)",
    "Runway（Gen-4）": "Runway (Gen-4)",
    "Veo（Google）": "Veo (Google)",
    "留着小标题，给看得懂分层的人或 API 用；粘到网页端前先看目标平台那一栏。":
      "Keeps the section headings for people or APIs that read them; check the platform column before pasting.",
    "首帧图、尾帧图各自上传；负向词有独立框（也是 2500 字）。":
      "Upload the first and last frame separately; the negative box holds 2500 characters too.",
    "没有负向框：负向词已省略，别塞进正向。首尾帧各传一张图；":
      "No negative box: the negative words are dropped, don't stuff them into the positive prompt. Upload one image per frame.",
    "没有负向参数：负向词已省略。官方四行模板：「画面呈现…」「镜头采用…」":
      "No negative parameter: negative words dropped. The official template is four lines: subject, action, scene, style.",
    "没有负向框：负向词已省略。运镜用方括号指令词（[Push in] 这类，":
      "No negative box: negative words dropped. Camera moves use bracketed commands like [Push in],",
    "没有负向框（它明确不建议否定措辞，可能反向理解）：负向词已省略。":
      "No negative box (it explicitly warns against negations, which may be read backwards): negative words dropped.",
    "有独立负向框，但只放得下 500 字。首帧+尾帧都支持；":
      "Has a negative box, but only 500 characters. First and last frame both supported;",
    "只认首帧、没有负向框：尾帧和负向词都省了。它吃长句，":
      "First frame only, no negative box: last frame and negative words are dropped. It handles long sentences,",
    // 体检结论（服务端给的固定句子）
    "这一镜没有 end_action，尾帧只能复述首帧。":
      "No end_action for this shot, so the last frame can only repeat the first.",
    "视频提示词里有多个运镜，模型多半会二选一。":
      "The video prompt asks for several camera moves; the model will pick one.",
    "有台词但视频提示词没让人开口，配音会像画外音。":
      "There is dialogue but the video prompt never has anyone speak, so the dub sounds like voice-over.",
    "没有负向词，手和脸是图像模型最先崩的地方。":
      "No negative words; hands and faces are where image models break first.",
    "搜标题、run id 或项目": "Search titles, run ids or projects",
    "搜索标题、run id 或项目": "Search titles, run ids or projects",
    "还没有 run。": "No runs yet.",
    "还没有项目。": "No projects yet.",
    "资产查过": "Assets checked",
    "版": "v",
    "调用": "calls",
    "错": "error",
  };

  /* 动态句子：拼出来的（带数字/名字）没法整句命中，用规则兜底。
   * 顺序有意义，先匹配先赢。 */
  const RULES = [
    [/^(\d+) 个角色 · (.*)$/, (m) => `${m[1]} character${m[1] === "1" ? "" : "s"} · ${m[2]}`],
    [/^第 (\d+) 集 · (.*)$/, (m) => `Ep. ${m[1]} · ${m[2]}`],
    [/^(\d+) 镜$/, (m) => `${m[1]} shot${m[1] === "1" ? "" : "s"}`],
    [/^(\d+) 段$/, (m) => `${m[1]} field${m[1] === "1" ? "" : "s"}`],
    [/^(\d+) 条$/, (m) => `${m[1]} item${m[1] === "1" ? "" : "s"}`],
    [/^(\d+) 条提醒$/, (m) => `${m[1]} warning${m[1] === "1" ? "" : "s"}`],
    [/^(\d+) 条体检$/, (m) => `${m[1]} lint note${m[1] === "1" ? "" : "s"}`],
    [/^体检 (\d+) 条$/, (m) => `Lint: ${m[1]} note${m[1] === "1" ? "" : "s"}`],
    [/^已重拼，体检 (\d+) 条$/, (m) => `Rebuilt, ${m[1]} lint note${m[1] === "1" ? "" : "s"}`],
    [/^明细 (\d+) 条$/, (m) => `${m[1]} entries`],
    [/^看看是哪几条$/, () => "see what they are"],
    [/^目标 (.*) · 实际 (.*)（(正好|多|少) (.*)）$/, (m) => {
      const word = m[3] === "正好" ? "on target" : m[3] === "多" ? "over" : "under";
      return `Goal ${m[1]} · actual ${m[2]} (${word} ${m[4]})`;
    }],
    [/^目标 (.*) · 实际 (.*)$/, (m) => `Goal ${m[1]} · actual ${m[2]}`],
    [/^总时长 (.*)$/, (m) => `Total ${m[1]}`],
    [/^出镜：(.*)$/, (m) => `On screen: ${m[1]}`],
    [/^出镜 ($|.*)/, (m) => `On screen ${m[1]}`],
    [/^台词：(.*)$/, (m) => `Dialogue: ${m[1]}`],
    [/^结束画面：(.*)$/, (m) => `Ends on: ${m[1]}`],
    [/^提示词（(\d+) 段，(.*)）$/, (m) => {
      const n = m[1];
      const tail = m[2] === "可一键复制" ? "copy per field" : "editable";
      return `Prompts (${n} field${n === "1" ? "" : "s"}, ${tail})`;
    }],
    [/^画面描述 (\d+) 字，超过 (\d+) 字预算；.*$/, (m) =>
      `Scene description is ${m[1]} characters, over the ${m[2]} budget — the model will cherry-pick.`],
    [/^「(.+)」至少要 (\d+) 秒才做得完，这一镜只有 ([\d.]+) 秒。$/, (m) =>
      `“${m[1]}” needs at least ${m[2]}s; this shot is only ${m[3]}s.`],
    [/没有匹配的 run。/, () => "No matching runs."],
    [/没有匹配的项目。/, () => "No matching projects."],
    [/^(\d+) 件$/, (m) => `${m[1]} item${m[1] === "1" ? "" : "s"}`],
    [/^(\d+) 个$/, (m) => `${m[1]}`],
    [/^\((\d+)\/(\d+)\)$/, (m) => `(${m[1]}/${m[2]})`],
    [/^发现 (\d+) 条$/, (m) => `Findings: ${m[1]}`],
    [/^（版本 (.+)），检查于 (.*)$/, (m) => `(v${m[1]}), checked ${m[2]}`],
    [/^(.*) 虚拟$/, (m) => `${m[1]} simulated`],
    [/^拿去做「(.*)」$/, (m) => `for ${m[1]}`],
    [/^请求失败（(\d+)）$/, (m) => `Request failed (${m[1]})`],
    [/^文件没了（(.*)）$/, (m) => `file gone (${m[1]})`],
    [/^这一条 run 还没有分镜（(.*)）$/, (m) => `This run has no storyboard yet (${m[1]})`],
    [/^版本 (.*) · 数据库 (.*)$/, (m) => `Version ${m[1]} · database ${m[2]}`],
    [/^(\d+) 分钟$/, (m) => `${m[1]} min`],
    [/^(\d+) 秒$/, (m) => `${m[1]}s`],
    [/^第 (\d+) 镜$/, (m) => `Shot ${m[1]}`],
    [/^镜 (\d+)$/, (m) => `Shot ${m[1]}`],
  ];

  let lang = "zh";
  let observer = null;
  let pending = false;

  function pickLang() {
    const q = new URLSearchParams(location.search).get("lang");
    if (q === "en" || q === "zh") return q;
    try {
      const saved = localStorage.getItem(STORE_KEY);
      if (saved === "en" || saved === "zh") return saved;
    } catch (e) { /* 隐私模式 */ }
    return (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";
  }

  function lookup(text) {
    const trimmed = text.trim();
    if (!trimmed) return text;
    // HTML 模板里的换行和缩进会进到文本节点里（`（版本\n  1）`），
    // 先压成一个空格再查字典，否则只能命中没换行的那些。
    const raw = trimmed.replace(/\s+/g, " ");
    // 注意：译文可能是空串（"集" 在英文里就没有对应词），所以不能用真假判断
    if (Object.prototype.hasOwnProperty.call(EN, raw)) return text.replace(trimmed, EN[raw]);
    for (const [re, rep] of RULES) {
      const m = raw.match(re);
      if (m) {
        const out = typeof rep === "function" ? rep(m) : raw.replace(re, rep);
        return text.replace(trimmed, out);
      }
    }
    return text;
  }

  function translateText(node) {
    const parent = node.parentElement;
    if (!parent || SKIP.has(parent.tagName)) return;
    if (parent.closest(".no-i18n")) return;
    const next = lookup(node.nodeValue);
    if (next !== node.nodeValue) node.nodeValue = next;
  }

  function translateAttrs(root) {
    const els = root.querySelectorAll ? root.querySelectorAll("[title],[placeholder],[aria-label]") : [];
    els.forEach((el) => {
      ["title", "placeholder", "aria-label"].forEach((a) => {
        const v = el.getAttribute(a);
        if (!v || !/[\u4e00-\u9fff]/.test(v)) return;
        const next = lookup(v);
        if (next !== v) el.setAttribute(a, next);
      });
    });
  }

  function translate(root) {
    if (lang !== "en" || !root) return;
    if (root.nodeType === Node.TEXT_NODE) { translateText(root); return; }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) translateText(n);
    translateAttrs(root.nodeType === Node.ELEMENT_NODE ? root : document.body);
  }

  function scheduleTranslate() {
    if (pending) return;
    pending = true;
    queueMicrotask(() => { pending = false; translate(document.body); });
  }

  function start() {
    lang = pickLang();
    document.documentElement.setAttribute("lang", lang === "en" ? "en" : "zh-CN");
    translate(document.body);
    if (!observer && typeof MutationObserver !== "undefined") {
      observer = new MutationObserver(scheduleTranslate);
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }
    setButton();
  }

  function setButton() {
    const b = document.getElementById("lang");
    if (!b) return;
    // 按钮上写的是"点一下会变成什么"
    b.textContent = lang === "en" ? "中文" : "EN";
    b.title = lang === "en" ? "切回中文" : "Switch to English";
  }

  function set(next) {
    lang = next === "en" ? "en" : "zh";
    try { localStorage.setItem(STORE_KEY, lang); } catch (e) { /* 隐私模式 */ }
    if (lang === "zh") {
      // 翻回去最省事的办法就是重放一遍服务端数据
      location.search = "?lang=zh" + location.hash;
      return;
    }
    document.documentElement.setAttribute("lang", "en");
    translate(document.body);
    setButton();
  }

  function toggle() { set(lang === "en" ? "zh" : "en"); }

  return { start, set, toggle, translate, get lang() { return lang; }, EN, RULES };
})();
