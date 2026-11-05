'use strict';
// dashboard/push.js —— 把电脑状态实时推到 Neos75 键盘小屏。
//
// 协议实现全部复用 ../wallpaper/screen_wallpaper.js（那个文件是只读参考，不改）：
//   crc32 / jpegInfo / buildStream / report / frames / readPacket / parseRead / exchange / transmit / validateTarget
//
// 命令：
//   node dashboard/push.js info                      只读：屏幕信息 + 剩余空间
//   node dashboard/push.js dry  [--widgets ...]      只采样 + 渲染，不推送（零风险，不开 USB）
//   node dashboard/push.js once [--widgets ...]      推一次
//   node dashboard/push.js loop [--widgets ...] [--interval 3000]
//   node dashboard/push.js push <file.jpg>           推现成 JPEG
//
// 公共选项：
//   --preset default|minimal|full|performance|network   （--widgets 优先）
//   --widgets a,b,c      逗号分隔的部件 id
//   --quality 72         初始 JPEG 质量
//   --limit 30000        字节上限，超出由 render.py 二分降质量
//   --state-dir DIR      历史采样目录（供 history_cpu 部件画折线）
//   --interval 3000      loop 的推送周期（毫秒）
//   --retries 4          推送失败后的最大尝试次数
//   --json               每轮输出一行 JSON（便于 GUI 解析）
//
// 退出码：0 成功 / 1 运行失败 / 2 参数错误 / 3 设备不匹配（尺寸或空间）
//
// 重要限制：原厂协议尾段没有最终 ACK，displayVerified 恒为 false，
// 推送后必须提示用户看屏确认。协议无序列号，同一时刻只允许一个上传者。

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const WALL = require('../wallpaper/screen_wallpaper.js');
const { jpegInfo, buildStream, frames, readPacket, parseRead, exchange, transmit, validateTarget } = WALL;

const HERE = __dirname;
const REPO = path.resolve(HERE, '..');
const W = 480;
const H = 360;

const PY = process.env.NEOS75_PYTHON || 'python';

// pwsh 优先，退回 Windows PowerShell 5.1（老系统上可能只有 powershell.exe）。
// 惰性解析：只在真的要起采样进程时才探一次，避免每次 require 都白付两次 spawn。
let _PS = null;
function psExe() {
  if (_PS) return _PS;
  if (process.env.NEOS75_PWSH) { _PS = process.env.NEOS75_PWSH; return _PS; }
  const { spawnSync } = require('node:child_process');
  for (const name of ['pwsh', 'powershell']) {
    try {
      const r = spawnSync(name, ['-NoProfile', '-Command', 'exit 0'],
        { windowsHide: true, stdio: 'ignore', timeout: 15000 });
      if (!r.error && r.status === 0) { _PS = name; return _PS; }
    } catch { /* 换下一个 */ }
  }
  _PS = 'pwsh'; // 都探不到就保持原名，让 spawn 的报错自己说话
  return _PS;
}

const HID_VID = 0x5542;
const HID_PID = 0x0001;

// ── 参数解析 ──────────────────────────────────────────────────────
function parseArgs(argv) {
  const o = {
    mode: null, file: null, widgets: null, preset: 'default',
    quality: 72, limit: 30000, interval: 3000, retries: 4,
    stateDir: path.join(os.tmpdir(), 'neos75-dashboard-state'),
    json: false, timeout: 1500,
  };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--widgets') o.widgets = argv[++i];
    else if (a === '--preset') o.preset = argv[++i];
    else if (a === '--quality') o.quality = Number(argv[++i]);
    else if (a === '--limit') o.limit = Number(argv[++i]);
    else if (a === '--interval') o.interval = Number(argv[++i]);
    else if (a === '--retries') o.retries = Number(argv[++i]);
    else if (a === '--state-dir') o.stateDir = argv[++i];
    else if (a === '--json') o.json = true;
    else if (a.startsWith('--')) throw new Error('未知选项: ' + a);
    else rest.push(a);
  }
  o.mode = rest[0] || null;
  o.file = rest[1] || null;
  return o;
}

