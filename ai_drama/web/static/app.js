'use strict';
/* ai-drama 只读查看器：原生 JS，没有构建步骤。
 *
 * 一条贯穿全文件的要求：**每个插进 HTML 的数据都要过 esc()**。这里的数据
 * 混着 LLM 的输出、角色名、文件名 —— "本地工具不用防"是个到第一次
 * 把别人的剧本导进来才会发现的假设。
 *
 * 另一条：**文件不在就说不在**，绝不用一张裂图代替。盘上有记录、文件没了，
 * 是这一整层最该报出来的事（`run.py assets --check` 也是为此存在）。
 */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const $ = (sel) => document.querySelector(sel);

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

const fmtMoney = (n) => '$' + (Number(n) || 0).toFixed(4);

/* 一条 run 的钱怎么念。dryrun 的价是编的，混在一起念就等于说这次真花了钱。 */
function moneyLabel(r) {
  const real = Number(r.real_money) || 0;
  const sim = Number(r.simulated_money) || 0;
  if (sim > 0 && real > 0) return `${fmtMoney(real)} + ${fmtMoney(sim)} 虚拟`;
  if (sim > 0 && real === 0) return `${fmtMoney(sim)} 虚拟`;
  return fmtMoney(real || r.money);
}
const fmtSec = (n) => (Number(n) || 0).toFixed(2) + 's';

function badge(state) {
  const s = String(state || '').toLowerCase();
  const label = {
    done: '完成', failed: '失败', running: '进行中', pending: '待跑',
    skipped: '跳过', pass: '通过', fail: '不通过', unchecked: '没查成',
  }[s] || state || '未知';
  return `<span class="badge ${esc(s)}">${esc(label)}</span>`;
}

/* 文件引用 → 一个能点/能看的东西。ref 为 null 表示"还没有这个东西"，
   exists 为 false 表示"记录在，文件没了" —— 两者分开显示。 */
function fileLink(ref) {
  if (!ref) return '<span class="dim">没有</span>';
  if (!ref.exists) return `<span class="miss">文件没了（${esc(ref.name)}）</span>`;
  return `<a href="${esc(ref.url)}" download>${esc(ref.name)} · ${fmtBytes(ref.size)}</a>`;
}

const mediaURL = (ref) => (ref && ref.exists ? esc(ref.url) : '');

const state = { runs: [], projects: [], data: null, tab: 'storyboard', asset: null,
                dialect: 'generic', dialects: {},
  query: '', framings: [] };

/* ————————————————————————————————— 路由 ————————————————————————————————— */
/* `#/run/<id>[/<tab>]` 或 `#/project/<id>`。
 *
 * 地址里带着状态有两个好处，都是实打实的：一条链接能指到某一条 run（"你看
 * 第 2 集那个镜头"可以直接发出去），以及浏览器后退键能用。
 *
 * 还有第三个：**打开页面默认就进最新一条 run**。以前光是"左边挑一条 run"，
 * 一屏黑底灰字，第一次打开的人只会觉得"什么都没显示"。
 */
function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function route() {
  const m = /^#\/(run|project)\/([^/]+)(?:\/(\w+))?/.exec(location.hash || '');
  if (!m) return false;
  const [, kind, id, tab] = m;
  if (kind === 'project') { openProject(id); return true; }
  state.tab = tab || 'storyboard';
  openRun(id);
  return true;
}

window.addEventListener('hashchange', route);

/* ————————————————————————————————— 侧栏 ————————————————————————————————— */

async function boot() {
  const meta = await getJSON('/api/meta');
  $('#where').textContent = meta.root;
  $('#kinds').innerHTML = meta.kinds.map((k) =>
    `<span class="kind ${k.off ? 'off' : ''}" title="${esc(k.env)}">` +
    `${esc(k.kind)} <b>${esc(k.provider || '关')}</b></span>`).join('');
  $('#emptyhint').innerHTML =
    `版本 ${esc(meta.version)} · 数据库 <code>${esc(meta.db)}</code>`;

  state.runs = await getJSON('/api/runs');
  state.projects = await getJSON('/api/projects');
  renderRuns();
  renderProjects();
  const q = $('#q');
  if (q) {
    q.value = state.query || '';
    q.oninput = () => {
      state.query = q.value;
      renderRuns();
      renderProjects();
    };
  }
  if (!route() && state.runs.length) go('#/run/' + state.runs[0].run_id);
  // 顶层 const 不会挂到 window 上，只能用这个名字判断它有没有定义
  if (typeof I18N !== 'undefined') {
    I18N.start();
    const lang = document.getElementById('lang');
    if (lang) lang.onclick = () => I18N.toggle();
  }
}


/* 搜索：一个框过滤两个列表。
 * 没引入任何下拉菜单，因为大家找 run 就是凭记忆里的一个词：
 * 剧名、这一句想法、run_id 的尾巴、“第 2 集”。所以把能看到的字全拿去匹配。 */
function matches(text) {
  const q = (state.query || '').trim().toLowerCase();
  if (!q) return true;
  return String(text || '').toLowerCase().includes(q);
}

function renderRuns() {
  const all = state.runs || [];
  const runs = all.filter((r) => matches(
    [r.title, r.idea, r.run_id, r.stage, r.state, r.episode_no].filter(Boolean).join(' ')));
  $('#runcount').textContent = runs.length === all.length
    ? (all.length ? `(${all.length})` : '')
    : `(${runs.length}/${all.length})`;
  if (!runs.length) {
    $('#runs').innerHTML = all.length
      ? '<li class="dim" style="cursor:default">没有匹配的 run。</li>'
      : '<li class="dim" style="cursor:default">还没有 run。' +
        '先 <code>python run.py run --idea "..."</code></li>';
    return;
  }
  $('#runs').innerHTML = runs.map((r) => {
    const no = r.episode_no ? `第 ${r.episode_no} 集 · ` : '';
    const title = r.title || r.idea || '(没有标题)';
    return `<li data-run="${esc(r.run_id)}">
      <span class="t">${badge(r.state)} ${esc(title)}</span>
      <span class="m">${esc(no)}${esc(r.run_id)} · ${moneyLabel(r)}</span>
    </li>`;
  }).join('');
  $('#runs').querySelectorAll('li[data-run]').forEach((li) => {
    li.onclick = () => go('#/run/' + li.dataset.run);
  });
}

