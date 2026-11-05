'use strict';
// Native on-screen widget channel: private 0x3D command over HID.
//
// This path writes text into RAM and lets the firmware's own 250 ms timer redraw
// the labels, so it never touches the JPEG/GIF upload stream. That keeps it free
// of the media pipeline's throttling, and it leaves every vendor page's own
// content untouched: the overlay only adds labels on top of the page you pick.
//
// Commands (payload after a5 3d 00 LEN 00 4e 37 01):
//   len 4                      probe: is the widget live on this page?
//   len 6+N  row 0..3          write one text row, 0..47 printable ASCII bytes
//   len 7    0x7f 1 mask       switch which pages the overlay answers on
//
// Status bytes: 0xfe accepted, 0xfd not initialised on this page,
//               0xfc invalid frame or unknown page.
const COMMAND = 0x3d;
const MAX_ROWS = 4;
// Compatible with 32-byte firmware row buffers (one byte for NUL).
const MAX_TEXT = 31;
const FULL_REFRESH_MS = 8000;

function checksum(body) {
  return (body.reduce((a, b) => a + b, 0) ^ 0xff) & 0xff;
}

function packet(bytes) {
  const body = [COMMAND, 0, bytes.length, ...bytes];
  const report = Buffer.alloc(65);
  report.set([0, 0xa5, ...body, checksum(body)]);
  return report;
}

function probePacket() {
  return packet([0, 0x4e, 0x37, 1]);
}

function rowPacket(row, text) {
  if (!Number.isInteger(row) || row < 0 || row > MAX_ROWS - 1) throw Error('row must be 0..3');
  if (typeof text !== 'string' || !new RegExp(`^[\x20-\x7e]{0,${MAX_TEXT}}$`).test(text)) {
    throw Error('each row needs 0..47 printable ASCII bytes');
  }
  return packet([0, 0x4e, 0x37, 1, row, text.length, ...Buffer.from(text, 'ascii')]);
}

// mask = 0 是合法值：设备接受它并把文字组件彻底关掉（实测回 0xFE，随后 probe 变 0xFD）。
function maskPacket(mask) {
  if (!Number.isInteger(mask) || mask < 0 || mask > 0x3f) throw Error('mask must be 0..0x3f (pages 1..6, 0=off)');
  return packet([0, 0x4e, 0x37, 1, 0x7f, 1, mask]);
}

// 空数组 → 0，表示六页全不显示。选页与关掉是同一件事，不需要单独的「关闭」命令。
function maskFromPages(pages) {
  let mask = 0;
  for (const p of pages) {
    if (!Number.isInteger(p) || p < 1 || p > 6) throw Error(`page ${p} outside 1..6`);
    mask |= 1 << (p - 1);
  }
  return mask;
}

function pagesFromMask(mask) {
  const out = [];
  for (let i = 0; i < 6; i++) if (mask & (1 << i)) out.push(i + 1);
  return out;
}

function parseAck(reply) {
  if (!reply || reply.length < 7 || reply[0] !== 0xa5 || reply[1] !== COMMAND || reply[3] !== 2) {
    throw Error('native acknowledgement header mismatch');
  }
  if (((reply.subarray(1, 6).reduce((a, b) => a + b, 0) ^ 0xff) & 0xff) !== reply[6]) {
    throw Error('native acknowledgement checksum mismatch');
  }
  return { ready: reply[4] === 0xfe, status: reply[4] };
}

function explain(status) {
  if (status === 0xfe) return '已接受';
  if (status === 0xfd) return '该页尚未初始化：先翻到目标页再试';
  if (status === 0xfc) return '无效帧或页号超出范围';
  return `设备返回未知状态 0x${status.toString(16)}`;
}

function openDevice(HID) {
  const hits = HID.devices().filter(d => d.vendorId === 0x5542 && d.productId === 1
    && d.usagePage === 0xff60 && d.usage === 0x61);
  if (hits.length === 0) throw Error('未找到屏幕接口：请确认键盘处于有线模式');
  if (hits.length > 1) throw Error(`检测到 ${hits.length} 个屏幕接口，请只连接一台键盘`);
  return new HID.HID(hits[0].path);
}

function exchange(dev, bytes, timeout = 1500) {
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (err, value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      dev.removeListener('data', onData);
      dev.removeListener('error', onError);
      if (err) reject(err); else resolve(value);
    };
    const onData = d => { if (d.length >= 7 && d[0] === 0xa5 && d[1] === COMMAND) finish(null, d); };
    const onError = e => finish(e);
    const timer = setTimeout(() => finish(Error('等待原生组件应答超时')), timeout);
    dev.on('data', onData);
    dev.on('error', onError);
    try { dev.write(bytes); } catch (e) { finish(e); }
  });
}

async function request(dev, bytes) {
  return parseAck(await exchange(dev, bytes));
}

async function probe(dev) { return request(dev, probePacket()); }

async function sendRows(dev, rows, previous = null) {
  if (!Array.isArray(rows) || rows.length !== MAX_ROWS) throw Error('需要正好四行文字');
  const reports = Array.from(rows, (text, row) => rowPacket(row, text));
  const caps = await probe(dev);
  if (!caps.ready) throw Error(explain(caps.status));
  let written = 0;
  for (let row = 0; row < MAX_ROWS; row++) {
    if (previous && rows[row] === previous[row]) continue;
    const ack = await request(dev, reports[row]);
    if (!ack.ready) throw Error(explain(ack.status));
    written++;
  }
  return { rows: MAX_ROWS, written, reports: 1 + written, acknowledged: true, displayVerified: false };
}

function rowStream(dev, clock = () => performance.now()) {
  // ACKs don't identify the active page. Only cache within a single acknowledged
  // page mask, never between selected pages or after an error. Probe even on a
  // steady frame; periodically send all rows before the firmware's 15s timeout.
  let single = false, previous = null, lastFull = -Infinity;
  const invalidate = () => { previous = null; lastFull = -Infinity; };
  return {
    invalidate,
    async mask(value) {
      invalidate();
      single = false;
      const result = await setMask(dev, value);
      single = result.pages.length === 1;
      return result;
    },
    async send(rows) {
      const full = !single || !previous || clock() - lastFull >= FULL_REFRESH_MS;
      try {
        const result = await sendRows(dev, rows, full ? null : previous);
        if (single) previous = rows.slice();
        if (full) lastFull = clock();
        return result;
      } catch (error) {
        invalidate();
        throw error;
      }
    },
  };
}

async function setMask(dev, mask) {
  const ack = await request(dev, maskPacket(mask));
  if (!ack.ready) throw Error(explain(ack.status));
  return { mask, pages: pagesFromMask(mask), acknowledged: true };
}

module.exports = {
  COMMAND, MAX_ROWS, MAX_TEXT,
  checksum, packet, probePacket, rowPacket, maskPacket,
  maskFromPages, pagesFromMask, parseAck, explain,
  openDevice, exchange, request, probe, sendRows, setMask, rowStream,
};