// ── node-hid 解析 ─────────────────────────────────────────────────
// 查找顺序：
//   1) 常规 require('node-hid')（Node 会从 dashboard/ 逐级向上找 node_modules，
//      所以父目录 aulac/neos75/node_modules 里的那份也能命中）
//   2) NODE_PATH 里的每个目录
//   3) 环境变量 NEOS75_NODE_MODULES 指向的绝对 node_modules 目录
//   4) 仓库根与其父目录下的 node_modules（桌面端 dash_bridge 会把 NODE_PATH 指到
//      仓库根的 node_modules，而实际安装位置可能在上一级，所以这里显式兜一层）
function hidModule() {
  try {
    return require('node-hid');
  } catch { /* 继续按目录挨个试 */ }

  const cands = [];
  for (const d of String(process.env.NODE_PATH || '').split(path.delimiter)) {
    if (d.trim()) cands.push(d.trim());
  }
  if (process.env.NEOS75_NODE_MODULES) cands.push(process.env.NEOS75_NODE_MODULES);
  cands.push(path.join(REPO, 'node_modules'));
  cands.push(path.join(path.dirname(REPO), 'node_modules'));

  const tried = [];
  for (const dir of cands) {
    tried.push(dir);
    try {
      return require(path.join(dir, 'node-hid'));
    } catch { /* 换下一个 */ }
  }
  throw new Error('找不到 node-hid。已尝试: ' + tried.join(' ; ')
    + '。请安装 node-hid，或设置 NEOS75_NODE_MODULES 为已有 node_modules 的绝对目录');
}

// ── 采样：常驻一个 pwsh 进程，按行读 stdout ───────────────────────
// 每次 spawn 新 pwsh 要 6~8 秒（含 Add-Type 编译 CoreAudio 类型），
// 所以 loop 必须复用同一个进程。
let o_json_quiet = false;

function samplerArgs(intervalMs, warmup) {
  const a = ['-NoProfile', '-NonInteractive', '-File', path.join(HERE, 'live_state.ps1'),
    '-IntervalMs', String(Math.max(0, Math.round(intervalMs)))];
  if (warmup) a.push('-Warmup');
  return a;
}

function startSampler(intervalMs, warmup = true) {
  const child = spawn(psExe(), samplerArgs(intervalMs, warmup), { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  let buf = '';
  let closed = false;
  let fatal = null;
  const queue = [];
  const waiters = [];
  const QUEUE_MAX = 8; // 消费者落后时只保留最新几帧，绝不无限堆积

  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).replace(/\r$/, '').replace(/^\uFEFF/, '').trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      let obj = null;
      try { obj = JSON.parse(line); } catch { continue; } // 非 JSON 行直接丢，不打断流
      if (waiters.length) waiters.shift().resolve(obj);
      else {
        queue.push(obj);
        if (queue.length > QUEUE_MAX) queue.splice(0, queue.length - QUEUE_MAX);
      }
    }
  });
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (d) => { if (!o_json_quiet) process.stderr.write('[ps] ' + d); });
  child.on('error', (e) => { fatal = e; while (waiters.length) waiters.shift().reject(e); });
  child.on('close', (code) => {
    closed = true;
    const err = new Error('采样进程退出（exit ' + code + '）');
    while (waiters.length) waiters.shift().reject(err);
  });

  return {
    child,
    get closed() { return closed; },
    get fatal() { return fatal; },
    next(timeoutMs = 15000) {
      if (queue.length) return Promise.resolve(queue.shift());
      if (fatal) return Promise.reject(fatal);
      if (closed) return Promise.reject(new Error('采样进程已退出'));
      return new Promise((resolve, reject) => {
        const entry = { resolve, reject };
        waiters.push(entry);
        const timer = setTimeout(() => {
          const i = waiters.indexOf(entry);
          if (i >= 0) waiters.splice(i, 1);
          reject(new Error('等待采样超时（' + timeoutMs + 'ms）'));
        }, timeoutMs);
        const done = (fn) => (v) => { clearTimeout(timer); fn(v); };
        entry.resolve = done(resolve);
        entry.reject = done(reject);
      });
    },
    stop() {
      try { child.kill(); } catch { /* 已退出 */ }
    },
  };
}