function renderProjects() {
  const all = state.projects || [];
  const projects = all.filter((p) => matches(
    [p.title, p.project_id, p.logline].filter(Boolean).join(' ')));
  $('#projcount').textContent = projects.length === all.length
    ? (all.length ? `(${all.length})` : '')
    : `(${projects.length}/${all.length})`;
  if (!projects.length) {
    $('#projects').innerHTML = all.length
      ? '<li class="dim" style="cursor:default">没有匹配的项目。</li>'
      : '<li class="dim" style="cursor:default">还没有项目。' +
        '一次 run 一集就是单集模式（角色每集重新发明）</li>';
    return;
  }
  $('#projects').innerHTML = projects.map((p) => {
    if (p._error) {
      return `<li style="cursor:default"><span class="t" style="color:var(--bad)">` +
        `读不出来</span><span class="m">${esc(p._error)}</span></li>`;
    }
    return `<li data-proj="${esc(p.project_id)}">
      <span class="t">${esc(p.title || p.project_id)}</span>
      <span class="m">${p.characters} 个角色 · ${esc(p.project_id)}</span>
    </li>`;
  }).join('');
  $('#projects').querySelectorAll('li[data-proj]').forEach((li) => {
    li.onclick = () => go('#/project/' + li.dataset.proj);
  });
}

function selectSide(el, id) {
  $('#side').querySelectorAll('li').forEach((x) => x.classList.remove('active'));
  const li = $(`li[data-run="${id}"], li[data-proj="${id}"]`);
  if (li) li.classList.add('active');
  if (el) el.classList.add('active');
}

async function openRun(runId) {
  selectSide(null, runId);
  state.runId = runId;
  state.data = await getJSON('/api/runs/' + encodeURIComponent(runId));
  // 资产单独拿一次：它可能有一两百行，塞进 run 的响应里会让每次切 tab 都重传。
  state.assets = await getJSON(
    `/api/runs/${encodeURIComponent(runId)}/assets`).then((r) => r.assets)
    .catch(() => []);
  state.tab = state.tab || 'storyboard';
  // 目标平台记在内存里（刷新回到通用）：这是"给谁写的"，不是 run 的属性。
  state.framings = (((state.data || {}).storyboard || {}).framings) || [];
  state.dialects = Object.fromEntries(
    (((state.data || {}).storyboard || {}).dialects || []).map((x) => [x.key, x]));
  if (!state.dialects[state.dialect]) state.dialect = 'generic';
  $('#empty').hidden = true;
  $('#content').hidden = false;
  renderRun();
}

async function openProject(projectId) {
  selectSide(null, projectId);
  const proj = await getJSON('/api/projects/' + encodeURIComponent(projectId));
  $('#empty').hidden = true;
  $('#content').hidden = false;
  $('#content').innerHTML = `
    <h1>${esc(proj.title || proj.project_id)}</h1>
    <div class="meta">
      <span>project_id <b>${esc(proj.project_id)}</b></span>
      <span>角色 <b>${proj.characters.length}</b></span>
      <span>更新 <b>${esc(proj.updated_at)}</b></span>
    </div>
    ${proj.logline ? `<p class="dim">${esc(proj.logline)}</p>` : ''}
    ${proj.style ? `<p class="dim">风格：${esc(proj.style)}</p>` : ''}
    <h3>角色圣经</h3>
    <div class="shots">${proj.characters.map(characterCard).join('') ||
      '<p class="dim">这部戏还没有在册角色。</p>'}</div>
    <p class="hint">这一份是 <code>projects/${esc(proj.project_id)}/project.json</code>，
      可以直接手改（改完下一集生效）。定妆照复用的是同一个文件 ——
      跨集一致性的硬保证在这里，不在提示词里。</p>`;
  bindLightbox();
}

function characterCard(c) {
  const portrait = c.portrait && c.portrait.exists
    ? `<img src="${mediaURL(c.portrait)}" alt="${esc(c.name)}">`
    : `<div class="none">${c.portrait ? '文件没了' : '没有定妆照'}</div>`;
  return `<div class="shot">
    <div class="thumb">${portrait}</div>
    <div class="body">
      <div class="row"><span class="label">${esc(c.name)}</span>
        <span class="dim">${esc(c.char_id || '无 id')}</span></div>
      ${c.aliases && c.aliases.length
        ? `<div class="row"><span class="dim">别名 ${esc(c.aliases.join('、'))}</span></div>` : ''}
      ${c.voice ? `<div class="row"><span class="dim">音色 ${esc(c.voice)}</span></div>` : ''}
      ${c.desc ? `<p class="act">${esc(c.desc)}</p>` : ''}
    </div></div>`;
}

/* ————————————————————————————————— 一条 run ————————————————————————————————— */

function renderRun() {
  const d = state.data;
  const run = d.run;
  const sb = d.storyboard;
  const qc = d.qc;
  const tabs = [
    ['storyboard', '分镜', sb ? sb.shots_total + ' 镜' : ''],
    ['timeline', '时间轴', d.timeline ? d.timeline.length + ' 段' : ''],
    ['qc', '质检', qc ? qc.state : ''],
    ['cost', '成本', d.cost ? moneyLabel({ ...d.cost, money: d.cost.total_money }) : ''],
    ['assets', '资产', d.assets ? d.assets.total + ' 件' : ''],
    ['film', '成片', ''],
  ];
  $('#content').innerHTML = `
    <h1>${esc((sb && sb.title) || run.idea || run.run_id)}</h1>
    <div class="meta">
      <span>${badge(run.state)}</span>
      <span>run <b>${esc(run.run_id)}</b></span>
      <span>阶段 <b>${esc(run.stage)}</b></span>
      ${run.episode_no ? `<span>第 <b>${esc(run.episode_no)}</b> 集</span>` : ''}
      ${run.aspect ? `<span>画幅 <b>${esc(run.aspect)}</b></span>` : ''}
      <span>创建 <b>${esc(run.created_at)}</b></span>
    </div>
    ${run.error ? `<div class="card" style="border-color:var(--bad)">
      <b style="color:var(--bad)">这条 run 出错了</b>
      <pre style="margin:8px 0 0">${esc(run.error)}</pre></div>` : ''}
    <div class="tabs">${tabs.map(([k, label, n]) =>
      `<button data-tab="${k}" class="${state.tab === k ? 'active' : ''}">${esc(label)}
       ${n ? `<span class="n">${esc(n)}</span>` : ''}</button>`).join('')}</div>
    <div id="tab"></div>`;
  $('#content').querySelectorAll('.tabs button').forEach((b) => {
    b.onclick = () => {
      state.tab = b.dataset.tab;
      go('#/run/' + state.data.run.run_id + '/' + state.tab);
    };
  });
  renderTab();
}

