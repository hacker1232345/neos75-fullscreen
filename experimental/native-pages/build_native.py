"""Offline-only prototype: replace reference firmware page 6 with RAM text widgets.

No USB, flashing, network, or force option. Not a production firmware profile.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib
from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

BASE = 0x8000
LENGTH = 491520
CODE_LENGTH = 487424
INPUT_CODE_SHA = '8465dd6334eb0dc0d0862477b5df05c5d957b1bfe3d19ba1ad222fd3397ef4b7'
CREATE, RECEIVE, TICK, LOAD, WAIT = 0x379ac, 0x37b80, 0x37cc0, 0x37e60, 0x37e80
END = 0x37ef4
COMMAND = 0x3d
TABLE_ENTRY = 0x35316
ROOT = 0x2000349c
CONTEXT_SLOT = ROOT + 0xec  # Retired page-6 background object. +e8 is a vendor flag!
MAGIC = 0x3150374e  # N7P1; private prototype ABI, not a vendor command.
MILLIS = 0x20003a54  # Read by the vendor's 0x3d308 elapsed-time helper.
TIMEOUT = 15000
STALE, EMPTY = WAIT+16, WAIT+32


def address(reg, value):
    return f'movw {reg}, #{value & 65535}\nmovt {reg}, #{value >> 16}'


def assemble(text, start, end):
    encoded, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(text, addr=start)
    data = bytes(encoded)
    if len(data) > end-start or len(data) % 2:
        raise ValueError(f'Assembly does not fit {start:x}: {len(data)} > {end-start}')
    return data + bytes.fromhex('00bf') * ((end-start-len(data))//2)


def code():
    # Keep all state inside one allocated context; publish only after UI and timer
    # creation. The receiver never invokes LVGL, allocators or storage functions.
    # Context: magic+0, last_rx_ms+4, labels+8, received_gen+24, applied_gen+40,
    # four 48-byte ASCII buffers+56, have_data+248, stale_displayed+252.
    # Preserve ROOT+e8: vendor startup 0x1ba20 sets that screen flag to 1.
    creator = f'''
        push {{r4-r7, lr}}
        sub sp, #4
        mov r4, r0
        movs r0, #0
        add.w r1, r4, #0xec
        movs r2, #7
    zero_slots:
        str r0, [r1], #4
        subs r2, #1
        bne zero_slots
        bl #0x26dd4
        str.w r0, [r4, #0xe4]
        cmp r0, #0
        beq create_done
        movw r1, #480
        movw r2, #360
        bl #0x2a770
        ldr.w r0, [r4, #0xe4]
        movs r1, #0
        bl #0x2a744
        ldr.w r0, [r4, #0xe4]
        movs r1, #255
        movs r2, #0
        bl #0x2a9d4
        ldr.w r0, [r4, #0xe4]
        movw r1, #0x1082
        movs r2, #0
        bl #0x2a9b4
        movw r0, #256
        bl #0x2637c
        mov r5, r0
        cmp r0, #0
        beq create_done
        movs r1, #0
        movs r2, #64
    zero_context:
        str r1, [r0], #4
        subs r2, #1
        bne zero_context
        movs r6, #0
    create_label:
        ldr.w r0, [r4, #0xe4]
        bl #0x25430
        cmp r0, #0
        beq create_done
        mov r7, r0
        add.w r1, r5, #8
        str.w r0, [r1, r6, lsl #2]
        {address('r1', WAIT)}
        bl #0x2625c
        mov r0, r7
        movs r1, #20
        movs r2, #70
        mul r2, r6, r2
        adds r2, #30
        bl #0x2a6e8
        mov r0, r7
        movw r1, #440
        movs r2, #64
        bl #0x2a770
        mov r0, r7
        movw r1, #65535
        movs r2, #0
        bl #0x2aafc
        mov r0, r7
        {address('r1', 0x45634)}
        movs r2, #0
        bl #0x2ab0c
        adds r6, #1
        cmp r6, #4
        blo create_label
        {address('r0', TICK | 1)}
        movs r1, #250
        movs r2, #0
        bl #0x2c574
        cmp r0, #0
        beq create_done
        {address('r0', MAGIC)}
        str r0, [r5]
        dmb sy
        str.w r5, [r4, #0xec]
    create_done:
        add sp, #4
        pop {{r4-r7, pc}}
    '''
    # Entry from the original command switch AFTER the vendor checksum check.
    # Frame: A5 3D 00 length 00 'N' '7' 01 [row N text...], checksum.
    # length=4 is a read-only capability query; length=6+N writes one row.
    receiver = f'''
        push {{r4-r7, lr}}
        sub sp, #4
        movs r6, #252
        ldrb r0, [r5, #2]
        cmp r0, #0
        bne receive_done
        ldrb r0, [r5, #4]
        cmp r0, #0
        bne receive_done
        cmp r4, #4
        blo receive_done
        cmp r4, #53
        bhi receive_done
        ldrb r0, [r5, #5]
        cmp r0, #78
        bne receive_done
        ldrb r0, [r5, #6]
        cmp r0, #55
        bne receive_done
        ldrb r0, [r5, #7]
        cmp r0, #1
        bne receive_done
        {address('r0', CONTEXT_SLOT)}
        ldr r7, [r0]
        movs r6, #253
        cmp r7, #0
        beq receive_done
        ldr r0, [r7]
        {address('r1', MAGIC)}
        cmp r0, r1
        bne receive_done
        movs r6, #254
        cmp r4, #4
        beq receive_done
        movs r6, #252
        cmp r4, #6
        blo receive_done
        ldrb r2, [r5, #8]
        cmp r2, #3
        bhi receive_done
        ldrb r3, [r5, #9]
        cmp r3, #47
        bhi receive_done
        adds r0, r3, #6
        cmp r0, r4
        bne receive_done
        add.w r5, r5, #10
        movs r4, #0
    validate_ascii:
        cmp r4, r3
        bhs write_row
        ldrb r0, [r5, r4]
        cmp r0, #32
        blo receive_done
        cmp r0, #126
        bhi receive_done
        adds r4, #1
        b validate_ascii
    write_row:
        str r7, [sp]
        add.w r4, r7, #24
        add.w r4, r4, r2, lsl #2
        ldr r6, [r4]
        adds r6, #1
        str r6, [r4]
        dmb sy
        movs r0, #48
        mul r2, r0, r2
        add.w r7, r7, #56
        add r7, r2
        movs r0, #0
    copy_row:
        cmp r0, r3
        bhs terminate_row
        ldrb r1, [r5, r0]
        strb r1, [r7, r0]
        adds r0, #1
        b copy_row
    terminate_row:
        movs r1, #0
        strb r1, [r7, r3]
        ldr r7, [sp]
        {address('r0', MILLIS)}
        ldr r0, [r0]
        str r0, [r7, #4]
        movs r0, #1
        str.w r0, [r7, #248]
        dmb sy
        adds r6, #1
        str r6, [r4]
        movs r6, #254
    receive_done:
        mov r0, r6
        add sp, #4
        pop {{r4-r7, lr}}
        strb.w r0, [sp, #0x22]
        b.w #0x356e8
    '''
    # Seqlock snapshot: no LVGL call while copying a mutable receive buffer.
    # Once copied, lv_label_set_text owns a copy of the stack string.
    tick = f'''
        push {{r4-r7, lr}}
        sub sp, #52
        {address('r0', ROOT)}
        ldr.w r4, [r0, #0xe4]
        ldr.w r5, [r0, #0xec]
        cmp r4, #0
        beq tick_done
        cmp r5, #0
        beq tick_done
        bl #0x2b114
        cmp r0, r4
        bne tick_done
        ldr.w r0, [r5, #248]
        cmp r0, #0
        beq tick_done
        {address('r0', MILLIS)}
        ldr r0, [r0]
        ldr r1, [r5, #4]
        subs r0, r0, r1
        movw r1, #{TIMEOUT}
        cmp r0, r1
        bhs stale_rows
        ldr.w r0, [r5, #252]
        cmp r0, #0
        beq start_rows
        movs r0, #0
        str.w r0, [r5, #252]
        mvn r0, #0
        str r0, [r5, #40]
        str r0, [r5, #44]
        str r0, [r5, #48]
        str r0, [r5, #52]
    start_rows:
        movs r6, #0
    tick_row:
        add.w r0, r5, #24
        ldr.w r7, [r0, r6, lsl #2]
        tst r7, #1
        bne next_row
        add.w r0, r5, #40
        ldr.w r1, [r0, r6, lsl #2]
        cmp r1, r7
        beq next_row
        dmb sy
        movs r0, #48
        mul r0, r6, r0
        add.w r1, r5, #56
        add r1, r0
        mov r0, sp
        movs r2, #48
        bl #0x83e4
        movs r0, #0
        strb.w r0, [sp, #47]
        dmb sy
        add.w r0, r5, #24
        ldr.w r1, [r0, r6, lsl #2]
        cmp r1, r7
        bne next_row
        add.w r0, r5, #8
        ldr.w r0, [r0, r6, lsl #2]
        cmp r0, #0
        beq next_row
        mov r1, sp
        bl #0x2625c
        add.w r0, r5, #40
        str.w r7, [r0, r6, lsl #2]
    next_row:
        adds r6, #1
        cmp r6, #4
        blo tick_row
        b tick_done
    stale_rows:
        ldr.w r0, [r5, #252]
        cmp r0, #0
        bne tick_done
        movs r0, #1
        str.w r0, [r5, #252]
        movs r6, #0
    stale_label:
        add.w r0, r5, #8
        ldr.w r0, [r0, r6, lsl #2]
        {address('r1', EMPTY)}
        cmp r6, #0
        bne set_stale
        {address('r1', STALE)}
    set_stale:
        bl #0x2625c
        adds r6, #1
        cmp r6, #4
        blo stale_label
    tick_done:
        add sp, #52
        pop {{r4-r7, pc}}
    '''
    loader = f'b.w #{TICK}'
    out = (assemble(creator, CREATE, RECEIVE) + assemble(receiver, RECEIVE, TICK)
           + assemble(tick, TICK, LOAD) + assemble(loader, LOAD, WAIT))
    data = b'Waiting for PC\0'.ljust(16, b'\0') + b'PC disconnected\0'.ljust(16, b'\0') + b'--\0'
    out += data + bytes(END-WAIT-len(data))
    return out


def patch(source):
    if len(source) != LENGTH:
        raise ValueError('需要完整的 491520 字节备份，不接受封装文件或短读。')
    sha = hashlib.sha256(source[:CODE_LENGTH]).hexdigest()
    if sha != INPUT_CODE_SHA:
        raise ValueError('实验仅匹配 reference 全屏+清爽代码哈希；未知固件拒绝。')
    # This unused vendor switch entry must still point at the default return.
    expected = [(TABLE_ENTRY, bytes.fromhex('f501')),
                (0x4488c, struct.pack('<I', 0x11abd)),
                (0x340c2, bytes.fromhex('d4f8e400'))]
    for address_, before in expected:
        if source[address_-BASE:address_-BASE+len(before)] != before:
            raise ValueError(f'旧字节不匹配 {address_:x}')
    edits = [(CREATE, code(), 'replace only original page 6 creator span'),
             (TABLE_ENTRY, struct.pack('<H', (RECEIVE-0x352fe)//2), 'private RAM command 0x3D'),
             (0x4488c, struct.pack('<I', LOAD | 1), 'page 6 enter callback'),
             (0x340c2, assemble('b.w #0x340ea', 0x340c2, 0x340c6), 'skip old page 6 media updater')]
    result = bytearray(source)
    records = []
    for at, after, purpose in edits:
        offset = at-BASE
        before = source[offset:offset+len(after)]
        result[offset:offset+len(after)] = after
        records.append(dict(address=hex(at), offset=hex(offset), before=before.hex(), after=after.hex(), purpose=purpose))
    assert result[:256] == source[:256]
    assert result[CODE_LENGTH:] == source[CODE_LENGTH:]
    differences = [i for i, (a, b) in enumerate(zip(source, result)) if a != b]
    payload = bytes(result)
    result += bytes.fromhex('ffff00104255000155464410')
    result += struct.pack('<I', (~zlib.crc32(result)) & 0xffffffff)
    report = dict(status='EXPERIMENTAL_OFFLINE_ONLY', input_code_sha256=sha,
                  source_sha256=hashlib.sha256(source).hexdigest(),
                  target_payload_sha256=hashlib.sha256(payload).hexdigest(),
                  target_sha256=hashlib.sha256(result).hexdigest(),
                  changed_bytes=len(differences), sectors=[hex(BASE+x) for x in sorted({i//4096*4096 for i in differences})],
                  patches=records, symbols=dict(create=CREATE, receive=RECEIVE, tick=TICK, load=LOAD),
                  device_tested=False, auto_flash=False)
    return bytes(result), report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.out.with_suffix('.json').exists():
        raise SystemExit('输出已存在；拒绝覆盖。')
    target, report = patch(args.input.read_bytes())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(target)
    args.out.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'patches'}, ensure_ascii=False, indent=2))