// ── 正在播放（SMTC）────────────────────────────────────────────
// 单独一路进程，而且固定用 powershell.exe（5.1）：
// Windows.Media.Control 是 WinRT 组件，PowerShell 7 里根本没有这个类型，
// 而上面的 psExe() 优先选 pwsh。所以这里不跟主采样器共享解释器。
let _PS51 = null;
function ps51() {
  if (_PS51) return _PS51;
  if (process.env.NEOS75_POWERSHELL51) { _PS51 = process.env.NEOS75_POWERSHELL51; return _PS51; }
  const { spawnSync } = require('node:child_process');
  const cand = path.join(process.env.SystemRoot || 'C:\\Windows',
    'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  try {
    if (fs.existsSync(cand)) {
      const r = spawnSync(cand, ['-NoProfile', '-Command', 'exit 0'],
        { windowsHide: true, stdio: 'ignore', timeout: 15000 });
      if (!r.error && r.status === 0) { _PS51 = cand; return _PS51; }
    }
  } catch { /* 退回裸名 */ }
  _PS51 = 'powershell';
  return _PS51;
}

function startMedia(intervalMs = 2000) {
  const child = spawn(ps51(),
    ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      path.join(HERE, 'media_state.ps1'), '-IntervalMs', String(Math.max(500, intervalMs))],
    { stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true });
  let buf = '';
  let latest = null;
  let seenAt = 0;
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).replace(/\r$/, '').replace(/^\uFEFF/, '').trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      try {
        const obj = JSON.parse(line);
        latest = obj;
        seenAt = Date.now();
      } catch { /* 非 JSON 行直接丢 */ }
    }
  });
  child.on('error', () => { /* 拿不到音乐不影响其它字段 */ });
  return {
    child,
    // 超过 30 秒没更新就当作不可用，避免把一首老歌当成「正在播放」。
    get value() {
      if (!latest) return null;
      return Date.now() - seenAt > 30000 ? null : latest;
    },
    stop() { try { child.kill(); } catch { /* 已退出 */ } },
  };
}

// 把媒体字段并进状态；没装播放器时给 null，render 会写「未在播放」。
function withMedia(state, media) {
  state.media = media || null;
  return state;
}

// 等第一帧媒体数据（最多 3 秒）：读不到就返回 null，不拖慢推送。
function waitMedia(media, timeoutMs = 3000) {
  return new Promise((resolve) => {
    if (media.value) { resolve(media.value); return; }
    const t0 = Date.now();
    const tick = () => {
      if (media.value) { resolve(media.value); return; }
      if (Date.now() - t0 >= timeoutMs) { resolve(null); return; }
      setTimeout(tick, 120);
    };
    tick();
  });
}

// 一次性采样：spawn 一个带 -Warmup 的 pwsh，取第一行就收工
async function sampleOnce(intervalMs = 0) {
  const s = startSampler(intervalMs, true);
  try {
    return await s.next(30000);
  } finally {
    s.stop();
  }
}

// ── 渲染：源码运行时调 python dashboard/render.py；打包运行时调随包渲染器 ──
// 桌面打包版不能让目标机器先装 Python + Pillow，所以由桌面程序通过 NEOS75_RENDER
// 指向随包的独立渲染器可执行文件；那个 exe 就是 dashboard/render.py 冻结出来的，
// 命令行参数完全一致。
function rendererCommand(pyArgs) {
  const exe = process.env.NEOS75_RENDER;
  if (exe && fs.existsSync(exe)) return { cmd: exe, args: pyArgs };
  return { cmd: PY, args: [path.join(HERE, 'render.py'), ...pyArgs] };
}

function renderJpeg(state, o) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'neos75-dash-'));
  const out = path.join(dir, 'frame.jpg');
  const pyArgs = ['--in', '-', '--out', out,
    '--preset', o.preset, '--quality', String(o.quality), '--state-dir', o.stateDir];
  if (o.widgets) { pyArgs.push('--widgets', o.widgets); }
  if (o.limit > 0) { pyArgs.push('--limit', String(o.limit)); }

  const { cmd, args } = rendererCommand(pyArgs);
  const t0 = Date.now();
  const { spawnSync } = require('node:child_process');
  const r = spawnSync(cmd, args, { input: JSON.stringify(state), encoding: 'utf8', windowsHide: true, timeout: 60000 });
  const ms = Date.now() - t0;
  if (r.error) throw new Error('渲染进程启动失败: ' + r.error.message);
  if (r.status !== 0) {
    const msg = (r.stderr || r.stdout || '').trim();
    const e = new Error('渲染失败（exit ' + r.status + '）: ' + msg);
    e.exitCode = r.status === 3 ? 2 : 1;
    throw e;
  }
  const jpeg = fs.readFileSync(out);
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* 临时目录清不掉不影响主流程 */ }
  return { jpeg, ms };
}

