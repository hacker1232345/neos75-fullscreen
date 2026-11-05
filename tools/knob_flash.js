#!/usr/bin/env node
/**
 * knob_flash.js —— 键盘旋钮配置的读写器（0x2E3C:0xDF11）
 *
 * 四个子命令：
 *   backup  --out <file>                     只读：整片备份 0x1CE40
 *   read    --fw <file>                      离线：反解六个动作位当前取值
 *   build   --fw <base> --plan <json> --out <file>
 *                                            离线：在 base 上改写六个动作位
 *   flash   --target <file> --yes            写入：擦除 → 写 → 读回逐字节比对 → 固化
 *   apply   --fw <base> --plan <json> --yes  一条龙：build + flash
 *
 * 为什么 base 用「设备自己的备份」而不是随包发的原厂固件：
 *   厂商固件不随仓库分发。而且从设备实读的镜像生成，天然适配这台机器上的
 *   任何历史版本 —— 只要六个动作位那几处字节在预期位置，改完必然自洽。
 *   若某个方向之前被改成动作、现在想退回翻页，生成器会自己写一段等效的
 *   翻页代码（movs r1,#N / movs r0,#0x39 / bl），不依赖原厂那几行。
 *
 * 安全纪律（与屏幕刷写器一致）：
 *   1. 先备份再动 flash；备份落盘成功之前不擦除
 *   2. 写入后逐字节读回比对，不一致立刻退出，不发 manifest
 *   3. 向量表 / bootmagic 扇区 / 参数区一字节不碰（生成器侧硬校验）
 *   4. 目标文件的 DFU 后缀 CRC 必须自洽
 */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const cp = require('child_process');
const kb = require('./kb_dfu');

const argv = process.argv.slice(2);
const arg = (n, d = null) => {
  const i = argv.indexOf('--' + n);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : d;
};
const has = (n) => argv.includes('--' + n);
const die = (code, msg) => { kb.log('✘ ' + msg); process.exit(code); };

const SITES = ['vol-cw', 'vol-ccw', 'vol-press', 'scr-cw', 'scr-ccw', 'scr-press'];

function pythonBin() {
  return process.env.NEOS75_PYTHON || 'python';
}

/** 调 knob_config.py 生成目标固件（离线，不发 USB）。 */
function build(base, plan, out) {
  kb.log('──── 离线生成（一个 USB 请求都不发）────');
  const script = path.join(__dirname, 'knob_config.py');
  const args = [script, '--fw', base, '--out', out];
  for (const s of SITES) {
    const v = plan[s];
    if (v) args.push('--' + s, String(v));
  }
  const r = cp.spawnSync(pythonBin(), args, { encoding: 'utf8', timeout: 120000 });
  if (r.error) die(2, '跑不动 knob_config.py：' + r.error.message);
  for (const line of ((r.stdout || '') + (r.stderr || '')).trim().split('\n')) {
    if (line.trim()) kb.log('  ' + line);
  }
  if (r.status !== 0) die(2, '生成失败（退出码 ' + r.status + '）');
  if (!fs.existsSync(out)) die(2, '生成器没写出目标文件');
  return out;
}

/** 反解六个动作位（离线）。 */
function readConfig(fw) {
  const script = path.join(__dirname, 'knob_config.py');
  const r = cp.spawnSync(pythonBin(), [script, '--read', fw], { encoding: 'utf8', timeout: 60000 });
  if (r.error) die(2, '跑不动 knob_config.py：' + r.error.message);
  const lines = (r.stdout || '').trim().split('\n').filter(Boolean);
  const out = {};
  for (const line of lines) {
    const m = line.trim().match(/^(vol-cw|vol-ccw|vol-press|scr-cw|scr-ccw|scr-press)\s+(.+)$/);
    if (m) out[m[1]] = null;
  }
  // 中文标签不好解析，直接再调一次 JSON 接口
  const py = process.env.NEOS75_PYTHON || 'python';
  const j = cp.spawnSync(py, ['-c',
    'import sys,json;sys.path.insert(0,r"' + path.join(__dirname).replace(/\\/g, '\\\\') + '");'
    + 'import knob_config as k;print(json.dumps(k.read_config(sys.argv[1]),ensure_ascii=False))',
    fw], { encoding: 'utf8', timeout: 60000 });
  if (j.status === 0 && j.stdout) {
    try { return JSON.parse(j.stdout.trim()); } catch { }
  }
  return out;
}

