"""Vendor symbol resolution by content signature.

The widget overlay has to *call* vendor functions (`bl 0x2637c` etc.) and read
vendor RAM variables. Those are absolute addresses, so a byte-for-byte copy of
the injected code is only valid on the exact firmware it was built for. Any
firmware whose layout shifted moves those targets, and the copied code would
call into unrelated instructions.

What actually stays put is the *content* of the vendor functions: the same
compiler and the same source produce the same 16 bytes at every function entry,
whichever address the linker placed it at. So the address is recoverable:

    signature = 16 bytes read from the function entry in a known-good image
    address   = where that exact 16-byte blob sits in the new firmware

Measured on this board: all 11 vendor functions have a signature that is unique
across the whole 487 KB code area, in both `66679ff2` (stock) and `8465dd63`
(clean). RAM variables need no signature at all -- they are fixed .bss addresses,
identical across builds; only function *code* relocates.
"""
import json
import struct
from pathlib import Path

# name -> address in the reference image (BASE 0x8000 already applied)
FUNCS = {
    'MEMCPY': 0x83e4,
    'LBL_CREATE': 0x25430,
    'LBL_TEXT': 0x2625c,
    'ALLOC': 0x2637c,
    'SET_POS': 0x2a6e8,
    'SET_SIZE': 0x2a770,
    'SET_COLOR': 0x2aafc,
    'SET_FONT': 0x2ab0c,
    'GETSCREEN': 0x2b114,
    'TIMER': 0x2c574,
    'FONT': 0x45634,
}
BASE = 0x8000
CODE_LENGTH = 487424
SIG_LEN = 16

# 注入层除了调厂商函数，还要碰三处固定结构。它们的地址同样随布局漂移：
#   TBB        命令分发表的入口（`tbb [pc,r]` 那条指令），表本身紧跟其后
#   CMD_RET    命令处理完要跳回去继续跑的位置
#   CMD_ENTRY  0x3D 这一项在表里的位置 = TBB + 0x18（表是连续的，跟着一起漂）
EXTRA = {
    'TBB': 0x352fa,
    'CMD_RET': 0x356e8,
}
CMD_ENTRY_DELTA = 0x35316 - 0x352fa     # 命令表项相对 `tbb [pc,r]` 那条指令的固定偏移


def _payload(img):
    if len(img) == CODE_LENGTH + 16 + 0x1000:
        return img[:CODE_LENGTH]
    return img[:CODE_LENGTH] if len(img) >= CODE_LENGTH else img


def counts(img, blob):
    n, i = 0, img.find(blob)
    while i >= 0:
        n += 1
        i = img.find(blob, i + 1)
    return n


def signature(img, addr, length=SIG_LEN):
    """Read the entry signature of the function at `addr`, widening until unique."""
    for ln in (length, 24, 32, 48):
        blob = img[addr - BASE:addr - BASE + ln]
        if len(blob) < ln:
            continue
        if counts(img, blob) == 1:
            return blob
    raise ValueError(f'{addr:#x} 处找不到全片唯一的入口签名')


def build_signatures(img):
    """name -> {'address', 'signature_hex'} for every vendor function + 结构锚点."""
    out = {}
    for name, addr in list(FUNCS.items()) + list(EXTRA.items()):
        blob = signature(img, addr)
        out[name] = {'address': addr, 'signature_hex': blob.hex()}
    return out


def resolve(img, sigs):
    """name -> address in `img`, matched by signature. Missing symbols are None."""
    found, missing = {}, []
    for name, rec in sigs.items():
        blob = bytes.fromhex(rec['signature_hex'])
        i = img.find(blob)
        if i < 0 or img.find(blob, i + 1) >= 0:
            missing.append(name)
            found[name] = None
        else:
            found[name] = BASE + i
    return found, missing


def cmd_entry(tbb):
    """命令表项地址 = TBB 指令地址 + 表基址偏移（由反汇编现场量出来的固定值）。"""
    return tbb + CMD_ENTRY_DELTA


def free_band(img, minimum=1024):
    """找最大的一块全 0xFF 连续区，返回 (起始地址, 长度)。注入代码往这儿放。"""
    best = (0, 0)
    start = None
    for i, b in enumerate(img[:CODE_LENGTH]):
        if b == 0xFF:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= minimum and i - start > best[1]:
                best = (BASE + start, i - start)
            start = None
    if start is not None:
        n = CODE_LENGTH - start
        if n >= minimum and n > best[1]:
            best = (BASE + start, n)
    return best


def save(path, sigs, source_note=''):
    doc = {'note': source_note, 'signature_bytes': SIG_LEN, 'functions': sigs}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    return doc


def load(path):
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    return doc['functions']


def selftest():
    """Two images of the same board must resolve to their own addresses."""
    import hashlib
    root = Path(__file__).resolve().parent.parent
    cands = [root / 'mcu-before-fullscreen.bin',
             root.parent / 'mcu_out' / 'before_2026-11-02T17-50-51-182Z.bin',
             Path('C:/tmp/rel15/run/orig_synth.bin')]
    imgs = []
    for p in cands:
        if p.exists():
            img = _payload(p.read_bytes())
            if len(img) == CODE_LENGTH:
                imgs.append((p.name, img))
    if len(imgs) < 2:
        print('需要两份镜像才能自检')
        return False
    sigs = build_signatures(imgs[0][1])
    bad = 0
    want = {**FUNCS, **EXTRA}
    for name, img in imgs:
        found, missing = resolve(img, sigs)
        if missing:
            print(f'  FAIL {name}: 缺签名 {missing}')
            bad += 1
            continue
        same = all(found[k] == want[k] for k in want)
        print(f'  {"PASS" if same else "WARN"} {name}: {len(want)}/{len(want)} 解析，'
              f'地址与参考{"一致" if same else "有偏移"}')
    return bad == 0


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        ok = selftest()
        print('PASS' if ok else 'FAIL')
        raise SystemExit(0 if ok else 1)
    root = Path(__file__).resolve().parent.parent
    src = root / 'mcu-before-fullscreen.bin'
    if not src.exists():
        src = Path('C:/tmp/rel15/run/orig_synth.bin')
    img = _payload(src.read_bytes())
    sigs = build_signatures(img)
    out = root / 'firmware' / 'anchors' / 'symbols.json'
    import hashlib
    save(out, sigs, f'来自 {src.name}（代码 sha256 {hashlib.sha256(img).hexdigest()[:16]}）')
    print(f'写入 {out}：{len(sigs)} 个符号签名（11 个厂商函数 + TBB + 命令返回点）')
