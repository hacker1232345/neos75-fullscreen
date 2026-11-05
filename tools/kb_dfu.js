/**
 * 键盘 DFU 传输层（0x2E3C:0xDF11，基址 0x08000000，载荷 118320 B）。
 *
 * 与屏幕（0x5542:0x1000，基址 0x8000）的区别：
 *   · 键盘 bootloader 按【实际数据长度】推进内部指针，允许一块连续写多段；
 *     屏幕按 wTransferSize 推进，必须每块独立 SET_ADDRESS。
 *   · 键盘单次 UPLOAD 用 64 B 步进最稳（大 wLength 读只有前 64 B 是真值）。
 *   · 扇区 0x1000，共 29 个（0x08000000..0x0801D000）。
 *   · 参数区 0x0801F800..0x08020000 在擦除终点之后，刷固件不会清用户设置。
 *
 * 该系列 bootloader 任意一次控制传输都可能 stall，重试即过；写入后立刻读会
 * 持续 stall，需要约 1.5 秒冷却。
 *
 * 本模块只导出传输原语，擦写范围、备份与门控由 knob_flash.js 负责。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const KB_VID = 0x1ea7, KB_PID = 0x4884;
const DFU_VID = 0x2e3c, DFU_PID = 0xdf11;
const BASE = 0x08000000;
const STEP = 64;              // 读步进
const SECTOR = 0x1000;
const FULL = 0x1ce40;         // 118336，含 16 B DFU 后缀
const PAYLOAD = FULL - 16;

const DNLOAD = 1, UPLOAD = 2, GETSTATUS = 3, CLRSTATUS = 4, GETSTATE = 5, ABORT = 6;
const ST_OK = 0, ST_ERROR = 10;
const IDLE = 2;

const hx = (n, w = 8) => '0x' + (n >>> 0).toString(16).toUpperCase().padStart(w, '0');
const ts = () => new Date().toISOString().slice(11, 23);
const log = (...a) => console.log(`[${ts()}]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const md5 = (b) => crypto.createHash('md5').update(b).digest('hex');
const STALL = /stalled|cancelled|canceled|borrowed mutably|timed out|getString|failed to open/i;

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i];
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return (c ^ 0xffffffff) >>> 0;
}

let RAW = null;
function raw() { if (!RAW) { require('usb'); RAW = require('usb/index.js'); } return RAW; }

async function attempt(fn, tries = 6, gap = 25) {
  let last;
  for (let i = 0; i < tries; i++) {
    try { return await fn(); } catch (e) {
      last = e;
      if (!STALL.test(e.message)) throw e;
      await sleep(gap);
    }
  }
  throw new Error(`重试 ${tries} 次仍失败：${last && last.message}`);
}

async function listDfu() {
  const l = await attempt(() => raw().nativeGetDevices(), 4, 20).catch(() => []);
  for (const d of l) {
    try {
      const v = await attempt(() => d.vendorId, 3, 10);
      const p = await attempt(() => d.productId, 3, 10);
      if (v === DFU_VID && p === DFU_PID) return d;
    } catch { }
  }
  return null;
}

function findApp() {
  const HID = require('node-hid');
  return HID.devices().find(
    (d) => d.vendorId === KB_VID && d.productId === KB_PID
      && d.usagePage === 0xff60 && d.usage === 0x61 && d.path);
}

async function kick() {
  const app = findApp();
  if (!app) return false;
  log('  键盘在应用模式，发 Enter-DFU');
  const HID = require('node-hid');
  const h = new HID.HID(app.path);
  h.on('error', () => { });
  const send = (b) => {
    const p = Buffer.alloc(64, 0);
    Buffer.from(b).copy(p);
    try { h.write(Buffer.concat([Buffer.from([0]), p])); } catch { }
  };
  send([0x07, 0x03, 0x01, 0x20]);
  await sleep(500);
  send([0x13, 0x02, 0x00, 0x00, 0x00, 0x00, 0x30, 0xd2]);
  await sleep(300);
  try { h.close(); } catch { }
  return true;
}

/**
 * 接管 DFU。返回 { dev, intf, cfg, state, close, abort, setAddr, toIdle, status,
 *             upload, dnload, readRange, waitApp, leave }。
 *
 * 「设备在总线上」不等于「有可用的 DFU」：上一轮 manifest 离开后句柄可能还没退干净，
 * 这时 open + claim 都能成功，但第一发 GETSTATUS 直接 stall。所以必须做健康检查，
 * 失败就丢弃句柄重新 kick，最多 3 轮。
 */
