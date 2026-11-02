'use strict';
// dashboard/selftest.js —— 纯离线自测。绝不打开 USB 设备。
//
// 覆盖：
//   1. widgets.json 里每个 id 都在 render.py 的实现里存在（跑 render.py --list 交叉校验）
//   2. 每个预设都能生成合法 480x360 baseline JPEG
//   3. 未注册的 widget id 会被拒绝（退出码 3）
//   4. 缺字段 / 空 / null 的 state 不会崩
//   5. 帧数与应答数与协议一致（用构造的 JPEG 校验，复用 screen_wallpaper.js）
//
// 用法：node dashboard/selftest.js
// 成功：console.log('PASS ...') 且 exit 0；失败 exit 非 0。

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const HERE = __dirname;
const REPO = path.resolve(HERE, '..');
const PY = process.env.NEOS75_PYTHON || 'python';
const RENDER = path.join(HERE, 'render.py');
// 打包版由桌面程序通过 NEOS75_RENDER 指向随包的独立渲染器（它就是 render.py
// 冻结出来的，命令行一致），这样自检不依赖目标机器上的 Python/Pillow。
const RENDER_EXE = process.env.NEOS75_RENDER && fs.existsSync(process.env.NEOS75_RENDER)
  ? process.env.NEOS75_RENDER : null;
const W = 480, H = 360;

const t = require('../wallpaper/screen_wallpaper.js');

let groups = 0;
const ok = () => groups++;

function runRender(args, input) {
  const cmd = RENDER_EXE || PY;
  const argv = RENDER_EXE ? args : [RENDER, ...args];
  return spawnSync(cmd, argv, {
    input, encoding: 'utf8', windowsHide: true, timeout: 120000,
    cwd: REPO,
  });
}

// 解析 JPEG 的 SOF 段；返回 {marker, precision, height, width, components}
function sofInfo(buf) {
  if (buf.length < 4 || buf.readUInt16BE(0) !== 0xffd8) throw new Error('不是 JPEG');
  let p = 2, out = null;
  while (p < buf.length) {
    if (buf[p++] !== 255) throw new Error('JPEG 标记损坏');
    while (buf[p] === 255) p++;
    const m = buf[p++];
    if (m === 0xd9) break;
    const n = buf.readUInt16BE(p);
    if (m === 0xc0 || m === 0xc1 || m === 0xc2) {
      out = {
        marker: m, precision: buf[p + 2],
        height: buf.readUInt16BE(p + 3), width: buf.readUInt16BE(p + 5),
        components: buf[p + 7],
      };
      break;
    }
    if (m === 0xda) break;
    p += n;
  }
  if (!out) throw new Error('JPEG 缺少 SOF 段');
  return out;
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'neos75-selftest-'));

const STATE = {
  clock: '14:23:07', date: '2026-11-03', uptime: '3d 04:12',
  cpu_load: 37.5, cpu_freq_ghz: 4.21, mem_pct: 61.2, mem_used_gb: 19.6, mem_total_gb: 32.0,
  temp_c: 58.0, gpu_pct: 12, disk_free_gb: 412.7, net_down_mbps: 3.21, net_up_mbps: 0.44,
  bat_pct: 82.0, bat_charging: true, bat_ac: true, vol_pct: 45,
  top_proc: 'chrome.exe', top_proc_pct: 12.3, ip: '192.168.1.42', host: 'DESKTOP-NEOS75',
};