/** 目标固件基本形状校验：长度、DFU 后缀、CRC、向量表。 */
function preflight(target) {
  const fw = fs.readFileSync(target);
  kb.log(`  目标 ${path.basename(target)}  ${fw.length} B  md5 ${kb.md5(fw)}`);
  if (fw.length !== kb.FULL) die(8, `长度 ${fw.length}，应为 ${kb.FULL}`);
  const suf = fw.subarray(fw.length - 16);
  const bcd = suf[0] | (suf[1] << 8);
  if (bcd !== 0xffff) die(8, `bcdDevice=${kb.hx(bcd, 4)} 不是 0xFFFF`);
  if (!(suf[8] === 0x55 && suf[9] === 0x46 && suf[10] === 0x44 && suf[11] === 0x10)) {
    die(8, `DFU 签名错：${suf.subarray(8, 12).toString('hex')}`);
  }
  const idP = suf[2] | (suf[3] << 8), idV = suf[4] | (suf[5] << 8);
  if (idV !== kb.DFU_VID || idP !== kb.DFU_PID) {
    die(8, `VID:PID = ${kb.hx(idV, 4)}:${kb.hx(idP, 4)}，不是键盘固件`);
  }
  const dwCRC = suf.readUInt32LE(12);
  const calc = kb.crc32(fw.subarray(0, fw.length - 4));
  if (dwCRC !== ((~calc) >>> 0)) {
    die(8, `CRC 不符：后缀 ${kb.hx(dwCRC)}，期望 ${kb.hx((~calc) >>> 0)}`);
  }
  kb.log(`  ✓ 长度 / 签名 / VID:PID / CRC 全部通过（crc32 ${kb.hx(calc)}）`);
  const sp = fw.readUInt32LE(0), rv = fw.readUInt32LE(4);
  if (!(sp >= 0x20000000 && sp < 0x20040000)) die(8, `栈顶 ${kb.hx(sp)} 不在 RAM`);
  if (!(rv >= kb.BASE && rv < kb.BASE + 0x40000)) die(8, `复位向量 ${kb.hx(rv)} 不在代码区`);
  kb.log(`  ✓ 向量表 SP=${kb.hx(sp)} Reset=${kb.hx(rv)}`);
  return fw;
}

async function doBackup(out) {
  if (!out) die(5, 'backup 需要 --out');
  kb.log('──── 接管 DFU ────');
  const d = await kb.takeOver();
  kb.log(`──── 整片备份 ${kb.hx(kb.FULL)} B（64 B 步进）────`);
  const before = await d.readRange(kb.BASE, kb.FULL, (off, len, sec) => {
    if (off % 0x4000 === 0) kb.log(`  …${kb.hx(off, 6)}/${kb.hx(len, 6)}  ${sec.toFixed(0)}s`);
  });
  fs.writeFileSync(out, before);
  kb.log(`  ✅ 备份 → ${out}  md5 ${kb.md5(before)}`);
  const sp = before.readUInt32LE(0), rv = before.readUInt32LE(4);
  kb.log(`  向量表 SP=${kb.hx(sp)} Reset=${kb.hx(rv)}`);
  await d.leave();
  await d.close();
  return { out, md5: kb.md5(before) };
}