async function takeOver({ quiet = false } = {}) {
  let dev = null, intf = null, cfgVal = 1;

  for (let round = 1; round <= 3 && !dev; round++) {
    if (round > 1) { if (!quiet) log(`  第 ${round} 轮（丢弃旧句柄后重试）`); await sleep(1500); }

    let cand = await listDfu();
    if (!cand) {
      if (!(await kick())) {
        throw new Error('键盘既不在应用模式(1EA7:4884)也不在 DFU(2E3C:DF11)。'
          + '请接 USB 线并切到有线模式，或按 fn+pts 进 DFU。');
      }
      const t0 = Date.now();
      while (Date.now() - t0 < 40000) { cand = await listDfu(); if (cand) break; await sleep(50); }
      if (!cand) { if (!quiet) log('  40 秒内没等到 DFU'); continue; }
      if (!quiet) log(`  DFU 出现，用时 ${((Date.now() - t0) / 1000).toFixed(2)}s`);
    }

    try {
      await attempt(() => { cand.open(); return true; });
      const nCfg = await attempt(() => cand.configurations.length);
      let found = null, cv = 1;
      for (let c = 0; c < nCfg && found === null; c++) {
        cv = await attempt(() => cand.configurations[c].configurationValue);
        const n = await attempt(() => cand.configurations[c].interfaces.length);
        for (let i = 0; i < n; i++) {
          const na = await attempt(() => cand.configurations[c].interfaces[i].alternates.length);
          for (let j = 0; j < na; j++) {
            const a = await attempt(() => {
              const x = cand.configurations[c].interfaces[i].alternates[j];
              return { cls: x.interfaceClass, sub: x.interfaceSubclass, proto: x.interfaceProtocol };
            });
            if (a.cls === 0xfe && a.sub === 0x01 && (a.proto === 1 || a.proto === 2)) {
              found = await attempt(() => cand.configurations[c].interfaces[i].interfaceNumber);
            }
          }
        }
      }
      if (found === null) throw new Error('没有 DFU 接口');
      await attempt(() => cand.selectConfiguration(cv));
      await attempt(() => cand.claimInterface(found));

      const dv = await attempt(() => cand.controlTransferIn(
        { requestType: 'class', recipient: 'interface', request: GETSTATUS, value: 0, index: found }, 6), 2, 10);
      const d = dv && dv.data;
      if (!d || d.byteLength < 6) throw new Error(`健康检查只回 ${d ? d.byteLength : 0} B`);
      const view = new DataView(d.buffer, d.byteOffset, d.byteLength);
      const stat = view.getUint8(0), state = view.getUint8(4);
      if (!quiet) log(`  健康检查通过：status=${stat} state=${state}`);
      dev = cand; intf = found; cfgVal = cv;
      if (state !== IDLE) {
        try {
          await attempt(() => cand.controlTransferOut(
            { requestType: 'class', recipient: 'interface', request: ABORT, value: 0, index: found },
            new Uint8Array(0)), 2, 10);
        } catch { }
      }
    } catch (e) {
      if (!quiet) log(`  接管失败：${e.message} → 丢弃句柄`);
      try { cand.close(); } catch { }
      try { cand.reset(); } catch { }
      dev = null;
      await sleep(800);
    }
  }
  if (!dev) throw new Error('三轮接管都失败。请拔插 USB 线，或按 fn+pts 手动进 DFU 后重试。');

  const bufOf = (r) => {
    const d = r && r.data;
    return d ? Buffer.from(new Uint8Array(d.buffer, d.byteOffset, d.byteLength)) : Buffer.alloc(0);
  };
  const inX = async (q, l, v = 0) => {
    try {
      return bufOf(await attempt(() => dev.controlTransferIn(
        { requestType: 'class', recipient: 'interface', request: q, value: v, index: intf }, l), 3, 15));
    } catch (e) { if (STALL.test(e.message)) return Buffer.alloc(0); throw e; }
  };
  const outX = async (q, d, v = 0) => attempt(() => dev.controlTransferOut(
    { requestType: 'class', recipient: 'interface', request: q, value: v, index: intf },
    d === undefined ? new Uint8Array(0) : d));

  const status = async () => {
    const b = await inX(GETSTATUS, 6);
    return b.length < 6 ? { status: -1, poll: 0, state: -1 }
      : { status: b[0], poll: b[1] | (b[2] << 8) | (b[3] << 16), state: b[4] };
  };
  const settle = async () => {
    let s = await status(), g = 0;
    while (s.state === 4 && g++ < 1000) { await sleep(Math.min(s.poll || 10, 50)); s = await status(); }
    return s;
  };
  const toIdle = async () => {
    try { await outX(ABORT); } catch { }
    const s = await inX(GETSTATE, 1);
    if (s[0] === ST_ERROR) { try { await outX(CLRSTATUS); } catch { } }
    return s[0];
  };
  const setAddr = async (a) => {
    const b = new Uint8Array(5); b[0] = 0x21;
    b[1] = a & 0xff; b[2] = (a >>> 8) & 0xff; b[3] = (a >>> 16) & 0xff; b[4] = (a >>> 24) & 0xff;
    await outX(DNLOAD, b, 0);
    return settle();
  };
  const upload = (len, v = 2) => inX(UPLOAD, len, v);
  const dnload = (data, v = 2) => outX(DNLOAD, data, v);

  /** 64 B 步进读一整段，返回 Buffer。每块都 SET_ADDRESS，wValue 恒为 2。 */
  async function readRange(from, length, onTick) {
    const out = Buffer.alloc(length);
    let empty = 0;
    const t0 = Date.now();
    for (let off = 0; off < length; off += STEP) {
      await toIdle(); await setAddr(from + off); await toIdle();
      const b = await upload(Math.min(STEP, length - off), 2);
      if (!b.length) {
        if (++empty >= 10) throw new Error(`@${hx(from + off, 6)} 连续空包，读失败`);
        continue;
      }
      empty = 0;
      b.copy(out, off, 0, Math.min(b.length, STEP));
      if (onTick && off % 0x4000 === 0) onTick(off, length, (Date.now() - t0) / 1000);
    }
    return out;
  }

  async function waitApp(timeoutMs = 90000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      if (findApp()) return (Date.now() - t0) / 1000;
      await sleep(300);
    }
    return -1;
  }

  async function leave() {
    await toIdle();
    await setAddr(BASE);
    // manifest 必须是零长度 DNLOAD 且 wValue=0。
    // 写成 dnload(空, 2) 时 wValue=2 被 DfuSe 当成「块号 2 的空块写入」，
    // 设备只会更新块指针，不会固化重启 —— 实测现象就是设备一直停在 DFU。
    await outX(DNLOAD, new Uint8Array(0), 0);
    await sleep(300);
    // 探一次状态：设备若已重启，这一发会直接断开（属正常）
    try { await status(); } catch { }
    await sleep(500);
  }

  async function close() { try { await dev.close(); } catch { } }

  return {
    dev, intf, cfg: cfgVal, status, settle, toIdle, setAddr, upload, dnload,
    readRange, waitApp, leave, close, get state() { return dev; },
  };
}

module.exports = {
  KB_VID, KB_PID, DFU_VID, DFU_PID, BASE, STEP, SECTOR, FULL, PAYLOAD, ST_OK,
  findApp, listDfu, kick, takeOver, crc32, md5, log, sleep, hx,
};
