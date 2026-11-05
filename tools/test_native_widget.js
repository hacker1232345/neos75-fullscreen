'use strict';
// Offline checks for the native widget channel. No USB device is opened.
const assert = require('assert');
const { EventEmitter } = require('events');
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
  assert.doesNotThrow(() => w.rowPacket(0, 'x'.repeat(31)));
  assert.throws(() => w.rowPacket(0, 'x'.repeat(32)), /printable ASCII/);
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

check('mask frame accepts 0 (off) and rejects out-of-range masks', () => {
  const off = w.maskPacket(0);
  assert.deepStrictEqual([...off.subarray(9, 12)], [0x7f, 1, 0]);
  assert.throws(() => w.maskPacket(0x40), /mask must be/);
  assert.throws(() => w.maskPacket(-1), /mask must be/);
  assert.throws(() => w.maskPacket(1.5), /mask must be/);
});

check('page list and mask round-trip for every subset', () => {
  for (let mask = 1; mask <= 0x3f; mask++) {
    assert.strictEqual(w.maskFromPages(w.pagesFromMask(mask)), mask);
  }
});

check('maskFromPages maps an empty selection to 0 and rejects bad page numbers', () => {
  // 空选 = 0 = 六页全不显示，设备实测接受（0xFE）。
  assert.strictEqual(w.maskFromPages([]), 0);
  assert.throws(() => w.maskFromPages([0]), /outside 1..6/);
  assert.throws(() => w.maskFromPages([7]), /outside 1..6/);
  assert.throws(() => w.maskFromPages(['2']), /outside 1..6/);
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

async function streamChecks() {
  const dev = new EventEmitter();
  const writes = [];
  let status = 0xfe, time = 0;
  dev.write = bytes => {
    writes.push(bytes[4] === 4 ? 'probe' : bytes[9]);
    queueMicrotask(() => dev.emit('data', ack(status)));
  };
  const stream = w.rowStream(dev, () => time);
  const frame = ['clock', 'cpu', 'mem', 'disk'];
  await stream.mask(16);
  const take = () => writes.splice(0);
  assert.deepEqual(take(), [127]);
  assert.equal((await stream.send(frame)).reports, 5);
  assert.deepEqual(take(), ['probe', 0, 1, 2, 3]);
  time = 2000;
  assert.equal((await stream.send(frame)).written, 0);
  assert.deepEqual(take(), ['probe']);
  const changed = ['clock', 'cpu', '', 'disk'];
  assert.equal((await stream.send(changed)).reports, 2);
  assert.deepEqual(take(), ['probe', 2]);
  time = 9000;
  await stream.send(changed);
  assert.deepEqual(take(), ['probe', 0, 1, 2, 3]);
  status = 0xfd;
  await assert.rejects(stream.send(changed), /目标页/);
  assert.deepEqual(take(), ['probe']);
  status = 0xfe;
  await stream.send(changed);
  assert.deepEqual(take(), ['probe', 0, 1, 2, 3]);
  await stream.mask(18);
  take();
  await stream.send(changed);
  assert.deepEqual(take(), ['probe', 0, 1, 2, 3]);
  await stream.send(changed);
  assert.deepEqual(take(), ['probe', 0, 1, 2, 3]);
  await assert.rejects(stream.send(['clock', 'cpu', 'x'.repeat(32), 'disk']), /ASCII/);
  assert.deepEqual(take(), []);
}
streamChecks().then(() => {
  console.log(`  ok  single-page delta, refresh, invalidation, multi-page fallback`);
  console.log(`${pass + 1} groups passed`);
}, error => { console.error(error); process.exitCode = 1; });
