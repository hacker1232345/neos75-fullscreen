'use strict';
// Private prototype command. Never flashes firmware or uploads media.
const { exchange } = require('../../wallpaper/screen_wallpaper');
const COMMAND = 0x3d;
function packet(row, text) {
  let payload = [0, 0x4e, 0x37, 1];
  if (row !== undefined) {
    if (!Number.isInteger(row) || row < 0 || row > 3) throw Error('row must be 0..3');
    if (typeof text !== 'string' || !/^[\x20-\x7e]{0,47}$/.test(text)) throw Error('each row requires 0..47 printable ASCII bytes');
    payload.push(row, text.length, ...Buffer.from(text, 'ascii'));
  }
  const body = [COMMAND, 0, payload.length, ...payload];
  const report = Buffer.alloc(65);
  report.set([0, 0xa5, ...body, (body.reduce((a, b) => a + b, 0) ^ 255) & 255]);
  return report;
}
function acknowledgement(r) {
  if (!r || r.length < 7 || r[0] !== 0xa5 || r[1] !== COMMAND || r[2] !== 0 || r[3] !== 2) throw Error('native acknowledgement header mismatch');
  if (((r.subarray(1, 6).reduce((a, b) => a + b, 0) ^ 255) & 255) !== r[6]) throw Error('native acknowledgement checksum mismatch');
  return { ready: r[4] === 0xfe, status: r[4], displayVerified: false };
}
async function request(dev, bytes) {
  return acknowledgement(await exchange(dev, bytes, r => r[0] === 0xa5 && r[1] === COMMAND));
}
async function probe(dev) { return request(dev, packet()); }
async function send(dev, rows) {
  if (!Array.isArray(rows) || rows.length !== 4) throw Error('provide exactly four rows');
  const reports = rows.map((text, row) => packet(row, text));
  const caps = await probe(dev);
  if (!caps.ready) throw Error(caps.status === 0xfd ? '请先在键盘切到第6页初始化原生组件' : '未确认匹配的实验固件，停止发送');
  for (const bytes of reports) {
    if (!(await request(dev, bytes)).ready) throw Error('设备拒绝原生组件数据');
  }
  return { rows: 4, acknowledged: true, displayVerified: false, mediaUpload: false };
}
async function main(args, hidProvider = () => require('node-hid')) {
  const mode = args[0];
  let rows;
  if (mode === '--probe' && args.length === 1) { /* no state changes */ }
  else if (mode === '--rows' && args.length === 3 && args[2] === '--yes') {
    rows = JSON.parse(require('fs').readFileSync(args[1], 'utf8'));
    if (!Array.isArray(rows) || rows.length !== 4) throw Error('provide exactly four rows');
    rows.forEach((text, row) => packet(row, text));
  } else throw Error('Usage: node experimental/native-pages/send.js --probe | --rows rows.json --yes');
  const HID = hidProvider();
  const hits = HID.devices().filter(d => d.vendorId === 0x5542 && d.productId === 1 && d.usagePage === 0xff60 && d.usage === 0x61);
  if (hits.length !== 1) throw Error('需且仅需一个 Neos75 屏幕接口');
  const dev = new HID.HID(hits[0].path);
  let fatal;
  const onError = e => { fatal = e; };
  dev.on('error', onError);
  try {
    const result = mode === '--probe' ? await probe(dev) : await send(dev, rows);
    if (fatal) throw fatal;
    console.log(JSON.stringify(result));
  } finally { dev.close(); dev.removeListener('error', onError); }
}
module.exports = { packet, acknowledgement, request, probe, send, main };
if (require.main === module) main(process.argv.slice(2)).catch(e => { console.error(e.message); process.exitCode = 1; });
