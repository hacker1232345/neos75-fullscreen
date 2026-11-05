"""The four-row ASCII text channel: pure offline checks, never opens USB."""
from pathlib import Path
import importlib.util
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))

spec = importlib.util.spec_from_file_location('native_rows', ROOT / 'dashboard' / 'native_rows.py')
rows_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rows_module)

STATE = dict(clock='22:07:40', date='2026-11-03', host='DESKTOP-7F2', ip='192.168.124.26',
             uptime='1d 06:28', cpu_load=42.0, cpu_freq_ghz=1.90, mem_pct=82.9, mem_used_gb=26.1,
             mem_total_gb=31.5, temp_c=None, gpu_pct=75, disk_free_gb=225.5,
             net_down_mbps=0.03, net_up_mbps=0.10, bat_pct=78, bat_ac=True, vol_pct=100,
             top_proc='DeepSeek Harness', top_proc_pct=16.8,
             weather={'status': 'unconfigured'}, codex={'state': 'completed', 'age': 12})


class TextFolding(unittest.TestCase):
    def test_printable_only_and_folded(self):
        # A Chinese process name must not leak bytes the firmware cannot store.
        self.assertEqual(rows_module.ascii_text('中文进程名 test'), 'test')
        self.assertEqual(rows_module.ascii_text('a\n b\t\tc'), 'a b c')
        self.assertEqual(len(rows_module.ascii_text('x' * 80)), rows_module.MAX_TEXT)

    def test_numbers_reject_boolean_and_missing(self):
        self.assertIsNone(rows_module.number(None))
        self.assertIsNone(rows_module.number(True))
        self.assertIsNone(rows_module.number(float('nan')))
        self.assertEqual(rows_module.number(42.0, 0, '%'), '42%')
        self.assertEqual(rows_module.number(1.9, 1, 'G'), '1.9G')


class RowPacking(unittest.TestCase):
    def test_rows_are_always_four_and_bounded(self):
        rows, omitted = rows_module.rows_from_state(STATE, ['clock_big', 'date', 'cpu', 'mem'])
        self.assertEqual(len(rows), 4)
        self.assertEqual(omitted, [])
        for row in rows:
            self.assertLessEqual(len(row), rows_module.MAX_TEXT)
            self.assertEqual(row, row.encode('ascii').decode('ascii'))

    def test_chart_widget_is_reported_not_silently_dropped(self):
        rows, omitted = rows_module.rows_from_state(STATE, ['history_cpu', 'cpu'])
        self.assertEqual(omitted, ['history_cpu'])
        self.assertTrue(any('CPU' in row for row in rows))

    def test_overflow_is_marked_in_the_last_row(self):
        everything = list(rows_module.TEXT_WIDGETS)
        rows, _ = rows_module.rows_from_state(STATE, everything)
        self.assertEqual(len(rows), 4)
        self.assertRegex(rows[-1], r' \+\d+$')
        self.assertLessEqual(len(rows[-1]), rows_module.MAX_TEXT)

    def test_missing_sensor_is_not_invented(self):
        rows, _ = rows_module.rows_from_state(STATE, ['temp'])
        self.assertIn('TEMP n/a', ' '.join(rows))
        rows, _ = rows_module.rows_from_state({}, ['cpu'])
        self.assertIn('CPU n/a', ' '.join(rows))

    def test_focus_utc_and_host_values_fit_and_hold_missing_states(self):
        widgets = ['focus_timer', 'clock_utc', 'day_progress', 'mem_available']
        state = dict(clock='23:59:59', clock_utc='15:59', focus_remaining=301, mem_available_gb=6.25)
        rows, omitted = rows_module.rows_from_state(state, widgets)
        self.assertFalse(omitted)
        self.assertIn('FOCUS 05:01', ' '.join(rows))
        self.assertIn('UTC 15:59', ' '.join(rows))
        self.assertIn('DAY 99%', ' '.join(rows))
        self.assertIn('RAM FREE 6.2G', ' '.join(rows))
        self.assertTrue(all(len(line.encode('ascii')) <= 31 for line in rows))
        missing, _ = rows_module.rows_from_state({}, widgets)
        self.assertIn('FOCUS idle', ' '.join(missing))
        self.assertIn('RAM FREE n/a', ' '.join(missing))

    def test_duplicate_widgets_are_counted_once(self):
        rows, _ = rows_module.rows_from_state(STATE, ['cpu', 'cpu'])
        self.assertEqual(sum(row.count('CPU') for row in rows), 1)

    def test_weather_and_codex_report_their_state(self):
        rows, _ = rows_module.rows_from_state(STATE, ['weather', 'codex'])
        text = ' '.join(rows)
        self.assertIn('WX n/a', text)
        self.assertIn('CODEX completed', text)
        weather = dict(STATE, weather={'status': 'ok', 'temperature': 18, 'humidity': 62})
        rows, _ = rows_module.rows_from_state(weather, ['weather'])
        self.assertIn('WX 18C 62%', ' '.join(rows))


class CommandLine(unittest.TestCase):
    def test_list_matches_the_registry(self):
        self.assertEqual(rows_module.main(['--list']), 0)

    def test_cli_reads_a_state_file(self):
        import tempfile, json, io, contextlib
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text(json.dumps(STATE), encoding='utf-8')
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(rows_module.main(['--in', str(path), '--widgets', 'cpu,mem']), 0)
            value = json.loads(buffer.getvalue())
            self.assertEqual(len(value['rows']), 4)
            self.assertEqual(value['omitted'], [])


if __name__ == '__main__':
    unittest.main()
