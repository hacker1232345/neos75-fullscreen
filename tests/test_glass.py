"""UI orchestration acceptance with a fake device; never opens USB."""
from pathlib import Path
import json
import sys
import tempfile
import threading
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
        self.api = Api(self.temp.name, self.core, sampler=self.sampler)

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

    def test_unknown_firmware_is_backed_up_but_not_written(self):
        self.report.update(patchable=False, firmwareKind='unsupported-version')
        self.api.patch(True)
        self.wait()
        self.core.generate.assert_not_called()
        self.core.maintain.assert_not_called()
        self.assertEqual(self.api.snapshot()['state']['phase'], 'error')
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
        self.assertEqual(other.bootstrap()['config'], desired)
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


if __name__ == '__main__':
    unittest.main()
