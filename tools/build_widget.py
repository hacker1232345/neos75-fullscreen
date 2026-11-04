"""Overlay widget builder.

Put a RAM text window on top of ANY vendor page without replacing it. The
vendor page table, creators, enter callbacks and page content stay
byte-identical: the only firmware changes are the 0x3D command-table entry
(so the private command reaches our receiver) and a block of code in a
vendor-free 0xFF region.

How the widget finds its page: the vendor firmware keeps the current page
index in one byte at 0x200044ad (0-based, written on every flip). The
receiver reads that byte, checks it against the build's page mask, and lazily
creates the four labels under the active screen. No page-enter hook and no
pointer comparison is involved, so vendor pages keep their original behaviour.
"""
import argparse
import hashlib
import json
import struct
import zlib
from pathlib import Path

try:
    from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
except ImportError:      # macOS arm64 没有 wheel：注入层降级，其它层照打
    Ks = None
    KS_ARCH_ARM = KS_MODE_THUMB = 0

BASE = 0x8000
LENGTH = 491520
CODE_LENGTH = 487424
REF_SHA = '8465dd6334eb0dc0d0862477b5df05c5d957b1bfe3d19ba1ad222fd3397ef4b7'

PAGE_TABLE = 0x44848
PAGE_VAR = 0x200044ad        # 0-based current page, written by the vendor flip path
SLOT_OFF = {1: 0x10, 2: 0x40, 3: 0x74, 4: 0xa8, 5: 0xcc, 6: 0xe4}
ORIG_ENTER = {1: 0x117d9, 2: 0x11849, 3: 0x11895, 4: 0x118d1, 5: 0x11915, 6: 0x11abd}

FREE = 0x4aacc            # 214 KB of 0xFF: no branch target, no movw/movt reference
F_RECEIVE = FREE
F_TICK = FREE + 0x500
F_ENSURE = FREE + 0x700
F_DATA = FREE + 0x900

CTX_WORD = 0x2000c610     # start of a 2547 B hole between static references
# The board has only 64 KB of RAM (upper bound 0x20010000), proved by writing a
# marker to 95 words across 0x20000000..0x2001f940: only addresses below that
# bound accepted the write. Both earlier guesses, 0x20011110 and 0x20012208, sat
# above the bound, so they never held anything. This hole is below the bound and
# was confirmed on hardware too: write 0x5A, wait, read back 0x5A.
MILLIS = 0x20003a54
TIMEOUT = 15000
CMD_TABLE = 0x35316
CMD_TBB = 0x352fa          # `tbb [pc,r]` 那条指令；命令表项偏移是在它的地址上算的
CMD_RET = 0x356e8          # 命令处理完跳回去继续跑的位置
CMD_TABLE_REF = 0x35316    # 参考镜像里的原值，用于换算相对位移
CMD_TBB_REF = 0x352fa

MAGIC = 0x5747314e        # 'NWG1'
ALLOC = 0x2637c
TIMER = 0x2c574
GETSCREEN = 0x2b114
LBL_CREATE = 0x25430
LBL_TEXT = 0x2625c
SET_POS = 0x2a6e8
SET_SIZE = 0x2a770
SET_COLOR = 0x2aafc
SET_FONT = 0x2ab0c
MEMCPY = 0x83e4
FONT = 0x45634

C_MAGIC, C_LAST, C_TIMER, C_PAGE = 0, 4, 8, 12
C_LABELS = 36         # 6 x 4 x 4
C_MASK = 132          # live page mask, seeded from the data block, rewritable by command
C_HAVE = 136
C_STALE = 140
C_RECV = 144          # 6 x 4 x 4   per page, per row generation counters
C_APPL = 240          # 6 x 4 x 4   generation currently drawn
C_BUF = 336           # 6 x 4 x 32  per page, per row text rows
CTX_SIZE = 1104
CTX_WORDS = CTX_SIZE // 4

D_WAIT = 0
D_BYE = 16
D_EMPTY = 32
D_MASK = 48           # page mask lives in the data block, rewritable at runtime
D_NONE = 52           # empty string: clears a row when a page leaves the mask
ROW_BYTES = 32        # one text row, per page


def imm(reg, value):
    return f'movw {reg}, #{value & 0xffff}\nmovt {reg}, #{value >> 16}'