// ── 设备 ──────────────────────────────────────────────────────────
function findScreen(HID) {
  const found = HID.devices().filter((d) =>
    d.vendorId === HID_VID && d.productId === HID_PID && d.usagePage === 0xff60 && d.usage === 0x61);
  if (found.length !== 1) throw new Error('需且仅需一个 5542:0001 FF60/61 屏幕接口（当前 ' + found.length + ' 个）');
  return found[0];
}

function openDevice() {
  const HID = hidModule();
  const info = findScreen(HID);
  const dev = new HID.HID(info.path);
  let fatal = null;
  const onError = (e) => { fatal = e; };
  dev.on('error', onError);
  return {
    HID, dev, devPath: info.path,
    get fatal() { return fatal; },
    close() { try { dev.removeListener('error', onError); dev.close(); } catch { /* 已关闭 */ } },
  };
}

// 读设备信息（selector 0x14）与图片剩余空间（selector 0x10）
async function probe(h, timeout) {
  const read = async (sel, min) => parseRead(
    await exchange(h.dev, readPacket(sel), (r) => r[0] === 0xa5 && r[1] === 0x41 && r[4] === sel, timeout),
    sel, min);
  const info = await read(0x14, 11);
  const space = (await read(0x10, 4)).readUInt32LE();
  return {
    vendorId: info.readUInt16LE(0),
    productId: info.readUInt16LE(2),
    width: info.readUInt16LE(4),
    height: info.readUInt16LE(6),
    supportJpg: info[10] === 1,
    imageSpace: space,
    info,
  };
}

// ── 推送（带退避重试 + 每次重试前重新探活） ───────────────────────
async function pushJpeg(jpeg, o) {
  let img;
  try {
    img = jpegInfo(jpeg);
  } catch (e) {
    // progressive / 非 8 位 / 非 RGB灰度 都属于「设备不接受这张图」，归到退出码 3
    e.exitCode = 3;
    throw e;
  }
  if (img.width !== W || img.height !== H) {
    const e = new Error('图片尺寸 ' + img.width + 'x' + img.height + ' 不是 ' + W + 'x' + H + '；设备不自动缩放');
    e.exitCode = 3;
    throw e;
  }
  const stream = buildStream(jpeg, img.width, img.height);
  const list = [...frames(stream)];
  const plan = { frames: list.length, acks: list.filter((f) => f.ack).length, bytes: stream.length };

  let lastErr = null;
  for (let attempt = 1; attempt <= Math.max(1, o.retries); attempt++) {
    let h = null;
    try {
      h = openDevice();
      const p = await probe(h, o.timeout);              // 先探活，再动手
      validateTarget(p.info, p.imageSpace, img, stream); // 尺寸/空间/身份校验
      if (h.fatal) throw h.fatal;
      const t0 = Date.now();
      const res = await transmit(h.dev, stream, o.timeout);
      const ms = Date.now() - t0;
      if (h.fatal) throw h.fatal;
      h.close();
      return { ...res, ...plan, pushMs: ms, attempts: attempt, space: p.imageSpace };
    } catch (e) {
      if (h) h.close();
      lastErr = e;
      // 尺寸/空间类错误重试没有意义，直接抛
      if (/尺寸|空间|身份|JPEG|不支持/.test(e.message) || e.exitCode === 3) break;
      if (attempt < o.retries) {
        const wait = 500 * Math.pow(2, attempt - 1); // 500 → 1000 → 2000 → 4000
        log(o, { event: 'retry', attempt, waitMs: wait, error: e.message });
        await new Promise((r) => setTimeout(r, wait));
      }
    }
  }
  const e = new Error('推送失败（已尝试 ' + o.retries + ' 次）: ' + (lastErr && lastErr.message));
  e.exitCode = lastErr && lastErr.exitCode === 3 ? 3 : 1;
  throw e;
}

