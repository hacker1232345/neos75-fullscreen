from pathlib import Path
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
from data_sources import Weather, Codex, location, search_cities, event_state

CITY = dict(label='测试城市', latitude=30.2, longitude=120.1)


class WeatherTests(unittest.TestCase):
    def test_unconfigured_never_contacts_network(self):
        fetch = Mock(side_effect=AssertionError('unexpected network'))
        self.assertEqual(Weather(fetch).read(None)['status'], 'unconfigured')
        fetch.assert_not_called()

    def test_location_rejects_invalid_coordinates_and_names(self):
        for change in [dict(latitude=True), dict(longitude=float('nan')), dict(latitude=91), dict(label='')]:
            with self.assertRaises(ValueError):
                location({**CITY, **change})

    def test_city_results_preserve_choice_and_encode_query(self):
        fetch = Mock(return_value={'results': [dict(name='杭州', admin1='浙江', country='中国', latitude=30.2, longitude=120.1),
                                              dict(name='杭州', admin1='其他地区', latitude=31.0, longitude=119.0)]})
        rows = search_cities('杭州', fetch)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['latitude'], rows[1]['latitude'])
        self.assertIn('%E6%9D%AD%E5%B7%9E', fetch.call_args.args[0])

    def test_fetch_does_not_block_rendering_then_caches(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(url):
            entered.set()
            release.wait(2)
            return {'current': dict(temperature_2m=0, weather_code=0, time='2026-11-03T12:00')}
        weather = Weather(fetch)
        self.assertEqual(weather.read(CITY)['status'], 'loading')
        self.assertTrue(entered.wait(1))
        release.set()
        until = time.monotonic() + 2
        while weather.pending and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(weather.read(CITY)['temperature'], 0)
        self.assertEqual(weather.read(CITY)['status'], 'ok')

    def test_outage_marks_cache_then_removes_expired_temperature(self):
        weather = Weather(Mock(side_effect=OSError('offline')), clock=lambda: 2000)
        key = (CITY['latitude'], CITY['longitude'])
        weather.cache[key] = dict(updated_at=1000, temperature=22, label='晴')
        weather.next_try[key] = 10000
        weather.errors.add(key)
        self.assertEqual(weather.read(CITY)['status'], 'stale')
        weather.clock = lambda: 5000
        value = weather.read(CITY)
        self.assertEqual(value['status'], 'unavailable')
        self.assertIsNone(value['temperature'])


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.folder = self.home / 'sessions/2026/11/03'
        self.folder.mkdir(parents=True)
        self.now = 1800000000
        self.source = Codex(self.home, clock=lambda: self.now)

    def write(self, events, name='rollout-abcdefgh.jsonl'):
        path = self.folder / name
        path.write_text(''.join(json.dumps(dict(timestamp=dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(),
                                                  type='event_msg', payload=dict(type=kind))) + '\n' for kind, stamp in events))
        os.utime(path, (self.now, self.now))
        return path

    def test_explicit_start_complete_and_interrupted(self):
        for event, expected in [('task_started', 'running'), ('task_complete', 'completed'), ('turn_aborted', 'interrupted')]:
            self.write([(event, self.now - 5)])
            self.assertEqual(self.source.read()['state'], expected)

    def test_silence_never_becomes_success(self):
        self.write([('task_started', self.now - 300)])
        self.assertEqual(self.source.read()['state'], 'unknown')

    def test_newest_turn_wins_over_old_completion(self):
        self.write([('task_complete', self.now - 50), ('task_started', self.now - 2)])
        self.assertEqual(self.source.read()['state'], 'running')

    def test_thread_filter_does_not_leak_other_task(self):
        self.write([('task_complete', self.now - 1)])
        self.assertEqual(self.source.read('different-thread')['state'], 'unavailable')

    def test_partial_and_large_records_fall_back_to_activity(self):
        path = self.write([])
        path.write_bytes(b'{"type":"response_item","private":"' + b'x' * 2000000)
        os.utime(path, (self.now, self.now))
        result = self.source.read()
        self.assertEqual(result['state'], 'activity')
        self.assertNotIn('private', json.dumps(result))

    def test_documented_app_server_status_mapping(self):
        self.assertEqual(event_state({'method': 'thread/status/changed', 'params': {'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}}}), 'waiting')
        self.assertEqual(event_state({'method': 'turn/completed', 'params': {'turn': {'status': 'failed'}}}), 'failed')
        self.assertEqual(event_state({'method': 'turn/completed', 'params': {'turn': {'status': 'interrupted'}}}), 'interrupted')

    def test_explicit_bridge_and_staleness(self):
        bridge = self.home / 'neos75-progress.json'
        bridge.write_text(json.dumps(dict(state='waiting', updated_at=self.now - 3, thread_id='abcdefgh')))
        self.assertEqual(self.source.read('abcdefgh')['state'], 'waiting')
        bridge.write_text(json.dumps(dict(state='completed', updated_at=self.now - 300)))
        self.assertNotEqual(self.source.read()['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
