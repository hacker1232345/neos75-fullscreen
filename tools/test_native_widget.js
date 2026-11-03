'use strict';
// Offline checks for the native widget channel. No USB device is opened.
const assert = require('assert');
const w = require('./native_widget.js');

let pass = 0;
function check(name, fn) {
  try { fn(); console.log(`  ok  ${name}`); pass++; }
  catch (e) { console.error(`FAIL  ${name}: ${e.message}`); process.exitCode = 1; }
}

function ack(status) {
  const body = [0xa5, 0x3d, 0, 2, status, 0];
  return Buffer.from([...body, (body.slice(1).reduce((a, b) => a + b, 0) ^ 0xff) & 0xff]);
}

check('probe frame is a 65-byte report starting with the report id', () => {
  const p = w.probePacket();
  assert.strictEqual(p.length, 65);
  assert.deepStrictEqual([...p.subarray(0, 8)], [0, 0xa5, 0x3d, 0, 4, 0, 0x4e, 0x37]);
  assert.strictEqual(p[8], 1);
});

check('probe frame carries a valid checksum', () => {
  // The report id occupies index 0 and 0xa5 index 1, so the vendor checksum
   // covers 0x3d (index 2) through the last payload byte (index 8).
  const p = w.probePacket();
  assert.strictEqual(w.checksum([...p.subarray(2, 9)]), p[9]);
});

check('row frame encodes index, length and ASCII payload', () => {
  const p = w.rowPacket(2, 'AB');
  assert.strictEqual(p[4], 8);
  assert.strictEqual(p[9], 2);
  assert.strictEqual(p[10], 2);
  assert.deepStrictEqual([p[11], p[12]], [0x41, 0x42]);
  assert.strictEqual(p[13], w.checksum([...p.subarray(2, 13)]));
});

check('row frame rejects non-printable and oversized text', () => {
  assert.throws(() => w.rowPacket(0, 'a\tb'), /printable ASCII/);
  assert.throws(() => w.rowPacket(0, 'x'.repeat(48)), /printable ASCII/);
  assert.throws(() => w.rowPacket(4, 'ok'), /row must be/);
});

check('mask frame encodes the page mask', () => {
  const p = w.maskPacket(0x2a);
  // The payload is 0x4e 0x37 0x01 0x7f 0x01 <mask>: seven bytes, so LEN = 7,
  // matching the laboratory sender widgetlab/setpage.js.
  assert.strictEqual(p[3], 0);
  assert.strictEqual(p[4], 7);
  assert.deepStrictEqual([...p.subarray(9, 12)], [0x7f, 1, 0x2a]);
  assert.strictEqual(p[12], w.checksum([...p.subarray(2, 12)]));
});

check('mask frame rejects an empty or out-of-range mask', () => {
  assert.throws(() => w.maskPacket(0), /mask must be/);
  assert.throws(() => w.maskPacket(0x40), /mask must be/);
});

check('page list and mask round-trip for every subset', () => {
  for (let mask = 1; mask <= 0x3f; mask++) {
    assert.strictEqual(w.maskFromPages(w.pagesFromMask(mask)), mask);
  }
});

check('maskFromPages refuses an empty selection and bad page numbers', () => {
  assert.throws(() => w.maskFromPages([]), /at least one page/);
  assert.throws(() => w.maskFromPages([0]), /outside 1..6/);
  assert.throws(() => w.maskFromPages([7]), /outside 1..6/);
});

check('acknowledgement parser accepts accepted and rejected statuses', () => {
  assert.deepStrictEqual(w.parseAck(ack(0xfe)), { ready: true, status: 0xfe });
  assert.deepStrictEqual(w.parseAck(ack(0xfd)), { ready: false, status: 0xfd });
  assert.deepStrictEqual(w.parseAck(ack(0xfc)), { ready: false, status: 0xfc });
});

check('acknowledgement parser rejects a bad checksum', () => {
  const bad = ack(0xfe);
  bad[6] ^= 0x01;
  assert.throws(() => w.parseAck(bad), /checksum/);
});

check('acknowledgement parser rejects a foreign reply', () => {
  assert.throws(() => w.parseAck(null), /header/);
  assert.throws(() => w.parseAck(Buffer.from([0xa5, 0x39, 0, 2, 0xfe, 0, 0xc6])), /header/);
});

check('status text covers every documented status', () => {
  assert.ok(w.explain(0xfe).includes('接受'));
  assert.ok(w.explain(0xfd).includes('目标页'));
  assert.ok(w.explain(0xfc).includes('无效帧'));
  assert.ok(w.explain(0x99).includes('0x99'));
});

console.log(`${pass} groups passed`);
