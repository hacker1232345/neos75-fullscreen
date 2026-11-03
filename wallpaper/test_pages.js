'use strict';
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const pages = require('./pages');
async function test() {
  assert.equal(pages.packet('next').subarray(0, 8).toString('hex'), '00a53900020001c3');
  assert.equal(pages.packet('previous').subarray(0, 8).toString('hex'), '00a53900020002c2');
  assert.equal(pages.packet('next').length, 65);
  assert.throws(() => pages.packet('factory-reset'));
  await assert.rejects(pages.main(['next']), /--yes/);
  // Cross-checked by executing the reference firmware parser in ARM emulation.
  const ack = Buffer.from('a5390002fe00c6', 'hex');
  pages.validateAck(ack);
  assert.throws(() => pages.validateAck(Buffer.from([0xa5, 0x39, 0, 1, 0, 0])));
  assert.throws(() => pages.validateAck(Buffer.from('a5390002fc00c8', 'hex')), /rejected/);
  class Device extends EventEmitter {
    write(bytes) { setImmediate(() => this.emit('data', ack)); this.written = Buffer.from(bytes); return bytes.length; }
  }
  const dev = new Device();
  assert.deepEqual(await pages.change(dev, 'next'), { direction: 'next', acknowledged: true, pageVerified: false });
  assert.equal(dev.written[6], 1);
  assert.equal(dev.listenerCount('data'), 0);
  console.log('PASS screen navigation: official packet vectors, checksum, explicit gate, fake HID acknowledgement; no USB opened');
}
test().catch(e => { console.error(e); process.exitCode = 1; });
