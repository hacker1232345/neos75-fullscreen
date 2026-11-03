"""Exercise telemetry semantics and fail-before-USB CLI gate; no real data sources."""
import contextlib
import io
import unittest
import stream


class TelemetryTests(unittest.TestCase):
    def test_unknown_values_and_expired_sources_are_not_fabricated(self):
        value = stream.rows({'clock':'12:34:56'}, {'weather':{'status':'unavailable','temperature':25},
                                                'codex':{'state':'unknown','age':121}})
        self.assertIn('N/A', value[0])
        self.assertEqual(value[1], 'WX N/A')
        self.assertEqual(value[2], 'CODEX UNKNOWN 121s')
        self.assertTrue(all(v.isascii() and len(v) <= 47 for v in value))

    def test_stale_weather_and_explicit_codex_state(self):
        value = stream.rows({'cpu_load':42,'temp_c':55,'mem_pct':50,'bat_pct':80,'clock':'12:34:56'},
                            {'weather':{'status':'stale','temperature':16.3,'label':'雨'},
                             'codex':{'state':'waiting','age':3}})
        self.assertEqual(value[0], 'CPU 42% TEMP 55C RAM 50%')
        self.assertEqual(value[1], 'WX 16.3C RAIN STALE')
        self.assertEqual(value[2], 'CODEX WAITING 3s')
        with self.assertRaises(ValueError):
            stream.rows({'clock':'中文'}, {})

    def test_unconfirmed_send_stops_before_importing_sampler(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            stream.main(['--send'])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
