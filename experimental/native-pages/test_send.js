'use strict';
// No real HID module is loaded by this test.
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const api = require('./send');
class Device extends EventEmitter {
  constructor(statuses = [0xfe]) { super(); this.statuses = statuses; this.reports = []; }
  write(bytes) {
    this.reports.push(Buffer.from(bytes));
    const status = this.statuses.length > 1 ? this.statuses.shift() : this.statuses[0];
    const body = [0x3d, 0, 2, status, 0];
    setImmediate(() => this.emit('data', Buffer.from([0xa5, ...body, (body.reduce((a,b) => a+b, 0)^255)&255])));
    return bytes.length;
  }
  close() { this.closed = true; }
}
async function test() {
  let opened = false;
  const forbidden = () => { opened = true; throw Error('must not open'); };
  await assert.rejects(api.main(['--rows', 'missing.json'], forbidden), /Usage/);
  await assert.rejects(api.main(['--invalid'], forbidden), /Usage/);
  assert.equal(opened, false);
  for (const rows of [[], ['a','b','c','中文'], ['a','b','c','\n'], ['a','b','c','X'.repeat(48)]]) {
    const d = new Device();
    await assert.rejects(api.send(d, rows));
    assert.equal(d.reports.length, 0);
  }
  for (const status of [0xfc, 0xfd]) {
    const d = new Device([status]);
    await assert.rejects(api.send(d, ['a','b','c','d']));
    assert.equal(d.reports.length, 1, 'unsupported device must receive only capability probe');
    assert.equal(d.listenerCount('data'), 0);
  }
  const d = new Device();
  assert.deepEqual(await api.send(d, ['', 'X'.repeat(47), 'CODEX UNKNOWN', 'WX N/A']),
    { rows: 4, acknowledged: true, displayVerified: false, mediaUpload: false });
  assert.equal(d.reports.length, 5);
  assert(d.reports.every(r => r.length === 65 && r[2] === 0x3d));
  assert.equal(d.listenerCount('data'), 0);
  assert.equal(d.listenerCount('error'), 0);
  const partial = new Device([0xfe, 0xfe, 0xfc]);
  await assert.rejects(api.send(partial, ['a','b','c','d']));
  assert.equal(partial.reports.length, 3, 'no automatic retry after rejection');
  assert.throws(() => api.acknowledgement(Buffer.from('a53d0002fe00ff','hex')), /checksum/);
  const failure = new Device([0xfc]);
  const fake = { devices: () => [{ vendorId:0x5542, productId:1, usagePage:0xff60, usage:0x61, path:'fake' }], HID: class { constructor() { return failure; } } };
  const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'neos-native-test-'));
  const file = path.join(dir, 'rows.json');
  try {
    fs.writeFileSync(file, JSON.stringify(['a','b','c','d']));
    await assert.rejects(api.main(['--rows', file, '--yes'], () => fake));
    assert.equal(failure.closed, true);
    assert.equal(failure.listenerCount('error'), 0);
  } finally { fs.unlinkSync(file); fs.rmdirSync(dir); }
  console.log('PASS native sender: preflight gates, unsupported firmware, bounds, ACK, stop on failure, cleanup; no USB');
}
test().catch(e => { console.error(e); process.exitCode = 1; });