// ── 输出 ──────────────────────────────────────────────────────────
function log(o, obj) {
  if (o.json) console.log(JSON.stringify(obj));
  else {
    const parts = Object.entries(obj).map(([k, v]) => k + '=' + v);
    console.log(parts.join('  '));
  }
}

function verbose(o, s) { if (!o.json) console.log(s); }

// ── 命令实现 ──────────────────────────────────────────────────────
async function cmdInfo(o) {
  const h = openDevice();
  try {
    const p = await probe(h, o.timeout);
    const out = {
      vendorId: p.vendorId, productId: p.productId, width: p.width, height: p.height,
      supportJpg: p.supportJpg, imageSpace: p.imageSpace, readOnly: true,
      system: process.platform, node: process.version,
    };
    if (o.json) console.log(JSON.stringify(out));
    else console.log(JSON.stringify(out, null, 2));
    return 0;
  } finally { h.close(); }
}

function planFor(jpeg) {
  const img = jpegInfo(jpeg);
  const stream = buildStream(jpeg, img.width, img.height);
  const list = [...frames(stream)];
  return {
    width: img.width, height: img.height, jpegBytes: jpeg.length,
    streamBytes: stream.length, frames: list.length,
    acks: list.filter((f) => f.ack).length, offline: true,
  };
}

async function cmdDry(o) {
  const t0 = Date.now();
  const media = startMedia(0);
  const state = withMedia(await sampleOnce(0), await waitMedia(media));
  media.stop();
  const msSample = Date.now() - t0;
  const { jpeg, ms: msRender } = renderJpeg(state, o);
  const plan = planFor(jpeg);
  log(o, {
    event: 'dry', kb: +(jpeg.length / 1024).toFixed(1), frames: plan.frames, acks: plan.acks,
    sampleMs: msSample, renderMs: msRender, pushed: false, displayVerified: false,
  });
  if (!o.json) {
    verbose(o, '  （dry：未打开 USB 设备，屏幕内容未改变）');
    verbose(o, '  图片字节 ' + jpeg.length + ' B，流 ' + plan.streamBytes + ' B，' + plan.width + 'x' + plan.height);
  }
  return 0;
}

async function cmdOnce(o) {
  const t0 = Date.now();
  const media = startMedia(0);
  const state = withMedia(await sampleOnce(0), await waitMedia(media));
  media.stop();
  const msSample = Date.now() - t0;
  const { jpeg, ms: msRender } = renderJpeg(state, o);
  const r = await pushJpeg(jpeg, o);
  log(o, {
    event: 'once', kb: +(jpeg.length / 1024).toFixed(1), frames: r.frames, acks: r.acks,
    sampleMs: msSample, renderMs: msRender, pushMs: r.pushMs, cycleMs: Date.now() - t0,
    attempts: r.attempts, displayVerified: r.displayVerified,
  });
  if (!o.json) verbose(o, '  ⚠ 原厂协议无最终 ACK，displayVerified=false —— 请肉眼看屏确认已更新。');
  return 0;
}

async function cmdPush(o) {
  if (!o.file) throw Object.assign(new Error('push 需要 <file.jpg>'), { exitCode: 2 });
  const jpeg = fs.readFileSync(o.file);
  const t0 = Date.now();
  const r = await pushJpeg(jpeg, o);
  log(o, {
    event: 'push', file: o.file, kb: +(jpeg.length / 1024).toFixed(1),
    frames: r.frames, acks: r.acks, pushMs: r.pushMs, cycleMs: Date.now() - t0,
    attempts: r.attempts, displayVerified: r.displayVerified,
  });
  if (!o.json) verbose(o, '  ⚠ 原厂协议无尾段 ACK，displayVerified=false —— 请肉眼看屏确认。');
  return 0;
}

