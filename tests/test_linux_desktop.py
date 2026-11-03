from pathlib import Path
import importlib.util
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'desktop'))
import glass
spec = importlib.util.spec_from_file_location('native_build', ROOT / 'desktop/build_exe.py')
native_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_build)


class LinuxDesktopSafety(unittest.TestCase):
    def test_linux_bundle_keeps_default_hidraw_and_libusb_backends(self):
        names = native_build.hid_prebuilds('linux', 'x64')
        self.assertIn('HID_hidraw-linux-x64', names)
        self.assertIn('HID-linux-x64', names)
        self.assertNotIn('HID_hidraw-linux-x64-musl', names)
        self.assertNotIn('HID-win32-x64', names)
        self.assertEqual(native_build.hid_prebuilds('darwin', 'arm64'), {'HID-darwin-arm64'})

    def test_root_gui_stops_before_loading_webview_or_starting_tasks(self):
        with patch.object(glass.sys, 'platform', 'linux'), patch.object(glass.os, 'geteuid', return_value=0, create=True), patch.object(glass, 'Api') as api:
            with self.assertRaisesRegex(RuntimeError, '普通用户'):
                glass.run()
            api.assert_not_called()

    def test_usb_rule_only_targets_known_application_and_dfu_ids(self):
        lines = [line for line in (ROOT / 'desktop/linux/70-neos75.rules').read_text().splitlines() if line and not line.startswith('#')]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIn('"5542"', line)
            self.assertTrue('"0001"' in line or '"1000"' in line)
            self.assertIn('TAG+="uaccess"', line)
            self.assertNotIn('0666', line)


if __name__ == '__main__':
    unittest.main()