function renderTab() {
  const d = state.data;
  const el = $('#tab');
  const fn = {
    storyboard: tabStoryboard, timeline: tabTimeline, qc: tabQc,
    cost: tabCost, assets: tabAssets, film: tabFilm,
  }[state.tab] || tabStoryboard;
  el.innerHTML = fn(d);
  if (state.tab === 'storyboard') bindLightbox();
  if (state.tab === 'assets') bindAssets();
}

/* ——— 分镜 ——— */
function tabStoryboard(d) {
  const sb = d.storyboard;
  if (!sb) {
    return '<p class="dim">这一条 run 还没有分镜（<code>episode.json</code> 还没生成）。</p>';
  }
  const cast = sb.characters.length
    ? `<div class="card"><h2 style="margin-bottom:10px">卡司（这一集用到的脸）</h2>
        <div class="grid">${sb.characters.map((c) => `<div class="stat">
          <div class="k">${esc(c.name)}${c.aliases && c.aliases.length
            ? ' · ' + esc(c.aliases.join('、')) : ''}</div>
          <div class="v" style="font-size:12px">${esc(c.char_id || '无 id')}</div>
          <div style="margin-top:8px">${c.portrait && c.portrait.exists
            ? `<img src="${mediaURL(c.portrait)}" style="width:64px;border-radius:4px">`
            : '<span class="miss">定妆照不在</span>'}</div>
          ${promptBox(anchorFields(c))}
        </div>`).join('')}</div></div>`
    : '';
  const scenes = sb.scenes.map((sc) => `<div class="scene">
    <div class="head"><b>${esc(sc.scene_id)}</b>
      ${sc.title ? `<span>${esc(sc.title)}</span>` : ''}
      <span class="dim">${esc(sc.location || '')} ${esc(sc.time_of_day || '')}</span>
      <span class="dim">${sc.shots.length} 镜</span></div>
    <div class="shots">${sc.shots.map(shotCard).join('')}</div></div>`).join('');
  const opts = Object.values(state.dialects || {});
  const bar = `<div class="pbar">
      <button class="cp" onclick="exportPrompts()">导出全部提示词</button>
      <button class="cp" onclick="exportShotsCsv()">导出分镜表 CSV</button>
      ${opts.length ? `<label class="dim">粘贴到
        <select id="dialect" onchange="state.dialect=this.value; renderTab()">
          ${opts.map((o) => `<option value="${esc(o.key)}"${o.key === state.dialect
            ? ' selected' : ''}>${esc(o.title)}</option>`).join('')}
        </select></label>
        <span class="dim">${esc((dialectOf(state.dialect) || {}).tip || '')}</span>` : ''}
      <span class="dim">整集的定妆照 + 每镜首帧/尾帧/运镜，一键存成 .txt</span>
    </div>`;
  // 定过目标就把"目标/实际"并排写出来：只显示总时长，用户没法判断差多少
  const goal = sb.target_seconds > 0
    ? `目标 ${fmtSec(sb.target_seconds)} · 实际 ${fmtSec(sb.seconds)}`
      + `（${sb.seconds >= sb.target_seconds ? '多' : '少'} `
      + `${Math.abs(sb.seconds - sb.target_seconds).toFixed(1)}s）`
    : `总时长 ${fmtSec(sb.seconds)}`;
  return bar + cast + scenes +
    `<p class="hint">${goal} · 结构来自
     <code>episode.json</code>（可以手改，改完 <code>resume --stages frames</code>）。</p>`;
}

/* 分镜表 CSV：给制片和剪辑的那一份。
   和上面那个 .txt 不同，这份是**表格**：一行一镜，列是场次/景别/时长/台词/
   出镜/四段提示词。走同一个 Blob 下载，不碰服务器。
   两个细节不能省：单元格要按 CSV 规则转义（台词里本来就有逗号和引号），
   开头要带 BOM（不然 Excel 打开全是乱码 —— 而这份文件的第一用途就是双击）。 */