async function cmdLoop(o) {
  // loop 常量：sampler 永远只 spawn 一次（pwsh 冷启动 6~8 秒）
  const interval = Math.max(1000, o.interval);
  const s = startSampler(Math.max(500, Math.min(interval, 3000)), true);
  verbose(o, '采样进程已启动（' + psExe() + '，每 ' + Math.max(500, Math.min(interval, 3000)) + 'ms），推送周期 ' + interval + 'ms');
  verbose(o, '⚠ 协议无序列号：同一时刻只允许一个上传者，请确认没有别的推送在跑。');

  const media = startMedia(2000);
  let stop = false;
  const bye = () => { stop = true; s.stop(); media.stop(); };
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);

  let round = 0;
  let ok = 0, fail = 0;
  while (!stop) {
    const cyc0 = Date.now();
    let sampleMs = 0, renderMs = 0, pushMs = 0, framesN = 0, acksN = 0, kb = 0;
    try {
      const ts = Date.now();
      const state = withMedia(await s.next(Math.max(20000, interval * 3)), media.value);
      sampleMs = Date.now() - ts;

      const tr = Date.now();
      const { jpeg, ms } = renderJpeg(state, o);
      renderMs = ms;
      kb = +(jpeg.length / 1024).toFixed(1);
      const r = await pushJpeg(jpeg, o);
      pushMs = r.pushMs; framesN = r.frames; acksN = r.acks;
      ok++;
      const cycleMs = Date.now() - cyc0;
      log(o, {
        event: 'loop', round: ++round, kb, frames: framesN, acks: acksN,
        sampleMs, renderMs, pushMs, cycleMs, attempts: r.attempts,
        displayVerified: r.displayVerified, ok, fail,
      });
    } catch (e) {
      fail++;
      log(o, { event: 'error', round: ++round, error: e.message, sampleMs, renderMs, pushMs, ok, fail });
      if (s.closed) {
        verbose(o, '采样进程已结束，停止 loop。');
        break;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    const rest = interval - (Date.now() - cyc0);
    if (rest > 0 && !stop) await new Promise((r) => setTimeout(r, rest));
    if (s.closed && !stop) { verbose(o, '采样进程已结束，停止 loop。'); break; }
  }
  s.stop();
  media.stop();
  verbose(o, '已停止。成功 ' + ok + ' 轮 / 失败 ' + fail + ' 轮。');
  if (!o.json) console.log('  ⚠ 全程 displayVerified=false —— 原厂协议无最终 ACK，请肉眼看屏确认。');
  return 0;
}

// ── 入口 ──────────────────────────────────────────────────────────
async function main(argv) {
  const o = parseArgs(argv);
  o_json_quiet = o.json; // --json 模式下不把 pwsh 的 stderr 混进 stdout
  if (!o.mode) {
    console.log([
      'node dashboard/push.js info                      只读：屏幕信息 + 剩余空间',
      'node dashboard/push.js dry  [--widgets ...]      只采样 + 渲染，不推送（零风险）',
      'node dashboard/push.js once [--widgets ...]      推一次',
      'node dashboard/push.js loop [--widgets ...] [--interval 3000] [--quality 72] [--limit 30000]',
      'node dashboard/push.js push <file.jpg>           推现成 JPEG',
      '',
      '公共选项：--preset --widgets --quality --limit --state-dir --json --retries',
      '注意：原厂协议无最终 ACK，displayVerified 恒为 false，推完请看屏确认。',
    ].join('\n'));
    return 0;
  }
  switch (o.mode) {
    case 'info': return cmdInfo(o);
    case 'dry': return cmdDry(o);
    case 'once': return cmdOnce(o);
    case 'loop': return cmdLoop(o);
    case 'push': return cmdPush(o);
    default:
      console.error('未知命令: ' + o.mode);
      return 2;
  }
}

if (require.main === module) {
  main(process.argv.slice(2))
    .then((code) => { process.exitCode = code; })
    .catch((e) => {
      console.error(o_json_quiet ? JSON.stringify({ event: 'fatal', error: e.message }) : ('错误: ' + e.message));
      process.exitCode = e.exitCode || 1;
    });
}

module.exports = { parseArgs, startSampler, sampleOnce, renderJpeg, pushJpeg, planFor, probe, findScreen, hidModule, main, startMedia, waitMedia, withMedia, ps51 };