async function doFlash(target) {
  const fw = preflight(target);
  const payload = fw.subarray(0, fw.length - 16);

  kb.log('──── 接管 DFU ────');
  const d = await kb.takeOver();

  kb.log(`──── 整片备份 ${kb.hx(kb.FULL)} B ────`);
  const before = await d.readRange(kb.BASE, kb.FULL, (off, len, sec) => {
    if (off % 0x4000 === 0) kb.log(`  …${kb.hx(off, 6)}/${kb.hx(len, 6)}  ${sec.toFixed(0)}s`);
  });
  const snap = `kb_before_${Date.now()}.bin`;
  fs.writeFileSync(snap, before);
  kb.log(`  ✅ 备份 → ${snap}  md5 ${kb.md5(before)}`);

  let same = 0;
  for (let i = 0; i < payload.length; i++) if (payload[i] === before[i]) same++;
  kb.log(`  与目标对比：相同 ${same} B，差 ${payload.length - same} B`);
  if (same === payload.length) {
    kb.log('  目标与现场完全一致，什么都不用做');
    await d.leave();
    await d.close();
    return { backup: snap, changed: 0, unchanged: true };
  }

  if (!has('yes')) {
    kb.log('──── 没有 --yes，只备份到此为止 ────');
    await d.leave();
    await d.close();
    return { backup: snap, changed: payload.length - same, unchanged: false, dryRun: true };
  }

  kb.log('──── 擦除 ────');
  const sectors = Math.ceil(payload.length / kb.SECTOR);
  const t0 = Date.now();
  for (let i = 0; i < sectors; i++) {
    const addr = kb.BASE + i * kb.SECTOR;
    await d.toIdle();
    const b = new Uint8Array(5); b[0] = 0x41;
    b[1] = addr & 0xff; b[2] = (addr >>> 8) & 0xff;
    b[3] = (addr >>> 16) & 0xff; b[4] = (addr >>> 24) & 0xff;
    await d.dnload(b, 0);
    const s = await d.settle();
    if (s.status !== kb.ST_OK) die(7, `擦除 ${kb.hx(addr)} 失败 status=${s.status}`);
    if (i % 8 === 0 || i === sectors - 1) kb.log(`  扇区 ${i + 1}/${sectors} ${kb.hx(addr)} ✓`);
  }
  kb.log(`  ✅ 擦除 ${((Date.now() - t0) / 1000).toFixed(2)}s`);

  kb.log(`──── 写入 ${payload.length} B ────`);
  const t1 = Date.now();
  for (let off = 0; off < payload.length; off += kb.SECTOR) {
    const chunk = payload.subarray(off, Math.min(off + kb.SECTOR, payload.length));
    await d.toIdle();
    const sa = await d.setAddr(kb.BASE + off);
    if (sa.status !== kb.ST_OK) die(7, `SET_ADDRESS ${kb.hx(kb.BASE + off)} 失败`);
    await d.dnload(new Uint8Array(chunk), 2);
    const s = await d.settle();
    if (s.status !== kb.ST_OK) die(7, `写入 ${kb.hx(kb.BASE + off)} 失败 status=${s.status}`);
  }
  kb.log(`  ✅ 写入 ${((Date.now() - t1) / 1000).toFixed(2)}s`);

  kb.log('──── 读回比对 ────');
  const after = await d.readRange(kb.BASE, kb.FULL);
  let bad = 0, first = -1;
  for (let i = 0; i < payload.length; i++) {
    if (after[i] !== payload[i]) { bad++; if (first < 0) first = i; }
  }
  if (bad) {
    kb.log(`    读回 ${after.subarray(first, first + 16).toString('hex')}`);
    kb.log(`    期望 ${payload.subarray(first, first + 16).toString('hex')}`);
    die(9, `读回差异 ${bad} 字节（首个 @${kb.hx(first, 6)}），不固化`);
  }
  kb.log('  ✅✅ 逐字节一致');
  fs.writeFileSync(`kb_after_${Date.now()}.bin`, after);

  kb.log('──── manifest 固化 + 等回应用模式 ────');
  await d.leave();
  // 关键顺序：必须先关掉 DFU 句柄再等应用模式。
  // Windows 在句柄仍打开时不会重新枚举同一个物理设备，设备其实已经重启回
  // 应用模式，只是我们一直握着旧句柄，findApp() 永远看不见它。
  await d.close();
  const back = await d.waitApp();
  if (back >= 0) kb.log(`  ✅ 键盘恢复（${back.toFixed(1)}s）`);
  else kb.log('  ⚠ 90 秒内没回到应用模式 —— 拔插 USB 线，或按 fn+pts 再试');
  return { backup: snap, changed: payload.length - same, flashed: true };
}

(async () => {
  const cmd = argv[0];
  if (cmd === 'read') {
    const fw = arg('fw');
    if (!fw) die(5, 'read 需要 --fw');
    kb.log(JSON.stringify(readConfig(fw), null, 2));
    return;
  }
  if (cmd === 'build') {
    const base = arg('fw'), plan = arg('plan'), out = arg('out');
    if (!base || !plan || !out) die(5, 'build 需要 --fw --plan --out');
    build(base, JSON.parse(fs.readFileSync(plan, 'utf8')), out);
    return;
  }
  if (cmd === 'backup') {
    kb.log(JSON.stringify(await doBackup(arg('out'))));
    return;
  }
  if (cmd === 'flash') {
    const target = arg('target');
    if (!target) die(5, 'flash 需要 --target');
    kb.log(JSON.stringify(await doFlash(target)));
    return;
  }
  if (cmd === 'apply') {
    const base = arg('fw'), plan = arg('plan');
    if (!base || !plan) die(5, 'apply 需要 --fw --plan');
    const out = arg('out') || path.join(path.dirname(plan), 'knob-target.bin');
    build(base, JSON.parse(fs.readFileSync(plan, 'utf8')), out);
    kb.log(JSON.stringify(await doFlash(out)));
    return;
  }
  die(5, '子命令：backup | read | build | flash | apply');
})().catch((e) => {
  kb.log('✘ ' + (e && e.message));
  process.exit(1);
});
