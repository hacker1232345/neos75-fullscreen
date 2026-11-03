'use strict';
const $ = id => document.getElementById(id);
const symbols = {clock_big:'◷',date:'▦',cpu:'⌘',mem:'▥',gpu:'▧',temp:'°',history_cpu:'⌁',top_proc:'≡',disk:'▰',net:'⇵',ip:'⌗',host:'▣',battery:'▱',volume:'♪',uptime:'◴'};
const descriptions = {clock_big:'清晰的大号数字时钟',date:'今天的日期',cpu:'处理器使用率与主频',mem:'内存使用率与容量',gpu:'显卡使用率，无法读取时显示 n/a',temp:'系统提供的温度，无法读取时显示 n/a',history_cpu:'最近 60 次 CPU 使用率变化',top_proc:'累计 CPU 用时最多的进程',disk:'可用磁盘空间',net:'当前下载与上传速度',ip:'电脑当前 IPv4 地址',host:'这台电脑的名称',battery:'笔记本电量与供电状态',volume:'系统音量，无法读取时显示 n/a',uptime:'电脑从开机到现在的运行时间'};
let api, registry, config, state = {}, previews = [], page = 0, revision = -1, ready = false, pending = false, timer, confirmAction;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(timer); timer = setTimeout(() => $('toast').hidden = true, 5000); }
async function call(name, ...args) { try { return await api[name](...args); } catch (e) { toast('操作失败，请查看诊断或重新打开程序。'); console.error(e); return {ok:false}; } }
function el(tag, cls, text) { const n = document.createElement(tag); if(cls) n.className = cls; if(text !== undefined) n.textContent = text; return n; }
function openDialog(id) { if(!$(id).open) $(id).showModal(); }
function showTab(name) { $('fullscreen').hidden = name !== 'fullscreen'; $('widgets').hidden = name !== 'widgets'; for(const x of ['fullscreen','widgets']) { const b = $('tab-'+x); b.classList.toggle('selected',x===name); if(x===name) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current'); } if(name==='widgets' && ready && !state.busy && !previews.length) refreshPreview(); }
function updatePreview() { const has = previews.length > 0 && config.widgets.length > 0; page = previews.length ? ((page % previews.length) + previews.length) % previews.length : 0; $('preview-image').hidden = !has; $('preview-placeholder').hidden = has; if(has) $('preview-image').src = previews[page]; $('page-label').textContent = has ? `${page+1} / ${previews.length} 页 · 480 × 360` : '480 × 360'; $('prev-page').disabled = $('next-page').disabled = previews.length < 2; }
function renderList() { const list = $('widget-list'); list.replaceChildren(); $('widget-count').textContent = config.widgets.length; $('empty').hidden = config.widgets.length > 0;
  config.widgets.forEach((id,i) => { const w = registry.widgets.find(x=>x.id===id); const row = el('li','widget-row'); row.append(el('span','widget-symbol',symbols[id]||'◇')); const title = el('span','widget-name',w.label); title.append(el('small','',w.group)); row.append(title); const actions = el('div','row-actions');
    for(const [text,label,offset] of [['↑','上移',-1],['↓','下移',1],['×','移除',0]]) { const b = el('button','icon-button',text); b.setAttribute('aria-label',`${label}${w.label}`); b.disabled = !!state.busy || (offset!==0 && (i+offset<0 || i+offset>=config.widgets.length)); b.addEventListener('click',async()=>{ const next=[...config.widgets]; if(offset) [next[i],next[i+offset]]=[next[i+offset],next[i]]; else next.splice(i,1); await save({...config,widgets:next}); }); actions.append(b); } row.append(actions); list.append(row); });
  $('preset').value=Object.keys(registry.presets).find(k=>JSON.stringify(registry.presets[k])===JSON.stringify(config.widgets))||'custom';
  $('interval').value = config.interval; updatePreview(); }
async function save(next) { if(pending || state.busy) return; pending = true; const result = await call('configure',next); pending = false; if(!result.ok) { toast(result.message||'配置未保存。'); return; } config=result.config; $('preset').value='custom'; previews=[]; page=0; renderList(); renderCatalog(); await refreshPreview(); }
function renderCatalog() { const root=$('catalog-list'), search=$('search').value.trim().toLowerCase(); root.replaceChildren(); let count=0;
  for(const group of registry.groups) { const items=registry.widgets.filter(w=>w.group===group && `${w.label} ${descriptions[w.id]||''} ${w.id}`.toLowerCase().includes(search)); if(!items.length) continue; root.append(el('h3','catalog-group',group)); for(const w of items) { count++; const row=el('div','catalog-row'), detail=el('div'); detail.append(el('strong','',w.label),el('p','',descriptions[w.id]||w.label)); const exists=config.widgets.includes(w.id); const add=el('button','',exists?'已添加':'添加'); add.disabled=exists||!!state.busy; add.setAttribute('aria-label',exists?`${w.label}已添加`:`添加${w.label}`); add.onclick=async()=>{await save({...config,widgets:[...config.widgets,w.id]});}; row.append(detail,add); root.append(row); } }
  if(!count) root.append(el('p','empty','没有找到组件，换个关键词试试。')); }
async function refreshPreview() { if(!config.widgets.length) { previews=[]; updatePreview(); return; } const r=await call('preview'); if(!r.ok && r.message) toast(r.message); await poll(); }
function paint() { const busy=!!state.busy; $('connection-label').textContent = state.connected ? 'USB 已连接' : (state.connection||'检测键盘'); $('connection-dot').classList.toggle('online',!!state.connected);
  $('status-title').textContent=state.title; $('status-message').textContent=state.message; $('status').classList.toggle('error',!!state.error); $('status').classList.toggle('done',state.phase==='done'); $('status-icon').textContent=state.error?'!':state.phase==='done'?'✓':busy?'…':'·';
  $('progress').hidden=!busy || !['patch','restore'].includes(state.kind); $('progress').value=state.progress||0;
  for(const id of ['connect','patch','add-widget','empty-add','preset','interval','preview-refresh','force']) $(id).disabled=busy;
  $('restore').disabled=busy||!state.restore; $('sync').disabled=busy&&state.kind!=='stream' || (!config.widgets.length&&!busy); $('sync').textContent=state.kind==='stream'?'停止同步':'开始同步'; $('sync-note').textContent=state.stream?`已发送 ${state.sent||0} 帧 · 请看屏确认`:'通过 USB 同步到键盘';
  // Preserve focus during polling: mutate action state, don't rebuild the list.
  $('widget-list').querySelectorAll('li').forEach((row,i)=>row.querySelectorAll('button').forEach((b,j)=>b.disabled=busy||(j===0&&i===0)||(j===1&&i===config.widgets.length-1)));
  if($('catalog').open) $('catalog-list').querySelectorAll('button').forEach(b=>{b.disabled=busy||b.textContent==='已添加';});
}
let polling=false;
async function poll() { if(!ready || polling) return; polling=true; try { const r=await api.snapshot(revision); state=r.state; if(r.previews) { previews=r.previews; revision=r.revision; updatePreview(); } paint(); } catch(e) { $('status-title').textContent='连接程序失败'; $('status-message').textContent='请关闭窗口后重新打开程序。'; } finally { polling=false; } }
function confirm(title,copy,button,fn) { $('confirm-title').textContent=title; $('confirm-copy').textContent=copy; $('confirm-action').textContent=button; confirmAction=fn; openDialog('confirmation'); }
async function boot(bridge) { if(ready) return; api=bridge; const data=await api.bootstrap(); registry=data.registry; config=data.config; ready=true; $('version').textContent=`v${data.version}`; $('backup-path').textContent=data.backupDirectory; renderList(); renderCatalog(); await poll(); setInterval(poll,700); }
$('tab-fullscreen').onclick=()=>showTab('fullscreen'); $('tab-widgets').onclick=()=>showTab('widgets');
$('connect').onclick=async()=>{const r=await call('connect');if(!r.ok)toast(r.message);await poll();};
$('patch').onclick=()=>{const force=$('force').checked; confirm(force?'无验证应用补丁':'应用全屏补丁',force?'将跳过版本与旧字节检查。固定偏移可能不兼容，并有导致屏幕无法启动的风险。仍然继续？':'程序会先保存这把键盘的完整备份，再自动匹配固件、应用补丁并回验。已是全屏版本则直接跳过；未知版本会停止。部分适配配置尚待实机验收。','备份并应用',()=>call('patch',true,force));};
$('confirm-action').onclick=async()=>{ $('confirmation').close(); const result=await confirmAction(); if(!result.ok)toast(result.message); await poll(); };
for(const b of document.querySelectorAll('.close')) b.onclick=()=>b.closest('dialog').close();
for(const id of ['add-widget','empty-add']) $(id).onclick=()=>{renderCatalog();openDialog('catalog');$('search').focus();};
$('search').oninput=renderCatalog;
$('preset').onchange=async e=>{if(registry.presets[e.target.value])await save({...config,widgets:[...registry.presets[e.target.value]]});};
$('interval').onchange=async e=>save({...config,interval:Number(e.target.value)});
$('preview-refresh').onclick=refreshPreview; $('prev-page').onclick=()=>{page--;updatePreview();}; $('next-page').onclick=()=>{page++;updatePreview();};
$('sync').onclick=async()=>{if(state.kind==='stream') {await call('stop');await poll();} else confirm('开始同步小组件','当前屏幕图片会被替换为组件画面。多页组件会依次轮播；请先关闭官方程序的上传任务。','开始同步',async()=>{const r=await call('start');await poll();return r;});};
$('backup-settings').onclick=()=>openDialog('settings'); $('open-backups').onclick=()=>call('open_backups');
$('restore').onclick=()=>{ $('settings').close(); confirm('恢复最近一次备份','请连接上次应用补丁的同一把键盘。程序会检查现场固件，再恢复该次应用前的备份。','检查并恢复',()=>call('restore',true));};
$('diagnostic').onclick=async()=>{const data=await call('diagnostics');$('log').textContent=data.text||'无法读取诊断。';openDialog('diagnostics');};
$('copy-log').onclick=async()=>{try{await navigator.clipboard.writeText($('log').textContent);toast('诊断记录已复制。');}catch{const selection=window.getSelection(),range=document.createRange();range.selectNodeContents($('log'));selection.removeAllRanges();selection.addRange(range);toast('已选中记录，请按 Ctrl+C（Mac 按 ⌘C）复制。');}};
window.addEventListener('pywebviewready',()=>boot(window.pywebview.api));
if(window.pywebview?.api) boot(window.pywebview.api);
// A visual browser review can supply a local test fixture. Never exposes a USB API over HTTP.
window.addEventListener('neos75previewready',()=>boot(window.neos75Preview));
