'use strict';
// Official _bs_flat.js: SCREEN_CONTROL=0x39; FLIP_DOWN=1; FLIP_UP=2.
// Runtime navigation only. No picture upload, firmware write or factory reset.
const { exchange } = require('./screen_wallpaper');
const CODES = Object.freeze({ previous: 2, next: 1 });
function packet(direction) {
  if (!Object.hasOwn(CODES, direction)) throw Error('Only previous/next are supported');
  const body = [0x39, 0, 2, 0, CODES[direction]];
  const bytes = Buffer.alloc(65);
  bytes.set([0, 0xa5, ...body, (body.reduce((a, b) => a + b, 0) ^ 255) & 255]);
  return bytes;
}
function validateAck(r) {
  if (r.length < 6 || r[0] !== 0xa5 || r[1] !== 0x39 || r[2] !== 0 || r[4] !== 0) throw Error('Page acknowledgement header mismatch');
  const n = r[3];
  if (n < 1 || 4 + n >= r.length) throw Error('Page acknowledgement truncated');
  if (((r.subarray(1, 4 + n).reduce((a, b) => a + b, 0) ^ 255) & 255) !== r[4 + n]) throw Error('Page acknowledgement checksum mismatch');
}
async function change(dev, direction) {
  const bytes = packet(direction);
  const ack = await exchange(dev, bytes, r => r[0] === 0xa5 && r[1] === 0x39);
  validateAck(ack);
  return { direction, acknowledged: true, pageVerified: false };
}
async function main(args) {
  const [direction, yes, ...extra] = args;
  packet(direction); // reject all other control opcodes before opening HID
  if (yes !== '--yes' || extra.length) throw Error('Page change requires --yes');
  const HID = require('node-hid');
  const hits = HID.devices().filter(d => d.vendorId === 0x5542 && d.productId === 1 && d.usagePage === 0xff60 && d.usage === 0x61);
  if (hits.length !== 1) throw Error('需且仅需一个 Neos75 屏幕接口');
  const dev = new HID.HID(hits[0].path);
  try { console.log(JSON.stringify(await change(dev, direction))); }
  finally { dev.close(); }
}
module.exports = { packet, validateAck, change, main };
if (require.main === module) main(process.argv.slice(2)).catch(e => { console.error(e.message); process.exitCode = 1; });