(async () => {
  // ── 1. 注册表交叉校验 ────────────────────────────────────────
  const wj = JSON.parse(fs.readFileSync(path.join(HERE, 'widgets.json'), 'utf8'));
  assert(Array.isArray(wj), 'widgets.json 顶层必须是数组');
  assert(wj.length >= 15, 'widgets.json 至少要有 15 个部件，实际 ' + wj.length);

  const ids = wj.map((w) => w.id);
  assert.equal(new Set(ids).size, ids.length, 'widgets.json 里有重复 id');
  for (const need of ['id', 'label', 'desc', 'group', 'default', 'minWidth', 'minHeight', 'draw']) {
    for (const w of wj) assert(Object.prototype.hasOwnProperty.call(w, need), `部件 ${w.id} 缺字段 ${need}`);
  }
  const REQUIRED = ['clock_big', 'date', 'cpu', 'mem', 'gpu', 'temp', 'disk', 'net', 'battery',
    'uptime', 'history_cpu', 'top_proc', 'volume', 'host', 'ip'];
  for (const r of REQUIRED) assert(ids.includes(r), '缺必需部件 id: ' + r);
  ok();

  const listRun = runRender(['--list']);
  assert.equal(listRun.status, 0, 'render.py --list 失败: ' + (listRun.stderr || '').slice(0, 400));
  const reg = JSON.parse(listRun.stdout);
  assert(Array.isArray(reg.widgets), 'render.py --list 缺少 widgets 数组');
  const impl = new Map(reg.widgets.map((w) => [w.id, w]));
  for (const w of wj) {
    const r = impl.get(w.id);
    assert(r, 'render.py 的实现里没有部件 ' + w.id);
    assert.equal(r.implemented, true, '部件 ' + w.id + ' 的 draw 函数未实现: ' + r.draw);
    assert.equal(r.draw, w.draw, '部件 ' + w.id + ' 的 draw 名两边不一致');
    assert.equal(r.minWidth, w.minWidth, '部件 ' + w.id + ' 的 minWidth 两边不一致');
    assert.equal(r.minHeight, w.minHeight, '部件 ' + w.id + ' 的 minHeight 两边不一致');
  }
  assert.equal(reg.widgets.length, wj.length, '两边部件数量不一致');
  ok();

  // 预设列表也要能从 render.py 拿到
  assert(reg.presets && typeof reg.presets === 'object', 'render.py --list 缺少 presets');
  for (const p of ['default', 'minimal', 'full', 'performance', 'network']) {
    assert(Array.isArray(reg.presets[p]), 'render.py 缺预设 ' + p);
  }
  ok();

  // ── 2. 每个预设都能生成合法 480x360 baseline JPEG ────────────
  const stateFile = path.join(tmp, 'state.json');
  fs.writeFileSync(stateFile, JSON.stringify(STATE), 'utf8');

  for (const preset of Object.keys(reg.presets)) {
    const out = path.join(tmp, `o_${preset}.jpg`);
    const prev = path.join(tmp, `p_${preset}.png`);
    const r = runRender(['--in', stateFile, '--out', out, '--preset', preset,
      '--quality', '72', '--limit', '30000', '--preview', prev,
      '--state-dir', path.join(tmp, 'sd')]);
    assert.equal(r.status, 0, `预设 ${preset} 渲染失败: ` + (r.stderr || '').slice(0, 400));
    const buf = fs.readFileSync(out);
    const si = sofInfo(buf);
    assert.equal(si.width, W, `预设 ${preset} 宽度 ${si.width} != ${W}`);
    assert.equal(si.height, H, `预设 ${preset} 高度 ${si.height} != ${H}`);
    assert.equal(si.marker, 0xc0, `预设 ${preset} 不是 baseline（SOF 标记 0x${si.marker.toString(16)}）`);
    assert.equal(si.precision, 8, `预设 ${preset} 不是 8 位精度`);
    assert(buf.length <= 30000, `预设 ${preset} 超出 --limit: ${buf.length} B`);
    assert(buf[buf.length - 2] === 0xff && buf[buf.length - 1] === 0xd9, `预设 ${preset} 缺 EOI`);
    // 顺便用官方 jpegInfo 再验一次（这就是设备侧会跑的那段逻辑）
    assert.deepEqual(t.jpegInfo(buf), { width: W, height: H });
  }
  ok();

  // 每个部件单独渲染一次，确保没有哪个 draw 会炸
  for (const w of wj) {
    const out = path.join(tmp, 'one.jpg');
    const r = runRender(['--in', stateFile, '--out', out, '--widgets', w.id, '--state-dir', path.join(tmp, 'sd')]);
    assert.equal(r.status, 0, `单部件 ${w.id} 渲染失败: ` + (r.stderr || '').slice(0, 300));
    assert.deepEqual(t.jpegInfo(fs.readFileSync(out)), { width: W, height: H });
  }
  ok();

  // ── 3. 未注册 widget 被拒绝 ──────────────────────────────────
  for (const bad of ['nosuch', 'cpu,nosuch', 'CPU']) { // 大小写敏感
    const r = runRender(['--in', stateFile, '--out', path.join(tmp, 'bad.jpg'), '--widgets', bad]);
    assert.equal(r.status, 3, `未注册 widget "${bad}" 应以退出码 3 拒绝，实际 ${r.status}`);
    assert(/未注册的部件 id/.test(r.stderr || ''), '未注册 widget 的报错文本不符: ' + r.stderr);
  }
  ok();

  // ── 4. 缺字段 / 空 / null 的 state 不崩 ──────────────────────
  const badStates = [
    ['空对象', {}],
    ['全部 null', Object.fromEntries(Object.keys(STATE).map((k) => [k, null]))],
    ['只有零星字段', { cpu_load: 3 }],
    ['类型错乱', { clock: 12345, cpu_load: 'abc', bat_charging: 'yes', uptime: [], gpu_pct: {}, mem_pct: NaN }],
    ['超长文本', { host: 'X'.repeat(500), top_proc: 'Y'.repeat(500), ip: '1.2.3.4'.repeat(50) }],
    ['越界数值', { cpu_load: 9999, mem_pct: -50, bat_pct: 12345, gpu_pct: 1e9, temp_c: -999 }],
  ];
  for (const [name, st] of badStates) {
    const out = path.join(tmp, 'bad_state.jpg');
    const r = runRender(['--in', '-', '--out', out, '--preset', 'full', '--state-dir', path.join(tmp, 'sd')],
      JSON.stringify(st));
    assert.equal(r.status, 0, `state「${name}」导致渲染失败: ` + (r.stderr || '').slice(0, 400));
    assert.deepEqual(t.jpegInfo(fs.readFileSync(out)), { width: W, height: H }, `state「${name}」输出尺寸不对`);
  }
  // stdin 非 JSON → 退出码 2，且不写半张图
  const badJson = runRender(['--in', '-', '--out', path.join(tmp, 'never.jpg')], '{not json');
  assert.equal(badJson.status, 2, '非法 JSON 应以退出码 2 拒绝，实际 ' + badJson.status);
  assert(!fs.existsSync(path.join(tmp, 'never.jpg')), '非法 JSON 不应产生输出文件');
  ok();

  // ── 5. 帧数 / 应答数与协议一致 ───────────────────────────────
  // 用真实渲染出来的 JPEG 走完整链路：jpegInfo → buildStream → frames
  const realJpeg = fs.readFileSync(path.join(tmp, 'o_default.jpg'));
  const img = t.jpegInfo(realJpeg);
  const stream = t.buildStream(realJpeg, img.width, img.height);
  assert.equal(stream.length, realJpeg.length + 13, '13 字节流头长度不符');
  assert.equal(stream.toString('ascii', 0, 2), 'UB');
  assert.equal(stream.readUInt16BE(2), W, '流头宽');
  assert.equal(stream.readUInt16BE(4), H, '流头高');
  assert.equal(stream.readUInt16BE(6), 1, '流头帧数应为 1');
  assert.equal(stream[8], 0, '流头 delay 应为 0');
  assert.equal(stream.readUInt32BE(9), realJpeg.length, '流头 JPEG 长度');
  ok();

  const list = [...t.frames(stream)];
  const head = list[0];
  assert.equal(head.kind, 'header');
  assert.equal(head.ack, true, '首帧必须等应答');
  assert.equal(head.report.length, 65, 'report 必须 65 字节');
  assert.equal(head.report[0], 0, 'reportId 0 必须在前');
  assert.equal(head.report[1], 0xba);
  // 首帧负载：[0x06, 0xAA, 0xB0, u32BE 流总长]
  // 叠上 report 前缀 [0x00, 0xBA] 后，整帧就是 [0xBA, 0x06, 0xAA, 0xB0, u32BE]
  assert.equal(head.report[2], 0x06);
  assert.equal(head.report[3], 0xaa);
  assert.equal(head.report[4], 0xb0);
  assert.equal(head.report.readUInt32BE(5), stream.length, '首帧里的流总长不符');
  assert(head.report.subarray(9).every((x) => x === 0), '首帧尾部应补 0');
  ok();

  for (const f of list) {
    assert.equal(f.report.length, 65, '每帧 report 必须 65 字节');
    assert.equal(f.report[0], 0, 'reportId 必须为 0');
    assert.equal(f.report[1], 0xba, '命令必须为 0xba');
    if (f.kind !== 'header') {
      const n = f.report[2];
      assert(n >= 1 && n <= 62, '数据帧长度 n 必须在 1..62，实际 ' + n);
    }
  }

  // 帧数公式与 frames() 的实现一致：
  //   k = floor(len/4096) 个完整块，每块 ceil(4096/62)=67 帧；
  //   余数 rem=len%4096，若 rem>0 再加 ceil(rem/62) 帧。
  const k = Math.floor(stream.length / 4096);
  const rem = stream.length % 4096;
  const PER_BLOCK = Math.ceil(4096 / 62);
  const tailFrames = rem > 0 ? Math.ceil(rem / 62) : 0;
  const wantFrames = 1 + k * PER_BLOCK + tailFrames;
  const acks = list.filter((f) => f.ack);
  assert.equal(list.length, wantFrames,
    `总帧数应为 1 + ${k}*${PER_BLOCK} + ${tailFrames} = ${wantFrames}，实际 ${list.length}`);
  assert.equal(acks.length, 1 + k,
    `应答数应为 1(头) + ${k}(完整块) = ${1 + k}，实际 ${acks.length}`);
  assert(acks.length > 1, '样本太小，没有覆盖到需要应答的完整块');

  // 每个完整块只有最后一帧带 u32LE CRC32，且必须等应答
  for (let bi = 0; bi < k; bi++) {
    const start = bi * 4096, end = start + 4096;
    const firstIdx = 1 + bi * PER_BLOCK;
    const lastIdx = firstIdx + PER_BLOCK - 1;
    const want = t.crc32(stream.subarray(start, end));
    // 块内除最后一帧外，都不带 CRC、都不等应答
    for (let i = firstIdx; i < lastIdx; i++) {
      assert.equal(list[i].ack, false, `第 ${bi} 块的第 ${i - firstIdx} 帧不该等应答`);
      const n = list[i].report[2];
      assert.equal(n, 62, `块内非末帧的长度应为 62，实际 ${n}`);
    }
    const f = list[lastIdx];
    assert.equal(f.kind, 'block-end', `第 ${bi} 块末帧 kind 应为 block-end`);
    assert.equal(f.ack, true, `第 ${bi} 块末帧必须等应答`);
    const n = f.report[2];
    assert(n >= 1 && n <= 62);
    const crcField = f.report.subarray(3 + n, 7 + n);
    assert.equal(crcField.readUInt32LE(), want, `第 ${bi} 块的 CRC32 不符`);
    // 末帧之后的字节必须是 0 填充
    assert(f.report.subarray(7 + n).every((x) => x === 0), `第 ${bi} 块末帧之后应补 0`);
  }
  ok();

  // 从 report 里还原原始流，必须逐字节等于原文
  const recovered = Buffer.concat(list.slice(1).map((f) => {
    const n = f.report[2];
    return f.report.subarray(3, 3 + n);
  }));
  assert.deepEqual(recovered, stream, '从 report 还原的流与原文不一致');
  ok();

  // CRC32 标准向量
  assert.equal(t.crc32(Buffer.from('123456789')), 0xcbf43926, 'CRC32 标准向量不符');
  ok();

  // 尺寸不符必须被拒（设备不自动缩放）
  const wrong = { width: 320, height: 240 };
  assert.throws(() => t.validateTarget(Buffer.from('4255010040014001000001', 'hex'), 1e6, wrong, Buffer.alloc(10)),
    /尺寸/);
  // progressive JPEG 必须被拒
  const prog = Buffer.from(realJpeg);
  const scan = prog.indexOf(Buffer.from([0xff, 0xc0]));
  assert(scan > 0, '找不到 SOF0');
  prog[scan + 1] = 0xc2;
  assert.throws(() => t.jpegInfo(prog), /baseline/);
  ok();

  fs.rmSync(tmp, { recursive: true, force: true });
  console.log(`PASS ${groups} groups: registry cross-check / presets / all-widgets / unknown-id / degenerate-state / user-units / protocol frames+acks+CRC / validation`);
})().catch((e) => {
  console.error(e);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* 清理失败不影响结论 */ }
  process.exitCode = 1;
});