function exportShotsCsv() {
  const sb = (state.data || {}).storyboard;
  if (!sb) return;
  const cols = ['场次', '镜号', '景别', '时长秒', '画面', '结束画面', '台词',
                '出镜', '首帧提示词', '尾帧提示词', '视频提示词',
                '负向词图', '负向词视频'];
  const cell = (v) => {
    const t = (v === null || v === undefined) ? '' : String(v);
    return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  };
  const rows = [cols.join(',')];
  for (const sc of (sb.scenes || [])) {
    for (const sh of (sc.shots || [])) {
      rows.push([
        sc.scene_id, (sh.order || 0) + 1, sh.framing, (sh.duration || 0).toFixed(2),
        sh.action, sh.end_action, sh.dialogue,
        (sh.characters || []).join('、'),
        sh.image_prompt, sh.last_frame, sh.video_prompt,
        sh.negative_prompt, sh.video_negative,
      ].map(cell).join(','));
    }
  }
  const name = ((sb.title || '分镜表') + '-分镜表.csv').replace(/[\\/:*?"<>|]/g, '_');
  const blob = new Blob(['\ufeff' + rows.join('\n')],
                        {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* 一键把整集的提示词导成一份纯文本：定妆照 + 每一镜的首帧/尾帧/运镜。
   走 Blob + <a download>，不碰服务器（页面是只读的），也不需要任何依赖。
   为什么要它：四条提示词一个个点复制，做一集得点几十次，还容易漏掉最后那镜。 */
function exportPrompts() {
  const sb = (state.data || {}).storyboard;
  if (!sb) return;
  const out = [];
  const line = (s) => out.push(s == null ? '' : String(s));
  const d = dialectOf(state.dialect);
  line(`# ${sb.title || '未命名'} — 提示词${d ? '（粘贴到：' + d.title + '）' : ''}`);
  if (sb.style) line(`\n风格：${sb.style}`);
  if ((sb.characters || []).length) {
    line('\n## 角色定妆照');
    sb.characters.forEach((c) => {
      line(`\n### ${c.name}${c.desc ? '（' + c.desc + '）' : ''}`);
      const fs = anchorFields(c);
      if (!fs.length) line('（这一集没有定妆照提示词）');
      fs.forEach(([title, text]) => {
        if (fs.length > 1) line(`【${title}】`);
        line(text);
      });
    });
  }
  (sb.scenes || []).forEach((sc) => {
    line('\n## ' + [sc.scene_id, sc.title, sc.location, sc.time_of_day]
      .filter(Boolean).join(' · '));
    (sc.shots || []).forEach((sh, i) => {
      const bits = [sh.label || `第 ${i + 1} 镜`, fmtSec(sh.duration)];
      if (sh.framing) bits.push(sh.framing);
      if ((sh.characters || []).length) bits.push('出镜 ' + sh.characters.join('、'));
      line('\n### ' + bits.filter(Boolean).join(' · '));
      if (sh.dialogue) line(`台词：${sh.dialogue}`);
      // 导出的就是"要粘进去的那几段"，所以跟着当前选的目标平台走：
      // 没有负向框的平台不该在文件里留一段用不上的负向词，
      // 让人以为漏粘了；被丢掉的段落改在文件里注明原因。
      const mine = (sh.dialects || {})[state.dialect];
      if (mine) {
        (mine.fields || []).forEach((f) => {
          line(`\n【${f.title}】\n${f.text}`);
        });
        (mine.dropped || []).forEach((why) => line(`\n（${why}）`));
      } else {
        if (sh.image_prompt) line(`\n【首帧】\n${sh.image_prompt}`);
        if (sh.last_frame) line(`\n【尾帧】\n${sh.last_frame}`);
        if (sh.video_prompt) line(`\n【运镜】\n${sh.video_prompt}`);
        if (sh.negative_prompt) line(`\n【负向·图】\n${sh.negative_prompt}`);
        if (sh.video_negative) line(`\n【负向·视频】\n${sh.video_negative}`);
      }
    });
  });
  const text = out.join('\n') + '\n';
  const name = `${sb.title || 'prompts'}-提示词.txt`.replace(/[\\/:*?"<>|]/g, '_');
  // 带 BOM：Windows 记事本能认出来，Excel 直接打开也不会全是乱码。
  const blob = new Blob(['\ufeff' + text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* 提示词框：定妆照在角色卡上，首帧/尾帧/运镜在每一镜上，同一套渲染。
   这里的 textarea 只是「方便先改再复制」的草稿 —— 页面是只读的，改的东西
   不落盘。要真改就动 episode.json 再 resume（见页面底部那行提示）。 */
// fields: [标题, 正文, 提示, 字段名?]。第四项存在 + 给了 editLabel，才出现「保存」——
// 只有「通用」那份是原始字段；可灵/即梦/Seedance 那几份是裁过、去过小标题的
// 派生文本，把派生文本存回去等于把原稿改坏，所以那几个平台只让复制。
// 定妆照也会被平台改写：硬上限截断 + 一版"只留长相和拍法"的紧凑版。
// 接口只回"被改写过的那几家"，拿不到就用原文。
function anchorFields(c) {
  const v = (c.anchor_dialects || {})[state.dialect];
  if (v && v.fields && v.fields.length) {
    return v.fields.map((f) => [f.title, f.text, f.hint]);
  }
  return c.anchor_prompt ? [['定妆照', c.anchor_prompt, '拿去做「角色参考图」']] : [];
}

function promptBox(fields, editLabel) {
  fields = (fields || []).filter(([, v]) => v);
  if (!fields.length) return '';
  const inner = fields.map(([name, value, hint, field]) => `
    <div class="pf">
      <div class="pfhead"><b>${name}</b><span class="dim">${esc(hint)}</span>
        ${(editLabel && field) ? `<button class="cp" data-label="${esc(editLabel)}"
          data-field="${esc(field)}" onclick="savePrompt(this)">保存</button>` : ''}
        <button class="cp" onclick="copyField(this)">复制</button></div>
      <textarea class="pftext"${(editLabel && field) ? '' : ' readonly'} rows="${Math.min(8, Math.max(3,
        Math.ceil((value || '').length / 46)))}">${esc(value)}</textarea>
    </div>`).join('');
  const tail = editLabel
    ? '，可以直接改'
    : '，可一键复制';
  return `<details class="prompts"><summary>提示词（${fields.length} 段${tail}）</summary>${inner}</details>`;
}

// 同一镜在不同平台的字段不同（有没有负向框、认不认尾帧图、方括号标题会不会
// 被当成画面内容），这份预渲染在后端做好了，前端只负责按选择切换。
function dialectOf(key) {
  const all = (state.dialects || {});
  return all[key] || all.generic || null;
}

function rawPrompts(sh) {
  // 「通用」这一份就是库里的原始五个字段 —— 只有它可编辑：
  // 其余平台的文本是裁过、去过小标题、合并过负向词的派生结果，
  // 把派生文本存回去等于把原稿改坏。
  return promptBox([
    ['首帧', sh.image_prompt, '拿去做「首帧/参考图」', 'image_prompt'],
    ['尾帧', sh.last_frame, '拿去做「尾帧/结束帧」——空的话说明这一镜模型没写结束画面', 'last_frame'],
    ['运镜', sh.video_prompt, '拿去做「画面描述/运镜」', 'video_prompt'],
    ['负向·图（不要什么）', sh.negative_prompt, '粘到「负向提示词」框', 'negative_prompt'],
    ['负向·视频（别动坏）', sh.video_negative, '视频专用的那一组：变脸、闪烁、跳帧', 'video_negative'],
  ], sh.label);
}

function shotPrompts(sh) {
  const d = dialectOf(state.dialect);
  const mine = (sh.dialects || {})[state.dialect];
  if (state.dialect === 'generic' || !d || !mine) {
    // 老数据（payload 里还没有 dialects）也走这里，不要因为版本错位
    // 把提示词整块藏起来。
    return rawPrompts(sh);
  }
  const fields = (mine.fields || []).map(f => [f.title, f.text, f.hint]);
  const dropped = (mine.dropped || []).length
    ? `<div class="dim drop">${mine.dropped.map(esc).join('<br>')}</div>` : '';
  const note = `<div class="dim drop">这一份是给${esc(d.title)}编排的，只读。
    要改就切回「通用」，改完再切过来看。</div>`;
  return promptBox(fields) + dropped + note;
}

function note(msg, bad) {
  const el = $('#note') || (() => {
    const d = document.createElement('div');
    d.id = 'note'; d.className = 'note';
    document.body.appendChild(d); return d;
  })();
  el.textContent = msg;
  el.className = 'note show' + (bad ? ' bad' : '');
  clearTimeout(extra._t);
  extra._t = setTimeout(() => { el.className = 'note'; }, 4000);
}

async function savePrompt(btn) {
  const label = btn.dataset.label, field = btn.dataset.field;
  const ta = btn.closest('.pf').querySelector('textarea');
  btn.disabled = true; btn.textContent = '存…';
  try {
    const r = await fetch(`/api/runs/${encodeURIComponent(state.runId)}`
      + `/shots/${encodeURIComponent(label)}/prompt`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field, text: ta.value }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    btn.textContent = '已存';
    note('已存进 episode.json 和库。画面要变，点这一镜上的「重跑这一镜」。');
  } catch (e) {
    btn.textContent = '没存上';
    note('没存上：' + e.message, true);
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = '保存'; }, 1800);
}

async function rerunShot(label) {
  if (!confirm(`只重跑 ${label}？\n\n会用现在的提示词重新生成这一镜的关键帧。`)) return;
  try {
    const r = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/rerun`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ labels: [label], stages: ['frames'] }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    note(`重跑 ${label} 中……跑完这一页会自己刷新。`);
    pollJob(j.job.id);
  } catch (e) {
    note('没跑起来：' + e.message, true);
  }
}

function pollJob(jobId) {
  const tick = async () => {
    let j;
    try {
      j = await getJSON('/api/jobs/' + encodeURIComponent(jobId));
    } catch (e) { return; }
    if (j.state === 'running') { setTimeout(tick, 1500); return; }
    if (j.state === 'done') {
      note('重跑完成，正在刷新。');
      await openRun(state.runId);
    } else {
      note('重跑失败：' + (j.error || '看终端里的报错'), true);
    }
  };
  setTimeout(tick, 1200);
}

function copyField(btn) {
  const ta = btn.closest('.pf').querySelector('textarea');
  const done = () => { btn.textContent = '已复制'; setTimeout(() => { btn.textContent = '复制'; }, 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(ta.value).then(done, () => { ta.select(); document.execCommand('copy'); done(); });
  } else {
    ta.select();
    document.execCommand('copy');
    done();
  }
}

function shotCard(sh) {
  const thumb = sh.keyframe && sh.keyframe.exists
    ? `<img src="${mediaURL(sh.keyframe)}" alt="${esc(sh.label)}">`
    : `<div class="none">${sh.keyframe ? '关键帧文件没了' : '还没有关键帧'}</div>`;
  const sfx = (sh.sfx || []).map((s) =>
    `<span title="${esc(s.file ? s.file.rel : '未生成')}">${esc(s.cue)}${
      s.file && s.file.exists ? '' : '（没文件）'}</span>`).join('、');
  return `<div class="shot">
    <div class="thumb">${thumb}</div>
    <div class="body">
      <div class="row"><span class="label">${esc(sh.label)}</span>
        <span>${badge(sh.state)}</span></div>
      <div class="row"><span class="dim">${esc(sh.framing || '')}</span>
        <span class="dim">${fmtSec(sh.duration)}</span></div>
      ${sh.action ? `<p class="act">${esc(sh.action)}</p>` : ''}
      ${sh.end_action ? `<p class="act dim">结束画面：${esc(sh.end_action)}</p>` : ''}
      ${sh.dialogue ? `<p class="dlg">${esc(sh.dialogue)}</p>` : ''}
      ${lintBox(sh)}
      ${shotPrompts(sh)}
      <div class="row"><button class="cp" onclick="rerunShot('${esc(sh.label)}')"
        title="只重跑这一镜：用现在的提示词重出关键帧">重跑这一镜</button></div>
      ${storyEditor(sh)}
      ${sfx ? `<div class="sfx">音效：${sfx}</div>` : ''}
      ${sh.characters && sh.characters.length
        ? `<div class="sfx">出镜：${esc(sh.characters.join('、'))}</div>`
        : (sh.declared ? '<div class="sfx">出镜：声明为空镜</div>' : '')}
      ${sh.error ? `<div class="err">${esc(sh.error)}</div>` : ''}
    </div></div>`;
}

// 体检结果。以前它只出现在命令行输出里 —— 用户跑到页面上就看不见了，
// 于是"体检通过"这句话等于没说。这里把每一条摊开：哪一条、哪个词、怎么改。
function lintBox(sh) {
  const found = sh.lint || [];
  const warn = found.filter((f) => f.level === 'warn');
  const badge = warn.length
    ? `<span class="lint bad">体检 ${warn.length} 条</span>`
    : '<span class="lint ok">体检通过</span>';
  if (!warn.length) return `<div class="lintrow">${badge}</div>`;
  const items = warn.map((f) => `<li><span class="code">${esc(f.code)}</span>${
    esc(f.message)}</li>`).join('');
  return `<div class="lintrow">${badge}</div>
    <details class="prompts"><summary>看看是哪几条</summary>
      <ul class="lintlist">${items}</ul></details>`;
}

// 分镜原文编辑器：动作、台词、结束画面、景别。
//
// 这四样是**骨架的原料**，不是成品：改完必须让后端用本地骨架重拼提示词、
// 重算时长。只在页面上改文字、不重拼，等于骗自己 —— 出图还是旧画面。
function storyEditor(sh) {
  const framings = state.framings || [];
  const opts = framings.map((f) => `<option value="${esc(f.key)}"${
    f.key === sh.framing ? ' selected' : ''}>${esc(f.title)}</option>`).join('');
  return `<details class="prompts">
    <summary>改分镜原文（保存后自动重拼提示词）</summary>
    <div class="pf">
      <div class="pfhead"><b>景别</b><span class="dim">改了会换镜头语言</span>
        <button class="cp" onclick="saveStory(this)">保存并重拼</button></div>
      <select class="pftext" data-field="framing">${opts}</select>
    </div>
    <div class="pf">
      <div class="pfhead"><b>动作</b><span class="dim">这一镜演什么</span></div>
      <textarea class="pftext" data-field="action" rows="3">${esc(sh.action || '')}</textarea>
    </div>
    <div class="pf">
      <div class="pfhead"><b>台词</b><span class="dim">「名字：内容」；台词长短决定时长</span></div>
      <textarea class="pftext" data-field="dialogue" rows="2">${esc(sh.dialogue || '')}</textarea>
    </div>
    <div class="pf">
      <div class="pfhead"><b>结束画面</b><span class="dim">尾帧的原料，空着会退回首帧动作</span></div>
      <textarea class="pftext" data-field="end_action" rows="2">${esc(sh.end_action || '')}</textarea>
    </div>
  </details>`;
}

function saveStory(btn) {
  const box = btn.closest('details');
  const shot = btn.closest('.shot');
  const label = shot.querySelector('.label').textContent.trim();
  const body = {};
  box.querySelectorAll('[data-field]').forEach((el) => {
    const key = el.getAttribute('data-field');
    const now = (el.value || '').trim();
    const was = (el.defaultValue || '').trim();
    // 只发改过的字段：没动的字段不该被重拼一遍（重拼会覆盖手改过的提示词）
    if (now !== was) body[key] = now;
  });
  if (!Object.keys(body).length) { note('没有改动'); return; }
  btn.disabled = true;
  fetch(`/api/runs/${encodeURIComponent(state.runId)}/shots/${label}/story`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json().then((d) => ({ ok: r.ok, d })))
    .then((res) => {
      if (!res.ok) throw new Error(res.d.error || '保存失败');
      const warns = (res.d.shot.lint || []).filter((f) => f.level === 'warn');
      note(warns.length ? `已重拼，体检 ${warns.length} 条提醒` : '已保存并重拼提示词');
      openRun(state.runId);
    })
    .catch((e) => { btn.disabled = false; note(e.message, true); });
}

function bindLightbox() {
  const lb = $('#lightbox');
  $('#content').querySelectorAll('.thumb img').forEach((img) => {
    img.onclick = () => { lb.hidden = false; lb.querySelector('img').src = img.src; };
  });
  lb.onclick = () => { lb.hidden = true; };
}

/* ——— 时间轴 ——— */
function tabTimeline(d) {
  const tl = d.timeline;
  const srt = d.subtitles.srt;
  if (!tl) {
    return '<p class="dim">还没有 <code>timeline.json</code>（拼接阶段跑完才有）。</p>';
  }
  const rows = tl.map((r) => `<tr>
    <td class="num">${(Number(r.start) || 0).toFixed(2)}s</td>
    <td class="num">${(Number(r.duration) || 0).toFixed(2)}s</td>
    <td>${esc(r.shot)}</td>
    <td>${r.dialogue ? esc(r.dialogue) : '<span class="dim">—</span>'}</td>
    <td>${r.sfx && r.sfx.length ? esc(r.sfx.join('、')) : '<span class="dim">—</span>'}</td>
  </tr>`).join('');
  return `<div class="grid" style="margin-bottom:14px">
    <div class="stat"><div class="k">段数</div><div class="v">${tl.length}</div></div>
    <div class="stat"><div class="k">总长</div><div class="v">${
      fmtSec(tl.reduce((a, r) => a + (Number(r.duration) || 0), 0))}</div></div>
    <div class="stat"><div class="k">字幕文件</div>
      <div class="v" style="font-size:13px">${
        d.subtitles.file && d.subtitles.file.exists
          ? fmtBytes(d.subtitles.file.size) : '无'}</div></div>
  </div>
  <table><thead><tr><th>起点</th><th>时长</th><th>镜头</th><th>台词</th>
    <th>音效</th></tr></thead><tbody>${rows}</tbody></table>
  <h3>字幕（SRT）</h3>
  ${srt ? `<pre>${esc(srt)}</pre>` : '<p class="dim">没有 subtitles.srt。</p>'}
  <p class="hint">字幕、音画对齐、音效共用**同一份**累加时间轴 —— 字幕飘掉
    几乎永远是"字幕自己又算了一遍时间"。</p>`;
}

/* ——— 质检 ——— */
function tabQc(d) {
  const qc = d.qc;
  if (!qc) {
    return '<p class="dim">还没有质检报告。' +
      '<code>python run.py qc &lt;run_id&gt;</code> 可以补跑。</p>';
  }
  const counts = qc.counts || {};
  const findings = qc.findings || [];
  const order = { unchecked: 0, block: 1, warn: 2, info: 3 };
  const sorted = [...findings].sort((a, b) => {
    const ka = a.unchecked ? 'unchecked' : (a.severity || 'info');
    const kb = b.unchecked ? 'unchecked' : (b.severity || 'info');
    return (order[ka] ?? 9) - (order[kb] ?? 9);
  });
  return `<div class="grid" style="margin-bottom:16px">
    <div class="stat"><div class="k">结论</div><div class="v">${badge(qc.state)}</div></div>
    <div class="stat"><div class="k">阻断</div><div class="v">${counts.block || 0}</div></div>
    <div class="stat"><div class="k">警告</div><div class="v">${counts.warn || 0}</div></div>
    <div class="stat"><div class="k">没查成</div><div class="v" style="color:var(--warn)">${
      counts.unchecked || 0}</div></div>
    <div class="stat"><div class="k">检查的镜头</div><div class="v">${
      qc.shots_total || 0}</div></div>
    <div class="stat"><div class="k">资产查过</div><div class="v">${
      qc.assets_checked === false ? '—' : '✓'}</div></div>
  </div>
  ${qc.state === 'unchecked' ? `<div class="card" style="border-color:var(--warn)">
    <b style="color:var(--warn)">没查成，不等于通过。</b>
    <span class="dim">报告里有些项目没有任何东西检查过 —— 它们显示为
    「没查成」，不计入通过。</span></div>` : ''}
  ${qc.blocked_reason ? `<div class="card"><b>被挡住的原因</b>
    <pre style="margin:8px 0 0">${esc(qc.blocked_reason)}</pre></div>` : ''}
  <h3>发现 ${findings.length} 条</h3>
  ${sorted.length ? sorted.map((f) => `<div class="finding ${
    f.unchecked ? 'unchecked' : (f.severity === 'block' ? 'block' : 'warn')}">
      <div class="r">${esc(f.rule)} · ${f.unchecked ? '没查成' : esc(f.severity)}${
        f.where ? ' · ' + esc(f.where) : ''}</div>
      <div class="msg">${esc(f.message)}</div>
      ${f.detail ? `<div class="sug">${esc(f.detail)}</div>` : ''}
      ${f.suggestion ? `<div class="sug">→ ${esc(f.suggestion)}</div>` : ''}
    </div>`).join('') : '<p class="dim">没有任何发现。</p>'}
  <p class="hint">规则来自 <code>config/qc_rules.yaml</code>（版本
    ${esc(qc.rules_version || '?')}），检查于 ${esc(qc.checked_at || '')}。</p>`;
}

/* ——— 成本 ——— */
function tabCost(d) {
  const c = d.cost;
  if (!c) return '<p class="dim">没有成本记录。</p>';
  const byStage = Object.entries(c.by_stage || {}).sort((a, b) => b[1] - a[1]);
  const byProv = Object.entries(c.by_provider || {}).sort((a, b) => b[1] - a[1]);
  const maxStage = Math.max(0.0001, ...byStage.map(([, v]) => v));
  const real = Number(c.real_money) || 0;
  const sim = Number(c.simulated_money) || 0;
  return `<div class="grid" style="margin-bottom:16px">
    <div class="stat"><div class="k">实际花费</div><div class="v">${
      fmtMoney(real)}</div>${
      sim > 0 ? `<div class="k" style="margin-top:6px">另有 ${fmtMoney(sim)} 为占位估算
        （dryrun 编的价，没有真的扣费）</div>` : ''}</div>
    <div class="stat"><div class="k">调用次数</div><div class="v">${c.calls || 0}</div></div>
  </div>
  <h3>按阶段</h3>
  <table><thead><tr><th>阶段</th><th>钱</th><th style="width:40%">占比</th>
    </tr></thead><tbody>${byStage.map(([k, v]) => `<tr>
      <td>${esc(k)}${(c.by_stage_simulated || {})[k] ? ' <span class="tag">虚拟</span>' : ''}</td>
      <td class="num">${fmtMoney(v)}</td>
      <td><div style="background:var(--accent);height:7px;border-radius:4px;
        width:${Math.max(2, (v / maxStage) * 100)}%"></div></td>
    </tr>`).join('')}</tbody></table>
  <h3>按 provider</h3>
  <table><thead><tr><th>provider</th><th>钱</th></tr></thead><tbody>${
    byProv.map(([k, v]) => `<tr><td>${esc(k)}${
      (c.by_provider_simulated || {})[k] ? ' <span class="tag">虚拟</span>' : ''}</td>
      <td class="num">${fmtMoney(v)}</td></tr>`).join('')}</tbody></table>
  <h3>明细 ${(c.lines || []).length} 条</h3>
  <table><thead><tr><th>阶段</th><th>provider</th><th>单位</th><th>用量</th>
    <th>钱</th><th>镜头</th></tr></thead><tbody>${
    (c.lines || []).slice(0, 300).map((l) => `<tr>
      <td>${esc(l.stage)}</td><td>${esc(l.provider)}${
      l.simulated ? ' <span class="tag">虚拟</span>' : ''}</td><td>${esc(l.unit)}</td>
      <td class="num">${Number(l.amount || 0).toFixed(2)}</td>
      <td class="num">${fmtMoney(l.money)}</td>
      <td>${esc(l.shot_label || '')}</td></tr>`).join('')}</tbody></table>
  ${(c.lines || []).length > 300 ? '<p class="hint">只显示前 300 条。</p>' : ''}`;
}

/* ——— 资产 ——— */
function tabAssets(d) {
  const a = d.assets;
  if (!a || !a.total) {
    return `<p class="dim">这一条 run 没有资产台账。
      台账是后来才加的 —— 重跑一次就有。</p>`;
  }
  const rows = (state.assets || []).map((x) => `<tr class="clickable"
      data-asset="${esc(x.asset_id)}">
    <td>${esc(x.kind)}</td><td>${esc(x.rel.split('/').pop())}</td>
    <td>${esc(x.stage)}</td><td>${esc(x.provider || '—')}</td>
    <td class="num">${fmtBytes(x.size)}</td>
    <td>${x.file && x.file.exists ? '' : '<span class="miss">文件没了</span>'}</td>
  </tr>`).join('');
  return `<div class="grid" style="margin-bottom:16px">
    <div class="stat"><div class="k">资产</div><div class="v">${a.total}</div></div>
    <div class="stat"><div class="k">体积</div><div class="v">${
      fmtBytes(a.bytes)}</div></div>
    <div class="stat"><div class="k">血缘边</div><div class="v">${a.edges}</div></div>
    ${Object.entries(a.by_kind || {})
      .sort((x, y) => (y[1].count || 0) - (x[1].count || 0)).slice(0, 6)
      .map(([k, v]) => `<div class="stat"><div class="k">${esc(k)}</div>
        <div class="v">${v.count}<span class="u">${fmtBytes(v.bytes)}</span></div>
        </div>`).join('')}
  </div>
  <table><thead><tr><th>种类</th><th>文件</th><th>阶段</th><th>provider</th>
    <th>大小</th><th></th></tr></thead><tbody>${rows}</tbody></table>
  <div id="assetbox"></div>
  <p class="hint">台账记的是"这东西是谁生成的、从什么变来的"。
    点一行看它的上游和血缘。</p>`;
}

async function bindAssets() {
  $('#tab').querySelectorAll('tr[data-asset]').forEach((tr) => {
    tr.onclick = () => showAsset(tr.dataset.asset);
  });
}

async function showAsset(assetId) {
  const box = $('#assetbox');
  if (!box) return;
  box.innerHTML = '<p class="dim">读台账…</p>';
  const a = await getJSON('/api/assets/' + encodeURIComponent(assetId));
  const one = (list, arrow) => list.length
    ? list.map((x) => `<div>${arrow} <span class="rp">[${esc(x.rel)}]</span>
        ${esc(x.asset.kind)} ${esc(x.asset.rel.split('/').pop())}</div>`).join('')
    : '<div class="dim">无</div>';
  box.innerHTML = `<div class="card" style="margin-top:16px">
    <h3 style="margin-top:0">${esc(a.asset.rel.split('/').pop())}
      <span class="dim">${esc(a.asset.kind)}</span></h3>
    <div class="meta">
      <span>asset_id <b>${esc(a.asset.asset_id)}</b></span>
      <span>阶段 <b>${esc(a.asset.stage)}</b></span>
      <span>provider <b>${esc(a.asset.provider || '—')}</b></span>
      <span>大小 <b>${fmtBytes(a.asset.size)}</b></span>
      <span>sha256 <b>${esc(a.asset.sha256 || '—')}</b></span>
    </div>
    <div style="margin:8px 0">${
      a.asset.file && a.asset.file.exists
        ? `<a href="${esc(a.asset.file.url)}" download>下载</a>`
        : '<span class="miss">记录还在，文件没了</span>'}</div>
    ${a.asset.prompt ? `<details><summary>提示词（${esc(a.asset.prompt_hash)}）</summary>
      <pre>${esc(a.asset.prompt)}</pre></details>` : ''}
    <h3 style="font-size:13px">上游（它是由什么变来的）</h3>
    <div class="tree">${one(a.upstream, '&lt;-')}</div>
    <h3 style="font-size:13px">下游（用到了它的东西）</h3>
    <div class="tree">${one(a.downstream, '-&gt;')}</div>
    <h3 style="font-size:13px">血缘链</h3>
    <div class="tree">${a.tree.map((n) =>
      `${'&nbsp;'.repeat(n.depth * 4)}${n.depth ? '└ ' : ''}${esc(n.label)}
       <span class="rp">${esc(n.kind)}${n.via ? ' · ' + esc(n.via) : ''}</span>
       ${n.repeat ? '<span class="rp">（同上，这里不再展开）</span>' : ''}`
      ).join('<br>')}</div>
    ${a.sharing.length ? `<h3 style="font-size:13px">同内容的另一份记录</h3>
      <div class="tree">${a.sharing.map((s) =>
        `${esc(s.kind)} ${esc(s.rel)}`).join('<br>')}</div>` : ''}
  </div>`;
}

/* ——— 成片 ——— */
function tabFilm(d) {
  const f = d.files;
  const v = f.final_video;
  const rows = Object.entries(f).filter(([k]) =>
    ['episode_json', 'timeline', 'qc_report', 'cost_report', 'bgm'].includes(k));
  return (v && v.exists
    ? `<video controls preload="metadata" src="${esc(v.url)}"></video>
       <p class="hint">${fmtBytes(v.size)} · ${
         d.timeline ? fmtSec(d.timeline.reduce((a, r) => a + (r.duration || 0), 0)) : ''}
         · <a href="${esc(v.url)}" download>下载成片</a></p>`
    : `<div class="card" style="border-color:var(--warn)">
         <b style="color:var(--warn)">还没有成片</b>
         <span class="dim">${v ? '记录在，文件没了' : '拼接阶段还没跑完'}</span></div>`)
  + `<h3>其他产物</h3>
     <table><thead><tr><th>产物</th><th></th></tr></thead><tbody>${
    rows.map(([k, ref]) => `<tr><td>${esc(k)}</td><td>${fileLink(ref)}</td></tr>`)
      .join('')}</tbody></table>
     <h3>分镜 JSON</h3>
     <p class="hint">这份文件可以直接手改（改台词、改时长），
       然后 <code>python run.py resume ${esc(d.run.run_id)} --stages frames</code>。</p>
     <pre>${esc(JSON.stringify(f.episode_json_data, null, 2) || '')}</pre>`;
}

/* ————————————————————————————————— 杂项 ————————————————————————————————— */

async function getJSON(url) {
  const res = await fetch(url);
  const text = await res.text();
  if (!res.ok) {
    let msg = text;
    try { msg = JSON.parse(text).error || text; } catch (e) { /* 原样显示 */ }
    // 报错必须**自己把内容区亮出来**：进页面失败时 #content 还是 hidden、
    // #empty 还显示着，把错误卡写进一个隐藏的容器等于什么都不显示 ——
    // 用户看到的是"页面上一片空白"，而真正的原因写在看不见的地方。
    $('#empty').hidden = true;
    $('#content').hidden = false;
    $('#content').innerHTML =
      `<div class="card" style="border-color:var(--bad)"><b style="color:var(--bad)">` +
      `请求失败（${res.status}）</b><pre style="margin:8px 0 0">${esc(msg)}</pre></div>`;
    throw new Error(msg);
  }
  return JSON.parse(text);
}

boot().catch((e) => {
  $('#empty').innerHTML = `<h1>连不上</h1><p class="dim">${esc(e.message || e)}</p>`;
});
