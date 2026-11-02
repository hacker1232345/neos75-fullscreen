/**
 * Neos75 屏幕 MCU DFU 传输层（由旧 UI 工具提取，已按 MCU 实测修正）。
 * MCU 每个 4096B 块必须独立 SET_ADDRESS，再以 DNLOAD block=2 写入。
 * 禁止沿用旧 UI 连续块语义；program 不自动重发；写后检查状态并逐字节回验。
 * 本模块只导出传输函数，擦写范围、备份和 SHA256 门控由 mcu_flash_live.js 管理。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const DFU_V = 0x5542, DFU_P = 0x1000;
const SCREEN_APP = { vid: 0x5542, pid: 0x0001 };
const PREPARE = [0x07, 0x03, 0x01, 0x20];
const TAIL = [0x13, 0x02, 0x00, 0x00, 0x00, 0x00, 0x78, 0xB7];

const FWALL = path.join(__dirname, '..', 'fw_all');
const OFFICIAL_UI = path.join(FWALL, '1_AULA-Neos75 Screen_V2.0_UI_2915-ZG06_2_31_V20_NEOS_SCREEN_UI_20260615.bin');
const BASE = 0x00008000;
const SEC = 0x1000;        // ★ 实测擦除粒度 4KB
const XFER = 4096;

const DNLOAD = 1, UPLOAD = 2, GETSTATUS = 3, CLRSTATUS = 4, ABORT = 6;
const hx = (n, w = 8) => '0x' + (n >>> 0).toString(16).toUpperCase().padStart(w, '0');
const ts = () => new Date().toISOString().slice(11, 23);
const log = (...a) => console.log(`[${ts()}]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const has = (k) => process.argv.includes('--' + k);
const die = (c, m) => { console.error(`\n✘ ${m}`); process.exit(c); };
const md5 = (b) => crypto.createHash('md5').update(b).digest('hex');
const isStall = (m) => /stalled|cancelled|canceled|borrowed|timed out|getString|failed to open/i.test(m);
const isFF = (b) => b.every((v) => v === 0xff);

let RAW = null;
async function rt(fn, tries = 40, gap = 25) {
  let last;
  for (let i = 0; i < tries; i++) { try { return await fn(); } catch (e) { if (!/borrowed mutably/.test(e.message)) throw e; last = e; await sleep(gap); } }
  throw new Error(`借用冲突（${last && last.message}）`);
}
function init() { if (!RAW) { require('usb'); RAW = require('usb/index.js'); } }

async function onBus() {
  init();
  let list = null;
  try { list = await rt(() => RAW.nativeGetDevices(), 20, 30); } catch { return { dfu: false, app: false }; }
  let dfu = false, app = false;
  for (const d of list) {
    let v, p;
    try { v = await rt(() => d.vendorId, 10, 10); p = await rt(() => d.productId, 10, 10); } catch { continue; }
    if (v === DFU_V && p === DFU_P) dfu = true;
    if (v === SCREEN_APP.vid && p === SCREEN_APP.pid) app = true;
  }
  return { dfu, app };
}
async function kick() {
  const bus = await onBus();
  if (bus.dfu) { log('已在 DFU'); return true; }
  if (!bus.app) { log('✘ 屏幕既不在应用模式也不在 DFU'); return false; }
  const HID = require('node-hid');
  const hits = HID.devices().filter((d) => d.vendorId === SCREEN_APP.vid && d.usagePage === 0xFF60 && d.usage === 0x61);
  if (!hits.length) { log('✘ 查不到屏幕 HID 控制接口'); return false; }
  const dev = new HID.HID(hits[0].path);
  dev.on('error', () => {}); dev.on('data', () => {});
  const send = (b) => { const p = Buffer.alloc(65, 0); Buffer.from(b).copy(p, 1); dev.write(Array.from(p)); };
  send(PREPARE); await sleep(700); send(TAIL); await sleep(300);
  try { dev.close(); } catch { }
  log('已发 enter-dfu');
  return true;
}
async function openDfu(waitMs) {
  init();
  const t0 = Date.now();
  while (Date.now() - t0 < waitMs) {
    let list = null;
    try { list = await rt(() => RAW.nativeGetDevices(), 20, 30); } catch { await sleep(150); continue; }
    for (const d of list) {
      let v, p;
      try { v = await rt(() => d.vendorId, 10, 10); p = await rt(() => d.productId, 10, 10); } catch { continue; }
      if (v !== DFU_V || p !== DFU_P) continue;
      for (let a = 1; a <= 3; a++) {
        try {
          await rt(() => d.open());
          const cfgs = await rt(() => d.configurations, 10, 60);
          let intf = -1;
          for (const c of cfgs) for (const it of (c.interfaces || []))
            for (const alt of (it.alternates || []))
              if (alt.interfaceClass === 0xfe && alt.interfaceSubclass === 0x01
                && (alt.interfaceProtocol === 1 || alt.interfaceProtocol === 2)) intf = it.interfaceNumber;
          if (intf < 0) { try { await rt(() => d.close(), 5, 20); } catch { } await sleep(250); continue; }
          await rt(() => d.claimInterface(intf), 10, 60);
          return { d, intf };
        } catch (e) {
          try { await rt(() => d.close(), 5, 20); } catch { }
          if (!isStall(e.message)) throw e;
          await sleep(600 * a);
        }
      }
    }
    await sleep(100);
  }
  return null;
}
function closeDfu(s) { try { s.d.close(); } catch { } }

function mk(s) {
  const { d, intf } = s;
  const cin = (r, l, v = 0) => d.controlTransferIn({ requestType: 'class', recipient: 'interface', request: r, value: v, index: intf }, l);
  const cout = (r, data, v = 0) => d.controlTransferOut({ requestType: 'class', recipient: 'interface', request: r, value: v, index: intf },
    data == null ? new Uint8Array(0) : (data instanceof Uint8Array ? data : new Uint8Array(data)));
  const clearHalt = async () => { for (const dir of ['in', 'out']) { try { await d.clearHalt(dir, intf); } catch { } } };
  /** F3：每个 USB 操作都包在重试里 */
  const op = async (label, fn, tries = 6) => {
    let last;
    for (let k = 0; k < tries; k++) {
      try { return await fn(); }
      catch (e) {
        last = e;
        if (!isStall(e.message)) throw e;
        await sleep(80 + k * 120);
        await clearHalt();
        try { await cout(ABORT, null); } catch { }
        await sleep(50);
      }
    }
    throw new Error(`${label} 重试 ${tries} 次仍失败（${last && last.message}）`);
  };
  const status = async () => op('GETSTATUS', async () => {
    const r = await cin(GETSTATUS, 6); const dv = r && r.data;
    if (!dv || dv.byteLength < 6) return null;
    const u = new DataView(dv.buffer, dv.byteOffset, dv.byteLength);
    return { status: u.getUint8(0), state: u.getUint8(4) };
  });
  const settle = async (n = 400) => { for (let i = 0; i < n; i++) { let x = null; try { x = await status(); } catch { } if (x && x.state !== 4) return x; await sleep(12); } return null; };
  const toIdle = async () => {
    let x = await settle().catch(() => null);
    if (x && x.state === 10) { try { await cout(CLRSTATUS, null); } catch { } x = await status().catch(() => null); }
    if (x && x.state !== 2) { try { await cout(ABORT, null); } catch { } await settle().catch(() => null); }
  };
  const setAddr = async (a) => op('SET_ADDRESS ' + hx(a), async () => {
    await cout(DNLOAD, Uint8Array.from([0x21, a & 255, (a >>> 8) & 255, (a >>> 16) & 255, (a >>> 24) & 255]), 0);
    return settle();
  });
  const erase = async (a) => op('ERASE ' + hx(a), async () => {
    await cout(DNLOAD, Uint8Array.from([0x41, a & 255, (a >>> 8) & 255, (a >>> 16) & 255, (a >>> 24) & 255]), 0);
    return settle();
  });
  /** F2：toIdle → setAddr → 连续 DNLOAD 4096 块 */
  const writeBlocks = async (addr, chunks) => {
    // MCU uses SET_ADDRESS + block number. Reusing block 2 without a new
    // address overwrites the same flash page; UI external-flash semantics differ.
    for (let i = 0; i < chunks.length; i++) {
      const at = addr + i * XFER;
      if (chunks[i].length !== XFER) throw new Error('MCU必须完整4096B块');
      await toIdle();
      await setAddr(at);
      await cout(DNLOAD, chunks[i], 2); // no automatic resend of a program operation
      const x = await settle();
      if (!x || x.status !== 0 || x.state !== 5)
        throw new Error(`MCU写入未完成 ${hx(at)}: ${JSON.stringify(x)}`);
      await sleep(30);
    }
  };
  /** 读一块：setAddr → toIdle → UPLOAD(wValue=2) */
  const read = async (addr, len) => {
    await toIdle();
    await setAddr(addr);
    await toIdle();
    return op('UPLOAD ' + hx(addr), async () => {
      const r = await cin(UPLOAD, len, 2); const dv = r && r.data;
      if (!dv || dv.byteLength !== len) throw new Error('读回长度不完整');
      return Buffer.from(new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength));
    });
  };
  /** 用 64B 小块读满一段（F4：小块最稳） */
  const readSpan = async (addr, len, onTick) => {
    const buf = Buffer.alloc(len);
    for (let p = 0; p < len; p += 64) {
      const b = await read(addr + p, 64);
      b.copy(buf, p, 0, Math.min(64, len - p));
      if (onTick && p % 1024 === 0) onTick(p, len);
    }
    return buf;
  };
  return { cin, cout, op, status, settle, toIdle, setAddr, erase, writeBlocks, read, readSpan };
}

/** 重开会话（F4：写入后立刻操作会 stall，需要较长冷却） */
async function reopen(s, tag) {
  closeDfu(s);
  await sleep(1800);
  let n = await openDfu(8000);
  if (!n) { log(`${tag} 会话丢失，kick…`); await kick(); await sleep(2500); n = await openDfu(45000); }
  if (n) log(`${tag} 已重开会话 state=${(await mk(n).status() || {}).state}`);
  return n;
}


module.exports={openDfu,mk,kick,closeDfu,sleep,log};
