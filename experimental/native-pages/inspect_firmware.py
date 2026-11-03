"""Read-only, offline reference screen-MCU inspector. No device libraries."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

BASE, LENGTH, CODE_LENGTH = 0x8000, 491520, 487424
EXPECTED = '8465dd6334eb0dc0d0862477b5df05c5d957b1bfe3d19ba1ad222fd3397ef4b7'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--disassemble', nargs=2, type=lambda v: int(v, 0), metavar=('START', 'END'))
    args = parser.parse_args()
    data = args.input.read_bytes()
    if len(data) != LENGTH or hashlib.sha256(data[:CODE_LENGTH]).hexdigest() != EXPECTED:
        parser.error('地址表仅匹配 reference 全屏+清爽完整 payload；不适用于其他构建。')
    if args.disassemble:
        start, end = args.disassemble
        if not BASE <= start < end <= BASE+CODE_LENGTH or end-start > 0x4000 or start % 2:
            parser.error('需偶数起始地址，单次不超过0x4000字节，且位于已知代码区。')
        md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        for ins in md.disasm(data[start-BASE:end-BASE], start):
            print(f'{ins.address:08x} {ins.bytes.hex():10} {ins.mnemonic:10} {ins.op_str}')
        return
    pages = []
    for i in range(6):
        slot, creator, enter = struct.unpack_from('<III', data, 0x44848-BASE+i*12)
        pages.append(dict(page=i+1, table=hex(0x44848+i*12), root_slot=hex(slot),
                          creator=hex(creator), enter=hex(enter)))
    print(json.dumps(dict(code_sha256=EXPECTED, address_base=hex(BASE), pages=pages,
                         page_switch='0x3c128', startup_flags='0x1ba20',
                         media_upload_completion='0x31c58', storage_writers=['0x38070','0x3816c','0xa68c'],
                         static_image_storage='0x70001000', page5_load='0x11914', gif_tick='0x33ac8',
                         vendor_command_parser='0x35220', unused_3d_switch='0x35316'), indent=2))


if __name__ == '__main__':
    main()
