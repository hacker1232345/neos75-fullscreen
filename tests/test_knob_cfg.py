"""tests/test_knob_config.py —— 旋钮配置生成器的回归测试（不碰 USB）

覆盖：
  * 出厂配置产物与官方固件逐字节相同
  * 六个动作位 × 全部合法动作，各生成一次：长度恒定、反解一致、禁区未动、
    两份 tbb 副本同步、代码段首条指令正确、CRC 自洽
  * 非法组合被拒（音量位不接受翻页、未知动作名、长度不对的镜像）
  * read_config 能反解官方固件与三种屏幕状态
"""
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'tools') not in sys.path:
    sys.path.insert(0, str(ROOT / 'tools'))

import knob_config as K

SIZE = K.PAYLOAD + K.SUFFIX

# 原厂键盘固件的 md5（E1..E10 每一版都改过它，不能拿改过的备份当基准）
OFFICIAL_MD5 = '24b5d4a6d1a53d6a7a93b20ccef9a416'

CANDIDATES = (
    ROOT / 'firmware' / 'keyboard-original.bin',
    ROOT.parent.parent / 'fw_all' / ('18564_AULA-Neos75_V2.0_stable_SI-2915_ZB48A1UBG2'
                              '_V20_5125_KB84_RGB_LCD_SV_20261027.bin'),
)


def _official():
    """只接受 md5 与官方一致的固件，避免拿改装过的镜像当基准。"""
    import hashlib
    for p in CANDIDATES:
        if not p.exists() or p.stat().st_size != SIZE:
            continue
        if hashlib.md5(p.read_bytes()).hexdigest() == OFFICIAL_MD5:
            return p
    raise unittest.SkipTest(
        'find no factory keyboard firmware (md5 %s), skip' % OFFICIAL_MD5)


class Factory(unittest.TestCase):
    def test_factory_config_reproduces_the_official_image_byte_for_byte(self):
        src = _official()
        ref = src.read_bytes()
        blob = K.build(src, K.FACTORY, verbose=False)
        self.assertEqual(len(blob), SIZE)
        self.assertEqual(blob, ref, 'factory config must equal the official image')

    def test_read_config_on_the_official_image_gives_the_factory_config(self):
        self.assertEqual(K.read_config(_official()), K.FACTORY)


class EverySlot(unittest.TestCase):
    def test_every_action_in_every_slot_round_trips(self):
        src = _official()
        ref = src.read_bytes()
        tmp = ROOT / 'build' / '_knob_rt.bin'
        tmp.parent.mkdir(parents=True, exist_ok=True)

        for site in K.SITES:
            keys = [a['key'] for a in K.CATALOG.values()
                    if a['code'] == 'action' or site in K.CODE_SITES]
            for key in keys:
                cfg = dict(K.FACTORY)
                cfg[site] = key
                with self.subTest(site=site, key=key):
                    blob = K.build(src, cfg, verbose=False)
                    self.assertEqual(len(blob), SIZE)

                    tmp.write_bytes(blob)
                    self.assertEqual(K.read_config(tmp)[site], key,
                                     'read back must return the same action')
                    self.assertEqual(K.touched_protected(blob, src), [],
                                     'vectors / bootmagic / params untouched')

                    for other, offs in K.TBB_SITES.items():
                        self.assertEqual(blob[offs[0]], blob[offs[1]],
                                         '%s two copies must agree' % other)

                    calc = zlib.crc32(blob[:-4]) & 0xFFFFFFFF
                    stored = struct.unpack_from('<I', blob, len(blob) - 4)[0]
                    self.assertEqual((~calc) & 0xFFFFFFFF, stored,
                                     'DFU suffix CRC must be self-consistent')

                    if site in K.CODE_SITES:
                        addr, _span = K.CODE_SITES[site]
                        off = addr - K.BASE
                        act = K.CATALOG[key]
                        if act['code'] == 'page':
                            # 翻页只有一种，写回去的就是原厂那一段
                            self.assertEqual(bytes(blob[off:off + _span]),
                                             K.OFFICIAL_CODE[site])
                        else:
                            self.assertEqual(blob[off:off + 2],
                                             bytes([act['action'], 0x20]))

        self.assertEqual(ref, src.read_bytes(), 'source must stay untouched')


class Reject(unittest.TestCase):
    def test_the_volume_slots_refuse_paging(self):
        src = _official()
        for site in K.TBB_SITES:
            for key, _label in K.PAGES:
                with self.subTest(site=site, key=key):
                    cfg = dict(K.FACTORY)
                    cfg[site] = key
                    with self.assertRaises(K.KnobError):
                        K.build(src, cfg, verbose=False)

    def test_an_unknown_action_is_refused(self):
        src = _official()
        cfg = dict(K.FACTORY)
        cfg['vol-cw'] = 'not-a-thing'
        with self.assertRaises(K.KnobError):
            K.build(src, cfg, verbose=False)

    def test_a_wrongly_sized_image_is_refused(self):
        src = _official()
        tmp = ROOT / 'build' / '_knob_bad.bin'
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(bytes(SIZE - 8))
        with self.assertRaises(K.KnobError):
            K.build(tmp, K.FACTORY, verbose=False)