def enc_bl(addr, target):
    off = target - (addr + 4)
    if not -0x1000000 <= off < 0x1000000:
        raise ValueError(f'bl 目标超出范围: {addr:#x} -> {target:#x}')
    off &= 0x1ffffff
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    imm10 = (off >> 12) & 0x3ff
    imm11 = (off >> 1) & 0x7ff
    j1 = (1 - (i1 ^ s)) & 1
    j2 = (1 - (i2 ^ s)) & 1
    hw1 = 0xf000 | (s << 10) | imm10
    hw2 = 0xd000 | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack('<HH', hw1, hw2)


def enc_bw(addr, target):
    off = target - (addr + 4)
    if not -0x1000000 <= off < 0x1000000:
        raise ValueError(f'b.w 目标超出范围: {addr:#x} -> {target:#x}')
    off &= 0x1ffffff
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    imm10 = (off >> 12) & 0x3ff
    imm11 = (off >> 1) & 0x7ff
    j1 = (1 - (i1 ^ s)) & 1
    j2 = (1 - (i2 ^ s)) & 1
    hw1 = 0xf000 | (s << 10) | imm10
    hw2 = 0x9000 | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack('<HH', hw1, hw2)


def fix_branches(data, start, text):
    """Keystone mis-encodes every wide branch whose target is an absolute
    address: it lands 0xE..0x10 past the intended function. Re-encode them
    from the targets that are written in the source text."""
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
    wants = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('bl #'):
            wants.append(('bl', int(s[4:].strip(), 0)))
        elif s.startswith('b.w #'):
            wants.append(('b.w', int(s[5:].strip(), 0)))
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    out = bytearray(data)
    seen = []
    for ins in md.disasm(bytes(data), start):
        if ins.mnemonic in ('bl', 'blx'):
            seen.append(('bl', ins.address - start))
        elif ins.mnemonic == 'b.w' and ins.size == 4:
            seen.append(('b.w', ins.address - start))
    if len(seen) != len(wants):
        raise ValueError(f'长跳数量不符：找到 {len(seen)} 条，源码声明 {len(wants)} 条')
    for (kind, off), (want_kind, target) in zip(seen, wants):
        if kind != want_kind:
            raise ValueError(f'{start + off:#x} 处应为 {want_kind}，实际 {kind}')
        addr = start + off
        blob = enc_bl(addr, target) if kind == 'bl' else enc_bw(addr, target)
        out[off:off + 4] = blob
    return bytes(out)


def keystone_ready():
    return Ks is not None


