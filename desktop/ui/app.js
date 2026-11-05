'use strict';
const $ = id => document.getElementById(id);
const symbols = {clock_big:'◷',date:'▦',cpu:'⌘',mem:'▥',gpu:'▧',temp:'°',history_cpu:'⌁',top_proc:'≡',disk:'▰',net:'⇵',ip:'⌗',host:'▣',battery:'▱',volume:'♪',uptime:'◴'};
const descriptions = {clock_big:'清晰的大号数字时钟',date:'今天的日期',cpu:'处理器使用率与主频',mem:'内存使用率与容量',gpu:'显卡使用率，无法读取时显示 n/a',temp:'系统提供的温度，无法读取时显示 n/a',history_cpu:'最近 60 次 CPU 使用率变化',top_proc:'累计 CPU 用时最多的进程',disk:'可用磁盘空间',net:'当前下载与上传速度',ip:'电脑当前 IPv4 地址',host:'这台电脑的名称',battery:'笔记本电量与供电状态',volume:'系统音量，无法读取时显示 n/a',uptime:'电脑从开机到现在的运行时间'};
let api, registry, config, state = {}, previews = [], page = 0, displayPage = null, revision = -1, ready = false, pending = false, timer, confirmAction;
let nativeInfo = {supported: [], unsupported: [], rows: ['', '', '', ''], omitted: [], ready: null}, nativeText = ['', '', '', ''];
Object.assign(symbols, {weather:'☀', codex:'⌁', focus_timer:'◷', clock_utc:'◎', day_progress:'◒', mem_available:'▥', playing:'♪', calendar:'▦', link:'↔', net_pulse:'⇵'});
descriptions.weather='所选城市的温度与天气，10 分钟更新，断网标记缓存';
descriptions.codex='本机 Codex 任务事件状态与更新时间，不显示聊天正文';
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(timer); timer = setTimeout(() => $('toast').hidden = true, 5000); }
async function call(name, ...args) { try { return await api[name](...args); } catch (e) { toast('操作失败，请查看诊断或重新打开程序。'); console.error(e); return {ok:false}; } }
function el(tag, cls, text) { const n = document.createElement(tag); if(cls) n.className = cls; if(text !== undefined) n.textContent = text; return n; }
function openDialog(id) { if(!$(id).open) $(id).showModal(); }
function showTab(name) { $('fullscreen').hidden = name !== 'fullscreen'; $('widgets').hidden = name !== 'widgets'; for(const x of ['fullscreen','widgets']) { const b = $('tab-'+x); b.classList.toggle('selected',x===name); if(x===name) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current'); } if(name==='widgets' && ready && !state.busy && !previews.length) refreshPreview(); }
function updatePreview() { const has = previews.length > 0 && config.widgets.length > 0; page = previews.length ? ((page % previews.length) + previews.length) % previews.length : 0; $('preview-image').hidden = !has; $('preview-placeholder').hidden = has; if(has) $('preview-image').src = previews[page]; $('page-label').textContent = has ? `${page+1} / ${previews.length} 页 · 480 × 360` : '480 × 360'; $('prev-page').disabled = $('next-page').disabled = previews.length < 2; }
function renderList() { const list = $('widget-list'); list.replaceChildren(); $('widget-count').textContent = config.widgets.length; $('empty').hidden = config.widgets.length > 0;
  config.widgets.forEach((id,i) => { const w = registry.widgets.find(x=>x.id===id); const row = el('li','widget-row'); const mark=el('span','widget-symbol',symbols[id]||'◇'); mark.setAttribute('aria-hidden','true'); row.append(mark); const title = el('span','widget-name',w.label); title.append(el('small','',w.group)); row.append(title); const actions = el('div','row-actions');
    for(const [text,label,offset] of [['↑','上移',-1],['↓','下移',1],['×','移除',0]]) { const b = el('button','icon-button',text); b.setAttribute('aria-label',`${label}${w.label}`); b.disabled = !!state.busy || (offset!==0 && (i+offset<0 || i+offset>=config.widgets.length)); b.addEventListener('click',async()=>{ const next=[...config.widgets]; if(offset) [next[i],next[i+offset]]=[next[i+offset],next[i]]; else next.splice(i,1); await save({...config,widgets:next}); }); actions.append(b); } row.append(actions); list.append(row); });
  $('preset').value=Object.keys(registry.presets).find(k=>JSON.stringify(registry.presets[k])===JSON.stringify(config.widgets))||'custom';
  $('focus-form').hidden = !config.widgets.includes('focus_timer');
  paintThemes(false);
  $('interval').value = config.interval; $('current-city').textContent=config.weather?.label||'尚未设置城市'; updatePreview(); }
async function save(next) { if(pending || state.busy) return; pending = true; const result = await call('configure',next); pending = false; if(!result.ok) { toast(result.message||'配置未保存。'); return; } config=result.config; $('preset').value='custom'; previews=[]; page=0; renderList(); renderCatalog(); await refreshPreview(); }
function renderCatalog() { const root=$('catalog-list'), search=$('search').value.trim().toLowerCase(); root.replaceChildren(); let count=0;
  for(const group of registry.groups) { const items=registry.widgets.filter(w=>w.group===group && `${w.label} ${w.desc||descriptions[w.id]||''} ${w.id}`.toLowerCase().includes(search)); if(!items.length) continue; root.append(el('h3','catalog-group',group)); for(const w of items) { count++; const row=el('div','catalog-row'), detail=el('div'); detail.append(el('strong','',w.label),el('p','',w.desc||descriptions[w.id]||w.label)); const exists=config.widgets.includes(w.id); const add=el('button','',exists?'已添加':'添加'); add.disabled=exists||!!state.busy; add.setAttribute('aria-label',exists?`${w.label}已添加`:`添加${w.label}`); add.onclick=async()=>{await save({...config,widgets:[...config.widgets,w.id]});}; row.append(detail,add); root.append(row); } }
  if(!count) root.append(el('p','empty','没有找到组件，换个关键词试试。')); }
async function refreshPreview() { if(!config.widgets.length) { previews=[]; updatePreview(); return; } const r=await call('preview'); if(!r.ok && r.message) toast(r.message); await poll(); }
function paint() { const busy=!!state.busy; $('connection-label').textContent = state.connected ? 'USB 已连接' : (state.connection||'检测键盘'); $('connection-dot').classList.toggle('online',!!state.connected);
  $('status-title').textContent=state.title; $('status-message').textContent=state.message; $('status').classList.toggle('error',!!state.error); $('status').classList.toggle('done',state.phase==='done'); $('status-icon').textContent=state.error?'!':state.phase==='done'?'✓':busy?'…':'·';
  $('progress').hidden=!busy || !['patch','restore'].includes(state.kind); $('progress').value=state.progress||0;
  for(const id of ['connect','patch','add-widget','empty-add','preset','interval','preview-refresh','force']) $(id).disabled=busy;
  $('restore').disabled=busy||!state.restore; $('sync').disabled=busy&&state.kind!=='stream' || (!config.widgets.length&&!busy); $('sync').textContent=state.kind==='stream'?'停止同步':'开始同步'; $('sync-note').textContent=state.stream?`已发送 ${state.sent||0} 帧 · 请看屏确认`:'通过 USB 同步到键盘';
  // Preserve focus during polling: mutate action state, don't rebuild the list.
  $('widget-list').querySelectorAll('li').forEach((row,i)=>row.querySelectorAll('button').forEach((b,j)=>b.disabled=busy||(j===0&&i===0)||(j===1&&i===config.widgets.length-1)));
  for(const id of ['data-settings','screen-previous','screen-next','city-query','city-search','codex-thread']) $(id).disabled=busy;
  for(const id of ['pin-page','cycle-pages']) $(id).disabled=!previews.length||(busy&&state.kind!=='stream');
  $('display-mode').textContent=displayPage===null?'自动轮播':`固定第 ${displayPage+1} 页`;
  $('pin-page').setAttribute('aria-pressed',String(displayPage===page));
  if($('catalog').open) $('catalog-list').querySelectorAll('button').forEach(b=>{b.disabled=busy||b.textContent==='已添加';});
  paintNative(busy);
  paintKnob(busy);
  paintThemes(busy);
}
let polling=false;
async function poll() { if(!ready || polling) return; polling=true; try { const r=await api.snapshot(revision); state=r.state; displayPage=r.displayPage??null; if(r.text) nativeText=r.text; if('focus' in r) showFocus(r.focus); if(r.previews) { previews=r.previews; revision=r.revision; updatePreview(); } paint(); } catch(e) { $('status-title').textContent='连接程序失败'; $('status-message').textContent='请关闭窗口后重新打开程序。'; } finally { polling=false; } }
function confirm(title,copy,button,fn) { $('confirm-title').textContent=title; $('confirm-copy').textContent=copy; $('confirm-action').textContent=button; confirmAction=fn; openDialog('confirmation'); }
function nativePages() { return config.native_pages || []; }
// ── 旋钮配置 ─────────────────────────────────────────────────────
let knobInfo = { sites: [], actions: [], config: {}, factory: {}, note: '', applied: null };

function paintKnob(busy) {
  const grid = $('knob-grid');
  if (!grid) return;
  const sites = knobInfo.sites || [];
  if (grid.dataset.count !== String(sites.length)) {
    grid.replaceChildren();
    for (const s of sites) {
      const row = el('div', 'knob-row');
      row.append(el('span', 'knob-label', s.label));
      const select = el('select', 'knob-select');
      select.id = 'knob-' + s.id;
      select.dataset.site = s.id;
      for (const a of knobInfo.actions || []) {
        if (a.cat === 'screen' && s.id !== 'scr-cw' && s.id !== 'scr-ccw') continue;
        const opt = el('option', null, a.label);
        opt.value = a.id;
        select.append(opt);
      }
      select.onchange = async () => {
        const next = Object.assign({}, config.knob || {}, { [s.id]: select.value });
        await save(Object.assign({}, config, { knob: next }));
      };
      row.append(select);
      grid.append(row);
    }
    grid.dataset.count = String(sites.length);
  }
  for (const s of sites) {
    const sel = $('knob-' + s.id);
    if (!sel) continue;
    const value = (knobInfo.config || {})[s.id];
    if (value && sel.value !== value) sel.value = value;
    sel.disabled = !!busy;
  }
  const note = $('knob-note');
  if (note && knobInfo.note) note.textContent = knobInfo.note;
  const st = $('knob-state');
  if (st) {
    const applied = knobInfo.applied;
    st.textContent = applied
      ? '已生成 ' + (applied.bytes ? Math.round(applied.bytes / 1024) + ' KB' : '') + '，可写入'
      : '尚未生成';
  }
}

async function knobBuild() {
  const r = await call('knob_build');
  if (!r.ok) toast(r.message || '未能生成');
  await poll();
}

async function knobApply() {
  confirm('写入旋钮配置',
    '会先整片备份键盘固件，再擦除、写入、逐字节回验。写入期间不要拔线或退出。',
    '备份并写入', async () => {
      const r = await call('knob_flash', { confirmed: true });
      if (!r.ok) toast(r.message || '写入未完成');
      await poll();
    });
}

async function knobReset() {
  const next = Object.assign({}, config, { knob: Object.assign({}, knobInfo.factory || {}) });
  await save(next);
}

function paintNative(busy) {
  const picked = new Set(nativePages());
  const holder = $('native-pages');
  if(holder.childElementCount !== 6) {
    holder.replaceChildren();
    for(let i = 1; i <= 6; i++) {
      const b = el('button', '', `第 ${i} 屏`);
      b.dataset.page = String(i);
      b.type = 'button';
      b.onclick = async () => {
        const next = new Set(nativePages());
        if(next.has(i)) next.delete(i); else next.add(i);
        await save({...config, native_pages: [...next].sort((a,b)=>a-b)});
      };
      holder.append(b);
    }
  }
  for(const b of holder.querySelectorAll('button')) {
    const on = picked.has(Number(b.dataset.page));
    b.classList.toggle('selected', on);
    b.setAttribute('aria-pressed', String(on));
    b.disabled = busy;
  }
  const lines = nativeText.concat(['', '', '', '']).slice(0, 4);
  const surface = $('native-preview');
  const content = JSON.stringify(lines);
  if(surface.dataset.content!==content) {
    surface.dataset.content=content;
    surface.replaceChildren(...lines.map(text=>el('span', text ? '' : 'empty', text || '（空行）')));
  }
  const counting = config.widgets.filter(id => nativeInfo.supported.includes(id)).length;
  const graphed = config.widgets.filter(id => !nativeInfo.supported.includes(id));
  $('native-note').textContent = graphed.length
    ? `当前有 ${counting} 个组件可以写入；${graphed.length} 个仅支持彩色看板。每行最多 31 个 ASCII 字符。`
    : '底图只需导入一次。每屏最多四行，每行 31 个字符，支持英文、数字和常见符号。';
  $('native-interval').value = String(config.native_interval ?? 3);
  $('native-interval').disabled = busy;
  for(const id of ['native-refresh','native-check','native-apply','native-base','native-export']) $(id).disabled = busy;
  const running = state.kind === 'native-stream';
  $('native-send').disabled = busy || !config.widgets.length;
  $('native-stream').disabled = running ? false : (busy || !nativePages().length || !config.widgets.length);
  $('native-stream').textContent = running ? '停止连续发送' : '连续发送';
  // 关闭按钮只在真的开着的时候可点：已经清空就不必再写一次。
  $('native-off').disabled = busy;
  $('native-off').textContent = nativePages().length ? '关闭文字组件' : '文字组件已关闭';
}
async function refreshNative() { const r = await call('native_preview'); if(!r.ok) toast(r.message || '未生成文字画面'); await poll(); }
function paintThemes(busy) {
  const root=$('theme-chips'), list=registry?.themes||[], current=config?.theme||'hades';
  if(!root.children.length) {
    for(const t of list) {
      const b=el('button','theme-chip'); b.type='button'; b.dataset.theme=t.id;
      b.setAttribute('role','radio'); b.title=t.blurb||'';
      const sw=el('span','swatch'); sw.setAttribute('aria-hidden','true');
      sw.style.background=`linear-gradient(135deg,${t.swatch} 0 60%,${t.swatch2} 61% 100%)`;
      b.append(sw,el('span','theme-name',t.label),el('small','theme-tone',t.light?'浅色':'深色'));
      b.onclick=async()=>{ if(!state.busy && !pending && config.theme!==t.id) await save({...config,theme:t.id}); };
      b.onkeydown=e=>{
        if(state.busy || pending) return;
        const buttons=[...root.querySelectorAll('button')], i=buttons.indexOf(b);
        if(i<0) return;
        const forward=['ArrowRight','ArrowDown'].includes(e.key), back=['ArrowLeft','ArrowUp'].includes(e.key);
        if(!forward&&!back&&e.key!=='Home'&&e.key!=='End') return;
        e.preventDefault();
        const next=e.key==='Home'?0:e.key==='End'?buttons.length-1:
          (i+(forward?1:-1)+buttons.length)%buttons.length;
        buttons[next].focus(); buttons[next].click();
      };
      root.append(b);
    }
  }
  for(const b of root.querySelectorAll('button')) {
    const chosen=b.dataset.theme===current;
    b.setAttribute('aria-disabled',String(busy||!!state.busy));
    b.setAttribute('aria-checked',String(chosen)); b.tabIndex=chosen?0:-1;
  }
}

function showFocus(value) {
  const display=$('focus-left');
  const next=value===null?'未启动':value===0?'时间到':
    [Math.floor(value/60),value%60].map(n=>String(n).padStart(2,'0')).join(':');
  if(display.textContent!==next) display.textContent=next;
}
async function changeFocus(value) {
  const r=await call('set_focus',value);
  if(!r.ok) { toast(r.message||'未能调整计时'); return; }
  showFocus(r.focus);
  if(!state.busy) await refreshPreview();
}
$('focus-form').onsubmit=e=>{ e.preventDefault(); changeFocus($('focus-minutes').valueAsNumber); };
$('focus-clear').onclick=()=>changeFocus(null);
$('native-base').onchange=e=>{ $('native-preview').dataset.skin=e.target.value; };
$('native-export').onclick=async()=>{
  const button=$('native-export');
  button.disabled=true;
  try {
    const r=await call('export_native_base',$('native-base').value);
    if(!r.ok) toast(r.message||'底图未保存'); else if(r.saved) toast('底图已保存，可用 A HUB 导入。');
  } finally { button.disabled=!!state.busy; }
};

async function boot(bridge) { if(ready) return; api=bridge; const data=await api.bootstrap(); registry=data.registry; config=data.config; if(data.native) nativeInfo={...nativeInfo,...data.native}; if(data.knob) { knobInfo={...knobInfo,...data.knob}; knobInfo.config={...(data.config.knob||{})}; } ready=true; $('version').textContent=`v${data.version}`; $('backup-path').textContent=data.backupDirectory; renderList(); renderCatalog(); await poll(); setInterval(poll,700); }
$('tab-fullscreen').onclick=()=>showTab('fullscreen'); $('tab-widgets').onclick=()=>showTab('widgets');
$('connect').onclick=async()=>{const r=await call('connect');if(!r.ok)toast(r.message);await poll();};
$('patch').onclick=()=>{const force=$('force').checked; confirm(force?'无验证应用补丁':'应用全屏补丁',force?'程序会先备份，再按旧字节逐条核对能套上几层补丁。若一层都对不上，只会套最基础的一层：上传进度条和覆盖层会保留，其余功能照常。固定偏移仍可能不兼容，并有导致屏幕无法启动的风险。仍然继续？':'程序会先保存这把键盘的完整备份，再自动匹配固件、应用补丁并回验。已是全屏版本则直接跳过；未知版本会停止。部分适配配置尚待实机验收。','备份并应用',()=>call('patch',true,force));};
$('confirm-action').onclick=async()=>{ $('confirmation').close(); const result=await confirmAction(); if(!result.ok)toast(result.message); await poll(); };
for(const b of document.querySelectorAll('.close')) b.onclick=()=>b.closest('dialog').close();
for(const id of ['add-widget','empty-add']) $(id).onclick=()=>{renderCatalog();openDialog('catalog');$('search').focus();};
$('search').oninput=renderCatalog;
$('preset').onchange=async e=>{if(registry.presets[e.target.value])await save({...config,widgets:[...registry.presets[e.target.value]]});};
$('interval').onchange=async e=>save({...config,interval:Number(e.target.value)});
$('preview-refresh').onclick=refreshPreview;
async function movePage(offset) { page+=offset;updatePreview();if(displayPage!==null) {await call('select_page',page);await poll();} }
$('prev-page').onclick=()=>movePage(-1); $('next-page').onclick=()=>movePage(1);
$('pin-page').onclick=async()=>{await call('select_page',page);await poll();};
$('cycle-pages').onclick=async()=>{await call('select_page',null);await poll();};
for(const direction of ['previous','next']) $('screen-'+direction).onclick=async()=>{const r=await call('screen_page',direction,true);if(!r.ok)toast(r.message);await poll();};
$('data-settings').onclick=()=>{$('codex-thread').value=config.codex_thread||'';openDialog('data-dialog');$('city-query').focus();};
$('city-form').onsubmit=async e=>{e.preventDefault();$('city-search').disabled=true;$('city-status').textContent='正在搜索城市…';$('city-results').replaceChildren();try{const r=await call('search_city',$('city-query').value);$('city-status').textContent=r.ok?(r.cities.length?'请选择城市':'没有找到城市，可尝试拼音或添加地区名'):r.message||'搜索失败';for(const city of r.cities||[]){const button=el('button','secondary',city.label);button.type='button';button.onclick=async()=>{await save({...config,weather:city});$('city-status').textContent=config.weather?.label===city.label?'城市已保存':'城市未保存，请先停止当前操作';};$('city-results').append(button);}}finally{$('city-search').disabled=!!state.busy;}};
$('codex-form').onsubmit=async e=>{e.preventDefault();const thread=$('codex-thread').value.trim();if(thread&&!/^[a-zA-Z0-9_-]{8,80}$/.test(thread)){$('codex-error').textContent='请输入有效任务 ID，或留空自动跟随。';$('codex-thread').focus();return;}await save({...config,codex_thread:thread});$('codex-error').textContent=config.codex_thread===thread?'任务选择已保存':'未保存，请先停止当前操作';};
$('native-refresh').onclick=refreshNative;
$('native-check').onclick=async()=>{const r=await call('native_probe');if(!r.ok)toast(r.message);await poll();};
$('native-apply').onclick=async()=>{const r=await call('native_pages');if(!r.ok)toast(r.message);await poll();};
// 一键关闭：把「显示在哪些界面」清空再应用，等于给键盘写掩码 0，四行文字立刻从所有界面消失。
$('native-off').onclick=async()=>{ if(nativePages().length) await save({...config, native_pages: []}); const r=await call('native_pages'); if(!r.ok) toast(r.message); else toast('已关闭文字组件，所有界面恢复原厂内容。'); await poll(); };
$('native-send').onclick=async()=>{const r=await call('native_push');if(!r.ok)toast(r.message);await poll();};
$('native-stream').onclick=async()=>{ if(state.kind==='native-stream') {await call('stop');await poll();return;} const r=await call('native_start');if(!r.ok)toast(r.message);await poll();};
$('native-interval').onchange=async e=>save({...config,native_interval:Number(e.target.value)});
$('sync').onclick=async()=>{if(state.kind==='stream') {await call('stop');await poll();} else confirm('开始同步小组件','当前屏幕图片会被替换为组件画面。多页组件会依次轮播；请先关闭官方程序的上传任务。','开始同步',async()=>{const r=await call('start');await poll();return r;});};
$('backup-settings').onclick=()=>openDialog('settings'); $('open-backups').onclick=()=>call('open_backups');
$('restore').onclick=()=>{ $('settings').close(); confirm('恢复最近一次备份','请连接上次应用补丁的同一把键盘。程序会检查现场固件，再恢复该次应用前的备份。','检查并恢复',()=>call('restore',true));};
$('diagnostic').onclick=async()=>{const data=await call('diagnostics');$('log').textContent=data.text||'无法读取诊断。';openDialog('diagnostics');};
$('copy-log').onclick=async()=>{try{await navigator.clipboard.writeText($('log').textContent);toast('诊断记录已复制。');}catch{const selection=window.getSelection(),range=document.createRange();range.selectNodeContents($('log'));selection.removeAllRanges();selection.addRange(range);toast('已选中记录，请按 Ctrl+C（Mac 按 ⌘C）复制。');}};
window.addEventListener('pywebviewready',()=>boot(window.pywebview.api));
if(window.pywebview?.api) boot(window.pywebview.api);
// A visual browser review can supply a local test fixture. Never exposes a USB API over HTTP.
window.addEventListener('neos75previewready',()=>boot(window.neos75Preview));

$('knob-preview').onclick = knobBuild;
$('knob-apply').onclick = knobApply;
$('knob-reset').onclick = knobReset;
