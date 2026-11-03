"""Run real Thumb prototype, vendor command parser and page switch in Unicorn.

Provide a private matching full-screen+clean payload via --input. LVGL/allocator
calls are explicit stubs; LCD, RTOS scheduling and peripheral timing are not emulated.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest
import zlib
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import *
import build_native as b

REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]
STOP, FRAME, STACK = 0x7f000, 0x20010000, 0x2001e000
MD = Cs(CS_ARCH_ARM, CS_MODE_THUMB)


def command(row=None, text=b'', *, payload=None):
    if payload is None:
        payload = b'\0N7\1' + (bytes([row, len(text)]) + text if row is not None else b'')
    body = bytes([b.COMMAND, 0, len(payload)]) + payload
    return b'\xa5' + body + bytes([(sum(body) ^ 255) & 255])


class Machine:
    def __init__(self, image, fail_alloc=False, fail_timer=False):
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0, 0x100000)
        self.u.mem_write(b.BASE, image[:b.LENGTH])
        self.u.mem_map(0x20000000, 0x20000)
        self.u.mem_map(0x80000000, 0x200000)
        self.heap = 0x80010000
        self.objects, self.texts, self.calls, self.acks, self.writes = {}, {}, [], [], []
        self.active, self.timer = 0, None
        self.stub_page5_loader, self.page5_loads = False, 0
        self.fail_alloc, self.fail_timer = fail_alloc, fail_timer
        self.interleave = None
        self.u.hook_add(UC_HOOK_CODE, self.hook)
        self.u.hook_add(UC_HOOK_MEM_WRITE, lambda uc, access, a, n, v, _: self.writes.append((a, n)))

    def word(self, address):
        return struct.unpack('<I', self.u.mem_read(address, 4))[0]

    def put(self, address, value):
        self.u.mem_write(address, struct.pack('<I', value))

    def alloc(self, size):
        address = self.heap
        self.heap += (size+15) & ~15
        return address

    def string(self, address):
        data = bytes(self.u.mem_read(address, 256))
        return data.split(b'\0')[0].decode('ascii')

    def hook(self, uc, address, size, _):
        if address == STOP:
            uc.emu_stop()
            return
        if address == 0x11914 and self.stub_page5_loader:
            # Isolate navigation from media decoding/peripheral reads. This does
            # NOT verify JPEG/GIF display; the real table and switch still run.
            self.page5_loads += 1
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        ins = next(MD.disasm(bytes(uc.mem_read(address, size)), address), None)
        if not ins or ins.mnemonic not in ('bl', 'blx'):
            return
        if ins.mnemonic == 'blx':
            # Execute genuine table-dispatched page creators/loaders.
            return
        dest = int(ins.op_str.removeprefix('#'), 0)
        args = [uc.reg_read(reg) for reg in REGS]
        self.calls.append((address, dest, args))
        result = 0
        if dest in (0x26dd4, 0x25430):
            result = self.alloc(256)
            self.objects[result] = args[0]
        elif dest == 0x2637c:
            result = 0 if self.fail_alloc else self.alloc(args[0])
        elif dest == 0x2c574:
            if not self.fail_timer:
                self.timer = args[:3]
                result = self.alloc(32)
        elif dest == 0x2625c:
            assert args[0] in self.objects, ('bad label pointer', args)
            self.texts[args[0]] = self.string(args[1])
        elif dest == 0x83e4:
            uc.mem_write(args[0], bytes(uc.mem_read(args[1], args[2])))
            if self.interleave:
                callback, self.interleave = self.interleave, None
                callback()
            result = args[0]
        elif dest == 0x2b114:
            result = self.active
        elif dest == 0x2b120:
            self.active = args[0]
        elif dest == 0x35074:
            self.acks.append(bytes(uc.mem_read(args[0], args[1])))
        elif dest in (0x2a770, 0x2a744, 0x2a9d4, 0x2a9b4, 0x2a6e8, 0x2aafc, 0x2ab0c,
                      0x357d0, 0x33fec, 0x3d2fc, 0x10c34, 0x1cd4c):
            pass
        elif dest in (b.TICK, 0x3401c, 0x35050, 0x351d0):
            return  # Execute these real firmware routines, not Python replacements.
        else:
            raise AssertionError(f'unexpected external call {address:x} -> {dest:x} {args}')
        uc.reg_write(UC_ARM_REG_R0, result)
        uc.reg_write(UC_ARM_REG_PC, (address+size) | 1)

    def run(self, entry, *args):
        for reg, value in zip(REGS, args):
            self.u.reg_write(reg, value)
        self.u.reg_write(UC_ARM_REG_SP, STACK)
        self.u.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.u.emu_start(entry | 1, STOP, count=40000)
        assert self.u.reg_read(UC_ARM_REG_PC) == STOP, 'instruction budget exceeded'
        assert self.u.reg_read(UC_ARM_REG_SP) == STACK, 'stack imbalance'

    def create(self):
        self.run(b.CREATE, b.ROOT)
        self.active = self.word(b.ROOT+0xe4)

    def receive(self, frame):
        self.u.mem_write(FRAME, frame + bytes(128-len(frame)))
        self.acks.clear()
        self.run(0x35220, FRAME, len(frame))
        assert len(self.acks) == 1, self.acks
        ack = self.acks[0]
        assert ack[0:4] == bytes([0xa5, frame[1], 0, 2]), ack.hex()
        assert ack[-1] == (sum(ack[1:-1]) ^ 255) & 255
        return ack[4]

    def rows(self):
        context = self.word(b.CONTEXT_SLOT)
        return [self.texts[self.word(context+8+i*4)] for i in range(4)]


class NativeTests(unittest.TestCase):
    def machine(self, **kwargs):
        m = Machine(TARGET, **kwargs)
        # Actual startup flag initialization, not an all-zero root structure.
        m.run(0x1ba20, b.ROOT)
        return m

    def test_real_page_table_creates_page6_and_reenters_without_reallocation(self):
        m = self.machine()
        m.put(b.ROOT, 0x800ef000)  # existing system screen, normal awake gate
        m.u.mem_write(0x2000bf77, b'\1')
        m.run(0x3c128, 5)
        self.assertEqual(m.active, m.word(b.ROOT+0xe4))
        self.assertEqual(len(m.objects), 5)
        self.assertEqual(m.timer[:2], [b.TICK | 1, 250])
        heap = m.heap
        m.put(b.ROOT+0xcc, 0x800ff000)  # existing page 5 object
        m.stub_page5_loader = True
        m.run(0x3c128, 4)
        self.assertEqual(m.active, 0x800ff000)
        self.assertEqual(m.page5_loads, 1)
        m.run(0x3c128, 5)
        self.assertEqual(m.heap, heap)
        self.assertEqual(m.word(b.ROOT+0x104), 0)
        self.assertEqual(m.word(b.ROOT+0xe8), 1, 'retain vendor page-6 initialization flag')

    def test_capability_does_not_initialize_page_or_write_state(self):
        stock = Machine(SOURCE)
        self.assertEqual(stock.receive(command()), 0xfc)
        self.assertFalse(any(d in (0x38070, 0x3816c, 0xa68c) for _, d, _ in stock.calls))
        m = self.machine()
        before = len(m.writes)
        self.assertEqual(m.receive(command()), 0xfd)
        self.assertIsNone(m.timer)
        self.assertEqual(m.word(b.CONTEXT_SLOT), 0)
        m.create()
        ctx = m.word(b.CONTEXT_SLOT)
        old = bytes(m.u.mem_read(ctx, 256))
        self.assertEqual(m.receive(command()), 0xfe)
        self.assertEqual(bytes(m.u.mem_read(ctx, 256)), old)

    def test_real_parser_data_is_drawn_only_on_widget_page(self):
        m = self.machine(); m.create()
        page6 = m.active
        m.active = 0x800f0000
        m.put(b.ROOT+0xcc, m.active)
        before = m.rows()
        ctx = m.word(b.CONTEXT_SLOT)
        for i, text in enumerate([b'CPU 42% 55C', b'WEATHER 16.3C RAIN', b'CODEX RUNNING', b'USB connected']):
            self.assertEqual(m.receive(command(i, text)), 0xfe)
        m.run(b.TICK)
        self.assertEqual(m.rows(), before)
        self.assertEqual(m.active, m.word(b.ROOT+0xcc))
        m.active = page6
        m.run(b.LOAD, b.ROOT)
        self.assertEqual(m.rows(), ['CPU 42% 55C', 'WEATHER 16.3C RAIN', 'CODEX RUNNING', 'USB connected'])
        self.assertFalse(any(d in (0x38070, 0x3816c, 0xa68c) for _, d, _ in m.calls))
        self.assertTrue(all(0x20000000 <= a < 0x20020000 or 0x80000000 <= a < 0x80200000 for a, _ in m.writes))

    def test_bad_payloads_and_checksum_do_not_modify_context(self):
        m = self.machine(); m.create()
        ctx = m.word(b.CONTEXT_SLOT)
        bad = [b'\0N7', b'\0N7\2', b'\0N7\1\4\1A', b'\0N7\1\0\x30'+b'a'*48,
               b'\0N7\1\0\2A', b'\0N7\1\0\1\xff', b'\0N7\1\0\1\n', b'\1N7\1']
        for payload in bad:
            old = bytes(m.u.mem_read(ctx, 256))
            self.assertEqual(m.receive(command(payload=payload)), 0xfc)
            self.assertEqual(bytes(m.u.mem_read(ctx, 256)), old)
        old = bytes(m.u.mem_read(ctx, 256))
        frame = bytearray(command(0, b'bad checksum')); frame[-1] ^= 1
        self.assertEqual(m.receive(bytes(frame)), 0xfc)
        self.assertEqual(bytes(m.u.mem_read(ctx, 256)), old)

    def test_empty_maximum_length_and_rapid_replacement(self):
        m = self.machine(); m.create()
        ctx = m.word(b.CONTEXT_SLOT)
        sentinel = bytes([0x9d])*16
        m.u.mem_write(ctx+256, sentinel)
        for i in range(100):
            self.assertEqual(m.receive(command(i % 4, (b'X'*47 if i % 2 else b''))), 0xfe)
        m.run(b.TICK)
        self.assertEqual(m.rows(), ['', 'X'*47, '', 'X'*47])
        self.assertEqual(bytes(m.u.mem_read(ctx+256, 16)), sentinel)
        calls = len(m.calls)
        m.run(b.TICK)
        self.assertFalse(any(d == 0x2625c for _, d, _ in m.calls[calls:]))

    def test_torn_row_snapshot_is_skipped_then_refreshed(self):
        m = self.machine(); m.create()
        m.receive(command(0, b'old'))
        ctx = m.word(b.CONTEXT_SLOT)
        def replace_during_snapshot():
            m.put(ctx+24, 4)
            m.u.mem_write(ctx+56, b'new\0')
        m.interleave = replace_during_snapshot
        m.run(b.TICK)
        self.assertEqual(m.rows()[0], 'Waiting for PC')
        m.run(b.TICK)
        self.assertEqual(m.rows()[0], 'new')
        m.put(ctx+24, 5)
        m.run(b.TICK)
        self.assertEqual(m.rows()[0], 'new')

    def test_allocator_and_timer_failure_leave_receiver_not_ready(self):
        for kwargs in [dict(fail_alloc=True), dict(fail_timer=True)]:
            m = self.machine(**kwargs); m.create()
            self.assertEqual(m.receive(command()), 0xfd)
            self.assertEqual(m.word(b.CONTEXT_SLOT), 0)

    def test_timeout_background_reconnect_and_clock_wrap(self):
        m = self.machine(); m.create()
        m.put(b.MILLIS, 0xfffffff0)
        for i in range(4):
            m.receive(command(i, b'LIVE'))
        m.run(b.TICK)
        self.assertEqual(m.rows(), ['LIVE']*4)
        m.put(b.MILLIS, (0xfffffff0+b.TIMEOUT-1) & 0xffffffff)
        m.run(b.TICK)
        self.assertEqual(m.rows(), ['LIVE']*4)
        m.put(b.MILLIS, (0xfffffff0+b.TIMEOUT) & 0xffffffff)
        page6 = m.active
        m.active = 0x800ff000
        m.run(b.TICK)
        self.assertEqual(m.rows(), ['LIVE']*4)
        m.active = page6
        m.run(b.TICK)
        self.assertEqual(m.rows(), ['PC disconnected', '--', '--', '--'])
        before = len(m.calls)
        m.run(b.TICK)
        self.assertFalse(any(d == 0x2625c for _, d, _ in m.calls[before:]))
        m.receive(command(0, b'BACK'))
        m.run(b.TICK)
        self.assertEqual(m.rows(), ['BACK', 'LIVE', 'LIVE', 'LIVE'])

    def test_patch_scope_unknown_image_and_tail_preservation(self):
        allowed = set()
        for p in REPORT['patches']:
            start = int(p['offset'], 16)
            allowed.update(range(start, start+len(bytes.fromhex(p['after']))))
        self.assertTrue(all(a == v or i in allowed for i, (a, v) in enumerate(zip(SOURCE, TARGET))))
        self.assertEqual(TARGET[b.CODE_LENGTH:b.LENGTH], SOURCE[b.CODE_LENGTH:])
        self.assertEqual(TARGET[:256], SOURCE[:256])
        self.assertEqual(TARGET[0x376d0-b.BASE:0x379ac-b.BASE], SOURCE[0x376d0-b.BASE:0x379ac-b.BASE])
        self.assertEqual(TARGET[0x44848-b.BASE:0x44884-b.BASE], SOURCE[0x44848-b.BASE:0x44884-b.BASE])
        altered = bytearray(SOURCE); altered[0x100] ^= 1
        with self.assertRaises(ValueError): b.patch(altered)
        with self.assertRaises(ValueError): b.patch(SOURCE[:-1])
        variant = bytearray(SOURCE); variant[-10] ^= 1
        patched, _ = b.patch(variant)
        self.assertEqual(patched[b.CODE_LENGTH:b.LENGTH], variant[b.CODE_LENGTH:])

    def test_node_host_vectors_match_actual_firmware_parser(self):
        js = "const p=require('./experimental/native-pages/send');const f=r=>r.subarray(1,6+r[4]).toString('hex');console.log(JSON.stringify([f(p.packet()),f(p.packet(2,'CODEX RUNNING'))]))"
        vectors = json.loads(subprocess.check_output(['node', '-e', js], cwd=Path(__file__).resolve().parents[2], text=True))
        self.assertEqual(vectors[0], command().hex())
        self.assertEqual(vectors[1], command(2, b'CODEX RUNNING').hex())
        m = self.machine(); m.create()
        self.assertEqual(m.receive(bytes.fromhex(vectors[1])), 0xfe)
        m.run(b.TICK)
        self.assertEqual(m.rows()[2], 'CODEX RUNNING')

    def test_dfu_suffix_and_erase_sector_reconstruction(self):
        self.assertEqual(len(TARGET), b.LENGTH+16)
        self.assertEqual(TARGET[-16:-4], bytes.fromhex('ffff00104255000155464410'))
        self.assertEqual(struct.unpack('<I', TARGET[-4:])[0], ~zlib.crc32(TARGET[:-4]) & 0xffffffff)
        self.assertEqual(hashlib.sha256(TARGET).hexdigest(), REPORT['target_sha256'])
        # Offline erase/program model only. No transport is imported.
        for size in (4096, 8192):
            reconstructed = bytearray(SOURCE)
            sectors = {i//size*size for i, (x,y) in enumerate(zip(SOURCE, TARGET)) if x != y}
            for start in sectors:
                reconstructed[start:start+size] = b'\xff'*size
                reconstructed[start:start+size] = TARGET[start:start+size]
            self.assertEqual(reconstructed, TARGET[:b.LENGTH])

    def test_vendor_navigation_ack_uses_status_not_read_selector(self):
        for image in (SOURCE, TARGET):
            m = Machine(image)
            frame = bytes.fromhex('a53900020001c3')
            self.assertEqual(m.receive(frame), 0xfe)
            self.assertEqual(m.acks, [bytes.fromhex('a5390002fe00c6')])
            self.assertTrue(any(dest == 0x1cd4c for _, dest, _ in m.calls))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--input', type=Path, required=True)
    args, rest = p.parse_known_args()
    SOURCE = args.input.read_bytes()
    TARGET, REPORT = b.patch(SOURCE)
    unittest.main(argv=[sys.argv[0], *rest])
