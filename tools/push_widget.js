'use strict';
// Command line front end for the native widget channel (tools/native_widget.js).
//
//   node tools/push_widget.js probe           问设备原生组件在这一页是否就绪
//   node tools/push_widget.js mask 2,4,6      设定哪些页显示组件（运行时，不写 Flash）
//   node tools/push_widget.js rows "a|b|c|d"  推四行文字
//   node tools/push_widget.js loop            常驻：按行读 JSON {"rows":[..4..]}，逐帧推送
//
// The text channel writes RAM only: no Flash erase, no DFU, and it never enters
// the JPEG/GIF upload queue, so it is independent of the media pipeline's throttle.
const readline = require('readline');
const w = require('./native_widget');

function parseRows(text) {
  if (typeof text !== 'string' || !text.length) throw Error('需要四行文字，用 | 分隔');
  const rows = text.split('|');
  if (rows.length !== w.MAX_ROWS) throw Error(`需要正好四行，收到 ${rows.length} 行`);
  return rows;
}

function parseMask(text) {
  if (typeof text !== 'string' || !text.length) throw Error('需要页掩码，例如 2,4,6 或 0x2a');
  if (/^0x[0-9a-f]+$/i.test(text)) {
    const mask = parseInt(text, 16);
    if (!(mask >= 1 && mask <= 0x3f)) throw Error('页掩码必须在 1..0x3f');
    return mask;
  }
  return w.maskFromPages(text.split(',').map(x => Number(x.trim())));
}

async function loop(emit) {
  const HID = require('node-hid');
  const dev = w.openDevice(HID);
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  let closed = false;
  const shutdown = () => {
    if (closed) return;
    closed = true;
    try { dev.close(); } catch { /* device may already be gone */ }
    emit({ stopped: true });
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  dev.on('error', e => emit({ error: e.message || String(e) }));
  for await (const line of lines) {
    const text = line.trim();
    if (!text) continue;
    let payload;
    try {
      payload = JSON.parse(text);
    } catch (e) {
      emit({ error: `帧格式不是 JSON：${e.message}` });
      continue;
    }
    if (payload.mask !== undefined) {
      try {
        emit({ mask: await w.setMask(dev, Number(payload.mask)) });
      } catch (e) {
        emit({ error: e.message || String(e) });
      }
    }
    if (payload.rows) {
      try {
        emit({ frame: await w.sendRows(dev, payload.rows) });
      } catch (e) {
        // A rejected frame must not end the session: the next one may succeed.
        emit({ error: e.message || String(e) });
      }
    }
  }
  shutdown();
}

async function once(argv) {
  const [command, arg] = argv;
  const HID = require('node-hid');
  const dev = w.openDevice(HID);
  try {
    if (command === 'probe') return await w.probe(dev);
    if (command === 'mask') return await w.setMask(dev, parseMask(arg));
    if (command === 'rows') return await w.sendRows(dev, parseRows(arg));
    throw Error(`未知命令 ${command || '(空)'}：可用 probe / mask / rows / loop`);
  } finally {
    dev.close();
  }
}

if (require.main === module) {
  const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
  const [command, arg] = process.argv.slice(2);
  const run = command === 'loop' ? loop(emit) : once([command, arg]).then(emit);
  run.catch(e => {
    process.stderr.write((e.message || String(e)) + '\n');
    process.exitCode = 1;
  });
}

module.exports = { parseRows, parseMask };