def asm(text, start, end):
    if Ks is None:
        raise ValueError('这台机器没有 keystone：没法把注入层重新汇编成新布局的机器码')
    enc, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(text, addr=start)
    data = fix_branches(bytes(enc), start, text)
    if len(data) > end - start or len(data) % 2:
        raise ValueError(f'代码超出 {start:#x}: {len(data)} > {end - start}')
    return data + bytes.fromhex('00bf') * ((end - start - len(data)) // 2)


def receive_text():
    return f'''
    push {{r4-r7, lr}}
    sub sp, #16
    movs r6, #252
    str r4, [sp, #8]
    ldrb r0, [r5, #2]
    cmp r0, #0
    bne rx_done
    ldrb r0, [r5, #4]
    cmp r0, #0
    bne rx_done
    cmp r4, #4
    blo rx_done
    cmp r4, #53
    bhi rx_done
    ldrb r0, [r5, #5]
    cmp r0, #78
    bne rx_done
    ldrb r0, [r5, #6]
    cmp r0, #55
    bne rx_done
    ldrb r0, [r5, #7]
    cmp r0, #1
    bne rx_done
    movs r6, #253
    {imm('r0', CTX_WORD)}
    ldr r7, [r0]
    cbz r7, rx_new
    ldr r0, [r7]
    {imm('r1', MAGIC)}
    cmp r0, r1
    beq rx_have
rx_new:
    {imm('r0', CTX_SIZE)}
    bl #{ALLOC}
    cmp r0, #0
    beq rx_done
    mov r7, r0
    movs r1, #0
    movs r2, #{CTX_WORDS}
rx_zero:
    str r1, [r0], #4
    subs r2, #1
    bne rx_zero
    {imm('r0', MAGIC)}
    str r0, [r7]
    dmb sy
    {imm('r0', F_DATA + D_MASK)}
    ldr r0, [r0]
    str.w r0, [r7, #{C_MASK}]
    {imm('r0', CTX_WORD)}
    str r7, [r0]
rx_have:
    str r5, [sp]
    str r7, [sp, #12]
    movs r6, #253
    ldrb r2, [r5, #8]
    cmp r2, #0x7f
    bne rx_page
    cmp r4, #7
    bne rx_done
    ldrb r0, [r5, #9]
    cmp r0, #1
    bne rx_done
    ldrb r0, [r5, #10]
    cmp r0, #0x3f
    bhi rx_done
    str r0, [r7, #{C_MASK}]
    dmb sy
    movs r6, #254
    b rx_done
rx_page:
    {imm('r0', PAGE_VAR)}
    ldrb r3, [r0]
    cmp r3, #5
    bhi rx_done
    movs r0, #1
    lsls r0, r0, r3
    ldr r1, [r7, #{C_MASK}]
    tst r0, r1
    beq rx_done
    bl #{GETSCREEN}
    cmp r0, #0
    beq rx_done
    str r0, [sp, #4]
    {imm('r1', PAGE_VAR)}
    ldrb r1, [r1]
    bl #{F_ENSURE}
    movs r6, #254
    ldr r7, [sp, #12]
    ldr r5, [sp]
    ldr r4, [sp, #8]
    cmp r4, #4
    beq rx_done
    movs r6, #252
    cmp r4, #6
    blo rx_done
    ldrb r2, [r5, #8]
    cmp r2, #3
    bhi rx_done
    ldrb r3, [r5, #9]
    cmp r3, #47
    bhi rx_done
    adds r0, r3, #6
    cmp r0, r4
    bne rx_done
    add.w r5, r5, #10
    movs r4, #0
rx_chk:
    cmp r4, r3
    bhs rx_store
    ldrb r0, [r5, r4]
    cmp r0, #32
    blo rx_done
    cmp r0, #126
    bhi rx_done
    adds r4, #1
    b rx_chk
rx_store:
    {imm('r0', PAGE_VAR)}
    ldrb r1, [r0]
    lsls r0, r1, #7
    movs r1, #{ROW_BYTES}
    mul r1, r1, r2
    add r0, r1
    {imm('r4', C_BUF)}
    add r4, r7
    add r4, r0
    movs r0, #0
rx_copy:
    cmp r0, r3
    bhs rx_term
    ldrb r1, [r5, r0]
    strb r1, [r4, r0]
    adds r0, #1
    b rx_copy
rx_term:
    movs r1, #0
    strb r1, [r4, r3]
    {imm('r0', PAGE_VAR)}
    ldrb r1, [r0]
    movs r0, #16
    mul r0, r0, r1
    add r0, r0, r2, lsl #2
    {imm('r4', C_RECV)}
    add r4, r7
    add r4, r0
    ldr r6, [r4]
    adds r6, #1
    str r6, [r4]
    dmb sy
    {imm('r0', MILLIS)}
    ldr r0, [r0]
    str r0, [r7, #{C_LAST}]
    movs r0, #1
    str.w r0, [r7, #{C_HAVE}]
    str.w r0, [r7, #{C_STALE}]
    dmb sy
    movs r6, #254
rx_done:
    mov r0, r6
    add sp, #16
    pop {{r4-r7, lr}}
    strb.w r0, [sp, #0x22]
    b.w #{CMD_RET}
'''


def _tick():
    return f'''
    push {{r4-r7, lr}}
    sub sp, #56
    {imm('r0', CTX_WORD)}
    ldr r4, [r0]
    cmp r4, #0
    beq tk_done
    ldr.w r0, [r4, #{C_HAVE}]
    cmp r0, #0
    beq tk_done
    {imm('r0', PAGE_VAR)}
    ldrb r6, [r0]
    cmp r6, #5
    bhi tk_done
    add.w r0, r4, #{C_LABELS}
    lsls r1, r6, #4
    add r0, r1
    ldr r0, [r0]
    cmp r0, #0
    beq tk_done
    movs r0, #1
    lsls r0, r0, r6
    ldr r1, [r4, #{C_MASK}]
    tst r0, r1
    beq tk_clear
    ldr r0, [r4, #{C_PAGE}]
    subs r0, r0, #1
    cmp r0, r6
    bne tk_done
    {imm('r0', MILLIS)}
    ldr r0, [r0]
    ldr r1, [r4, #{C_LAST}]
    subs r0, r0, r1
    movw r1, #{TIMEOUT}
    cmp r0, r1
    bhs tk_stale
    movs r0, #16
    mul r0, r0, r6
    add.w r5, r4, #{C_RECV}
    add r5, r0
    movs r7, #0
tk_row:
    ldr.w r2, [r5, r7, lsl #2]
    movs r0, #16
    mul r0, r0, r6
    add.w r1, r4, #{C_APPL}
    add r1, r0
    ldr.w r3, [r1, r7, lsl #2]
    cmp r2, r3
    beq tk_row_next
    movs r0, #{ROW_BYTES}
    mul r0, r0, r7
    movs r1, #128
    mul r1, r1, r6
    add r1, r0
    {imm('r0', C_BUF)}
    add r0, r4
    add r0, r1
    mov r1, r0
    mov r0, sp
    movs r2, #{ROW_BYTES}
    bl #{MEMCPY}
    movs r0, #0
    strb.w r0, [sp, #{ROW_BYTES - 1}]
    add.w r0, r4, #{C_LABELS}
    lsls r1, r6, #4
    add r0, r1
    ldr.w r0, [r0, r7, lsl #2]
    cmp r0, #0
    beq tk_row_next
    mov r1, sp
    bl #{LBL_TEXT}
    movs r0, #16
    mul r0, r0, r6
    add.w r1, r4, #{C_APPL}
    add r1, r0
    ldr.w r2, [r5, r7, lsl #2]
    str.w r2, [r1, r7, lsl #2]
tk_row_next:
    adds r7, #1
    cmp r7, #4
    blo tk_row
    b tk_done
tk_clear:
    movs r0, #16
    mul r0, r0, r6
    add.w r1, r4, #{C_APPL}
    add r1, r0
    ldr r0, [r1]
    adds r2, r0, #1
    cmp r2, #0
    beq tk_done
    movs r7, #0
tk_clear_row:
    add.w r0, r4, #{C_LABELS}
    lsls r1, r6, #4
    add r0, r1
    ldr.w r0, [r0, r7, lsl #2]
    cmp r0, #0
    beq tk_clear_next
    {imm('r1', F_DATA + D_NONE)}
    bl #{LBL_TEXT}
tk_clear_next:
    adds r7, #1
    cmp r7, #4
    blo tk_clear_row
    mvn r0, #0
    movs r1, #16
    mul r1, r1, r6
    {imm('r2', C_APPL)}
    add r2, r4
    add r2, r1
    str r0, [r2]
    str r0, [r2, #4]
    str r0, [r2, #8]
    str r0, [r2, #12]
    b tk_done
tk_stale:
    ldr.w r0, [r4, #{C_STALE}]
    cmp r0, #0
    beq tk_done
    movs r0, #0
    str.w r0, [r4, #{C_STALE}]
    movs r7, #0
tk_stale_row:
    add.w r0, r4, #{C_LABELS}
    lsls r1, r6, #4
    add r0, r1
    ldr.w r0, [r0, r7, lsl #2]
    cmp r0, #0
    beq tk_stale_next
    {imm('r1', F_DATA + D_BYE)}
    cmp r7, #0
    bne tk_stale_set
    {imm('r1', F_DATA + D_WAIT)}
tk_stale_set:
    bl #{LBL_TEXT}
tk_stale_next:
    adds r7, #1
    cmp r7, #4
    blo tk_stale_row
    mvn r0, #0
    movs r1, #16
    mul r1, r1, r6
    {imm('r2', C_APPL)}
    add r2, r4
    add r2, r1
    str r0, [r2]
    str r0, [r2, #4]
    str r0, [r2, #8]
    str r0, [r2, #12]
tk_done:
    add sp, #56
    pop {{r4-r7, pc}}
'''


def _ensure():
    return f'''
    push {{r4-r7, lr}}
    sub sp, #4
    mov r4, r0
    mov r5, r1
    {imm('r7', CTX_WORD)}
    ldr r6, [r7]
    cmp r6, #0
    beq en_out
    adds r0, r5, #1
    str r0, [r6, #{C_PAGE}]
    ldr r0, [r6, #{C_TIMER}]
    cmp r0, #0
    bne en_timer
    {imm('r0', F_TICK | 1)}
    movs r1, #250
    movs r2, #0
    bl #{TIMER}
    cmp r0, #0
    beq en_out
    str r0, [r6, #{C_TIMER}]
en_timer:
    add.w r0, r6, #{C_LABELS}
    lsls r1, r5, #4
    add r0, r1
    ldr.w r0, [r0]
    cmp r0, #0
    bne en_out
    movs r7, #0
en_lbl:
    mov r0, r4
    bl #{LBL_CREATE}
    cmp r0, #0
    beq en_out
    add.w r1, r6, #{C_LABELS}
    lsls r2, r5, #4
    add r1, r2
    str.w r0, [r1, r7, lsl #2]
    {imm('r1', F_DATA + D_EMPTY)}
    bl #{LBL_TEXT}
    add.w r0, r6, #{C_LABELS}
    lsls r1, r5, #4
    add r0, r1
    ldr.w r0, [r0, r7, lsl #2]
    movs r1, #20
    movs r2, #70
    mul r2, r2, r7
    adds r2, #30
    bl #{SET_POS}
    add.w r0, r6, #{C_LABELS}
    lsls r1, r5, #4
    add r0, r1
    ldr.w r0, [r0, r7, lsl #2]
    movs r1, #440
    movs r2, #64
    bl #{SET_SIZE}
    add.w r0, r6, #{C_LABELS}
    lsls r1, r5, #4
    add r0, r1
    ldr.w r0, [r0, r7, lsl #2]
    movw r1, #65535
    movs r2, #0
    bl #{SET_COLOR}
    add.w r0, r6, #{C_LABELS}
    lsls r1, r5, #4
    add r0, r1
    ldr.w r0, [r0, r7, lsl #2]
    {imm('r1', FONT)}
    movs r2, #0
    bl #{SET_FONT}
    adds r7, #1
    cmp r7, #4
    blo en_lbl
en_out:
    add sp, #4
    pop {{r4-r7, pc}}
'''


def page_mask(pages):
    mask = 0
    for p in pages:
        mask |= 1 << (p - 1)
    return mask


def visible_symbols():
    """当前生效的厂商地址：默认是参考镜像里的值，可被 relocate() 改写。"""
    names = ('MEMCPY LBL_CREATE LBL_TEXT ALLOC SET_POS SET_SIZE SET_COLOR '
             'SET_FONT GETSCREEN TIMER FONT CMD_RET').split()
    return {n: globals()[n] for n in names}


def relocate(syms=None, free=None):
    """换一套厂商地址（和可选的空闲带起点），返回原来那套，便于还原。"""
    old = visible_symbols()
    old['FREE'] = globals()['FREE']          # 无论改不改，都要记下来给 restore 用
    old['TBB'] = globals()['CMD_TBB']
    old['CMD_TABLE'] = globals()['CMD_TABLE']
    if syms:
        for k, v in syms.items():
            if k == 'FREE':
                continue
            if k == 'TBB':
                globals()['CMD_TBB'] = v
                globals()['CMD_TABLE'] = v + (CMD_TABLE_REF - CMD_TBB_REF)
                continue
            if k not in old:
                raise ValueError(f'未知符号 {k}')
            if not isinstance(v, int) or v & 1:
                raise ValueError(f'{k} 地址必须是偶数整数，收到 {v!r}')
            globals()[k] = v
    if free is not None:
        globals()['FREE'] = free
    return old


def restore(old):
    """把 relocate() 返回的旧值装回去（含由 FREE 派生的四个段起点）。"""
    for k, v in old.items():
        if k in ('FREE', 'TBB'):
            continue
        globals()[k] = v
    base = old.get('FREE')
    if base is not None:
        globals()['FREE'] = base
        globals()['F_RECEIVE'] = base
        globals()['F_TICK'] = base + 0x500
        globals()['F_ENSURE'] = base + 0x700
        globals()['F_DATA'] = base + 0x900
    if old.get('TBB') is not None:
        globals()['CMD_TBB'] = old['TBB']
        globals()['CMD_TABLE'] = old['CMD_TABLE']


def build_code(pages):
    mask = page_mask(pages)
    chunks = [
        ('receive', receive_text(), F_RECEIVE, F_TICK),
        ('tick', _tick(), F_TICK, F_ENSURE),
        ('ensure', _ensure(), F_ENSURE, F_DATA),
    ]
    out = b''
    layout = {}
    for name, text, start, end in chunks:
        blob = asm(text, start, end)
        layout[name] = dict(start=hex(start), end=hex(start + len(blob)), size=len(blob))
        out += blob
    data = (b'Waiting for PC\0'.ljust(16, b'\0')
            + b'PC disconnected\0'.ljust(16, b'\0')
            + b'--\0'.ljust(16, b'\0')
            + struct.pack('<I', mask)
            + bytes(12))
    out += data
    layout['data'] = dict(start=hex(F_DATA), size=len(data), mask=hex(mask))
    return out, layout


def patch(source, pages, strict=True):
    """把覆盖层打进一份 491520 字节的 payload。

    strict=True：只接受参考镜像（原来的行为，给已验证的 profile 用）。
    strict=False：允许任意镜像 —— 前提是调用方已经用 `relocate()` 把厂商函数地址、
                  命令表项、空闲带起点都换成了这份固件上的真值。此时只保留
                  「空闲带必须全 0xFF」「命令项必须还是 0x01f5」这两条物理前提。
    """
    if len(source) != LENGTH:
        raise ValueError('需要完整 491520 字节 payload')
    sha = hashlib.sha256(source[:CODE_LENGTH]).hexdigest()
    if strict and sha != REF_SHA:
        raise ValueError(f'仅支持 reference 代码哈希，收到 {sha}')
    if set(source[FREE - BASE:0x7f000 - BASE]) != {0xff}:
        raise ValueError('空闲区不再全为 0xFF，拒绝写入')
    if struct.unpack_from('<H', source, CMD_TABLE - BASE)[0] != 0x1f5:
        raise ValueError('命令表 0x3D 项与预期不符')

    code, layout = build_code(pages)
    result = bytearray(source)
    result[FREE - BASE:FREE - BASE + len(code)] = code
    result[CMD_TABLE - BASE:CMD_TABLE - BASE + 2] = struct.pack('<H', (F_RECEIVE - (CMD_TBB + 4)) // 2)

    payload = bytes(result)
    diff = [i for i in range(LENGTH) if payload[i] != source[i]]
    sectors = sorted({BASE + (i // 4096) * 4096 for i in diff})
    total = payload + bytes.fromhex('ffff00104255000155464410')
    total += struct.pack('<I', (~zlib.crc32(total)) & 0xffffffff)
    report = dict(status='overlay', input_code_sha256=sha, pages=pages,
                  source_sha256=hashlib.sha256(source).hexdigest(),
                  target_sha256=hashlib.sha256(total).hexdigest(),
                  target_payload_sha256=hashlib.sha256(payload).hexdigest(),
                  changed_bytes=len(diff), sectors=[hex(s) for s in sectors],
                  layout=layout,
                  page_table_untouched=bytes(result[PAGE_TABLE - BASE:PAGE_TABLE - BASE + 72])
                  == bytes(source[PAGE_TABLE - BASE:PAGE_TABLE - BASE + 72]),
                  vendor_enter_untouched=all(
                      bytes(result[a - BASE:a - BASE + 32]) == bytes(source[a - BASE:a - BASE + 32])
                      for a in ORIG_ENTER.values()))
    return total, report


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--pages', default='none',
                    help="默认 none：装上组件但一页都不开，用户自己在程序里选")
    args = ap.parse_args()
    text = args.pages.strip().lower()
    pages = [] if text in ('', 'none', 'off', '0') else sorted({int(x) for x in text.split(',') if x.strip()})
    for p in pages:
        if p not in SLOT_OFF:
            raise SystemExit(f'页码必须在 1..6，收到 {p}')
    fw, rep = patch(args.input.read_bytes(), pages)
    args.out.write_bytes(fw)
    args.out.with_suffix('.json').write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(rep, ensure_ascii=False, indent=2))