class Catalog(unittest.TestCase):
    def test_catalog_has_26_actions_plus_two_pages(self):
        items = K.catalog_json()
        self.assertEqual(len(items), len(K.ACTIONS) + len(K.PAGES))
        acts = [i for i in items if i['action'] is not None]
        self.assertEqual(len(acts), 26)
        self.assertEqual([i['action'] for i in acts], list(range(0xA8, 0xA8 + 26)))
        for i in acts:
            idx = i['action'] - 0xA8
            self.assertEqual(i['hid'], K.HID_CODES[idx])
            self.assertEqual(K.CATALOG[i['id']]['tbb'], K.TBB_BYTES[idx])


class ReadBack(unittest.TestCase):
    def test_read_config_distinguishes_the_three_screen_states(self):
        src = _official()
        tmp = ROOT / 'build' / '_knob_rb.bin'
        tmp.parent.mkdir(parents=True, exist_ok=True)

        cfg = dict(K.FACTORY)
        tmp.write_bytes(K.build(src, cfg, verbose=False))
        for site in ('scr-cw', 'scr-ccw'):
            self.assertEqual(K.read_config(tmp)[site], 'page')

        for site in ('scr-cw', 'scr-ccw'):
            for key in ('next', 'prev'):
                cfg = dict(K.FACTORY, **{site: key})
                tmp.write_bytes(K.build(src, cfg, verbose=False))
                self.assertEqual(K.read_config(tmp)[site], key,
                                 '%s 设成 %s 后应当读回 %s' % (site, key, key))

        # 再退回翻页，必须能真正退回原厂字节
        cfg = dict(K.FACTORY, **{'scr-cw': 'next', 'scr-ccw': 'prev'})
        tmp.write_bytes(K.build(src, cfg, verbose=False))
        cfg = dict(K.FACTORY)
        tmp.write_bytes(K.build(src, cfg, verbose=False))
        for site in ('scr-cw', 'scr-ccw'):
            self.assertEqual(K.read_config(tmp)[site], 'page')


class RevertingToPage(unittest.TestCase):
    """把已经改成动作代码的屏幕旋钮退回原厂翻页。

    现场踩过的坑：生成器原来写的是「配置等于出厂值就整段跳过」，于是基线
    已经被改成动作代码时，选回「翻页」会留下旧的切歌行为 —— 屏幕旋钮转起来
    还是切歌，退不回翻页。现在改成「翻页一律写回原厂那 22/24 字节」。
    """

    def test_page_survives_a_baseline_that_was_already_patched(self):
        src = _official()
        ref = src.read_bytes()
        stepped = dict(K.FACTORY, **{'scr-cw': 'next', 'scr-ccw': 'prev'})
        patched = K.build(src, stepped, verbose=False)
        self.assertEqual(K.read_config_from_bytes(patched), stepped)
        for site, (addr, span) in K.CODE_SITES.items():
            off = addr - K.BASE
            self.assertNotEqual(bytes(patched[off:off + span]),
                                K.OFFICIAL_CODE[site],
                                '%s 应当已被改成动作代码' % site)
        back = K.build(patched, dict(K.FACTORY), verbose=False)
        self.assertEqual(K.read_config_from_bytes(back), K.FACTORY)
        for site, (addr, span) in K.CODE_SITES.items():
            off = addr - K.BASE
            self.assertEqual(bytes(back[off:off + span]), K.OFFICIAL_CODE[site],
                             '%s 必须写回原厂字节' % site)
        self.assertEqual(bytes(back), ref, '退回出厂配置后应与官方固件逐字节相同')

    def test_action_code_starts_with_the_action_number(self):
        for site, (addr, span) in K.CODE_SITES.items():
            for key, act in K.CATALOG.items():
                if act['code'] != 'action':
                    continue
                blob = K._emit(site, act, span, addr, None)
                self.assertEqual(len(blob), span, '%s/%s 长度必须是 %d' % (site, key, span))
                self.assertEqual(bytes(blob[:2]), bytes([act['action'] & 0xff, 0x20]),
                                 '%s/%s 首条应当是 movs r0,#0x%02x' % (site, key, act['action']))
                core = bytes(blob)
                while core.endswith(b'\x00\xbf'):
                    core = core[:-2]
                self.assertGreater(len(core), 8, '%s/%s 不该整段都是填充' % (site, key))
                # 末尾必须是跳回出口的无条件分支：16 位 B 的编码是 0b11100xxx。
                hw = core[-2] | (core[-1] << 8)
                self.assertEqual(hw >> 11, 0b11100,
                                 '%s/%s 末尾应当是跳回出口的无条件分支（得到 0x%04x）'
                                 % (site, key, hw))

if __name__ == '__main__':
    unittest.main()
