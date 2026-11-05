"""桥接层（desktop/dash_bridge.py）的旋钮部分：必须能在进程内工作。

现场踩过的坑：打包成 exe 之后 `_python()` 返回的是 exe 自己，于是
`[exe, 'tools/knob_config.py', '--catalog']` 被当成程序自身的参数，动作目录
拿不到，界面里六个下拉框全空 —— 而源码运行时完全正常，测试也就发现不了。
所以这里断言「不经过子进程也能拿到目录 / 反解 / 生成」。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'desktop'))
sys.path.insert(0, str(ROOT / 'tools'))

import dash_bridge                                  # noqa: E402
import knob_config as K                             # noqa: E402

SIZE = K.PAYLOAD + K.SUFFIX


def _official():
    import hashlib
    for p in (ROOT / 'firmware' / 'keyboard-original.bin',
              ROOT.parent.parent / 'fw_all' / ('18564_AULA-Neos75_V2.0_stable_SI-2915_ZB48A1UBG2'
                                        '_V20_5125_KB84_RGB_LCD_SV_20261027.bin')):
        if p.exists() and p.stat().st_size == SIZE and \
                hashlib.md5(p.read_bytes()).hexdigest() == '24b5d4a6d1a53d6a7a93b20ccef9a416':
            return p
    raise unittest.SkipTest('no factory keyboard firmware')


class Catalog(unittest.TestCase):
    def test_catalog_never_shells_out(self):
        with mock.patch.object(dash_bridge, '_run', side_effect=AssertionError(
                'catalog 不该再走子进程')):
            data = dash_bridge.knob_catalog()
        self.assertEqual(len(data['sites']), 6)
        self.assertGreaterEqual(len(data['actions']), 26)
        ids = [a['id'] for a in data['actions']]
        self.assertEqual(len(ids), len(set(ids)), '目录里不该有重复动作')
        self.assertIn('page', ids)
        # 每个出厂值都必须在目录里，否则界面一打开就显示空
        for site in data['sites']:
            self.assertIn(site['factory'], ids, '%s 的出厂值不在目录里' % site['id'])

    def test_sites_and_defaults_agree(self):
        self.assertEqual(tuple(dash_bridge.KNOB_SITES), K.SITES)
        self.assertEqual(dash_bridge.KNOB_DEFAULTS, K.FACTORY)


class Build(unittest.TestCase):
    def test_build_in_process_matches_the_generator(self):
        src = _official()
        cfg = {'vol-cw': 'next', 'vol-ccw': 'prev', 'vol-press': 'prev',
               'scr-cw': 'page', 'scr-ccw': 'page', 'scr-press': 'next'}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'k.bin'
            res = dash_bridge.knob_build(src, out, cfg)
            self.assertEqual(out.stat().st_size, SIZE)
            self.assertEqual(out.read_bytes(), K.build(src, cfg, verbose=False),
                             '桥接层与生成器必须产出同一份字节')
            self.assertTrue(any('md5' in ln for ln in res['log']))

    def test_unknown_action_is_refused_not_silently_defaulted(self):
        src = _official()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'k.bin'
            with self.assertRaises(dash_bridge.DashboardError):
                dash_bridge.knob_build(src, out, {'vol-cw': 'nope', 'vol-ccw': 'vol-down',
                                                  'vol-press': 'mute', 'scr-cw': 'page',
                                                  'scr-ccw': 'page', 'scr-press': 'calc'})
            self.assertFalse(out.exists(), '失败时不该留下半成品')

    def test_read_back_uses_the_generator(self):
        src = _official()
        got = dash_bridge.knob_read(src)['config']
        self.assertEqual(got, K.read_config(src))


if __name__ == '__main__':
    unittest.main()
