"""knob_config.py —— 键盘上方两个旋钮的功能自定义

把「音量旋钮 / 屏幕旋钮 × 正转 / 逆时针 / 按下」这六个动作位，从一组内置功能里
任选一个。生成一份新固件，刷进去就生效，互不牵连。

用法：
    python tools/knob_config.py --list
    python tools/knob_config.py --fw <原厂固件> --out <输出> \
        --vol-cw next --vol-ccw prev --vol-press mute \
        --scr-cw page --scr-ccw page --scr-press calc
    python tools/knob_config.py --read <任意固件>     # 反解当前配置

为什么能这么做（都是真机实验确认的）：
  1. 音量旋钮的三个动作、以及屏幕旋钮的「按下」，最终都汇到固件里的一张
     「动作号 → HID 消费类码」跳转表。这张表在固件里有两份完全相同的副本
     （文件偏移 0x36BE 和 0x3764），两份都必须改，只改一份不生效。
     每个动作位占一个字节，字节值就是表里的跳转偏移。
  2. 屏幕旋钮的左转和右转**不走那张表**：它们在固件代码里是两个固定入口
     0x080116FC（逆时针）和 0x0801171E（顺时针），原厂在这里发一条串口帧
     让屏幕翻页。把它们改写成「发一次按下 + 短延时 + 松开」，就能产生任意动作号。
     ★ 这两处是函数内部的分支入口，改写时必须保持栈平衡：不建自己的帧，
       做完直接跳到原函数出口 0x080117BE，否则键盘会重启。

安全边界：
  · 只写 8 个数据字节（4 个动作位 × 2 份副本）+ 最多 2 段代码
  · 向量表、bootmagic/DFU 扇区、参数区一个字节都不碰
  · 自动重算 DFU 后缀 CRC（crc32 的按位反码）
  · 出厂配置下产物与原厂固件逐字节相同（可用来自检）
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = 0x08000000
PAYLOAD = 118320
SUFFIX = 16

# 只改数据字节、不重写代码段（给「连预生成表都缺失」的极端环境兜底用）
KNOB_BYTES_ONLY = False

# 「动作号 → tbb 字节 → HID 消费类码」，索引 = 动作号 - 0xA8
TBB_BYTES = (
    0x0D, 0x0F, 0x11, 0x13, 0x15, 0x1B, 0x1F, 0x21, 0x1D, 0x24,
    0x27, 0x2A, 0x33, 0x36, 0x39, 0x3C, 0x3F, 0x42, 0x49, 0x17,
    0x19, 0x45, 0x47, 0x2D, 0x30, 0x00,
)
HID_CODES = (
    0x0E2, 0x0E9, 0x0EA, 0x0B5, 0x0B6, 0x0B7, 0x0CD, 0x183, 0x0CC, 0x18A,
    0x192, 0x194, 0x221, 0x223, 0x224, 0x225, 0x226, 0x227, 0x22A, 0x0B3,
    0x0B4, 0x06F, 0x070, 0x19F, 0x1CB, 0x000,
)

# key, 中文标签, 归类
ACTIONS = (
    ('mute', '静音', 'media'),
    ('vol-up', '音量 +', 'media'),
    ('vol-down', '音量 −', 'media'),
    ('next', '下一曲', 'media'),
    ('prev', '上一曲', 'media'),
    ('eject', '弹出光驱', 'media'),
    ('rewind', '快退', 'media'),
    ('vendor-183', '厂商码 0x183', 'vendor'),
    ('fast-forward', '快进', 'media'),
    ('vendor-18a', '厂商码 0x18A', 'vendor'),
    ('calc', '打开计算器', 'app'),
    ('vendor-194', '厂商码 0x194', 'vendor'),
    ('vendor-221', '厂商码 0x221', 'vendor'),
    ('vendor-223', '厂商码 0x223', 'vendor'),
    ('vendor-224', '厂商码 0x224', 'vendor'),
    ('vendor-225', '厂商码 0x225', 'vendor'),
    ('vendor-226', '厂商码 0x226', 'vendor'),
    ('vendor-227', '厂商码 0x227', 'vendor'),
    ('vendor-22a', '厂商码 0x22A', 'vendor'),
    ('play-pause', '播放 / 暂停', 'media'),
    ('stop', '停止', 'media'),
    ('bright-up', '亮度 +', 'bright'),
    ('bright-down', '亮度 −', 'bright'),
    ('mail', '邮件', 'app'),
    ('home', '主页', 'app'),
    ('none', '不做事', 'none'),
)

# 「翻页」只有一个选项。原厂那段代码里带方向判断（cmp r0,#2），同一份字节
# 放到顺时针位就是下一屏、放到逆时针位就是上一屏 —— 拆成两个选项是假的，
# 选「上一屏」放到顺时针位照样会往下一屏翻。
PAGES = (
    ('page', '翻页（原厂行为）'),
)

CATALOG = {}
for i, (key, label, cat) in enumerate(ACTIONS):
    CATALOG[key] = {'key': key, 'label': label, 'cat': cat, 'code': 'action',
                    'action': 0xA8 + i, 'tbb': TBB_BYTES[i], 'hid': HID_CODES[i]}
for key, label in PAGES:
    CATALOG[key] = {'key': key, 'label': label, 'cat': 'screen', 'code': 'page',
                    'action': None, 'tbb': None, 'hid': None}

SITES = ('vol-cw', 'vol-ccw', 'vol-press', 'scr-cw', 'scr-ccw', 'scr-press')

SITE_LABELS = {
    'vol-cw': '音量旋钮 · 顺时针',
    'vol-ccw': '音量旋钮 · 逆时针',
    'vol-press': '音量旋钮 · 按下',
    'scr-cw': '屏幕旋钮 · 顺时针',
    'scr-ccw': '屏幕旋钮 · 逆时针',
    'scr-press': '屏幕旋钮 · 按下',
}

# 出厂状态（官方固件就是这样）
FACTORY = {
    'vol-cw': 'vol-up',
    'vol-ccw': 'vol-down',
    'vol-press': 'mute',
    'scr-cw': 'page',
    'scr-ccw': 'page',
    'scr-press': 'calc',
}

# 走 tbb 表的四个位：文件偏移（副本 0 第 N 格 / 副本 1 第 N 格）
TBB_SITES = {
    'vol-press': (0x36BE, 0x3764),
    'vol-cw': (0x36BF, 0x3765),
    'vol-ccw': (0x36C0, 0x3766),
    'scr-press': (0x36C8, 0x376E),
}

# 走代码的两个位：绝对地址 + 可覆盖字节数
CODE_SITES = {
    'scr-ccw': (0x080116FC, 22),
    'scr-cw': (0x0801171E, 24),
}

# 原厂「屏幕旋钮左右旋转」两段代码的原始字节（官方固件里逐字节抄下来的）。
# 恢复翻页时直接用它们：原厂写法里带着方向翻转判断，自己硬编码方向会在
# 某些状态下把正反转搞反 —— 抄原厂字节，行为与出厂完全一致。
OFFICIAL_CODE = {
    'scr-ccw': bytes.fromhex('487980b30888adf800000098fbf7baff022804d00221'),
    'scr-cw': bytes.fromhex('4879f8b10888adf800000098fbf7a9ff022801d00121ede7'),
}

EPILOGUE = 0x080117BE
PRESS = 0x080122B0
RELEASE = 0x080161B0
PAGE_SEND = 0x0801490C

PROTECTED = ((0x000000, 0x000300), (0x00C000, 0x00D000), (0x01F800, PAYLOAD))


class KnobError(Exception):
    pass


def resolve(key):
    if key not in CATALOG:
        raise KnobError('未知动作：%s' % key)
    return CATALOG[key]


_TABLE = None


def _table():
    """预生成的机器码表（tools/knob_code.json，由 tools/gen_knob_code.py 产生）。

    打包版的 exe 里没有 keystone，也没有 Python，运行时无法汇编；而这两处入口
    的机器码只随「动作号」变化，全部可能值的段数很有限，所以开发时算一次、
    存成 JSON，运行时查表即可。表与生成器一致性由 tests 守护。
    """
    global _TABLE
    if _TABLE is None:
        path = Path(__file__).resolve().parent / 'knob_code.json'
        if not path.exists():
            raise KnobError('缺少 tools/knob_code.json：重装程序，或运行 '
                            'python tools/gen_knob_code.py 重新生成')
        _TABLE = json.loads(path.read_text(encoding='utf-8'))['codes']
    return _TABLE


def _emit(site, act, span, dest, asm=None):
    """生成改写「屏幕旋转」两个入口的机器码。asm 为 None 时查预生成表。"""
    if asm is None:
        per = _table().get(site) or {}
        if act['key'] not in per:
            raise KnobError('%s 没有「%s」的预生成机器码' % (site, act['label']))
        code = bytes.fromhex(per[act['key']])
        if len(code) != span:
            raise KnobError('%s 的预生成机器码长度 %d，应为 %d' % (site, len(code), span))
        return code
    tag = site.replace('-', '_')
    if act['code'] == 'page':
        # 原厂字节：自带方向判断，正反转都对
        return OFFICIAL_CODE[site]
    else:
        no = act['action']
        src = ('movs r0, #0x%02x\nbl #0x%08x\n'
               'movs r2, #0x10\n'
               'k_%s:\nsubs r2, #1\nbne k_%s\n'
               'movs r0, #0x%02x\nbl #0x%08x\nb #0x%08x\n'
               % (no, PRESS, tag, tag, no, RELEASE, EPILOGUE))
    code, _ = asm(src, dest)
    if len(code) > span:
        raise KnobError('%s：生成的代码 %d 字节超出 %d 字节' % (site, len(code), span))
    return bytes(code) + b'\x00\xbf' * ((span - len(code)) // 2)


def build(source, cfg, verbose=True):
    """从原厂（或任意合法）键盘固件生成一份新固件，返回字节串。"""
    # source 可以是路径，也可以直接是一份固件字节（桌面程序手里已经有备份时用）
    if isinstance(source, (bytes, bytearray)):
        data = bytearray(source)
    else:
        data = bytearray(Path(source).read_bytes())
    if len(data) != PAYLOAD + SUFFIX:
        raise KnobError('固件长度不对：%d（需要 %d）' % (len(data), PAYLOAD + SUFFIX))

    asm = None
    if not KNOB_BYTES_ONLY:
        try:
            from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
            ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)

            def asm(src, dest):
                return ks.asm(src, dest)
        except ImportError:
            asm = None      # 回落到 knob_code.json 预生成表

    report = []

    for site, offs in TBB_SITES.items():
        act = resolve(cfg[site])
        if act['code'] != 'action':
            raise KnobError('%s 不接受「%s」：翻页只有屏幕旋钮的左右旋转可用'
                            % (site, act['label']))
        for off in offs:
            old = data[off]
            data[off] = act['tbb']
            report.append('%-10s 0x%05x  %02x -> %02x  %s'
                          % (site, off, old, act['tbb'], act['label']))

    for site, (addr, span) in CODE_SITES.items():
        act = resolve(cfg[site])
        off = addr - BASE
        if act['code'] == 'page':
            blob = OFFICIAL_CODE[site]
        else:
            blob = _emit(site, act, span, addr, asm)
        if len(blob) < span:
            blob = bytes(blob) + b'\x00\xbf' * ((span - len(blob)) // 2)
        if len(blob) != span:
            raise KnobError('%s：代码长度 %d 与预期 %d 不符' % (site, len(blob), span))
        if act['code'] == 'page' and bytes(data[off:off + span]) == bytes(blob):
            # 基线本来就是原厂翻页写法，一个字节都不用动（保持与官方逐字节相同）
            report.append('%-10s 0x%08x  未改动        %s' % (site, addr, act['label']))
            continue
        data[off:off + span] = blob
        report.append('%-10s 0x%08x  %2d/%2d B      %s'
                      % (site, addr, len(blob), span, act['label']))

    crc = zlib.crc32(bytes(data[:len(data) - 4])) & 0xFFFFFFFF
    struct.pack_into('<I', data, len(data) - 4, (~crc) & 0xFFFFFFFF)

    if verbose:
        for line in report:
            sys.stdout.write('  ' + line + '\n')
    return bytes(data)


def write(source, out, cfg):
    blob = build(source, cfg)
    Path(out).write_bytes(blob)
    return blob


def read_config(path):
    """反解六个动作位当前取值（给桌面程序显示用）。"""
    return read_config_from_bytes(Path(path).read_bytes())


def read_config_from_bytes(blob):
    """同一套反解，但直接吃字节（桌面程序手里已经有一份备份时用它）。"""
    data = bytes(blob)
    out = {}
    by_tbb = {a['tbb']: a['key'] for a in CATALOG.values() if a['tbb'] is not None}
    by_action = {a['action']: a['key'] for a in CATALOG.values() if a['action'] is not None}

    for site, offs in TBB_SITES.items():
        b = data[offs[0]]
        out[site] = by_tbb.get(b, 'unknown')

    for site, (addr, _span) in CODE_SITES.items():
        off = addr - BASE
        head = data[off:off + 4]
        if data[off:off + len(OFFICIAL_CODE[site])] == OFFICIAL_CODE[site]:
            # 原厂翻页写法
            out[site] = 'page'
        elif head[1] == 0x20:
            # movs r0,#<动作号>
            out[site] = by_action.get(head[0], 'unknown')
        elif head[2:4] == b'\x39\x20':
            # 自己汇编出来的翻页（历史版本留下的，仍认作翻页）
            out[site] = 'page'
        else:
            out[site] = 'unknown'
    return out


def diff_against_factory(blob, official_path):
    """返回与官方固件不同的字节偏移列表。"""
    ref = Path(official_path).read_bytes()
    return [i for i in range(len(ref)) if blob[i] != ref[i]]


def touched_protected(blob, official_path):
    """返回落在禁区里的改动偏移（正常应为空）。"""
    bad = []
    for i in diff_against_factory(blob, official_path):
        if any(lo <= i < hi for lo, hi in PROTECTED):
            bad.append(i)
    return bad


def catalog_json():
    items = []
    for key, label, cat in ACTIONS:
        a = CATALOG[key]
        items.append({'id': key, 'label': label, 'cat': cat,
                      'action': a['action'], 'hid': a['hid']})
    for key, label in PAGES:
        items.append({'id': key, 'label': label, 'cat': 'screen',
                      'action': None, 'hid': None})
    return items


def _cmd_list():
    sys.stdout.write('可用动作（--vol-cw / --scr-press 等的取值）：\n')
    for key, label, cat in ACTIONS:
        a = CATALOG[key]
        sys.stdout.write('  %-14s %-16s HID=0x%03X  动作号=0x%02X  [%s]\n'
                         % (key, label, a['hid'], a['action'], cat))
    sys.stdout.write('\n仅屏幕旋钮左右旋转可用（保留原厂翻页）：\n')
    for key, label in PAGES:
        sys.stdout.write('  %-14s %s\n' % (key, label))
    sys.stdout.write('\n六个动作位：\n')
    for s in SITES:
        sys.stdout.write('  %-10s %-18s 出厂 = %s\n'
                         % (s, SITE_LABELS[s], CATALOG[FACTORY[s]]['label']))


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--list', action='store_true', help='列出全部可选动作')
    ap.add_argument('--catalog', action='store_true', help='输出 JSON 目录（给桌面程序）')
    ap.add_argument('--read', metavar='FW', help='反解某份固件的当前配置')
    ap.add_argument('--fw', metavar='FW', help='原厂（或合法）键盘固件')
    ap.add_argument('--out', metavar='OUT', help='输出文件')
    for s in SITES:
        ap.add_argument('--' + s, default=None, help=SITE_LABELS[s])
    a = ap.parse_args(argv)

    if a.list:
        _cmd_list()
        return 0
    if a.catalog:
        sys.stdout.write(json.dumps({'actions': catalog_json()}, ensure_ascii=False) + '\n')
        return 0

    if a.read:
        cfg = read_config(a.read)
        for s in SITES:
            sys.stdout.write('  %-10s %-18s %s\n'
                             % (s, SITE_LABELS[s],
                                CATALOG.get(cfg[s], {'label': cfg[s]})['label']
                                if cfg[s] in CATALOG else cfg[s]))
        return 0

    if not a.fw or not a.out:
        sys.stdout.write('需要 --fw 与 --out（或 --list / --read）\n')
        return 2

    cfg = {s: (getattr(a, s.replace('-', '_')) or FACTORY[s]) for s in SITES}
    try:
        blob = build(a.fw, cfg)
    except KnobError as e:
        sys.stdout.write('✗ %s\n' % e)
        return 3

    Path(a.out).write_bytes(blob)
    bad = touched_protected(blob, a.fw)
    if bad:
        sys.stdout.write('✗ 改到了禁区：%s\n' % [hex(i) for i in bad[:8]])
        return 4

    changed = len(diff_against_factory(blob, a.fw))
    sys.stdout.write('输出 %s  %d B  改动 %d 字节  md5 见下\n' % (a.out, len(blob), changed))
    import hashlib
    sys.stdout.write('md5 %s\n' % hashlib.md5(blob).hexdigest())
    return 0


if __name__ == '__main__':
    sys.exit(main())
