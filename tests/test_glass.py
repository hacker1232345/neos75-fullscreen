"""UI orchestration acceptance with a fake device; never opens USB."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'desktop'))
from glass import Api, load_renderer
import glass


class PortableSampler(unittest.TestCase):
    def test_missing_cpu_frequency_keeps_other_metrics_available(self):
        # psutil does not expose cpu_freq on Apple Silicon.
        with patch.object(glass.sys, 'platform', 'darwin'), patch.dict(glass.psutil.__dict__):
            glass.psutil.__dict__.pop('cpu_freq', None)
            sampler = glass.Sampler()
            self.addCleanup(sampler.close)
            metrics = sampler.read()
            self.assertIsNone(metrics['cpu_freq_ghz'])
            self.assertIsInstance(metrics['cpu_load'], (float, int))
            self.assertGreater(metrics['mem_total_gb'], 0)


class GlassWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Mock(VERSION='test')
        self.report = dict(complete=True, bytes=491520, appReturned=True,
                           firmwareKind='supported-original', patchable=True)
        self.order = []
        def node(script, args=(), **kwargs):
            self.order.append(script)
            if script.endswith('mcu_backup.js'):
                Path(args[0]).write_bytes(bytes(491520))
                return json.dumps(self.report)
            return '{}'
        def generate(source, target, **kwargs):
            self.order.append('generate')
            data = bytearray(Path(source).read_bytes())
            data[32768] = 1
            Path(target).write_bytes(data + bytes(16))
            return {'force': kwargs['force']}
        self.core.node_run.side_effect = node
        self.core.generate.side_effect = generate
        self.core.maintain.side_effect = lambda *a, **k: self.order.append(a[0])
        self.sampler = Mock()
        self.sampler.read.return_value = {'clock': '12:34:56', 'cpu_load': 20}
        self.api = Api(self.temp.name, self.core, sampler=self.sampler, sources=Mock(read=Mock(return_value={})))

    def wait(self):
        self.api._thread.join(4)
        self.assertFalse(self.api._thread.is_alive())

    def test_no_write_on_boot_or_unconfirmed_action(self):
        self.api.bootstrap()
        self.api.snapshot()
        self.assertFalse(self.api.patch()['ok'])
        self.assertFalse(self.api.restore()['ok'])
        self.core.node_run.assert_not_called()
        self.core.maintain.assert_not_called()

    def test_one_click_backs_up_generates_flashes_verifies_then_leaves(self):
        self.assertTrue(self.api.patch(True)['ok'])
        self.wait()
        self.assertEqual(self.order, ['tools/mcu_backup.js', 'generate', 'flash', 'tools/leave_mcu.js'])
        self.assertEqual(self.api.snapshot()['state']['phase'], 'done')
        self.assertFalse(self.core.maintain.call_args.kwargs['force'])
        self.assertEqual(len(list(Path(self.temp.name).rglob('original.bin'))), 1)

    def test_unknown_firmware_is_still_adapted_instead_of_refused(self):
        # v1.7 起「版本不认识」不再是终点：备份完之后仍然生成，偏移由内容锚点现算。
        # 老版本在这里直接报「固件不匹配」把用户挡在外面，只能去勾「无验证」，
        # 结果进度条留在了屏幕上 —— 这条用例就是防止那个行为回来。
        self.report.update(patchable=False, firmwareKind='unsupported-version')
        self.api.patch(True)
        self.wait()
        self.assertIn('generate', self.order)
        self.assertEqual(self.order, ['tools/mcu_backup.js', 'generate', 'flash', 'tools/leave_mcu.js'])
        self.assertEqual(self.api.snapshot()['state']['phase'], 'done')

    def test_unadaptable_firmware_keeps_the_backup_and_never_writes(self):
        # 适配器一个字节都套不上时（generate 抛错），必须停在生成这一步：
        # 不刷、不擦，备份留着，并且报错要说清是哪一层卡住。
        self.report.update(patchable=False, firmwareKind='unsupported-version')
        def refuse(source, target, **kwargs):
            self.order.append('generate')
            raise ValueError('这份固件把小组件要用到的那几个厂商函数改过了（签名对不上）')
        self.core.generate.side_effect = refuse
        self.api.patch(True)
        self.wait()
        self.assertEqual(self.order, ['tools/mcu_backup.js', 'generate'])
        self.core.maintain.assert_not_called()
        self.assertEqual(self.api.snapshot()['state']['phase'], 'error')
        self.assertIn('签名对不上', self.api.snapshot()['state']['message'])
        self.assertTrue(list(Path(self.temp.name).rglob('original.bin')))

    def test_incomplete_backup_and_already_current_never_write(self):
        for update in [dict(complete=False), dict(complete=True, firmwareKind='already-fullscreen')]:
            self.report.update(update)
            self.api.patch(True)
            self.wait()
        self.core.generate.assert_not_called()
        self.core.maintain.assert_not_called()

    def test_failed_flash_retains_recovery_and_never_leaves_dfu(self):
        self.core.maintain.side_effect = RuntimeError('write failed')
        self.api.patch(True)
        self.wait()
        self.assertTrue(self.api.snapshot()['state']['restore'])
        self.assertNotIn('tools/leave_mcu.js', self.order)
        self.assertFalse(self.api.snapshot()['state']['busy'])

    def test_tasks_exclusive_closing_and_configuration_blocked_during_flash(self):
        started, release = threading.Event(), threading.Event()
        def hold(*args, **kwargs):
            started.set()
            release.wait(3)
        self.core.maintain.side_effect = hold
        self.api.patch(True)
        self.assertTrue(started.wait(2))
        self.assertFalse(self.api.start()['ok'])
        self.assertFalse(self.api.configure({'widgets': []})['ok'])
        self.assertFalse(self.api._closing())
        release.set()
        self.wait()

    def test_free_selection_reorder_empty_persist_and_invalid_rejection(self):
        desired = {'widgets':['mem','cpu','clock_big'], 'interval':3}
        self.assertTrue(self.api.configure(desired)['ok'])
        other = Api(self.temp.name, self.core, sampler=self.sampler)
        saved = other.bootstrap()['config']
        # Persistence round-trips the chosen widgets and interval; the native-channel
        # keys keep their defaults because this configuration never set them.
        self.assertEqual({k: saved[k] for k in ('widgets', 'interval')}, desired)
        self.assertEqual(saved['native_pages'], [])
        self.assertEqual(saved['native_interval'], 3)
        for ids in [['wrong'], ['cpu','cpu'], 'cpu']:
            self.assertFalse(other.configure({'widgets': ids})['ok'])
        self.assertTrue(other.configure({'widgets':[]})['ok'])
        self.assertFalse(other.start()['ok'])
        self.assertEqual(other._renderer.resolve_widgets([]), [])

    def test_paginated_preview_baseline_jpeg_and_no_usb(self):
        ids = [w['id'] for w in self.api.bootstrap()['registry']['widgets']]
        self.api.configure({'widgets':ids})
        self.api.preview()
        self.wait()
        self.assertGreater(len(self.api.snapshot()['previews']), 1)
        pages = self.api._renderer.paginate(ids)
        self.assertEqual([w for page in pages for w in page], ids)
        for page in pages:
            boxes, info = self.api._renderer.layout(page)
            self.assertFalse(info['scaled'])
            self.assertFalse(info['overflow'])
            for box in boxes:
                self.assertGreaterEqual(box['h'], self.api._renderer.WMAP[box['id']]['minHeight'])
            data, img = self.api._renderer.render({}, widgets=page)
            self.assertEqual(img.size, (480,360))
            self.assertNotIn(b'\xff\xc2', data)  # no progressive SOF marker
        self.core.node_run.assert_not_called()

    def test_stream_stop_waits_for_current_send_and_releases_task(self):
        started, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            started.set()
            release.wait(3)
        self.core.dash.return_value.push_once.side_effect = send
        self.api.start()
        self.assertTrue(started.wait(2))
        self.api.stop()
        self.assertFalse(self.api.configure({'widgets':[]})['ok'])
        release.set()
        self.wait()
        self.assertFalse(self.api.snapshot()['state']['stream'])
        self.assertEqual(self.core.dash.return_value.push_once.call_count, 1)

    def test_restore_checks_unmodified_regions_before_writing(self):
        self.api.patch(True)
        self.wait()
        self.core.maintain.reset_mock()
        old = self.core.node_run.side_effect
        def different(script, args=(), **kwargs):
            result = old(script, args, **kwargs)
            if script.endswith('mcu_backup.js'):
                image = bytearray(Path(args[0]).read_bytes())
                image[-1] = 8  # different device configuration outside protected patch groups
                Path(args[0]).write_bytes(image)
            return result
        self.core.node_run.side_effect = different
        self.api.restore(True)
        self.wait()
        self.core.maintain.assert_not_called()
        self.assertTrue(self.api.snapshot()['state']['error'])

    def test_invalid_saved_recovery_target_rejected_before_usb_access(self):
        self.api.patch(True)
        self.wait()
        self.core.node_run.reset_mock()
        self.core.maintain.reset_mock()
        self.core.validate_firmware.side_effect = ValueError('saved target changed')
        self.api.restore(True)
        self.wait()
        self.core.node_run.assert_not_called()
        self.core.maintain.assert_not_called()


    def test_weather_settings_persist_without_device_access(self):
        city = dict(label='测试城市', latitude=30, longitude=120)
        config = dict(widgets=['weather', 'codex'], weather=city, codex_thread='abcdefgh')
        self.assertTrue(self.api.configure(config)['ok'])
        other = Api(self.temp.name, self.core, sampler=self.sampler)
        self.assertEqual(other.bootstrap()['config']['weather'], city)
        self.assertEqual(other.bootstrap()['config']['codex_thread'], 'abcdefgh')
        self.core.node_run.assert_not_called()

    def test_fixed_page_changes_between_transfers_and_navigation_is_exclusive(self):
        ids = [w['id'] for w in self.api.bootstrap()['registry']['widgets']]
        self.api.configure(dict(widgets=ids))
        sent = []
        def send(*args, **kwargs):
            sent.append(Path(kwargs['image']).read_bytes())
            if len(sent) == 1:
                self.assertFalse(self.api.screen_page('next', True)['ok'])
                self.assertTrue(self.api.select_page(1)['ok'])
            else:
                self.api.stop()
        self.core.dash.return_value.push_once.side_effect = send
        with patch.object(self.api, '_make_frames', return_value=[b'first', b'second']), patch.object(self.api._stop, 'wait', return_value=False):
            self.api.start()
            self.wait()
        self.assertEqual(sent, [b'first', b'second'])
        self.assertFalse(self.api.select_page(999)['ok'])
        self.assertFalse(self.api.screen_page('next')['ok'])
        self.assertTrue(self.api.screen_page('previous', True)['ok'])
        self.wait()
        self.assertEqual(self.core.node_run.call_args.args, ('wallpaper/pages.js', ['previous', '--yes']))

    # --- the native text channel: RAM only, never Flash, never DFU -----------

    def test_native_pages_must_be_a_unique_subset_of_six(self):
        base = dict(widgets=['cpu'])
        self.assertTrue(self.api.configure(dict(base, native_pages=[2, 4, 6]))['ok'])
        for bad in ([0], [7], [1, 1], [-1], ['2'], None if False else [1, 'x']):
            self.assertFalse(self.api.configure(dict(base, native_pages=bad))['ok'], bad)
        self.assertTrue(self.api.configure(dict(base, native_pages=[]))['ok'])
        for bad in (0, 1, 4, 60, 'x'):
            self.assertFalse(self.api.configure(dict(base, native_interval=bad))['ok'], bad)

    def test_native_preview_folds_the_selected_widgets_without_usb(self):
        self.assertTrue(self.api.configure({'widgets': ['clock_big', 'cpu', 'mem', 'history_cpu']})['ok'])
        self.api.native_preview()
        self.wait()
        rows = self.api.snapshot()['text']
        self.assertEqual(len(rows), 4)
        self.assertIn('CPU', ' '.join(rows))
        self.assertTrue(any('12:34:56' in row for row in rows))
        self.assertFalse(self.api.snapshot()['state']['native'])
        self.core.node_run.assert_not_called()
        self.core.dash.return_value.native_once.assert_not_called()

    def test_native_start_needs_at_least_one_page(self):
        """连续发送必须有页面可写；空选只用于「关闭」，不能开通道。"""
        self.assertTrue(self.api.configure({'widgets': ['cpu']})['ok'])
        self.api.native_start()
        self.wait()
        self.assertEqual(self.api.snapshot()['state']['phase'], 'error')
        self.core.dash.return_value.native_popen.assert_not_called()

    def test_native_off_switch_writes_mask_zero(self):
        """清空页选择后应用 = 掩码 0 = 干净的关闭，而不是报错。"""
        self.assertTrue(self.api.configure({'widgets': ['cpu'], 'native_pages': [1, 2, 3]})['ok'])
        self.assertTrue(self.api.configure({'widgets': ['cpu'], 'native_pages': []})['ok'])
        self.assertEqual(self.api._native_mask(), 0)
        self.core.dash.return_value.native_mask.return_value = {'masks': 1, 'displayVerified': False}
        self.assertTrue(self.api.native_pages()['ok'])
        self.wait()
        self.assertEqual(self.core.dash.return_value.native_mask.call_args.args, (0,))
        state = self.api.snapshot()['state']
        self.assertFalse(state['native'])
        self.assertFalse(state['nativeReady'])
        self.assertEqual(state['phase'], 'done')
        self.assertEqual(state['title'], '文字组件已关闭')

    def test_native_pages_writes_only_the_mask(self):
        self.assertTrue(self.api.configure({'widgets': ['cpu'], 'native_pages': [2, 4, 6]})['ok'])
        self.core.dash.return_value.native_mask.return_value = {'masks': 1, 'displayVerified': False}
        self.assertTrue(self.api.native_pages()['ok'])
        self.wait()
        self.assertEqual(self.core.dash.return_value.native_mask.call_args.args, (0b0101010,))
        self.assertEqual(self.api.snapshot()['state']['nativeSent'], 1)
        self.core.dash.return_value.native_once.assert_not_called()

    def test_native_push_sends_four_rows_and_never_rewrites_the_mask(self):
        self.assertTrue(self.api.configure({'widgets': ['cpu'], 'native_pages': [3]})['ok'])
        self.core.dash.return_value.native_once.return_value = {'frames': 1, 'masks': 0, 'displayVerified': False}
        self.assertTrue(self.api.native_push()['ok'])
        self.wait()
        rows = self.core.dash.return_value.native_once.call_args.args[0]
        self.assertEqual(len(rows), 4)
        # A fixed frame must not carry a mask: that would retarget pages behind the user's back.
        self.assertNotIn('mask', self.core.dash.return_value.native_once.call_args.kwargs)
        self.assertEqual(self.api.snapshot()['state']['nativeSent'], 1)

    def test_native_stream_reopens_the_channel_and_survives_rejected_frames(self):
        self.assertTrue(self.api.configure({'widgets': ['cpu'], 'native_pages': [1, 2]})['ok'])
        # 设备先拒一帧、再收一帧，之后继续反复拒 —— 这不代表通道坏了，而是「当前界面
        # 没有组件」。断言只看「被拒之后仍然继续推、并且成功过一帧」，不看具体帧数：
        # 计数随后台线程推进，盯死数字就成了跟调度赛跑（macOS arm64 上必挂）。
        replies = [{'ok': True}, {'error': '该页尚未初始化'}, {'ok': True}]
        rejected = []

        def write(process, payload):
            if 'rows' in payload:
                self.assertEqual(len(payload['rows']), 4)
            else:
                self.assertEqual(payload, {'mask': 0b11})
            if replies:
                reply = replies.pop(0)
            else:
                reply = {'error': '该页尚未初始化'}
            if reply.get('error'):
                rejected.append(reply['error'])
            return reply

        def fake_popen():
            return Mock(stdin=Mock(), stdout=Mock())

        self.core.dash.return_value.native_popen.side_effect = fake_popen
        # Pace the loop by hand so the test does not sit through the real interval.
        def pace(seconds):
            time.sleep(0.01)
            return False
        patch_stop = patch.object(self.api._stop, 'wait', side_effect=pace)
        with patch.object(self.api, '_write_native', side_effect=write), patch_stop:
            self.assertTrue(self.api.native_start()['ok'])
            for _ in range(200):
                if self.api.snapshot()['state']['nativeSent'] >= 1 and rejected:
                    break
                time.sleep(0.01)
            self.assertGreaterEqual(self.api.snapshot()['state']['nativeSent'], 1)
            self.assertTrue(rejected, '被拒的那一帧必须被跳过而不是中断会话')
            # 会话仍然活着：还在 native 阶段，没有被错误打成 error。
            self.assertEqual(self.api.snapshot()['state']['phase'], 'native')
            self.api.stop()
            self.wait()
        self.core.dash.return_value.native_close.assert_called_once()
        self.assertEqual(self.api.snapshot()['state']['phase'], 'idle')
        self.assertFalse(self.api.snapshot()['state']['native'])


    # --- 四套配色：对应官方四种颜色 -----------------------------------------

    def test_theme_defaults_to_hades_and_rejects_unknown(self):
        self.assertEqual(self.api.bootstrap()['config']['theme'], 'hades')
        names = [t['id'] for t in self.api.bootstrap()['registry']['themes']]
        self.assertEqual(names, ['hades', 'selene', 'venus', 'athena'])
        for name in names:
            self.assertTrue(self.api.configure({'widgets': ['cpu'], 'theme': name})['ok'], name)
            self.assertEqual(self.api._config['theme'], name)
        # 真正错的名字要拒绝；大小写和空格只是写法差异，归一化后应照常接受。
        for bad in ('pink', 'gray', 'hades2', 5, True):
            self.assertFalse(self.api.configure({'widgets': ['cpu'], 'theme': bad})['ok'], bad)
        # 大小写、首尾空格和空串只是写法差异，归一化后应当照常接受（空串 = 退回深色）。
        for loose in ('HADES', ' Athena ', 'venus ', ''):
            self.assertTrue(self.api.configure({'widgets': ['cpu'], 'theme': loose})['ok'], loose)

    def test_a_saved_config_without_a_theme_falls_back_instead_of_breaking(self):
        """旧 settings.json 没有 theme 键，升级后必须还能打开，退回深色。"""
        (self.api._dir / 'settings.json').write_text(
            '{"widgets": ["cpu"], "interval": 5}', encoding='utf-8')
        other = Api(self.temp.name, self.core, sampler=self.sampler,
                    sources=Mock(read=Mock(return_value={})))
        self.assertEqual(other.bootstrap()['config']['theme'], 'hades')
        self.assertEqual(other.bootstrap()['config']['widgets'], ['cpu'])

    def test_two_themes_produce_different_pixels_without_touching_usb(self):
        ids = ['clock_big', 'cpu', 'mem', 'temp', 'battery']
        self.assertTrue(self.api.configure({'widgets': ids})['ok'])
        state = {'clock': '12:34:56', 'cpu_load': 20, 'mem_pct': 50, 'temp_c': 45}
        shots = {}
        for name in ('hades', 'selene', 'venus', 'athena'):
            data, img = self.api._renderer.render(state, widgets=ids, theme=name)
            self.assertEqual(img.size, (480, 360))
            self.assertFalse(data.startswith(b'\xff\xc2'))
            shots[name] = data
        self.assertEqual(len(set(shots.values())), 4, '四套配色必须渲染出四份不同的画面')
        # 浅色主题的左上角必须比深色主题亮：这是「银/白」和「黑/灰」最直接的差别。
        def corner(name):
            _, img = self.api._renderer.render(state, widgets=ids, theme=name)
            return sum(img.convert('RGB').getpixel((4, 4)))
        self.assertGreater(corner('venus'), corner('hades'))
        self.assertGreater(corner('athena'), corner('selene'))
        self.core.node_run.assert_not_called()

    def test_the_chosen_theme_reaches_the_preview_frames(self):
        """配色必须真的进到发出去的 JPEG，而不是只存在配置里。"""
        self.assertTrue(self.api.configure({'widgets': ['cpu'], 'theme': 'venus'})['ok'])
        self.api.preview()
        self.wait()
        self.assertEqual(len(self.api.snapshot()['previews']), 1)
        _, light = self.api._renderer.render({}, widgets=['cpu'], theme='venus')
        self.assertGreater(sum(light.convert('RGB').getpixel((4, 4)), 0), 600)
        self.core.node_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
